#!/usr/bin/env python3
"""Small public front door for bounded Workbench artifact inspection."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from hol_workbench.hashing import short_sha256

REPLAY_SCHEMA = "hol-workbench.warm-vanilla-artifact.v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inspect",
        description="Summarize a Workbench receipt or run directory without opening raw logs.",
        allow_abbrev=False,
    )
    parser.add_argument("run_dir", nargs="+")
    parser.add_argument("--verbose", action="store_true", help="show the full card-first inspect report")
    parser.add_argument("--tail", type=int, help="print the last N raw-log lines after the compact summary")
    parser.add_argument("--grep", help="regex filter for a bounded raw-log peek after the compact summary")
    parser.add_argument("--limit", type=int, default=40, help="maximum grep matches when --tail is absent")
    parser.add_argument(
        "--max-line-chars",
        type=int,
        default=240,
        help="maximum characters per raw-log line printed by --tail/--grep",
    )
    return parser


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _replay_receipt(path: Path) -> Path | None:
    if path.is_file():
        candidates = [path]
        if path.name == "transcript.log":
            candidates.insert(0, Path(f"{path}.json"))
    elif path.is_dir():
        candidates = [path / "transcript.log.json"]
        try:
            candidates.extend(child / "transcript.log.json" for child in path.iterdir() if child.is_dir())
        except OSError:
            return None
    else:
        return None
    matches = [candidate for candidate in candidates if _read_json(candidate).get("schema") == REPLAY_SCHEMA]
    if not matches:
        return None
    return max(matches, key=lambda candidate: candidate.stat().st_mtime_ns)


def _bool(value: object) -> str:
    if value is None:
        return "unknown"
    return str(bool(value)).lower()


def _binding_card_label(row: dict[str, Any]) -> str:
    probe = str(row.get("status") or "unknown")
    if probe == "missing" and row.get("unverified_binding_like_text_observed") is True:
        return "missing (unverified binding-like text observed)"
    return probe


def _last_unverified_binding(rows: list[dict[str, Any]]) -> tuple[str, int] | None:
    candidates: list[tuple[int, str]] = []
    for row in rows:
        name = row.get("name")
        lines = row.get("unverified_binding_like_transcript_lines")
        if not isinstance(name, str) or not isinstance(lines, list):
            continue
        candidates.extend((line, name) for line in lines if type(line) is int and line > 0)
    if not candidates:
        return None
    line, name = max(candidates)
    return name, line


def _recorded_source_identity(receipt: dict[str, Any]) -> str:
    short = short_sha256(receipt.get("source_sha256"))
    return f" sha={short}" if short else ""


def _bounded_transcript(args: argparse.Namespace, receipt: dict[str, Any]) -> None:
    if args.tail is None and args.grep is None:
        return
    raw_value = receipt.get("raw_transcript") or receipt.get("transcript")
    raw_path = Path(str(raw_value)).expanduser() if raw_value else None
    if raw_path is None or not raw_path.is_file():
        print("transcript peek: unavailable")
        return
    try:
        lines = raw_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        print(f"transcript peek: unavailable ({exc})")
        return
    if args.grep is not None:
        try:
            pattern = re.compile(args.grep)
        except re.error as exc:
            raise SystemExit(f"invalid --grep pattern: {exc}") from exc
        lines = [line for line in lines if pattern.search(line)]
    limit = args.tail if args.tail is not None else args.limit
    if limit < 0:
        raise SystemExit("--tail and --limit must be non-negative")
    selected = lines[-limit:] if args.tail is not None and limit else lines[:limit]
    print(f"transcript peek: {len(selected)} bounded line(s) from {raw_path}")
    for line in selected:
        print(line[: args.max_line_chars])


def _inspect_replay(args: argparse.Namespace, receipt_path: Path) -> int:
    receipt = _read_json(receipt_path)
    bindings = receipt.get("bindings")
    binding_rows = bindings if isinstance(bindings, list) else []
    semantic_succeeded = bool(
        receipt.get("semantic_source_status") == "succeeded"
        and receipt.get("source_completed")
        and receipt.get("completion_marker_valid")
        and receipt.get("claims_complete")
        and receipt.get("semantic_exit_status") == 0
    )
    source_accepted = bool(receipt.get("source_completed") and receipt.get("completion_marker_valid"))
    exit_fields = ("exit_status", "worker_exit_status", "process_exit_status")
    recorded_exit_fields = {name: receipt[name] for name in exit_fields if name in receipt}
    legacy_exit_fallback = not recorded_exit_fields
    recorded_exits_complete = len(recorded_exit_fields) == len(exit_fields) and all(
        type(recorded_exit_fields[name]) is int for name in exit_fields
    )
    recorded_exits_zero = recorded_exits_complete and all(recorded_exit_fields[name] == 0 for name in exit_fields)
    succeeded = semantic_succeeded and (legacy_exit_fallback or recorded_exits_zero)
    status = "succeeded" if succeeded else "failed" if semantic_succeeded else receipt.get("semantic_source_status")
    print(f"replay: {receipt.get('evidence') or 'unknown'}")
    print(f"status: {status or 'failed'}")
    print(f"source_acceptance: {'accepted' if source_accepted else 'not_accepted'}")
    print(f"source: {receipt.get('source') or 'unknown'}{_recorded_source_identity(receipt)}")
    print(f"profile: {receipt.get('logical_profile') or receipt.get('physical_profile') or 'unknown'}")
    print(f"transport: {receipt.get('transport') or receipt.get('transport_status') or 'unknown'}")
    print(f"source_completed: {_bool(receipt.get('source_completed'))}")
    print(f"claims_complete: {_bool(receipt.get('claims_complete'))}")
    print(f"completion_marker_valid: {_bool(receipt.get('completion_marker_valid'))}")
    if legacy_exit_fallback:
        print("exit_status_check: unavailable (legacy receipt; semantic fallback)")
    else:
        print(f"exit_status_check: {'passed' if recorded_exits_zero else 'failed'}")
        for name in exit_fields:
            print(f"{name}: {receipt.get(name, 'missing')}")
    dict_rows = [row for row in binding_rows if isinstance(row, dict)]
    print(f"bindings: {len(binding_rows)}")
    labels = [_binding_card_label(row) for row in dict_rows]
    for row, label in zip(dict_rows[:12], labels[:12], strict=True):
        print(f"  {row.get('name') or 'unnamed'}: {label}")
    if len(dict_rows) > 12:
        print(f"  ... {len(dict_rows) - 12} more")
    if receipt.get("first_failure"):
        failure_line = receipt.get("first_failure_transcript_line")
        coordinate = f"transcript_line={failure_line} " if type(failure_line) is int else ""
        print(f"first_failure: {coordinate}{receipt['first_failure']}")
    last_unverified = _last_unverified_binding(dict_rows)
    if last_unverified is not None and not source_accepted:
        name, line = last_unverified
        print(f"last_binding_like_text: {name} (unverified, transcript_line={line})")
    probe_unresolved = all(
        str(row.get("status") or "unknown") in {"missing", "unknown"} for row in dict_rows
    )
    if dict_rows and not source_accepted and probe_unresolved:
        print("binding_note: probe missing; transcript text does not identify the residual binding")
    foundation = receipt.get("foundation_delta")
    if isinstance(foundation, dict):
        deltas = foundation.get("deltas")
        if isinstance(deltas, dict):
            print(
                f"foundation_delta: status={foundation.get('status') or 'unknown'} "
                f"axioms={deltas.get('axioms')} definitions={deltas.get('definitions')} "
                f"types={deltas.get('types')} constants={deltas.get('constants')}"
            )
        else:
            print(f"foundation_delta: {foundation.get('status') or 'unavailable'}")
    else:
        print("foundation_delta: unavailable (not recorded)")
    advisory = receipt.get("foundation_advisory") or receipt.get("promotion_advisory")
    if isinstance(advisory, dict):
        print(
            f"foundation_advisory: {advisory.get('recommendation') or 'manual_review'} "
            "(not publication authority)"
        )
        for reason in advisory.get("reasons") or []:
            print(f"  advisory_reason: {reason}")
    else:
        print("foundation_advisory: unavailable (not recorded)")
    cleanup = receipt.get("cleanup")
    if isinstance(cleanup, dict):
        print(f"cleanup: {cleanup.get('status') or 'unknown'}")
        print(f"verified_quiescent: {_bool(cleanup.get('verified_quiescent'))}")
    else:
        print("cleanup: not recorded (published warm seat may remain)")
    print(f"receipt: {receipt_path}")
    print(
        f"check_scope: {receipt.get('evidence_boundary') or 'exact source completion and named theorem binding checks'}"
    )
    if args.verbose:
        print(f"source_sha256: {receipt.get('source_sha256') or 'unknown'}")
        print(f"executed_source_sha256: {receipt.get('executed_source_sha256') or 'unknown'}")
        print(f"profile_sha256: {receipt.get('profile_sha256') or 'unknown'}")
        print(f"source_dependency_closure_sha256: {receipt.get('source_dependency_closure_sha256') or 'unknown'}")
        print(f"transcript: {receipt.get('transcript') or 'unavailable'}")
        print(f"raw_transcript: {receipt.get('raw_transcript') or 'unavailable'}")
    _bounded_transcript(args, receipt)
    return 0 if succeeded else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    current = [receipt for value in args.run_dir if (receipt := _replay_receipt(Path(value).resolve()))]
    if current:
        return _inspect_replay(args, max(current, key=lambda path: path.stat().st_mtime_ns))
    print(
        "inspect: no recorded warm-replay receipt found; expected transcript.log.json, "
        "its containing replay directory, or its run root",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
