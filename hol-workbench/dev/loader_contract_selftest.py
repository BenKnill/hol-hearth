#!/usr/bin/env python3
"""Hostile stdlib contract for the shared OCaml loader scanner."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

WORKBENCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKBENCH_ROOT))

from hol_workbench.proofs.loader_scan import scan_ocaml_loaders  # noqa: E402
from hol_workbench.proofs.profile_inference import (  # noqa: E402
    SOURCE_LOADER_SCAN_REFUSED_PROFILE,
    suggest_warmup_profile_for_source,
)
from hol_workbench.proofs.source import (  # noqa: E402
    extract_define_from_elf_inputs,
    extract_hol_needs,
    extract_hol_source_loads,
    scan_hol_loaders,
)
from hol_workbench.source_dependency_closure import (  # noqa: E402
    build_source_dependency_closure,
    literal_source_loads,
)


@dataclass(frozen=True)
class SourceCase:
    name: str
    source: bytes
    literals: tuple[tuple[str, str], ...] = ()
    dynamic: tuple[str, ...] = ()
    status: str = "ok"


@dataclass(frozen=True)
class ArtifactCase:
    name: str
    source: bytes
    literals: tuple[tuple[str, str, str], ...] = ()
    dynamic: tuple[str, ...] = ()


SOURCE_CASES = (
    SourceCase("needs-exact", b'needs "a.ml";;', (("needs", "a.ml"),)),
    SourceCase(
        "nested-comment-and-parentheses",
        b'needs (* outer "*)" (* inner {|*)|} *) *) ((("a.ml")));;',
        (("needs", "a.ml"),),
    ),
    SourceCase("apostrophe-shields-comment-end", b'(* \'*) needs "bad.ml";;', status="refused"),
    SourceCase(
        "apostrophe-shielded-then-real-comment-end",
        b'(* \'*) *) needs "good.ml";;',
        (("needs", "good.ml"),),
    ),
    SourceCase(
        "quoted-string",
        b'loadt {tag|a "q" (* x *) ;;.ml|tag};;',
        (("loadt", 'a "q" (* x *) ;;.ml'),),
    ),
    SourceCase("quoted-string-no-escapes", br'loads {|a\n.ml|};;', (("loads", r"a\n.ml"),)),
    SourceCase(
        "quoted-extension-opaque",
        (
            b'let a = {%foo|needs "bad-empty.ml";; (* ) |};;\n'
            b'let b = {%foo bar|loadt "bad-named.ml";; *) |bar};;\n'
            b'let c = {%%foo|loads "bad-double.ml";;|};;\n'
            b'needs "good.ml";;\n'
        ),
        (("needs", "good.ml"),),
    ),
    SourceCase(
        "quoted-delimiter-at-bound",
        b"loadt {" + b"a" * 4096 + b"|good.ml|" + b"a" * 4096 + b"};;",
        (("loadt", "good.ml"),),
    ),
    SourceCase(
        "quoted-delimiter-over-bound",
        b"{" + b"a" * 4097 + b"|needs bad|" + b"a" * 4097 + b"}",
        status="refused",
    ),
    SourceCase(
        "quoted-extension-delimiter-over-bound",
        b"{%foo " + b"a" * 4097 + b"|needs bad|" + b"a" * 4097 + b"}",
        status="refused",
    ),
    SourceCase("invalid-quoted-digit-delimiter", b'{tag2|needs "bad.ml";;|tag2}', status="refused"),
    SourceCase("invalid-quoted-digit-first", b'{2tag|needs "bad.ml"|2tag}', status="refused"),
    SourceCase("invalid-quoted-upper-delimiter", b'{Tag|needs "bad.ml";;|Tag}', status="refused"),
    SourceCase(
        "invalid-quoted-apostrophe-delimiter",
        b'{tag_x\'|needs "bad.ml";;|tag_x\'}',
        status="refused",
    ),
    SourceCase("use-directive", b'#use "a.ml";;', (("#use", "a.ml"),)),
    SourceCase("raw-identifier", br'\#needs "a.ml";;', (("needs", "a.ml"),)),
    SourceCase("raw-keyword-is-not-control", br'\#then needs "decoy.ml";;', dynamic=("needs",)),
    SourceCase("malformed-raw-identifier", br'\needs "hidden.ml";;', status="refused"),
    SourceCase(
        "line-directive",
        b'# 42 "generated.ml"\nneeds "a.ml";;',
        (("needs", "a.ml"),),
    ),
    SourceCase(
        "standalone-cr-line-directive",
        b'let x=1;;\r# 42 "generated.ml"\rneeds "a.ml";;',
        (("needs", "a.ml"),),
    ),
    SourceCase(
        "malformed-line-directive",
        b'# 42 needs "hidden.ml"\nneeds "early.ml";;',
        status="refused",
    ),
    SourceCase(
        "single-semicolon-sequence",
        b'loads "iterate.ml"; loads "cart.ml";;',
        (("loads", "iterate.ml"), ("loads", "cart.ml")),
    ),
    SourceCase(
        "conditional-call-branches",
        b'if flag then loads "a.ml" else loads "b.ml";;',
        (("loads", "a.ml"), ("loads", "b.ml")),
    ),
    SourceCase("nested-call", b'ignore (needs "a.ml");;', (("needs", "a.ml"),)),
    SourceCase("rhs-call", b'let _ = needs (("a.ml"));;', (("needs", "a.ml"),)),
    SourceCase(
        "try-with-boundary",
        b'try needs "a.ml" with Failure _ -> ();;',
        (("needs", "a.ml"),),
    ),
    SourceCase(
        "struct-call-position",
        b'module M = struct needs "a.ml";; end;;',
        (("needs", "a.ml"),),
    ),
    SourceCase(
        "numeric-and-unicode-escapes",
        br'needs "caf\195\169/\u{03bb}\x2e\o155\108";;',
        (("needs", "café/λ.ml"),),
    ),
    SourceCase("escaped-line-continuation", b'needs "a\\\n  .ml";;', (("needs", "a.ml"),)),
    SourceCase(
        "escaped-crcrlf-continuation",
        b'needs "a\\\r\r\n  b.ml";;',
        (("needs", "ab.ml"),),
    ),
    SourceCase(
        "preserved-bare-cr-escape",
        b'needs "a\\\r  b.ml";;',
        (("needs", "a\\\r  b.ml"),),
    ),
    SourceCase(
        "opaque-constructs",
        (
            b'let ordinary = "needs \\"bad-string.ml\\";;";;\n'
            b'let quoted = {|loadt "bad-quoted.ml";;|};;\n'
            b'(* needs "bad-comment.ml";; *)\n'
            b"let c = 'n';; let id (x:'a) = x;;\n"
            b'let term = `needs "bad-term.ml";;`;;\n'
            b'let raw_newline = \'\n\';;\n'
            b'needs "good.ml";;\n'
        ),
        (("needs", "good.ml"),),
    ),
    SourceCase("conditional-argument", b'needs (if flag then "a.ml" else "b.ml");;', dynamic=("needs",)),
    SourceCase("concatenation", b'needs ("a" ^ ".ml");;', dynamic=("needs",)),
    SourceCase("variable", b'needs path;;', dynamic=("needs",)),
    SourceCase("function-decoy", b'needs (f "decoy.ml");;', dynamic=("needs",)),
    SourceCase("tuple", b'needs ("a.ml", "b.ml");;', dynamic=("needs",)),
    SourceCase("sequence-argument", b'needs ("a.ml"; "b.ml");;', dynamic=("needs",)),
    SourceCase("annotation", b'needs ("a.ml" : string);;', dynamic=("needs",)),
    SourceCase("extra-expression", b'needs "a.ml" extra;;', dynamic=("needs",)),
    SourceCase("later-decoy", b'needs;; print_endline "decoy.ml";;', dynamic=("needs",)),
    SourceCase(
        "variable-with-later-decoy",
        b'needs path; print_endline "decoy.ml";;',
        dynamic=("needs",),
    ),
    SourceCase(
        "binding-named-needs",
        b'let needs x = print_endline "not-a-dependency.ml";;',
        dynamic=("needs",),
    ),
    SourceCase(
        "shadowed-needs",
        b'let needs x = x;; needs "not-a-dependency.ml";;',
        dynamic=("needs", "needs"),
    ),
    SourceCase(
        "parameter-shadowed-needs",
        b'let f needs = needs "not-a-dependency.ml";;',
        dynamic=("needs", "needs"),
    ),
    SourceCase(
        "parenthesized-binding-needs",
        b'let (needs) x = x;; needs "not-a-dependency.ml";;',
        dynamic=("needs", "needs"),
    ),
    SourceCase(
        "attributed-binding-needs",
        b'let[@inline] needs x = x;; needs "not-a-dependency.ml";;',
        dynamic=("needs", "needs"),
    ),
    SourceCase(
        "shadowed-loadt",
        b'let loadt x = x;; loadt "not-a-dependency.ml";;',
        dynamic=("loadt", "loadt"),
    ),
    SourceCase(
        "lambda-shadowed-loads",
        b'fun loads -> loads "not-a-dependency.ml";;',
        dynamic=("loads", "loads"),
    ),
    SourceCase("value-position", b'foo needs "decoy.ml";;', dynamic=("needs",)),
    SourceCase("non-utf8-runtime-path", br'needs "\255";;', dynamic=("needs",)),
    SourceCase("nul-runtime-path", br'needs "\000.ml";;', dynamic=("needs",)),
    SourceCase(
        "nbsp-is-not-blank",
        ("needs" + "\u00a0" + '"decoy.ml";;').encode(),
        status="refused",
    ),
    SourceCase("vertical-tab-is-not-blank", b'needs\v"decoy.ml";;', status="refused"),
    SourceCase("qualified-decoy", b'M.needs "decoy.ml";;'),
    SourceCase("local-open-qualified-decoy", b'M.(needs "decoy.ml");;'),
    SourceCase("functor-local-open-qualified-decoy", b'F(X).(needs "decoy.ml");;'),
    SourceCase("hash-decoy", b'#needs "decoy.ml";;'),
    SourceCase("longer-identifiers", 'myneeds "x";; needsé "y";; needś "z";;'.encode()),
    SourceCase("type-variable", b"let x : 'needs = value;; \"decoy.ml\";;"),
    SourceCase("invalid-utf8-atomic", b'needs "early.ml";;\xff', status="refused"),
    SourceCase("nul-control-atomic", b'needs "early.ml";;\x00', status="refused"),
    SourceCase("soh-control-atomic", b'needs "early.ml";;\x01', status="refused"),
    SourceCase("stray-backslash-atomic", b'needs "early.ml";;\\', status="refused"),
    SourceCase("illegal-unicode-atomic", 'needs "early.ml";; ☃'.encode(), status="refused"),
    SourceCase("unterminated-string-atomic", b'needs "early.ml";; "oops', status="refused"),
    SourceCase("unterminated-quoted-atomic", b'needs "early.ml";; {|oops', status="refused"),
    SourceCase("unterminated-comment-atomic", b'needs "early.ml";; (* oops', status="refused"),
    SourceCase("unterminated-backquote-atomic", b'needs "early.ml";; `oops', status="refused"),
    SourceCase("unbalanced-delimiter-atomic", b'needs "early.ml";; (', status="refused"),
    SourceCase("phrase-boundary-inside-delimiter", b'(needs "bad.ml";;);;', status="refused"),
    SourceCase(
        "invalid-dotted-quoted-delimiter",
        b'{tag.x|needs "bad.ml";;|tag.x}',
        status="refused",
    ),
    SourceCase(
        "invalid-spaced-quoted-delimiter",
        b'{tag x|needs "bad.ml"|tag x}',
        status="refused",
    ),
    SourceCase(
        "invalid-long-quoted-delimiter",
        b"{" + b"a" * 100 + b'2|needs "bad.ml"|' + b"a" * 100 + b"2}",
        status="refused",
    ),
    SourceCase("invalid-decimal-escape", br'needs "a\400.ml";;', status="refused"),
    SourceCase("preserved-short-hex-escape", br'needs "a\x2.ml";;', (("needs", r"a\x2.ml"),)),
    SourceCase("invalid-octal-escape", br'needs "a\o477.ml";;', status="refused"),
    SourceCase("invalid-surrogate-escape", br'needs "a\u{d800}.ml";;', status="refused"),
    SourceCase("invalid-large-unicode-escape", br'needs "a\u{110000}.ml";;', status="refused"),
    SourceCase("preserved-unknown-escape", br'needs "a\q.ml";;', (("needs", r"a\q.ml"),)),
    SourceCase("invalid-unicode-character-escape", br"let c = '\u{41}';;", status="refused"),
    SourceCase(
        "lax-comment-string-escapes",
        br'(* "\q \400 *)" *) needs "good.ml";;',
        (("needs", "good.ml"),),
    ),
)


ARTIFACT_CASES = (
    ArtifactCase(
        "define-from-elf",
        b'define_from_elf "mc" "obj.o";;',
        (("define_from_elf", "mc", "obj.o"),),
    ),
    ArtifactCase(
        "define-from-elf-parenthesized-quoted",
        b'define_from_elf (({|mc|})) (({obj|build/obj.o|obj}));;',
        (("define_from_elf", "mc", "build/obj.o"),),
    ),
    ArtifactCase(
        "define-assert-from-elf",
        b'define_assert_from_elf "mc" "obj.o" [];;',
        (("define_assert_from_elf", "mc", "obj.o"),),
    ),
    ArtifactCase(
        "define-assert-dynamic-third",
        b'define_assert_from_elf "mc" "obj.o" (if flag then [] else instructions);;',
        (("define_assert_from_elf", "mc", "obj.o"),),
    ),
    ArtifactCase(
        "define-from-elf-try-boundary",
        b'try define_from_elf "mc" "obj.o" with Failure _ -> ();;',
        (("define_from_elf", "mc", "obj.o"),),
    ),
    ArtifactCase("dynamic-name", b'define_from_elf name "obj.o";;', dynamic=("define_from_elf",)),
    ArtifactCase("dynamic-path", b'define_from_elf "mc" path;;', dynamic=("define_from_elf",)),
    ArtifactCase(
        "concatenated-path",
        b'define_from_elf "mc" ("obj" ^ ".o");;',
        dynamic=("define_from_elf",),
    ),
    ArtifactCase(
        "extra-define-from-expression",
        b'define_from_elf "mc" "obj.o" extra;;',
        dynamic=("define_from_elf",),
    ),
    ArtifactCase(
        "missing-assert-instructions",
        b'define_assert_from_elf "mc" "obj.o";;',
        dynamic=("define_assert_from_elf",),
    ),
    ArtifactCase(
        "missing-assert-before-in",
        b'let f = define_assert_from_elf "mc" "obj.o" in f [];;',
        dynamic=("define_assert_from_elf",),
    ),
    ArtifactCase(
        "missing-assert-before-else",
        b'if flag then define_assert_from_elf "mc" "obj.o" else [];;',
        dynamic=("define_assert_from_elf",),
    ),
    ArtifactCase(
        "extra-assert-expression",
        b'define_assert_from_elf "mc" "obj.o" [] extra;;',
        dynamic=("define_assert_from_elf",),
    ),
    ArtifactCase(
        "qualified-assert-instructions",
        b'define_assert_from_elf "mc" "obj.o" Instructions.table;;',
        (("define_assert_from_elf", "mc", "obj.o"),),
    ),
    ArtifactCase(
        "invalid-assert-third-typevar",
        b"define_assert_from_elf \"mc\" \"obj.o\" 'a;;",
        dynamic=("define_assert_from_elf",),
    ),
    ArtifactCase(
        "incomplete-assert-third-keyword",
        b'define_assert_from_elf "mc" "obj.o" fun;;',
        dynamic=("define_assert_from_elf",),
    ),
    ArtifactCase(
        "incomplete-assert-third-begin",
        b'define_assert_from_elf "mc" "obj.o" begin;;',
        dynamic=("define_assert_from_elf",),
    ),
    ArtifactCase(
        "incomplete-assert-third-while",
        b'define_assert_from_elf "mc" "obj.o" while;;',
        dynamic=("define_assert_from_elf",),
    ),
    ArtifactCase(
        "incomplete-assert-third-assert",
        b'define_assert_from_elf "mc" "obj.o" assert;;',
        dynamic=("define_assert_from_elf",),
    ),
    ArtifactCase(
        "nul-artifact-path",
        br'define_from_elf "mc" "\000.o";;',
        dynamic=("define_from_elf",),
    ),
    ArtifactCase(
        "elf-bindings-are-not-calls",
        (
            b'let define_from_elf name file = pair name file;;\n'
            b'let define_assert_from_elf name file = pair name file;;\n'
        ),
    ),
    ArtifactCase(
        "shadowed-elf-binding-is-not-an-artifact",
        (
            b'let define_from_elf name file = pair name file;;\n'
            b'define_from_elf "mc" "decoy.o";;\n'
        ),
        dynamic=("define_from_elf",),
    ),
)


def _literal_source_rows(source: bytes) -> tuple[tuple[str, str], ...]:
    scan = scan_ocaml_loaders(source)
    return tuple(
        (item.loader, item.path)
        for item in scan.literal_occurrences
        if item.family == "source" and item.path is not None
    )


def _dynamic_rows(source: bytes, family: str) -> tuple[str, ...]:
    scan = scan_ocaml_loaders(source)
    return tuple(item.loader for item in scan.dynamic_occurrences if item.family == family)


def _source_tables(root: Path) -> int:
    for case in SOURCE_CASES:
        scan = scan_ocaml_loaders(case.source)
        assert scan.status == case.status, (case.name, scan)
        assert _literal_source_rows(case.source) == case.literals, case.name
        assert _dynamic_rows(case.source, "source") == case.dynamic, case.name
        if case.status == "refused":
            assert scan.refusal is not None, case.name
            assert not scan.occurrences, case.name

        source = root / f"{case.name}.ml"
        source.write_bytes(case.source)
        file_scan = scan_hol_loaders(source)
        assert file_scan == scan, case.name
        need_rows = tuple(("needs", str(item["path"])) for item in extract_hol_needs(source))
        load_rows = tuple(
            (str(item["loader"]), str(item["path"])) for item in extract_hol_source_loads(source)
        )
        expected_needs = tuple(item for item in case.literals if item[0] == "needs")
        expected_loads = tuple(item for item in case.literals if item[0] != "needs")
        assert need_rows == expected_needs, case.name
        assert load_rows == expected_loads, case.name
        closure_rows = tuple(
            (str(item["loader"]), str(item["declared_path"])) for item in literal_source_loads(source)
        )
        assert closure_rows == case.literals, case.name
    return len(SOURCE_CASES)


def _artifact_tables(root: Path) -> int:
    for case in ARTIFACT_CASES:
        scan = scan_ocaml_loaders(case.source)
        literals = tuple(
            (item.loader, item.name, item.path)
            for item in scan.literal_occurrences
            if item.family == "artifact" and item.name is not None and item.path is not None
        )
        assert literals == case.literals, case.name
        assert _dynamic_rows(case.source, "artifact") == case.dynamic, case.name

        source = root / f"{case.name}.ml"
        source.write_bytes(case.source)
        adapter = extract_define_from_elf_inputs(source)
        adapter_literals = tuple(
            (str(item["loader"]), str(item["name"]), str(item["path"]))
            for item in adapter
            if item.get("expression") == "literal"
        )
        adapter_dynamic = tuple(
            str(item["loader"]) for item in adapter if item.get("expression") != "literal"
        )
        assert adapter_literals == case.literals, case.name
        assert adapter_dynamic == case.dynamic, case.name
    return len(ARTIFACT_CASES)


def _span_and_escape_contract() -> None:
    source = 'let café = 1;;\nneeds "λ.ml";;'.encode()
    scan = scan_ocaml_loaders(source)
    assert scan.status == "ok"
    occurrence = scan.literal_occurrences[0]
    assert occurrence.source_line == 2
    assert occurrence.loader_span.start == source.index(b"needs")
    assert occurrence.loader_span.bytes_from(source) == b"needs"
    assert occurrence.path_literal is not None
    raw = occurrence.path_literal.span.bytes_from(source)
    assert raw == '"λ.ml"'.encode()
    assert occurrence.path_literal.raw_sha256 == hashlib.sha256(raw).hexdigest()
    assert scan.source_sha256 == hashlib.sha256(source).hexdigest()

    escaped = scan_ocaml_loaders(br'needs "\092\x2f\o141\u{03bb}.ml";;')
    assert escaped.literal_occurrences[0].path_literal is not None
    assert escaped.literal_occurrences[0].path_literal.value_bytes == "\\/aλ.ml".encode()

    newlines = scan_ocaml_loaders(b'needs "a\rb\r\nc\n.ml";;')
    assert newlines.literal_occurrences[0].path_literal is not None
    assert newlines.literal_occurrences[0].path_literal.value_bytes == b"a\rb\nc\n.ml"

    quoted_newlines = scan_ocaml_loaders(b'needs {|a\rb\r\nc\n.ml|};;')
    assert quoted_newlines.literal_occurrences[0].path_literal is not None
    assert quoted_newlines.literal_occurrences[0].path_literal.value_bytes == b"a\rb\nc\n.ml"


def _consumer_convergence(root: Path) -> int:
    exact = root / "profile-exact.ml"
    exact.write_bytes(b'needs "arm/proofs/foo.ml";;')
    assert suggest_warmup_profile_for_source(exact) == "s2n-arm"

    conditional = root / "profile-conditional.ml"
    conditional.write_bytes(b'needs (if flag then "arm/proofs/a.ml" else "b.ml");;')
    assert suggest_warmup_profile_for_source(conditional) == SOURCE_LOADER_SCAN_REFUSED_PROFILE
    conditional_closure = build_source_dependency_closure(conditional)
    assert conditional_closure["literal_edge_count"] == 0
    assert conditional_closure["dynamic_loader_count"] == 1
    assert conditional_closure["semantic_identity_complete"] is False

    binding = root / "profile-binding.ml"
    binding.write_bytes(b'let needs x = print_endline "x86/proofs/decoy.ml";;')
    assert suggest_warmup_profile_for_source(binding) == SOURCE_LOADER_SCAN_REFUSED_PROFILE
    binding_closure = build_source_dependency_closure(binding)
    assert binding_closure["literal_edge_count"] == 0
    assert binding_closure["dynamic_loader_count"] == 1

    hint_child = root / "needs-hint-child.ml"
    hint_child.write_bytes(b'let tactic = X86_STEPS_TAC;;')
    needs_hint = root / "profile-needs-child-text.ml"
    needs_hint.write_bytes(b'needs "needs-hint-child.ml";;')
    assert suggest_warmup_profile_for_source(needs_hint) == "core"

    load_hint_child = root / "load-hint-child.ml"
    load_hint_child.write_bytes(b'let tactic = X86_STEPS_TAC;;')
    load_hint = root / "profile-load-child-text.ml"
    load_hint.write_bytes(b'loadt "load-hint-child.ml";;')
    assert suggest_warmup_profile_for_source(load_hint) == "s2n-x86"

    nested_need_child = root / "nested-needs-child.ml"
    nested_need_child.write_bytes(b'needs "x86/proofs/nested.ml";;')
    nested_need = root / "profile-nested-needs.ml"
    nested_need.write_bytes(b'needs "nested-needs-child.ml";;')
    assert suggest_warmup_profile_for_source(nested_need) == "s2n-x86"

    dynamic_need_child = root / "dynamic-needs-child.ml"
    dynamic_need_child.write_bytes(b'needs path;;')
    dynamic_need = root / "profile-dynamic-needs-child.ml"
    dynamic_need.write_bytes(b'needs "dynamic-needs-child.ml";;')
    assert suggest_warmup_profile_for_source(dynamic_need) == SOURCE_LOADER_SCAN_REFUSED_PROFILE

    malformed = root / "profile-malformed.ml"
    malformed.write_bytes(b'needs "early.ml";;\xff')
    assert suggest_warmup_profile_for_source(malformed) == SOURCE_LOADER_SCAN_REFUSED_PROFILE
    malformed_closure = build_source_dependency_closure(malformed)
    assert malformed_closure["literal_edge_count"] == 0
    assert malformed_closure["dynamic_loader_count"] == 1
    assert malformed_closure["dynamic_loaders"][0]["refusal_kind"] == "invalid_utf8"

    artifact = root / "exact-object.o"
    artifact.write_bytes(b"exact-object")
    artifact_source = root / "artifact-exact.ml"
    artifact_source.write_text(
        f'define_assert_from_elf "mc" "{artifact}" [];;\n',
        encoding="utf-8",
    )
    artifact_closure = build_source_dependency_closure(artifact_source)
    assert artifact_closure["literal_artifact_count"] == 1
    assert artifact_closure["dynamic_artifact_count"] == 0

    dynamic_artifact = root / "artifact-dynamic.ml"
    dynamic_artifact.write_bytes(b'define_from_elf "mc" ("exact" ^ ".o");;')
    dynamic_artifact_closure = build_source_dependency_closure(dynamic_artifact)
    assert dynamic_artifact_closure["literal_artifact_count"] == 0
    assert dynamic_artifact_closure["dynamic_artifact_count"] == 1

    nul_source = root / "profile-nul-path.ml"
    nul_source.write_bytes(br'needs "\000.ml";;')
    assert suggest_warmup_profile_for_source(nul_source) == SOURCE_LOADER_SCAN_REFUSED_PROFILE
    nul_closure = build_source_dependency_closure(nul_source)
    assert nul_closure["literal_edge_count"] == 0
    assert nul_closure["dynamic_loader_count"] == 1
    return 11


def main() -> int:
    if not sys.platform.startswith("linux"):
        raise SystemExit("loader contract selftest is Linux-only")
    with tempfile.TemporaryDirectory(prefix="loader-contract-") as temporary:
        root = Path(temporary)
        source_cases = _source_tables(root)
        artifact_cases = _artifact_tables(root)
        _span_and_escape_contract()
        convergence_cases = _consumer_convergence(root)
    print(
        "loader_contract_selftest=passed "
        f"source_cases={source_cases} artifact_cases={artifact_cases} "
        f"convergence_cases={convergence_cases}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
