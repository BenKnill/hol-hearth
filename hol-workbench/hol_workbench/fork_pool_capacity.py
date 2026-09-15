"""Controller-side logical capacity for one fork-basis warm pool."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from hol_workbench.criu_pool_restore import restored_pool_lock
from hol_workbench.criu_shelf_capacity import broker_pool_attempt_limit, physical_broker_capacity
from hol_workbench.criu_snapshot_admission import StaticSnapshotAdmissionDecision
from hol_workbench.criu_snapshot_compat import (
    validate_authoring_shelf_manifest,
    verify_static_snapshot_admission_decision,
)
from hol_workbench.jsonio import atomic_write_json, read_json
from hol_workbench.proof_run_runtime import utc_now
from hol_workbench.proof_run_warm_session import warm_socket_path, write_warm_pool, write_warm_session_card

EXPANDABLE_POOL_STATES = {"ready", "stopped"}


def _physical_session(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    for session in sessions:
        if not session.get("control_session_dir"):
            return session
    raise RuntimeError("fork pool has no physical control session")


def _logical_session_dir(pool_dir: Path, worker_index: int) -> Path:
    return pool_dir / "sessions" / f"logical-seat-{worker_index}"


def ensure_profile_logical_capacity(
    profile_root: Path,
    target_capacity: int,
    *,
    admission_decision: StaticSnapshotAdmissionDecision | None = None,
) -> dict[str, Any]:
    """Expand one validated profile shelf without restoring or rebuilding it."""

    if admission_decision is None:
        validate_authoring_shelf_manifest(profile_root)
    else:
        verify_static_snapshot_admission_decision(profile_root, admission_decision)
    pools = sorted((profile_root / "pool").glob("*"))
    if len(pools) != 1:
        raise RuntimeError(f"expected one pool under {profile_root / 'pool'}, found {len(pools)}")
    with restored_pool_lock(pools[0]):
        return ensure_fork_pool_logical_capacity(pools[0], target_capacity)


def ensure_fork_pool_logical_capacity(pool_dir: Path, target_capacity: int) -> dict[str, Any]:
    """Expand a fork pool with logical aliases to its existing control socket.

    A fork-basis pool owns one loaded HOL parent. Its seats are only exclusive
    lease records; the worker already accepts concurrent control clients and
    serializes the short parent-fork handshake. Expanding therefore creates no
    HOL process and does not alter the CRIU image.

    The caller must hold the pool lifecycle and metadata locks.
    """

    if target_capacity <= 0:
        raise ValueError("logical fork-pool capacity must be positive")
    pool = read_json(pool_dir / "pool.json")
    if not isinstance(pool, dict):
        raise TypeError("pool metadata is not a JSON object")
    if pool.get("engine") != "fork_basis":
        raise RuntimeError("logical capacity expansion requires a fork_basis pool")
    status = str(pool.get("status") or "unknown")
    if status not in EXPANDABLE_POOL_STATES:
        raise RuntimeError(f"logical capacity expansion requires pool status ready or stopped, found {status}")
    raw_sessions = pool.get("sessions")
    if (
        not isinstance(raw_sessions, list)
        or not raw_sessions
        or any(not isinstance(item, dict) for item in raw_sessions)
    ):
        raise RuntimeError("fork pool has no valid logical session inventory")
    sessions: list[dict[str, Any]] = raw_sessions
    current_capacity = len(sessions)
    requested = int(pool.get("size_requested") or current_capacity)
    physical_capacity = physical_broker_capacity(pool)
    if physical_capacity is not None:
        # Aliases cannot create another v3 endpoint, session lock, or broker.
        # Keep the durable inventory intact and let public admission queue.
        return {
            "changed": False,
            "previous_capacity": current_capacity,
            "effective_capacity": min(requested, current_capacity, physical_capacity),
            "logical_alias_inventory": current_capacity,
            "physical_basis_count": physical_capacity,
            "capacity_limit_reason": (
                "reviewed_broker_global_attempt_limit"
                if broker_pool_attempt_limit(pool) is not None
                else "mechanical_v3_one_active_child_per_control_session"
            ),
        }
    if target_capacity <= current_capacity and requested >= target_capacity:
        return {
            "changed": False,
            "previous_capacity": current_capacity,
            "effective_capacity": min(requested, current_capacity),
            "physical_basis_count": 1,
        }

    physical = _physical_session(sessions)
    physical_dir_text = physical.get("session_dir")
    if not isinstance(physical_dir_text, str) or not physical_dir_text:
        raise RuntimeError("physical fork session has no session directory")
    physical_dir = Path(physical_dir_text)
    if not physical_dir.is_absolute() or pool_dir.resolve() not in physical_dir.resolve().parents:
        raise RuntimeError(f"physical fork session is outside its pool: {physical_dir}")
    physical_json = physical_dir / "session.json"
    physical_state = read_json(physical_json)
    if not isinstance(physical_state, dict):
        raise RuntimeError(f"physical fork session metadata is not an object: {physical_json}")

    pool.setdefault("snapshot_size_requested", requested)
    pool["size_requested"] = max(target_capacity, current_capacity)
    pool["capacity_model"] = "one_basis_parent_with_logical_alias_seats"
    expanded_utc = utc_now()
    for worker_index in range(current_capacity + 1, target_capacity + 1):
        logical_dir = _logical_session_dir(pool_dir, worker_index)
        if logical_dir.is_symlink():
            raise RuntimeError(f"logical fork seat directory must not be a symlink: {logical_dir}")
        logical_dir.mkdir(parents=True, exist_ok=True)
        logical_state = deepcopy(physical_state)
        logical_state.update(
            {
                "session_id": logical_dir.name,
                "session_dir": str(logical_dir),
                "control_session_dir": str(physical_dir),
                "worker_index": worker_index,
                "status": "ready" if status == "ready" else "stopped",
                "lease": None,
                "created_utc": expanded_utc,
                "updated_utc": expanded_utc,
                "capacity_kind": "logical_control_alias",
                "socket": str(warm_socket_path(physical_dir)),
            }
        )
        atomic_write_json(logical_dir / "session.json", logical_state)
        write_warm_session_card(logical_dir / "session-card.txt", logical_state)

        logical = deepcopy(physical)
        logical.update(
            {
                "worker_index": worker_index,
                "status": "idle" if status == "ready" else "stopped",
                "session_dir": str(logical_dir),
                "session_id": logical_dir.name,
                "control_session_dir": str(physical_dir),
                "lease": None,
                "last_attempt": None,
                "created_utc": expanded_utc,
                "capacity_kind": "logical_control_alias",
            }
        )
        sessions.append(logical)

    pool["logical_capacity_expanded_utc"] = expanded_utc
    write_warm_pool(pool_dir, pool)
    return {
        "changed": True,
        "previous_capacity": current_capacity,
        "effective_capacity": len(sessions),
        "physical_basis_count": 1,
    }
