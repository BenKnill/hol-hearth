"""Fail-closed process ownership for a public CRIU restore transaction."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hol_workbench.process_groups import (
    process_birth_identity,
    process_start_ticks,
    signal_process_group,
    terminate_process_group,
    wait_for_quiescence,
)

RESTORED_ROLES = ("worker", "basis")
STOPPED_PROCESS_STATES = {"T", "t"}
TERMINAL_PROCESS_STATES = {"X", "x", "Z"}


def _positive_int(value: Any, *, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"restored {field} is not a positive integer") from exc
    if result <= 0:
        raise RuntimeError(f"restored {field} is not a positive integer")
    return result


def read_recorded_pool_processes(pool: Path) -> dict[str, dict[str, int]]:
    """Read the two owned process groups recorded in a stopped pool."""
    data = json.loads((pool / "pool.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("restored pool metadata is not a JSON object")
    records: dict[str, dict[str, int]] = {}
    for role in RESTORED_ROLES:
        pid = _positive_int(data.get(f"{role}_pid"), field=f"{role}_pid")
        pgid = _positive_int(data.get(f"{role}_pgid") or pid, field=f"{role}_pgid")
        records[role] = {"pid": pid, "recorded_pgid": pgid}
    return records


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_restored_root_pid(
    pidfile: Path,
    *,
    reader: Callable[[Path], str] = _read_text,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    """Read CRIU's root-owned authoritative PID with one bounded sudo fallback."""
    try:
        raw = reader(pidfile)
    except PermissionError:
        try:
            completed = run(
                ["sudo", "-n", "cat", "--", str(pidfile)],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"cannot read CRIU root pidfile {pidfile}") from exc
        raw = completed.stdout
    return _positive_int(raw.strip(), field="root pidfile")


def process_state(pid: int) -> str | None:
    """Return the Linux process state letter from ``/proc/PID/stat``."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        _, separator, tail = raw.rpartition(")")
        return tail.split()[0] if separator else None
    except (IndexError, OSError):
        return None


def child_pids(pid: int, *, proc_root: Path = Path("/proc")) -> list[int]:
    """Read direct children of every task while CRIU keeps the tree stopped."""
    task_root = proc_root / str(pid) / "task"

    def task_ids() -> list[int]:
        return sorted(int(item.name) for item in task_root.iterdir() if item.name.isdigit())

    before = task_ids()
    if not before:
        raise RuntimeError(f"restored process {pid} has no readable task records")
    children: set[int] = set()
    for tid in before:
        path = task_root / str(tid) / "children"
        children.update(int(item) for item in path.read_text(encoding="utf-8").split())
    after = task_ids()
    if after != before:
        raise RuntimeError(f"restored process {pid} task list changed during stopped-tree capture")
    return sorted(children)


def capture_authoritative_tree(
    root_pid: int,
    *,
    children_reader: Callable[[int], list[int]] = child_pids,
    pgid_reader: Callable[[int], int] = os.getpgid,
    start_ticks_reader: Callable[[Any], int | None] = process_start_ticks,
    identity_reader: Callable[[Any], str | None] = process_birth_identity,
    state_reader: Callable[[int], str | None] = process_state,
) -> list[dict[str, Any]]:
    """Capture the exact stopped process tree rooted at CRIU's pidfile PID."""
    pending = [root_pid]
    seen: set[int] = set()
    records: list[dict[str, Any]] = []
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        record: dict[str, Any] = {
            "pid": pid,
            "pgid": None,
            "start_ticks": start_ticks_reader(pid),
            "identity": identity_reader(pid),
            "state": state_reader(pid),
        }
        try:
            record["pgid"] = pgid_reader(pid)
        except OSError as exc:
            record["pgid_error"] = f"{type(exc).__name__}: {exc}"
        try:
            children = children_reader(pid)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            record["children_error"] = f"{type(exc).__name__}: {exc}"
            children = []
        record["children"] = children
        records.append(record)
        pending.extend(children)
    return records


