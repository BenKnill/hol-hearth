"""Fail-closed cleanup and recovery for internal CRIU lazy-pages launch."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hol_workbench.criu_lazy_pages_owner import (
    OWNER_NAME,
    SOCKET_NAME,
    daemon_liveness,
    read_owner,
    socket_reuse_preflight,
    terminate_bound_daemon,
    terminate_owned_daemon,
)
from hol_workbench.criu_lazy_pages_protocol import LazyPagesLauncher, _close_fd, lazy_pages_image_lock
from hol_workbench.criu_lazy_pages_records import (
    PENDING_NAME,
    _validate_birth_record,
    read_pending_launch,
    write_recovery_receipt,
)
from hol_workbench.jsonio import durable_unlink
from hol_workbench.process_groups import process_birth_identity, process_start_ticks
from hol_workbench.processes import process_is_alive


def retire_prior_owner(
    image_dir: Path,
    record: dict[str, Any],
    *,
    terminate: Callable[..., dict[str, Any]] = terminate_owned_daemon,
) -> dict[str, Any]:
    """Remove reusable artifacts only after verified daemon quiescence."""
    cleanup = terminate(record)
    if cleanup.get("verified_quiescent") is not True:
        raise RuntimeError("prior lazy-pages daemon cleanup is not verified quiescent")
    root = image_dir.resolve()
    for field in ("socket", "pidfile", "log"):
        artifact = Path(str(record[field])).resolve()
        if artifact.parent != root:
            raise RuntimeError(f"prior lazy-pages {field} escapes the CRIU image directory")
        durable_unlink(artifact)
    durable_unlink(root / OWNER_NAME)
    return cleanup


def prepare_launch(
    image_dir: Path,
    *,
    pidfile: Path,
    terminate: Callable[..., dict[str, Any]] = terminate_owned_daemon,
) -> dict[str, Any] | None:
    """Block ambiguous recovery and retire a prior verified owner if present."""
    root = image_dir.resolve()
    if (root / PENDING_NAME).exists():
        raise RuntimeError("an incomplete lazy-pages launch intent requires recovery")
    preflight = socket_reuse_preflight(root)
    if preflight["status"] == "blocked":
        raise RuntimeError(str(preflight["reason"]))
    cleanup = None
    if preflight["status"] == "owned_requires_cleanup":
        cleanup = retire_prior_owner(root, preflight["owner"], terminate=terminate)
    stale_pidfiles = sorted(path for path in root.glob("lazy-pages-*.pid") if path.is_file())
    if stale_pidfiles:
        raise RuntimeError(
            "lazy-pages pidfile exists without a current ready owner or pending recovery: "
            + ", ".join(str(path) for path in stale_pidfiles)
        )
    if pidfile.resolve().parent != root:
        raise RuntimeError("requested lazy-pages pidfile is outside the CRIU image directory")
    return cleanup


def abort_launcher(launcher: LazyPagesLauncher, *, daemon_bound: bool = False) -> dict[str, Any]:
    """Close owned pipes and settle the gated parent without guessing daemon identity."""
    was_released = launcher.released
    _close_fd(launcher, "gate_write_fd")
    _close_fd(launcher, "status_read_fd")
    try:
        launcher.process.wait(timeout=1)
        parent_live = False
    except subprocess.TimeoutExpired:
        parent_live = True
    if was_released and not daemon_bound:
        return {
            "status": "unverified",
            "verified_quiescent": False,
            "verified_launcher_quiescent": not parent_live,
            "reason": "launcher was released but no daemon birth identity was bound",
            "surviving_pids": [launcher.pid] if parent_live else [],
            "daemon_identity_unavailable": True,
        }
    return {
        "status": "survivors" if parent_live else "quiescent",
        "verified_quiescent": False,
        "verified_launcher_quiescent": not parent_live,
        "reason": (
            "owned launcher parent settled"
            if not parent_live
            else "owned launcher parent survived the bounded settle wait"
        ),
        "surviving_pids": [launcher.pid] if parent_live else [],
    }


def cleanup_failed_launch(
    launcher: LazyPagesLauncher | None,
    *,
    pidfile: Path,
    bound_daemon: dict[str, Any] | None = None,
    terminate: Callable[..., dict[str, Any]] = terminate_bound_daemon,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Clean only an in-memory bound daemon; a PID-only pidfile is not authority."""
    if launcher is not None and launcher.released and bound_daemon is not None:
        try:
            cleanup = terminate(bound_daemon)
        except BaseException as exc:
            cleanup = {
                "status": "unverified",
                "verified_quiescent": False,
                "reason": f"daemon cleanup raised {type(exc).__name__}: {exc}",
                "surviving_pids": [bound_daemon["pid"]],
            }
        try:
            launcher_cleanup = abort_launcher(launcher, daemon_bound=True)
        except BaseException as exc:
            launcher_cleanup = {
                "status": "unverified",
                "verified_quiescent": False,
                "verified_launcher_quiescent": False,
                "reason": f"launcher settle raised {type(exc).__name__}: {exc}",
                "surviving_pids": [launcher.pid],
            }
        cleanup["launcher_cleanup"] = launcher_cleanup
        daemon_verified = cleanup.get("verified_quiescent") is True
        launcher_verified = launcher_cleanup.get("verified_launcher_quiescent") is True
        cleanup["verified_daemon_quiescent"] = daemon_verified
        cleanup["verified_quiescent"] = daemon_verified and launcher_verified
        if cleanup["verified_quiescent"] is not True:
            cleanup["status"] = "unverified"
            cleanup["surviving_pids"] = sorted(
                set(cleanup.get("surviving_pids") or []) | set(launcher_cleanup.get("surviving_pids") or [])
            )
        return bound_daemon, cleanup
    cleanup = (
        abort_launcher(launcher)
        if launcher is not None
        else {
            "status": "unverified",
            "verified_quiescent": False,
            "verified_launcher_quiescent": False,
            "reason": "lazy-pages launcher was not acquired",
            "surviving_pids": [],
        }
    )
    if launcher is not None and launcher.released and pidfile.is_file():
        cleanup["reason"] = "released launch has a pidfile but no persisted or in-memory daemon birth identity"
        cleanup["daemon_identity_unavailable"] = True
        cleanup["verified_quiescent"] = False
        cleanup["status"] = "unverified"
    return bound_daemon, cleanup


