from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

THEOREM_LINE_RE = re.compile(r"^val ([A-Za-z0-9_']+)\s*(?::|.*:)\s*thm\s*=")
VAL_BINDING_RE = re.compile(r"^val ([A-Za-z_][A-Za-z0-9_']*)\b")
ANONYMOUS_VALUE_RE = re.compile(r"^-\s*:")
ANONYMOUS_TOP_LEVEL = "__anonymous_top_level__"
PROVED_LINE_RE = re.compile(r"^proved ([A-Za-z0-9_']+)$")
PROBE_OBSERVED_RE = re.compile(r"^__HOL_CLAIM_OBSERVED__:([A-Za-z0-9_']+)$")
PROBE_MISMATCH_RE = re.compile(r"__HOL_CLAIM_MISMATCH__:([A-Za-z0-9_']+)")
LITERAL_LOAD_STARTED_RE = re.compile(r"^__PROOF_RUN_LITERAL_LOAD_STARTED__:(needs|loadt|loads):(.+)$")
LITERAL_LOAD_COMPLETED_RE = re.compile(r"^__PROOF_RUN_LITERAL_LOAD_COMPLETED__:(needs|loadt|loads):(.+)$")
PRELUDE_COMPLETED_MARKER = "__PROOF_RUN_PRELUDE_COMPLETED__"
SOURCE_LOAD_COMPLETED_MARKER = "__PROOF_RUN_SOURCE_LOAD_COMPLETED__"
TACTIC_EXCEPTION_PREFIX = "__HOL_TACTIC_EXCEPTION__:"
TACTIC_EXCEPTION_SEQUENCE_PREFIX = "__HOL_TACTIC_EXCEPTION_SEQUENCE__:"
TACTIC_GOAL_TRANSITION_PREFIX = "__HOL_TACTIC_GOAL_TRANSITION__:"
SOURCE_LOAD_FAILED_PREFIX = "__PROOF_RUN_SOURCE_LOAD_FAILED__:"


@dataclass(frozen=True)
class ImportantObservation:
    completed_binding: str | None
    claim_observation: dict | None
    event: dict | None


@dataclass
class BindingCompletionTracker:
    """Exclude the OCaml unit result produced by harness marker phrases."""

    suppress_next_harness_unit: bool = False

    def record(self, stripped_line: str, record_event: Callable[[dict[str, Any]], Any]) -> str:
        if self.suppress_next_harness_unit and stripped_line:
            self.suppress_next_harness_unit = False
            if val_binding_name(stripped_line) == ANONYMOUS_TOP_LEVEL:
                return stripped_line
        semantic = record_val_binding_completion(stripped_line, record_event)
        if stripped_line in {PRELUDE_COMPLETED_MARKER, SOURCE_LOAD_COMPLETED_MARKER}:
            self.suppress_next_harness_unit = True
        return semantic


def val_binding_name(stripped_line: str) -> str | None:
    """Return the name announced by an OCaml top-level ``val`` result."""

    match = VAL_BINDING_RE.match(stripped_line)
    if match:
        return match.group(1)
    return ANONYMOUS_TOP_LEVEL if ANONYMOUS_VALUE_RE.match(stripped_line) else None


def record_val_binding_completion(stripped_line: str, record_event: Callable[[dict[str, Any]], Any]) -> str:
    """Persist every top-level ``val`` result without expanding the live important-line stream."""

    started_load = literal_load_started_event(stripped_line)
    if started_load is not None:
        record_event(started_load)
        return ""
    completed_load = literal_load_completed_event(stripped_line)
    if completed_load is not None:
        record_event(completed_load)
        return ""
    if stripped_line == PRELUDE_COMPLETED_MARKER:
        record_event({"kind": "prelude-completed"})
        return ""
    if stripped_line == SOURCE_LOAD_COMPLETED_MARKER:
        record_event({"kind": "source-load-completed"})
        return ""
    name = val_binding_name(stripped_line)
    if name:
        record_event({"kind": "binding-completed", "name": name})
    return stripped_line


def literal_load_started_event(stripped_line: str) -> dict[str, str] | None:
    """Decode the flushed harness marker emitted before a literal loader runs."""

    match = LITERAL_LOAD_STARTED_RE.match(stripped_line)
    if not match:
        return None
    return {
        "kind": "literal-load-started",
        "loader": match.group(1),
        "declared_path": match.group(2),
    }


def literal_load_completed_event(stripped_line: str) -> dict[str, str] | None:
    """Decode the harness marker emitted only after a literal loader returns."""

    match = LITERAL_LOAD_COMPLETED_RE.match(stripped_line)
    if not match:
        return None
    return {
        "kind": "literal-load-completed",
        "loader": match.group(1),
        "declared_path": match.group(2),
    }


def important_observation(stripped_line: str, stored_line: str, raw_log_line: int) -> ImportantObservation:
    probe_match = PROBE_OBSERVED_RE.match(stripped_line)
    if probe_match:
        name = probe_match.group(1)
        return ImportantObservation(
            completed_binding=name,
            claim_observation={
                "name": name,
                "source": "semantic_probe",
                "status": "observed",
                "line": stored_line,
                "raw_log_line": raw_log_line,
            },
            event={
                "kind": "claim-checked",
                "name": name,
                "status": "observed",
                "line": stored_line,
            },
        )

    probe_mismatch = PROBE_MISMATCH_RE.search(stripped_line)
    if probe_mismatch:
        name = probe_mismatch.group(1)
        return ImportantObservation(
            completed_binding=None,
            claim_observation={
                "name": name,
                "source": "semantic_probe",
                "status": "mismatch",
                "line": stored_line,
                "raw_log_line": raw_log_line,
            },
            event={
                "kind": "claim-checked",
                "name": name,
                "status": "mismatch",
                "line": stored_line,
            },
        )

    theorem_match = THEOREM_LINE_RE.match(stripped_line)
    if theorem_match:
        name = theorem_match.group(1)
        return ImportantObservation(
            completed_binding=name,
            claim_observation={
                "name": name,
                "source": "theorem_output",
                "status": "observed",
                "line": stored_line,
                "raw_log_line": raw_log_line,
            },
            event={"kind": "theorem", "name": name, "line": stored_line},
        )

    proved_match = PROVED_LINE_RE.match(stripped_line)
    if proved_match:
        name = proved_match.group(1)
        return ImportantObservation(
            completed_binding=name,
            claim_observation={
                "name": name,
                "source": "proved_output",
                "status": "observed",
                "line": stored_line,
                "raw_log_line": raw_log_line,
            },
            event={"kind": "proved", "name": name, "line": stored_line},
        )

    return ImportantObservation(completed_binding=None, claim_observation=None, event=None)
