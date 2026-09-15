"""Warm-pool basis/preload readiness policy."""

from __future__ import annotations

from typing import Any


def required_pool_preloads(pool: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in pool.get("preloads") or [] if isinstance(item, dict) and item.get("status") == "loaded"]


def warm_pool_missing_preloads(pool: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    required = required_pool_preloads(pool)
    loaded = item.get("loaded_preloads") or []
    missing = []
    for preload in required:
        if not any(
            isinstance(entry, dict)
            and entry.get("path") == preload.get("path")
            and entry.get("sha256") == preload.get("sha256")
            and entry.get("status") == "loaded"
            for entry in loaded
        ):
            missing.append({"path": preload.get("path"), "sha256": preload.get("sha256")})
    return missing


def annotate_warm_pool_basis_state(pool: dict[str, Any]) -> bool:
    changed = False
    for item in pool.get("sessions") or []:
        if not isinstance(item, dict):
            continue
        missing = warm_pool_missing_preloads(pool, item)
        ready = not missing
        if item.get("basis_ready") != ready:
            item["basis_ready"] = ready
            changed = True
        if item.get("basis_missing_preloads") != missing:
            item["basis_missing_preloads"] = missing
            changed = True
        if missing and item.get("status") in {"idle", "dirty"}:
            item["status"] = "basis_missing"
            item["lease"] = None
            changed = True
    return changed
