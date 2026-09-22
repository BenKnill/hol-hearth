#!/usr/bin/env python3
"""Compare a leaf's literal loads with a public profile recipe; never run HOL.

The default is recipe-text evidence only. Deep reports also read the canonical
dependency closure and validated published source inventory; neither runs HOL.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from hol_workbench.cli.inspect import REPLAY_SCHEMA, _replay_receipt
from hol_workbench.cli.published_profile import resolve_published_warm_profile
from hol_workbench.hashing import sha256_bytes
from hol_workbench.logical_source_roots import logical_source_root_declarations
from hol_workbench.proofs.loader_scan import LoaderScanResult, scan_ocaml_loaders
from hol_workbench.source_dependency_package import dependency_transport_status
from hol_workbench.source_execution_plan import capture_source_dependency_closure, decide_profile_satisfaction

REPORT_SCHEMA = "hol-hearth.leaf-needs-report.v1"
REPORT_EVIDENCE = "static_recipe_text_comparison"
RECEIPT_EVIDENCE = "warm_exploration"
BOUNDARY = (
    "Recipe text only: not live shelf admission, not a proof, and not evidence "
    "that the warm image loaded these bytes."
)
REPO_ROOT = Path(__file__).resolve().parents[3]


class LeafNeedsError(ValueError):
    """The comparison would require guessing."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hearth leaf-needs",
        description="Compare a leaf's literal loads with a public profile's checked-in recipe. " + BOUNDARY,
        allow_abbrev=False,
    )
    parser.add_argument("source", help="leaf .ml file")
    parser.add_argument("--profile", required=True, help="public profile name")
    parser.add_argument("--receipt", help="optional prove receipt or run directory for this exact leaf and profile")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--deep", action="store_true",
                        help="read the transitive source/ELF closure and published warm inventory; no HOL or cache writes")
    return parser


def _scan(data: bytes, label: str) -> LoaderScanResult:
    scan = scan_ocaml_loaders(data)
    if scan.status == "refused":
        assert scan.refusal is not None
        raise LeafNeedsError(f"{label} line {scan.refusal.source_line}: {scan.refusal.reason}")
    return scan


def _recipe(profile: str, repo_root: Path) -> tuple[bytes, dict[str, Any]]:
    manifest = json.loads((repo_root / "hol-workbench" / "warmup-profiles.json").read_text(encoding="utf-8"))
    if profile not in manifest.get("public_authoring_profiles", []):
        names = ", ".join(manifest.get("public_authoring_profiles", []))
        raise LeafNeedsError(f"unknown public profile {profile!r}; choose one of: {names}")
    entry = manifest["profiles"][profile]
    data = (repo_root / "profiles" / f"{profile}.ml").read_bytes()
    if data != ("\n".join(entry["base_lines"]) + "\n").encode():
        raise LeafNeedsError(f"profiles/{profile}.ml differs from warmup-profiles.json; run ./hearth check")
    return data, entry


def _receipt_identity(value: str, *, profile: str, source_sha256: str, recipe_sha256: str) -> dict[str, Any]:
    path = _replay_receipt(Path(value).expanduser().absolute())
    if path is None:
        raise LeafNeedsError(f"no {REPLAY_SCHEMA} receipt found at {value}")
    raw = path.read_bytes()
    receipt = json.loads(raw)
    if receipt.get("logical_profile") != profile:
        raise LeafNeedsError(
            f"receipt profile {receipt.get('logical_profile')!r} does not match selected profile {profile!r}")
    if receipt.get("source_sha256") != source_sha256:
        raise LeafNeedsError("receipt source SHA-256 does not match the current leaf bytes")
    recorded_recipe = receipt.get("profile_sha256")
    if recorded_recipe is not None and recorded_recipe != recipe_sha256:
        raise LeafNeedsError("receipt profile SHA-256 does not match the checked-in recipe bytes")
    return {
        "evidence_class": RECEIPT_EVIDENCE,
        "path": str(path),
        "receipt_sha256": sha256_bytes(raw),
        "recorded_evidence": receipt.get("evidence"),
        "logical_profile": profile,
        "source_sha256": source_sha256,
        "profile_sha256": recorded_recipe,
        "profile_basis_id": receipt.get("profile_basis_id"),
        "semantic_source_status": receipt.get("semantic_source_status"),
    }


