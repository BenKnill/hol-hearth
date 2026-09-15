"""Fail-closed publication for completed CRIU shelf builds."""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from hol_workbench.criu_publication_provenance import publication_provenance_failures
from hol_workbench.criu_shelf_result_evidence import shelf_result_evidence_failures
from hol_workbench.criu_snapshot_compat import (
    SNAPSHOT_MANIFEST_FILENAME,
    snapshot_manifest_projection,
    snapshot_projection_compatibility,
    validate_authoring_shelf_manifest,
)

RESULTS_FILENAME = "results.json"


def build_publication_failures(
    root: Path,
    rows: Sequence[dict[str, Any]],
    requested_profiles: Sequence[str],
) -> list[str]:
    """Return every reason a build batch is not safe to publish."""

    root = root.expanduser().resolve()
    requested = tuple(requested_profiles)
    failures: list[str] = []
    if not requested:
        failures.append("no profiles were requested")
    if len(rows) != len(requested):
        failures.append(f"completed {len(rows)} of {len(requested)} requested profiles")

    for index, profile in enumerate(requested):
        if index >= len(rows):
            break
        row = rows[index]
        actual_profile = str(row.get("profile") or "")
        if actual_profile != profile:
            failures.append(
                f"row {index} profile {actual_profile or 'missing'} does not match requested profile {profile}"
            )
            continue
        if row.get("status") != "ok":
            failures.append(f"profile {profile} build status is {row.get('status') or 'missing'}")
        if row.get("stop_verified_quiescent") is not True:
            failures.append(f"profile {profile} has no verified-quiescent final stop")

        profile_root = (root / profile).resolve()
        evidence_failures = shelf_result_evidence_failures(profile_root, row)
        failures.extend(f"profile {profile} operational evidence: {failure}" for failure in evidence_failures)
        expected_manifest_path = profile_root / SNAPSHOT_MANIFEST_FILENAME
        recorded_manifest_path = Path(str(row.get("snapshot_manifest") or "")).expanduser()
        if not recorded_manifest_path.is_absolute():
            recorded_manifest_path = (root / recorded_manifest_path).resolve()
        else:
            recorded_manifest_path = recorded_manifest_path.resolve()
        if recorded_manifest_path != expected_manifest_path:
            failures.append(f"profile {profile} snapshot manifest path is not rooted in its final shelf path")
            continue

        try:
            manifest = validate_authoring_shelf_manifest(profile_root)
        except RuntimeError as exc:
            failures.append(f"profile {profile} snapshot manifest revalidation failed: {exc}")
            continue
        provenance_failures = publication_provenance_failures(
            profile_root,
            manifest.get("publication_provenance"),
            expected_profile_basis_id=manifest.get("profile_basis_id"),
            expected_profile_sha256=manifest.get("profile_sha256"),
            verify_image_files=True,
        )
        failures.extend(f"profile {profile} publication provenance: {failure}" for failure in provenance_failures)
        projection = snapshot_manifest_projection(manifest)
        mismatched = sorted(key for key, value in projection.items() if row.get(key) != value)
        if mismatched:
            failures.append(f"profile {profile} results projection differs from its manifest: {', '.join(mismatched)}")
            continue
        compatibility = snapshot_projection_compatibility(row)
        if not compatibility["compatible"]:
            failures.append(f"profile {profile} results projection is incompatible: {compatibility['reason']}")
    return failures


def publish_build_results(
    root: Path,
    rows: Sequence[dict[str, Any]],
    requested_profiles: Sequence[str],
) -> Path:
    """Durably publish one complete batch as the shelf discovery marker."""

    root = root.expanduser().resolve()
    failures = build_publication_failures(root, rows, requested_profiles)
    if failures:
        raise RuntimeError("CRIU shelf build is not publishable: " + "; ".join(failures))

    path = root / RESULTS_FILENAME
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(list(rows), indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        tmp.unlink(missing_ok=True)
    return path
