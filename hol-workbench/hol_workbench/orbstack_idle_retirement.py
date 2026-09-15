"""Low-write, fail-closed retirement for disposable restored profile runtimes."""

from __future__ import annotations

import argparse
import io
import secrets
import subprocess
import time
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hol_workbench.checkout_python import checkout_module_command
from hol_workbench.cli.orbstack_criu_restore import EXPECTED_RESTORE_ERRORS, pool_is_live
from hol_workbench.criu_shelf_admission import try_exclusive_criu_shelf_admission
from hol_workbench.criu_shelf_capacity import restored_shelf_capacity
from hol_workbench.criu_shelf_demand import read_shelf_demands
from hol_workbench.criu_shelf_owner import read_shelf_owners
from hol_workbench.fork_pool_lifecycle import stop_pool_runtime
from hol_workbench.jsonio import atomic_write_json, read_json
from hol_workbench.process_groups import process_birth_identity
from hol_workbench.proof_run_fork_children import read_active_children

DEFAULT_IDLE_SECONDS = 600.0
CHECK_INTERVAL_SECONDS = 10.0
POLICY_SCHEMA = "hol-workbench.orbstack-runtime-cache.v1"
POLICY_FILENAME = "runtime-cache.json"
ACTIVITY_FILENAME = "runtime-cache.activity"


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def policy_path(profile_root: Path) -> Path:
    return profile_root / POLICY_FILENAME


def activity_path(profile_root: Path) -> Path:
    return profile_root / ACTIVITY_FILENAME


def one_pool(profile_root: Path) -> Path:
    pools = sorted((profile_root / "pool").glob("*"))
    if len(pools) != 1:
        raise RuntimeError(f"expected one pool under {profile_root / 'pool'}, found {len(pools)}")
    return pools[0]


def touch_profile_activity(profile_root: Path) -> None:
    """Renew the runtime cache lease with a metadata-only touch after first creation."""

    activity = activity_path(profile_root)
    activity.touch(exist_ok=True)


def _monitor_live(record: dict[str, Any]) -> bool:
    expected = record.get("monitor_identity")
    return bool(expected and process_birth_identity(record.get("monitor_pid")) == expected)


def schedule_profile_retirement(
    profile_root: Path,
    *,
    logical_profile: str,
    idle_seconds: float = DEFAULT_IDLE_SECONDS,
) -> dict[str, Any]:
    """Renew one monitor; never writes or updates the immutable CRIU image."""

    if idle_seconds <= 0:
        raise ValueError("profile retirement idle period must be positive")
    touch_profile_activity(profile_root)
    current = read_json(policy_path(profile_root))
    if current.get("schema") == POLICY_SCHEMA and _monitor_live(current):
        return {**current, "scheduled": False, "activity_renewed": True}
    token = secrets.token_hex(12)
    command = checkout_module_command(
        "hol_workbench.orbstack_idle_retirement",
        "--monitor",
        "--profile-root",
        str(profile_root),
        "--logical-profile",
        logical_profile,
        "--idle-seconds",
        str(idle_seconds),
        "--token",
        token,
    )
    pending = {
        "schema": POLICY_SCHEMA,
        "logical_profile": logical_profile,
        "physical_profile": profile_root.name,
        "profile_root": str(profile_root),
        "idle_seconds": idle_seconds,
        "monitor_token": token,
        "monitor_pid": None,
        "monitor_identity": None,
        "monitor_started_utc": _utc_now(),
        "runtime_status": "live-cache",
        "shelf_status": "restore-ready",
        "data_preserved": True,
        "immutable_shelf_preserved": True,
        "activity_path": str(activity_path(profile_root)),
    }
    atomic_write_json(policy_path(profile_root), pending)
    monitor = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    record = {
        **pending,
        "monitor_pid": monitor.pid,
        "monitor_identity": process_birth_identity(monitor.pid),
    }
    atomic_write_json(policy_path(profile_root), record)
    return {**record, "scheduled": True, "activity_renewed": True}


