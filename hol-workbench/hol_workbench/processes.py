"""Small process helpers shared by pool and proof orchestration code."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

TERMINAL_PROCESS_STATES = {"X", "x", "Z"}


def pid_int(value: Any) -> int | None:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def process_is_alive(pid: Any) -> bool:
    pid_value = pid_int(pid)
    if pid_value is None:
        return False
    try:
        os.kill(pid_value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_table_pid_alive(pid: Any, process_table: dict[int, dict[str, Any]]) -> bool:
    pid_value = pid_int(pid)
    if pid_value is None:
        return False
    return pid_value in process_table or process_is_alive(pid_value)


def descendant_pids(seed_pids: set[int], process_table: dict[int, dict[str, Any]]) -> set[int]:
    """Return live descendants of known worker pids."""
    if not seed_pids:
        return set()
    children_by_parent: dict[int, list[int]] = {}
    for pid, info in process_table.items():
        raw_ppid = info.get("ppid")
        if raw_ppid is None:
            continue
        try:
            ppid = int(raw_ppid)
        except (TypeError, ValueError):
            continue
        children_by_parent.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = list(seed_pids)
    while stack:
        parent = stack.pop()
        for child in children_by_parent.get(parent, []):
            if child in seed_pids or child in found:
                continue
            found.add(child)
            stack.append(child)
    return found


def _positive_unique(values: Iterable[Any]) -> list[int]:
    return sorted({value for item in values if (value := pid_int(item)) is not None})


def _parse_linux_process_stat(raw: str) -> dict[str, int | str] | None:
    prefix, separator, tail = raw.rpartition(")")
    if not separator:
        return None
    try:
        pid = int(prefix.split(" ", 1)[0])
        fields = tail.split()
        return {
            "pid": pid,
            "state": fields[0],
            "ppid": int(fields[1]),
            "pgid": int(fields[2]),
            "start_ticks": int(fields[19]),
        }
    except (IndexError, ValueError):
        return None


def read_linux_process_snapshot(proc_root: Path = Path("/proc")) -> dict[int, dict[str, int | str]] | None:
    """Return a best-effort Linux process table, or ``None`` off Linux."""
    if not proc_root.is_dir():
        return None
    process_table: dict[int, dict[str, int | str]] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            record = _parse_linux_process_stat((entry / "stat").read_text(encoding="utf-8"))
        except OSError:
            continue
        if record is not None:
            process_table[int(record["pid"])] = record
    return process_table


def _snapshot_descendant_pids(pid: int, process_table: dict[int, dict[str, int | str]]) -> set[int]:
    children_by_parent: dict[int, list[int]] = {}
    for child_pid, record in process_table.items():
        children_by_parent.setdefault(int(record["ppid"]), []).append(child_pid)
    descendants: set[int] = set()
    pending = [pid]
    while pending:
        parent = pending.pop()
        for child in children_by_parent.get(parent, []):
            if child in descendants:
                continue
            descendants.add(child)
            pending.append(child)
    return descendants


def _snapshot_process_is_live(record: dict[str, int | str]) -> bool:
    return str(record["state"]) not in TERMINAL_PROCESS_STATES


def _target_identity_matches(target: dict[str, Any], observed: dict[str, int | str]) -> bool:
    expected_identity = target.get("expected_identity")
    if expected_identity is not None:
        return expected_identity == f"linux-proc:{observed['start_ticks']}"
    expected_start_ticks = pid_int(target.get("expected_start_ticks"))
    return expected_start_ticks is not None and expected_start_ticks == observed["start_ticks"]


def _reap_zombie(
    pid: int,
    *,
    waitpid: Callable[[int, int], tuple[int, int]],
) -> str:
    try:
        waited_pid, _status = waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return "external_parent"
    except OSError as exc:
        return f"waitpid_error:{type(exc).__name__}"
    return "reaped_by_workbench" if waited_pid == pid else "not_exited"


def _process_observations(
    targets: list[dict[str, Any]],
    process_table: dict[int, dict[str, int | str]],
    *,
    reap_statuses: dict[int, str],
    waitpid: Callable[[int, int], tuple[int, int]],
) -> tuple[list[dict[str, Any]], set[int]]:
    observations: list[dict[str, Any]] = []
    live_pids: set[int] = set()
    for target in targets:
        pid = pid_int(target.get("pid"))
        if pid is None:
            continue
        observed = process_table.get(pid)
        receipt: dict[str, Any] = {
            "role": target.get("role"),
            "pid": pid,
            "pgid": pid_int(target.get("pgid")),
            "expected_identity": target.get("expected_identity"),
            "expected_start_ticks": pid_int(target.get("expected_start_ticks")),
        }
        if observed is None:
            receipt.update({"status": "absent", "state": None, "reap_status": reap_statuses.get(pid)})
            observations.append(receipt)
            continue
        receipt.update(
            {
                "state": observed["state"],
                "observed_pgid": observed["pgid"],
                "observed_start_ticks": observed["start_ticks"],
            }
        )
        if not _target_identity_matches(target, observed):
            receipt.update({"status": "identity_mismatch", "reap_status": None})
            live_pids.add(pid)
            observations.append(receipt)
            continue
        if observed["state"] != "Z":
            receipt.update({"status": "live_survivor", "reap_status": None})
            live_pids.add(pid)
            observations.append(receipt)
            continue

        reap_status = reap_statuses.get(pid)
        if reap_status is None:
            reap_status = _reap_zombie(pid, waitpid=waitpid)
            reap_statuses[pid] = reap_status
        if reap_status == "reaped_by_workbench":
            process_table.pop(pid, None)
            receipt.update({"status": "absent", "state": None, "reap_status": reap_status})
            observations.append(receipt)
            continue

        live_descendants = sorted(
            child_pid
            for child_pid in _snapshot_descendant_pids(pid, process_table)
            if _snapshot_process_is_live(process_table[child_pid])
        )
        pgid = int(observed["pgid"])
        live_group_members = sorted(
            member_pid
            for member_pid, member in process_table.items()
            if int(member["pgid"]) == pgid and _snapshot_process_is_live(member)
        )
        receipt.update(
            {
                "live_descendant_pids": live_descendants,
                "live_group_member_pids": live_group_members,
                "reap_status": reap_status,
            }
        )
        if live_descendants or live_group_members:
            receipt["status"] = "zombie_with_live_processes"
            live_pids.add(pid)
        else:
            receipt["status"] = "zombie_awaiting_external_reaper"
        observations.append(receipt)
    return observations, live_pids


def _live_process_groups(
    pgids: list[int],
    process_table: dict[int, dict[str, int | str]],
) -> tuple[list[int], list[dict[str, Any]]]:
    live_pgids: list[int] = []
    observations: list[dict[str, Any]] = []
    for pgid in pgids:
        members = [record for record in process_table.values() if int(record["pgid"]) == pgid]
        live_members = sorted(int(record["pid"]) for record in members if _snapshot_process_is_live(record))
        zombie_members = sorted(int(record["pid"]) for record in members if record["state"] == "Z")
        if live_members:
            status = "live_survivor"
            live_pgids.append(pgid)
        elif zombie_members:
            status = "zombie_only"
        else:
            status = "absent"
        observations.append(
            {
                "pgid": pgid,
                "status": status,
                "live_member_pids": live_members,
                "zombie_member_pids": zombie_members,
            }
        )
    return live_pgids, observations


def wait_for_owned_processes(
    *,
    process_targets: Iterable[dict[str, Any]],
    pgids: Iterable[Any],
    timeout_seconds: float,
    poll_seconds: float = 0.05,
    snapshot_reader: Callable[[], dict[int, dict[str, int | str]] | None] = read_linux_process_snapshot,
    waitpid: Callable[[int, int], tuple[int, int]] = os.waitpid,
    fallback_wait: Callable[..., dict[str, Any]] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait for owned targets while distinguishing zombies from live survivors."""
    targets = [dict(target) for target in process_targets if pid_int(target.get("pid")) is not None]
    target_pgids = _positive_unique(pgids)
    started = monotonic()
    deadline = started + max(0.0, float(timeout_seconds))
    reap_statuses: dict[int, str] = {}
    while True:
        process_table = snapshot_reader()
        if process_table is None:
            if fallback_wait is None:
                from hol_workbench.process_groups import wait_for_quiescence

                fallback_wait = wait_for_quiescence
            result = fallback_wait(
                pids=[target["pid"] for target in targets],
                pgids=target_pgids,
                timeout_seconds=max(0.0, deadline - monotonic()),
            )
            return {
                **result,
                "observation_source": "portable_signal_probe",
                "process_observations": [],
                "process_group_observations": [],
                "zombie_pids": [],
                "absent_pids": [],
            }
        process_observations, live_pids = _process_observations(
            targets,
            process_table,
            reap_statuses=reap_statuses,
            waitpid=waitpid,
        )
        live_pgids, group_observations = _live_process_groups(target_pgids, process_table)
        zombie_pids = sorted(
            int(item["pid"]) for item in process_observations if item["status"] == "zombie_awaiting_external_reaper"
        )
        absent_pids = sorted(int(item["pid"]) for item in process_observations if item["status"] == "absent")
        now = monotonic()
        if not live_pids and not live_pgids:
            status = "quiescent"
        elif now >= deadline:
            status = "survivors"
        else:
            sleep(min(max(0.0, poll_seconds), max(0.0, deadline - now)))
            continue
        return {
            "status": status,
            "elapsed_seconds": round(now - started, 3),
            "live_pids": sorted(live_pids),
            "live_pgids": live_pgids,
            "observation_source": "linux_proc",
            "process_observations": process_observations,
            "process_group_observations": group_observations,
            "zombie_pids": zombie_pids,
            "absent_pids": absent_pids,
        }