def build_report(source: Path, profile: str, *, receipt: str | None = None, deep: bool = False,
                 repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    if not source.is_file():
        raise LeafNeedsError(f"leaf source is not a regular file: {source}")
    recipe_data, entry = _recipe(profile, repo_root)
    leaf_data = source.read_bytes()
    recipe_scan = _scan(recipe_data, f"profiles/{profile}.ml")
    leaf_scan = _scan(leaf_data, str(source))
    recipe_paths = {item.path for item in recipe_scan.literal_occurrences if item.family == "source"}

    named: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    reloads: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    dynamic: list[dict[str, Any]] = []
    for item in leaf_scan.occurrences:
        row = {"loader": item.loader, "path": item.path, "line": item.source_line}
        if item.outcome == "dynamic":
            dynamic.append({"loader": item.loader, "line": item.source_line, "reason": item.reason})
        elif item.family == "artifact":
            artifacts.append(row)
        elif item.loader != "needs":
            reloads.append(row)
        elif item.path in recipe_paths:
            named.append(row)
        else:
            missing.append(row)

    roots = [
        {"alias": row.get("alias"), "revision": row.get("revision")}
        for row in entry.get("logical_source_roots") or []
    ]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "evidence_class": REPORT_EVIDENCE,
        "boundary": BOUNDARY,
        "profile": profile,
        "recipe": f"profiles/{profile}.ml",
        "recipe_sha256": sha256_bytes(recipe_data),
        "recipe_cwd_policy": entry.get("cwd_policy"),
        "recipe_source_roots": roots,
        "recipe_literal_loads": [
            {"loader": item.loader, "path": item.path, "line": item.source_line}
            for item in recipe_scan.literal_occurrences if item.family == "source"
        ],
        "leaf": str(source),
        "leaf_sha256": sha256_bytes(leaf_data),
        "needs_named_by_recipe": named,
        "needs_not_in_recipe": missing,
        "non_needs_source_loads": reloads,
        "artifact_loads": artifacts,
        "dynamic_loads": dynamic,
        "receipt": None,
    }
    if receipt is not None:
        report["receipt"] = _receipt_identity(
            receipt, profile=profile, source_sha256=report["leaf_sha256"], recipe_sha256=report["recipe_sha256"])
    if deep:
        inventory: dict[str, Any] = {"status": "unavailable"}
        published = None
        try:
            published = resolve_published_warm_profile(repo_root / "hol-workbench" / "bin", profile)
        except (OSError, RuntimeError, ValueError, SystemExit) as exc:
            inventory["reason"] = str(exc)
        closure, holdir = capture_source_dependency_closure(
            source,
            profile_cwd=published.cwd if published is not None else None,
            legacy_holdir_roots=(published.legacy_holdir_roots if published is not None else
                                tuple(Path(value) for value in entry.get("legacy_holdir_roots", []))),
            logical_source_root_declarations=(published.logical_source_roots if published is not None else
                                              logical_source_root_declarations(
                                                  entry.get("logical_source_roots"), profile=profile)),
            use_analysis_cache=False,
        )
        if closure["entrypoint"]["sha256"] != report["leaf_sha256"]:
            raise LeafNeedsError("leaf bytes changed during the dependency scan; run the report again")
        status, reason = dependency_transport_status(closure)
        satisfaction = None
        if published is not None:
            try:
                satisfaction, status, reason = decide_profile_satisfaction(
                    closure, profile_root=published.root, logical_profile=profile,
                    profile_cwd=published.cwd, holdir_root=holdir,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                status = str(getattr(exc, "status", "refused_profile_satisfaction"))
                reason = str(exc)
                inventory = {"status": "refused", "reason": reason}
            else:
                inventory = {
                    "status": "verified" if satisfaction is not None else "no_matching_dependencies",
                    "evidence_class": "verified_published_source_inventory",
                    "boundary": "Static published inventory validation only; no live execution grant or queue admission.",
                    "profile_satisfaction": satisfaction,
                }
        report["preflight"] = {
            "evidence_class": "static_dependency_closure",
            "boundary": (
                "Current disk inputs under the bounded literal-loader contract; any verified warm inventory is separate evidence. "
                "No queue admission, restore, theorem, or ISA/ABI audit. Generic OCaml effects are outside this contract. "
                "An attached receipt matches leaf/profile identities only; its dependency identity is not checked here."
            ),
            "status": "complete" if status in {"packaged", "packaged_with_profile_satisfaction"} else "incomplete",
            "disk_closure_status": "complete" if closure["semantic_identity_complete"] else "incomplete",
            "holdir": str(holdir) if holdir is not None else None,
            "transport_status": status,
            "transport_reason": reason,
            "warm_inventory": inventory,
            "closure": closure,
        }
    return report


def _rows(title: str, rows: list[dict[str, Any]], note: str) -> None:
    print(f"{title}: {len(rows)}{'' if not rows else '  (' + note + ')'}")
    for row in rows:
        target = row.get("path") if row.get("path") is not None else f"<dynamic: {row.get('reason')}>"
        print(f"  line {row['line']}: {row['loader']} {target}")


def print_report(report: dict[str, Any]) -> None:
    print(f"LEAF NEEDS: {report['leaf']} sha={report['leaf_sha256'][:12]}")
    print(f"PROFILE RECIPE: {report['recipe']} sha={report['recipe_sha256'][:12]} "
          f"cwd_policy={report['recipe_cwd_policy']}")
    for root in report["recipe_source_roots"]:
        print(f"  pinned recipe root, not resolved here: {root['alias']} @ {str(root['revision'])[:12]}")
    print(f"EVIDENCE: {report['evidence_class']}")
    print(f"BOUNDARY: {report['boundary']}")
    _rows("needs named by recipe", report["needs_named_by_recipe"], "literal text match with a recipe load")
    _rows("needs not in recipe", report["needs_not_in_recipe"],
          "not named by recipe text; recipe loads are not followed transitively")
    _rows("other source loads", report["non_needs_source_loads"], "not needs; HOL runs them whatever the recipe loaded")
    _rows("artifact loads", report["artifact_loads"], "object inputs; never covered by a recipe")
    _rows("dynamic loads", report["dynamic_loads"], "not compared")
    if "preflight" in report:
        preflight = report["preflight"]
        closure = preflight["closure"]
        print(f"DEEP PREFLIGHT: {preflight['status']} evidence_class={preflight['evidence_class']}")
        print(f"BOUNDARY: {preflight['boundary']}")
        print(f"DISK CLOSURE: {preflight['disk_closure_status']}; warm inventory checked separately")
        print(f"DEPENDENCY SHA-256: {closure['strict_sha256']}")
        print(f"SOURCE EDGES: {closure['literal_edge_count']}; ELF INPUTS: {closure['literal_artifact_count']}")
        for kind, records in (("source", closure["records"]), ("ELF", closure["artifacts"])):
            for row in records:
                print(f"  {kind} {row['declaring_file']}:{row['source_line']}: "
                      f"{row['loader']} {row['declared_path']} [{row['resolution']}] "
                      f"sha={row.get('sha256') or '-'}")
        print(f"STATIC TRANSPORT: {preflight['transport_status']}: {preflight['transport_reason']}")
        inventory = preflight["warm_inventory"]
        print(f"WARM INVENTORY: {inventory['status']}")
        if inventory.get("reason"):
            print(f"  {inventory['reason']}")
        satisfaction = inventory.get("profile_satisfaction")
        if satisfaction is not None:
            print(f"  evidence_class={inventory['evidence_class']}; {inventory['boundary']}")
            print(f"  satisfaction SHA-256: {satisfaction['strict_sha256']}")
            for row in satisfaction["edges"]:
                print(f"  {row['declaring_file']}:{row['source_line']}: {row['declared_path']} "
                      f"[{row['resolution']}] sha={row['sha256']}")
            print(f"  captured source edges attested against loaded inventory: {len(satisfaction['captured_warm_sources'])}")
        for kind, rows in (("source", closure["dynamic_loaders"]), ("ELF", closure["dynamic_artifacts"])):
            for row in rows:
                print(f"  dynamic {kind} {row.get('declaring_file')}:{row.get('source_line')}: "
                      f"{row.get('loader')} {row.get('reason')}")
        for reason in [*closure["limit_reasons"], *closure["project_inputs"].get("blockers", [])]:
            print(f"  blocker: {reason}")
    receipt = report["receipt"]
    if receipt is None:
        print("receipt: none")
    else:
        print(f"receipt: {receipt['path']} sha={receipt['receipt_sha256'][:12]} "
              f"evidence_class={receipt['evidence_class']} recorded={receipt['recorded_evidence']} "
              f"status={receipt['semantic_source_status']}")
        print("  identity match only: the receipt names this leaf's bytes and profile; it is not promotion")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = build_report(Path(args.source).expanduser().absolute(), args.profile, receipt=args.receipt,
                              deep=args.deep)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"leaf-needs: refused: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    preflight = report.get("preflight")
    return 2 if preflight and (preflight["status"] != "complete" or
                              not preflight["transport_status"].startswith("packaged")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
