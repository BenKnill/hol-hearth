#!/usr/bin/env python3
"""Transcript-based claim accounting for raw vanilla HOL runs."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from hol_workbench.evidence_policy import RAW_LOG_POLICY
from hol_workbench.filter_output import strip_ansi
from hol_workbench.fork_child_ocaml import ocaml_string_literal
from hol_workbench.hashing import sha256_bytes, sha256_text
from hol_workbench.ids import slugify, stamp
from hol_workbench.jsonio import atomic_write_json, write_text_lines
from hol_workbench.logical_source_roots import (
    LogicalSourceRootError,
    logical_source_root_declarations,
)
from hol_workbench.profile_registry import (
    ProfileRegistryError,
    load_public_profile_manifest,
    public_profile_names,
)
from hol_workbench.proof_run_runtime import default_warm_startup_command
from hol_workbench.proofs.profile_inference import infer_public_profile_for_source
from hol_workbench.proofs.theorem_scan import extract_hol_theorems_bytes
from hol_workbench.source_dependency_closure import SourceDependencyInferenceError
from hol_workbench.source_dependency_package import (
    DependencyPackageError,
    dependency_package_entrypoint,
    literal_elf_artifact_runtime_cwd,
    materialize_dependency_package,
)
from hol_workbench.source_execution_plan import (
    capture_source_dependency_closure,
    source_execution_prelude,
)

VANILLA_RUN_SCHEMA = "proof-run.vanilla-run.v1"
VANILLA_CLAIM_SCHEMA = "proof-run.vanilla-claim.v1"
FAILURE_LINE_RE = re.compile(
    r"^(?:Error:|Exception:|Exception raised:|Unbound value\b|Unbound constructor\b|Unbound module\b|"
    r"Reference to undefined global\b|Tactic failed\b|Failure\b|"
    r"Stack overflow during evaluation \(looping recursion\?\)\.$)"
)
HOL_CLAIM_NAME = r"[A-Za-z_][A-Za-z0-9_']*"
HOL_CLAIM_NAME_RE = re.compile(rf"^{HOL_CLAIM_NAME}$")
PROBE_SCHEMA = "hol-workbench.final-replay-probe.v1"
PROBE_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
PROBE_OK_PREFIX = "__HOL_CLAIM_PROBE_OK__"
PROBE_MISMATCH_PREFIX = "__HOL_CLAIM_PROBE_NO__"
PROBE_DONE_PREFIX = "__HOL_CLAIM_PROBE_DONE__"
EVIDENCE_BOUNDARY = (
    "exact nonce-bound source completion and named theorem binding checks "
    "over raw transcript bytes"
)


class ClaimProbeContractError(ValueError):
    """The static inventory cannot be represented by an unambiguous probe."""


def _validated_nonce(nonce: str | None) -> str:
    value = secrets.token_hex(16) if nonce is None else nonce
    if PROBE_NONCE_RE.fullmatch(value) is None:
        raise ClaimProbeContractError("claim-probe nonce must be exactly 32 lowercase hexadecimal characters")
    return value


def _validated_claims(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = [claim for claim in claims if claim.get("name")]
    names: list[str] = []
    for claim in selected:
        name = str(claim["name"])
        if HOL_CLAIM_NAME_RE.fullmatch(name) is None:
            raise ClaimProbeContractError(f"claim-probe theorem name is not a HOL identifier: {name!r}")
        names.append(name)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ClaimProbeContractError(f"duplicate static theorem names: {', '.join(duplicates)}")
    return selected


def _strict_matcher_source(nonce: str) -> list[str]:
    canonicalize = f"hol_workbench_probe_canonicalize_{nonce}"
    matcher = f"hol_workbench_probe_matches_{nonce}"
    return [
        f"let {canonicalize} tm =",
        "  let tvs = type_vars_in_term tm in",
        "  let rec number_typevars n tys =",
        "    match tys with",
        "      [] -> []",
        "    | ty::rest ->",
        '        (mk_vartype ("hol_workbench_probe_ty_" ^ string_of_int n),ty)::',
        "        number_typevars (n + 1) rest in",
        "  inst (number_typevars 0 tvs) tm;;",
        f"let {matcher} a b =",
        "  aconv a b ||",
        f"  (try aconv ({canonicalize} a) ({canonicalize} b)",
        "   with Failure _ -> false);;",
    ]


def build_claim_probe(
    source_bytes: bytes,
    claims: list[dict[str, Any]],
    *,
    nonce: str | None = None,
    prefix_bytes: bytes = b"",
) -> tuple[bytes, dict[str, Any]]:
    """Append one exact probe suffix while preserving the supplied source bytes."""

    source_bytes.decode("utf-8", errors="strict")
    selected = _validated_claims(claims)
    token = _validated_nonce(nonce)
    matcher = f"hol_workbench_probe_matches_{token}"
    rows: list[dict[str, Any]] = []
    lines = _strict_matcher_source(token)
    for ordinal, claim in enumerate(selected):
        name = str(claim["name"])
        ok_marker = f"{PROBE_OK_PREFIX}:{token}:{ordinal:08d}"
        mismatch_marker = f"{PROBE_MISMATCH_PREFIX}:{token}:{ordinal:08d}"
        quote = claim.get("statement_quote")
        if claim.get("statement_extractable"):
            if not isinstance(quote, str) or len(quote) < 2 or not quote.startswith("`") or not quote.endswith("`"):
                raise ClaimProbeContractError(f"literal theorem {name} lacks one exact closed HOL quotation")
            quote_hash = sha256_text(quote)
            if quote_hash != claim.get("statement_quote_sha256"):
                raise ClaimProbeContractError(f"literal theorem {name} quotation hash does not match its exact bytes")
            th = f"hol_workbench_probe_th_{ordinal:08d}_{token}"
            lines.append(
                f"let ({th} : thm) = {name} in "
                f"if hyp {th} = [] && {matcher} (concl {th}) {quote} "
                f"then print_endline {ocaml_string_literal(ok_marker)} "
                f"else print_endline {ocaml_string_literal(mismatch_marker)};;"
            )
            verification_kind = "kernel_conclusion_and_empty_hypotheses"
        else:
            quote_hash = None
            lines.append(f"let (_ : thm) = {name} in print_endline {ocaml_string_literal(ok_marker)};;")
            verification_kind = "binding_and_thm_type_only_nonliteral_statement"
        rows.append(
            {
                "ordinal": ordinal,
                "name": name,
                "ok_marker": ok_marker,
                "mismatch_marker": mismatch_marker,
                "verification_kind": verification_kind,
                "statement_quote_sha256": quote_hash,
                "statement_quote_sha256_role": (
                    "exact layout-sensitive source quotation provenance; not semantic theorem equivalence"
                    if quote_hash is not None
                    else None
                ),
            }
        )
    completion_marker = f"{PROBE_DONE_PREFIX}:{token}"
    lines.append(f"print_endline {ocaml_string_literal(completion_marker)};;")
    suffix = ("\n".join(lines) + "\n").encode("utf-8")
    source_payload = source_bytes + b"\n" + suffix
    payload = prefix_bytes + (b"\n" if prefix_bytes else b"") + source_payload
    return payload, {
        "schema": PROBE_SCHEMA,
        "nonce": token,
        "source_sha256": sha256_bytes(source_bytes),
        "source_byte_count": len(source_bytes),
        "prefix_sha256": sha256_bytes(prefix_bytes) if prefix_bytes else None,
        "prefix_byte_count": len(prefix_bytes),
        "generated_separator_hex": "0a",
        "suffix_sha256": sha256_bytes(suffix),
        "suffix_byte_count": len(suffix),
        "executed_payload_sha256": sha256_bytes(payload),
        "completion_marker": completion_marker,
        "claims": rows,
        "statement_hash_boundary": (
            "quotation and source hashes are layout-sensitive provenance; kernel matching uses HOL term equality"
        ),
    }


def load_instrumented_source_command(path: Path) -> bytes:
    """Load one absolute instrumented file and make included-file errors fatal."""

    absolute = path.resolve()
    return (f"use_file_raise_failure := true;;\nloadt {ocaml_string_literal(str(absolute))};;\n").encode()


def vanilla_source_context(
    source: Path,
    *,
    workbench_dir: Path,
) -> tuple[str, tuple[dict[str, str], ...]]:
    """Select only the public registry context implied by the exact source."""

    manifest = load_public_profile_manifest(workbench_dir)
    inference = infer_public_profile_for_source(source, public_profile_names(manifest))
    if inference.status != "selected" or inference.profile is None:
        raise ClaimProbeContractError(
            f"source context inference refused ({inference.inferred_profile}): {inference.reason}"
        )
    profile = manifest["profiles"].get(inference.profile)
    if not isinstance(profile, dict):
        raise ClaimProbeContractError(f"selected public source context {inference.profile!r} has no profile record")
    declarations = logical_source_root_declarations(
        profile.get("logical_source_roots"),
        profile=inference.profile,
    )
    return inference.profile, declarations


def validate_vanilla_source_snapshot(closure: dict[str, Any], source_bytes: bytes) -> None:
    """Bind the initially inventoried source bytes to the captured closure."""

    entrypoint = closure.get("entrypoint")
    source_sha256 = sha256_bytes(source_bytes)
    source_size = len(source_bytes)
    if (
        not isinstance(entrypoint, dict)
        or entrypoint.get("sha256") != source_sha256
        or entrypoint.get("size_bytes") != source_size
    ):
        captured_sha256 = entrypoint.get("sha256") if isinstance(entrypoint, dict) else None
        captured_size = entrypoint.get("size_bytes") if isinstance(entrypoint, dict) else None
        raise DependencyPackageError(
            "refused_source_changed",
            "entrypoint bytes changed while capturing the cold dependency closure: "
            f"initial sha256/size {source_sha256}/{source_size}, "
            f"captured {captured_sha256}/{captured_size}; rerun prove SOURCE",
        )


# Preserve the ordinary standalone replay API while sharing exact ELF coordinates.
vanilla_artifact_runtime_cwd = literal_elf_artifact_runtime_cwd


def default_hol_environment(holdir: Path) -> dict[str, str]:
    env = dict(os.environ)
    stublibs = holdir / "_opam" / "lib" / "stublibs"
    env["HOLLIGHT_DIR"] = str(holdir)
    env["HOLLIGHT_USE_MODULE"] = "1"
    env["LINE_EDITOR"] = "cat"
    env["PATH"] = str(holdir / "_opam" / "bin") + os.pathsep + env.get("PATH", "")
    current = env.get("CAML_LD_LIBRARY_PATH", "")
    parts = [str(stublibs), *(part for part in current.split(os.pathsep) if part)]
    env["CAML_LD_LIBRARY_PATH"] = os.pathsep.join(dict.fromkeys(parts))
    return env


def diagnostic_claim_lines(transcript: str, names: list[str]) -> dict[str, list[int]]:
    """Locate ordinary toplevel output for debugging, never as evidence."""

    requested = set(names)
    found: dict[str, list[int]] = {name: [] for name in names}
    patterns = (
        re.compile(rf"\bproved\s+(?P<name>{HOL_CLAIM_NAME})(?![A-Za-z0-9_'])"),
        re.compile(rf"\bval\s+(?P<name>{HOL_CLAIM_NAME})\s*:\s*thm\b"),
    )
    for index, line in enumerate(transcript.splitlines(), 1):
        for pattern in patterns:
            for match in pattern.finditer(line):
                if match.group("name") in requested:
                    found[match.group("name")].append(index)
    return {name: lines for name, lines in found.items() if lines}


def _exact_marker_lines(transcript: bytes, contract: dict[str, Any]) -> dict[bytes, list[int]]:
    markers = {str(contract["completion_marker"]).encode("ascii")}
    for row in contract.get("claims") or []:
        markers.add(str(row["ok_marker"]).encode("ascii"))
        markers.add(str(row["mismatch_marker"]).encode("ascii"))
    found: dict[bytes, list[int]] = {marker: [] for marker in markers}
    for index, line in enumerate(transcript.splitlines(), 1):
        if line in found:
            found[line].append(index)
    return found


def first_error(transcript: str) -> tuple[int | None, str | None]:
    """Return a bounded exception block, including the useful wrapped message."""

    lines = transcript.splitlines()
    for offset, line in enumerate(lines):
        stripped = line.strip().removeprefix("# ").strip()
        if not (FAILURE_LINE_RE.search(stripped) or re.search(r"\bException(?: raised)?:", stripped)):
            continue
        message = [stripped]
        for continuation in lines[offset + 1:offset + 14]:
            text = continuation.strip()
            if not text or text.startswith(("val ", "# ", "__HOL_", "HOL_WORKBENCH_", "Error in included file")):
                break
            if not continuation[:1].isspace() and not FAILURE_LINE_RE.match(text):
                break
            message.append(text)
        return offset + 1, " ".join(message)[:2400]
    return None, None


def diagnostic_claim_output(source: list[str], lines: list[int]) -> list[dict[str, Any]]:
    """Keep bounded printed theorem text, explicitly separate from kernel probes."""

    output = []
    for lineno in lines[-3:]:
        offset = lineno - 1
        first = source[offset]
        text = [first]
        for following in source[offset + 1:offset + 28]:
            stripped = following.strip()
            if not stripped or stripped.startswith(("val ", "# ", "__HOL_", "HOL_WORKBENCH_")):
                break
            if not following[:1].isspace() and not stripped.startswith("|-"):
                break
            text.append(following)
        full = "\n".join(text)
        bounded = full[:4096]
        # This is transcript formatting, not a HOL term parser or proof check.
        header, separator, conclusion = bounded.partition("=")
        output.append({
            "transcript_line": lineno,
            "text": bounded,
            "printed_conclusion": strip_ansi(conclusion.strip()) if separator and ": thm" in header else None,
            "truncated": len(full) > len(bounded) or len(text) == 28,
            "verified": False,
        })
    return output


def binding_status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Count all recorded claims; diagnostic output never contributes to proved."""

    counts = dict.fromkeys(("proved", "failed", "printed_unprobed", "missing", "unknown"), 0)
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def claim_target_row(claim: dict) -> dict:
    return {
        "name": claim.get("theorem"),
        "status": claim.get("status"),
        "evidence_class": claim.get("evidence"),
        "evidence_boundary": EVIDENCE_BOUNDARY,
        "observed_transcript_line": claim.get("observed_transcript_line"),
        "first_error_transcript_line": claim.get("first_error_transcript_line"),
        "claim_json": claim.get("claim_json"),
    }


