"""Verified cooperative stop for warm HOL sessions."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hol_workbench.jsonio import atomic_write_json
from hol_workbench.process_groups import terminate_process_group
from hol_workbench.processes import wait_for_owned_processes
from hol_workbench.proof_run_fork_children import read_active_children
from hol_workbench.proof_run_runtime import utc_now

COOPERATIVE_STOP_GRACE_SECONDS = 5.0
FORCED_STOP_GRACE_SECONDS = 1.0


def fork_pool_dir(session_dir: Path, session: dict[str, Any]) -> Path:
    recorded = session.get("pool_dir")
    if recorded:
        return Path(str(recorded)).resolve()
    if session_dir.parent.name == "sessions":
        return session_dir.parent.parent
    raise RuntimeError(f"cannot determine fork pool for session {session_dir}")


def _active_child_records(session_dir: Path, session: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    if session.get("engine") != "fork_basis":
        return [], None
    try:
        registry = session.get("active_children_registry")
        if registry and not Path(str(registry)).is_file():
            raise RuntimeError(f"fork active-child registry is missing: {registry}")
        records = read_active_children(fork_pool_dir(session_dir, session))
        for item in records:
            int(item["pgid"])
        return records, None
    except (KeyError, OSError, TypeError, ValueError, RuntimeError) as exc:
        return [], str(exc)


def _target_groups(session_dir: Path, session: dict[str, Any]) -> tuple[list[dict[str, Any]], list[int], str | None]:
    child_records, registry_error = _active_child_records(session_dir, session)
    child_pgids = [int(item["pgid"]) for item in child_records]
    basis_pgid = session.get("basis_pgid") or session.get("basis_pid") or session.get("hol_pid")
    worker_pgid = session.get("worker_pgid") or session.get("worker_pid")
    owned_pgids = [*child_pgids]
    for value in (basis_pgid, worker_pgid):
        if value is None:
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in owned_pgids:
            owned_pgids.append(number)
    return child_records, owned_pgids, registry_error


def _process_targets(session: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "role": "worker",
            "pid": session.get("worker_pid"),
            "pgid": session.get("worker_pgid") or session.get("worker_pid"),
            "expected_start_ticks": session.get("worker_start_ticks"),
            "expected_identity": session.get("worker_identity"),
        },
        {
            "role": "basis",
            "pid": session.get("basis_pid") or session.get("hol_pid"),
            "pgid": session.get("basis_pgid") or session.get("basis_pid") or session.get("hol_pid"),
            "expected_start_ticks": session.get("basis_start_ticks"),
            "expected_identity": session.get("basis_identity"),
        },
    ]


def stop_warm_session(
    *,
    session_dir: Path,
    session: dict[str, Any],
    request_stop: Callable[[], dict[str, Any]],
    cooperative_grace_seconds: float = COOPERATIVE_STOP_GRACE_SECONDS,
    forced_grace_seconds: float = FORCED_STOP_GRACE_SECONDS,
) -> dict[str, Any]:
    child_records, owned_pgids, registry_error = _target_groups(session_dir, session)
    process_targets = _process_targets(session)
    owned_pids = [target["pid"] for target in process_targets]
    initial = wait_for_owned_processes(process_targets=process_targets, pgids=owned_pgids, timeout_seconds=0)
    response = None
    request_error = None
    if initial["status"] != "quiescent":
        try:
            response = request_stop()
        except Exception as exc:
            request_error = f"{type(exc).__name__}: {exc}"
    refreshed_children, refresh_error = _active_child_records(session_dir, session)
    registry_error = registry_error or refresh_error
    records_by_pgid = {int(item["pgid"]): item for item in child_records}
    for record in refreshed_children:
        pgid = int(record["pgid"])
        records_by_pgid[pgid] = record
        if pgid not in owned_pgids:
            owned_pgids.append(pgid)
    cooperative = wait_for_owned_processes(
        process_targets=process_targets,
        pgids=owned_pgids,
        timeout_seconds=cooperative_grace_seconds if initial["status"] != "quiescent" else 0,
    )
    escalations = []
    if cooperative["status"] != "quiescent":
        basis_pgid = session.get("basis_pgid") or session.get("basis_pid") or session.get("hol_pid")
        worker_pgid = session.get("worker_pgid") or session.get("worker_pid")
        if basis_pgid is None:
            basis_pgid_int = 0
        else:
            try:
                basis_pgid_int = int(basis_pgid)
            except (TypeError, ValueError):
                basis_pgid_int = 0
        ordered = [*records_by_pgid, basis_pgid, worker_pgid]
        seen: set[int] = set()
        for value in ordered:
            try:
                pgid = int(value)
            except (TypeError, ValueError):
                continue
            if pgid <= 0 or pgid in seen:
                continue
            seen.add(pgid)
            record = records_by_pgid.get(pgid)
            if record:
                identity_pid = record.get("pid")
                expected_identity = record.get("process_identity")
                expected_start_ticks = record.get("process_start_ticks")
            elif pgid == basis_pgid_int:
                identity_pid = session.get("basis_pid") or session.get("hol_pid")
                expected_identity = session.get("basis_identity")
                expected_start_ticks = session.get("basis_start_ticks")
            else:
                identity_pid = session.get("worker_pid")
                expected_identity = session.get("worker_identity")
                expected_start_ticks = session.get("worker_start_ticks")
            escalations.append(
                terminate_process_group(
                    pgid,
                    identity_pid=identity_pid,
                    expected_identity=expected_identity,
                    expected_start_ticks=expected_start_ticks,
                    require_identity=True,
                    term_grace_seconds=forced_grace_seconds,
                    kill_grace_seconds=forced_grace_seconds,
                )
            )
    final_children, final_registry_error = _active_child_records(session_dir, session)
    registry_error = registry_error or final_registry_error
    escalated_pgids = {int(item["pgid"]) for item in escalations if item.get("pgid")}
    for record in final_children:
        pgid = int(record["pgid"])
        if pgid not in owned_pgids:
            owned_pgids.append(pgid)
        if pgid not in escalated_pgids:
            escalations.append(
                terminate_process_group(
                    pgid,
                    identity_pid=record.get("pid"),
                    expected_identity=record.get("process_identity"),
                    expected_start_ticks=record.get("process_start_ticks"),
                    require_identity=True,
                    term_grace_seconds=forced_grace_seconds,
                    kill_grace_seconds=forced_grace_seconds,
                )
            )
    final = wait_for_owned_processes(
        process_targets=process_targets,
        pgids=owned_pgids,
        timeout_seconds=forced_grace_seconds if escalations else 0,
    )
    verified = final["status"] == "quiescent" and registry_error is None
    return {
        "status": "stopped" if verified else "stop_failed",
        "verified_quiescent": verified,
        "request_response": response,
        "request_error": request_error,
        "registry_error": registry_error,
        "cooperative_wait": cooperative,
        "escalations": escalations,
        "final_wait": final,
        "owned_pids": [int(value) for value in owned_pids if isinstance(value, int) and value > 0],
        "owned_pgids": owned_pgids,
    }


def run_warm_stop_command(
    *, session_dir: Path, session: dict[str, Any], request_stop: Callable[[], dict[str, Any]]
) -> int:
    result = stop_warm_session(session_dir=session_dir, session=session, request_stop=request_stop)
    session["status"] = result["status"]
    session["stop_result"] = result
    session["updated_utc"] = utc_now()
    atomic_write_json(session_dir / "session.json", session)
    print("proof-run warm stop")
    print(f"session: {session_dir}")
    print(f"status: {result['status']}")
    print(f"verified_quiescent: {str(result['verified_quiescent']).lower()}")
    if result.get("request_error"):
        print(f"cooperative request: {result['request_error']}", file=sys.stderr)
    if result["status"] != "stopped":
        final = result.get("final_wait") or {}
        print(f"surviving pids: {final.get('live_pids') or []}", file=sys.stderr)
        print(f"surviving process groups: {final.get('live_pgids') or []}", file=sys.stderr)
        if result.get("registry_error"):
            print(f"active-child registry: {result['registry_error']}", file=sys.stderr)
    else:
        zombie_pids = (result.get("final_wait") or {}).get("zombie_pids") or []
        if zombie_pids:
            print(f"zombie pids awaiting external reaper: {zombie_pids}")
    return 0 if result["status"] == "stopped" else 1
