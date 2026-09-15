"""Durable ownership and scoped cancellation for one CRIU shelf route."""

from __future__ import annotations

import os
import secrets
import signal
import time
from pathlib import Path
from typing import Any

from hol_workbench.jsonio import atomic_write_json, read_json
from hol_workbench.process_groups import process_birth_identity
from hol_workbench.proof_run_fork_children import read_active_children
from hol_workbench.proof_run_runtime import utc_now

SHELF_OWNER_SCHEMA = "hol-workbench.criu-shelf-owner.v1"
SHELF_OWNER_FILENAME = "admission-owner.json"


def shelf_owner_path(profile_root: Path, slot: int = 1) -> Path:
    if slot <= 1:
        return profile_root / SHELF_OWNER_FILENAME
    return profile_root / f"admission-owner-{slot}.json"


def _shelf_owner_paths(profile_root: Path) -> list[Path]:
    legacy = shelf_owner_path(profile_root)
    return [legacy, *sorted(path for path in profile_root.glob("admission-owner-*.json") if path != legacy)]


def new_shelf_owner(
    profile_root: Path,
    *,
    logical_profile: str,
    owner_kind: str,
    source: Path,
    route_receipt: Path | None = None,
) -> dict[str, Any]:
    pid = os.getpid()
    return {
        "schema": SHELF_OWNER_SCHEMA,
        "attempt_id": f"route-{pid}-{secrets.token_hex(4)}",
        "logical_profile": logical_profile,
        "physical_profile": profile_root.name,
        "owner_kind": owner_kind,
        "source": str(source),
        "pid": pid,
        "process_identity": process_birth_identity(pid),
        "started_utc": utc_now(),
        "started_epoch": time.time(),
        "progress": "admitted",
        "route_receipt": str(route_receipt) if route_receipt else None,
    }


def write_shelf_owner(profile_root: Path, owner: dict[str, Any]) -> None:
    slot = int(owner.get("admission_slot") or 1)
    atomic_write_json(shelf_owner_path(profile_root, slot), owner)


def update_shelf_owner(profile_root: Path, attempt_id: str, **updates: Any) -> dict[str, Any]:
    owner = read_shelf_owner(profile_root, require_live=False, attempt_id=attempt_id)
    if not owner or owner.get("attempt_id") != attempt_id:
        return {}
    owner.update(updates)
    write_shelf_owner(profile_root, owner)
    return owner


def clear_shelf_owner(profile_root: Path, attempt_id: str) -> None:
    for path in _shelf_owner_paths(profile_root):
        owner = read_json(path)
        if owner.get("attempt_id") == attempt_id:
            path.unlink(missing_ok=True)


def read_shelf_owners(profile_root: Path, *, require_live: bool = True) -> list[dict[str, Any]]:
    owners: list[dict[str, Any]] = []
    for path in _shelf_owner_paths(profile_root):
        owner = read_json(path)
        if owner.get("schema") != SHELF_OWNER_SCHEMA:
            continue
        if require_live:
            expected = owner.get("process_identity")
            if not expected or process_birth_identity(owner.get("pid")) != expected:
                continue
        started = owner.get("started_epoch")
        owner["elapsed_seconds"] = round(max(0.0, time.time() - float(started)), 1) if started else None
        owners.append(owner)
    return owners


