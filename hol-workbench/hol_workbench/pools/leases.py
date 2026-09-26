"""Warm-pool seat checkout and release policy."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hol_workbench.criu_shelf_capacity import (
    broker_pool_attempt_limit,
    physical_broker_capacity,
    physical_control_session,
)
from hol_workbench.pools.basis import annotate_warm_pool_basis_state, required_pool_preloads, warm_pool_missing_preloads
from hol_workbench.processes import process_is_alive


class WarmPoolCheckoutError(RuntimeError):
    def __init__(self, message: str, *, required_preloads: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.required_preloads = required_preloads or []


def lease_owner_metadata(
    *,
    agent: str | None,
    lease_id: str,
    leased_utc: str,
    owner_kind: str,
    owner_pid: int | None = None,
    owner_context_run: str | None = None,
    owner_command: str | None = None,
) -> dict[str, Any]:
    lease = {
        "lease_id": lease_id,
        "agent": agent,
        "leased_utc": leased_utc,
        "owner_kind": owner_kind,
    }
    if owner_pid is not None:
        lease["owner_pid"] = owner_pid
    if owner_context_run:
        lease["owner_context_run"] = owner_context_run
    if owner_command:
        lease["owner_command"] = owner_command
    return lease


def stale_pool_leases(
    pool: dict[str, Any], *, owner_alive: Callable[[Any], bool] = process_is_alive,
) -> list[dict[str, Any]]:
    """Describe busy seats whose lease owner process no longer exists; never mutates."""
    stale: list[dict[str, Any]] = []
    for item in pool.get("sessions") or []:
        if not isinstance(item, dict) or item.get("status") != "busy":
            continue
        lease = item.get("lease")
        if not isinstance(lease, dict):
            continue
        owner_pid = lease.get("owner_pid")
        if type(owner_pid) is not int or owner_alive(owner_pid):
            continue
        stale.append({
            "session_dir": item.get("session_dir"), "owner_pid": owner_pid, "lease_id": lease.get("lease_id"),
            "leased_utc": lease.get("leased_utc"), "owner_command": lease.get("owner_command"),
        })
    return stale


def reclaim_stale_leases(
    pool: dict[str, Any], *, released_utc: str, owner_alive: Callable[[Any], bool] = process_is_alive,
) -> list[dict[str, Any]]:
    """Return busy seats whose owner process is gone to idle.

    A client that was killed outright (a shell `kill`, an OOM kill, a lost
    terminal) never reaches its release path, and its seat then blocks every
    later checkout with "no basis-ready warm pool worker". A lease is reclaimed
    only when its recorded owner pid does not exist at all; an unknown or live
    owner keeps its seat, so pid reuse cannot free a seat that is in use.
    """
    reclaimed = stale_pool_leases(pool, owner_alive=owner_alive)
    stale_dirs = {row["session_dir"] for row in reclaimed}
    for item in pool.get("sessions") or []:
        if not isinstance(item, dict) or item.get("session_dir") not in stale_dirs or item.get("status") != "busy":
            continue
        lease = item.get("lease") or {}
        item["status"] = "idle"
        item["lease"] = None
        item["released_utc"] = released_utc
        item["release_reason"] = (
            f"stale lease reclaimed: owner pid {lease.get('owner_pid')} is not running "
            f"(leased {lease.get('leased_utc')}, {lease.get('owner_command') or 'unknown command'})"
        )
    if reclaimed:
        history = [row for row in pool.get("reclaimed_leases") or [] if isinstance(row, dict)]
        history.extend({**row, "reclaimed_utc": released_utc} for row in reclaimed)
        pool["reclaimed_leases"] = history[-20:]
    return reclaimed


def checkout_warm_pool_seat(
    pool: dict[str, Any],
    *,
    session_alive: Callable[[dict[str, Any]], bool],
    lease_factory: Callable[[], dict[str, Any]],
    allow_dirty: bool = False,
    owner_alive: Callable[[Any], bool] = process_is_alive,
    released_utc: str | None = None,
) -> dict[str, Any]:
    pool_status = str(pool.get("status") or "unknown")
    if pool_status != "ready":
        raise WarmPoolCheckoutError(f"warm pool lifecycle status {pool_status} is not ready for checkout")
    reclaim_stale_leases(pool, released_utc=released_utc or "", owner_alive=owner_alive)
    annotate_warm_pool_basis_state(pool)
    attempt_limit = broker_pool_attempt_limit(pool)
    active = [
        item for item in pool.get("sessions") or []
        if isinstance(item, dict) and (item.get("status") == "busy" or item.get("lease"))
    ]
    if attempt_limit is not None and len(active) >= attempt_limit:
        raise WarmPoolCheckoutError("no idle warm pool worker available: broker attempt capacity is occupied")
    physical_capacity = physical_broker_capacity(pool)
    occupied_controls = {
        physical_control_session(item)
        for item in pool.get("sessions") or []
        if isinstance(item, dict) and (item.get("status") == "busy" or item.get("lease"))
    } if physical_capacity is not None else set()
    preferred_statuses = ["idle"] + (["dirty"] if allow_dirty else [])
    for wanted_status in preferred_statuses:
        for item in pool.get("sessions") or []:
            if not isinstance(item, dict) or item.get("status") != wanted_status:
                continue
            if physical_capacity is not None and physical_control_session(item) in occupied_controls:
                continue
            missing = warm_pool_missing_preloads(pool, item)
            if missing:
                item["status"] = "basis_missing"
                item["basis_ready"] = False
                item["basis_missing_preloads"] = missing
                item["lease"] = None
                continue
            if not session_alive(item):
                item["status"] = "dead"
                item["lease"] = None
                continue
            item["status"] = "busy"
            item["lease"] = lease_factory()
            return item
    required = required_pool_preloads(pool)
    if required:
        raise WarmPoolCheckoutError("no basis-ready warm pool worker available", required_preloads=required)
    raise WarmPoolCheckoutError("no idle warm pool worker available")


def release_warm_pool_seat(
    pool: dict[str, Any],
    *,
    session: Path,
    status: str,
    released_utc: str,
    reason: str | None = None,
) -> dict[str, Any]:
    session_text = str(session.resolve())
    for item in pool.get("sessions") or []:
        if not isinstance(item, dict):
            continue
        if str(Path(item.get("session_dir", "")).resolve()) != session_text:
            continue
        pool_status = str(pool.get("status") or "unknown")
        if pool_status != "ready":
            if pool.get("engine") == "fork_basis" and pool_status == "poisoned":
                item["status"] = "poisoned"
                item["lease"] = None
                item["released_utc"] = released_utc
                item["release_reason"] = reason or str(
                    pool.get("poison_reason") or "fork-basis pool generation is quarantined"
                )
                return item
            item["release_reason"] = (
                f"eval release deferred because pool lifecycle status {pool_status} owns seat state"
            )
            return item
        if pool.get("engine") == "fork_basis" and status == "dirty":
            status = "idle"
            reason = "fork basis child exited; basis remains clean"
        item["status"] = status
        item["lease"] = None
        item["released_utc"] = released_utc
        if reason:
            item["release_reason"] = reason
        return item
    raise RuntimeError(f"session is not part of this warm pool: {session}")


def quarantine_fork_basis_pool(
    pool: dict[str, Any],
    *,
    session: Path,
    reason: str,
    quarantined_utc: str,
) -> dict[str, Any]:
    """Quarantine one shared fork manager while preserving active sibling ownership."""

    if pool.get("engine") != "fork_basis":
        raise RuntimeError("pool-generation quarantine requires a fork-basis pool")
    session_text = str(session.resolve())
    released: dict[str, Any] | None = None
    pool["status"] = "poisoned"
    pool["poison_reason"] = reason
    pool["quarantined_utc"] = quarantined_utc
    for item in pool.get("sessions") or []:
        if not isinstance(item, dict):
            continue
        item["pool_generation_quarantined"] = True
        item["pool_generation_poison_reason"] = reason
        is_released = str(Path(item.get("session_dir", "")).resolve()) == session_text
        active_sibling = item.get("status") == "busy" and bool(item.get("lease")) and not is_released
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
        raise RuntimeError(f"session is not part of this warm pool: {session}")
    return released
