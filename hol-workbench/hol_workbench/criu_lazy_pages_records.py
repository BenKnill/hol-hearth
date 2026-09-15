"""Durable launch, failure, and recovery records for CRIU lazy-pages."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hol_workbench.criu_lazy_pages_owner import SOCKET_NAME, validate_bound_daemon
from hol_workbench.criu_lazy_pages_protocol import (
    STATUS_FD,
    LazyPagesLauncher,
    _validate_paths,
    launcher_record,
)
from hol_workbench.jsonio import durable_atomic_write_json

PENDING_SCHEMA = "hol-workbench.criu-lazy-pages-launch.v1"
FAILURE_SCHEMA = "hol-workbench.criu-lazy-pages-launch-failure.v1"
RECOVERY_SCHEMA = "hol-workbench.criu-lazy-pages-recovery.v1"
PENDING_NAME = "lazy-pages-launch.json"
FAILURE_PHASES = {
    "preflight",
    "spawn",
    "pending_publication",
    "gate_release",
    "status_wait",
    "parent_reap",
    "daemon_bind",
    "daemon_handoff",
    "owner_publication",
}


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _operation_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")


def write_pending_launch(
    image_dir: Path,
    *,
    pidfile: Path,
    log: Path,
    launcher: LazyPagesLauncher,
    controller_pid: int,
    controller_start_ticks: int,
    controller_identity: str,
) -> Path:
    """Persist conservative release intent before opening the launcher gate."""
    root, resolved_pidfile, resolved_log = _validate_paths(image_dir, pidfile, log)
    if launcher.identity != f"linux-proc:{launcher.start_ticks}":
        raise RuntimeError("lazy-pages launcher pending record requires a Linux birth identity")
    if controller_identity != f"linux-proc:{controller_start_ticks}":
        raise RuntimeError("lazy-pages controller pending record requires a Linux birth identity")
    path = root / PENDING_NAME
    durable_atomic_write_json(
        path,
        {
            "schema": PENDING_SCHEMA,
            "state": "release_authorized",
            "image_dir": str(root),
            "socket": str(root / SOCKET_NAME),
            "pidfile": str(resolved_pidfile),
            "log": str(resolved_log),
            "status_fd": STATUS_FD,
            "command": launcher.command,
            "launcher": launcher_record(launcher),
            "controller": {
                "pid": controller_pid,
                "start_ticks": controller_start_ticks,
                "birth_identity": controller_identity,
            },
            "created_utc": _utc_now(),
        },
    )
    return path


def write_bound_pending(image_dir: Path, daemon: dict[str, Any]) -> Path:
    """Add the exact daemon birth to pending before owner publication."""
    root = image_dir.resolve()
    path = root / PENDING_NAME
    record = read_pending_launch(root)
    if record is None or record.get("state") != "release_authorized":
        raise RuntimeError("lazy-pages daemon handoff requires release-authorized pending intent")
    record["state"] = "daemon_bound"
    record["daemon"] = validate_bound_daemon(daemon)
    record["daemon_bound_utc"] = _utc_now()
    durable_atomic_write_json(path, record)
    return path


def write_launch_failure(
    image_dir: Path,
    *,
    phase: str,
    error: BaseException,
    launcher: LazyPagesLauncher | None,
    pidfile: Path,
    log: Path,
    daemon_attempt: dict[str, Any] | None,
    cleanup: dict[str, Any],
) -> Path:
    if phase not in FAILURE_PHASES:
        raise RuntimeError(f"unsupported lazy-pages failure phase: {phase}")
    root, resolved_pidfile, resolved_log = _validate_paths(image_dir, pidfile, log)
    path = root / f"lazy-pages-launch-failed-{_operation_stamp()}.json"
    durable_atomic_write_json(
        path,
        {
            "schema": FAILURE_SCHEMA,
            "status": "lazy_pages_launch_failed",
            "phase": phase,
            "reason": f"{type(error).__name__}: {error}",
            "pidfile": str(resolved_pidfile),
            "log": str(resolved_log),
            "launcher": launcher_record(launcher) if launcher is not None else None,
            "launcher_released": launcher.released if launcher is not None else False,
            "daemon_attempt": daemon_attempt,
            "cleanup": cleanup,
            "verified_quiescent": cleanup.get("verified_quiescent") is True,
            "surviving_pids": cleanup.get("surviving_pids") or [],
            "created_utc": _utc_now(),
        },
    )
    return path


def _validate_birth_record(label: str, record: object) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise RuntimeError(f"lazy-pages pending {label} identity is unavailable")
    pid = record.get("pid")
    ticks = record.get("start_ticks")
    identity = record.get("birth_identity")
    if (
        not isinstance(pid, int)
        or pid <= 0
        or not isinstance(ticks, int)
        or ticks <= 0
        or identity != f"linux-proc:{ticks}"
    ):
        raise RuntimeError(f"lazy-pages pending {label} identity is invalid")
    return dict(record)


def read_pending_launch(image_dir: Path) -> dict[str, Any] | None:
    root = image_dir.resolve()
    path = root / PENDING_NAME
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"lazy-pages pending intent is unreadable: {path}") from exc
    if not isinstance(record, dict) or record.get("schema") != PENDING_SCHEMA:
        raise RuntimeError("lazy-pages pending intent has an unsupported schema")
    state = record.get("state")
    if state not in {"release_authorized", "daemon_bound"}:
        raise RuntimeError("lazy-pages pending intent has an unsupported state")
    if Path(str(record.get("image_dir") or "")).resolve() != root:
        raise RuntimeError("lazy-pages pending intent image directory does not match")
    if Path(str(record.get("socket") or "")).resolve() != root / SOCKET_NAME:
        raise RuntimeError("lazy-pages pending intent socket is not canonical")
    pidfile = Path(str(record.get("pidfile") or ""))
    log = Path(str(record.get("log") or ""))
    _validate_paths(root, pidfile, log)
    if record.get("status_fd") != STATUS_FD:
        raise RuntimeError("lazy-pages pending intent status fd does not match the supported route")
    _validate_birth_record("launcher", record.get("launcher"))
    _validate_birth_record("controller", record.get("controller"))
    if state == "daemon_bound":
        validate_bound_daemon(record.get("daemon"))
    elif record.get("daemon") is not None:
        raise RuntimeError("release-authorized lazy-pages pending intent cannot claim a daemon birth")
    return record


def write_recovery_receipt(
    image_dir: Path,
    *,
    status: str,
    pending_record: dict[str, Any],
    owner: dict[str, Any] | None,
    daemon_attempt: dict[str, Any] | None,
    cleanup: dict[str, Any],
) -> Path:
    root = image_dir.resolve()
    path = root / f"lazy-pages-recovery-{_operation_stamp()}.json"
    durable_atomic_write_json(
        path,
        {
            "schema": RECOVERY_SCHEMA,
            "status": status,
            "pending": pending_record,
            "owner": owner,
            "daemon_attempt": daemon_attempt,
            "cleanup": cleanup,
            "verified_quiescent": cleanup.get("verified_quiescent") is True,
            "surviving_pids": cleanup.get("surviving_pids") or [],
            "created_utc": _utc_now(),
        },
    )
    return path