def read_shelf_owner(
    profile_root: Path,
    *,
    require_live: bool = True,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    owners = read_shelf_owners(profile_root, require_live=require_live)
    if attempt_id is not None:
        return next((owner for owner in owners if owner.get("attempt_id") == attempt_id), {})
    return owners[0] if owners else {}


def shelf_owner_progress(profile_root: Path, owner: dict[str, Any]) -> dict[str, Any]:
    """Return one bounded progress signal, preferring the active disposable child."""
    for pool in sorted((profile_root / "pool").glob("*")):
        pool_data = read_json(pool / "pool.json")
        busy_sessions = [
            item for item in pool_data.get("sessions") or [] if isinstance(item, dict) and item.get("status") == "busy"
        ]
        owner_pid = owner.get("pid")
        busy = next(
            (
                item
                for item in busy_sessions
                if isinstance(item.get("lease"), dict) and item["lease"].get("owner_pid") == owner_pid
            ),
            busy_sessions[0] if len(busy_sessions) == 1 else {},
        )
        seat = busy.get("worker_index")
        if seat is None:
            seat = Path(str(busy.get("session_dir") or "")).name or None
        try:
            children = read_active_children(pool)
        except (OSError, RuntimeError, ValueError):
            continue
        if children and len(busy_sessions) == 1:
            child = children[-1]
            return {
                "kind": "active-disposable-child",
                "attempt_id": child.get("attempt_id"),
                "registered_utc": child.get("registered_utc"),
                "pid": child.get("pid"),
                "pool": str(pool),
                "seat": seat,
            }
        if busy:
            return {
                "kind": "active-seat",
                "stage": owner.get("progress") or "evaluating",
                "pool": str(pool),
                "seat": seat,
                "active_child_count": len(children),
            }
    return {
        "kind": "route-stage",
        "stage": owner.get("progress") or "admitted",
        "pool": owner.get("pool"),
    }


def progress_summary(progress: dict[str, Any]) -> str:
    if progress.get("kind") == "active-disposable-child":
        seat = progress.get("seat")
        seat_text = "-" if seat is None else str(seat)
        return f"active-child:{progress.get('attempt_id') or '-'} seat={seat_text}"
    if progress.get("kind") == "active-seat":
        seat = progress.get("seat")
        seat_text = "-" if seat is None else str(seat)
        return f"active-seat:{seat_text} stage={progress.get('stage') or '-'}"
    return f"stage:{progress.get('stage') or '-'}"


def owner_cancel_command(owner: dict[str, Any]) -> str:
    return f"hol-workbench/bin/orbstack-criu cancel {owner.get('logical_profile')} --attempt {owner.get('attempt_id')}"


def _owner_for_attempt(profile_root: Path, attempt_id: str) -> dict[str, Any]:
    primary = read_shelf_owner(profile_root)
    if primary.get("attempt_id") == attempt_id:
        return primary
    return next(
        (owner for owner in read_shelf_owners(profile_root) if owner.get("attempt_id") == attempt_id),
        {},
    )


def cancel_shelf_owner(profile_root: Path, attempt_id: str, *, wait_seconds: float = 10.0) -> dict[str, Any]:
    owner = _owner_for_attempt(profile_root, attempt_id)
    if not owner:
        primary = read_shelf_owner(profile_root)
        active_owners = [primary] if primary else []
        active_owners.extend(read_shelf_owners(profile_root))
        active = list(dict.fromkeys(item.get("attempt_id") for item in active_owners if item.get("attempt_id")))
        if not active:
            return {"status": "not-running", "attempt_id": attempt_id}
        result = {
            "status": "attempt-mismatch",
            "attempt_id": attempt_id,
            "active_attempt_id": active[0],
        }
        if len(active) > 1:
            result["active_attempt_ids"] = active
        return result
    try:
        os.kill(int(owner["pid"]), signal.SIGINT)
    except (OSError, TypeError, ValueError) as exc:
        return {
            "status": "signal-failed",
            "attempt_id": attempt_id,
            "pid": owner.get("pid"),
            "error": f"{type(exc).__name__}: {exc}",
        }
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        current = _owner_for_attempt(profile_root, attempt_id)
        if not current:
            receipt = read_json(Path(str(owner.get("route_receipt")))) if owner.get("route_receipt") else {}
            cancellation = receipt.get("cancellation") if isinstance(receipt, dict) else None
            return {
                "status": "interrupted",
                "attempt_id": attempt_id,
                "route_status": receipt.get("status") if receipt else None,
                "verified_quiescent": cancellation.get("verified_quiescent")
                if isinstance(cancellation, dict)
                else None,
                "seat_reusable": cancellation.get("seat_reusable") if isinstance(cancellation, dict) else None,
                "route_receipt": owner.get("route_receipt"),
            }
        time.sleep(0.05)
    return {"status": "cleanup-timeout", "attempt_id": attempt_id, "pid": owner.get("pid")}
