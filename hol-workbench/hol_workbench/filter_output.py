"""Pure helpers for classifying and bounding HOL output lines."""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from hol_workbench.filter_output_events import TACTIC_EXCEPTION_PREFIX
from hol_workbench.hashing import sha256_text
from hol_workbench.jsonio import read_json_strict

NOISE_PATTERNS = [
    ("search-limit", re.compile(r"^Searching with limit \d+$")),
    ("typevar-warning", re.compile(r"^Warning: inventing type variables$")),
    ("benign-redefinition", re.compile(r"^Warning: Benign redefinition$")),
    ("cpu-time", re.compile(r"^CPU time \(user\): ")),
    ("search-progress", re.compile(r"^(?:0\.\.)+.*solved at \d+$")),
    ("gbasis", re.compile(r"^\d+ basis elements and \d+ critical pairs$")),
    ("certificate", re.compile(r"^Translating certificate to HOL inferences$")),
    ("proof-generation", re.compile(r"^Generating HOL version of proof$")),
    ("small-prime-proof", re.compile(r"^proving that \d+ is (?:prime|composite)$")),
    ("symbolic-step", re.compile(r"^Stepping to state s\d+$")),
    ("bdd", re.compile(r"^BDD with \d+ variables, \d+ nodes and \d+ cached results$")),
    ("bdd-definitions", re.compile(r"^BDD with \d+ definitions, \d+ variables, \d+ nodes and \d+ cached results$")),
]

IMPORTANT_PATTERNS = [
    re.compile(r"^(Fatal error|Exception|Error|Failure|.*[Ee]xception.*|.*[Ff]ailed.*|.*[Ee]rror.*)$"),
    re.compile(rf"^{re.escape(TACTIC_EXCEPTION_PREFIX)}"),
    re.compile(r"^val [A-Za-z0-9_']+\s*(?::|.*:)\s*thm\s*="),
    re.compile(r"^proved [A-Za-z0-9_']+$"),
    re.compile(r"^__HOL_CLAIM_OBSERVED__:[A-Za-z0-9_']+$"),
    re.compile(r"^__HOL_CLAIM_MISMATCH__:[A-Za-z0-9_']+$"),
    re.compile(r"^Running time: "),
]

OCAML_PROMPT_RE = re.compile(r"^#\s*")
OCAML_EXCEPTION_DECLARATION_RE = re.compile(r"^exception\s+[A-Z][A-Za-z0-9_']*(?:\s+(?:of\s+.+|=\s*.+))?$")
TIMEOUT_STATUSES = {124, 137}
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def truncate_multiline(text: str | None, *, max_chars: int = 1600, max_lines: int = 28) -> str | None:
    if text is None:
        return None
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars].rstrip()
        truncated = True
    if truncated:
        out += "\n... [truncated]"
    return out


def compact_goal_snapshot(snapshot: dict | None, *, max_chars: int = 1600, max_lines: int = 28) -> dict | None:
    if not snapshot:
        return None
    compact = {
        "kind": snapshot.get("kind"),
        "recovery": snapshot.get("recovery"),
        "raw_log_start": snapshot.get("raw_log_start"),
        "raw_log_end": snapshot.get("raw_log_end"),
        "line_count": snapshot.get("line_count"),
        "truncated": snapshot.get("truncated"),
        "frontier": snapshot.get("frontier"),
        "authoring_frontier_diagnostic": snapshot.get("authoring_frontier_diagnostic"),
    }
    text = truncate_multiline(snapshot.get("text"), max_chars=max_chars, max_lines=max_lines)
    if text:
        compact["text"] = text
    return {key: value for key, value in compact.items() if value is not None}


def source_label(source: str | None, line: int | None) -> str | None:
    if source and line:
        return f"{source}:{line}"
    return source


def load_preflight_claim(path: str | None) -> dict:
    if not path:
        return {}
    claim_path = Path(path).resolve()
    data = read_json_strict(claim_path)
    claim = data.get("claim") or {}
    return {
        "claim_json": str(claim_path),
        "theorem": claim.get("name"),
        "statement": claim.get("statement"),
        "source": source_label(claim.get("source"), claim.get("statement_line") or claim.get("source_line")),
        "source_sha256": claim.get("source_sha256"),
        "statement_line": claim.get("statement_line"),
        "statement_end_line": claim.get("statement_end_line"),
    }


