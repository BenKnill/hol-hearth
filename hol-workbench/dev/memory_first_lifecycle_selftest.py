#!/usr/bin/env python3
"""Executable stdlib contract for the memory-first process lifecycle."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

WORKBENCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKBENCH_ROOT))

from hol_workbench import (  # noqa: E402
    criu_shelf_admission as criu_shelf_admission_module,
)
from hol_workbench import (  # noqa: E402
    hot_profile_session,
    orbstack_idle_retirement,
)
from hol_workbench.cli import orbstack_criu_restore, prove_loop  # noqa: E402
from hol_workbench.fork_broker_protocol import (  # noqa: E402
    BROKER_FRAME_CONTROL,
    BROKER_FRAME_HEADER,
    BROKER_FRAME_RAW_OUTPUT,
    BrokerProtocolError,
    receive_frame,
    send_control,
    send_raw_output,
)
from hol_workbench.hot_profile_session import broker_response_proves_reusable  # noqa: E402
from hol_workbench.jsonio import atomic_write_json  # noqa: E402
from hol_workbench.pools import fork_attempt_lifecycle as fork_attempt_lifecycle_module  # noqa: E402
from hol_workbench.pools.fork_attempt_lifecycle import (  # noqa: E402
    ACTIVE_CHILDREN_SCHEMA,
    wait_for_fork_attempt_cleanup,
)
from hol_workbench.pools.leases import release_warm_pool_seat  # noqa: E402
from hol_workbench.pools.seat_lifecycle import (  # noqa: E402
    checkout_pool_seat_locked,
    finalize_fork_pool_seat_locked,
)
from hol_workbench.pools.session_lifecycle import (  # noqa: E402
    pool_session_alive,
    send_session_request,
    session_socket_path,
)
from hol_workbench.pools.store import (  # noqa: E402
    read_warm_pool,
    warm_pool_lock,
    write_warm_pool_json,
)
from hol_workbench.process_groups import (  # noqa: E402
    OwnedProcessSet,
    process_birth_identity,
    process_start_ticks,
    terminate_process_group,
)
from hol_workbench.proof_run_fork_broker_client import BufferedBrokerFrameReader  # noqa: E402
from hol_workbench.proof_run_fork_children import ForkSpawnGate  # noqa: E402


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def require_raises(error_type: type[BaseException], detail: str, action: Any) -> None:
    try:
        action()
    except error_type as exc:
        require(detail in str(exc), f"unexpected {error_type.__name__}: {exc}")
    else:
        raise AssertionError(f"expected {error_type.__name__}: {detail}")


def current_session(session_dir: Path) -> dict[str, Any]:
    pid = os.getpid()
    ticks = process_start_ticks(pid)
    identity = process_birth_identity(pid)
    require(ticks is not None, "current Linux process start ticks are unavailable")
    require(identity == f"linux-proc:{ticks}", "current Linux process birth identity is unavailable")
    return {
        "schema": "proof-run.warm-session.v1",
        "session_id": session_dir.name,
        "session_dir": str(session_dir),
        "status": "ready",
        "worker_pid": pid,
        "worker_pgid": os.getpgrp(),
        "worker_start_ticks": ticks,
        "worker_identity": identity,
        "hol_pid": pid,
        "basis_pid": pid,
        "basis_pgid": os.getpgrp(),
        "basis_start_ticks": ticks,
        "basis_identity": identity,
        "engine": "fork_basis",
        "execution_topology": "mechanical_basis_broker_v3",
    }


def test_process_identity(root: Path) -> None:
    session_dir = root / "identity-session"
    session_dir.mkdir()
    good = current_session(session_dir)
    atomic_write_json(session_dir / "session.json", good)
    item = {"session_dir": str(session_dir)}
    require(pool_session_alive(item), "exact current worker and basis identities were rejected")

    bad = dict(good)
    bad["worker_identity"] = "linux-proc:1"
    atomic_write_json(session_dir / "session.json", bad)
    require(not pool_session_alive(item), "worker birth-identity mismatch was accepted")

    bad = dict(good)
    bad["basis_start_ticks"] = int(good["basis_start_ticks"]) + 1
    atomic_write_json(session_dir / "session.json", bad)
    require(not pool_session_alive(item), "basis start-tick mismatch was accepted")

    bad = dict(good)
    bad["hol_pid"] = os.getpid() + 1
    atomic_write_json(session_dir / "session.json", bad)
    require(not pool_session_alive(item), "HOL PID alias mismatch was accepted")

    bad = dict(good)
    bad.pop("basis_identity")
    atomic_write_json(session_dir / "session.json", bad)
    require(not pool_session_alive(item), "missing basis identity was accepted")
    atomic_write_json(session_dir / "session.json", good)


def registry_payload(children: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema": ACTIVE_CHILDREN_SCHEMA, "children": children}


def test_registry_authority(root: Path) -> None:
    attempt_id = "attempt-a"
    registry = root / "active-children.json"
    transcript = root / "transcript.txt"
    transcript.write_text("client_disconnected; cleanup=quiescent\n", encoding="utf-8")
    missing = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.0,
    )
    require(missing["status"] == "unverified", "missing registry was accepted")

    registry.write_text("{malformed", encoding="utf-8")
    malformed = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.0,
    )
    require(malformed["status"] == "unverified", "malformed registry was accepted")

    ticks = process_start_ticks(os.getpid())
    identity = process_birth_identity(os.getpid())
    require(ticks is not None and identity is not None, "registry child identity is unavailable")
    child = {
        "attempt_id": attempt_id,
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "process_start_ticks": ticks,
        "process_identity": identity,
    }
    atomic_write_json(registry, registry_payload([child]))
    survivors = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.0,
    )
    require(survivors["status"] == "survivors", "transcript overrode a nonempty registry")

    atomic_write_json(registry, registry_payload([]))
    unseen_empty = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.0,
    )
    require(unseen_empty["status"] == "unverified", "unobserved empty registry proved cleanup")

    atomic_write_json(registry, registry_payload([child]))

    def invalidate_registry() -> None:
        time.sleep(0.02)
        atomic_write_json(registry, registry_payload([{"pid": os.getpid()}]))

    invalidator = threading.Thread(target=invalidate_registry)
    invalidator.start()
    invalid_row = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.5,
    )
    invalidator.join()
    require(invalid_row["status"] == "unverified", "invalid post-observation child row proved cleanup")

    atomic_write_json(registry, registry_payload([child]))

    def clear_registry() -> None:
        time.sleep(0.02)
        atomic_write_json(registry, registry_payload([]))

    clearer = threading.Thread(target=clear_registry)
    clearer.start()
    quiescent = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.5,
    )
    clearer.join()
    require(quiescent["status"] == "quiescent", "observed registry transition was not accepted")

    atomic_write_json(registry, registry_payload([child]))

    def corrupt_registry() -> None:
        time.sleep(0.02)
        registry.write_text("not-json", encoding="utf-8")

    corrupter = threading.Thread(target=corrupt_registry)
    corrupter.start()
    corrupted = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.5,
    )
    corrupter.join()
    require(corrupted["status"] == "unverified", "malformed post-observation registry proved cleanup")


def test_broker_reuse_proof() -> None:
    explicit = {
        "status": "prompt_lost",
        "seat_reusable": True,
        "child_quiescent": True,
        "cleanup_verified": True,
    }
    require(broker_response_proves_reusable(explicit), "explicit lifecycle proof was rejected")
    for key in ("seat_reusable", "child_quiescent", "cleanup_verified"):
        incomplete = dict(explicit)
        incomplete.pop(key)
        require(not broker_response_proves_reusable(incomplete), f"missing {key} was accepted")
    require(not broker_response_proves_reusable({"status": "ok"}), "semantic success proved seat reuse")
    require(not broker_response_proves_reusable(None), "missing broker response proved seat reuse")


def new_pool(pool_dir: Path, session_dir: Path) -> dict[str, Any]:
    return {
        "schema": "proof-run.warm-pool.v1",
        "pool_id": pool_dir.name,
        "pool_dir": str(pool_dir),
        "status": "ready",
        "engine": "fork_basis",
        "preloads": [],
        "sessions": [
            {
                "worker_index": 1,
                "session_dir": str(session_dir),
                "status": "idle",
                "lease": None,
                "loaded_preloads": [],
            }
        ],
    }


def test_seat_finalization(root: Path) -> None:
    session_dir = root / "pool-session"
    pool_dir = root / "pool"
    session_dir.mkdir()
    atomic_write_json(session_dir / "session.json", current_session(session_dir))
    write_warm_pool_json(pool_dir, new_pool(pool_dir, session_dir))

    with warm_pool_lock(pool_dir):
        pool = read_warm_pool(pool_dir)
        checked_out = checkout_pool_seat_locked(pool_dir, pool, agent="selftest")
        require(checked_out["status"] == "busy", "checkout did not lease the idle seat")
    with warm_pool_lock(pool_dir):
        pool = read_warm_pool(pool_dir)
        finalized = finalize_fork_pool_seat_locked(
            pool_dir,
            pool,
            session=session_dir,
            reusable=True,
            reason="explicit selftest quiescence",
        )
        require(finalized["status"] == "idle", "proven reusable seat did not return idle")
    persisted = read_warm_pool(pool_dir)
    require(persisted["status"] == "ready", "idle release poisoned the pool")
    require(persisted["sessions"][0]["lease"] is None, "idle release retained its lease")

    with warm_pool_lock(pool_dir):
        pool = read_warm_pool(pool_dir)
        checkout_pool_seat_locked(pool_dir, pool, agent="selftest")
    with warm_pool_lock(pool_dir):
        pool = read_warm_pool(pool_dir)
        quarantined = finalize_fork_pool_seat_locked(
            pool_dir,
            pool,
            session=session_dir,
            reusable=False,
            reason="unverified selftest cleanup",
        )
        require(quarantined["status"] == "poisoned", "unverified seat was not quarantined")
    persisted = read_warm_pool(pool_dir)
    require(persisted["status"] == "poisoned", "unverified seat left pool ready")
    require(persisted["sessions"][0]["lease"] is None, "quarantine retained its lease")


def test_socket_session(root: Path) -> None:
    logical = root / "logical-session"
    physical = root / "physical-session"
    logical.mkdir()
    physical.mkdir()
    atomic_write_json(logical / "session.json", {"control_session_dir": str(physical)})
    endpoint = session_socket_path(logical)
    endpoint.unlink(missing_ok=True)
    ready = threading.Event()
    server_errors: list[BaseException] = []

    def serve_once() -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(endpoint))
                server.listen(1)
                server.settimeout(1.0)
                ready.set()
                connection, _ = server.accept()
                with connection:
                    raw = connection.recv(4096).split(b"\n", 1)[0]
                    request = json.loads(raw.decode("utf-8"))
                    connection.sendall(json.dumps({"status": "ok", "echo": request["value"]}).encode() + b"\n")
        except BaseException as exc:
            server_errors.append(exc)
            ready.set()

    server = threading.Thread(target=serve_once)
    server.start()
    require(ready.wait(1.0), "socket fixture did not become ready")
    response = send_session_request(logical, {"value": "round-trip"}, timeout_seconds=1.0)
    server.join(timeout=1.0)
    endpoint.unlink(missing_ok=True)
    require(not server.is_alive(), "socket fixture did not stop")
    require(not server_errors, f"socket fixture failed: {server_errors}")
    require(response.get("echo") == "round-trip", "session socket response was not preserved")


def test_disposable_timeout_cleanup() -> None:
    child_code = (
        "import signal,time;signal.signal(signal.SIGTERM, lambda *_: None);print('ready', flush=True);time.sleep(30)"
    )
    owner = OwnedProcessSet(term_grace_seconds=0.05, kill_grace_seconds=0.5)
    child = owner.spawn(
        [sys.executable, "-I", "-B", "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_stdout = child.process.stdout
    if child_stdout is None:
        raise AssertionError("disposable child stdout was not captured")
    require(child_stdout.readline().strip() == "ready", "disposable child did not become ready")
    receipts = owner.cleanup()
    require(len(receipts) == 1, "disposable cleanup returned an unexpected receipt count")
    receipt = receipts[0]
    require(receipt.verified_quiescent, f"disposable child survived cleanup: {receipt.as_dict()}")
    require(receipt.term_sent and receipt.kill_sent, "timeout cleanup did not escalate TERM to KILL")


def test_broker_frames() -> None:
    left, right = socket.socketpair()
    raw = b"\x00\xffHOL\n\x80"
    try:
        send_control(left, {"schema": "selftest", "operation": "describe"})
        frame_type, encoded = receive_frame(right)
        require(frame_type == BROKER_FRAME_CONTROL, "control frame type changed")
        require(
            json.loads(encoded) == {"operation": "describe", "schema": "selftest"},
            "control frame payload changed",
        )
        send_raw_output(right, raw)
        frame_type, observed = receive_frame(left)
        require(frame_type == BROKER_FRAME_RAW_OUTPUT, "raw-output frame type changed")
        require(observed == raw, "raw-output bytes were decoded or changed")
    finally:
        left.close()
        right.close()

    left, right = socket.socketpair()
    try:
        left.sendall(BROKER_FRAME_HEADER.pack(0x7F, 0))
        require_raises(BrokerProtocolError, "unknown broker frame type", lambda: receive_frame(right))
    finally:
        left.close()
        right.close()

    left, right = socket.socketpair()
    reader = BufferedBrokerFrameReader()
    payload = b"\x00\xfffragmented broker output\n"
    frame = BROKER_FRAME_HEADER.pack(BROKER_FRAME_RAW_OUTPUT, len(payload)) + payload
    right.settimeout(0.01)
    try:
        left.sendall(frame[:2])
        require(reader.receive(right) is None, "partial broker header was treated as complete")
        left.sendall(frame[2:6])
        require(reader.receive(right) is None, "partial broker payload was treated as complete")
        left.sendall(frame[6:])
        require(
            reader.receive(right) == (BROKER_FRAME_RAW_OUTPUT, payload),
            "fragmented broker frame was not reassembled byte-exactly",
        )
    finally:
        left.close()
        right.close()


def test_spawn_registration_gate() -> None:
    stop_event = threading.Event()
    gate = ForkSpawnGate(stop_event)
    spawn_entered = threading.Event()
    allow_registration = threading.Event()
    registration_finished = threading.Event()

    def spawn() -> None:
        with gate.spawning():
            spawn_entered.set()
            allow_registration.wait(timeout=1.0)
            registration_finished.set()

    spawn_thread = threading.Thread(target=spawn)
    stop_thread = threading.Thread(target=gate.begin_stop)
    spawn_thread.start()
    require(spawn_entered.wait(timeout=1.0), "spawn registration did not begin")
    stop_thread.start()
    time.sleep(0.02)
    require(not stop_event.is_set(), "stop became visible before spawn registration finished")
    allow_registration.set()
    spawn_thread.join(timeout=1.0)
    stop_thread.join(timeout=1.0)
    require(registration_finished.is_set(), "spawn registration did not finish")
    require(stop_event.is_set(), "stop was not published after spawn registration finished")


def test_identity_bound_signal() -> None:
    signals: list[int] = []
    mismatch = terminate_process_group(
        99,
        identity_pid=99,
        expected_start_ticks=100,
        require_identity=True,
        term_grace_seconds=0,
        kill_grace_seconds=0,
        signal_group=lambda _pgid, signum: signals.append(signum) or True,
        group_alive=lambda _pgid: True,
        identity_reader=lambda _pid: 200,
    )
    require(mismatch["status"] == "identity_mismatch", "reused PID identity was not refused")
    require(signals == [], "reused PID was signalled")

    signals.clear()
    identities = iter(("birth-a", "birth-b"))
    changed = terminate_process_group(
        99,
        identity_pid=99,
        expected_identity="birth-a",
        require_identity=True,
        term_grace_seconds=0,
        kill_grace_seconds=0,
        signal_group=lambda _pgid, signum: signals.append(signum) or True,
        group_alive=lambda _pgid: True,
        birth_identity_reader=lambda _pid: next(identities),
    )
    require(signals == [signal.SIGTERM], "identity change did not suppress delayed SIGKILL")
    require(changed["status"] == "identity_mismatch", "delayed identity change was not reported")
    require(changed["term_sent"] is True and changed["kill_sent"] is False, "signal receipt is incoherent")


def test_stale_release_fails_closed(root: Path) -> None:
    session = root / "stale-release-session"
    session.mkdir()
    lease = {"lease_id": "old-eval"}
    pool = {
        "engine": "fork_basis",
        "status": "restore_failed",
        "sessions": [
            {
                "worker_index": 1,
                "status": "restore_failed",
                "session_dir": str(session),
                "lease": lease,
            }
        ],
    }
    released = release_warm_pool_seat(
        pool,
        session=session,
        status="dirty",
        released_utc="2026-08-12T00:00:00Z",
    )
    require(released["status"] == "restore_failed", "delayed release reopened a failed seat")
    require(released["lease"] == lease, "delayed release erased lifecycle-owned lease evidence")
    require(
        released["release_reason"]
        == "eval release deferred because pool lifecycle status restore_failed owns seat state",
        "delayed release did not report lifecycle ownership",
    )


HOT_SERVER_FIXTURE = r"""
import os
import socket
import sys

