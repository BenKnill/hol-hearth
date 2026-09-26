"""Locked warm-pool seat transitions without command or card dependencies."""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from hol_workbench.ids import utc_now
from hol_workbench.pools.leases import (
    WarmPoolCheckoutError,
    checkout_warm_pool_seat,
    lease_owner_metadata,
    quarantine_fork_basis_pool,
    release_warm_pool_seat,
)
from hol_workbench.pools.session_lifecycle import pool_session_alive
from hol_workbench.pools.store import write_warm_pool_json


class WarmPoolLeaseIdCollision(RuntimeError):
    """A proposed lease ID already belongs to an existing pool owner."""


def _persist(pool_dir: Path, pool: dict[str, Any]) -> None:
    write_warm_pool_json(pool_dir, pool, updated_utc=utc_now())


def _short_sha(value: object) -> str:
    return str(value)[:12] if value else "unknown"


def checkout_pool_seat_locked(
    pool_dir: Path,
    pool: dict[str, Any],
    *,
    agent: str | None,
    lease_id: str | None = None,
    allow_dirty: bool = False,
    owner_kind: str = "manual",
    owner_command: str | None = None,
) -> dict[str, Any]:
    """Checkout one live seat; the caller must hold the pool metadata lock."""

    if lease_id is not None and not lease_id:
        raise ValueError("warm pool lease id must be nonempty")
    selected_lease_id = secrets.token_hex(8) if lease_id is None else lease_id
    if any(
        isinstance(item, dict)
        and isinstance(item.get("lease"), dict)
        and item["lease"].get("lease_id") == selected_lease_id
        for item in pool.get("sessions") or []
    ):
        raise WarmPoolLeaseIdCollision(f"warm pool lease id is already active: {selected_lease_id}")

    def lease_factory() -> dict[str, Any]:
        return lease_owner_metadata(
            lease_id=selected_lease_id,
            agent=agent,
            leased_utc=utc_now(),
            owner_kind=owner_kind,
            owner_pid=os.getpid(),
            owner_context_run=os.environ.get("HOL_WORKBENCH_CONTEXT_RUN_DIR"),
            owner_command=owner_command,
        )

    try:
        item = checkout_warm_pool_seat(
            pool,
            session_alive=pool_session_alive,
            lease_factory=lease_factory,
            allow_dirty=allow_dirty,
            released_utc=utc_now(),
        )
    except WarmPoolCheckoutError as exc:
        _persist(pool_dir, pool)
        if exc.required_preloads:
            required = ", ".join(
                f"{Path(str(preload.get('path') or '')).name}:{_short_sha(preload.get('sha256'))}"
                for preload in exc.required_preloads
            )
            raise RuntimeError(f"no basis-ready warm pool worker available; required preloads: {required}") from exc
        raise RuntimeError(str(exc)) from exc
    _persist(pool_dir, pool)
    return item


def release_pool_seat_locked(
    pool_dir: Path,
    pool: dict[str, Any],
    *,
    session: Path,
    status: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Release one seat; the caller must hold the pool metadata lock."""

    item = release_warm_pool_seat(
        pool,
        session=session,
        status=status,
        reason=reason,
        released_utc=utc_now(),
    )
    _persist(pool_dir, pool)
    return item


def quarantine_fork_basis_locked(
    pool_dir: Path,
    pool: dict[str, Any],
    *,
    session: Path,
    reason: str,
) -> dict[str, Any]:
    """Quarantine a fork basis; the caller must hold the pool metadata lock."""

    item = quarantine_fork_basis_pool(
        pool,
        session=session,
        reason=reason,
        quarantined_utc=utc_now(),
    )
    _persist(pool_dir, pool)
    return item


def quarantine_fork_basis_lease_locked(
    pool_dir: Path,
    pool: dict[str, Any],
    *,
    lease_id: str,
    reason: str,
) -> dict[str, Any]:
    """Quarantine the generation and clear every row with one corrupt lease ID."""

    if pool.get("engine") != "fork_basis":
        raise RuntimeError("pool-generation quarantine requires a fork-basis pool")
    quarantined_utc = utc_now()
    pool["status"] = "poisoned"
    pool["poison_reason"] = reason
    pool["quarantined_utc"] = quarantined_utc
    released: dict[str, Any] | None = None
    for item in pool.get("sessions") or []:
        if not isinstance(item, dict):
            continue
        lease = item.get("lease")
        is_released = isinstance(lease, dict) and lease.get("lease_id") == lease_id
        item["pool_generation_quarantined"] = True
        item["pool_generation_poison_reason"] = reason
        active_sibling = item.get("status") == "busy" and bool(lease) and not is_released
        if active_sibling:
            item["release_after_lease_status"] = "poisoned"
            continue
        item["status"] = "poisoned"
        item["lease"] = None
        item["released_utc"] = quarantined_utc
        item["release_reason"] = reason
        if is_released:
            released = item
    if released is None:
        raise RuntimeError(f"lease is not part of this warm pool: {lease_id}")
    _persist(pool_dir, pool)
    return released


def finalize_fork_pool_seat_locked(
    pool_dir: Path,
    pool: dict[str, Any],
    *,
    session: Path,
    reusable: bool,
    reason: str,
) -> dict[str, Any]:
    """Release a proven-quiescent seat or quarantine its shared generation."""

    if reusable:
        return release_pool_seat_locked(
            pool_dir,
            pool,
            session=session,
            status="idle",
            reason=reason,
        )
    return quarantine_fork_basis_locked(
        pool_dir,
        pool,
        session=session,
        reason=reason,
    )