def build_claim(args: argparse.Namespace) -> dict | None:
    preflight = load_preflight_claim(args.claim_json)
    statement = args.claim_statement if args.claim_statement is not None else preflight.get("statement")
    preview = truncate_multiline(statement)
    if not (
        args.claim_theorem
        or args.claim_scope
        or statement
        or args.claim_source
        or args.claim_source_sha256
        or args.non_claim
        or preflight
    ):
        return None
    claim = {
        "theorem": args.claim_theorem or preflight.get("theorem"),
        "scope": args.claim_scope,
        "statement_preview": preview,
        "statement_truncated": preview != statement if statement is not None else None,
        "statement_sha256": sha256_text(statement),
        "statement_line": preflight.get("statement_line"),
        "statement_end_line": preflight.get("statement_end_line"),
        "source": args.claim_source or preflight.get("source"),
        "source_sha256": args.claim_source_sha256 or preflight.get("source_sha256"),
        "claim_json": preflight.get("claim_json"),
        "non_claims": args.non_claim,
    }
    return {key: value for key, value in claim.items() if value is not None}


def verify_claim_observed(
    claim: dict | None,
    completed_bindings: list[str],
    claim_observations: list[dict],
    *,
    mode: str,
    evidence_mode: str,
    child_exit_status: int | None,
    final_exit_status: int,
) -> dict | None:
    if not claim or not claim.get("theorem"):
        return None
    expected = str(claim["theorem"])
    observed_exact = expected in completed_bindings
    observed_casefold = expected.lower() in {item.lower() for item in completed_bindings}
    expected_observations = [item for item in claim_observations if item.get("name") == expected]
    semantic_observed = any(
        item.get("source") == "semantic_probe" and item.get("status") == "observed" for item in expected_observations
    )
    semantic_mismatch = any(
        item.get("source") == "semantic_probe" and item.get("status") == "mismatch" for item in expected_observations
    )
    output_name_observed = any(
        item.get("source") in ("theorem_output", "proved_output") and item.get("status") == "observed"
        for item in expected_observations
    )
    if evidence_mode == "semantic-probe":
        if semantic_observed:
            status = "observed"
        elif semantic_mismatch:
            status = "claim_mismatch"
        elif observed_exact or observed_casefold or output_name_observed:
            status = "semantic_missing_output_name_only"
        else:
            status = "missing"
    elif semantic_mismatch:
        status = "claim_mismatch"
    elif observed_exact:
        status = "observed"
    elif observed_casefold:
        status = "case_mismatch"
    elif mode == "exit-success" and child_exit_status == 0 and final_exit_status == 0:
        status = "accepted_child_success"
    elif mode == "exit-success" and child_exit_status == 0:
        status = "rejected_filter_failed"
    elif mode == "off":
        status = "not_required"
    else:
        status = "missing"
    if semantic_observed:
        evidence_class = "semantic_probe"
        semantic_verification = {
            "status": "observed",
            "mode": evidence_mode,
            "reason": "generated semantic probe observed the expected theorem and statement marker",
        }
    elif semantic_mismatch:
        evidence_class = "semantic_probe_mismatch"
        semantic_verification = {
            "status": "mismatch",
            "mode": evidence_mode,
            "reason": "generated semantic probe reported a theorem/statement mismatch",
        }
    elif output_name_observed or status == "semantic_missing_output_name_only":
        evidence_class = "observed_output_name"
        semantic_verification = {
            "status": "missing" if status == "semantic_missing_output_name_only" else "unavailable",
            "mode": evidence_mode,
            "reason": (
                "semantic-probe mode requires the generated claim marker, but only theorem-name output was observed"
                if status == "semantic_missing_output_name_only"
                else "the expected theorem name was seen in output, but no semantic probe marker was observed"
            ),
        }
    elif status == "accepted_child_success":
        evidence_class = "child_exit_only"
        semantic_verification = {
            "status": "unavailable",
            "mode": evidence_mode,
            "reason": "claim verification accepted child exit success without observing a theorem statement",
        }
    elif mode == "off":
        evidence_class = "not_required"
        semantic_verification = {
            "status": "not_required",
            "mode": evidence_mode,
            "reason": "claim verification was disabled",
        }
    else:
        evidence_class = "none"
        semantic_verification = {
            "status": "missing",
            "mode": evidence_mode,
            "reason": "no semantic probe marker was observed",
        }
    return {
        "expected": expected,
        "status": status,
        "mode": mode,
        "evidence_mode": evidence_mode,
        "evidence_class": evidence_class,
        "semantic_verification": semantic_verification,
        "observation_sources": sorted(
            {source for item in expected_observations if isinstance((source := item.get("source")), str) and source}
        ),
        "observations_tail": expected_observations[-20:],
        "observed_exact": observed_exact,
        "observed_casefold": observed_casefold,
        "final_exit_status": final_exit_status,
        "child_exit_status": child_exit_status,
        "completed_bindings_tail": completed_bindings[-20:],
    }