endpoint, nonce = sys.argv[1:]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(endpoint)
server.listen(4)
print("ready", flush=True)
try:
    while True:
        connection, _ = server.accept()
        with connection:
            request = connection.makefile("rb").readline().decode("ascii").rstrip("\n")
            if request == f"status\t{nonce}":
                connection.sendall(
                    f"__HOL_WORKBENCH_HOT__:{nonce}:ready:server:{os.getpid()}\n".encode("ascii")
                )
            elif request == f"stop\t{nonce}":
                connection.sendall(
                    f"__HOL_WORKBENCH_HOT__:{nonce}:stopping:server:0\n".encode("ascii")
                )
                break
finally:
    server.close()
    try:
        os.unlink(endpoint)
    except FileNotFoundError:
        pass
"""


def selftest_sibling_lease() -> dict[str, str]:
    return {
        "lease_id": "unrelated-sibling-lease",
        "owner_kind": "unrelated-selftest-owner",
    }


class PreReadyInterruptFixture:
    """Injected public-loop lifecycle with real pool, registry, and processes."""

    def __init__(self, root: Path, phase: str) -> None:
        self.phase = phase
        self.root = root / phase
        self.root.mkdir()
        self.source = self.root / "source.ml"
        self.source.write_text("let SELFTEST = prove (`T`, MESON_TAC[]);;\n", encoding="utf-8")
        self.script_dir = self.root / "bin"
        self.script_dir.mkdir()
        self.profile_root = self.root / "profile"
        self.profile_root.mkdir()
        self.pool_dir = self.profile_root / "pool" / "basis"
        self.session_dir = self.root / "persistent-session"
        self.session_dir.mkdir()
        self.registry = self.session_dir / "active-children.json"
        atomic_write_json(self.registry, registry_payload([]))
        session = current_session(self.session_dir)
        session["active_children_registry"] = str(self.registry)
        atomic_write_json(self.session_dir / "session.json", session)
        self.sibling_session_dir = self.root / "persistent-sibling-session"
        self.sibling_session_dir.mkdir()
        atomic_write_json(
            self.sibling_session_dir / "session.json",
            current_session(self.sibling_session_dir),
        )
        self.sibling_lease = {
            "lease_id": "ab" * 8 if phase == "lease_collision" else "unrelated-sibling-lease",
            "owner_kind": "unrelated-selftest-owner",
        }
        pool = new_pool(self.pool_dir, self.session_dir)
        pool["sessions"].append(
            {
                "worker_index": 2,
                "session_dir": str(
                    self.session_dir if phase == "duplicate_session_path" else self.sibling_session_dir
                ),
                "status": "busy",
                "lease": dict(self.sibling_lease),
                "loaded_preloads": [],
            }
        )
        write_warm_pool_json(self.pool_dir, pool)
        self.loop_dir = self.root / "loop-session"
        self.socket_path = self.loop_dir / "session.sock"
        self.nonce = "ab" * 24
        self.admission_entered = False
        self.admission_owner_persisted = False
        self.broker_called = False
        self.checkout_persisted = False
        self.second_cleanup_seen = False
        self.seat_second_signal_seen = False
        self.server_owner: OwnedProcessSet | None = None
        self.server_process: Any = None
        self.child_owners: list[OwnedProcessSet] = []
        self.child_processes: list[Any] = []
        self.child_receipts: list[Any] = []
        self.cleanup_threads: list[threading.Thread] = []
        self.server_reapers: list[threading.Thread] = []
        self.signal_threads: list[threading.Thread] = []
        self.admission_locks: list[Any] = []
        self.admission_contended = threading.Event()
        self.registry_observed = threading.Event()
        self.real_try_lock_group = criu_shelf_admission_module._try_lock_group
        self.real_write_shelf_owner = criu_shelf_admission_module.write_shelf_owner
        self.real_attempt_children = fork_attempt_lifecycle_module._attempt_children
        self.real_finalize_lease = hot_profile_session._finalize_lease
        self.real_remove_tree = prove_loop._remove_tree

    def mkdtemp(self, *_args: Any, **_kwargs: Any) -> str:
        self.loop_dir.mkdir(mode=0o700)
        if self.phase == "mkdtemp_handoff":
            os.kill(os.getpid(), signal.SIGINT)
        return str(self.loop_dir)

    def _start_admission_interrupt(self) -> None:
        if self.phase != "admission":
            return
        for name in ("admission.lock", "admission-1.lock"):
            lock = (self.profile_root / name).open("a+", encoding="utf-8")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.admission_locks.append(lock)

        def interrupt_after_contention() -> None:
            self.admission_entered = self.admission_contended.wait(timeout=2.0)
            os.kill(os.getpid(), signal.SIGINT)

        interrupter = threading.Thread(target=interrupt_after_contention)
        interrupter.start()
        self.signal_threads.append(interrupter)

    def observe_try_lock_group(self, locks: list[Any]) -> bool:
        acquired = self.real_try_lock_group(locks)
        if not acquired:
            self.admission_contended.set()
        return acquired

    def write_owner_then_interrupt(self, profile_root: Path, owner: dict[str, Any]) -> None:
        self.real_write_shelf_owner(profile_root, owner)
        self.admission_owner_persisted = bool(list(profile_root.glob("admission-owner*.json")))
        os.kill(os.getpid(), signal.SIGINT)

    def _spawn_owned_child(self) -> tuple[OwnedProcessSet, Any]:
        code = (
            "import signal,time;"
            "signal.signal(signal.SIGTERM, lambda *_: None);"
            "print('ready', flush=True);time.sleep(30)"
        )
        owner = OwnedProcessSet(term_grace_seconds=0.05, kill_grace_seconds=0.5)
        child = owner.spawn(
            [sys.executable, "-I", "-B", "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        child_stdout = child.process.stdout
        if child_stdout is None:
            raise AssertionError("hostile child stdout was not captured")
        require(child_stdout.readline().strip() == "ready", "hostile child did not become ready")
        child.require_live()
        self.child_owners.append(owner)
        self.child_processes.append(child)
        return owner, child

    def restore(self, *_args: Any, **_kwargs: Any) -> int:
        if self.phase != "restore":
            return 0
        owner, _child = self._spawn_owned_child()
        try:
            os.kill(os.getpid(), signal.SIGINT)
            raise AssertionError("SIGINT did not interrupt restore")
        finally:
            self.child_receipts.extend(owner.cleanup())

    def checkout(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        item = checkout_pool_seat_locked(*args, **kwargs)
        if self.phase in {
            "duplicate_session_path",
            "post_checkout_signal",
            "post_checkout_exception",
        }:
            persisted = read_warm_pool(self.pool_dir)
            seat = next(
                row for row in persisted["sessions"] if row.get("session_dir") == str(self.session_dir)
            )
            self.checkout_persisted = seat.get("status") == "busy" and bool(seat.get("lease"))
            if self.phase == "post_checkout_signal":
                os.kill(os.getpid(), signal.SIGINT)
                return item
            raise KeyboardInterrupt
        return item

    def _start_server(self) -> None:
        owner = OwnedProcessSet(term_grace_seconds=0.05, kill_grace_seconds=0.5)
        server = owner.spawn(
            [sys.executable, "-I", "-B", "-c", HOT_SERVER_FIXTURE, str(self.socket_path), self.nonce],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        server_stdout = server.process.stdout
        if server_stdout is None:
            raise AssertionError("hostile server stdout was not captured")
        require(server_stdout.readline().strip() == "ready", "hostile server did not become ready")
        server.require_live()
        require(self.socket_path.is_socket(), "hostile server socket was not published")
        self.server_owner = owner
        self.server_process = server
        reaper = threading.Thread(target=server.process.wait)
        reaper.start()
        self.server_reapers.append(reaper)

    def _registry_child(self, attempt_id: str, child: Any) -> dict[str, Any]:
        identity = child.identity
        require(identity.start_ticks is not None, "hostile child start ticks are unavailable")
        require(identity.birth_identity is not None, "hostile child birth identity is unavailable")
        return {
            "attempt_id": attempt_id,
            "pid": identity.pid,
            "pgid": identity.pgid,
            "process_start_ticks": identity.start_ticks,
            "process_identity": identity.birth_identity,
        }

    def broker(self, _session: Path, request: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.broker_called = True
        self._start_server()
        if self.phase in {"cleanup_interrupted", "cleanup_oserror", "second_signal"}:
            return {
                "status": "ok",
                "exit_status": 0,
                "seat_reusable": True,
                "child_quiescent": True,
                "cleanup_verified": True,
            }
        owner, child = self._spawn_owned_child()
        attempt_id = str(request["attempt_id"])
        atomic_write_json(self.registry, registry_payload([self._registry_child(attempt_id, child)]))
        if self.phase == "bootstrap_verified":

            def clean_registered_child() -> None:
                require(self.registry_observed.wait(timeout=2.0), "registry row was never observed")
                self.child_receipts.extend(owner.cleanup())
                atomic_write_json(self.registry, registry_payload([]))

            cleanup = threading.Thread(target=clean_registered_child)
            cleanup.start()
            self.cleanup_threads.append(cleanup)
        else:
            self.child_receipts.extend(owner.cleanup())
        if self.phase == "seat_second_signal":
            raise KeyboardInterrupt
        os.kill(os.getpid(), signal.SIGINT)
        raise AssertionError("SIGINT did not interrupt the in-flight broker request")

    def finalize_with_second_signal(self, *args: Any, **kwargs: Any) -> None:
        self.seat_second_signal_seen = True
        os.kill(os.getpid(), signal.SIGINT)
        self.real_finalize_lease(*args, **kwargs)

    def interrupt_watch(self, *_args: Any, **_kwargs: Any) -> None:
        if self.phase == "cleanup_interrupted":
            raise KeyboardInterrupt
        os.kill(os.getpid(), signal.SIGINT)
        raise AssertionError("SIGINT did not interrupt the watch")

    def hostile_remove_tree(self, path: Path) -> None:
        self.second_cleanup_seen = True
        if self.phase == "cleanup_interrupted":
            raise KeyboardInterrupt
        if self.phase == "cleanup_oserror":
            raise OSError("injected session-tree cleanup refusal")
        os.kill(os.getpid(), signal.SIGINT)
        self.real_remove_tree(path)

    def observe_attempt_children(self, registry: Path, attempt_id: str) -> Any:
        snapshot = self.real_attempt_children(registry, attempt_id)
        if snapshot.children:
            self.registry_observed.set()
        return snapshot

    def _exact_child_cleanup(self) -> bool:
        expected = {
            (child.identity.pid, child.identity.pgid, child.identity.birth_identity)
            for child in self.child_processes
        }
        observed = {
            (receipt.pid, receipt.pgid, receipt.expected_identity)
            for receipt in self.child_receipts
            if receipt.verified_quiescent
        }
        return expected <= observed

    def _release_fixture_admission_locks(self) -> None:
        locks, self.admission_locks = self.admission_locks, []
        for lock in locks:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            lock.close()

    def _admission_locks_reacquirable(self) -> bool:
        locks: list[Any] = []
        try:
            for name in ("admission.lock", "admission-1.lock"):
                lock = (self.profile_root / name).open("a+", encoding="utf-8")
                locks.append(lock)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            for lock in reversed(locks):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()

    def _force_cleanup(self) -> None:
        for thread in self.cleanup_threads:
            thread.join(timeout=2.0)
        for owner in self.child_owners:
            owner.cleanup()
        if self.server_owner is not None:
            self.server_owner.cleanup()
        for thread in self.server_reapers:
            thread.join(timeout=2.0)
        for thread in self.signal_threads:
            thread.join(timeout=2.0)
        self._release_fixture_admission_locks()
        self.socket_path.unlink(missing_ok=True)

    def run(self) -> dict[str, Any]:
        (self.root / "build.json").write_text('{"ocaml":"5.3.0"}\n')
        stdout = StringIO()
        stderr = StringIO()
        result: int | None = None
        escaped: BaseException | None = None
        profile = SimpleNamespace(
            name="light",
            root=self.profile_root,
            cwd=self.root,
            capacity=1,
            legacy_holdir_roots=(),
            logical_source_roots=(),
        )
        self._start_admission_interrupt()
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(prove_loop, "_profile", return_value="light"))
                stack.enter_context(patch.object(prove_loop, "_native_eval_dir", return_value=self.root))
                stack.enter_context(patch.object(prove_loop, "resolve_published_warm_profile", return_value=profile))
                stack.enter_context(patch.object(prove_loop, "_source_import_roots", return_value={}))
                stack.enter_context(patch.object(prove_loop, "_source_runtime_cwd", return_value=None))
                stack.enter_context(patch.object(prove_loop.tempfile, "mkdtemp", side_effect=self.mkdtemp))
                stack.enter_context(patch.object(prove_loop.secrets, "token_hex", side_effect=lambda n: "ab" * n))
                stack.enter_context(patch.object(hot_profile_session, "restored_shelf_capacity", return_value=1))
                stack.enter_context(patch.object(orbstack_criu_restore, "main", self.restore))
                stack.enter_context(patch.object(hot_profile_session, "checkout_pool_seat_locked", self.checkout))
                stack.enter_context(patch.object(hot_profile_session, "run_broker_capture_request", self.broker))
                stack.enter_context(
                    patch.object(orbstack_idle_retirement, "schedule_profile_retirement", return_value=None)
                )
                if self.phase == "admission":
                    stack.enter_context(
                        patch.object(
                            criu_shelf_admission_module,
                            "_try_lock_group",
                            self.observe_try_lock_group,
                        )
                    )
                if self.phase == "admission_owner_handoff":
                    stack.enter_context(
                        patch.object(
                            criu_shelf_admission_module,
                            "write_shelf_owner",
                            self.write_owner_then_interrupt,
                        )
                    )
                if self.phase == "bootstrap_verified":
                    stack.enter_context(
                        patch.object(
                            fork_attempt_lifecycle_module,
                            "_attempt_children",
                            self.observe_attempt_children,
                        )
                    )
                if self.phase == "seat_second_signal":
                    stack.enter_context(
                        patch.object(
                            hot_profile_session,
                            "_finalize_lease",
                            self.finalize_with_second_signal,
                        )
                    )
                if self.phase in {"cleanup_interrupted", "cleanup_oserror", "second_signal"}:
                    stack.enter_context(patch.object(prove_loop, "_watch", self.interrupt_watch))
                    stack.enter_context(patch.object(prove_loop, "_remove_tree", self.hostile_remove_tree))
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    try:
                        result = prove_loop.main(
                            [str(self.source), "--loop", "--profile", "light"],
                            script_dir=self.script_dir,
                            cwd=self.root,
                        )
                    except BaseException as exc:
                        escaped = exc
            for thread in self.cleanup_threads:
                thread.join(timeout=2.0)
                require(not thread.is_alive(), "hostile cleanup thread did not settle")
            for thread in self.signal_threads:
                thread.join(timeout=2.0)
                require(not thread.is_alive(), "hostile signal thread did not settle")
            self._release_fixture_admission_locks()
            pool = read_warm_pool(self.pool_dir)
            seat = next(row for row in pool["sessions"] if row.get("worker_index") == 1)
            sibling = next(row for row in pool["sessions"] if row.get("worker_index") == 2)
            demand_residue = list((self.profile_root / "admission-demands").glob("*.json"))
            owner_residue = list(self.profile_root.glob("admission-owner*.json"))
            outcome = {
                "result": result,
                "escaped": type(escaped).__name__ if escaped is not None else None,
                "stdout": stdout.getvalue(),
                "stderr": stderr.getvalue(),
                "loop_dir_exists": self.loop_dir.exists(),
                "socket_exists": self.socket_path.exists(),
                "server_live": bool(self.server_process and self.server_process.process.poll() is None),
                "pool_status": pool.get("status"),
                "seat_status": seat.get("status"),
                "lease": seat.get("lease"),
                "poison_reason": pool.get("poison_reason"),
                "persistent_session_exists": self.session_dir.is_dir(),
                "admission_entered": self.admission_entered,
                "admission_owner_persisted": self.admission_owner_persisted,
                "admission_cleaned": not demand_residue and not owner_residue,
                "admission_locks_reacquirable": self._admission_locks_reacquirable(),
                "broker_called": self.broker_called,
                "checkout_persisted": self.checkout_persisted,
                "seat_second_signal_seen": self.seat_second_signal_seen,
                "second_cleanup_seen": self.second_cleanup_seen,
                "exact_child_cleanup": self._exact_child_cleanup(),
                "sibling_status": sibling.get("status"),
                "sibling_lease": sibling.get("lease"),
                "sibling_release_after": sibling.get("release_after_lease_status"),
            }
        finally:
            self._force_cleanup()
        return outcome


def test_pre_ready_interrupt_cleanup(root: Path) -> None:
    outcomes = {
        phase: PreReadyInterruptFixture(root, phase).run()
        for phase in (
            "mkdtemp_handoff",
            "admission",
            "admission_owner_handoff",
            "restore",
            "post_checkout_signal",
            "post_checkout_exception",
            "duplicate_session_path",
            "bootstrap_verified",
            "bootstrap_unverified",
            "seat_second_signal",
            "cleanup_interrupted",
            "cleanup_oserror",
            "second_signal",
            "lease_collision",
        )
    }
    failures: list[str] = []

    def expect(phase: str, condition: object, detail: str) -> None:
        if not condition:
            failures.append(f"{phase}: {detail}; outcome={outcomes[phase]!r}")

    for phase, outcome in outcomes.items():
        expect(phase, outcome["escaped"] is None, "interrupt escaped the public loop")
        output = outcome["stdout"] + outcome["stderr"]
        expect(phase, "Traceback" not in output, "traceback reached normal UX")
        for exception_name in ("KeyboardInterrupt", "WarmPoolEvalInterrupted", "HotBootstrapInterrupted"):
            expect(phase, exception_name not in output, f"{exception_name} reached normal UX")
        expect(phase, outcome["persistent_session_exists"], "persistent broker session was removed")
        expect(phase, outcome["admission_cleaned"], "admission owner or demand residue survived")
        expect(phase, outcome["admission_locks_reacquirable"], "admission lock survived cleanup")

    clean_pre_ready = (
        "mkdtemp_handoff",
        "admission",
        "admission_owner_handoff",
        "restore",
        "post_checkout_signal",
        "post_checkout_exception",
        "bootstrap_verified",
        "bootstrap_unverified",
        "seat_second_signal",
    )
    for phase in clean_pre_ready:
        outcome = outcomes[phase]
        expect(phase, outcome["result"] == 130, "Ctrl-C did not return 130")
        expect(phase, not outcome["loop_dir_exists"], "ephemeral loop directory survived cleanup")
        expect(phase, not outcome["socket_exists"], "ephemeral loop socket survived cleanup")
        expect(phase, not outcome["server_live"], "ephemeral loop server survived cleanup")
        expect(phase, "HOT LOOP: ready" not in outcome["stdout"], "pre-ready interrupt printed READY")
        expect(phase, outcome["stdout"].splitlines().count("HOT LOOP: stopping") == 1, "missing stopping line")
        expect(
            phase,
            outcome["stdout"].splitlines().count("HOT LOOP: stopped (SIGINT, exit 130)") == 1,
            "missing signal-aware stopped line",
        )

    for phase in ("mkdtemp_handoff", "admission", "admission_owner_handoff", "restore"):
        outcome = outcomes[phase]
        expect(phase, outcome["pool_status"] == "ready", "pre-checkout interrupt changed pool lifecycle")
        expect(phase, outcome["seat_status"] == "idle", "pre-checkout interrupt changed the seat")
        expect(phase, outcome["lease"] is None, "pre-checkout interrupt persisted a lease")
        expect(phase, not outcome["broker_called"], "pre-checkout interrupt dispatched a broker request")
    expect("admission", outcomes["admission"]["admission_entered"], "real contended admission did not begin")
    expect(
        "admission_owner_handoff",
        outcomes["admission_owner_handoff"]["admission_owner_persisted"],
        "admission owner was not persisted before interruption",
    )
    expect("restore", outcomes["restore"]["exact_child_cleanup"], "restore child cleanup lost exact identity")

    seat_second = outcomes["seat_second_signal"]
    expect(
        "seat_second_signal",
        seat_second["seat_second_signal_seen"],
        "second Ctrl-C did not reach seat finalization",
    )
    expect(
        "seat_second_signal",
        seat_second["pool_status"] == "poisoned",
        "unverified second-signal cleanup did not poison the pool",
    )
    expect(
        "seat_second_signal",
        seat_second["seat_status"] == "poisoned",
        "second Ctrl-C stranded the checked-out seat",
    )
    expect(
        "seat_second_signal",
        seat_second["lease"] is None,
        "second Ctrl-C retained the checked-out lease",
    )
    expect(
        "seat_second_signal",
        seat_second["exact_child_cleanup"],
        "second Ctrl-C lost exact child cleanup authority",
    )

    for phase in ("post_checkout_signal", "post_checkout_exception"):
        post_checkout = outcomes[phase]
        expect(phase, post_checkout["checkout_persisted"], "fixture missed the persisted lease window")
        expect(phase, not post_checkout["broker_called"], "post-checkout interrupt dispatched a request")
        expect(phase, post_checkout["pool_status"] == "ready", "quiescent pre-dispatch seat poisoned pool")
        expect(phase, post_checkout["seat_status"] == "idle", "quiescent pre-dispatch seat did not reopen")
        expect(phase, post_checkout["lease"] is None, "post-checkout interrupt stranded its lease")
        expect(phase, post_checkout["sibling_status"] == "busy", "unrelated busy sibling changed status")
        expect(phase, post_checkout["sibling_lease"] == selftest_sibling_lease(), "unrelated lease changed")

    verified = outcomes["bootstrap_verified"]
    expect("bootstrap_verified", verified["broker_called"], "in-flight request was not dispatched")
    expect("bootstrap_verified", verified["exact_child_cleanup"], "in-flight child cleanup lost exact identity")
    expect("bootstrap_verified", verified["pool_status"] == "ready", "verified quiescence poisoned the pool")
    expect("bootstrap_verified", verified["seat_status"] == "idle", "verified quiescence did not reopen the seat")
    expect("bootstrap_verified", verified["lease"] is None, "verified quiescence retained its lease")

    unverified = outcomes["bootstrap_unverified"]
    expect("bootstrap_unverified", unverified["exact_child_cleanup"], "unverified fixture child actually survived")
    expect("bootstrap_unverified", unverified["pool_status"] == "poisoned", "stale registry did not poison pool")
    expect("bootstrap_unverified", unverified["seat_status"] == "poisoned", "stale registry did not poison seat")
    expect("bootstrap_unverified", unverified["lease"] is None, "poisoned seat retained a stale lease")
    expect("bootstrap_unverified", unverified["sibling_status"] == "busy", "active sibling was unlocked")
    expect(
        "bootstrap_unverified",
        unverified["sibling_lease"] == selftest_sibling_lease(),
        "active sibling lease changed",
    )
    expect(
        "bootstrap_unverified",
        unverified["sibling_release_after"] == "poisoned",
        "active sibling did not inherit poisoned release status",
    )
    expect(
        "bootstrap_unverified",
        "survivors" in str(unverified["poison_reason"]),
        "poison reason did not preserve registry authority",
    )

    duplicate = outcomes["duplicate_session_path"]
    expect("duplicate_session_path", duplicate["result"] == 1, "duplicate session path did not fail closed")
    expect(
        "duplicate_session_path",
        not duplicate["loop_dir_exists"] and not duplicate["socket_exists"] and not duplicate["server_live"],
        "duplicate session path left ephemeral loop residue",
    )
    expect("duplicate_session_path", duplicate["pool_status"] == "poisoned", "corrupt pool stayed ready")
    expect("duplicate_session_path", duplicate["seat_status"] == "poisoned", "owned seat was not poisoned")
    expect("duplicate_session_path", duplicate["lease"] is None, "owned corrupt lease stayed busy")
    expect("duplicate_session_path", duplicate["sibling_status"] == "busy", "foreign duplicate was released")
    expect(
        "duplicate_session_path",
        duplicate["sibling_lease"] == selftest_sibling_lease(),
        "foreign duplicate lease changed",
    )
    expect(
        "duplicate_session_path",
        duplicate["sibling_release_after"] == "poisoned",
        "foreign duplicate did not inherit poisoned release status",
    )
    expect(
        "duplicate_session_path",
        "session ownership is ambiguous" in duplicate["stderr"],
        "duplicate session path lacked bounded failure detail",
    )

    for phase in ("cleanup_interrupted", "cleanup_oserror"):
        incomplete = outcomes[phase]
        expect(phase, incomplete["result"] == 1, "cleanup failure did not fail closed")
        expect(phase, incomplete["second_cleanup_seen"], "cleanup failure fixture did not fire")
        expect(phase, incomplete["loop_dir_exists"], "incomplete cleanup did not preserve the loop directory")
        expect(phase, not incomplete["socket_exists"], "cleanup failure left the stopped server socket")
        expect(phase, not incomplete["server_live"], "cleanup failure left the loop server alive")
        expect(phase, incomplete["seat_status"] == "idle", "cleanup failure stranded the finalized seat")
        expect(phase, incomplete["lease"] is None, "cleanup failure retained a stale lease")
        expect(
            phase,
            not any(line.startswith("HOT LOOP: stopped") for line in incomplete["stdout"].splitlines()),
            "cleanup falsely stopped",
        )
        expect(phase, "stop incomplete" in incomplete["stderr"], "incomplete cleanup was not reported")

    second = outcomes["second_signal"]
    expect("second_signal", second["result"] == 130, "second Ctrl-C changed interrupt status")
    expect("second_signal", second["second_cleanup_seen"], "second Ctrl-C fixture did not fire")
    expect("second_signal", not second["loop_dir_exists"], "second Ctrl-C prevented completed cleanup")
    expect("second_signal", second["seat_status"] == "idle", "second Ctrl-C stranded the finalized seat")
    expect("second_signal", second["lease"] is None, "second Ctrl-C retained a stale lease")
    expect(
        "second_signal",
        second["stdout"].splitlines().count("HOT LOOP: stopped (SIGINT, exit 130)") == 1,
        "cleanup truth was lost",
    )

    collision = outcomes["lease_collision"]
    expect("lease_collision", collision["result"] == 1, "lease collision did not fail closed")
    expect("lease_collision", not collision["broker_called"], "lease collision dispatched a request")
    expect(
        "lease_collision",
        not collision["loop_dir_exists"] and not collision["socket_exists"] and not collision["server_live"],
        "lease collision left ephemeral loop residue",
    )
    expect("lease_collision", collision["seat_status"] == "idle", "lease collision seized the idle target")
    expect("lease_collision", collision["lease"] is None, "lease collision leased the target")
    expect("lease_collision", collision["sibling_status"] == "busy", "lease collision released another owner")
    expect(
        "lease_collision",
        collision["sibling_lease"] == {"lease_id": "ab" * 8, "owner_kind": "unrelated-selftest-owner"},
        "lease collision changed another owner's lease",
    )
    expect("lease_collision", "already active" in collision["stderr"], "lease collision lacked bounded detail")

    require(not failures, "hostile pre-ready interrupt failures:\n" + "\n".join(failures))


def main() -> int:
    if sys.platform != "linux":
        raise SystemExit("memory-first lifecycle selftest is Linux-only")
    with tempfile.TemporaryDirectory(prefix="memory-first-lifecycle-") as temporary:
        root = Path(temporary)
        test_process_identity(root)
        test_registry_authority(root)
        test_broker_reuse_proof()
        test_seat_finalization(root)
        test_socket_session(root)
        test_disposable_timeout_cleanup()
        test_broker_frames()
        test_spawn_registration_gate()
        test_identity_bound_signal()
        test_stale_release_fails_closed(root)
        test_pre_ready_interrupt_cleanup(root)
    print("memory_first_lifecycle_selftest=passed cases=11 hostile_interrupt_cases=14")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
