"""Read-only public worker and queue status."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hol_workbench.cli.prove_profiles import public_authoring_profile_names
from hol_workbench.cli.public_commands import public_command
from hol_workbench.cli.published_profile import resolve_published_warm_profile
from hol_workbench.criu_shelf_capacity import restored_shelf_capacity
from hol_workbench.criu_shelf_demand import read_shelf_demands
from hol_workbench.criu_shelf_owner import read_shelf_owners
from hol_workbench.orbstack_runtime_inventory import profile_runtime_inventory


def _age_seconds(created_utc: object, *, now: datetime | None = None) -> float | None:
    if not isinstance(created_utc, str):
        return None
    try:
        created = datetime.fromisoformat(created_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    current = now or datetime.now(UTC)
    return max(0.0, (current - created).total_seconds())


def _profile_status(script_dir: Path, name: str) -> dict[str, Any]:
    try:
        profile = resolve_published_warm_profile(script_dir, name)
    except (OSError, RuntimeError, SystemExit, ValueError) as exc:
        return {
            "profile": name,
            "status": "unavailable",
            "capacity": 0,
            "active": 0,
            "queued": 0,
            "reason": " ".join(str(exc).split())[:300],
        }
    owners = read_shelf_owners(profile.root)
    runtime = profile_runtime_inventory(profile.root)
    owner_ids = {str(owner.get("attempt_id")) for owner in owners if owner.get("attempt_id")}
    demands = sorted(
        (
            demand
            for demand in read_shelf_demands(profile.root)
            if str(demand.get("attempt_id") or "") not in owner_ids
        ),
        key=lambda demand: str(demand.get("created_utc") or ""),
    )
    active = len(owners)
    queued = len(demands)
    status = "queued" if queued else "busy" if active else "idle"
    return {
        "profile": name,
        "status": status,
        "capacity": min(profile.capacity, restored_shelf_capacity(profile.root)),
        "active": active,
        "workers": int(runtime.get("process_count") or 0),
        "queued": queued,
        "oldest_wait_seconds": _age_seconds(demands[0].get("created_utc")) if demands else None,
        "owners": owners,
        "queue": [
            {**demand, "position": position, "wait_seconds": _age_seconds(demand.get("created_utc"))}
            for position, demand in enumerate(demands, start=1)
        ],
        "runtime": runtime,
    }


def collect_status(script_dir: Path, *, profile: str | None = None) -> dict[str, Any]:
    names = public_authoring_profile_names(script_dir)
    if profile is not None:
        if profile not in names:
            raise ValueError(f"unknown public profile {profile!r}; available: {', '.join(names)}")
        names = (profile,)
    rows = [_profile_status(script_dir, name) for name in names]
    unavailable = sum(row["status"] == "unavailable" for row in rows)
    return {
        "schema": "hol-workbench.public-runtime-status.v1",
        "status": "degraded" if unavailable == len(rows) else "ready",
        "profiles": rows,
        "summary": {
            "profiles": len(rows),
            "active": sum(int(row["active"]) for row in rows),
            "workers": sum(int(row.get("workers") or 0) for row in rows),
            "queued": sum(int(row["queued"]) for row in rows),
            "unavailable": unavailable,
        },
    }


def _duration(seconds: object) -> str:
    if not isinstance(seconds, int | float):
        return "-"
    value = float(seconds)
    return f"{value:.1f}s" if value < 60 else f"{value / 60:.1f}m"


def render_status(report: dict[str, Any]) -> list[str]:
    summary = report["summary"]
    lines = [
        f"HEARTH: {report['status']} processes={summary.get('workers', 0)} "
        f"active={summary['active']} queued={summary['queued']}",
        "PROFILE             STATE        PROCESSES ACTIVE/CAP  QUEUED  OLDEST",
    ]
    for row in report["profiles"]:
        active = f"{row['active']}/{row['capacity']}"
        lines.append(
            f"{row['profile']:<19} {row['status']:<12} {row.get('workers', 0):<8} "
            f"{active:<11} {row['queued']:<7} "
            f"{_duration(row.get('oldest_wait_seconds'))}"
        )
    lines.extend(
        [
            f"NEXT: {public_command('prove', 'status', '--watch')}",
            f"DETAILS: {public_command('prove', 'status', '--json')}",
        ]
    )
    return lines


def _parse(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="prove status", description=__doc__)
    parser.add_argument("--profile")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    parsed = parser.parse_args(args)
    if parsed.interval < 0.2:
        parser.error("--interval must be at least 0.2 seconds")
    return parsed


def main(args: list[str], *, script_dir: str | Path) -> int:
    options = _parse(args)
    scripts = Path(script_dir).resolve()
    while True:
        try:
            report = collect_status(scripts, profile=options.profile)
        except (OSError, RuntimeError, SystemExit, ValueError) as exc:
            print(f"prove status: {exc}", file=sys.stderr)
            return 2
        if options.json:
            print(json.dumps(report, sort_keys=True))
        else:
            print("\n".join(render_status(report)))
        if not options.watch:
            return 0
        try:
            time.sleep(options.interval)
        except KeyboardInterrupt:
            return 130
