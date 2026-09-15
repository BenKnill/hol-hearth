"""Crash-safe controller and exact-profile cleanup for warm readiness probes."""

from __future__ import annotations

import fcntl
import io
import os
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from hol_workbench.cli import orbstack_criu_restore
from hol_workbench.criu_restore_transaction import capture_authoritative_tree, terminate_restored_tree
from hol_workbench.fork_pool_lifecycle import stop_pool_runtime_locked
from hol_workbench.pools import read_warm_pool, warm_pool_lock
from hol_workbench.pools.lifecycle import warm_pool_lifecycle_lock
from hol_workbench.process_groups import process_birth_identity
from hol_workbench.processes import process_is_alive
from hol_workbench.proof_run_fork_children import read_active_children
from hol_workbench.proof_run_warm_session import write_warm_pool

CONTROLLER_LOCK_FILENAME = "warm-vanilla-readiness.lock"
RecoverProfile = Callable[[Path], int | dict[str, Any]]


class ReadinessControllerBusy(RuntimeError):
    """Another controller owns this readiness run root."""


@contextmanager
def readiness_controller_lock(run_root: Path) -> Iterator[Path]:
    """Hold one nonblocking run-root lock for an entire readiness invocation."""
    path = run_root / CONTROLLER_LOCK_FILENAME
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReadinessControllerBusy(f"readiness controller lock is held: {path}") from exc
        try:
            yield path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def controller_record() -> dict[str, Any]:
    pid = os.getpid()
    return {
        "pid": pid,
        "identity": process_birth_identity(pid),
        "nonce": secrets.token_hex(16),
    }


def controller_identity_complete(record: Any) -> bool:
    return (
        isinstance(record, dict)
        and isinstance(record.get("pid"), int)
        and record["pid"] > 0
        and isinstance(record.get("identity"), str)
        and bool(record["identity"])
        and isinstance(record.get("nonce"), str)
        and len(record["nonce"]) >= 16
    )


def controller_is_live(record: Any) -> bool:
    return (
        controller_identity_complete(record)
        and orbstack_criu_restore.pid_is_alive(record["pid"])
        and process_birth_identity(record["pid"]) == record["identity"]
    )


def bounded_output_tail(text: str, *, max_chars: int = 1_200, max_lines: int = 3) -> list[str]:
    """Return a line- and character-bounded suffix without a partial leading word."""
    lines = text.splitlines()[-max_lines:]
    tail = "\n".join(lines)
    if len(tail) <= max_chars:
        return tail.splitlines()
    tail = tail[-max_chars:]
    if tail and not tail[0].isspace():
        boundary = next((index for index, char in enumerate(tail) if char.isspace()), None)
        if boundary is None:
            return [f"[output token omitted: {len(text)} characters]"]
        tail = tail[boundary:].lstrip()
    return tail.splitlines()


def bounded_message(text: str, *, max_chars: int = 800) -> str:
    """Bound one diagnostic without cutting its final displayed word."""
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    prefix = compact[:max_chars]
    if not compact[max_chars].isspace():
        if " " not in prefix:
            return f"[diagnostic token omitted: {len(compact)} characters]"
        prefix = prefix.rsplit(" ", 1)[0]
    return prefix.rstrip() + " ..."


def _one_pool(profile_root: Path) -> Path:
    pools = sorted((profile_root / "pool").glob("*"))
    if len(pools) != 1:
        raise RuntimeError(f"expected one pool under {profile_root / 'pool'}, found {len(pools)}")
    return pools[0]


def profile_runtime_state(profile_root: Path) -> dict[str, Any]:
    """Read whether an exact physical shelf was live before this smoke."""
    resolved = profile_root.expanduser().resolve()
    try:
        pool = _one_pool(resolved)
        with warm_pool_lock(pool):
            data = read_warm_pool(pool)
            runtime_live = orbstack_criu_restore.pool_is_live(pool)
        return {
            "known": True,
            "profile_root": str(resolved),
            "pool": str(pool),
            "pool_status": str(data.get("status") or "unknown"),
            "runtime_live": runtime_live,
        }
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError) as exc:
        return {
            "known": False,
            "profile_root": str(resolved),
            "runtime_live": None,
            "reason": f"{type(exc).__name__}: {exc}",
        }


def _live_lease_owners(pool_data: dict[str, Any]) -> list[dict[str, Any]]:
    owners: list[dict[str, Any]] = []
    for session in pool_data.get("sessions") or []:
        if not isinstance(session, dict) or not isinstance(session.get("lease"), dict):
            continue
        lease = session["lease"]
        owner_pid = lease.get("owner_pid")
        if isinstance(owner_pid, int) and owner_pid > 0 and process_is_alive(owner_pid):
            owners.append(
                {
                    "worker_index": session.get("worker_index"),
                    "owner_pid": owner_pid,
                    "owner_kind": lease.get("owner_kind"),
                    "lease_id": lease.get("lease_id"),
                }
            )
    return owners


def _verified_stopped(pool: Path) -> tuple[bool, dict[str, Any]]:
    with warm_pool_lock(pool):
        data = read_warm_pool(pool)
    sessions = [item for item in data.get("sessions") or [] if isinstance(item, dict)]
    verified = bool(sessions) and all(
        item.get("status") == "stopped"
        and isinstance(item.get("stop_result"), dict)
        and item["stop_result"].get("verified_quiescent") is True
        for item in sessions
    )
    return data.get("status") == "stopped" and verified, data