def clear_verified_daemon_artifacts(image_dir: Path, *, pidfile: Path, pending: Path | None) -> None:
    root = image_dir.resolve()
    for artifact in (root / SOCKET_NAME, root / OWNER_NAME, pidfile, pending):
        if artifact is not None:
            durable_unlink(artifact)


def _pending_launcher_liveness(
    pending_record: dict[str, Any],
    *,
    start_ticks_reader: Callable[[Any], int | None],
    identity_reader: Callable[[Any], str | None],
    pid_alive: Callable[[Any], bool],
) -> str:
    launcher = _validate_birth_record("launcher", pending_record.get("launcher"))
    pid = launcher["pid"]
    if not pid_alive(pid):
        return "gone"
    ticks = start_ticks_reader(pid)
    identity = identity_reader(pid)
    if ticks is None or identity is None or identity != f"linux-proc:{ticks}":
        return "unavailable"
    if ticks != launcher["start_ticks"] or identity != launcher["birth_identity"]:
        return "replaced"
    return "exact"


def recover_lazy_pages_launch(
    image_dir: Path,
    *,
    terminate_prior: Callable[..., dict[str, Any]] = terminate_owned_daemon,
    terminate_bound: Callable[..., dict[str, Any]] = terminate_bound_daemon,
    start_ticks_reader: Callable[[Any], int | None] = process_start_ticks,
    identity_reader: Callable[[Any], str | None] = process_birth_identity,
    pid_alive: Callable[[Any], bool] = process_is_alive,
) -> dict[str, Any]:
    """Recover one durable launch intent without guessing at daemon identity."""
    root = image_dir.resolve()
    with lazy_pages_image_lock(root):
        pending_record = read_pending_launch(root)
        if pending_record is None:
            return {"status": "clear", "verified_quiescent": False, "receipt": None}
        pending_path = root / PENDING_NAME
        pidfile = Path(pending_record["pidfile"]).resolve()
        current_owner = read_owner(root)
        if current_owner is not None:
            return _recover_with_owner(
                root,
                pending_path=pending_path,
                pending_record=pending_record,
                current_owner=current_owner,
                terminate_prior=terminate_prior,
                start_ticks_reader=start_ticks_reader,
                identity_reader=identity_reader,
                pid_alive=pid_alive,
            )
        return _recover_without_owner(
            root,
            pending_path=pending_path,
            pending_record=pending_record,
            pidfile=pidfile,
            terminate_bound=terminate_bound,
            start_ticks_reader=start_ticks_reader,
            identity_reader=identity_reader,
            pid_alive=pid_alive,
        )


