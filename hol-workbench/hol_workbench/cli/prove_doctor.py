"""Optional checks of warm snapshot compatibility and queue state.

This does not run proof source or validate its object files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from hol_workbench.cli.prove_runtime_status import collect_status
from hol_workbench.cli.public_commands import public_command
from hol_workbench.cli.published_profile import resolve_published_warm_profile
from hol_workbench.criu_shelf_demand import read_shelf_demands
from hol_workbench.criu_shelf_owner import read_shelf_owners, shelf_owner_progress
from hol_workbench.runtime_config import RuntimeConfigError, load_runtime_config


def _attempt_ids(rows: list[dict[str, Any]]) -> set[str]:
    return {str(row.get("attempt_id")) for row in rows if row.get("attempt_id")}


def collect_doctor(script_dir: Path, *, profile: str | None, stalled_after: float) -> dict[str, Any]:
    layers: list[dict[str, Any]] = [{"layer": "linux", "status": "ready", "detail": "command is running in Linux"}]
    try:
        config = load_runtime_config()
    except (OSError, RuntimeConfigError, ValueError) as exc:
        layers.append({"layer": "config", "status": "blocked", "detail": " ".join(str(exc).split())[:300]})
        runtime_config: dict[str, Any] | None = None
    else:
        runtime_config = config.record()
        layers.append({"layer": "config", "status": "ready", "detail": str(config.path)})

    try:
        status = collect_status(script_dir, profile=profile)
    except (OSError, RuntimeError, SystemExit, ValueError) as exc:
        detail = " ".join(str(exc).split())[:300]
        layers.extend(
            [
                {"layer": "profiles", "status": "blocked", "detail": detail},
                {"layer": "queue", "status": "not-checked", "detail": "profile discovery failed"},
                {"layer": "lifecycle", "status": "not-checked", "detail": "profile discovery failed"},
            ]
        )
        return {
            "schema": "hol-workbench.public-doctor.v1",
            "status": "blocked",
            "layers": layers,
            "runtime_config": runtime_config,
            "runtime": {
                "status": "blocked",
                "profiles": [],
                "summary": {"profiles": 0, "workers": 0, "active": 0, "queued": 0, "unavailable": 0},
            },
            "suspected_stalls": [],
            "stale_records": [],
        }
    unavailable = [row["profile"] for row in status["profiles"] if row["status"] == "unavailable"]
    layers.append(
        {
            "layer": "profiles",
            "status": "blocked" if unavailable else "ready",
            "detail": ", ".join(unavailable) if unavailable else f"{len(status['profiles'])} compatible",
        }
    )

    saturated = [
        row["profile"]
        for row in status["profiles"]
        if row["queued"] and row["active"] >= row["capacity"] > 0
    ]
    waiting = [row["profile"] for row in status["profiles"] if row["queued"] and row["profile"] not in saturated]
    queue_state = "saturated" if saturated else "waiting" if waiting else "clear"
    layers.append(
        {
            "layer": "queue",
            "status": queue_state,
            "detail": ", ".join(saturated or waiting) if saturated or waiting else "no live waiters",
        }
    )

    stale_records: list[str] = []
    suspected_stalls: list[dict[str, Any]] = []
    for row in status["profiles"]:
        if row["status"] == "unavailable":
            continue
        resolved = resolve_published_warm_profile(script_dir, str(row["profile"]))
        live_owners = read_shelf_owners(resolved.root)
        all_owners = read_shelf_owners(resolved.root, require_live=False)
        live_demands = read_shelf_demands(resolved.root)
        all_demands = read_shelf_demands(resolved.root, require_live=False)
        stale_count = len(_attempt_ids(all_owners) - _attempt_ids(live_owners)) + len(
            _attempt_ids(all_demands) - _attempt_ids(live_demands)
        )
        if stale_count:
            stale_records.append(f"{row['profile']}={stale_count}")
        for owner in live_owners:
            elapsed = owner.get("elapsed_seconds")
            progress = shelf_owner_progress(resolved.root, owner)
            if (
                isinstance(elapsed, int | float)
                and elapsed >= stalled_after
                and progress.get("kind") != "active-disposable-child"
            ):
                suspected_stalls.append(
                    {
                        "profile": row["profile"],
                        "attempt_id": owner.get("attempt_id"),
                        "elapsed_seconds": elapsed,
                        "progress": progress,
                    }
                )
    lifecycle_state = "suspected-stall" if suspected_stalls else "clean"
    layers.append(
        {
            "layer": "lifecycle",
            "status": lifecycle_state,
            "detail": (
                ", ".join(str(item["profile"]) for item in suspected_stalls)
                if suspected_stalls
                else "no live stall"
            ),
        }
    )

    blocked = any(layer["status"] == "blocked" for layer in layers)
    degraded = blocked or queue_state != "clear" or lifecycle_state != "clean"
    return {
        "schema": "hol-workbench.public-doctor.v1",
        "status": "blocked" if blocked else "degraded" if degraded else "healthy",
        "layers": layers,
        "runtime_config": runtime_config,
        "runtime": status,
        "suspected_stalls": suspected_stalls,
        "stale_records": stale_records,
    }


def _layer(report: dict[str, Any], name: str) -> dict[str, Any]:
    return next(layer for layer in report["layers"] if layer["layer"] == name)


def render_doctor(
    report: dict[str, Any], *, profile: str | None = "light", stalled_after: float = 120.0
) -> list[str]:
    runtime = report["runtime"]["summary"]
    config = _layer(report, "config")
    profiles = _layer(report, "profiles")
    queue = _layer(report, "queue")
    lifecycle = _layer(report, "lifecycle")
    if report["status"] == "healthy":
        next_step = "rerun the same prove command"
    elif config["status"] == "blocked":
        next_step = ("select an existing runtime with HOL_WORKBENCH_RUNTIME_CONFIG or hearth configure; "
                     "use hearth setup only for a new environment; see docs/setup.md")
    elif queue["status"] in {"saturated", "waiting"} and lifecycle["status"] == "clean":
        next_step = public_command("prove", "status", *(["--profile", profile] if profile else []), "--watch")
    else:
        next_step = "inspect DETAILS and profile build logs; see docs/setup.md before changing an existing runtime"
    selection = ["--profile", profile] if profile is not None else ["--all-profiles"]
    return [
        f"DOCTOR: {report['status']}",
        "OS: ready (Linux)",
        f"CONFIG: {config['status']} ({config['detail']})",
        f"PROFILES: {profiles['status']} ({profiles['detail']})",
        f"WORKERS: resident={runtime.get('workers', 0)} active={runtime['active']}",
        f"QUEUE: {queue['status']} ({queue['detail']})",
        f"LIFECYCLE: {lifecycle['status']} ({lifecycle['detail']})",
        f"NEXT: {next_step}",
        f"DETAILS: {public_command('prove', 'doctor', *selection, '--stalled-after', str(stalled_after), '--json')}",
    ]


def _parse(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="prove doctor", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--profile", default="light", help="Profile to check (default: light)")
    group.add_argument("--all-profiles", action="store_true", help="Also check optional profiles that may not be installed")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--stalled-after", type=float, default=120.0)
    options = parser.parse_args(args)
    if options.stalled_after < 30:
        parser.error("--stalled-after must be at least 30 seconds")
    return options


def main(args: list[str], *, script_dir: str | Path) -> int:
    options = _parse(args)
    try:
        report = collect_doctor(
            Path(script_dir).resolve(), profile=None if options.all_profiles else options.profile, stalled_after=options.stalled_after
        )
    except (OSError, RuntimeError, SystemExit, ValueError) as exc:
        print(f"prove doctor: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True) if options.json else "\n".join(render_doctor(
        report, profile=None if options.all_profiles else options.profile, stalled_after=options.stalled_after
    )))
    return 0 if report["status"] == "healthy" else 1