def _mark_stop_failed(pool: Path, reason: str) -> None:
    with warm_pool_lock(pool):
        data = read_warm_pool(pool)
        data["status"] = "stop_failed"
        for session in data.get("sessions") or []:
            if not isinstance(session, dict):
                continue
            session["status"] = "stop_failed"
            session["stop_result"] = {
                "status": "stop_failed",
                "verified_quiescent": False,
                "reason": reason,
            }
        write_warm_pool(pool, data)


def _retire_identity_displaced_broker(pool: Path, pool_data: dict[str, Any]) -> dict[str, Any] | None:
    """Stop one restored broker whose CRIU birth identity displaced the saved one.

    This is recovery-only. The current process must name this exact pool in
    its command line before it is treated as belonging to the shelf; a reused
    PID is never enough authority to terminate anything.
    """
    sessions = [item for item in pool_data.get("sessions") or [] if isinstance(item, dict)]
    workers = {(item.get("worker_pid"), item.get("worker_identity")) for item in sessions}
    if len(workers) != 1:
        return None
    worker_pid, expected_identity = workers.pop()
    if not isinstance(worker_pid, int) or worker_pid <= 0 or not orbstack_criu_restore.pid_is_alive(worker_pid):
        return None
    if isinstance(expected_identity, str) and process_birth_identity(worker_pid) == expected_identity:
        return None
    try:
        command_line = Path(f"/proc/{worker_pid}/cmdline").read_bytes()
    except OSError as exc:
        raise RuntimeError(f"cannot inspect identity-displaced broker {worker_pid}") from exc
    pool_argument = b"--pool-dir\0" + os.fsencode(pool) + b"\0"
    if pool_argument not in command_line:
        raise RuntimeError(f"identity-displaced PID {worker_pid} does not name exact pool {pool}; refusing termination")
    tree = capture_authoritative_tree(worker_pid)
    return terminate_restored_tree(tree, root_pid=worker_pid)


def recover_and_stop_profile_root(profile_root: Path) -> dict[str, Any]:
    """Recover a restore transaction and verified-stop one exact physical shelf."""
    resolved = profile_root.expanduser().resolve()
    pool = _one_pool(resolved)
    image_dir = resolved / "criu-image"
    stdout = io.StringIO()
    stderr = io.StringIO()
    receipt: dict[str, Any] = {
        "status": "stop_failed",
        "exit_status": 1,
        "profile_root": str(resolved),
        "pool": str(pool),
        "incomplete_restore_recovered": False,
    }
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr), warm_pool_lifecycle_lock(pool):
            with warm_pool_lock(pool):
                before = read_warm_pool(pool)
                pool_status_before = before.get("status")
                live_owners = _live_lease_owners(before)
                if live_owners:
                    receipt["reason"] = "live shelf admission owner prevents recovery stop"
                    receipt["live_owners"] = live_owners
                    return {
                        **receipt,
                        "stdout_tail": bounded_output_tail(stdout.getvalue()),
                        "stderr_tail": bounded_output_tail(stderr.getvalue()),
                    }
                if before.get("status") not in {"restoring", "restore_failed", "stopping", "stop_failed"}:
                    before["status"] = "stopping"
                    write_warm_pool(pool, before)
                active_children = read_active_children(pool, require_registry=True)
                transaction_recovered = orbstack_criu_restore.recover_incomplete_restore_locked(
                    image_dir=image_dir,
                    pool=pool,
                )
                displaced_cleanup = _retire_identity_displaced_broker(pool, before)
                if displaced_cleanup is not None and displaced_cleanup.get("verified_quiescent") is not True:
                    raise RuntimeError("identity-displaced broker did not stop quiescently")
                current = read_warm_pool(pool)
                current["status"] = "stopping"
                write_warm_pool(pool, current)
            stop_status = stop_pool_runtime_locked(pool)
            verified, final_pool = _verified_stopped(pool)
            receipt.update(
                {
                    "status": "stopped" if stop_status == 0 and verified else "stop_failed",
                    "exit_status": 0 if stop_status == 0 and verified else 1,
                    "identity_displaced_broker_cleanup": displaced_cleanup,
                    "incomplete_restore_recovered": transaction_recovered,
                    "active_children_before_stop": active_children,
                    "pool_status_before": pool_status_before,
                    "pool_status_after": final_pool.get("status"),
                    "verified_quiescent": verified,
                }
            )
            if not verified:
                _mark_stop_failed(pool, "exact physical-profile stop was not verified quiescent")
    except (OSError, RuntimeError, SystemExit, TypeError, ValueError) as exc:
        receipt["reason"] = f"{type(exc).__name__}: {exc}"
        try:
            with warm_pool_lifecycle_lock(pool):
                _mark_stop_failed(pool, receipt["reason"])
        except (OSError, RuntimeError, SystemExit, TypeError, ValueError):
            pass
    receipt["stdout_tail"] = bounded_output_tail(stdout.getvalue())
    receipt["stderr_tail"] = bounded_output_tail(stderr.getvalue())
    return receipt
