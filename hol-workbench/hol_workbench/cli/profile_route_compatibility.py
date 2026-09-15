"""Admission-relevant compatibility lookup for the ordinary warm route."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hol_workbench.cli.prove_profiles import warmup_profile_publication_guard
from hol_workbench.criu_snapshot_admission import (
    SnapshotAdmissionRequest,
    StaticSnapshotAdmissionDecision,
)
from hol_workbench.criu_snapshot_compat import (
    SNAPSHOT_MANIFEST_FILENAME,
    default_snapshot_admission_request,
    publication_identity_guard_failures,
    snapshot_projection_compatibility,
    snapshot_runtime_compatibility,
    validate_snapshot_manifest,
)
from hol_workbench.restored_execution_topology import (
    RestoredExecutionTopology,
    restored_execution_topology,
)

WORKBENCH_BIN = Path(__file__).resolve().parents[2] / "bin"
PublicationGuardResolver = Callable[[str | Path, str], dict[str, Any] | None]
SnapshotManifestValidator = Callable[..., dict[str, Any]]


def _successful_rows(root: Path) -> list[dict]:
    rows: list[dict] = []
    for result in sorted(root.glob("criu-warm-profiles-*/results.json")):
        try:
            candidates = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in candidates if isinstance(candidates, list) else []:
            if isinstance(row, dict) and row.get("status") == "ok":
                rows.append({**row, "run_root": str(result.parent)})
    return rows


def compatible_profile_row(
    root: Path,
    profile: str,
    *,
    expected_base_name: str,
    expected_cwd: str,
    expected_basis_id: str = "",
    expected_sha256: str = "",
    expected_capacity: int = 1,
    expected_execution_topology: str | None = None,
    admission_request: SnapshotAdmissionRequest | None = None,
    include_controller_attempt: bool = True,
    publication_guard_resolver: PublicationGuardResolver = warmup_profile_publication_guard,
    snapshot_manifest_validator: SnapshotManifestValidator | None = None,
) -> dict:
    required_publication_guard = publication_guard_resolver(WORKBENCH_BIN, profile)
    result = profile_compatibility(
        root,
        profile,
        expected_base_name=expected_base_name,
        expected_cwd=expected_cwd,
        expected_basis_id=expected_basis_id,
        expected_sha256=expected_sha256,
        expected_capacity=expected_capacity,
        expected_execution_topology=expected_execution_topology,
        include_controller_attempt=include_controller_attempt,
        publication_guard_resolver=publication_guard_resolver,
    )
    request = admission_request or default_snapshot_admission_request()
    physical_profile = str(result.get("physical_profile") or "")
    run_root = str(result.get("run_root") or "")
    manifest_path = Path(run_root) / physical_profile / SNAPSHOT_MANIFEST_FILENAME
    authoritative_failure = "authoritative snapshot manifest is missing"
    if manifest_path.is_file():
        try:
            manifest = (snapshot_manifest_validator or validate_snapshot_manifest)(
                manifest_path.parent,
                admission_request=request,
                required_publication_guard=required_publication_guard,
                include_controller_attempt=include_controller_attempt,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            authoritative_failure = str(exc)
        else:
            decision = StaticSnapshotAdmissionDecision.from_record(manifest["static_admission_decision"])
            identity = decision.profile_identity
            identity_failures = []
            if identity.get("profile") != physical_profile:
                identity_failures.append(
                    f"manifest profile {identity.get('profile') or 'missing'} != selected {physical_profile}"
                )
            if Path(str(identity.get("profile_base") or "")).name != expected_base_name:
                identity_failures.append(
                    f"manifest base {Path(str(identity.get('profile_base') or '-')).name} != expected {expected_base_name}"
                )
            if str(identity.get("profile_cwd") or "") != expected_cwd:
                identity_failures.append(
                    f"manifest cwd {identity.get('profile_cwd') or 'missing'} != expected {expected_cwd}"
                )
            if expected_basis_id and str(identity.get("profile_basis_id") or "") != expected_basis_id:
                identity_failures.append(
                    f"manifest basis ID {identity.get('profile_basis_id') or 'missing'} != expected {expected_basis_id}"
                )
            if expected_sha256 and str(identity.get("profile_sha256") or "") != expected_sha256:
                identity_failures.append(
                    f"manifest SHA-256 {identity.get('profile_sha256') or 'missing'} != expected {expected_sha256}"
                )
            if expected_execution_topology and identity.get("execution_topology") != expected_execution_topology:
                identity_failures.append(
                    f"manifest topology {identity.get('execution_topology') or 'missing'} "
                    f"!= expected {expected_execution_topology}"
                )
            if identity_failures:
                authoritative_failure = "; ".join(identity_failures)
            else:
                return {
                    **result,
                    "compatibility_status": "compatible",
                    "reason": decision.reason,
                    "runtime_compatibility": decision.runtime_compatibility,
                    "runtime_drift": decision.runtime_compatibility.get("runtime_drift"),
                    "static_admission_decision": decision.record(),
                }
    raise SystemExit(
        f"no compatible CRIU shelf published for profile {profile}: "
        f"authoritative admission failed: {authoritative_failure}; "
        f"candidate projection: {result['reason']}; maintainer shelf publication is required"
    )


def _compact_identity_value(value: str) -> str:
    return value if len(value) <= 16 else f"{value[:12]}..."


def _matches_expected_topology(row: dict, expected_execution_topology: str | None) -> bool:
    if not expected_execution_topology:
        return True
    try:
        return restored_execution_topology(row).value == expected_execution_topology
    except RuntimeError:
        return False


def _matches_publication_guard(row: dict, required_publication_guard: dict | None) -> bool:
    if required_publication_guard is None:
        return True
    return not publication_identity_guard_failures(
        row.get("publication_identity_guard"),
        expected_guard=required_publication_guard,
    )


def _identity_mismatch_reason(
    row: dict,
    *,
    expected_base_name: str,
    expected_cwd: str,
    expected_basis_id: str,
    expected_sha256: str,
    expected_execution_topology: str | None,
    required_publication_guard: dict | None,
) -> str:
    problems: list[str] = []
    actual_base = Path(str(row.get("profile_base") or "-")).name
    if actual_base != expected_base_name:
        problems.append(f"expected base {expected_base_name}, latest is {actual_base}")

    actual_cwd = str(row.get("profile_cwd") or "")
    if actual_cwd != expected_cwd:
        if actual_cwd:
            problems.append(f"expected cwd {expected_cwd}, latest is {actual_cwd}")
        else:
            problems.append(f"expected cwd {expected_cwd}, latest shelf has no recorded cwd")

    actual_sha256 = str(row.get("profile_sha256") or "")
    if actual_sha256 and expected_sha256 and actual_sha256 != expected_sha256:
        problems.append(
            "expected SHA-256 "
            f"{_compact_identity_value(expected_sha256)}, latest is {_compact_identity_value(actual_sha256)}"
        )

    actual_basis_id = str(row.get("profile_basis_id") or "")
    if expected_basis_id and actual_basis_id != expected_basis_id:
        problems.append(f"expected basis ID {expected_basis_id}, latest is {actual_basis_id or 'missing'}")

    if expected_execution_topology:
        try:
            actual_topology = restored_execution_topology(row).value
        except RuntimeError:
            actual_topology = "invalid"
        if actual_topology != expected_execution_topology:
            problems.append(f"expected topology {expected_execution_topology}, latest is {actual_topology}")
    if not _matches_publication_guard(row, required_publication_guard):
        problems.append("latest shelf does not satisfy the current publication identity guard")

    return "; ".join(problems) or "recorded profile identity does not match the requested route"


def profile_compatibility(
    root: Path,
    profile: str,
    *,
    expected_base_name: str,
    expected_cwd: str,
    expected_basis_id: str = "",
    expected_sha256: str = "",
    expected_capacity: int = 1,
    expected_execution_topology: str | None = None,
    include_controller_attempt: bool = True,
    publication_guard_resolver: PublicationGuardResolver = warmup_profile_publication_guard,
) -> dict:
    """Describe current route compatibility without restoring a shelf."""

    required_publication_guard = publication_guard_resolver(WORKBENCH_BIN, profile)
    rows = _successful_rows(root)
    matches = [row for row in rows if row.get("profile") == profile]
    same_base_rows = [row for row in rows if Path(str(row.get("profile_base") or "")).name == expected_base_name]
    expected_identity = [
        row
        for row in rows
        if Path(str(row.get("profile_base") or "")).name == expected_base_name
        and str(row.get("profile_cwd") or "") == expected_cwd
        and (not expected_basis_id or row.get("profile_basis_id") == expected_basis_id)
        and (not row.get("profile_sha256") or not expected_sha256 or row.get("profile_sha256") == expected_sha256)
        and _matches_publication_guard(row, required_publication_guard)
    ]
    contracts = [
        (
            row,
            snapshot_projection_compatibility(row),
            snapshot_runtime_compatibility(row, include_controller_attempt=include_controller_attempt),
        )
        for row in expected_identity
    ]
    compatible = [
        (row, projection, runtime)
        for row, projection, runtime in contracts
        if projection["compatible"] and runtime["compatible"]
    ]
    rebuild_command = f"CRIU_PROFILES={profile} hol-workbench/bin/orbstack-criu build"
    if compatible:
        exact = [(row, projection, runtime) for row, projection, runtime in compatible if row.get("profile") == profile]
        winner, projection, runtime = exact[-1] if exact else compatible[-1]
        reason = (
            "current profile identity, manifest authority, and mechanical broker contract match"
            if runtime.get("execution_topology") == RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3.value
            else "current profile identity, manifest authority, and fork-worker capabilities match"
        )
        if runtime["runtime_drift"]:
            reason += "; embedded runtime build differs but is ABI-compatible"
        published_capacity = int(winner.get("orbstack_public_capacity") or 1)
        capacity_expansion = max(0, expected_capacity - published_capacity)
        if capacity_expansion:
            reason += (
                f"; controller will add {capacity_expansion} logical seat(s) to the existing fork basis after restore"
            )
        return {
            **winner,
            "logical_profile": profile,
            "physical_profile": str(winner.get("profile") or profile),
            "historical_build_status": "ok",
            "compatibility_status": "compatible",
            "reason": reason,
            "manifest_compatibility": projection,
            "runtime_compatibility": runtime,
            "runtime_drift": runtime["runtime_drift"],
            "published_capacity": published_capacity,
            "requested_capacity": expected_capacity,
            "logical_capacity_expansion_required": capacity_expansion,
            "rebuild_command": None,
        }
    stale_runtime_rows = [
        (row, projection, runtime)
        for row, projection, runtime in contracts
        if not projection["compatible"] or not runtime["compatible"]
    ]
    selected_projection: dict | None = None
    selected_runtime: dict | None = None
    if stale_runtime_rows:
        status = "stale-runtime"
        historical, selected_projection, selected_runtime = stale_runtime_rows[-1]
        reason = str(
            selected_projection["reason"] if not selected_projection["compatible"] else selected_runtime["reason"]
        )
    elif matches or same_base_rows:
        status = "identity-mismatch"
        historical = matches[-1] if matches else same_base_rows[-1]
        reason = _identity_mismatch_reason(
            historical,
            expected_base_name=expected_base_name,
            expected_cwd=expected_cwd,
            expected_basis_id=expected_basis_id,
            expected_sha256=expected_sha256,
            expected_execution_topology=expected_execution_topology,
            required_publication_guard=required_publication_guard,
        )
    else:
        status = "missing"
        reason = f"no successful shelf found for expected base {expected_base_name}"
        historical = {}
    return {
        **historical,
        "logical_profile": profile,
        "physical_profile": str(historical.get("profile") or "") or None,
        "historical_build_status": "ok" if historical else "missing",
        "compatibility_status": status,
        "reason": reason,
        "manifest_compatibility": selected_projection,
        "runtime_compatibility": selected_runtime,
        "runtime_drift": selected_runtime.get("runtime_drift") if selected_runtime is not None else None,
        "rebuild_command": rebuild_command,
    }