def target_pack_status(claim_docs: list[dict], *, child_status: int) -> str:
    statuses = {str(claim.get("status") or "unknown") for claim in claim_docs}
    if child_status == 0 and statuses == {"proved"}:
        return "complete"
    if "failed" in statuses or child_status != 0:
        return "failed"
    if statuses & {"missing", "unknown", "printed_unprobed"}:
        return "partial"
    return "failed"


def target_pack_summary(claim_docs: list[dict], *, child_status: int) -> dict:
    rows = [claim_target_row(claim) for claim in claim_docs]
    return {
        "overall_status": target_pack_status(claim_docs, child_status=child_status),
        "required_targets": [row["name"] for row in rows],
        "helper_targets": [],
        "targets": rows,
        "evidence_boundary": EVIDENCE_BOUNDARY,
    }


def claim_doc(
    claim: dict,
    *,
    status: str,
    evidence: str,
    observed_line: int | None,
    first_error_line: str | None,
    first_error_lineno: int | None,
    verification_kind: str,
    marker_count: int,
    mismatch_marker_count: int,
    diagnostic_lines: list[int],
    diagnostic_output: list[dict[str, Any]] | None = None,
) -> dict:
    return {
        "schema": VANILLA_CLAIM_SCHEMA,
        "theorem": claim.get("name"),
        "source": claim.get("source"),
        "source_sha256": claim.get("source_sha256"),
        "statement": claim.get("statement"),
        "statement_sha256": sha256_text(claim.get("statement")),
        "statement_sha256_role": (
            "layout-sensitive normalized static source representation; not semantic theorem equivalence"
        ),
        "statement_quote_sha256": claim.get("statement_quote_sha256"),
        "statement_quote_sha256_role": (
            "exact layout-sensitive source quotation provenance; not semantic theorem equivalence"
            if claim.get("statement_quote_sha256")
            else None
        ),
        "source_span": [claim.get("source_line"), claim.get("statement_end_line") or claim.get("source_line")],
        "status": status,
        "evidence": evidence,
        "verification_kind": verification_kind,
        "probe_marker_count": marker_count,
        "probe_mismatch_marker_count": mismatch_marker_count,
        "observed_transcript_line": observed_line,
        "first_error_line": first_error_line,
        "first_error_transcript_line": first_error_lineno,
        "natural_output_diagnostic_lines": diagnostic_lines,
        "natural_output_diagnostic": diagnostic_output or [],
        "natural_output_is_evidence": False,
        "raw_log_policy": RAW_LOG_POLICY,
    }


