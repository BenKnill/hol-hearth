"""HOL and OCaml source parsing primitives."""

import re
from pathlib import Path

from hol_workbench.hashing import sha256_file
from hol_workbench.proofs.loader_scan import LoaderScanResult, scan_ocaml_loaders


def _truncate_text(text: str | None, *, max_chars: int = 1600, max_lines: int = 28) -> str | None:
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


def read_hol_backquote(text: str, start: int) -> tuple[str, int] | None:
    if start >= len(text) or text[start] != "`":
        return None
    end = start + 1
    while end < len(text):
        if text[end] == "`":
            return text[start + 1 : end], end + 1
        end += 1
    return None


def read_first_prove_argument_preview(text: str, masked: str, start: int) -> str:
    depth = 0
    end = start
    while end < len(masked):
        char = masked[end]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            break
        end += 1
    return _truncate_text(text[start:end].strip(), max_chars=400, max_lines=8) or ""


def _ocaml_char_literal_end(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] != "'" or start + 2 >= len(text):
        return None
    index = start + 1
    if text[index] in {"\n", "\r", "'"}:
        return None
    if text[index] == "\\":
        index += 1
        if index >= len(text):
            return None
        if text[index].isdigit():
            index += 1
            while index < len(text) and index < start + 5 and text[index].isdigit():
                index += 1
        elif text[index] in {"x", "o"}:
            index += 1
            digits = 0
            while index < len(text) and digits < 3 and text[index] in "0123456789abcdefABCDEF":
                index += 1
                digits += 1
            if digits == 0:
                return None
        elif text[index] == "u" and index + 1 < len(text) and text[index + 1] == "{":
            close = text.find("}", index + 2)
            if close < 0:
                return None
            index = close + 1
        else:
            index += 1
    else:
        index += 1
    return index + 1 if index < len(text) and text[index] == "'" else None


def _ocaml_quoted_string_end(text: str, start: int) -> int | None:
    if start >= len(text) or text[start] != "{":
        return None
    pipe = text.find("|", start + 1)
    if pipe < 0:
        return None
    delimiter = text[start + 1 : pipe]
    if delimiter and not re.fullmatch(r"[a-z_][A-Za-z0-9_']*", delimiter):
        return None
    close = text.find(f"|{delimiter}}}", pipe + 1)
    return close + len(delimiter) + 2 if close >= 0 else None


def _mask_range(chars: list[str], start: int, end: int) -> None:
    for index in range(start, end):
        if chars[index] != "\n":
            chars[index] = " "


def mask_ocaml_comments_and_strings(text: str) -> str:
    chars = list(text)
    i = 0
    comment_depth = 0
    in_string = False
    while i < len(text):
        if comment_depth:
            if text.startswith("(*", i):
                chars[i] = " "
                chars[i + 1] = " "
                comment_depth += 1
                i += 2
                continue
            if text.startswith("*)", i):
                chars[i] = " "
                chars[i + 1] = " "
                comment_depth -= 1
                i += 2
                continue
            if chars[i] != "\n":
                chars[i] = " "
            i += 1
            continue
        if in_string:
            if chars[i] != "\n":
                chars[i] = " "
            if text[i] == "\\" and i + 1 < len(text):
                if chars[i + 1] != "\n":
                    chars[i + 1] = " "
                i += 2
                continue
            if text[i] == '"':
                in_string = False
            i += 1
            continue
        if text.startswith("(*", i):
            chars[i] = " "
            chars[i + 1] = " "
            comment_depth = 1
            i += 2
            continue
        if text[i] == "'":
            char_end = _ocaml_char_literal_end(text, i)
            if char_end is not None:
                _mask_range(chars, i, char_end)
                i = char_end
                continue
        if text[i] == "{":
            quoted_end = _ocaml_quoted_string_end(text, i)
            if quoted_end is not None:
                _mask_range(chars, i, quoted_end)
                i = quoted_end
                continue
        if text[i] == '"':
            chars[i] = " "
            in_string = True
            i += 1
            continue
        if text[i] == "`":
            quoted = read_hol_backquote(text, i)
            if quoted is not None:
                _term, end = quoted
                i = end
                continue
        i += 1
    return "".join(chars)


