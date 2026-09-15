"""Current-controller client for the mechanical restored fork-basis broker."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hol_workbench.broker_connection_identity import observe_broker_connection
from hol_workbench.fork_broker_protocol import (
    BROKER_CONTROL_SCHEMA,
    BROKER_DESCRIBE_SCHEMA,
    BROKER_EVENT_SCHEMA,
    BROKER_FRAME_CONTROL,
    BROKER_FRAME_HEADER,
    BROKER_FRAME_RAW_OUTPUT,
    BROKER_MAX_CONTROL_BYTES,
    BROKER_MAX_RAW_BYTES,
    BROKER_PROTOCOL,
    BrokerProtocolError,
    receive_frame,
    send_control,
)
from hol_workbench.proof_run_fork_budget import FORK_SPAWN_RECOVERY_TIMEOUT_SECONDS
from hol_workbench.proof_run_fork_phrase import fork_eval_phrase
from hol_workbench.proof_run_fork_spawn import prepare_fork_spawn

FORK_SPAWN_TIMEOUT_MESSAGE = "HOL basis timed out during fork spawn {attempt_id}"


def _round_elapsed(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None


def _canonical_sha256(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def controller_runtime_files(package_root: Path | None = None) -> tuple[str, ...]:
    """Load the audit-only controller closure only when an audit asks for it."""

    from hol_workbench.controller_runtime_bundle import controller_runtime_files as runtime_files

    return runtime_files(package_root)


def controller_runtime_sha256(package_root: Path | None = None) -> str:
    """Compatibility shim for final/publication audits, never broker admission."""

    from hol_workbench.controller_runtime_bundle import controller_runtime_sha256 as runtime_sha256

    return runtime_sha256(package_root)


class BufferedBrokerFrameReader:
    """Incrementally receive broker frames without losing partial reads on poll timeouts."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._frame_type: int | None = None
        self._payload_size: int | None = None

    def receive(self, conn: socket.socket) -> tuple[int, bytes] | None:
        while True:
            if self._frame_type is None and len(self._buffer) >= BROKER_FRAME_HEADER.size:
                frame_type, payload_size = BROKER_FRAME_HEADER.unpack(self._buffer[: BROKER_FRAME_HEADER.size])
                if frame_type not in {BROKER_FRAME_CONTROL, BROKER_FRAME_RAW_OUTPUT}:
                    raise BrokerProtocolError(f"unknown broker frame type: {frame_type}")
                maximum = BROKER_MAX_CONTROL_BYTES if frame_type == BROKER_FRAME_CONTROL else BROKER_MAX_RAW_BYTES
                if payload_size > maximum:
                    raise BrokerProtocolError(f"broker frame exceeds {maximum} bytes")
                del self._buffer[: BROKER_FRAME_HEADER.size]
                self._frame_type = frame_type
                self._payload_size = payload_size

            if (
                self._frame_type is not None
                and self._payload_size is not None
                and len(self._buffer) >= self._payload_size
            ):
                payload = bytes(self._buffer[: self._payload_size])
                del self._buffer[: self._payload_size]
                frame_type = self._frame_type
                self._frame_type = None
                self._payload_size = None
                return frame_type, payload

            try:
                chunk = conn.recv(64 * 1024)
            except TimeoutError:
                return None
            if not chunk:
                raise EOFError("broker peer disconnected")
            self._buffer.extend(chunk)


def _read_session(session_dir: Path) -> dict[str, Any]:
    data = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("broker session metadata is not an object")
    return data


