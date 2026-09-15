#!/usr/bin/env python3
"""Developer-only final replay and durable evidence utilities."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

CLAIM_SET_SCHEMA = "proof-run.claim-set.v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proof-run",
        description=(
            "Developer-only cold final replay and durable claim comparison. "
            "Ordinary authoring uses prove SOURCE.ml --loop."
        ),
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    vanilla = commands.add_parser(
        "vanilla",
        help="run one raw HOL source and account for every named claim",
        allow_abbrev=False,
    )
    vanilla.add_argument("--source", required=True, help="HOL Light .ml source to run through the raw toplevel")
    vanilla.add_argument("--all", action="store_true", help="account for all named claims extracted from --source")
    vanilla.add_argument("--holdir", default=os.environ.get("HOLDIR", str(Path.home() / "src" / "hol-light")))
    vanilla.add_argument("--cwd", help="working directory for the raw HOL command; default source directory")
    vanilla.add_argument("--run-root", default="runs", help="directory for the durable final-replay receipt")
    vanilla.add_argument("--timeout", type=float, default=None, help="raw HOL execution timeout seconds")
    vanilla.add_argument("--label", help="human-readable replay label")

    claim_set = commands.add_parser(
        "claim-set",
        help="save or compare theorem names, statement hashes, and durable evidence",
        allow_abbrev=False,
    )
    claim_set_input = claim_set.add_mutually_exclusive_group(required=True)
    claim_set_input.add_argument("--source", action="append", dest="sources", help="HOL source; repeatable")
    claim_set_input.add_argument("--manifest", help="proof-run manifest JSON")
    claim_set_input.add_argument("--batch-summary", help="historical batch-summary.json")
    claim_set_input.add_argument(
        "--run-dir", help="historical run directory containing run.json or vanilla-run.json"
    )
    claim_set_input.add_argument("--claim-set", help=f"existing {CLAIM_SET_SCHEMA} JSON")
    claim_set.add_argument("--baseline", help=f"saved {CLAIM_SET_SCHEMA} JSON to compare against")
    claim_set.add_argument("--save", help="write the current claim set to this path")
    claim_set.add_argument("--json", action="store_true", help="emit machine-readable claim set or diff JSON")

    diff = commands.add_parser(
        "diff",
        help="compare two historical proof-run directories without reading raw logs",
        allow_abbrev=False,
    )
    diff.add_argument("left_run_dir")
    diff.add_argument("right_run_dir")
    diff.add_argument("--json", action="store_true", help="emit machine-readable run diff JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args: Any = _parser().parse_args(argv)
    if args.command == "vanilla":
        from hol_workbench.vanilla_claims import vanilla_command

        return vanilla_command(args)
    if args.command == "claim-set":
        from hol_workbench.proof_run_claim_sets import claim_set_command

        return claim_set_command(args)
    if args.command == "diff":
        from hol_workbench.proof_run_diff import diff_command

        return diff_command(args)
    raise AssertionError(f"unhandled proof-run command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