def tree_capture_problems(
    *,
    authoritative_root_pid: int | None,
    recorded: dict[str, dict[str, int]],
    tree: list[dict[str, Any]],
) -> list[str]:
    """Describe gaps that prevent an exact ownership/quiescence claim."""
    if authoritative_root_pid is None:
        return ["authoritative CRIU root pid is unavailable"]
    if not tree:
        return ["restored process tree is empty"]
    problems: list[str] = []
    by_pid: dict[int, dict[str, Any]] = {}
    for item in tree:
        pid = item.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            problems.append("restored process record has an invalid pid")
            continue
        if pid in by_pid:
            problems.append(f"restored process {pid} was captured more than once")
        by_pid[pid] = item
        if not isinstance(item.get("pgid"), int) or int(item["pgid"]) <= 0:
            problems.append(f"restored process {pid} has no process group")
        if not isinstance(item.get("start_ticks"), int) or int(item["start_ticks"]) <= 0:
            problems.append(f"restored process {pid} has no start identity")
        if not isinstance(item.get("identity"), str) or not item["identity"]:
            problems.append(f"restored process {pid} has no birth identity")
        if item.get("state") is None:
            problems.append(f"restored process {pid} has no readable state")
        if item.get("children_error"):
            problems.append(f"restored process {pid} child ownership is unavailable")
        children = item.get("children")
        if not isinstance(children, list) or any(not isinstance(child, int) or child <= 0 for child in children):
            problems.append(f"restored process {pid} has an invalid child list")
    if authoritative_root_pid not in by_pid:
        problems.append(f"authoritative CRIU root pid {authoritative_root_pid} is absent from the captured tree")
    for role, expected in recorded.items():
        if expected["pid"] not in by_pid:
            problems.append(f"recorded {role} pid {expected['pid']} is absent from the captured tree")
    referenced: set[int] = set()
    for item in by_pid.values():
        children = item.get("children")
        if isinstance(children, list):
            referenced.update(child for child in children if isinstance(child, int) and child > 0)
    missing = sorted(referenced - by_pid.keys())
    if missing:
        problems.append(f"captured child processes are missing records {missing}")
    return list(dict.fromkeys(problems))