def broker_description_problem(
    description: Mapping[str, Any],
    *,
    nonce: str,
    session: Mapping[str, Any],
    expected: Mapping[str, Any] | None,
) -> str | None:
    required = {
        "schema": BROKER_DESCRIBE_SCHEMA,
        "status": "described",
        "nonce": nonce,
        "broker_protocol": BROKER_PROTOCOL,
        "fork_snapshot_abi": session.get("fork_snapshot_abi"),
        "broker_runtime_sha256": session.get("broker_runtime_sha256"),
        "profile_basis_id": session.get("profile_basis_id"),
        "endpoint": session.get("socket"),
        # Logical seats lease the same physical broker; its generation names
        # the control session, while all runtime/process checks remain exact.
        "session_dir": session.get("control_session_dir") or session.get("session_dir"),
    }
    if expected:
        required.update({key: value for key, value in expected.items() if not str(key).startswith("_")})
    for key, value in required.items():
        if description.get(key) != value:
            return f"broker description {key} mismatch: observed={description.get(key)!r} expected={value!r}"
    broker = description.get("broker")
    basis = description.get("basis")
    if not isinstance(broker, dict) or not isinstance(basis, dict):
        return "broker description process identities are missing"
    expected_broker = {
        "pid": session.get("worker_pid"),
        "pgid": session.get("worker_pgid"),
        "start_ticks": session.get("worker_start_ticks"),
        "birth_identity": session.get("worker_identity"),
    }
    expected_basis = {
        "pid": session.get("basis_pid"),
        "pgid": session.get("basis_pgid"),
        "start_ticks": session.get("basis_start_ticks"),
        "birth_identity": session.get("basis_identity"),
    }
    if broker != expected_broker:
        return "broker description process identity does not match the live session"
    if basis != expected_basis:
        return "broker description basis identity does not match the live session"
    generation_payload = {
        "broker_protocol": description.get("broker_protocol"),
        "broker_runtime_sha256": description.get("broker_runtime_sha256"),
        "fork_snapshot_abi": description.get("fork_snapshot_abi"),
        "profile_basis_id": description.get("profile_basis_id"),
        "endpoint": description.get("endpoint"),
        "session_dir": description.get("session_dir"),
        "broker": broker,
        "basis": basis,
    }
    if description.get("generation_sha256") != _canonical_sha256(generation_payload):
        return "broker description generation identity is invalid"
    return None


@contextmanager
def described_broker_connection(
    session_dir: Path,
    *,
    expected: Mapping[str, Any] | None = None,
    timeout_seconds: float = 5.0,
) -> Iterator[tuple[socket.socket, dict[str, Any], str, dict[str, Any]]]:
    session_dir = session_dir.expanduser().resolve()
    session = _read_session(session_dir)
    endpoint = Path(str(session.get("socket") or ""))
    if not endpoint.is_absolute():
        raise RuntimeError("broker session endpoint is not absolute")
    nonce = secrets.token_hex(24)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout_seconds)
    try:
        client.connect(str(endpoint))
        observed_connection = None
        connection_expectation = expected.get("_controller_connection") if expected else None
        if connection_expectation is not None:
            if not isinstance(connection_expectation, Mapping):
                raise BrokerProtocolError("broker connection expectation is malformed")
            endpoint_identity = connection_expectation.get("endpoint")
            expected_broker = connection_expectation.get("broker")
            if not isinstance(endpoint_identity, Mapping) or not isinstance(expected_broker, Mapping):
                raise BrokerProtocolError("broker connection expectation is incomplete")
            try:
                observed_connection = observe_broker_connection(
                    client,
                    expected_endpoint=endpoint_identity,
                    expected_broker=expected_broker,
                )
            except RuntimeError as exc:
                raise BrokerProtocolError(str(exc)) from exc
        send_control(
            client,
            {
                "schema": BROKER_CONTROL_SCHEMA,
                "operation": "describe_runtime",
                "nonce": nonce,
            },
        )
        frame_type, raw = receive_frame(client)
        if frame_type != BROKER_FRAME_CONTROL:
            raise BrokerProtocolError("broker description was not a control frame")
        description = json.loads(raw.decode("utf-8"))
        if not isinstance(description, dict):
            raise BrokerProtocolError("broker description is not an object")
        if problem := broker_description_problem(description, nonce=nonce, session=session, expected=expected):
            raise BrokerProtocolError(problem)
        if observed_connection is not None:
            description["controller_observed_connection"] = observed_connection
        yield client, description, nonce, session
    finally:
        client.close()


