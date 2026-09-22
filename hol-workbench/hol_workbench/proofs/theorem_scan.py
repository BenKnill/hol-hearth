"""Static extraction of named HOL theorem statements."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hol_workbench.hashing import (
    sha256_bytes,
    sha256_text,
)
from hol_workbench.proofs.loader_scan import scan_claim_binding_starts
from hol_workbench.hashing import (
    sha256_file_strict as sha256,
)
from hol_workbench.proofs.source import (
    DIRECT_THEOREM_RE,
    THEOREM_RE,
    mask_hol_backquote_interiors,
    mask_ocaml_comments_and_strings,
    read_first_prove_argument_preview,
    read_hol_backquote,
)

SCHEMA_VERSION = 1


def normalize_hol_statement(statement: str) -> str:
    lines = [line.strip() for line in statement.strip().splitlines()]
    collapsed = []
    blank = False
    for line in lines:
        if not line:
            if not blank and collapsed:
                collapsed.append("")
            blank = True
            continue
        collapsed.append(line)
        blank = False
    return "\n".join(collapsed).strip()


def truncate_text(text: str | None, *, max_chars: int = 1600, max_lines: int = 28) -> str | None:
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


def extract_hol_theorems(source: Path) -> list[dict[str, Any]]:
    text = source.read_text(encoding="utf-8", errors="replace")
    source_hash = sha256(source)
    return extract_hol_theorems_text(source, text, source_hash=source_hash)


def extract_hol_theorems_bytes(source: Path, source_bytes: bytes) -> list[dict[str, Any]]:
    """Extract from the exact supplied bytes without rereading the source."""

    text = source_bytes.decode("utf-8", errors="strict")
    return extract_hol_theorems_text(source, text, source_hash=sha256_bytes(source_bytes))


def extract_hol_theorems_text(source: Path, text: str, *, source_hash: str | None) -> list[dict[str, Any]]:
    """Shared current scanner over caller-owned text and source identity."""

    binding_starts = scan_claim_binding_starts(text.encode("utf-8"))
    masked = mask_ocaml_comments_and_strings(text)
    search_masked = mask_hol_backquote_interiors(masked)
    claims: list[dict[str, Any]] = []
    matches = [("prove", match.start(), match) for match in THEOREM_RE.finditer(search_masked)]
    matches.extend(("direct_rule", match.start(), match) for match in DIRECT_THEOREM_RE.finditer(search_masked))
    byte_offset = character_offset = 0
    for kind, start, match in sorted(matches, key=lambda item: item[1]):
        byte_offset += len(text[character_offset:start].encode("utf-8"))
        character_offset = start
        if byte_offset not in binding_starts:
            continue
        qpos = match.end() if kind == "prove" else match.end() - 1
        while qpos < len(search_masked) and search_masked[qpos].isspace():
            qpos += 1
        if qpos >= len(search_masked) or search_masked[qpos] != "`":
            arg_preview = read_first_prove_argument_preview(text, masked, qpos)
            claims.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "theorem",
                    "name": match.group(1),
                    "statement": None,
                    "statement_preview": None,
                    "statement_extractable": False,
                    "statement_source": "nonliteral_prove_argument",
                    "proof_constructor": "prove",
                    "prove_argument_preview": arg_preview,
                    "source": str(source),
                    "source_line": text.count("\n", 0, match.start()) + 1,
                    "statement_line": None,
                    "statement_end_line": None,
                    "source_sha256": source_hash,
                    "statement_truncated": None,
                }
            )
            continue
        quoted = read_hol_backquote(text, qpos)
        if quoted is None:
            continue
        statement, end = quoted
        statement_quote = text[qpos:end]
        normalized = normalize_hol_statement(statement)
        claims.append(
            {
                "schema_version": SCHEMA_VERSION,
                "kind": "theorem",
                "name": match.group(1),
                "statement": normalized,
                "statement_quote": statement_quote,
                "statement_quote_sha256": sha256_text(statement_quote),
                "statement_preview": truncate_text(normalized),
                "statement_extractable": True,
                "statement_source": "literal_backquote" if kind == "prove" else "direct_rule_backquote",
                "proof_constructor": "prove" if kind == "prove" else match.group(2),
                "source": str(source),
                "source_line": text.count("\n", 0, match.start()) + 1,
                "statement_line": text.count("\n", 0, qpos) + 1,
                "statement_end_line": text.count("\n", 0, end) + 1,
                "source_sha256": source_hash,
                "statement_truncated": truncate_text(normalized) != normalized,
            }
        )
    return claims
