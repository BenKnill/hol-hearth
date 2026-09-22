"""Normalize transcript lines for nonce probes and proof budget diagnostics."""


from __future__ import annotations


import re


OCAML_PROMPT_RE = re.compile(r"^#\s*")


ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_ansi(line: str) -> str:
    return ANSI_RE.sub("", line)


def semantic_line(line: str) -> str:
    return OCAML_PROMPT_RE.sub("", line.strip())