def broker_control_request(
    session_dir: Path,
    operation: str,
    *,
    expected: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if operation not in {"status", "stop"}:
        raise ValueError(f"unsupported broker control operation: {operation}")
    with described_broker_connection(session_dir, expected=expected) as (client, _description, nonce, _session):
        send_control(
            client,
            {
                "schema": BROKER_CONTROL_SCHEMA,
                "operation": operation,
                "handshake_nonce": nonce,
            },
        )
        frame_type, raw = receive_frame(client)
        if frame_type != BROKER_FRAME_CONTROL:
            raise BrokerProtocolError("broker control response was not a control frame")
        response = json.loads(raw.decode("utf-8"))
        if not isinstance(response, dict) or response.get("schema") != BROKER_EVENT_SCHEMA:
            raise BrokerProtocolError("broker control response is malformed")
        if response.get("status") == "error":
            raise BrokerProtocolError(str(response.get("message") or "broker control failed"))
        return response


def prepare_broker_spawn(
    *,
    source: Path,
    broker_output: Path,
    attempt_id: str,
    raw_source: bool = True,
    source_phrase_builder: Callable[..., str] | None = None,
    foundation_marker_prefix: str | None = None,
) -> dict[str, Any]:
    spawn = prepare_fork_spawn(
        {
            "attempt_id": attempt_id,
            "source": str(source.expanduser().resolve()),
            "transcript_path": str(broker_output.expanduser().resolve()),
            "raw_source": raw_source,
            "foundation_marker_prefix": foundation_marker_prefix,
        },
        source_phrase_builder=source_phrase_builder,
        fork_phrase_builder=fork_eval_phrase,
    )
    return {
        **spawn,
        "script": spawn["phrase"].encode("utf-8"),
        "output_path": Path(spawn["transcript_path"]),
    }


def _is_fork_spawn_timeout_event(event: object, *, attempt_id: str) -> bool:
    return (
        isinstance(event, dict)
        and event.get("schema") == BROKER_EVENT_SCHEMA
        and event.get("status") == "error"
        and event.get("message") == FORK_SPAWN_TIMEOUT_MESSAGE.format(attempt_id=attempt_id)
        and event.get("active_children") == []
    )


def _recover_timed_out_broker_spawn(
    session_dir: Path,
    *,
    spawn: Mapping[str, Any],
    expected: Mapping[str, Any] | None,
    description: Mapping[str, Any],
) -> dict[str, Any]:
    """Make the broker identity-bind and quiesce the delayed unregistered child."""

    started = time.monotonic()
    recovery_deadline = started + FORK_SPAWN_RECOVERY_TIMEOUT_SECONDS
    blocker = Path(spawn["ack_path"]).with_name(f".fork-spawn-recovery-{secrets.token_hex(8)}")
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("regular file blocks a nested registration ACK path\n", encoding="utf-8")
    blocked_ack_path = blocker / "registration-ack.json"
    try:
        while time.monotonic() < recovery_deadline:
            remaining = recovery_deadline - time.monotonic()
            with described_broker_connection(
                session_dir,
                expected=expected,
                timeout_seconds=max(1.0, remaining + 0.5),
            ) as (client, recovered_description, nonce, _session):
                if recovered_description.get("generation_sha256") != description.get("generation_sha256"):
                    raise BrokerProtocolError("fork spawn recovery changed restored generation")
                send_control(
                    client,
                    {
                        "schema": BROKER_CONTROL_SCHEMA,
                        "operation": "spawn",
                        "handshake_nonce": nonce,
                        "attempt_id": str(spawn["attempt_id"]),
                        # A blank phrase advances no HOL state. The broker reads
                        # the delayed token from the original phrase after this
                        # write and never treats this as a new proof attempt.
                        "script_b64": base64.b64encode(b"\n").decode("ascii"),
                        "spawn_token": str(spawn["spawn_token"]),
                        "refusal_token": str(spawn["refusal_token"]),
                        # The child still waits on the original ACK path. This
                        # deliberately impossible path makes broker-side ACK
                        # publication fail only after the PID is identity-bound;
                        # the frozen broker then terminates and verifies it.
                        "ack_path": str(blocked_ack_path),
                        "ownership_path": str(Path(spawn["ownership_path"]).resolve()),
                        "output_path": str(Path(spawn["output_path"]).resolve()),
                    },
                )
                frame_type, raw = receive_frame(client)
                if frame_type != BROKER_FRAME_CONTROL:
                    raise BrokerProtocolError("fork spawn recovery response was not a control frame")
                event = json.loads(raw.decode("utf-8"))
                if _is_fork_spawn_timeout_event(event, attempt_id=str(spawn["attempt_id"])):
                    continue
                if not isinstance(event, dict) or event.get("status") != "error":
                    raise BrokerProtocolError("fork spawn recovery did not refuse registration")
                if event.get("active_children") != [] or str(blocker) not in str(event.get("message") or ""):
                    raise BrokerProtocolError("fork spawn recovery did not verify registered-child quiescence")
                status = broker_control_request(session_dir, "status", expected=expected)
                if (
                    status.get("status") != "ready"
                    or status.get("active_children") != []
                    or status.get("generation_sha256") != description.get("generation_sha256")
                ):
                    raise BrokerProtocolError("fork spawn recovery did not preserve an idle exact generation")
                return {
                    "cleanup_status": "broker_registered_then_quiesced",
                    "seat_reusable": True,
                    "recovery_elapsed_seconds": round(time.monotonic() - started, 3),
                }
        raise BrokerProtocolError("fork spawn recovery timed out before identity-bound cleanup")
    finally:
        blocker.unlink(missing_ok=True)
        Path(spawn["ack_path"]).unlink(missing_ok=True)
        Path(spawn["ownership_path"]).unlink(missing_ok=True)


def run_broker_attempt(
    session_dir: Path,
    *,
    spawn: Mapping[str, Any],
    transcript: Path,
    expected: Mapping[str, Any] | None = None,
    cancel_decider: Callable[[bytes, float], str | None] | None = None,
    response_timeout_seconds: float = 900.0,
    fork_spawn_timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    transcript = transcript.expanduser().resolve()
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_bytes(b"")
    started = time.monotonic()
    cancellation_reason: str | None = None
    raw_bytes = 0
    spawn_timeout_event: dict[str, Any] | None = None
    with described_broker_connection(
        session_dir,
        expected=expected,
        timeout_seconds=min(response_timeout_seconds, 5.0),
    ) as (client, description, nonce, _session):
        client.settimeout(0.05)
        send_control(
            client,
            {
                "schema": BROKER_CONTROL_SCHEMA,
                "operation": "spawn",
                "handshake_nonce": nonce,
                "attempt_id": str(spawn["attempt_id"]),
                "script_b64": base64.b64encode(bytes(spawn["script"])).decode("ascii"),
                "spawn_token": str(spawn["spawn_token"]),
                "refusal_token": str(spawn["refusal_token"]),
                "ack_path": str(Path(spawn["ack_path"]).resolve()),
                "ownership_path": str(Path(spawn["ownership_path"]).resolve()),
                "output_path": str(Path(spawn["output_path"]).resolve()),
            },
        )
        deadline = started + response_timeout_seconds
        spawned: dict[str, Any] | None = None
        spawned_elapsed_seconds: float | None = None
        first_output_elapsed_seconds: float | None = None
        last_output_elapsed_seconds: float | None = None
        final_elapsed_seconds: float | None = None
        final: dict[str, Any] | None = None
        cancel_sent = False
        frame_reader = BufferedBrokerFrameReader()
        while final is None:
            if time.monotonic() >= deadline:
                cancellation_reason = cancellation_reason or "controller_response_timeout"
            frame = frame_reader.receive(client)
            if frame is None:
                raw = b""
                frame_type = -1
            else:
                frame_type, raw = frame
            if frame_type == BROKER_FRAME_RAW_OUTPUT:
                with transcript.open("ab") as handle:
                    handle.write(raw)
                raw_bytes += len(raw)
                output_elapsed = time.monotonic() - started
                first_output_elapsed_seconds = first_output_elapsed_seconds or output_elapsed
                last_output_elapsed_seconds = output_elapsed
            elif frame_type == BROKER_FRAME_CONTROL:
                event = json.loads(raw.decode("utf-8"))
                if not isinstance(event, dict) or event.get("schema") != BROKER_EVENT_SCHEMA:
                    raise BrokerProtocolError("broker attempt event is malformed")
                if event.get("status") == "error":
                    if _is_fork_spawn_timeout_event(event, attempt_id=str(spawn["attempt_id"])):
                        spawn_timeout_event = event
                        final = {}
                        continue
                    raise BrokerProtocolError(str(event.get("message") or "broker attempt failed"))
                if event.get("generation_sha256") != description.get("generation_sha256"):
                    raise BrokerProtocolError("broker attempt event changed restored generation")
                if event.get("attempt_id") != spawn["attempt_id"]:
                    raise BrokerProtocolError("broker attempt event changed attempt identity")
                if event.get("status") == "spawned":
                    if spawned is not None:
                        raise BrokerProtocolError("broker emitted duplicate spawn acknowledgement")
                    spawned = event
                    spawned_elapsed_seconds = time.monotonic() - started
                elif event.get("status") in {"completed", "cleanup_failed"}:
                    if spawned is None or event.get("child") != spawned.get("child"):
                        raise BrokerProtocolError("broker final event changed spawned child identity")
                    final = event
                    final_elapsed_seconds = time.monotonic() - started
                    continue
                else:
                    raise BrokerProtocolError(f"unknown broker attempt event: {event.get('status')!r}")
            if spawned is not None and cancellation_reason is None and cancel_decider is not None:
                cancellation_reason = cancel_decider(
                    raw if frame_type == BROKER_FRAME_RAW_OUTPUT else b"", time.monotonic() - started
                )
            if spawned is not None and cancellation_reason is not None and not cancel_sent:
                send_control(
                    client,
                    {
                        "schema": BROKER_CONTROL_SCHEMA,
                        "operation": "cancel",
                        "attempt_id": str(spawn["attempt_id"]),
                    },
                )
                cancel_sent = True
        if spawn_timeout_event is None:
            total_elapsed_seconds = time.monotonic() - started
            return {
                "description": description,
                "spawned": spawned,
                "spawned_elapsed_seconds": spawned_elapsed_seconds,
                "final": final,
                "controller_cancellation_reason": cancellation_reason,
                "elapsed_seconds": round(total_elapsed_seconds, 3),
                "raw_output_bytes": raw_bytes,
                "phase_timing": {
                    "spawn_ack_elapsed_seconds": _round_elapsed(spawned_elapsed_seconds),
                    "first_output_elapsed_seconds": _round_elapsed(first_output_elapsed_seconds),
                    "last_output_elapsed_seconds": _round_elapsed(last_output_elapsed_seconds),
                    "final_response_elapsed_seconds": _round_elapsed(final_elapsed_seconds),
                    "post_output_to_final_seconds": _round_elapsed(
                        (final_elapsed_seconds - last_output_elapsed_seconds)
                        if final_elapsed_seconds is not None and last_output_elapsed_seconds is not None
                        else None
                    ),
                },
                "transcript": str(transcript),
            }
    recovery = _recover_timed_out_broker_spawn(
        session_dir,
        spawn=spawn,
        expected=expected,
        description=description,
    )
    return {
        "description": description,
        "spawned": None,
        "spawned_elapsed_seconds": None,
        "final": None,
        "controller_cancellation_reason": None,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "raw_output_bytes": raw_bytes,
        "transcript": str(transcript),
        "fork_spawn_timeout": {
            "deadline_seconds": fork_spawn_timeout_seconds,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            **recovery,
        },
    }