def mask_hol_backquote_interiors(text: str) -> str:
    chars = list(text)
    index = 0
    while index < len(text):
        if text[index] != "`":
            index += 1
            continue
        quoted = read_hol_backquote(text, index)
        if quoted is None:
            index += 1
            continue
        _term, end = quoted
        for pos in range(index + 1, end - 1):
            if chars[pos] != "\n":
                chars[pos] = " "
        index = end
    return "".join(chars)


THEOREM_RE = re.compile(
    r"\blet\s+([A-Za-z][A-Za-z0-9_']*)\s*=\s*(?:time\s+)?prove\s*\(",
    re.MULTILINE,
)
DIRECT_THEOREM_RULES = (
    "ARITH_RULE",
    "INT_ARITH",
    "TAUT",
    "REAL_ARITH",
    "REAL_RING",
    "RING_RULE",
)
DIRECT_THEOREM_RE = re.compile(
    r"\blet\s+([A-Za-z][A-Za-z0-9_']*)\s*=\s*(?:time\s+)?("
    + "|".join(re.escape(name) for name in DIRECT_THEOREM_RULES)
    + r")\s*`",
    re.MULTILINE,
)

ENVIRONMENT_DECLARATION_RE = re.compile(
    r"\blet\s+([A-Za-z][A-Za-z0-9_']*)\s*=\s*("
    r"new_definition|new_specification|new_recursive_definition|define|define_finite_type|"
    r"new_type_definition|new_basic_type_definition|define_type"
    r")\b",
    re.MULTILINE,
)


OCAML_LEVEL_REAL_ANNOTATION_RE = re.compile(r"\([A-Za-z_][A-Za-z0-9_']*\s*:\s*real\s*\)")
SOURCE_READINESS_SCHEMA = "proof-run.source-readiness.v1"
PROOF_TEMPLATE_SUGGESTIONS_SCHEMA = "proof-run.proof-template-suggestions.v1"
TRANSITIVE_NEEDS_MAX_DEPTH = 5
TRANSITIVE_NEEDS_MAX_FILES = 40

NEED_PROFILE_RULES = (
    (
        re.compile(r"(?:^|/)Multivariate/flyspeck\.ml$"),
        {
            "profile": "flyspeck-geom",
            "severity": "warning",
            "expected_load": "huge",
            "message": "full Flyspeck geometry is a separate huge load; warm flyspeck-geom deliberately, usually with --pool-size 1, and do not use it for ordinary realanalysis/tether calculus",
        },
    ),
    (
        re.compile(r"(?:^|/)Multivariate/realanalysis\.ml$"),
        {
            "profile": "heavy",
            "severity": "warning",
            "expected_load": "very_heavy",
            "message": "full Multivariate realanalysis is a heavy calculus load; warm the shared heavy profile or an exact source base before repeated theorem/tactic edits",
        },
    ),
    (
        re.compile(r"(?:^|/)(?:Multivariate/|Tutorial/Vectors\.ml$)"),
        {
            "profile": "heavy",
            "severity": "warning",
            "expected_load": "heavy",
            "message": "multivariate library load can be expensive; reuse the shared heavy profile instead of opening another narrow heavy pool",
        },
    ),
    (
        re.compile(r"(?:^|/)Examples/solovay\.ml$"),
        {
            "profile": "diagnostic-noisy",
            "severity": "warning",
            "expected_load": "noisy",
            "message": "this support file is known to print failure-looking diagnostics; treat them as setup diagnostics unless target probes are missing",
        },
    ),
    (
        re.compile(r"(?:^|/)Library/ringtheory\.ml$"),
        {
            "profile": "light",
            "severity": "warning",
            "expected_load": "heavy",
            "message": "ringtheory is useful automation setup; reuse the shared light profile instead of cold-loading it repeatedly",
            "suggested_output_profile": "ringtheory",
        },
    ),
    (
        re.compile(r"(?:^|/)Library/(?:analysis|transc|realanalysis|card|wo|sets|topology)\.ml$"),
        {
            "profile": "heavy",
            "severity": "notice",
            "expected_load": "medium",
            "message": "this library can be a meaningful part of startup time; warm dev is usually friendlier than repeated cold replay",
        },
    ),
)

