"""Claim-set loading, comparison, and presentation for proof-run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hol_workbench.evidence_policy import RAW_LOG_POLICY
from hol_workbench.hashing import sha256_file, sha256_text
from hol_workbench.ids import utc_now
from hol_workbench.jsonio import atomic_write_json, read_json
from hol_workbench.proofs.theorem_scan import extract_hol_theorems

CLAIM_SET_SCHEMA = "proof-run.claim-set.v1"
CLAIM_SET_DIFF_SCHEMA = "proof-run.claim-set-diff.v1"
VANILLA_RUN_SCHEMA = "proof-run.vanilla-run.v1"


def resolve_manifest_path(value: str, *, manifest_dir: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = manifest_dir / path
    return str(path.resolve())


def claim_set_record(
    *,
    theorem: str,
    source: str | None = None,
    source_sha256: str | None = None,
    statement_sha256: str | None = None,
    statement_source: str | None = None,
    statement_extractable: bool | None = None,
    statement_line: int | None = None,
    statement_end_line: int | None = None,
    proof_constructor: str | None = None,
    proof_status: str = "unknown",
    evidence: str | None = None,
    verification_status: str | None = None,
    run_dir: str | None = None,
    target_json: str | None = None,
) -> dict:
    record = {
        "theorem": theorem,
        "source": source,
        "source_sha256": source_sha256,
        "statement_sha256": statement_sha256,
        "statement_source": statement_source,
        "statement_extractable": statement_extractable,
        "statement_line": statement_line,
        "statement_end_line": statement_end_line,
        "proof_constructor": proof_constructor,
        "proof_status": proof_status or "unknown",
        "evidence": evidence,
        "verification_status": verification_status,
        "run_dir": run_dir,
        "target_json": target_json,
    }
    return {key: value for key, value in record.items() if value is not None}


def claim_set_document(*, kind: str, path: str | None, claims: list[dict], warnings: list[str] | None = None) -> dict:
    sorted_claims = sorted(claims, key=lambda item: (str(item.get("theorem") or ""), str(item.get("source") or "")))
    counts: dict[str, int] = {}
    for claim in sorted_claims:
        theorem = str(claim.get("theorem") or "")
        counts[theorem] = counts.get(theorem, 0) + 1
    duplicate_theorems = sorted(name for name, count in counts.items() if name and count > 1)
    all_warnings = list(warnings or [])
    if duplicate_theorems:
        all_warnings.append("duplicate theorem name(s): " + ", ".join(duplicate_theorems))
    return {
        "schema": CLAIM_SET_SCHEMA,
        "created_utc": utc_now(),
        "source": {"kind": kind, "path": path},
        "claim_count": len(sorted_claims),
        "claims": sorted_claims,
        "duplicate_theorems": duplicate_theorems,
        "warnings": all_warnings,
        "raw_log_policy": RAW_LOG_POLICY,
    }


def load_claim_set_from_sources(paths: list[str]) -> dict:
    claims = []
    resolved_paths = []
    for raw_path in paths:
        source = Path(raw_path).expanduser().resolve()
        resolved_paths.append(str(source))
        if not source.exists():
            raise SystemExit(f"source not found: {source}")
        if not source.is_file():
            raise SystemExit(f"source is not a file: {source}")
        for claim in extract_hol_theorems(source):
            claims.append(
                claim_set_record(
                    theorem=claim["name"],
                    source=str(source),
                    source_sha256=claim.get("source_sha256") or sha256_file(source),
                    statement_sha256=sha256_text(claim.get("statement")),
                    statement_source=claim.get("statement_source"),
                    statement_extractable=claim.get("statement_extractable"),
                    statement_line=claim.get("statement_line"),
                    statement_end_line=claim.get("statement_end_line"),
                    proof_constructor=claim.get("proof_constructor"),
                )
            )
    source_path = resolved_paths[0] if len(resolved_paths) == 1 else f"{len(resolved_paths)} source files"
    return claim_set_document(kind="source", path=source_path, claims=claims)


def load_claim_set_from_manifest(path: Path) -> dict:
    manifest_path = path.expanduser().resolve()
    manifest = read_json(manifest_path)
    if not manifest:
        raise SystemExit(f"manifest not found or empty: {manifest_path}")
    raw_targets = manifest.get("targets")
    if not isinstance(raw_targets, list):
        raise SystemExit("manifest must contain a targets list")
    claims = []
    for raw in raw_targets:
        if not isinstance(raw, dict):
            raise SystemExit("manifest targets must be objects")
        theorem = raw.get("theorem") or raw.get("name") or raw.get("id")
        if not theorem:
            raise SystemExit(f"manifest target is missing theorem/name/id: {raw!r}")
        source = raw.get("source")
        claims.append(
            claim_set_record(
                theorem=str(theorem),
                source=resolve_manifest_path(str(source), manifest_dir=manifest_path.parent) if source else None,
                source_sha256=raw.get("source_sha256"),
                statement_sha256=raw.get("statement_sha256"),
                statement_source=raw.get("statement_source"),
                statement_extractable=raw.get("statement_extractable"),
                statement_line=raw.get("statement_line"),
                statement_end_line=raw.get("statement_end_line"),
                proof_constructor=raw.get("proof_constructor"),
                proof_status=raw.get("status") or "unknown",
                evidence=raw.get("evidence"),
                verification_status=raw.get("verification_status"),
            )
        )
    return claim_set_document(kind="manifest", path=str(manifest_path), claims=claims)


def resolve_artifact_path(value: str | None, *, base: Path) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def claim_set_record_from_target_json(target_json_path: Path, *, row: dict | None = None) -> dict | None:
    target_json_path = target_json_path.expanduser().resolve()
    target = read_json(target_json_path)
    if not target:
        return None
    claim_path = resolve_artifact_path(target.get("claim"), base=target_json_path.parent)
    claim = read_json(claim_path) if claim_path else {}
    verification = target.get("claim_verification") or (claim.get("verification") or {})
    source = claim.get("source") or {}
    row = row or {}
    theorem = target.get("claim_name") or claim.get("name") or row.get("theorem") or row.get("id")
    if not theorem:
        return None
    return claim_set_record(
        theorem=str(theorem),
        source=source.get("path") or row.get("source"),
        source_sha256=source.get("sha256"),
        statement_sha256=claim.get("statement_sha256") or row.get("statement_sha256"),
        statement_source=claim.get("statement_source") or row.get("statement_source"),
        statement_extractable=claim.get("statement_extractable")
        if "statement_extractable" in claim
        else row.get("statement_extractable"),
        statement_line=source.get("statement_line") or row.get("statement_line"),
        statement_end_line=source.get("statement_end_line") or row.get("statement_end_line"),
        proof_constructor=claim.get("proof_constructor")
        or target.get("proof_constructor")
        or row.get("proof_constructor"),
        proof_status=target.get("status") or row.get("status") or "unknown",
        evidence=verification.get("evidence_class") or row.get("evidence"),
        verification_status=verification.get("status") or row.get("verification_status"),
        run_dir=row.get("run_dir"),
        target_json=str(target_json_path),
    )


def load_claim_set_from_batch_summary(path: Path) -> dict:
    summary_path = path.expanduser().resolve()
    summary = read_json(summary_path)
    if not summary:
        raise SystemExit(f"batch summary not found or empty: {summary_path}")
    raw_targets = summary.get("targets")
    if not isinstance(raw_targets, list):
        raise SystemExit("batch summary must contain a targets list")
    claims = []
    warnings = []
    for row in raw_targets:
        if not isinstance(row, dict):
            raise SystemExit("batch summary targets must be objects")
        target_json_path = resolve_artifact_path(row.get("target_json"), base=summary_path.parent)
        record = claim_set_record_from_target_json(target_json_path, row=row) if target_json_path else None
        if record is None:
            theorem = row.get("theorem") or row.get("id")
            if not theorem:
                warnings.append(f"skipped batch row without theorem or target_json: {row!r}")
                continue
            record = claim_set_record(
                theorem=str(theorem),
                source=row.get("source"),
                statement_sha256=row.get("statement_sha256"),
                proof_status=row.get("status") or "unknown",
                evidence=row.get("evidence"),
                run_dir=row.get("run_dir"),
                target_json=row.get("target_json"),
            )
        claims.append(record)
    return claim_set_document(kind="batch-summary", path=str(summary_path), claims=claims, warnings=warnings)


def load_claim_set_from_vanilla_run(run_dir: Path, run: dict) -> dict:
    claims = []
    warnings = []
    accounting = run.get("accounting") or {}
    run_observed = (
        run.get("status") == "proved"
        and run.get("child_exit_status") == 0
        and accounting.get("completion_marker_valid") is True
    )
    if not run_observed:
        warnings.append(
            "vanilla run is not a completed zero-exit proved receipt; individual claims are not marked observed"
        )
    for row in run.get("claims") or []:
        theorem = row.get("theorem")
        if not theorem:
            warnings.append(f"skipped vanilla claim row without theorem: {row!r}")
            continue
        source_span = row.get("source_span") or []
        statement_line = source_span[0] if len(source_span) >= 1 else None
        statement_end_line = source_span[1] if len(source_span) >= 2 else None
        claim_observed = (
            run_observed
            and row.get("status") == "proved"
            and str(row.get("evidence") or "").startswith("nonce_probe_")
        )
        claims.append(
            claim_set_record(
                theorem=str(theorem),
                source=row.get("source") or run.get("source"),
                source_sha256=row.get("source_sha256") or run.get("source_sha256"),
                statement_sha256=row.get("statement_sha256"),
                statement_source=row.get("statement_source"),
                statement_extractable=row.get("statement_extractable"),
                statement_line=statement_line,
                statement_end_line=statement_end_line,
                proof_constructor=row.get("proof_constructor"),
                proof_status=row.get("status") or "unknown",
                evidence=row.get("evidence"),
                verification_status="observed" if claim_observed else None,
                run_dir=str(run_dir),
                target_json=row.get("claim_json"),
            )
        )
    return claim_set_document(
        kind="vanilla-run-dir",
        path=str(run_dir),
        claims=claims,
        warnings=warnings,
    )


def load_claim_set_from_run_dir(path: Path) -> dict:
    run_dir = path.expanduser().resolve()
    run = read_json(run_dir / "run.json")
    if not run:
        vanilla_run = read_json(run_dir / "vanilla-run.json")
        if not vanilla_run:
            raise SystemExit(f"neither run.json nor vanilla-run.json found under {run_dir}")
        if vanilla_run.get("schema") != VANILLA_RUN_SCHEMA:
            raise SystemExit(
                f"unsupported vanilla-run.json schema under {run_dir}: {vanilla_run.get('schema')!r}"
            )
        return load_claim_set_from_vanilla_run(run_dir, vanilla_run)
    claims = []
    warnings = []
    for row in run.get("targets") or []:
        target_json_path = resolve_artifact_path(row.get("target_json"), base=run_dir)
        record = claim_set_record_from_target_json(target_json_path, row=row) if target_json_path else None
        if record:
            record["run_dir"] = str(run_dir)
            claims.append(record)
        else:
            warnings.append(f"could not read target claim from row: {row!r}")
    return claim_set_document(kind="run-dir", path=str(run_dir), claims=claims, warnings=warnings)


def load_claim_set_file(path: Path) -> dict:
    claim_set_path = path.expanduser().resolve()
    data = read_json(claim_set_path)
    if not data:
        raise SystemExit(f"claim-set baseline not found or empty: {claim_set_path}")
    if data.get("schema") != CLAIM_SET_SCHEMA:
        raise SystemExit(f"not a {CLAIM_SET_SCHEMA} file: {claim_set_path}")
    return data


def load_claim_set_from_args(args: argparse.Namespace) -> dict:
    if args.sources:
        return load_claim_set_from_sources(args.sources)
    if args.manifest:
        return load_claim_set_from_manifest(Path(args.manifest))
    if args.batch_summary:
        return load_claim_set_from_batch_summary(Path(args.batch_summary))
    if args.run_dir:
        return load_claim_set_from_run_dir(Path(args.run_dir))
    if args.claim_set:
        return load_claim_set_file(Path(args.claim_set))
    raise SystemExit("one claim-set input is required")


def claims_by_theorem(claim_set: dict) -> dict[str, dict]:
    by_name = {}
    for claim in claim_set.get("claims") or []:
        theorem = claim.get("theorem")
        if theorem:
            by_name[str(theorem)] = claim
    return by_name


def claim_has_observed_proof(claim: dict) -> bool:
    if claim.get("proof_status") != "proved":
        return False
    evidence = claim.get("evidence")
    verification = claim.get("verification_status")
    return (
        evidence in {"semantic_probe", "observed_output_name", "observed_theorem_binding"} or verification == "observed"
    )


def claim_has_failed_proof(claim: dict) -> bool:
    status = claim.get("proof_status")
    return status not in (None, "unknown", "proved")


def compare_claim_sets(baseline: dict, current: dict) -> dict:
    baseline_by_name = claims_by_theorem(baseline)
    current_by_name = claims_by_theorem(current)
    baseline_names = set(baseline_by_name)
    current_names = set(current_by_name)
    new_theorems = [current_by_name[name] for name in sorted(current_names - baseline_names)]
    deleted_theorems = [baseline_by_name[name] for name in sorted(baseline_names - current_names)]
    statement_changed = []
    proof_failed = []
    proof_still_observed = []
    for name in sorted(baseline_names & current_names):
        old = baseline_by_name[name]
        new = current_by_name[name]
        old_hash = old.get("statement_sha256")
        new_hash = new.get("statement_sha256")
        changed = old_hash != new_hash
        if changed:
            statement_changed.append(
                {
                    "theorem": name,
                    "baseline_statement_sha256": old_hash,
                    "current_statement_sha256": new_hash,
                    "baseline": old,
                    "current": new,
                }
            )
        if claim_has_failed_proof(new):
            proof_failed.append(new)
        if not changed and claim_has_observed_proof(new):
            proof_still_observed.append(new)
    changed_or_regressed = bool(new_theorems or deleted_theorems or statement_changed or proof_failed)
    return {
        "schema": CLAIM_SET_DIFF_SCHEMA,
        "status": "changed" if changed_or_regressed else "clean",
        "baseline": baseline.get("source"),
        "current": current.get("source"),
        "baseline_claim_count": baseline.get("claim_count", len(baseline_by_name)),
        "current_claim_count": current.get("claim_count", len(current_by_name)),
        "new_theorems": new_theorems,
        "deleted_theorems": deleted_theorems,
        "statement_changed": statement_changed,
        "proof_failed": proof_failed,
        "proof_still_observed": proof_still_observed,
        "warnings": (baseline.get("warnings") or []) + (current.get("warnings") or []),
        "raw_log_policy": RAW_LOG_POLICY,
    }


def short_sha(value: str | None) -> str:
    return value[:12] if value else "unknown"


def claim_location(claim: dict) -> str:
    source = claim.get("source") or "unknown source"
    line = claim.get("statement_line")
    return f"{source}:{line}" if line else str(source)


def print_claim_group(title: str, claims: list[dict]) -> None:
    print(f"{title}:")
    if not claims:
        print("- none")
        return
    for claim in claims:
        print(
            f"- {claim.get('theorem')} "
            f"statement={short_sha(claim.get('statement_sha256'))} "
            f"proof={claim.get('proof_status', 'unknown')} "
            f"evidence={claim.get('evidence') or '-'} "
            f"at {claim_location(claim)}"
        )


def print_statement_changes(changes: list[dict]) -> None:
    print("STATEMENT CHANGED:")
    if not changes:
        print("- none")
        return
    for change in changes:
        current = change.get("current") or {}
        print(
            f"- {change.get('theorem')} "
            f"{short_sha(change.get('baseline_statement_sha256'))} -> "
            f"{short_sha(change.get('current_statement_sha256'))} "
            f"at {claim_location(current)}"
        )


def print_claim_set_summary(claim_set: dict) -> None:
    print("proof-run claim-set")
    print(f"source: {claim_set.get('source')}")
    print(f"claims: {claim_set.get('claim_count')}")
    warnings = claim_set.get("warnings") or []
    if warnings:
        print("warnings:")
        for item in warnings:
            print(f"- {item}")
    print_claim_group("CLAIMS", claim_set.get("claims") or [])
    print(f"raw log policy: {RAW_LOG_POLICY}")


def print_claim_set_diff(report: dict) -> None:
    print("proof-run claim-set diff")
    print(f"status: {report.get('status')}")
    print(f"baseline: {report.get('baseline')}")
    print(f"current: {report.get('current')}")
    print(f"claims: baseline={report.get('baseline_claim_count')} current={report.get('current_claim_count')}")
    warnings = report.get("warnings") or []
    if warnings:
        print("warnings:")
        for item in warnings:
            print(f"- {item}")
    print("")
    print_claim_group("NEW THEOREM", report.get("new_theorems") or [])
    print_claim_group("DELETED THEOREM", report.get("deleted_theorems") or [])
    print_statement_changes(report.get("statement_changed") or [])
    print_claim_group("PROOF FAILED", report.get("proof_failed") or [])
    print_claim_group("PROOF STILL OBSERVED", report.get("proof_still_observed") or [])
    print(f"raw log policy: {RAW_LOG_POLICY}")


def claim_set_command(args: argparse.Namespace) -> int:
    current = load_claim_set_from_args(args)
    if args.save:
        atomic_write_json(Path(args.save).expanduser().resolve(), current)
    if args.baseline:
        baseline = load_claim_set_file(Path(args.baseline))
        report = compare_claim_sets(baseline, current)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print_claim_set_diff(report)
            if args.save:
                print(f"saved current claim set: {Path(args.save).expanduser().resolve()}")
        return 0 if report["status"] == "clean" else 1
    if args.json:
        print(json.dumps(current, indent=2, sort_keys=True))
    else:
        print_claim_set_summary(current)
        if args.save:
            print(f"saved claim set: {Path(args.save).expanduser().resolve()}")
    return 0