def runtime_cache_state(profile_root: Path) -> dict[str, Any]:
    record = read_json(policy_path(profile_root))
    if record.get("schema") != POLICY_SCHEMA:
        return {
            "policy": "not-yet-managed",
            "idle_seconds": DEFAULT_IDLE_SECONDS,
            "data_preserved": True,
            "immutable_shelf_preserved": True,
        }
    activity = activity_path(profile_root)
    age = max(0.0, time.time() - activity.stat().st_mtime) if activity.exists() else None
    return {
        **record,
        "monitor_live": _monitor_live(record),
        "idle_age_seconds": round(age, 1) if age is not None else None,
        "retire_in_seconds": round(max(0.0, float(record.get("idle_seconds") or 0) - age), 1)
        if age is not None and record.get("runtime_status") == "live-cache"
        else None,
    }


def _quiescence_problem(profile_root: Path, pool: Path) -> str | None:
    if read_shelf_demands(profile_root):
        return "live route demand"
    if read_shelf_owners(profile_root):
        return "active route owner"
    pool_state = read_json(pool / "pool.json")
    sessions = pool_state.get("sessions")
    if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
        return "pool session inventory unavailable"
    if any(item.get("lease") or item.get("status") == "busy" for item in sessions):
        return "active pool lease"
    try:
        children = read_active_children(pool, require_registry=True)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return f"active-child inventory unavailable: {type(exc).__name__}: {exc}"
    if children:
        return "active disposable proof child"
    return None


def _stop_runtime(profile_root: Path, pool: Path) -> tuple[bool, str]:
    del profile_root
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        status = stop_pool_runtime(pool)
    return status == 0, output.getvalue()[-2000:]


def monitor_profile(
    profile_root: Path,
    *,
    logical_profile: str,
    idle_seconds: float,
    token: str,
    check_interval: float = CHECK_INTERVAL_SECONDS,
) -> int:
    pool = one_pool(profile_root)
    while True:
        record = read_json(policy_path(profile_root))
        if record.get("schema") != POLICY_SCHEMA or record.get("monitor_token") != token:
            return 0
        activity = activity_path(profile_root)
        age = max(0.0, time.time() - activity.stat().st_mtime) if activity.exists() else idle_seconds
        if age < idle_seconds:
            time.sleep(min(check_interval, max(0.1, idle_seconds - age)))
            continue
        problem = _quiescence_problem(profile_root, pool)
        if problem:
            time.sleep(check_interval)
            continue
        capacity = restored_shelf_capacity(profile_root)
        with try_exclusive_criu_shelf_admission(profile_root, capacity=capacity) as exclusive:
            if exclusive:
                record = read_json(policy_path(profile_root))
                if record.get("monitor_token") != token:
                    return 0
                current_age = max(0.0, time.time() - activity.stat().st_mtime) if activity.exists() else idle_seconds
                problem = _quiescence_problem(profile_root, pool)
                if current_age >= idle_seconds and not problem:
                    try:
                        live = pool_is_live(pool)
                    except EXPECTED_RESTORE_ERRORS:
                        live = None
                    if live is not None:
                        if live:
                            stopped, output = _stop_runtime(profile_root, pool)
                            if not stopped:
                                failed = {
                                    **record,
                                    "runtime_status": "retire-failed",
                                    "retire_error": output,
                                    "retire_attempt_utc": _utc_now(),
                                }
                                atomic_write_json(policy_path(profile_root), failed)
                                return 1
                        retired = {
                            **record,
                            "runtime_status": "retired",
                            "retired_utc": _utc_now(),
                            "retire_reason": f"idle for at least {idle_seconds:.0f}s with no owners, demands, or children",
                            "shelf_status": "restore-ready",
                            "data_preserved": True,
                            "immutable_shelf_preserved": True,
                            "restore_behavior": "next named-profile use restores the same immutable shelf",
                        }
                        atomic_write_json(policy_path(profile_root), retired)
                        return 0
        time.sleep(check_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("--profile-root", required=True, type=Path)
    parser.add_argument("--logical-profile", required=True)
    parser.add_argument("--idle-seconds", required=True, type=float)
    parser.add_argument("--token", required=True)
    args = parser.parse_args(argv)
    if not args.monitor:
        parser.error("this module is an internal profile retirement monitor")
    return monitor_profile(
        args.profile_root,
        logical_profile=args.logical_profile,
        idle_seconds=args.idle_seconds,
        token=args.token,
    )


if __name__ == "__main__":
    raise SystemExit(main())
