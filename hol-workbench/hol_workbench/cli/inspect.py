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
from hol_workbench.proof_diagnostics import print_proof_diagnostics

REPLAY_SCHEMA = "hol-workbench.warm-vanilla-artifact.v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="inspect",
        description="Summarize a Workbench receipt or run directory without opening raw logs.",
        allow_abbrev=False,
    )
    parser.add_argument("run_dir", nargs="+")
    parser.add_argument("--json", action="store_true", help="print the complete recorded receipt")
    parser.add_argument("--binding", action="append", default=[], help="show an exact theorem binding (repeatable)")
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
            children = [child for child in path.iterdir() if child.is_dir()]
            candidates.extend(child / "transcript.log.json" for child in children)
            # A caller-selected root may contain watch sessions, each with attempts.
            candidates.extend(
                attempt / "transcript.log.json"
                for child in children for attempt in child.iterdir() if attempt.is_dir()
            )
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
    if probe == "printed_unprobed":
        return "printed_unprobed (diagnostic only; no verified kernel probe)"
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



def _failure_details(receipt: dict[str, Any]) -> None:
    if not receipt.get("first_failure"):
        return
    value = receipt.get("raw_transcript") or receipt.get("transcript")
    try:
        lines = Path(str(value)).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    recorded_line = receipt.get("first_failure_transcript_line")
    if type(recorded_line) is int and 0 < recorded_line <= len(lines):
        index = recorded_line - 1
    else:
        needle = str(receipt["first_failure"]).strip().removeprefix("# ").strip()
        index = next((i for i, line in enumerate(lines) if needle and needle in line), None)
    if index is None:
        return
    print("failure_details: diagnostic transcript; generated-file positions are not editable-source coordinates")
    for line in lines[index:index + 14]:
        if line.startswith(("val ", "HOL_WORKBENCH_", "__HOL_", "# val ")):
            break
        print("  " + line[:800])


def _input_details(receipt: dict[str, Any], *, verbose: bool) -> None:
    closure = receipt.get("source_dependency_closure") or {}
    entry = closure.get("entrypoint") or {}
    rows = {entry.get("path"): entry.get("sha256")} if entry else {}
    for row in closure.get("records") or []:
        if row.get("resolved_path"):
            rows[row["resolved_path"]] = row.get("sha256")
    if rows:
        digest = short_sha256(receipt.get("source_dependency_closure_sha256")) or "unknown"
        print(f"inputs: {len(rows)} files; closure_sha={digest}")
        print("binding_scope: discovered entrypoint claims; imported declarations are not individually probed")
    if verbose:
        for path, digest in rows.items():
            print(f"  input: {path} sha256={digest or 'unavailable'}")


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
    selected_names = set(args.binding)
    selected = [row for row in binding_rows if isinstance(row, dict) and row.get("name") in selected_names]
    selection_ok = not selected_names or (
        {row.get("name") for row in selected} == selected_names and all(row.get("status") == "proved" for row in selected)
    )
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0 if succeeded and selection_ok else 1
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
    # Keep this lightweight public front door independent of evaluator imports.
    counts = dict.fromkeys(("proved", "failed", "printed_unprobed", "missing", "unknown"), 0)
    for row in dict_rows:
        binding_status = str(row.get("status") or "unknown")
        counts[binding_status] = counts.get(binding_status, 0) + 1
    print("binding_counts: " + " ".join(f"{name}={count}" for name, count in counts.items()))
    display_rows = selected if selected_names else sorted(dict_rows, key=lambda row: row.get("status") == "proved")
    visible = display_rows if args.verbose or selected_names else display_rows[:12]
    accounting = (receipt.get("transcript_accounting") or {}).get("claim_accounting") or []
    claims = {row.get("theorem"): row for row in accounting if isinstance(row, dict)}
    for row in visible:
        name = row.get("name") or "unnamed"
        claim = claims.get(name) or {}
        span = claim.get("source_span")
        location = f" source_line={span[0]}" if isinstance(span, list) and span else ""
        print(f"  {name}: {_binding_card_label(row)}{location}")
        if row.get("status") == "printed_unprobed":
            printed = row.get("printed_output") or []
            if isinstance(printed, list):
                for item in printed[-1:]:
                    if isinstance(item, dict) and item.get("printed_conclusion"):
                        print("    printed_conclusion (unverified): " +
                              " ".join(str(item["printed_conclusion"]).split())[:1600])
        if selected_names and claim.get("statement"):
            print("    source_statement: " + " ".join(str(claim["statement"]).split())[:1600])
            print("    statement_scope: source quotation; status is from the named kernel probe")
    if len(display_rows) > len(visible):
        hidden = display_rows[len(visible):]
        hidden_counts: dict[str, int] = {}
        for row in hidden:
            hidden_status = str(row.get("status") or "unknown")
            hidden_counts[hidden_status] = hidden_counts.get(hidden_status, 0) + 1
        summary = ", ".join(f"{count} {name}" for name, count in hidden_counts.items())
        print(f"  ... {len(hidden)} more ({summary}); use --verbose or --binding NAME")
    for name in sorted(selected_names - {row.get("name") for row in selected}):
        print(f"  {name}: not recorded (not a claim of theorem absence)")
    _input_details(receipt, verbose=args.verbose)
    if receipt.get("first_failure"):
        failure_line = receipt.get("first_failure_transcript_line")
        coordinate = f"transcript_line={failure_line} " if type(failure_line) is int else ""
        print(f"first_failure: {coordinate}{receipt['first_failure']}")
    if not source_accepted:
        running = receipt.get("running_binding") or (receipt.get("transcript_accounting") or {}).get("running_binding")
        if isinstance(running, dict):
            if running.get("status") == "running_at_interruption" and running.get("name"):
                print(f"running_at_interruption: {running['name']} source={running.get('source')}:{running.get('source_line')} (diagnostic only)")
            else:
                print("running_at_interruption: unknown (" + str(running.get("reason") or "no reliable location recorded") + ")")
        attribution = receipt.get("failing_binding") or (receipt.get("transcript_accounting") or {}).get("failing_binding")
        if isinstance(attribution, dict) and attribution.get("status") == "identified" and attribution.get("name"):
            print(f"failing_binding: {attribution['name']} source={attribution.get('source')}:{attribution.get('source_line')}")
        else:
            reason = attribution.get("reason") if isinstance(attribution, dict) else None
            print("failing_binding: unknown" + (f" ({reason})" if reason else " (no reliable location recorded)"))
    last_unverified = _last_unverified_binding(dict_rows)
    if last_unverified is not None and not source_accepted:
        name, line = last_unverified
        print(f"last_binding_like_text: {name} (unverified, transcript_line={line})")
    probe_unresolved = all(
        str(row.get("status") or "unknown") in {"missing", "unknown", "printed_unprobed"} for row in dict_rows
    )
    if dict_rows and not source_accepted and probe_unresolved:
        print("binding_note: named theorem probes are missing; unverified printed theorem text "
              "does not establish which binding failed")
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
    _failure_details(receipt)
    print_proof_diagnostics(receipt, verbose=args.verbose)
    _bounded_transcript(args, receipt)
    return 0 if succeeded and selection_ok else 1


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