def strip_ansi(line: str) -> str:
    return ANSI_RE.sub("", line)


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def parse_expected_count(value: str) -> tuple[str, int] | None:
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"expected KEY=N, got {value!r}")
    key, raw_count = value.split("=", 1)
    try:
        count = int(raw_count)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected integer count in {value!r}") from exc
    if count <= 0:
        return None
    return key, count


def parse_expected_counts(values: list[str] | None) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values or []:
        parsed = parse_expected_count(value)
        if parsed is not None:
            key, count = parsed
            result[key] = count
    return result


def validate_nonnegative(parser: argparse.ArgumentParser, args: argparse.Namespace, name: str) -> None:
    if getattr(args, name) < 0:
        parser.error(f"--{name.replace('_', '-')} must be non-negative")


def validate_exit_status(parser: argparse.ArgumentParser, args: argparse.Namespace, name: str) -> None:
    value = getattr(args, name)
    if value < 1 or value > 255:
        parser.error(f"--{name.replace('_', '-')} must be in 1..255")


def semantic_line(line: str) -> str:
    return OCAML_PROMPT_RE.sub("", line.strip())


def is_source_echo_failure_line(stripped: str) -> bool:
    return bool(
        OCAML_EXCEPTION_DECLARATION_RE.fullmatch(stripped)
        or re.match(r"^(?:\|\s*)?with\s+(?:Failure|Error|Exception)\b.*->", stripped)
        or re.match(r"^\|\s*(?:Failure|Error|Exception)\b.*->", stripped)
        or re.match(r"^\d+\s*\|\s*(?:with\s+)?(?:Failure|Error|Exception)\b.*->", stripped)
    )


def is_ocaml_exception_declaration_line(stripped: str) -> bool:
    """Return whether a top-level line declares, rather than raises, an OCaml exception."""
    return bool(OCAML_EXCEPTION_DECLARATION_RE.fullmatch(stripped))


def classify(line: str) -> str | None:
    stripped = semantic_line(line)
    for name, pattern in NOISE_PATTERNS:
        if pattern.match(stripped):
            return name
    return None


def is_important(line: str) -> bool:
    stripped = semantic_line(line)
    if is_source_echo_failure_line(stripped) or is_ocaml_exception_declaration_line(stripped):
        return False
    return any(pattern.match(stripped) for pattern in IMPORTANT_PATTERNS)


def truncate_line(line: str, limit: int) -> str:
    if len(line) <= limit:
        return line
    omitted = len(line) - limit
    return line[:limit] + f"... [truncated {omitted} chars]"


def format_duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    if seconds < 0.5:
        return "<1s"
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def parse_progress_marker(payload: str, max_chars: int) -> dict[str, Any]:
    event: dict[str, Any] = {"kind": "progress", "raw": truncate_line(payload, max_chars)}
    fields: dict[str, str] = {}
    for part in payload.split():
        if "=" in part:
            key, value = part.split("=", 1)
            fields[key] = truncate_line(value, max_chars)
    if fields:
        event["fields"] = fields
        if "phase" in fields:
            event["phase"] = fields["phase"]
        if "theorem" in fields:
            event["theorem"] = fields["theorem"]
    return event