WORK_SURFACE_SIGNAL_PATTERNS = (
    (
        "definitions",
        re.compile(r"\b(?:new_definition|new_specification|new_recursive_definition|define|define_finite_type)\b"),
        "definitions change the HOL environment; use fast checks while editing and cold audit only for final evidence",
    ),
    (
        "types_or_datatypes",
        re.compile(r"\b(?:new_type_definition|new_basic_type_definition|define_type)\b"),
        "type or datatype declarations affect later parsing/proofs; use fast checks while editing and cold audit only for final evidence",
    ),
    (
        "syntax_or_parser",
        re.compile(
            r"\b(?:parse_as_infix|parse_as_binder|make_overload|override_interface|remove_interface|set_fixity)\b"
        ),
        "parser or overload changes make source order important; use fast checks while editing and cold audit only for final evidence",
    ),
    (
        "custom_tactic",
        re.compile(r"\blet\s+[A-Za-z][A-Za-z0-9_']*TAC\b"),
        "custom tactics are OCaml proof programs; keep exploration filtered and replay cold for final evidence",
    ),
    (
        "term_metaprogramming",
        re.compile(r"\b(?:mk_var|mk_comb|mk_abs|mk_eq|mk_imp|mk_forall|list_mk|end_itlist|itlist)\b"),
        "HOL terms appear to be generated by OCaml; static statement extraction may be incomplete",
    ),
)

PROFILE_HINT_PATTERNS = (
    (
        "probability",
        "probability_library",
        re.compile(r"\b(?:EXPECTATION(?:_[A-Z0-9_]+)?|PROB_SPACE(?:_[A-Z0-9_]+)?)\b"),
        "source mentions probability-space or expectation bindings; use the probability profile",
    ),
    (
        "heavy",
        "realanalysis_calculus",
        re.compile(
            r"\b(?:"
            r"REAL_DIFF_TAC|HAS_REAL_DERIVATIVE(?:_[A-Z0-9_]+)?|"
            r"has_real_derivative|real_derivative|atreal|"
            r"REAL_MVT(?:_[A-Z0-9_]+)?|REAL_CONTINUOUS(?:_[A-Z0-9_]+)?|"
            r"REAL_DIFFERENTIABLE(?:_[A-Z0-9_]+)?|REAL_INTEGRAL(?:_[A-Z0-9_]+)?|"
            r"HAS_VECTOR_DERIVATIVE(?:_[A-Z0-9_]+)?|"
            r"CONTINUOUS_ON(?:_[A-Z0-9_]+)?|COMPACT(?:_[A-Z0-9_]+)?|CONNECTED(?:_[A-Z0-9_]+)?|"
            r"NORM_POS_LE|HAS_DERIVATIVE(?:_[A-Z0-9_]+)?|DIFFERENTIABLE_IMP_CONTINUOUS_AT"
            r")\b"
        ),
        "source mentions realanalysis/calculus theorem or tactic names; prefer the heavy profile before repeated edits",
    ),
)


def scan_hol_loaders(source: Path) -> LoaderScanResult:
    """Scan exact source bytes once under the shared loader contract."""

    return scan_ocaml_loaders(source.read_bytes())


def _define_from_elf_inputs(scan: LoaderScanResult) -> list[dict]:
    if scan.status == "refused":
        assert scan.refusal is not None
        return [
            {
                "loader": "<source-lexical>",
                "source_line": scan.refusal.source_line,
                "source_offset": scan.refusal.span.start,
                "expression": "unknown",
                "complete_path_argument": False,
                "name": None,
                "reason": scan.refusal.reason,
                "scan_status": "refused",
                "refusal_kind": scan.refusal.kind,
            }
        ]

    inputs = []
    for item in scan.occurrences:
        if item.family != "artifact":
            continue
        base = {
            "loader": item.loader,
            "source_line": item.source_line,
            "source_offset": item.loader_span.start,
        }
        if item.outcome == "literal":
            inputs.append(
                {
                    **base,
                    "expression": "literal",
                    "complete_path_argument": True,
                    "name": item.name,
                    "path": item.path,
                }
            )
        else:
            inputs.append(
                {
                    **base,
                    "expression": "unknown",
                    "complete_path_argument": False,
                    "name": item.name,
                    "reason": item.reason,
                }
            )
    return inputs


