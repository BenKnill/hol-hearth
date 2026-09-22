"""Decode diagnostic binding and load events; these are not theorem probes."""


from __future__ import annotations


import re


VAL_BINDING_RE = re.compile(r"^val ([A-Za-z_][A-Za-z0-9_']*)\b")


ANONYMOUS_VALUE_RE = re.compile(r"^-\s*:")


ANONYMOUS_TOP_LEVEL = "__anonymous_top_level__"


LITERAL_LOAD_STARTED_RE = re.compile(r"^__PROOF_RUN_LITERAL_LOAD_STARTED__:(needs|loadt|loads):(.+)$")


LITERAL_LOAD_COMPLETED_RE = re.compile(r"^__PROOF_RUN_LITERAL_LOAD_COMPLETED__:(needs|loadt|loads):(.+)$")


def val_binding_name(stripped_line: str) -> str | None:
    """Return the name announced by an OCaml top-level ``val`` result."""

    match = VAL_BINDING_RE.match(stripped_line)
    if match:
        return match.group(1)
    return ANONYMOUS_TOP_LEVEL if ANONYMOUS_VALUE_RE.match(stripped_line) else None


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
