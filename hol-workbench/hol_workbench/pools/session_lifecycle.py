"""Minimal process and socket lifecycle for a warm session."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from hol_workbench.hashing import sha256_text
from hol_workbench.jsonio import read_json
from hol_workbench.process_groups import process_birth_identity, process_start_ticks
from hol_workbench.processes import process_is_alive


def session_socket_path(session_dir: Path) -> Path:
    """Return the short Unix-socket path recorded for one physical session."""

    control_dir = session_dir.resolve()
    try:
        session = read_json(control_dir / "session.json")
    except (OSError, ValueError):
        session = {}
    recorded_control = session.get("control_session_dir") if isinstance(session, dict) else None
    if isinstance(recorded_control, str) and recorded_control:
        control_dir = Path(recorded_control).resolve()
    digest = sha256_text(str(control_dir))
    if digest is None:
        raise AssertionError("a concrete warm control path must have a digest")
    return Path("/tmp") / f"proof-run-warm-{digest[:16]}.sock"


def send_session_request(
    session_dir: Path,
    request: dict[str, Any],
    *,
    timeout_seconds: float | None = 30.0,
) -> dict[str, Any]:
    """Send one bounded request without importing authoring cards or reports."""

    try:
        session = read_json(session_dir / "session.json")
    except (OSError, ValueError):
        session = {}
    if session.get("execution_topology") == "mechanical_basis_broker_v3":
        action = str(request.get("action") or "")
        if action not in {"status", "stop"}:
            raise RuntimeError("mechanical broker evaluation requires the current external controller")
        from hol_workbench.proof_run_fork_broker_client import broker_control_request

        return broker_control_request(session_dir, action)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        if timeout_seconds is not None:
            client.settimeout(timeout_seconds)
        client.connect(str(session_socket_path(session_dir)))
        client.sendall(json.dumps(request, sort_keys=True).encode("utf-8") + b"\n")
        chunks: list[bytes] = []
        while True:
            data = client.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise RuntimeError("warm worker returned an empty response")
    response = json.loads(raw.decode("utf-8"))
    if not isinstance(response, dict):
        raise RuntimeError("warm worker returned a non-object response")
    if response.get("status") == "error":
        raise RuntimeError(response.get("message") or "warm worker error")
    return response


def read_session(session_dir: Path) -> dict[str, Any]:
    session_json = session_dir / "session.json"
    if not session_json.exists():
        raise SystemExit(f"warm session metadata not found: {session_json}")
    return read_json(session_json)


def pool_session_alive(item: dict[str, Any]) -> bool:
    """Require both recorded Linux identities for the broker and HOL basis."""

    session_dir = Path(str(item.get("session_dir") or ""))
    session = read_json(session_dir / "session.json")
    if not session:
        return False
    try:
        basis_pid = int(str(session.get("basis_pid") or ""))
        hol_pid = int(str(session.get("hol_pid") or ""))
    except (TypeError, ValueError):
        return False
    if basis_pid <= 0 or hol_pid != basis_pid:
        return False

    def role_alive(*, pid_key: str, ticks_key: str, identity_key: str) -> bool:
        raw_pid = session.get(pid_key)
        raw_ticks = session.get(ticks_key)
        if raw_pid is None or raw_ticks is None:
            return False
        try:
            pid = int(raw_pid)
            expected_ticks = int(raw_ticks)
        except (TypeError, ValueError):
            return False
        expected_identity = session.get(identity_key)
        if pid <= 0 or expected_ticks <= 0 or not isinstance(expected_identity, str):
            return False
        if expected_identity != f"linux-proc:{expected_ticks}":
            return False
        return (
            process_is_alive(pid)
            and process_start_ticks(pid) == expected_ticks
            and process_birth_identity(pid) == expected_identity
        )

    return role_alive(
        pid_key="worker_pid",
        ticks_key="worker_start_ticks",
        identity_key="worker_identity",
    ) and role_alive(
        pid_key="basis_pid",
        ticks_key="basis_start_ticks",
        identity_key="basis_identity",
    )