def account_claims(
    claims: list[dict[str, Any]], transcript: bytes, contract: dict[str, Any]
) -> tuple[list[dict], dict]:
    """Account only exact nonce-bound full-line markers in raw transcript bytes."""

    selected = _validated_claims(claims)
    if [str(claim["name"]) for claim in selected] != [str(row["name"]) for row in contract.get("claims") or []]:
        raise ClaimProbeContractError("claim inventory does not match probe contract order")
    names = [str(claim.get("name")) for claim in claims if claim.get("name")]
    diagnostic = transcript.decode("utf-8", errors="replace")
    natural = diagnostic_claim_lines(diagnostic, names)
    diagnostic_lines = diagnostic.splitlines()
    error_lineno, error_line = first_error(diagnostic)
    marker_lines = _exact_marker_lines(transcript, contract)
    docs: list[dict] = []
    for claim, probe in zip(selected, contract["claims"], strict=True):
        ok_lines = marker_lines[str(probe["ok_marker"]).encode("ascii")]
        mismatch_lines = marker_lines[str(probe["mismatch_marker"]).encode("ascii")]
        if len(ok_lines) == 1 and not mismatch_lines:
            status = "proved"
            evidence = (
                "nonce_probe_kernel_conclusion"
                if probe["verification_kind"] == "kernel_conclusion_and_empty_hypotheses"
                else "nonce_probe_binding_only"
            )
            observed_line = ok_lines[0]
        elif len(mismatch_lines) == 1 and not ok_lines:
            status = "failed"
            evidence = "nonce_probe_conclusion_mismatch"
            observed_line = mismatch_lines[0]
        elif len(ok_lines) > 1 or len(mismatch_lines) > 1 or (ok_lines and mismatch_lines):
            status = "failed"
            evidence = "nonce_probe_marker_ambiguous"
            observed_line = None
        else:
            printed = bool(natural.get(str(claim["name"])))
            status = "printed_unprobed" if printed else "missing"
            evidence = "unverified_toplevel_output" if printed else "nonce_probe_marker_missing"
            observed_line = None
        docs.append(
            claim_doc(
                claim,
                status=status,
                evidence=evidence,
                observed_line=observed_line,
                first_error_line=None,
                first_error_lineno=None,
                verification_kind=str(probe["verification_kind"]),
                marker_count=len(ok_lines),
                mismatch_marker_count=len(mismatch_lines),
                diagnostic_lines=natural.get(str(claim["name"]), []),
                diagnostic_output=diagnostic_claim_output(diagnostic_lines, natural.get(str(claim["name"]), [])),
            )
        )
    completion_lines = marker_lines[str(contract["completion_marker"]).encode("ascii")]
    meta = {
        "first_error_line": error_line,
        "first_error_transcript_line": error_lineno,
        "completion_marker_count": len(completion_lines),
        "completion_marker_observed": len(completion_lines) == 1,
        "completion_marker_valid": len(completion_lines) == 1,
        "observed_claims": [doc["theorem"] for doc in docs if doc["status"] == "proved"],
        "natural_output_diagnostics": natural,
        "natural_output_is_evidence": False,
        "probe_contract": contract,
    }
    return docs, meta


