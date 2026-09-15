"""Accurate guest-side process and memory inventory for CRIU profile runtimes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hol_workbench.criu_pool_process_inventory import pool_process_inventory
from hol_workbench.jsonio import read_json
from hol_workbench.proof_run_fork_children import read_active_children

MEMORY_FIELDS = {
    "Rss": "rss_kb",
    "Pss": "pss_kb",
    "Private_Clean": "private_clean_kb",
    "Private_Dirty": "private_dirty_kb",
    "Private_Hugetlb": "private_hugetlb_kb",
    "Swap": "swap_kb",
    "SwapPss": "swap_pss_kb",
}


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def process_memory(pid: int, *, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    """Read proportional and private memory from Linux ``smaps_rollup``."""

    path = proc_root / str(pid) / "smaps_rollup"
    values = dict.fromkeys(MEMORY_FIELDS.values(), 0)
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"pid": pid, "available": False, "reason": f"{type(exc).__name__}: {exc}"}
    for line in lines:
        name, separator, raw = line.partition(":")
        field = MEMORY_FIELDS.get(name)
        if not separator or field is None:
            continue
        token = raw.strip().split()[0] if raw.strip() else "0"
        try:
            values[field] = int(token)
        except ValueError:
            continue
    values["uss_kb"] = values["private_clean_kb"] + values["private_dirty_kb"] + values["private_hugetlb_kb"]
    return {"pid": pid, "available": True, **values}


def _process_role(record: dict[str, Any], pool: dict[str, Any], child_pids: set[int]) -> str:
    pid = _positive_int(record.get("pid"))
    if pid in child_pids:
        return "evaluation_child"
    if pid == _positive_int(pool.get("basis_pid")):
        return "basis_parent"
    if pid == _positive_int(pool.get("worker_pid")):
        return "fork_manager"
    return "runtime_descendant"


def pool_runtime_inventory(pool_dir: Path) -> dict[str, Any]:
    """Describe one pool without mistaking logical seats for HOL processes."""

    try:
        pool = read_json(pool_dir / "pool.json")
    except (OSError, TypeError, ValueError) as exc:
        return {
            "pool": str(pool_dir),
            "available": False,
            "reason": f"cannot read pool metadata: {type(exc).__name__}: {exc}",
        }
    sessions = [item for item in pool.get("sessions") or [] if isinstance(item, dict)]
    try:
        active_children = read_active_children(pool_dir)
        child_error = None
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        active_children = []
        child_error = f"{type(exc).__name__}: {exc}"
    child_pids = {pid for item in active_children if (pid := _positive_int(item.get("pid"))) is not None}
    processes = pool_process_inventory(pool_dir)
    rows: list[dict[str, Any]] = []
    totals = {"rss_kb": 0, "pss_kb": 0, "uss_kb": 0, "swap_kb": 0, "swap_pss_kb": 0}
    memory_errors: list[str] = []
    for process in processes.get("survivors") or []:
        pid = _positive_int(process.get("pid"))
        if pid is None:
            continue
        memory = process_memory(pid)
        if not memory.get("available"):
            memory_errors.append(f"pid {pid}: {memory.get('reason')}")
        for field in totals:
            totals[field] += int(memory.get(field) or 0)
        rows.append({**process, "role": _process_role(process, pool, child_pids), "memory": memory})
    leased = sum(1 for item in sessions if item.get("lease") or item.get("status") == "busy")
    errors = list(processes.get("errors") or [])
    errors.extend(memory_errors)
    if child_error:
        errors.append(f"active child inventory unavailable: {child_error}")
    return {
        "pool": str(pool_dir),
        "available": not errors,
        "errors": errors,
        "engine": pool.get("engine"),
        "runtime_status": pool.get("status"),
        "logical_capacity": len(sessions),
        "occupied_seats": leased,
        "idle_seats": max(0, len(sessions) - leased),
        "physical_basis_parents": 1
        if _positive_int(pool.get("basis_pid")) in (processes.get("survivor_pids") or [])
        else 0,
        "active_children": active_children,
        "active_child_count": len(active_children),
        "process_count": len(rows),
        "processes": rows,
        "memory": {
            **totals,
            "metric": "linux_smaps_rollup",
            "meaning": "PSS apportions shared pages; USS is private memory; RSS double-counts shared pages",
            "complete": not memory_errors,
        },
    }


def profile_runtime_inventory(profile_root: Path) -> dict[str, Any]:
    pools = sorted((profile_root / "pool").glob("*"))
    if len(pools) != 1:
        return {
            "available": False,
            "reason": f"expected one pool under {profile_root / 'pool'}, found {len(pools)}",
        }
    return pool_runtime_inventory(pools[0])


def guest_memory_summary(*, proc_root: Path = Path("/proc")) -> dict[str, Any]:
    fields: dict[str, int] = {}
    try:
        lines = (proc_root / "meminfo").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
    for line in lines:
        name, separator, raw = line.partition(":")
        if not separator:
            continue
        token = raw.strip().split()[0] if raw.strip() else "0"
        try:
            fields[name] = int(token)
        except ValueError:
            continue
    wanted = {
        "mem_total_kb": fields.get("MemTotal"),
        "mem_available_kb": fields.get("MemAvailable"),
        "cached_kb": fields.get("Cached"),
        "buffers_kb": fields.get("Buffers"),
        "swap_total_kb": fields.get("SwapTotal"),
        "swap_free_kb": fields.get("SwapFree"),
    }
    return {
        "available": all(value is not None for value in wanted.values()),
        **wanted,
        "metric": "linux_proc_meminfo",
        "meaning": "OrbStack guest-wide memory; not attributable to Workbench alone",
    }


def summarize_profile_memory(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum unique physical shelves, avoiding logical-profile alias double counting."""

    seen: set[str] = set()
    totals = {"rss_kb": 0, "pss_kb": 0, "uss_kb": 0, "swap_kb": 0, "swap_pss_kb": 0}
    live_profiles = 0
    complete = True
    for row in rows:
        inventory = row.get("runtime_inventory") or {}
        pool = str(inventory.get("pool") or "")
        if not pool or pool in seen:
            continue
        seen.add(pool)
        memory = inventory.get("memory") or {}
        if inventory.get("process_count"):
            live_profiles += 1
        complete = complete and bool(memory.get("complete", False))
        for field in totals:
            totals[field] += int(memory.get(field) or 0)
    return {
        **totals,
        "live_physical_profiles": live_profiles,
        "unique_physical_shelves": len(seen),
        "complete": complete,
        "metric": "sum_unique_profile_linux_smaps_rollup",
    }
