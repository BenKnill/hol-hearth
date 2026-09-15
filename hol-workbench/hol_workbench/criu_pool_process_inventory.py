"""Fail-closed survivor inventory for one stopped CRIU warm pool."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hol_workbench.pools.reset import parse_command_tokens, resolved_absolute_path
from hol_workbench.process_groups import process_start_ticks
from hol_workbench.processes import descendant_pids
from hol_workbench.proof_run_fork_children import read_active_children

PID_KEYS = ("worker_pid", "basis_pid", "hol_pid")
PGID_KEYS = ("worker_pgid", "basis_pgid", "hol_pgid")


def load_process_table() -> dict[int, dict[str, Any]]:
    """Take one bounded process snapshot for fail-closed pool cleanup."""

    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,pgid=,rss=,etime=,lstart=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    table: dict[int, dict[str, Any]] = {}
    for line in proc.stdout.splitlines():
        pieces = line.strip().split(None, 10)
        if len(pieces) < 10:
            continue
        try:
            pid = int(pieces[0])
            ppid = int(pieces[1])
            pgid = int(pieces[2])
            rss = int(pieces[3])
        except ValueError:
            continue
        table[pid] = {
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            "rss_kb": rss,
            "etime": pieces[4] if len(pieces) > 4 else None,
            "process_start_ticks": process_start_ticks(pid),
            # Bind cleanup selection to the same process-table snapshot. The
            # /proc start tick is useful detail, but cannot replace this ID.
            "process_identity": f"ps-lstart:{' '.join(pieces[5:10])}",
            "command": pieces[10] if len(pieces) > 10 else "",
        }
    return table


def command_pool_dir(command: str) -> Path | None:
    tokens = parse_command_tokens(command)
    for index, token in enumerate(tokens):
        if token == "--pool-dir" and index + 1 < len(tokens):
            return resolved_absolute_path(tokens[index + 1])
        if token.startswith("--pool-dir="):
            return resolved_absolute_path(token.split("=", 1)[1])
    return None


def live_pool_role_pids(
    pool: Path,
    *,
    process_table: Mapping[int, dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Resolve exactly one manager and basis process for a live build pool."""

    pool = pool.expanduser().resolve()
    table = dict(process_table) if process_table is not None else load_process_table()
    try:
        metadata = json.loads((pool / "pool.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        metadata = {}
    records = [metadata] if isinstance(metadata, dict) else []
    records.extend(item for item in (metadata.get("sessions") or []) if isinstance(item, dict))
    manager_candidates: set[int] = set()
    basis_candidates: set[int] = set()
    for record in records:
        manager = _positive_int(record.get("worker_pid"))
        basis = _positive_int(record.get("basis_pid") or record.get("hol_pid"))
        if manager in table:
            manager_candidates.add(manager)
        if basis in table:
            basis_candidates.add(basis)
    for pid, info in table.items():
        command = str(info.get("command") or "")
        if ("_fork-basis-worker" in command or "hol_workbench.proof_run_fork_broker" in command) and command_pool_dir(
            command
        ) == pool:
            manager_candidates.add(pid)
    for pid in descendant_pids(manager_candidates, table):
        command = str(table.get(pid, {}).get("command") or "")
        if "ocaml-hol" in command or "ocamlrun" in command:
            basis_candidates.add(pid)
    return {
        "manager": next(iter(manager_candidates)) if len(manager_candidates) == 1 else 0,
        "ocaml": next(iter(basis_candidates)) if len(basis_candidates) == 1 else 0,
    }


def _positive_int(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _proc_binding_sources(pid: int, *, pool: Path, proc_root: Path) -> set[str]:
    sources: set[str] = set()
    process_root = proc_root / str(pid)
    try:
        cwd = Path(os.readlink(process_root / "cwd"))
    except OSError:
        cwd = None
    if cwd is not None and cwd.is_absolute() and _within(cwd, pool):
        sources.add("proc_cwd")
    try:
        descriptors = list((process_root / "fd").iterdir())
    except OSError:
        descriptors = []
    for descriptor in descriptors:
        try:
            target = Path(os.readlink(descriptor))
        except OSError:
            continue
        if target.is_absolute() and _within(target, pool):
            sources.add("proc_fd")
            break
    return sources


def pool_process_inventory(
    pool: Path,
    *,
    process_table: Mapping[int, dict[str, Any]] | None = None,
    proc_root: Path = Path("/proc"),
) -> dict[str, Any]:
    """Return every live process still bound to a pool by durable or live identity."""

    pool = pool.expanduser().resolve()
    table = dict(process_table) if process_table is not None else load_process_table()
    errors: list[str] = []
    if not table:
        errors.append("process table is unavailable or empty")
    if not proc_root.is_dir():
        errors.append(f"process filesystem is unavailable: {proc_root}")
    try:
        metadata = json.loads((pool / "pool.json").read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise TypeError("pool metadata is not a JSON object")
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        metadata = {}
        errors.append(f"cannot read pool process metadata: {exc}")

    sources: dict[int, set[str]] = {}
    recorded_pgids: set[int] = set()

    def add_pid(value: object, source: str) -> None:
        pid = _positive_int(value)
        if pid is not None and pid in table:
            sources.setdefault(pid, set()).add(source)

    def add_record(record: dict[str, Any], source: str) -> None:
        for key in PID_KEYS:
            add_pid(record.get(key), f"{source}.{key}")
        for key in PGID_KEYS:
            pgid = _positive_int(record.get(key))
            if pgid is not None:
                recorded_pgids.add(pgid)

    add_record(metadata, "pool")
    for index, session in enumerate(metadata.get("sessions") or []):
        if isinstance(session, dict):
            add_record(session, f"session[{index}]")
    try:
        active_children = read_active_children(pool)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        active_children = []
        errors.append(f"cannot read fork active-child registry: {exc}")
    for index, child in enumerate(active_children):
        add_pid(child.get("pid"), f"active_child[{index}].pid")
        pgid = _positive_int(child.get("pgid"))
        if pgid is not None:
            recorded_pgids.add(pgid)

    for pid, info in table.items():
        command = str(info.get("command") or "")
        if command_pool_dir(command) == pool:
            sources.setdefault(pid, set()).add("exact_pool_argument")
        pgid = _positive_int(info.get("pgid"))
        if pgid is not None and pgid in recorded_pgids:
            sources.setdefault(pid, set()).add("recorded_process_group")
        for source in _proc_binding_sources(pid, pool=pool, proc_root=proc_root):
            sources.setdefault(pid, set()).add(source)

    seeds = set(sources)
    for pid in descendant_pids(seeds, table):
        sources.setdefault(pid, set()).add("descendant")
    survivors = [
        {
            "pid": pid,
            "ppid": _positive_int(table[pid].get("ppid")),
            "pgid": _positive_int(table[pid].get("pgid")),
            "sources": sorted(sources[pid]),
            "command": str(table[pid].get("command") or "")[-500:],
        }
        for pid in sorted(sources)
        if pid in table
    ]
    return {
        "pool": str(pool),
        "process_count": len(table),
        "proc_bindings_checked": proc_root.is_dir(),
        "errors": errors,
        "survivor_pids": [item["pid"] for item in survivors],
        "survivors": survivors,
        "zero_survivors": not errors and not survivors,
    }