@dataclass
class VanillaOptions:
    source: Path
    holdir: Path
    cwd: Path
    run_root: Path
    timeout: float | None
    label: str | None


def run_vanilla(options: VanillaOptions) -> int:
    source = options.source.resolve()
    source_bytes = source.read_bytes()
    claims = [claim for claim in extract_hol_theorems_bytes(source, source_bytes) if claim.get("name")]
    if not claims:
        raise SystemExit(f"proof-run vanilla found no named claims in {source}")
    workbench_dir = Path(__file__).resolve().parents[1]
    try:
        source_context_profile, logical_roots = vanilla_source_context(
            source,
            workbench_dir=workbench_dir,
        )
        holdir = options.holdir.expanduser().resolve(strict=True)
        if not holdir.is_dir():
            raise OSError(f"cold HOLDIR is not a directory: {holdir}")
        closure, captured_holdir = capture_source_dependency_closure(
            source,
            profile_cwd=None,
            legacy_holdir_roots=(),
            logical_source_root_declarations=logical_roots,
            holdir_root_override=holdir,
        )
        validate_vanilla_source_snapshot(closure, source_bytes)
        if captured_holdir != holdir:
            raise ClaimProbeContractError("captured cold HOLDIR does not match --holdir")
    except (
        ClaimProbeContractError,
        DependencyPackageError,
        LogicalSourceRootError,
        OSError,
        ProfileRegistryError,
        SourceDependencyInferenceError,
        ValueError,
    ) as exc:
        status = getattr(exc, "status", "refused_source_context")
        raise SystemExit(f"proof-run vanilla blocked before HOL launch: {status}; {exc}") from exc
    label = options.label or source.stem
    run_dir = options.run_root / f"{stamp()}-{os.getpid()}-{slugify(label, fallback='vanilla')}"
    claims_dir = run_dir / "claims"
    run_dir.mkdir(parents=True, exist_ok=False)
    claims_dir.mkdir(parents=True, exist_ok=True)
    raw_log = run_dir / "vanilla.raw.log"
    command_json = run_dir / "command.json"
    run_json = run_dir / "vanilla-run.json"
    card = run_dir / "vanilla-card.txt"
    receipt = run_dir / "proof-receipt.md"
    closure_json = run_dir / "source-dependency-closure.json"
    package_json = run_dir / "dependency-package.json"
    package_root = run_dir / "source-package"
    virtual_entrypoint = dependency_package_entrypoint(package_root, closure)
    artifact_runtime_cwd = vanilla_artifact_runtime_cwd(
        closure,
        original_cwd=options.cwd,
        package_root=package_root,
    )
    prefix = source_execution_prelude(
        package_root=package_root,
        virtual_entrypoint=virtual_entrypoint,
        closure=closure,
        profile_satisfaction=None,
        literal_elf_runtime_cwd=artifact_runtime_cwd,
    )
    payload, probe_contract = build_claim_probe(
        source_bytes,
        claims,
        prefix_bytes=prefix,
    )
    try:
        instrumented_source, package = materialize_dependency_package(
            source=source,
            closure=closure,
            destination=package_root,
            entrypoint_output_bytes=payload,
        )
    except (DependencyPackageError, OSError) as exc:
        status = getattr(exc, "status", "refused_dependency_changed")
        raise SystemExit(f"proof-run vanilla blocked before HOL launch: {status}; {exc}") from exc
    if instrumented_source != virtual_entrypoint or instrumented_source.read_bytes() != payload:
        raise SystemExit("proof-run vanilla blocked before HOL launch: instrumented dependency package changed")
    if artifact_runtime_cwd is not None:
        artifact_runtime_cwd.mkdir(parents=True, exist_ok=True)
        package["runtime_cwd"] = str(artifact_runtime_cwd)
    atomic_write_json(closure_json, closure)
    atomic_write_json(package_json, package)
    loader_command = load_instrumented_source_command(instrumented_source)
    started = time.time()
    command = default_warm_startup_command(holdir)
    atomic_write_json(
        command_json,
        {
            "argv": command,
            "cwd": str(options.cwd),
            "source": str(source),
            "timeout_seconds": options.timeout,
            "environment": "HOL Light vanilla toplevel transcript",
            "source_context_profile": source_context_profile,
            "source_dependency_closure": str(closure_json),
            "dependency_package": str(package_json),
            "artifact_runtime_cwd": str(artifact_runtime_cwd) if artifact_runtime_cwd is not None else None,
            "source_sha256": sha256_bytes(source_bytes),
            "probe_suffix_sha256": probe_contract["suffix_sha256"],
            "executed_payload_sha256": probe_contract["executed_payload_sha256"],
            "instrumented_source": str(instrumented_source),
            "loader_command_sha256": sha256_bytes(loader_command),
            "probe_contract": probe_contract,
        },
    )
    print("proof-run vanilla")
    print(f"source: {source}")
    print(f"run dir: {run_dir}")
    print("evidence: nonce_bound_claim_probe")
    print(f"evidence_boundary: {EVIDENCE_BOUNDARY}")
    print("")
    try:
        proc = subprocess.run(
            command,
            cwd=str(options.cwd),
            input=loader_command,
            text=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=default_hol_environment(holdir),
            timeout=options.timeout,
            check=False,
        )
        child_status = proc.returncode
        transcript = proc.stdout or b""
    except subprocess.TimeoutExpired as exc:
        child_status = 124
        transcript = b""
        if isinstance(exc.stdout, bytes):
            transcript = cast(bytes, exc.stdout)
        transcript += f"\n[vanilla run timed out after {options.timeout}s]\n".encode()
    raw_log.write_bytes(transcript)
    claim_docs, accounting = account_claims(claims, transcript, probe_contract)
    for claim in claim_docs:
        path = claims_dir / f"{slugify(str(claim.get('theorem') or 'claim'), fallback='claim')}.json"
        claim["claim_json"] = str(path)
        atomic_write_json(path, claim)
    status = (
        "proved"
        if child_status == 0
        and all(claim["status"] == "proved" for claim in claim_docs)
        and accounting["completion_marker_valid"]
        else "failed"
    )
    target_pack = target_pack_summary(claim_docs, child_status=child_status)
    finished = time.time()
    summary = {
        "schema": VANILLA_RUN_SCHEMA,
        "status": status,
        "overall_status": target_pack["overall_status"],
        "source": str(source),
        "source_sha256": sha256_bytes(source_bytes),
        "source_byte_count": len(source_bytes),
        "source_context_profile": source_context_profile,
        "source_dependency_closure_sha256": closure.get("strict_sha256"),
        "source_dependency_semantic_identity_complete": closure.get("semantic_identity_complete"),
        "dependency_transport_status": package.get("dependency_transport_status"),
        "artifact_runtime_cwd": str(artifact_runtime_cwd) if artifact_runtime_cwd is not None else None,
        "probe_suffix_sha256": probe_contract["suffix_sha256"],
        "executed_payload_sha256": probe_contract["executed_payload_sha256"],
        "run_dir": str(run_dir),
        "child_exit_status": child_status,
        "process_exit_status": child_status,
        "elapsed_seconds": round(finished - started, 3),
        "claim_count": len(claim_docs),
        "claims": claim_docs,
        "target_pack": target_pack,
        "accounting": accounting,
        "artifacts": {
            "card": str(card),
            "receipt": str(receipt),
            "raw_log": str(raw_log),
            "command_json": str(command_json),
            "claims_dir": str(claims_dir),
            "instrumented_source": str(instrumented_source),
            "source_dependency_closure": str(closure_json),
            "dependency_package": str(package_json),
        },
        "evidence": "nonce_bound_claim_probe",
        "evidence_boundary": EVIDENCE_BOUNDARY,
        "raw_log_policy": RAW_LOG_POLICY,
    }
    atomic_write_json(run_json, summary)
    lines = [
        "VANILLA CLAIM ACCOUNTING CARD",
        "=============================",
        f"status: {status}",
        f"overall_status: {target_pack['overall_status']}",
        f"source: {source}",
        f"source_context_profile: {source_context_profile}",
        f"dependency_transport_status: {package.get('dependency_transport_status')}",
        f"child_exit_status: {child_status}",
        f"process_exit_status: {child_status}",
        f"claim_count: {len(claim_docs)}",
        f"first_error_line: {accounting['first_error_line'] or '-'}",
        f"completion_marker_count: {accounting['completion_marker_count']}",
        "",
        "claims:",
    ]
    for claim in claim_docs:
        lines.append(f"- {claim['theorem']}: {claim['status']} ({claim['evidence']})")
        if claim.get("first_error_line"):
            lines.append(f"  first_error: {claim['first_error_line']}")
    lines += [
        "",
        "trust boundary:",
        "- evidence is only an exact nonce-bound full-line marker in raw transcript bytes",
        "- literal statements are checked by kernel conclusion matching with empty hypotheses",
        "- nonliteral statements receive an explicitly binding-only typed check",
        "- ordinary val/proved output is diagnostic only",
        "- process exit status is reported separately from proof/target-pack status",
        "",
        "canonical reading order:",
        f"1. vanilla card: {card}",
        f"2. run JSON: {run_json}",
        f"3. per-claim JSON files: {claims_dir}",
        f"4. raw log, targeted grep/tail only: {raw_log}",
        "",
        "raw log policy:",
        f"- {RAW_LOG_POLICY}",
    ]
    write_text_lines(card, lines)
    receipt_lines = [
        "# Vanilla Claim Accounting Receipt",
        "",
        f"- status: {status}",
        f"- overall_status: {target_pack['overall_status']}",
        f"- source: {source}",
        f"- source_context_profile: {source_context_profile}",
        f"- dependency_transport_status: {package.get('dependency_transport_status')}",
        f"- child_exit_status: {child_status}",
        f"- process_exit_status: {child_status}",
        f"- claim_count: {len(claim_docs)}",
        "- evidence: nonce_bound_claim_probe",
        f"- evidence_boundary: {EVIDENCE_BOUNDARY}",
        f"- vanilla_card: {card}",
        f"- vanilla_run_json: {run_json}",
        f"- raw_log: {raw_log}",
    ]
    write_text_lines(receipt, receipt_lines)
    print("vanilla summary")
    print(f"status: {status}")
    print(f"claims: {len(claim_docs)}")
    print(f"first error: {accounting['first_error_line'] or '-'}")
    print(f"completion marker count: {accounting['completion_marker_count']}")
    print(f"vanilla card: {card}")
    print(f"vanilla run json: {run_json}")
    print(f"proof receipt: {receipt}")
    return 0 if status == "proved" else 1


def vanilla_command(args: argparse.Namespace) -> int:
    if not getattr(args, "all", False):
        raise SystemExit("proof-run vanilla currently requires --all for claim accounting")
    options = VanillaOptions(
        source=Path(args.source).expanduser().resolve(),
        holdir=Path(args.holdir).expanduser().resolve(),
        cwd=Path(args.cwd).expanduser().resolve() if args.cwd else Path(args.source).expanduser().resolve().parent,
        run_root=Path(args.run_root).expanduser().resolve(),
        timeout=args.timeout,
        label=args.label,
    )
    return run_vanilla(options)