def extract_define_from_elf_inputs_text(text: str) -> list[dict]:
    """Compatibility adapter for callers that already hold strict source text."""

    try:
        source = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        source = b"\xff"
    return _define_from_elf_inputs(scan_ocaml_loaders(source))


def extract_define_from_elf_inputs(source: Path) -> list[dict]:
    return _define_from_elf_inputs(scan_hol_loaders(source))


def extract_define_from_elf_paths(source: Path) -> list[dict]:
    return [
        {
            "loader": item["loader"],
            "name": item["name"],
            "path": item["path"],
            "source_line": item["source_line"],
        }
        for item in extract_define_from_elf_inputs(source)
        if item.get("expression") == "literal"
    ]


def classify_need_path(path: str) -> dict:
    normalized = path.strip()
    for pattern, profile in NEED_PROFILE_RULES:
        if pattern.search(normalized):
            return {
                "profile": profile.get("profile"),
                "severity": profile.get("severity"),
                "expected_load": profile.get("expected_load"),
                "message": profile.get("message"),
                "suggested_output_profile": profile.get("suggested_output_profile"),
            }
    return {
        "profile": "ordinary",
        "severity": "info",
        "expected_load": "unknown",
        "message": "ordinary source dependency; not profiled by this static readiness check",
        "suggested_output_profile": None,
    }


def extract_hol_needs(source: Path, *, scan: LoaderScanResult | None = None) -> list[dict]:
    scan = scan if scan is not None else scan_hol_loaders(source)
    needs = []
    for item in scan.literal_occurrences:
        if item.loader != "needs" or item.path is None:
            continue
        path = item.path
        profile = classify_need_path(path)
        needs.append(
            {
                "path": path,
                "source_line": item.source_line,
                **{key: value for key, value in profile.items() if value is not None},
            }
        )
    return needs


def resolve_local_source_load_path(source: Path, load_path: str) -> Path | None:
    raw = Path(load_path).expanduser()
    candidate = raw if raw.is_absolute() else source.parent / raw
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved.is_file():
        return resolved
    return None


def extract_hol_source_loads(source: Path, *, scan: LoaderScanResult | None = None) -> list[dict]:
    scan = scan if scan is not None else scan_hol_loaders(source)
    loads = []
    for item in scan.literal_occurrences:
        if item.loader not in {"#use", "load", "loads", "loadt"} or item.path is None:
            continue
        path = item.path
        loader = item.loader
        raw = Path(path).expanduser()
        local_candidate = raw if raw.is_absolute() else source.parent / raw
        resolved = resolve_local_source_load_path(source, path)
        entry = {
            "loader": loader,
            "path": path,
            "source_line": item.source_line,
            "source_local_candidate": str(local_candidate.resolve()),
            "source_local_exists": resolved is not None,
            "resolution": "source_local" if resolved is not None else "load_path_or_missing",
            "meaning": "static source-load scan; replay still performs the actual HOL/OCaml load",
        }
        if resolved is not None:
            entry["resolved_path"] = str(resolved)
            entry["sha256"] = sha256_file(resolved)
        elif raw.is_absolute():
            entry["resolution"] = "missing_absolute"
            entry["severity"] = "warning"
            entry["message"] = f"{loader} target does not exist: {path}"
        elif "/" not in path and path.endswith(".ml"):
            entry["severity"] = "warning"
            entry["message"] = (
                f'{loader} "{path}" does not exist beside the source; '
                "replay may depend on HOL load_path or fail with Not_found"
            )
        else:
            entry["severity"] = "info"
            entry["message"] = "relative source load is not beside the source; HOL load_path may still resolve it"
        loads.append(entry)
    return loads