def validate_restored_tree(
    *,
    authoritative_root_pid: int,
    recorded: dict[str, dict[str, int]],
    tree: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Bind recorded worker/basis ownership to one stopped CRIU tree."""
    errors: list[str] = []
    worker = recorded["worker"]
    if authoritative_root_pid != worker["pid"]:
        errors.append(f"CRIU root pid {authoritative_root_pid} does not match recorded worker pid {worker['pid']}")
    if worker["recorded_pgid"] != authoritative_root_pid:
        errors.append(
            f"recorded worker pgid {worker['recorded_pgid']} does not match CRIU root pid {authoritative_root_pid}"
        )
    by_pid = {int(item["pid"]): item for item in tree}
    capture_problems = tree_capture_problems(
        authoritative_root_pid=authoritative_root_pid,
        recorded=recorded,
        tree=tree,
    )
    if capture_problems:
        errors.append("restored tree ownership capture is incomplete: " + "; ".join(capture_problems))
    roles: dict[str, dict[str, Any]] = {}
    for role in RESTORED_ROLES:
        expected = recorded[role]
        observed = by_pid.get(expected["pid"])
        if observed is None:
            errors.append(f"restored {role} pid {expected['pid']} is outside the CRIU root tree")
            continue
        roles[role] = observed
        if observed.get("pgid") != expected["recorded_pgid"]:
            errors.append(
                f"restored {role} pgid {observed.get('pgid')} does not match recorded pgid {expected['recorded_pgid']}"
            )
        if observed.get("start_ticks") is None or observed.get("identity") is None:
            errors.append(f"restored {role} process identity is unavailable")
        if observed.get("state") not in STOPPED_PROCESS_STATES:
            errors.append(f"restored {role} process is not stopped (state={observed.get('state')!r})")
        if observed.get("children_error"):
            errors.append(f"restored {role} child ownership is unavailable")
    expected_pgids = {item["recorded_pgid"] for item in recorded.values()}
    unexpected_pgids = sorted(
        {int(item["pgid"]) for item in tree if isinstance(item.get("pgid"), int)} - expected_pgids
    )
    if unexpected_pgids:
        errors.append(f"restored tree has unrecorded process groups {unexpected_pgids}")
    unstopped = sorted(int(item["pid"]) for item in tree if item.get("state") not in STOPPED_PROCESS_STATES)
    if unstopped:
        errors.append(f"restored tree has processes not held stopped {unstopped}")
    if errors:
        raise RuntimeError("; ".join(dict.fromkeys(errors)))
    return roles


def capture_recorded_processes(recorded: dict[str, dict[str, int]]) -> dict[str, dict[str, Any]]:
    """Capture worker/basis identities for the internal build-time restore."""
    roles: dict[str, dict[str, Any]] = {}
    for role, expected in recorded.items():
        pid = expected["pid"]
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
        roles[role] = {
            "pid": pid,
            "pgid": pgid,
            "start_ticks": process_start_ticks(pid),
            "identity": process_birth_identity(pid),
            "state": process_state(pid),
        }
        if pgid != expected["recorded_pgid"]:
            raise RuntimeError(f"restored {role} process group is unavailable")
        if roles[role]["start_ticks"] is None or roles[role]["identity"] is None:
            raise RuntimeError(f"restored {role} process identity is unavailable")
    return roles


def _identity_matches(record: dict[str, Any]) -> bool:
    expected = record.get("identity")
    return isinstance(expected, str) and process_birth_identity(record.get("pid")) == expected


def resume_restored_processes(roles: dict[str, dict[str, Any]]) -> list[int]:
    """Resume only the identity-validated worker and basis process groups."""
    resumed: list[int] = []
    for role in ("basis", "worker"):
        record = roles[role]
        pid = int(record["pid"])
        pgid = int(record["pgid"])
        if not _identity_matches(record) or os.getpgid(pid) != pgid:
            raise RuntimeError(f"restored {role} identity changed before resume")
        if not signal_process_group(pgid, signal.SIGCONT):
            raise RuntimeError(f"restored {role} process group disappeared before resume")
        resumed.append(pgid)
    deadline = time.monotonic() + 0.5
    while True:
        problems = []
        terminal = []
        for role, record in roles.items():
            pid = int(record["pid"])
            state = process_state(pid)
            if state in TERMINAL_PROCESS_STATES:
                terminal.append(f"{role}:{state}")
            elif state is None or state in STOPPED_PROCESS_STATES or not _identity_matches(record):
                problems.append(role)
        if terminal:
            raise RuntimeError(f"restored process groups entered terminal states after SIGCONT: {terminal}")
        if not problems:
            return resumed
        if time.monotonic() >= deadline:
            raise RuntimeError(f"restored process groups did not resume cleanly after SIGCONT: {problems}")
        time.sleep(0.01)


def terminate_restored_tree(tree: list[dict[str, Any]], *, root_pid: int | None) -> dict[str, Any]:
    """Kill captured restored groups with birth-identity checks and verify quiescence."""
    representatives: dict[int, dict[str, Any]] = {}
    for record in tree:
        pgid = record.get("pgid")
        if not isinstance(pgid, int) or pgid <= 0:
            continue
        existing = representatives.get(pgid)
        if existing is None or int(record["pid"]) == pgid:
            representatives[pgid] = record
    root_pgid = next(
        (int(item["pgid"]) for item in tree if item.get("pid") == root_pid and isinstance(item.get("pgid"), int)),
        None,
    )
    ordered_pgids = sorted(representatives, key=lambda pgid: pgid == root_pgid)
    terminations = []
    for pgid in ordered_pgids:
        record = representatives[pgid]
        terminations.append(
            terminate_process_group(
                pgid,
                identity_pid=record.get("pid"),
                expected_identity=record.get("identity"),
                expected_start_ticks=record.get("start_ticks"),
                require_identity=True,
                term_grace_seconds=0,
                kill_grace_seconds=1,
            )
        )
    final = wait_for_quiescence(
        pids=[item.get("pid") for item in tree],
        pgids=representatives,
        timeout_seconds=1,
    )
    verified = final["status"] == "quiescent"
    return {
        "status": "quiescent" if verified else "survivors",
        "verified_quiescent": verified,
        "terminations": terminations,
        "final_wait": final,
    }


def tree_receipt_records(tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep failure artifacts compact and free of transient traversal details."""
    keys = ("pid", "pgid", "start_ticks", "identity", "state")
    return [{key: item.get(key) for key in keys} for item in tree]
