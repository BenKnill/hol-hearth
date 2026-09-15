"""Frozen mechanical broker for one restored HOL fork basis."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import select
import selectors
import socket
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from hol_workbench.fork_broker_protocol import (
    BROKER_CONTROL_SCHEMA,
    BROKER_DESCRIBE_SCHEMA,
    BROKER_EVENT_SCHEMA,
    BROKER_FORK_SNAPSHOT_ABI,
    BROKER_FRAME_CONTROL,
    BROKER_PROTOCOL,
    BROKER_TRANSPORT_SAFETY_TIMEOUT_SECONDS,
    BrokerProtocolError,
    broker_runtime_sha256,
    receive_control,
    receive_frame,
    send_control,
    send_raw_output,
)
from hol_workbench.proof_run_fork_basis_safety import admit_single_threaded_basis
from hol_workbench.proof_run_fork_broker_children import (
    ActiveBrokerChildren,
    _atomic_json,
    acknowledge_registered_child,
    process_birth_identity,
    process_group_is_alive,
    process_start_ticks,
    reap_orphaned_children,
    terminate_child,
    wait_for_quiescence,
)

BROKER_READY_SCHEMA = "proof-run.fork-broker-ready.v1"
BROKER_READY_PREFIX = "__PROOF_RUN_FORK_BROKER_READY__:"
BROKER_PRELOAD_OK_PREFIX = "__PROOF_RUN_FORK_BROKER_PRELOAD_OK__:"
BROKER_RAW_CHUNK_BYTES = 64 * 1024
BROKER_CHILD_QUIESCENCE_SECONDS = 2.0


def _basis_preload_phrase(preload: Path, token: str) -> bytes:
    path = json.dumps(str(preload))
    marker = json.dumps(token)
    return (
        f"if Toploop.use_file Format.std_formatter {path} "
        "then (loaded_files := "
        f"(Filename.basename {path},Digest.file {path})::!loaded_files; "
        f"print_endline {marker}) "
        "else exit 78;;"
    ).encode()


def _identity(pid: int, *, pgid: int | None = None) -> dict[str, Any]:
    return {
        "pid": pid,
        "pgid": pgid if pgid is not None else pid,
        "start_ticks": process_start_ticks(pid),
        "birth_identity": process_birth_identity(pid),
    }


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _request_fields(request: dict[str, Any], allowed: set[str]) -> None:
    extras = sorted(set(request) - allowed)
    if extras:
        raise BrokerProtocolError("unknown broker control fields: " + ", ".join(extras))


class ForkBasisBroker:
    def __init__(self, args: argparse.Namespace) -> None:
        self.pool_dir = Path(args.pool_dir).resolve()
        self.holdir = Path(args.holdir).resolve()
        self.cwd = Path(args.cwd).resolve()
        self.session_dirs = [Path(item).resolve() for item in json.loads(args.session_dirs_json)]
        self.socket_paths = [Path(item).resolve() for item in json.loads(args.socket_paths_json)]
        self.startup_argv = json.loads(args.startup_argv_json)
        self.environment = {str(key): str(value) for key, value in json.loads(args.environment_json).items()}
        self.preloads = [Path(item).resolve() for item in json.loads(args.preloads_json)]
        self.preload_timeout = args.preload_timeout
        self.spawn_timeout = float(args.fork_spawn_timeout)
        self.profile_basis_id = str(args.profile_basis_id)
        self.fork_snapshot_abi = str(args.fork_snapshot_abi)
        self.broker_protocol = str(args.broker_protocol)
        self.broker_runtime_sha256 = str(args.broker_runtime_sha256)
        self.allow_non_linux_test_basis = bool(args.allow_non_linux_test_basis)
        if len(self.session_dirs) != len(self.socket_paths) or not self.session_dirs:
            raise RuntimeError("broker requires matching non-empty session and endpoint inventories")
        if len(set(self.session_dirs)) != len(self.session_dirs) or len(set(self.socket_paths)) != len(
            self.socket_paths
        ):
            raise RuntimeError("broker session and endpoint inventories must be unique")
        if self.broker_protocol != BROKER_PROTOCOL:
            raise RuntimeError("broker protocol argument does not match the implementation")
        if self.fork_snapshot_abi != BROKER_FORK_SNAPSHOT_ABI:
            raise RuntimeError("broker snapshot ABI argument does not match the implementation")
        if self.broker_runtime_sha256 != broker_runtime_sha256():
            raise RuntimeError("broker runtime argument does not match the executing bundle")
        if not self.profile_basis_id:
            raise RuntimeError("broker mathematical basis identity is empty")
        self.stop_event = threading.Event()
        self.parent_lock = threading.Lock()
        self.session_locks = {str(path): threading.Lock() for path in self.session_dirs}
        self.registry = ActiveBrokerChildren(self.pool_dir)
        self.listeners: list[socket.socket] = []
        self.handlers: set[threading.Thread] = set()
        self.handlers_lock = threading.Lock()
        self.active_attempt_ids: set[str] = set()
        self.active_attempts_lock = threading.Lock()
        self.basis: subprocess.Popen[bytes] | None = None
        self.basis_identity: dict[str, Any] | None = None
        self.raw_log = self.pool_dir / "fork-basis" / "basis.raw.log"
        self.ready_json = self.pool_dir / "fork-worker-ready.json"
        self.selector: selectors.BaseSelector | None = None
        self.parent_pending = b""

    def _append_parent_raw(self, payload: bytes) -> None:
        self.raw_log.parent.mkdir(parents=True, exist_ok=True)
        with self.raw_log.open("ab") as handle:
            handle.write(payload)

    def _send_parent(self, payload: bytes) -> None:
        assert self.basis is not None and self.basis.stdin is not None
        self.basis.stdin.write(payload)
        self.basis.stdin.flush()

    def _read_parent_until(self, tokens: tuple[bytes, ...], *, timeout: float, purpose: str) -> list[bytes]:
        assert self.basis is not None and self.selector is not None
        deadline = time.monotonic() + timeout
        lines: list[bytes] = []
        while True:
            if self.basis.poll() is not None:
                raise RuntimeError(f"HOL basis exited during {purpose}: {self.basis.returncode}")
            if any(any(token in line for token in tokens) for line in lines):
                return lines
            now = time.monotonic()
            if now >= deadline:
                raise RuntimeError(f"HOL basis timed out during {purpose}")
            for key, _ in self.selector.select(timeout=min(0.1, deadline - now)):
                chunk = os.read(key.fd, BROKER_RAW_CHUNK_BYTES)
                if not chunk:
                    continue
                self._append_parent_raw(chunk)
                self.parent_pending += chunk
                while b"\n" in self.parent_pending:
                    line, self.parent_pending = self.parent_pending.split(b"\n", 1)
                    lines.append(line)

    def _start_basis(self) -> None:
        self.raw_log.parent.mkdir(parents=True, exist_ok=True)
        self.basis = subprocess.Popen(
            self.startup_argv,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=self.environment,
            close_fds=True,
            start_new_session=True,
        )
        assert self.basis.stdout is not None
        self.basis_identity = _identity(self.basis.pid)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.basis.stdout, selectors.EVENT_READ)
        ready_token = f"{BROKER_READY_PREFIX}{os.getpid():x}".encode()
        lines = [b'#load "unix.cma";;', b"Sys.set_signal Sys.sigchld Sys.Signal_ignore;;"]
        for preload in self.preloads:
            token = f"{BROKER_PRELOAD_OK_PREFIX}{preload.name}"
            lines.append(_basis_preload_phrase(preload, token))
        lines.append(b"print_endline " + json.dumps(ready_token.decode()).encode() + b";;")
        self._send_parent(b"\n".join(lines) + b"\n")
        timeout = float(self.preload_timeout) if self.preloads and self.preload_timeout else 60.0
        observed = self._read_parent_until((ready_token,), timeout=timeout, purpose="basis preload")
        for preload in self.preloads:
            token = f"{BROKER_PRELOAD_OK_PREFIX}{preload.name}".encode()
            if not any(token in line for line in observed):
                raise RuntimeError(f"broker basis preload did not complete: {preload}")
        admit_single_threaded_basis(
            self.basis.pid,
            allow_non_linux_test_basis=self.allow_non_linux_test_basis,
        )

    def _description(self, *, nonce: str, endpoint: Path, session_dir: Path) -> dict[str, Any]:
        assert self.basis is not None
        broker = _identity(os.getpid(), pgid=os.getpgrp())
        basis = _identity(self.basis.pid)
        generation_payload = {
            "broker_protocol": self.broker_protocol,
            "broker_runtime_sha256": self.broker_runtime_sha256,
            "fork_snapshot_abi": self.fork_snapshot_abi,
            "profile_basis_id": self.profile_basis_id,
            "endpoint": str(endpoint),
            "session_dir": str(session_dir),
            "broker": broker,
            "basis": basis,
        }
        return {
            "schema": BROKER_DESCRIBE_SCHEMA,
            "status": "described",
            "nonce": nonce,
            **generation_payload,
            "generation_sha256": _canonical_sha256(generation_payload),
            "claim_boundary": (
                "fresh nonce identifies the responding endpoint; implementation confidence comes from "
                "the exact broker runtime bundle and behavioral tests"
            ),
        }

    def _spawn_child(self, request: dict[str, Any]) -> tuple[dict[str, Any], Path]:
        _request_fields(
            request,
            {
                "schema",
                "operation",
                "handshake_nonce",
                "attempt_id",
                "script_b64",
                "spawn_token",
                "refusal_token",
                "ack_path",
                "ownership_path",
                "output_path",
            },
        )
        attempt_id = str(request.get("attempt_id") or "")
        spawn_token = str(request.get("spawn_token") or "")
        refusal_token = str(request.get("refusal_token") or "")
        if not attempt_id or not spawn_token or not refusal_token:
            raise BrokerProtocolError("spawn identity and mechanical tokens must be non-empty")
        try:
            script = base64.b64decode(str(request.get("script_b64") or ""), validate=True)
        except (ValueError, TypeError) as exc:
            raise BrokerProtocolError("spawn script is not valid base64") from exc
        if not script:
            raise BrokerProtocolError("spawn script is empty")
        ack_path = Path(str(request.get("ack_path") or ""))
        ownership_path = Path(str(request.get("ownership_path") or ""))
        output_path = Path(str(request.get("output_path") or ""))
        if not all(path.is_absolute() for path in (ack_path, ownership_path, output_path)):
            raise BrokerProtocolError("spawn lifecycle and output paths must be absolute")
        spawn_bytes = spawn_token.encode()
        refusal_bytes = refusal_token.encode()
        with self.parent_lock:
            assert self.basis is not None
            admit_single_threaded_basis(
                self.basis.pid,
                allow_non_linux_test_basis=self.allow_non_linux_test_basis,
            )
            self._send_parent(script)
            lines = self._read_parent_until(
                (spawn_bytes, refusal_bytes),
                timeout=self.spawn_timeout,
                purpose=f"fork spawn {attempt_id}",
            )
        for line in lines:
            if refusal_bytes in line:
                detail = line.split(refusal_bytes, 1)[1].decode("utf-8", errors="replace")
                raise RuntimeError(f"HOL basis refused fork: {detail}")
            if spawn_bytes not in line:
                continue
            try:
                pid = int(line.split(spawn_bytes, 1)[1].strip())
            except ValueError as exc:
                raise RuntimeError("fork spawn token did not contain a PID") from exc
            child = acknowledge_registered_child(
                self.registry,
                attempt_id=attempt_id,
                pid=pid,
                ack_path=ack_path,
                ownership_path=ownership_path,
                timeout_seconds=self.spawn_timeout,
            )
            return child, output_path
        raise RuntimeError("fork spawn token was not observed")

    @staticmethod
    def _read_new_output(path: Path, offset: int) -> tuple[bytes, int]:
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                payload = handle.read(BROKER_RAW_CHUNK_BYTES)
        except OSError:
            return b"", offset
        return payload, offset + len(payload)

    def _serve_attempt(
        self,
        conn: socket.socket,
        request: dict[str, Any],
        *,
        description: dict[str, Any],
    ) -> None:
        attempt_id = str(request.get("attempt_id") or "")
        prior_child_reaping: list[dict[str, Any]] = []
        with self.active_attempts_lock:
            if attempt_id in self.active_attempt_ids:
                raise RuntimeError("broker already owns this active attempt")
            if not self.active_attempt_ids:
                prior_child_reaping = reap_orphaned_children(self.registry)
            self.active_attempt_ids.add(attempt_id)
        child: dict[str, Any] | None = None
        try:
            child, output_path = self._spawn_child(request)
            send_control(
                conn,
                {
                    "schema": BROKER_EVENT_SCHEMA,
                    "status": "spawned",
                    "attempt_id": child["attempt_id"],
                    "child": child,
                    "prior_child_reaping": prior_child_reaping,
                    "generation_sha256": description["generation_sha256"],
                },
            )
            offset = 0
            termination = "child_exited"
            cleanup: dict[str, Any] | None = None
            while True:
                payload, offset = self._read_new_output(output_path, offset)
                if payload:
                    send_raw_output(conn, payload)
                if not process_group_is_alive(int(child["pgid"])):
                    cleanup = {
                        "status": "quiescent",
                        "verified_quiescent": wait_for_quiescence(
                            child,
                            timeout_seconds=BROKER_CHILD_QUIESCENCE_SECONDS,
                        ),
                    }
                    break
                readable, _, _ = select.select([conn], [], [], 0.05)
                if not readable:
                    continue
                try:
                    frame_type, raw = receive_frame(conn)
                except EOFError:
                    termination = "controller_disconnected"
                    cleanup = terminate_child(child)
                    break
                if frame_type != BROKER_FRAME_CONTROL:
                    termination = "malformed_control"
                    cleanup = terminate_child(child)
                    raise BrokerProtocolError("controller sent raw output to the broker")
                try:
                    control = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    termination = "malformed_control"
                    cleanup = terminate_child(child)
                    raise BrokerProtocolError(f"malformed attempt control: {exc}") from exc
                if not isinstance(control, dict) or control.get("operation") != "cancel":
                    termination = "unknown_attempt_operation"
                    cleanup = terminate_child(child)
                    raise BrokerProtocolError("only explicit cancel is accepted while a child is active")
                _request_fields(control, {"schema", "operation", "attempt_id"})
                if control.get("schema") != BROKER_CONTROL_SCHEMA or control.get("attempt_id") != child["attempt_id"]:
                    termination = "mismatched_cancel"
                    cleanup = terminate_child(child)
                    raise BrokerProtocolError("cancel does not identify the active attempt")
                termination = "explicit_cancel"
                cleanup = terminate_child(child)
                break
            while True:
                payload, new_offset = self._read_new_output(output_path, offset)
                if not payload:
                    break
                send_raw_output(conn, payload)
                offset = new_offset
            verified = bool((cleanup or {}).get("verified_quiescent")) and not process_group_is_alive(
                int(child["pgid"])
            )
            if verified:
                self.registry.unregister(int(child["pgid"]))
            send_control(
                conn,
                {
                    "schema": BROKER_EVENT_SCHEMA,
                    "status": "completed" if verified else "cleanup_failed",
                    "attempt_id": child["attempt_id"],
                    "termination": termination,
                    "child": child,
                    "child_cleanup": cleanup,
                    "child_quiescent": verified,
                    "seat_reusable": verified,
                    "output_bytes": offset,
                    "prior_child_reaping": prior_child_reaping,
                    "generation_sha256": description["generation_sha256"],
                },
            )
        except BaseException:
            if child is not None:
                cleanup = terminate_child(child)
                if cleanup.get("verified_quiescent") and not process_group_is_alive(int(child["pgid"])):
                    self.registry.unregister(int(child["pgid"]))
            raise
        finally:
            with self.active_attempts_lock:
                self.active_attempt_ids.discard(attempt_id)

    def _handle_connection(self, conn: socket.socket, *, endpoint: Path, session_dir: Path) -> None:
        session_lock = self.session_locks[str(session_dir)]
        conn.settimeout(BROKER_TRANSPORT_SAFETY_TIMEOUT_SECONDS)
        with conn:
            try:
                describe = receive_control(conn)
                _request_fields(describe, {"schema", "operation", "nonce"})
                if (
                    describe.get("schema") != BROKER_CONTROL_SCHEMA
                    or describe.get("operation") != "describe_runtime"
                    or not isinstance(describe.get("nonce"), str)
                    or not describe["nonce"]
                ):
                    raise BrokerProtocolError("first broker operation must be nonce-bearing describe_runtime")
                description = self._description(
                    nonce=str(describe["nonce"]),
                    endpoint=endpoint,
                    session_dir=session_dir,
                )
                send_control(conn, description)
                request = receive_control(conn)
                if request.get("schema") != BROKER_CONTROL_SCHEMA:
                    raise BrokerProtocolError("broker operation uses the wrong control schema")
                if request.get("operation") == "status":
                    _request_fields(request, {"schema", "operation", "handshake_nonce"})
                    if request.get("handshake_nonce") != describe["nonce"]:
                        raise BrokerProtocolError("status is not bound to this handshake")
                    send_control(
                        conn,
                        {
                            "schema": BROKER_EVENT_SCHEMA,
                            "status": "stopping" if self.stop_event.is_set() else "ready",
                            "active_children": self.registry.snapshot(),
                            "generation_sha256": description["generation_sha256"],
                        },
                    )
                    return
                if request.get("operation") == "stop":
                    _request_fields(request, {"schema", "operation", "handshake_nonce"})
                    if request.get("handshake_nonce") != describe["nonce"]:
                        raise BrokerProtocolError("stop is not bound to this handshake")
                    self.stop_event.set()
                    send_control(
                        conn,
                        {
                            "schema": BROKER_EVENT_SCHEMA,
                            "status": "stopping",
                            "active_children": self.registry.snapshot(),
                            "generation_sha256": description["generation_sha256"],
                        },
                    )
                    return
                if request.get("operation") != "spawn":
                    raise BrokerProtocolError(f"unknown broker operation: {request.get('operation')!r}")
                if request.get("handshake_nonce") != describe["nonce"]:
                    raise BrokerProtocolError("spawn is not bound to this handshake")
                if not session_lock.acquire(blocking=False):
                    raise BrokerProtocolError("broker session already owns an active child")
                try:
                    self._serve_attempt(conn, request, description=description)
                finally:
                    session_lock.release()
            except EOFError:
                return
            except (BrokerProtocolError, RuntimeError, OSError, ValueError) as exc:
                with suppress(OSError):
                    send_control(
                        conn,
                        {
                            "schema": BROKER_EVENT_SCHEMA,
                            "status": "error",
                            "message": str(exc),
                            "active_children": self.registry.snapshot(),
                        },
                    )

    def _publish_ready(self) -> None:
        assert self.basis is not None
        broker_identity = _identity(os.getpid(), pgid=os.getpgrp())
        basis_identity = _identity(self.basis.pid)
        for index, (session_dir, endpoint) in enumerate(zip(self.session_dirs, self.socket_paths, strict=True), 1):
            session_dir.mkdir(parents=True, exist_ok=True)
            _atomic_json(
                session_dir / "session.json",
                {
                    "schema": "proof-run.warm-session.v1",
                    "session_id": session_dir.name,
                    "session_dir": str(session_dir),
                    "status": "ready",
                    "holdir": str(self.holdir),
                    "cwd": str(self.cwd),
                    "socket": str(endpoint),
                    "control_session_dir": str(session_dir),
                    "worker_pid": broker_identity["pid"],
                    "worker_pgid": broker_identity["pgid"],
                    "worker_start_ticks": broker_identity["start_ticks"],
                    "worker_identity": broker_identity["birth_identity"],
                    "hol_pid": basis_identity["pid"],
                    "basis_pid": basis_identity["pid"],
                    "basis_pgid": basis_identity["pgid"],
                    "basis_start_ticks": basis_identity["start_ticks"],
                    "basis_identity": basis_identity["birth_identity"],
                    "pool_dir": str(self.pool_dir),
                    "active_children_registry": str(self.registry.path),
                    "engine": "fork_basis",
                    "execution_topology": "mechanical_basis_broker_v3",
                    "fork_snapshot_abi": self.fork_snapshot_abi,
                    "broker_protocol": self.broker_protocol,
                    "broker_runtime_sha256": self.broker_runtime_sha256,
                    "profile_basis_id": self.profile_basis_id,
                    "worker_index": index,
                },
            )
        _atomic_json(
            self.ready_json,
            {
                "schema": BROKER_READY_SCHEMA,
                "status": "ready",
                "pool_dir": str(self.pool_dir),
                "worker_pid": broker_identity["pid"],
                "worker_pgid": broker_identity["pgid"],
                "worker_start_ticks": broker_identity["start_ticks"],
                "worker_identity": broker_identity["birth_identity"],
                "basis_pid": basis_identity["pid"],
                "basis_pgid": basis_identity["pgid"],
                "basis_start_ticks": basis_identity["start_ticks"],
                "basis_identity": basis_identity["birth_identity"],
                "session_dirs": [str(path) for path in self.session_dirs],
                "socket_paths": [str(path) for path in self.socket_paths],
                "execution_topology": "mechanical_basis_broker_v3",
                "fork_snapshot_abi": self.fork_snapshot_abi,
                "broker_protocol": self.broker_protocol,
                "broker_runtime_sha256": self.broker_runtime_sha256,
                "profile_basis_id": self.profile_basis_id,
                "raw_log": str(self.raw_log),
            },
        )

    def run(self) -> int:
        try:
            self.pool_dir.mkdir(parents=True, exist_ok=True)
            self._start_basis()
            for session_dir, endpoint in zip(self.session_dirs, self.socket_paths, strict=True):
                session_dir.mkdir(parents=True, exist_ok=True)
                endpoint.parent.mkdir(parents=True, exist_ok=True)
                endpoint.unlink(missing_ok=True)
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(endpoint))
                listener.listen(16)
                self.listeners.append(listener)

                def serve(
                    listener: socket.socket = listener,
                    endpoint: Path = endpoint,
                    session_dir: Path = session_dir,
                ) -> None:
                    while not self.stop_event.is_set():
                        try:
                            conn, _ = listener.accept()
                        except OSError:
                            return
                        handler = threading.Thread(
                            target=self._run_handler,
                            args=(conn, endpoint, session_dir),
                            daemon=True,
                        )
                        with self.handlers_lock:
                            self.handlers.add(handler)
                        handler.start()

                threading.Thread(target=serve, daemon=True).start()
            self._publish_ready()
            assert self.basis is not None
            while not self.stop_event.is_set() and self.basis.poll() is None:
                time.sleep(0.1)
            return 0 if self.stop_event.is_set() else int(self.basis.returncode or 1)
        except Exception as exc:
            print(f"proof-run fork basis broker failed: {exc}", file=sys.stderr)
            return 1
        finally:
            self.stop_event.set()
            for listener in self.listeners:
                with suppress(OSError):
                    listener.close()
            for endpoint in self.socket_paths:
                endpoint.unlink(missing_ok=True)
            for child in self.registry.snapshot():
                cleanup = terminate_child(child)
                if cleanup.get("verified_quiescent"):
                    self.registry.unregister(int(child["pgid"]))
            with self.handlers_lock:
                handlers = list(self.handlers)
            for handler in handlers:
                if handler is not threading.current_thread():
                    handler.join(timeout=1)
            if self.basis is not None and self.basis.poll() is None and self.basis_identity is not None:
                terminate_child(
                    {
                        "pid": self.basis.pid,
                        "pgid": self.basis.pid,
                        "process_identity": self.basis_identity["birth_identity"],
                        "process_start_ticks": self.basis_identity["start_ticks"],
                    },
                    timeout_seconds=3.0,
                )
            if self.selector is not None:
                self.selector.close()

    def _run_handler(self, conn: socket.socket, endpoint: Path, session_dir: Path) -> None:
        try:
            self._handle_connection(conn, endpoint=endpoint, session_dir=session_dir)
        finally:
            with self.handlers_lock:
                self.handlers.discard(threading.current_thread())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-dir", required=True)
    parser.add_argument("--holdir", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--session-dirs-json", required=True)
    parser.add_argument("--socket-paths-json", required=True)
    parser.add_argument("--startup-argv-json", required=True)
    parser.add_argument("--environment-json", required=True)
    parser.add_argument("--preloads-json", required=True)
    parser.add_argument("--preload-timeout", type=float)
    parser.add_argument("--fork-spawn-timeout", type=float, default=60.0)
    parser.add_argument("--profile-basis-id", required=True)
    parser.add_argument("--fork-snapshot-abi", required=True)
    parser.add_argument("--broker-protocol", required=True)
    parser.add_argument("--broker-runtime-sha256", required=True)
    parser.add_argument("--allow-non-linux-test-basis", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    return ForkBasisBroker(build_parser().parse_args(argv)).run()


if __name__ == "__main__":
    raise SystemExit(main())
