"""List or retire prepared project bases; each live basis is a resident HOL process."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from hol_workbench.cli.public_commands import public_command
from hol_workbench.project_basis import (
    MAX_LIVE_BASES, basis_cache_dir, list_bases, project_basis_lock, retire_listed_basis,
)


def _age(epoch: object) -> str:
    if not isinstance(epoch, int | float):
        return "-"
    seconds = max(0.0, time.time() - float(epoch))
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.0f}m" if seconds < 5400 else f"{seconds / 3600:.1f}h"


def render(cache_dir: Path, rows: list[dict[str, Any]]) -> list[str]:
    live = [row for row in rows if row["live"] and row["status"] == "ready"]
    lines = [f"BASIS CACHE: {cache_dir} live={len(live)} max_live={MAX_LIVE_BASES} indexed={len(rows)}"]
    if live:
        lines.append("KEY           PROFILE       LAST_USED  PREPARED  BASIS_PID  SOURCE")
    for row in live:
        prepared = row.get("preparation_eval_elapsed_seconds")
        prepared_text = f"{float(prepared):.0f}s" if isinstance(prepared, int | float) else "-"
        lines.append(f"{row['key'][:12]:<13} {str(row.get('logical_profile') or '-'):<13} "
                     f"{_age(row.get('last_used_epoch_seconds')):<10} {prepared_text:<9} "
                     f"{str(row.get('basis_pid') or '-'):<10} {row.get('source') or '-'}")
    retired = len(rows) - len(live)
    if retired:
        lines.append(f"RETIRED: {retired} indexed record(s) without live processes; they cost nothing")
    if live:
        lines.append(f"NEXT: {public_command('basis', 'retire', 'KEY')} frees one; --all frees every live basis here")
    else:
        lines.append("NEXT: prove SOURCE.ml --basis DEPS.ml prepares a basis once for identical inputs")
    return lines


def _parse(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="hearth basis",
        description="Show the prepared project bases kept for --basis reuse, or retire them.",
        allow_abbrev=False,
    )
    parser.add_argument("action", nargs="?", choices=["list", "retire"], default="list")
    parser.add_argument("keys", nargs="*", help="basis keys (prefixes accepted) to retire")
    parser.add_argument("--all", action="store_true", help="retire every live basis in the cache")
    parser.add_argument("--cache-root", metavar="DIR",
                        help="a run root whose DIR/.project-bases held bases before the shared cache")
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(args)
    if parsed.action == "retire" and not parsed.keys and not parsed.all:
        parser.error("retire needs KEY... or --all")
    if parsed.action == "list" and parsed.keys:
        parser.error("list takes no keys")
    return parsed


def main(args: list[str], *, script_dir: str | Path, cwd: str | Path | None = None) -> int:
    options = _parse(args)
    del script_dir
    cache_root = None
    if options.cache_root:
        base = Path(cwd).resolve() if cwd is not None else Path.cwd()
        cache_root = (base / Path(options.cache_root).expanduser()).resolve()
    cache_dir = basis_cache_dir(cache_root)
    rows = list_bases(cache_dir) if cache_dir.is_dir() else []
    if options.action == "list":
        if options.json:
            print(json.dumps({"cache_dir": str(cache_dir), "max_live_bases": MAX_LIVE_BASES, "bases": rows},
                             sort_keys=True))
        else:
            print("\n".join(render(cache_dir, rows)))
        return 0
    live = [row for row in rows if row["live"] and row["status"] == "ready"]
    if options.all:
        selected = live
    else:
        selected = []
        for key in options.keys:
            found = [row for row in live if row["key"].startswith(key)]
            if len(found) != 1:
                print(f"basis: {key!r} matches {len(found)} live bases; use a longer key", file=sys.stderr)
                return 1
            selected.extend(found)
    if not selected:
        print(f"basis: no live basis to retire under {cache_dir}")
        return 0
    failures = 0
    with project_basis_lock(cache_root):
        for row in selected:
            try:
                retire_listed_basis(row, reason="retired_by_hearth_basis")
                print(f"RETIRED: {row['key'][:12]} profile={row.get('logical_profile')} source={row.get('source')}")
            except (OSError, RuntimeError, ValueError) as exc:
                failures += 1
                print(f"basis: could not retire {row['key'][:12]}: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], script_dir=Path(__file__).resolve().parents[2] / "bin"))