def progress_snapshot(progress: dict | None, counts: Mapping[str, int], elapsed_seconds: float) -> dict:
    snapshot = {
        "phase": "running",
        "percent": None,
        "eta_seconds": None,
        "rate": None,
        "rate_unit": None,
    }
    if not progress:
        return snapshot

    profiled_total = progress.get("expected_noise_total") or 0
    profiled_observed = 0
    profiled_percent = None
    if profiled_total:
        profiled_observed = sum(
            min(counts.get(key, 0), expected) for key, expected in progress["expected_noise_counts"].items()
        )
        profiled_percent = int((profiled_observed / profiled_total) * 100)

    symbolic_total = progress.get("symbolic_total") or 0
    symbolic_current = 0
    symbolic_percent = None
    if symbolic_total:
        symbolic_current = min(progress.get("symbolic_current", 0), symbolic_total)
        symbolic_percent = int((symbolic_current / symbolic_total) * 100)

    if symbolic_total and symbolic_current > 0:
        snapshot["phase"] = "symbolic"
        snapshot["percent"] = max(value for value in (symbolic_percent, profiled_percent) if value is not None)
        done = symbolic_current
        total = symbolic_total
        unit = "steps/s"
    elif profiled_total:
        snapshot["phase"] = "proof-search"
        snapshot["percent"] = profiled_percent
        done = profiled_observed
        total = profiled_total
        unit = "noise/s"
    elif symbolic_total:
        snapshot["phase"] = "symbolic-wait"
        snapshot["percent"] = symbolic_percent
        done = symbolic_current
        total = symbolic_total
        unit = "steps/s"
    else:
        return snapshot

    progress_fraction = done / total if total else 0
    if done > 0 and elapsed_seconds >= 1.0 and progress_fraction >= 0.05:
        rate = done / elapsed_seconds
        snapshot["rate"] = round(rate, 2)
        snapshot["rate_unit"] = unit
        if total > done:
            snapshot["eta_seconds"] = elapsed_seconds * (total - done) / done
    return snapshot


def final_phase(exit_status: int, *, interrupted_signal: int | None = None) -> str:
    if interrupted_signal is not None:
        return "interrupted"
    if exit_status == 0:
        return "completed"
    if exit_status in TIMEOUT_STATUSES:
        return "timeout"
    return "failed"


def final_progress_snapshot(snapshot: dict, exit_status: int, *, interrupted_signal: int | None = None) -> dict:
    final = dict(snapshot)
    phase = final_phase(exit_status, interrupted_signal=interrupted_signal)
    final["phase"] = phase
    if phase == "completed":
        final["percent"] = 100
        final["eta_seconds"] = 0
    else:
        final["eta_seconds"] = None
    return final


def progress_percent(progress: dict | None, counts: Mapping[str, int], elapsed_seconds: float = 0.0) -> int | None:
    if not progress:
        return None
    return progress_snapshot(progress, counts, elapsed_seconds).get("percent")


def format_status(counts: Mapping[str, int], progress: dict | None = None, elapsed_seconds: float = 0.0) -> str:
    total = sum(counts.values())
    top_items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    parts = ", ".join(f"{key} {value}" for key, value in top_items)
    if len(counts) > len(top_items):
        parts += f", +{len(counts) - len(top_items)} classes"
    progress_parts = []
    if progress and progress.get("expected_noise_total"):
        observed = sum(min(counts.get(key, 0), expected) for key, expected in progress["expected_noise_counts"].items())
        percent = int((observed / progress["expected_noise_total"]) * 100)
        progress_parts.append(f"profiled {observed}/{progress['expected_noise_total']} ({percent}%)")
    if progress and progress.get("symbolic_total"):
        current = min(progress.get("symbolic_current", 0), progress["symbolic_total"])
        percent = int((current / progress["symbolic_total"]) * 100)
        progress_parts.append(f"symbolic {current}/{progress['symbolic_total']} ({percent}%)")
    snapshot = progress_snapshot(progress, counts, elapsed_seconds)
    if snapshot.get("phase") != "running":
        progress_parts.insert(0, f"phase {snapshot['phase']}")
    eta = format_duration(snapshot.get("eta_seconds"))
    if eta:
        eta_prefix = "" if eta.startswith("<") else "~"
        progress_parts.append(f"ETA {eta_prefix}{eta}")
    rate = snapshot.get("rate")
    if rate is not None:
        progress_parts.append(f"{rate:g} {snapshot.get('rate_unit')}")
    progress_text = "; ".join(progress_parts)
    if progress_text:
        return f"[filtered] progress: {progress_text}; suppressed {total} (top {parts})"
    return f"[filtered] suppressed {total} (top {parts})"
