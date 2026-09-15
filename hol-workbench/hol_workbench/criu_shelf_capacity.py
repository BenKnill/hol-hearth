"""Configured and restored capacity for one physical CRIU profile shelf."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hol_workbench.jsonio import read_json
from hol_workbench.restored_execution_topology import RestoredExecutionTopology


def physical_control_session(session: dict[str, Any]) -> str:
    control = session.get("control_session_dir") or session.get("session_dir")
    if not isinstance(control, str) or not Path(control).is_absolute():
        raise RuntimeError("mechanical broker session has no absolute control identity")
    return str(Path(control).resolve())


def broker_pool_attempt_limit(pool: dict) -> int | None:
    """Return the reviewed broker-wide limit, not the number of sockets.

    A mechanical fork pool is served by one broker and one loaded basis.
    Serial v3 brokers own a global active-attempt guard even when published
    with several distinct control sessions. This is a capacity restriction
    only; ordinary snapshot admission still validates the frozen runtime.
    """
    if pool.get("execution_topology") != RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3.value:
        return None
    # Keep the reviewed runtime/capacity relation in one authority. Import
    # lazily because snapshot admission also consumes pool policy modules.
    from hol_workbench.criu_snapshot_compat import REVIEWED_BROKER_VARIANTS

    variant = REVIEWED_BROKER_VARIANTS.get(str(pool.get("broker_runtime_sha256") or ""))
    if variant is None:
        return None  # Unknown runtimes cannot pass snapshot admission.
    limit = variant.get("maximum_concurrent_attempts")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
        raise RuntimeError("reviewed broker has an invalid attempt capacity limit")
    return limit


def physical_broker_capacity(pool: dict) -> int | None:
    """Bound capacity by both session locks and any broker-wide guard."""
    if pool.get("execution_topology") != RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3.value:
        return None
    sessions = [item for item in pool.get("sessions") or [] if isinstance(item, dict)]
    controls: set[str] = set()
    for session in sessions:
        controls.add(physical_control_session(session))
    limit = broker_pool_attempt_limit(pool)
    return len(controls) if limit is None else min(len(controls), limit)


def configured_shelf_capacity(assignments: dict[str, str]) -> int:
    """Return the maintainer-configured public capacity, defaulting conservatively."""
    value = assignments.get("PROFILE_ORBSTACK_PUBLIC_CAPACITY") or "1"
    try:
        capacity = int(value)
    except ValueError as exc:
        raise ValueError(f"invalid OrbStack public capacity {value!r}") from exc
    if capacity <= 0:
        raise ValueError(f"invalid OrbStack public capacity {value!r}")
    return capacity


def restored_shelf_capacity(profile_root: Path) -> int:
    """Read the capacity that the published snapshot can actually restore.

    Capacity is fail-closed: both the requested pool size and the durable
    session inventory must agree that a slot exists. Old one-seat shelves
    remain valid with capacity one.
    """
    pools = sorted((profile_root / "pool").glob("*"))
    if not pools:
        # Restore remains authoritative for a missing/corrupt shelf. Capacity
        # discovery is conservative so diagnostics and test doubles can reach
        # that existing failure boundary.
        return 1
    if len(pools) != 1:
        raise RuntimeError(f"expected one pool under {profile_root / 'pool'}, found {len(pools)}")
    data = read_json(pools[0] / "pool.json")
    sessions = [item for item in data.get("sessions") or [] if isinstance(item, dict)]
    requested = data.get("size_requested")
    if requested is None:
        requested_count = len(sessions)
    else:
        try:
            requested_count = int(requested)
        except (TypeError, ValueError):
            requested_count = len(sessions)
    capacity = min(requested_count, len(sessions))
    physical_capacity = physical_broker_capacity(data)
    if physical_capacity is not None:
        capacity = min(capacity, physical_capacity)
    if capacity <= 0:
        raise RuntimeError(f"CRIU shelf has no durable sessions: {profile_root}")
    return capacity
