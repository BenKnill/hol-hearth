"""Cancel one active or queued attempt by attempt id or source path; the warm seat stays."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from hol_workbench.cli.prove_profiles import public_authoring_profile_names
from hol_workbench.cli.public_commands import public_command
from hol_workbench.cli.published_profile import resolve_published_warm_profile
from hol_workbench.criu_shelf_demand import read_shelf_demands
from hol_workbench.criu_shelf_owner import cancel_shelf_owner, read_shelf_owners
from hol_workbench.process_groups import process_birth_identity


def list_attempts(script_dir: Path, *, profile: str | None = None) -> list[dict[str, Any]]:
    """Return every live active or queued attempt with the profile that admits it."""
    names = public_authoring_profile_names(script_dir)
    if profile is not None:
        if profile not in names:
            raise ValueError(f"unknown public profile {profile!r}; available: {', '.join(names)}")
        names = (profile,)
    rows: list[dict[str, Any]] = []
    for name in names:
        try:
            resolved = resolve_published_warm_profile(script_dir, name)
        except (OSError, RuntimeError, SystemExit, ValueError):
            continue
        owners = read_shelf_owners(resolved.root)
        owner_ids = {str(owner.get("attempt_id")) for owner in owners}
        for owner in owners:
            rows.append({"kind": "active", "profile": name, "profile_root": resolved.root, **owner})
        for demand in read_shelf_demands(resolved.root):
            if str(demand.get("attempt_id") or "") in owner_ids:
                continue
            rows.append({"kind": "queued", "profile": name, "profile_root": resolved.root, **demand})
    return rows


def _same_source(row: dict[str, Any], source: Path) -> bool:
    recorded = row.get("source")
    if not isinstance(recorded, str) or not recorded:
        return False
    try:
        return Path(recorded).expanduser().resolve() == source
    except (OSError, RuntimeError, ValueError):
        return recorded == str(source)


def _cancel_queued(row: dict[str, Any], *, wait_seconds: float) -> dict[str, Any]:
    """Interrupt a waiter exactly as Ctrl-C would, after checking its process identity."""
    pid = row.get("pid")
    attempt_id = str(row.get("attempt_id") or "")
    if not isinstance(pid, int) or process_birth_identity(pid) != row.get("process_identity"):
        return {"status": "not-running", "attempt_id": attempt_id}
    try:
        os.kill(pid, signal.SIGINT)
    except OSError as exc:
        return {"status": "signal-failed", "attempt_id": attempt_id, "pid": pid, "error": f"{type(exc).__name__}: {exc}"}
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        still_queued = any(
            str(demand.get("attempt_id") or "") == attempt_id
            for demand in read_shelf_demands(Path(row["profile_root"]))
        )
        if not still_queued or process_birth_identity(pid) != row.get("process_identity"):
            return {"status": "dequeued", "attempt_id": attempt_id, "pid": pid}
        time.sleep(0.05)
    return {"status": "cleanup-timeout", "attempt_id": attempt_id, "pid": pid}


def cancel_attempt(row: dict[str, Any], *, wait_seconds: float) -> dict[str, Any]:
    if row["kind"] == "active":
        return cancel_shelf_owner(Path(row["profile_root"]), str(row.get("attempt_id") or ""), wait_seconds=wait_seconds)
    return _cancel_queued(row, wait_seconds=wait_seconds)


def _parse(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hearth cancel",
        description="Interrupt one attempt the way Ctrl-C would. Receipts stay; the shared warm seat stays.",
        allow_abbrev=False,
    )
    parser.add_argument("attempt", nargs="?", help="attempt id as printed by hearth status (route-PID-XXXX)")
    parser.add_argument("--source", metavar="FILE.ml", help="cancel the attempt evaluating this source instead")
    parser.add_argument("--profile", help="only look at this profile's seat and queue")
    parser.add_argument("--wait", type=float, default=30.0,
                        help="seconds to wait for the attempt to release its seat or queue slot (default 30)")
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(args)
    if bool(parsed.attempt) == bool(parsed.source):
        parser.error("name exactly one attempt: ATTEMPT_ID or --source FILE.ml")
    if not parsed.wait >= 0:
        parser.error("--wait must be zero or positive")
    return parsed


def main(args: list[str], *, script_dir: str | Path, cwd: str | Path | None = None) -> int:
    options = _parse(args)
    scripts = Path(script_dir).resolve()
    try:
        rows = list_attempts(scripts, profile=options.profile)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"cancel: {exc}", file=sys.stderr)
        return 2
    if options.source:
        base = Path(cwd).resolve() if cwd is not None else Path.cwd()
        source = (base / Path(options.source).expanduser()).resolve()
        matches = [row for row in rows if _same_source(row, source)]
    else:
        matches = [row for row in rows if str(row.get("attempt_id") or "") == options.attempt]
    if not matches:
        wanted = options.attempt or options.source
        print(f"cancel: no active or queued attempt matches {wanted}; nothing was signalled", file=sys.stderr)
        print(f"NEXT: {public_command('status')} lists attempt ids", file=sys.stderr)
        return 1
    if len(matches) > 1:
        ids = ", ".join(str(row.get("attempt_id")) for row in matches)
        print(f"cancel: {len(matches)} attempts evaluate that source; name one attempt id: {ids}", file=sys.stderr)
        return 1
    row = matches[0]
    result = cancel_attempt(row, wait_seconds=options.wait)
    report = {
        "attempt_id": row.get("attempt_id"), "kind": row["kind"], "profile": row["profile"],
        "source": row.get("source"), "pid": row.get("pid"), "result": result,
    }
    if options.json:
        print(json.dumps(report, sort_keys=True, default=str))
    else:
        print(f"CANCEL: {row.get('attempt_id')} ({row['kind']}) profile={row['profile']} pid={row.get('pid')} "
              f"source={row.get('source')}")
        status = result.get("status")
        if status in {"interrupted", "dequeued"}:
            print(f"RESULT: {status}; SIGINT delivered like Ctrl-C; receipts are retained and the warm seat stays")
        elif status == "cleanup-timeout":
            print(f"RESULT: SIGINT delivered, but the attempt had not released its seat after {options.wait:g}s; "
                  "the interrupted child is still winding down and records its receipt when it exits. "
                  f"Check {public_command('status')} before signalling again")
        else:
            print(f"RESULT: {status}; {result.get('error') or 'nothing further was signalled'}")
    return 0 if result.get("status") in {"interrupted", "dequeued"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], script_dir=Path(__file__).resolve().parents[2] / "bin"))