def _recover_with_owner(
    root: Path,
    *,
    pending_path: Path,
    pending_record: dict[str, Any],
    current_owner: dict[str, Any],
    terminate_prior: Callable[..., dict[str, Any]],
    start_ticks_reader: Callable[[Any], int | None],
    identity_reader: Callable[[Any], str | None],
    pid_alive: Callable[[Any], bool],
) -> dict[str, Any]:
    matching_owner = all(
        Path(str(current_owner[field])).resolve() == Path(str(pending_record[field])).resolve()
        for field in ("socket", "pidfile", "log")
    )
    if not matching_owner:
        cleanup = {
            "status": "unverified",
            "verified_quiescent": False,
            "reason": "pending intent does not match the published owner artifacts",
            "surviving_pids": [current_owner["daemon"]["pid"]],
        }
        receipt = write_recovery_receipt(
            root,
            status="blocked_owner_mismatch",
            pending_record=pending_record,
            owner=current_owner,
            daemon_attempt=None,
            cleanup=cleanup,
        )
        return {**cleanup, "receipt": receipt}
    liveness = daemon_liveness(
        current_owner["daemon"],
        start_ticks_reader=start_ticks_reader,
        identity_reader=identity_reader,
        pid_alive=pid_alive,
    )
    if liveness == "exact":
        cleanup = {
            "status": "owner_ready",
            "verified_quiescent": False,
            "reason": "published owner is live; only stale pending intent was removed",
            "surviving_pids": [],
        }
        try:
            durable_unlink(pending_path)
        except OSError as exc:
            cleanup["pending_clear_error"] = f"{type(exc).__name__}: {exc}"
            status = "owner_ready_pending_retained"
        else:
            status = "owner_ready_pending_cleared"
        receipt = write_recovery_receipt(
            root,
            status=status,
            pending_record=pending_record,
            owner=current_owner,
            daemon_attempt=current_owner["daemon"],
            cleanup=cleanup,
        )
        return {**cleanup, "receipt": receipt}
    if liveness in {"gone", "replaced"}:
        cleanup = retire_prior_owner(root, current_owner, terminate=terminate_prior)
        try:
            durable_unlink(pending_path)
        except OSError as exc:
            cleanup["pending_clear_error"] = f"{type(exc).__name__}: {exc}"
            status = "stale_owner_clean_pending_retained"
        else:
            status = "stale_owner_cleared"
        receipt = write_recovery_receipt(
            root,
            status=status,
            pending_record=pending_record,
            owner=current_owner,
            daemon_attempt=current_owner["daemon"],
            cleanup=cleanup,
        )
        return {**cleanup, "receipt": receipt}
    cleanup = {
        "status": "unverified",
        "verified_quiescent": False,
        "reason": "published owner daemon identity is unavailable during recovery",
        "surviving_pids": [current_owner["daemon"]["pid"]],
    }
    receipt = write_recovery_receipt(
        root,
        status="blocked_owner_unavailable",
        pending_record=pending_record,
        owner=current_owner,
        daemon_attempt=current_owner["daemon"],
        cleanup=cleanup,
    )
    return {**cleanup, "receipt": receipt}


