"""Process-safe lifecycle operations for a restored fork-broker pool."""

from __future__ import annotations

from pathlib import Path

from hol_workbench.ids import utc_now
from hol_workbench.jsonio import read_json
from hol_workbench.pools.lifecycle import warm_pool_lifecycle_lock
from hol_workbench.pools.session_lifecycle import read_session, send_session_request
from hol_workbench.pools.store import read_warm_pool, warm_pool_lock, write_warm_pool_json
from hol_workbench.proof_run_warm_stop import run_warm_stop_command


def _stop_session(session_dir: Path) -> int:
    session = read_session(session_dir)
    return run_warm_stop_command(
        session_dir=session_dir,
        session=session,
        request_stop=lambda: send_session_request(session_dir, {"action": "stop"}, timeout_seconds=5),
    )


def _write_pool_state(pool_dir: Path, pool: dict) -> None:
    write_warm_pool_json(pool_dir, pool, updated_utc=utc_now())


def stop_pool_runtime(pool_dir: Path) -> int:
    """Stop exactly the processes owned by one pool and preserve its receipt."""

    pool_dir = pool_dir.expanduser().resolve()
    with warm_pool_lifecycle_lock(pool_dir):
        return stop_pool_runtime_locked(pool_dir)


def stop_pool_runtime_locked(pool_dir: Path) -> int:
    """Stop one pool while its lifecycle lock is already held."""

    pool_dir = pool_dir.expanduser().resolve()
    with warm_pool_lock(pool_dir):
        pool = read_warm_pool(pool_dir)
        sessions = [Path(item["session_dir"]) for item in pool.get("sessions") or []]
        engine = pool.get("engine")
        pool["status"] = "stopping"
        for item in pool.get("sessions") or []:
            item["status_before_stop"] = item.get("status")
            item["status"] = "stopping"
        _write_pool_state(pool_dir, pool)
    if engine == "fork_basis":
        sessions = sessions[:1]
    failures = sum(_stop_session(session) != 0 for session in sessions)
    with warm_pool_lock(pool_dir):
        pool = read_warm_pool(pool_dir)
        shared_state = read_json(sessions[0] / "session.json") if engine == "fork_basis" and sessions else None
        for item in pool.get("sessions") or []:
            session_state = shared_state or read_json(Path(item["session_dir"]) / "session.json")
            item["status"] = session_state.get("status") or "stop_failed"
            item["stop_result"] = session_state.get("stop_result")
            if item["status"] == "stopped":
                item["lease"] = None
        pool["status"] = "stopped" if failures == 0 else "stop_failed"
        _write_pool_state(pool_dir, pool)
    print("fork pool stop")
    print(f"pool: {pool_dir}")
    print(f"status: {pool['status']}")
    return 0 if failures == 0 else 1