def resolve_local_need_path(source: Path, need_path: str) -> Path | None:
    raw = Path(need_path).expanduser()
    candidates = [raw] if raw.is_absolute() else [source.parent / raw]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def extract_transitive_hol_needs(
    source: Path,
    *,
    direct_needs: list[dict] | None = None,
    max_depth: int = TRANSITIVE_NEEDS_MAX_DEPTH,
    max_files: int = TRANSITIVE_NEEDS_MAX_FILES,
) -> list[dict]:
    """Follow local needs files far enough to spot hidden heavy setup.

    This is intentionally a static source-load scan, not a proof dependency
    graph. It follows only locally resolvable files so HOL library paths are
    classified but not recursively walked.
    """
    source = source.expanduser().resolve()
    scan_root = source.parent
    allowed_roots = {scan_root}
    direct = direct_needs if direct_needs is not None else extract_hol_needs(source)
    transitive: list[dict] = []
    visited_files = {source}
    seen_records: set[tuple[str, str, int]] = set()
    stack: list[tuple[Path, list[str], int]] = []

    for need in direct:
        resolved = resolve_local_need_path(source, str(need.get("path") or ""))
        if resolved and resolved not in visited_files:
            # A literal first-hop load explicitly authorizes this bounded local
            # dependency root even when the entrypoint lives in a nested
            # attempts/ directory. Recursive traversal remains confined to the
            # entrypoint root plus those explicitly named roots.
            allowed_roots.add(resolved.parent)
            stack.append((resolved, [str(need.get("path") or resolved)], 1))

    scanned_files = 0
    while stack and scanned_files < max_files:
        current, via, depth = stack.pop()
        if current in visited_files or depth > max_depth:
            continue
        visited_files.add(current)
        scanned_files += 1
        try:
            child_needs = extract_hol_needs(current)
        except OSError:
            continue
        for child in child_needs:
            child_path = str(child.get("path") or "")
            key = (child_path, str(current), int(child.get("source_line") or 0))
            if key in seen_records:
                continue
            seen_records.add(key)
            transitive.append(
                {
                    **child,
                    "source": str(current),
                    "via": via,
                    "depth": depth,
                    "meaning": "static transitive needs entry; this is load-shape guidance, not proof-dependency evidence",
                }
            )
            resolved = resolve_local_need_path(current, child_path)
            if (
                resolved
                and any(path_is_within(resolved, root) for root in allowed_roots)
                and resolved not in visited_files
                and depth < max_depth
            ):
                stack.append((resolved, [*via, child_path], depth + 1))
    return transitive


def claim_source_label(claim: dict | None) -> str | None:
    if not claim:
        return None
    source = claim.get("source")
    line = claim.get("statement_line") or claim.get("source_line")
    if source and line:
        return f"{source}:{line}"
    return source


def compact_claim(claim: dict) -> dict:
    return {
        "name": claim.get("name"),
        "source": claim.get("source"),
        "source_line": claim.get("source_line"),
        "statement_line": claim.get("statement_line"),
        "statement_end_line": claim.get("statement_end_line"),
        "statement_preview": claim.get("statement_preview"),
        "statement_truncated": claim.get("statement_truncated"),
        "statement_extractable": claim.get("statement_extractable"),
        "statement_source": claim.get("statement_source"),
        "prove_argument_preview": claim.get("prove_argument_preview"),
    }


def claim_target_end_lines(claims: list[dict], total_lines: int) -> dict[str, int]:
    ordered = sorted(
        [claim for claim in claims if claim.get("name") and claim.get("source_line")],
        key=lambda claim: int(claim.get("source_line") or 0),
    )
    ends: dict[str, int] = {}
    for index, claim in enumerate(ordered):
        start = int(claim.get("source_line") or 0)
        next_start = int(ordered[index + 1].get("source_line") or 0) if index + 1 < len(ordered) else 0
        end = next_start - 1 if next_start > start else total_lines
        ends[str(claim.get("name"))] = max(start, end)
    return ends