def _recover_without_owner(
    root: Path,
    *,
    pending_path: Path,
    pending_record: dict[str, Any],
    pidfile: Path,
    terminate_bound: Callable[..., dict[str, Any]],
    start_ticks_reader: Callable[[Any], int | None],
    identity_reader: Callable[[Any], str | None],
    pid_alive: Callable[[Any], bool],
) -> dict[str, Any]:
    daemon_attempt = pending_record.get("daemon") if pending_record.get("state") == "daemon_bound" else None
    launcher_liveness = _pending_launcher_liveness(
        pending_record,
        start_ticks_reader=start_ticks_reader,
        identity_reader=identity_reader,
        pid_alive=pid_alive,
    )
    if not isinstance(daemon_attempt, dict):
        cleanup = {
            "status": "unverified",
            "verified_quiescent": False,
            "verified_daemon_quiescent": False,
            "launcher_liveness": launcher_liveness,
            "reason": "pending launch has no persisted daemon birth identity; pidfile PID is not kill authority",
            "pidfile_present": pidfile.is_file(),
            "socket_present": (root / SOCKET_NAME).exists(),
            "surviving_pids": (
                [pending_record["launcher"]["pid"]] if launcher_liveness in {"exact", "unavailable"} else []
            ),
        }
        receipt = write_recovery_receipt(
            root,
            status="blocked_missing_daemon_identity",
            pending_record=pending_record,
            owner=None,
            daemon_attempt=None,
            cleanup=cleanup,
        )
        return {**cleanup, "receipt": receipt}

    daemon_state = daemon_liveness(
        daemon_attempt,
        start_ticks_reader=start_ticks_reader,
        identity_reader=identity_reader,
        pid_alive=pid_alive,
    )
    if daemon_state == "exact":
        try:
            cleanup = terminate_bound(daemon_attempt)
        except BaseException as exc:
            cleanup = {
                "status": "unverified",
                "verified_quiescent": False,
                "reason": f"persisted daemon cleanup raised {type(exc).__name__}: {exc}",
                "surviving_pids": [daemon_attempt["pid"]],
            }
    elif daemon_state in {"gone", "replaced"}:
        cleanup = {
            "status": "quiescent",
            "verified_quiescent": True,
            "reason": f"persisted daemon birth identity is {daemon_state}",
            "surviving_pids": [],
        }
    else:
        cleanup = {
            "status": "unverified",
            "verified_quiescent": False,
            "reason": "persisted daemon birth identity is unavailable",
            "surviving_pids": [daemon_attempt["pid"]],
        }
    cleanup["daemon_liveness"] = daemon_state
    cleanup["launcher_liveness"] = launcher_liveness
    daemon_verified = cleanup.get("verified_quiescent") is True
    launcher_verified = launcher_liveness in {"gone", "replaced"}
    cleanup["verified_daemon_quiescent"] = daemon_verified
    cleanup["verified_quiescent"] = daemon_verified and launcher_verified
    if not launcher_verified:
        launcher_pid = pending_record["launcher"]["pid"]
        cleanup["surviving_pids"] = sorted(set(cleanup.get("surviving_pids") or []) | {launcher_pid})
    status = "blocked_survivors"
    if cleanup["verified_quiescent"] is True:
        try:
            clear_verified_daemon_artifacts(root, pidfile=pidfile, pending=pending_path)
        except OSError as exc:
            cleanup["artifact_clear_error"] = f"{type(exc).__name__}: {exc}"
            status = "cleanup_verified_artifacts_retained"
        else:
            status = "recovered"
    receipt = write_recovery_receipt(
        root,
        status=status,
        pending_record=pending_record,
        owner=None,
        daemon_attempt=daemon_attempt,
        cleanup=cleanup,
    )
    return {**cleanup, "receipt": receipt}
