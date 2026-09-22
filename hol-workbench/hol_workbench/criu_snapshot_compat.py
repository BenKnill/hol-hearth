"""Authoritative compatibility and provenance for CRIU fork-worker shelves."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from hol_workbench.criu_loaded_capture_schema import CAPTURE_FILENAME, CAPTURE_SCHEMA
from hol_workbench.criu_loaded_provenance import (
    LoadedProvenanceError,
    loaded_closure_entry_path,
    validate_loaded_closure,
    validate_runtime_closure,
)
from hol_workbench.criu_publication_provenance import (
    publication_provenance_failures,
    publication_provenance_record,
)
from hol_workbench.criu_snapshot_admission import (
    BINDING_BUDGET_RESET_CAPABILITY,
    BINDING_SCOPED_REQUIRED_CAPABILITIES,
    LiveExecutionGrant,
    SnapshotAdmissionMode,
    SnapshotAdmissionRequest,
    StaticSnapshotAdmissionDecision,
    read_snapshot_admission_artifact,
)
from hol_workbench.criu_snapshot_environment import (
    snapshot_compatibility_policy,
    snapshot_environment_compatibility,
    snapshot_environment_failures,
    snapshot_environment_sha256,
)
from hol_workbench.fork_broker_protocol import BROKER_PROTOCOL, broker_runtime_sha256
from hol_workbench.jsonio import JsonObject, atomic_write_json, read_json, read_json_strict
from hol_workbench.restored_execution_topology import (
    FORK_SNAPSHOT_ABI_V3,
    LEGACY_V2_SNAPSHOT,
    RestoredExecutionTopology,
    fork_snapshot_abi_for_topology,
    restored_execution_topology,
)

FORK_SNAPSHOT_ABI = FORK_SNAPSHOT_ABI_V3
BROKER_SNAPSHOT_CAPABILITIES = (
    "mechanical_child_spawn",
    "raw_output_bytes",
    "explicit_cancel",
    "peer_loss_cleanup",
    "verified_child_quiescence",
    "seat_reusable",
)
V3_SNAPSHOT_CAPABILITIES = tuple(sorted(set(BROKER_SNAPSHOT_CAPABILITIES) | set(BINDING_SCOPED_REQUIRED_CAPABILITIES)))
REQUIRED_FORK_SNAPSHOT_CAPABILITIES = BINDING_SCOPED_REQUIRED_CAPABILITIES
SNAPSHOT_MANIFEST_SCHEMA = "hol-workbench.criu-snapshot-manifest.v3"
SNAPSHOT_MANIFEST_FILENAME = "snapshot-manifest.json"
SERIAL_V3_BROKER_RUNTIME_SHA256 = "28e427ad0ab22e001f3a4b852c2f7661dd23ea22760f69451f7c002303032ce6"
CONCURRENT_V3_BROKER_RUNTIME_SHA256 = "0aaf71d37b37ebc76f7b1d9ca12c64ca1000e25d3d4f674761a05c6c711d8b8d"
REVIEWED_CONTROLLER_BROKER_RUNTIME_SHA256 = "4e6281597b0f7fe538190dc100bfebfd6fdf16e150e7fdcf724f6b633ddc6cb7"


def current_controller_executable_identity_sha256() -> str:
    from hol_workbench.controller_runtime_bundle import controller_executable_identity_sha256

    return controller_executable_identity_sha256()


# Compatibility aliases are diagnostic only. Shelf admission uses the one
# canonical executable identity below rather than metadata-only sub-identities.
def current_controller_wire_identity_sha256() -> str:
    return current_controller_executable_identity_sha256()


def current_controller_admission_identity_sha256() -> str:
    return current_controller_executable_identity_sha256()


def controller_attempt_compatibility() -> JsonObject:
    """Classify the legacy final-evidence controller review.

    Published shelves freeze the broker implementation and mathematical basis.
    A final-evidence controller is independently authorized for each attempt.
    Keeping this result nested and non-authoritative for shelf compatibility
    binds drift to that narrower layer; public authoring passes
    ``include_controller_attempt=False`` and never consults this function.
    """

    from hol_workbench.controller_runtime_bundle import (
        IMMUTABLE_AUTHORING_ATTEMPT,
        INDEPENDENTLY_REVIEWED_ATTEMPT,
        reviewed_controller_runtime_authorization,
    )

    executable_identity_sha256 = current_controller_executable_identity_sha256()
    try:
        authorization = reviewed_controller_runtime_authorization()
    except RuntimeError as exc:
        return {
            "compatible": False,
            "status": "controller_authorization_unavailable",
            "reason": f"independent controller authorization is unavailable: {exc}",
            "controller_executable_identity_sha256": executable_identity_sha256,
            "controller_authorization_id": None,
            "controller_authorization_sha256": None,
        }
    reviewed_identity = str(authorization.get("executable_identity_sha256") or "")
    authorization_mode = str(authorization.get("authorization_mode") or INDEPENDENTLY_REVIEWED_ATTEMPT)
    if reviewed_identity != executable_identity_sha256:
        return {
            "compatible": False,
            "status": "controller_executable_identity_incompatible",
            "reason": "current admission-to-credit graph is not the independently reviewed identity",
            "controller_executable_identity_sha256": executable_identity_sha256,
            "controller_authorization_id": authorization.get("authorization_id"),
            "controller_authorization_sha256": authorization.get("authorization_sha256"),
        }
    if authorization_mode not in {INDEPENDENTLY_REVIEWED_ATTEMPT, IMMUTABLE_AUTHORING_ATTEMPT}:
        return {
            "compatible": False,
            "status": "controller_authorization_mode_invalid",
            "reason": "current controller attempt has an unsupported authorization mode",
            "controller_executable_identity_sha256": executable_identity_sha256,
            "controller_authorization_id": authorization.get("authorization_id"),
            "controller_authorization_sha256": authorization.get("authorization_sha256"),
        }
    independently_reviewed = authorization_mode == INDEPENDENTLY_REVIEWED_ATTEMPT
    return {
        "compatible": True,
        "status": "compatible" if independently_reviewed else "authoring_observed",
        "reason": (
            "current admission-to-credit graph matches its independent attempt authorization"
            if independently_reviewed
            else "current controller graph is frozen and observed for warm authoring only"
        ),
        "authorization_mode": authorization_mode,
        "independently_reviewed": independently_reviewed,
        "controller_executable_identity_sha256": executable_identity_sha256,
        "controller_authorization_id": authorization.get("authorization_id"),
        "controller_authorization_sha256": authorization.get("authorization_sha256"),
    }


REVIEWED_BROKER_VARIANTS: dict[str, JsonObject] = {
    SERIAL_V3_BROKER_RUNTIME_SHA256: {
        "status": "admitted",
        "runtime_variant": "published_serial_v3",
        "execution_topology": RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3.value,
        "fork_snapshot_abi": FORK_SNAPSHOT_ABI_V3,
        "broker_protocol": BROKER_PROTOCOL,
        "capabilities": list(V3_SNAPSHOT_CAPABILITIES),
        "maximum_concurrent_attempts": 1,
    },
    CONCURRENT_V3_BROKER_RUNTIME_SHA256: {
        "status": "admitted",
        "runtime_variant": "published_concurrent_v3",
        "execution_topology": RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3.value,
        "fork_snapshot_abi": FORK_SNAPSHOT_ABI_V3,
        "broker_protocol": BROKER_PROTOCOL,
        "capabilities": list(V3_SNAPSHOT_CAPABILITIES),
        "maximum_concurrent_attempts": None,
    },
    REVIEWED_CONTROLLER_BROKER_RUNTIME_SHA256: {
        "status": "admitted",
        "runtime_variant": "published_concurrent_v3_current_at_review",
        "execution_topology": RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3.value,
        "fork_snapshot_abi": FORK_SNAPSHOT_ABI_V3,
        "broker_protocol": BROKER_PROTOCOL,
        "capabilities": list(V3_SNAPSHOT_CAPABILITIES),
        "maximum_concurrent_attempts": None,
    },
}


def _json_record(value: object) -> JsonObject | None:
    """Narrow one decoded JSON value without changing its stored fields."""

    if isinstance(value, dict):
        return value
    return None


def _read_manifest_snapshot(path: Path) -> tuple[JsonObject, str]:
    """Parse and hash one immutable read of an authoritative manifest."""

    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"expected JSON object in {path}")
    return data, hashlib.sha256(raw).hexdigest()


def default_snapshot_admission_request(
    *,
    explicit_proof_search_budget_seconds: float | str | None = None,
    purpose: str = "named_profile_evaluation",
) -> SnapshotAdmissionRequest:
    return SnapshotAdmissionRequest.create(
        required_capabilities=REQUIRED_FORK_SNAPSHOT_CAPABILITIES,
        explicit_proof_search_budget_seconds=explicit_proof_search_budget_seconds,
        purpose=purpose,
    )


def snapshot_runtime_compatibility(
    data: JsonObject,
    *,
    required_capabilities: frozenset[str] | set[str] | tuple[str, ...] = REQUIRED_FORK_SNAPSHOT_CAPABILITIES,
    include_controller_attempt: bool = True,
) -> JsonObject:
    """Classify a frozen v3 broker runtime; legacy v2 is never executable."""
    try:
        topology = restored_execution_topology(data)
    except RuntimeError as exc:
        return {
            "compatible": False,
            "status": (
                "legacy_v2_snapshot_refused"
                if data.get("execution_topology") in (None, "", "embedded_fork_manager_v2")
                else "execution_topology_invalid"
            ),
            "reason": str(exc),
            "execution_topology": LEGACY_V2_SNAPSHOT,
        }
    recorded_abi = str(data.get("fork_snapshot_abi") or "")
    if topology is RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3:
        recorded_protocol = str(data.get("broker_protocol") or "")
        recorded_broker_sha256 = str(data.get("broker_runtime_sha256") or "")
        recorded_bundle = str(data.get("broker_runtime_bundle") or "")
        current_broker_sha256 = broker_runtime_sha256()
        reviewed_variant = REVIEWED_BROKER_VARIANTS.get(recorded_broker_sha256)
        bundle_broker_sha256 = ""
        bundle_problem = ""
        if recorded_bundle:
            try:
                bundle_broker_sha256 = broker_runtime_sha256(Path(recorded_bundle) / "hol_workbench")
            except OSError as exc:
                bundle_problem = str(exc)
        raw_capabilities = data.get("fork_snapshot_capabilities")
        capability_list_valid = (
            isinstance(raw_capabilities, list)
            and all(isinstance(item, str) and item for item in raw_capabilities)
            and len(raw_capabilities) == len(set(raw_capabilities))
        )
        recorded_capabilities: set[str] = (
            {item for item in raw_capabilities if isinstance(item, str)}
            if isinstance(raw_capabilities, list) and capability_list_valid
            else set()
        )
        required = set(BROKER_SNAPSHOT_CAPABILITIES) | set(required_capabilities)
        missing_capabilities = sorted(required - recorded_capabilities)
        runtime_drift = recorded_broker_sha256 != current_broker_sha256
        controller_attempt = controller_attempt_compatibility() if include_controller_attempt else None
        reviewed_capabilities = (
            set(reviewed_variant.get("capabilities") or []) if reviewed_variant is not None else set()
        )
        if not recorded_broker_sha256:
            status, reason = "broker_runtime_provenance_missing", "broker runtime SHA-256 is missing"
        elif reviewed_variant is None:
            status, reason = (
                "broker_runtime_unknown",
                "broker runtime bundle has no exact reviewed compatibility variant",
            )
        elif reviewed_variant.get("status") == "retired":
            status, reason = "broker_runtime_retired", "broker runtime compatibility variant is retired"
        elif reviewed_variant.get("status") != "admitted":
            status, reason = "broker_runtime_variant_invalid", "broker runtime compatibility state is invalid"
        elif reviewed_variant.get("execution_topology") != topology.value:
            status, reason = (
                "broker_runtime_variant_invalid",
                "broker runtime compatibility variant names a different execution topology",
            )
        elif reviewed_variant.get("maximum_concurrent_attempts") is not None and (
            not isinstance(reviewed_variant.get("maximum_concurrent_attempts"), int)
            or isinstance(reviewed_variant.get("maximum_concurrent_attempts"), bool)
            or int(reviewed_variant["maximum_concurrent_attempts"]) <= 0
        ):
            status, reason = (
                "broker_runtime_variant_invalid",
                "broker runtime compatibility variant has an invalid capacity limit",
            )
        elif recorded_abi != reviewed_variant.get("fork_snapshot_abi"):
            status, reason = (
                "abi_mismatch",
                f"broker snapshot ABI {recorded_abi or 'missing'} != {reviewed_variant.get('fork_snapshot_abi')}",
            )
        elif recorded_protocol != reviewed_variant.get("broker_protocol"):
            status, reason = (
                "broker_protocol_mismatch",
                f"broker protocol {recorded_protocol or 'missing'} != {reviewed_variant.get('broker_protocol')}",
            )
        elif not recorded_bundle:
            status, reason = "broker_runtime_bundle_missing", "broker runtime bundle path is missing"
        elif bundle_problem:
            status, reason = (
                "broker_runtime_bundle_unreadable",
                f"broker runtime bundle is unreadable: {bundle_problem}",
            )
        elif bundle_broker_sha256 != recorded_broker_sha256:
            status, reason = (
                "broker_runtime_bundle_mismatch",
                "broker runtime bundle bytes do not match their recorded SHA-256",
            )
        elif not capability_list_valid or recorded_capabilities != reviewed_capabilities or missing_capabilities:
            status, reason = (
                "broker_capability_mismatch",
                (
                    "v3 snapshot capability identity is noncanonical or differs from its reviewed variant"
                    if not missing_capabilities
                    else "v3 snapshot lacks required capabilities: " + ", ".join(missing_capabilities)
                ),
            )
        else:
            runtime_variant = str(reviewed_variant["runtime_variant"])
            maximum_concurrent_attempts = reviewed_variant.get("maximum_concurrent_attempts")
            reason = (
                "exact frozen broker bytes match a reviewed ABI, wire, admission, capability, lifecycle, "
                "and capacity relation"
            )
            result: JsonObject = {
                "compatible": True,
                "status": "compatible",
                "reason": reason,
                "execution_topology": topology.value,
                "fork_snapshot_abi": recorded_abi,
                "broker_protocol": recorded_protocol,
                "recorded_broker_runtime_sha256": recorded_broker_sha256,
                "current_broker_runtime_sha256": current_broker_sha256,
                "broker_runtime_bundle": recorded_bundle,
                "bundle_broker_runtime_sha256": bundle_broker_sha256,
                "recorded_capabilities": sorted(recorded_capabilities),
                "binding_budget_owner": "current_controller",
                "review_status": reviewed_variant["status"],
                "runtime_drift": runtime_drift,
                "runtime_variant": runtime_variant,
                "maximum_concurrent_attempts": maximum_concurrent_attempts,
            }
            if controller_attempt is not None:
                result.update(
                    {
                        "controller_executable_identity_sha256": controller_attempt[
                            "controller_executable_identity_sha256"
                        ],
                        "controller_authorization_id": controller_attempt.get("controller_authorization_id"),
                        "controller_authorization_sha256": controller_attempt.get("controller_authorization_sha256"),
                        "controller_attempt_compatibility": controller_attempt,
                    }
                )
            return result
        result = {
            "compatible": False,
            "status": status,
            "reason": reason,
            "execution_topology": topology.value,
            "fork_snapshot_abi": recorded_abi,
            "broker_protocol": recorded_protocol or None,
            "recorded_broker_runtime_sha256": recorded_broker_sha256 or None,
            "current_broker_runtime_sha256": current_broker_sha256,
            "broker_runtime_bundle": recorded_bundle or None,
            "bundle_broker_runtime_sha256": bundle_broker_sha256 or None,
            "recorded_capabilities": sorted(recorded_capabilities),
            "missing_capabilities": missing_capabilities,
            "binding_budget_owner": "current_controller",
            "runtime_drift": recorded_broker_sha256 != current_broker_sha256,
        }
        if controller_attempt is not None:
            result.update(
                {
                    "controller_executable_identity_sha256": controller_attempt[
                        "controller_executable_identity_sha256"
                    ],
                    "controller_authorization_id": controller_attempt.get("controller_authorization_id"),
                    "controller_authorization_sha256": controller_attempt.get("controller_authorization_sha256"),
                    "controller_attempt_compatibility": controller_attempt,
                }
            )
        return result
    raise AssertionError(f"unreachable restored topology: {topology}")


def snapshot_projection_compatibility(data: JsonObject) -> JsonObject:
    """Classify whether a build/index row projects the authoritative manifest contract."""

    if data.get("snapshot_manifest_schema") != SNAPSHOT_MANIFEST_SCHEMA:
        return {
            "compatible": False,
            "status": "manifest_schema_mismatch",
            "reason": "published shelf does not project the authoritative manifest schema",
        }
    if data.get("snapshot_admission_authority") is not True:
        return {
            "compatible": False,
            "status": "manifest_authority_missing",
            "reason": "published shelf does not project authoritative manifest admission",
        }
    if not data.get("loaded_strict_sha256") or not data.get("ocaml_runtime_strict_sha256"):
        return {
            "compatible": False,
            "status": "manifest_provenance_missing",
            "reason": "published shelf does not project loaded and OCaml runtime provenance",
        }
    if not data.get("snapshot_environment_sha256"):
        return {
            "compatible": False,
            "status": "environment_provenance_missing",
            "reason": "published shelf does not project its diagnostic environment identity",
        }
    return {
        "compatible": True,
        "status": "compatible",
        "reason": "published shelf projects the authoritative admission contract",
    }


def _lexical_absolute(path: object) -> Path:
    return Path(os.path.abspath(Path(str(path)).expanduser()))


def _semantic_input_failures(
    provenance: JsonObject,
    *,
    profile_base: object,
    profile_cwd: object,
    extra_preloads: object,
) -> list[str]:
    holdir = _lexical_absolute(provenance.get("holdir") or "")
    loaded_closure: JsonObject = provenance.get("loaded_closure") or {}
    entries = loaded_closure.get("entries") or []
    actual: dict[str, list[Path]] = {"profile_base": [], "profile_cwd": [], "extra_preload": []}
    loaded_paths: list[Path] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            path = loaded_closure_entry_path(entry, holdir=holdir)
        except LoadedProvenanceError:
            continue
        loaded_paths.append(_lexical_absolute(path))
        raw_roles = entry.get("roles")
        roles = {role for role in raw_roles if isinstance(role, str)} if isinstance(raw_roles, list) else set()
        for role in roles & actual.keys():
            actual[role].append(_lexical_absolute(path))
    expected_base = _lexical_absolute(profile_base) if profile_base else None
    expected_cwd = _lexical_absolute(profile_cwd) if profile_cwd else None
    expected_extras = (
        sorted({_lexical_absolute(path) for path in extra_preloads}) if isinstance(extra_preloads, list) else []
    )
    failures = []
    if expected_base is None or actual["profile_base"] != [expected_base]:
        failures.append("loaded closure does not bind exactly the configured profile base")
    expected_cwd_entries = (
        sorted(path for path in loaded_paths if path.is_relative_to(expected_cwd))
        if expected_cwd is not None and expected_cwd != holdir
        else []
    )
    if expected_cwd is None or not expected_cwd.is_dir():
        failures.append("configured profile cwd is missing or invalid")
    elif sorted(actual["profile_cwd"]) != expected_cwd_entries:
        failures.append("loaded closure does not bind exactly the configured profile cwd inputs")
    if sorted(actual["extra_preload"]) != expected_extras:
        failures.append("loaded closure does not bind exactly the configured semantic extra preloads")
    return failures


def _snapshot_provenance_failures(
    provenance: Any,
    *,
    profile_base: object = None,
    profile_cwd: object = None,
    extra_preloads: object = None,
) -> list[str]:
    if not isinstance(provenance, dict):
        return ["authoritative loaded/runtime provenance is missing"]
    failures: list[str] = []
    if provenance.get("capture_schema") != CAPTURE_SCHEMA:
        failures.append("provenance capture schema is incompatible")
    if provenance.get("capture_status") != "captured_pre_dump":
        failures.append("provenance capture did not complete before dump")
    if provenance.get("child_quiescent") is not True:
        failures.append("provenance capture child was not quiescent")
    holdir_text = str(provenance.get("holdir") or "")
    holdir = Path(holdir_text)
    if not holdir.is_absolute() or not holdir.is_dir():
        failures.append(f"recorded HOL directory is missing or invalid: {holdir_text or 'missing'}")
    else:
        failures.extend(
            f"loaded HOL provenance: {failure}"
            for failure in validate_loaded_closure(provenance.get("loaded_closure") or {}, holdir=holdir)
        )
    failures.extend(
        f"OCaml runtime provenance: {failure}"
        for failure in validate_runtime_closure(provenance.get("runtime_closure") or {})
    )
    failures.extend(
        _semantic_input_failures(
            provenance,
            profile_base=profile_base,
            profile_cwd=profile_cwd,
            extra_preloads=extra_preloads,
        )
    )
    return failures


def _capture_for_manifest(profile_root: Path, metadata: JsonObject) -> JsonObject:
    expected = profile_root / "provenance" / CAPTURE_FILENAME
    summary = metadata.get("provenance_capture")
    if not isinstance(summary, dict):
        raise RuntimeError("successful shelf build is missing its provenance capture summary")
    recorded_path = Path(str(summary.get("path") or ""))
    try:
        if recorded_path.resolve(strict=True) != expected.resolve(strict=True):
            raise RuntimeError(f"provenance capture path is outside this profile: {recorded_path}")
        capture = read_json_strict(expected)
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"cannot read CRIU provenance capture {expected}: {exc}") from exc
    loaded: JsonObject = capture.get("loaded_closure") or {}
    runtime: JsonObject = capture.get("runtime_closure") or {}
    capture_state: JsonObject = capture.get("capture") or {}
    expected_summary = {
        "path": str(expected),
        "status": capture.get("status"),
        "admission_authority": capture.get("admission_authority"),
        "loaded_record_count": loaded.get("record_count"),
        "loaded_unique_record_count": loaded.get("unique_record_count"),
        "loaded_strict_sha256": loaded.get("strict_sha256"),
        "runtime_strict_sha256": runtime.get("strict_sha256"),
        "child_quiescent": capture_state.get("child_quiescent"),
    }
    if summary != expected_summary:
        raise RuntimeError("provenance capture summary does not match its durable sidecar")
    if capture.get("schema") != CAPTURE_SCHEMA or capture.get("admission_authority") is not False:
        raise RuntimeError("provenance capture sidecar has an incompatible authority contract")
    return capture


def snapshot_manifest_projection(data: JsonObject) -> JsonObject:
    """Return the compact build/index projection of one authoritative manifest."""

    provenance: JsonObject = data.get("snapshot_provenance") or {}
    loaded: JsonObject = provenance.get("loaded_closure") or {}
    runtime: JsonObject = provenance.get("runtime_closure") or {}
    publication_guard: JsonObject = data.get("publication_identity_guard") or {}
    guard_capture: JsonObject = publication_guard.get("capture") or {}
    publication_provenance: JsonObject = data.get("publication_provenance") or {}
    workbench_source: JsonObject = publication_provenance.get("workbench_source") or {}
    profile_recipe: JsonObject = publication_provenance.get("profile_recipe") or {}
    image_inventory: JsonObject = publication_provenance.get("image_inventory") or {}
    post_restore_smoke: JsonObject = publication_provenance.get("post_restore_smoke") or {}
    return {
        "profile": data.get("profile"),
        "profile_base": data.get("profile_base"),
        "profile_sha256": data.get("profile_sha256"),
        "profile_basis_id": data.get("profile_basis_id"),
        "profile_cwd": data.get("profile_cwd"),
        "extra_preloads": data.get("extra_preloads") or [],
        "fork_snapshot_abi": data.get("fork_snapshot_abi"),
        "execution_topology": data.get("execution_topology"),
        "fork_snapshot_capabilities": data.get("fork_snapshot_capabilities") or [],
        "broker_protocol": data.get("broker_protocol"),
        "broker_runtime_sha256": data.get("broker_runtime_sha256"),
        "broker_runtime_bundle": data.get("broker_runtime_bundle"),
        "snapshot_manifest_schema": data.get("schema"),
        "snapshot_admission_authority": data.get("admission_authority"),
        "loaded_record_count": loaded.get("record_count"),
        "loaded_unique_record_count": loaded.get("unique_record_count"),
        "loaded_strict_sha256": loaded.get("strict_sha256"),
        "ocaml_runtime_strict_sha256": runtime.get("strict_sha256"),
        "snapshot_environment_sha256": data.get("snapshot_environment_sha256"),
        "publication_guard_status": guard_capture.get("status"),
        "workbench_revision": workbench_source.get("revision"),
        "workbench_tree": workbench_source.get("tree"),
        "profile_recipe_sha256": profile_recipe.get("recipe_sha256"),
        "image_inventory_sha256": image_inventory.get("inventory_sha256"),
        "image_inventory_bytes": image_inventory.get("size_bytes"),
        "post_restore_smoke_sha256": post_restore_smoke.get("smoke_sha256"),
    }


def snapshot_manifest_failure_lines(profile_root: Path, error: RuntimeError) -> list[str]:
    """Render the compact fail-closed restore card for one invalid shelf."""

    manifest = read_json(profile_root / SNAPSHOT_MANIFEST_FILENAME)
    profile = str(manifest.get("profile") or profile_root.name)
    return [
        "status=failed",
        "failing_phase=snapshot_manifest",
        f"profile_root={profile_root}",
        f"reason={error}",
        (
            "retry_action=maintainer shelf publication required: "
            f"CRIU_PROFILES={profile} hol-workbench/bin/orbstack-criu build"
        ),
    ]


def write_snapshot_manifest(profile_root: Path, *, profile: str, metadata: JsonObject) -> Path:
    profile_root = profile_root.expanduser().resolve()
    capture = _capture_for_manifest(profile_root, metadata)
    capture_state: JsonObject = capture.get("capture") or {}
    provenance = {
        "capture_schema": capture.get("schema"),
        "capture_status": capture.get("status"),
        "captured_utc": capture.get("created_utc"),
        "child_quiescent": capture_state.get("child_quiescent"),
        "holdir": capture.get("holdir"),
        "loaded_closure": capture.get("loaded_closure"),
        "runtime_closure": capture.get("runtime_closure"),
    }
    failures = _snapshot_provenance_failures(
        provenance,
        profile_base=metadata.get("profile_base"),
        profile_cwd=metadata.get("profile_cwd"),
        extra_preloads=metadata.get("extra_preloads") or [],
    )
    if failures:
        raise RuntimeError("captured CRIU snapshot provenance is invalid: " + "; ".join(failures))
    environment = metadata.get("snapshot_environment")
    environment_failures = snapshot_environment_failures(environment)
    if environment_failures:
        raise RuntimeError("captured CRIU snapshot environment is invalid: " + "; ".join(environment_failures))
    path = profile_root / SNAPSHOT_MANIFEST_FILENAME
    topology = RestoredExecutionTopology(str(metadata.get("execution_topology") or ""))
    if topology is not RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3:
        raise RuntimeError("snapshot publication requires the v3 mechanical broker")
    publication_identity_guard = metadata.get("publication_identity_guard")
    guard_failures = publication_identity_guard_failures(publication_identity_guard)
    if publication_identity_guard is not None and guard_failures:
        raise RuntimeError("publication identity guard is invalid: " + "; ".join(guard_failures))
    publication_provenance = publication_provenance_record(
        profile_root,
        workbench_source=metadata.get("workbench_source") or {},
        profile_recipe=metadata.get("profile_recipe") or {},
        image_inventory=metadata.get("image_inventory") or {},
        post_restore_smoke=metadata.get("post_restore_smoke") or {},
        expected_profile_basis_id=str(metadata.get("profile_basis_id") or ""),
        expected_profile_sha256=str(metadata.get("profile_sha256") or ""),
    )
    atomic_write_json(
        path,
        {
            "schema": SNAPSHOT_MANIFEST_SCHEMA,
            "status": "ok",
            "admission_authority": True,
            "evidence_boundary": "warm shelf admission only; not final theorem evidence",
            "profile": profile,
            "profile_base": metadata.get("profile_base"),
            "profile_sha256": metadata.get("profile_sha256"),
            "profile_basis_id": metadata.get("profile_basis_id"),
            "profile_cwd": metadata.get("profile_cwd"),
            "extra_preloads": metadata.get("extra_preloads") or [],
            "execution_topology": topology.value,
            "fork_snapshot_abi": fork_snapshot_abi_for_topology(topology),
            "fork_snapshot_capabilities": list(V3_SNAPSHOT_CAPABILITIES),
            "broker_protocol": BROKER_PROTOCOL,
            "broker_runtime_sha256": metadata.get("broker_runtime_sha256"),
            "broker_runtime_bundle": metadata.get("broker_runtime_bundle"),
            "snapshot_provenance": provenance,
            "snapshot_environment": environment,
            "snapshot_environment_sha256": snapshot_environment_sha256(environment),
            "publication_identity_guard": publication_identity_guard,
            "publication_provenance": publication_provenance,
            "compatibility_policy": snapshot_compatibility_policy(),
            "image_dir": "criu-image",
            "pool_dir": "pool",
        },
    )
    return path


def _preload_content_identities(value: object) -> list[str] | None:
    """Return ordered preload byte identities, independent of installation path."""

    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return None
    identities: list[str] = []
    for item in value:
        try:
            identities.append(hashlib.sha256(Path(item).expanduser().read_bytes()).hexdigest())
        except OSError:
            return None
    return identities


def publication_identity_guard_failures(
    record: object,
    *,
    expected_guard: JsonObject | None = None,
) -> list[str]:
    """Validate a captured guard and optionally bind it to current profile policy."""

    if record is None:
        return ["publication identity guard is missing"] if expected_guard is not None else []
    if not isinstance(record, dict):
        return ["publication identity guard is not an object"]
    preflight = record.get("preflight")
    capture = record.get("capture")
    if not isinstance(preflight, dict) or not isinstance(capture, dict):
        return ["publication identity guard lacks preflight or capture evidence"]
    failures = []
    for phase, expected_phase in ((preflight, "pre_pool_start"), (capture, "pre_criu_dump")):
        if phase.get("schema") != "hol-workbench.criu-publication-identity-guard.v1":
            failures.append("publication identity guard schema is incompatible")
        if phase.get("phase") != expected_phase:
            failures.append(f"publication identity guard phase is not {expected_phase}")
    if preflight.get("status") != "passed":
        failures.append("publication identity guard preflight did not pass")
    if capture.get("status") != "passed":
        failures.append("publication identity guard capture did not pass")
    if preflight.get("profile") != capture.get("profile"):
        failures.append("publication identity guard profile changed between preflight and capture")
    if preflight.get("reference_profile") != capture.get("reference_profile"):
        failures.append("publication identity guard reference changed between preflight and capture")
    preflight_expected = _json_record(preflight.get("expected")) or {}
    capture_expected = _json_record(capture.get("expected")) or {}
    capture_observed = _json_record(capture.get("observed")) or {}
    for key in ("loaded_strict_sha256", "ocaml_runtime_strict_sha256", "snapshot_environment_sha256"):
        if key in capture_expected and capture_expected.get(key) != capture_observed.get(key):
            failures.append(f"publication identity guard legacy {key} expectation differs from capture")
        observed = capture_observed.get(key)
        if not isinstance(observed, str) or re.fullmatch(r"[0-9a-f]{64}", observed) is None:
            failures.append(f"publication identity guard observed {key} is not a lowercase SHA-256")
    if expected_guard is not None:
        expected_fields = {
            "profile_sha256": expected_guard.get("profile_sha256"),
            "execution_topology": expected_guard.get("execution_topology"),
        }
        if preflight.get("reference_profile") != expected_guard.get("reference_profile"):
            failures.append("publication identity guard reference differs from current profile policy")
        if (
            expected_guard.get("require_publication_provenance") is True
            and preflight_expected.get("require_publication_provenance") is not True
        ):
            failures.append("publication identity guard lost required committed publication provenance")
        for key, expected in expected_fields.items():
            if preflight_expected.get(key) != expected:
                failures.append(f"publication identity guard {key} differs from current profile policy")
        recorded_preloads = preflight_expected.get("extra_preloads")
        expected_preloads = expected_guard.get("required_extra_preloads")
        if recorded_preloads != expected_preloads:
            recorded_identities = _preload_content_identities(recorded_preloads)
            expected_identities = _preload_content_identities(expected_preloads)
            if recorded_identities is None or expected_identities is None or recorded_identities != expected_identities:
                failures.append("publication identity guard extra_preloads differ from current profile policy")
    return failures


def publication_identity_guard_manifest_failures(
    data: JsonObject,
    *,
    expected_guard: JsonObject | None = None,
) -> list[str]:
    """Bind guard evidence to the authoritative manifest it protects."""

    record = data.get("publication_identity_guard")
    failures = publication_identity_guard_failures(record, expected_guard=expected_guard)
    if not isinstance(record, dict):
        return failures
    preflight = _json_record(record.get("preflight")) or {}
    capture = _json_record(record.get("capture")) or {}
    preflight_expected = _json_record(preflight.get("expected")) or {}
    preflight_observed = _json_record(preflight.get("observed")) or {}
    capture_observed = _json_record(capture.get("observed")) or {}
    provenance = _json_record(data.get("snapshot_provenance")) or {}
    loaded = _json_record(provenance.get("loaded_closure")) or {}
    runtime = _json_record(provenance.get("runtime_closure")) or {}
    manifest_fields = {
        "profile_sha256": data.get("profile_sha256"),
        "execution_topology": data.get("execution_topology"),
        "extra_preloads": data.get("extra_preloads") or [],
        "loaded_strict_sha256": loaded.get("strict_sha256"),
        "ocaml_runtime_strict_sha256": runtime.get("strict_sha256"),
        "snapshot_environment_sha256": data.get("snapshot_environment_sha256"),
    }
    for key, observed in manifest_fields.items():
        guarded = (
            capture_observed.get(key)
            if key in {"loaded_strict_sha256", "ocaml_runtime_strict_sha256", "snapshot_environment_sha256"}
            else preflight_expected.get(key)
        )
        if guarded != observed:
            failures.append(f"publication identity guard {key} differs from authoritative manifest")
    publication_provenance = _json_record(data.get("publication_provenance")) or {}
    if preflight_expected.get("require_publication_provenance") is True and publication_provenance.get(
        "workbench_source"
    ) != preflight_observed.get("workbench_source"):
        failures.append("publication identity guard Hearth revision differs from publication provenance")
    return failures


def static_snapshot_admission_decision(
    profile_root: Path,
    data: JsonObject,
    *,
    request: SnapshotAdmissionRequest,
    manifest_sha256: str,
    include_controller_attempt: bool = True,
) -> StaticSnapshotAdmissionDecision:
    """Classify one authoritative manifest under one explicit request."""

    resolved_root = profile_root.expanduser().resolve()
    manifest_path = resolved_root / SNAPSHOT_MANIFEST_FILENAME
    if len(manifest_sha256) != 64:
        raise RuntimeError("snapshot admission requires the digest of the parsed manifest bytes")
    runtime = snapshot_runtime_compatibility(
        data,
        required_capabilities=request.required_capabilities,
        include_controller_attempt=include_controller_attempt,
    )
    if runtime["compatible"] and BINDING_BUDGET_RESET_CAPABILITY in request.required_capabilities:
        mode = SnapshotAdmissionMode.FULL_BINDING_SCOPED
        reason = str(runtime["reason"])
    else:
        mode = SnapshotAdmissionMode.REFUSED
        reason = (
            str(runtime["reason"])
            if BINDING_BUDGET_RESET_CAPABILITY in request.required_capabilities
            else "binding-scoped admission requires proof_search_budget_binding_resets"
        )
    provenance = _json_record(data.get("snapshot_provenance")) or {}
    loaded = _json_record(provenance.get("loaded_closure")) or {}
    profile_identity = {
        "profile": data.get("profile"),
        "profile_base": data.get("profile_base"),
        "profile_sha256": data.get("profile_sha256"),
        "profile_basis_id": data.get("profile_basis_id"),
        "profile_cwd": data.get("profile_cwd"),
        "snapshot_environment_sha256": data.get("snapshot_environment_sha256"),
        "loaded_closure_sha256": loaded.get("strict_sha256"),
        "execution_topology": runtime.get("execution_topology") or LEGACY_V2_SNAPSHOT,
        "fork_snapshot_abi": data.get("fork_snapshot_abi"),
        "broker_protocol": data.get("broker_protocol"),
        "broker_runtime_sha256": data.get("broker_runtime_sha256"),
    }
    decision = StaticSnapshotAdmissionDecision(
        request=request,
        manifest_path=str(manifest_path),
        manifest_sha256=manifest_sha256,
        profile_identity=profile_identity,
        runtime_compatibility=runtime,
        mode=mode,
        reason=reason,
    )
    return StaticSnapshotAdmissionDecision.from_record(decision.record())


def verify_static_snapshot_admission_decision(
    profile_root: Path,
    decision: StaticSnapshotAdmissionDecision | JsonObject,
    *,
    expected_request: SnapshotAdmissionRequest | None = None,
    include_controller_attempt: bool = True,
) -> StaticSnapshotAdmissionDecision:
    """Verify one already-computed decision without reinterpreting its policy."""

    parsed = StaticSnapshotAdmissionDecision.from_record(
        decision.record() if isinstance(decision, StaticSnapshotAdmissionDecision) else decision
    )
    if parsed.mode is SnapshotAdmissionMode.REFUSED:
        raise RuntimeError(f"snapshot admission was refused: {parsed.reason}")
    if expected_request is not None:
        canonical_request = SnapshotAdmissionRequest.from_record(expected_request.record())
        if parsed.request.record() != canonical_request.record():
            raise RuntimeError("static snapshot admission request does not match the routed request")
    resolved_root = profile_root.expanduser().resolve()
    manifest_path = resolved_root / SNAPSHOT_MANIFEST_FILENAME
    if Path(parsed.manifest_path).expanduser().resolve() != manifest_path:
        raise RuntimeError("static snapshot admission names a different manifest")
    manifest, current_sha256 = _read_manifest_snapshot(manifest_path)
    if current_sha256 != parsed.manifest_sha256:
        raise RuntimeError("snapshot manifest changed after static admission")
    canonical = static_snapshot_admission_decision(
        resolved_root,
        manifest,
        request=parsed.request,
        manifest_sha256=current_sha256,
        include_controller_attempt=include_controller_attempt,
    )
    if canonical.record() != parsed.record():
        raise RuntimeError("static snapshot admission decision does not match the authoritative manifest")
    return parsed


def read_verified_snapshot_manifest(
    profile_root: Path,
    *,
    expected_sha256: str,
) -> JsonObject:
    """Read one manifest snapshot and require its admitted byte identity."""

    manifest, observed_sha256 = _read_manifest_snapshot(
        profile_root.expanduser().resolve() / SNAPSHOT_MANIFEST_FILENAME
    )
    if observed_sha256 != expected_sha256:
        raise RuntimeError("snapshot manifest changed after static admission")
    return manifest


def validate_snapshot_admission_artifact(
    path: Path,
    *,
    profile_root: Path,
    expected_artifact_sha256: str | None = None,
    expected_decision_sha256: str | None = None,
    expected_request_sha256: str | None = None,
) -> JsonObject:
    """Revalidate a durable decision against the recorded pool generation.

    This verifies hash and generation consistency. It is not nonce-bearing
    endpoint or implementation attestation.
    """

    from hol_workbench.cli.orbstack_criu_restore import pool_record_is_live

    artifact = read_snapshot_admission_artifact(path)
    if expected_artifact_sha256 is not None and artifact["artifact_sha256"] != expected_artifact_sha256:
        raise RuntimeError("snapshot admission artifact does not match the routed artifact digest")
    decision = verify_static_snapshot_admission_decision(profile_root, artifact["static_decision"])
    if expected_decision_sha256 is not None and decision.decision_sha256 != expected_decision_sha256:
        raise RuntimeError("snapshot admission artifact does not match the routed decision digest")
    request_sha256 = str(decision.request.record()["request_sha256"])
    if expected_request_sha256 is not None and request_sha256 != expected_request_sha256:
        raise RuntimeError("snapshot admission artifact does not match the routed request digest")
    grant = LiveExecutionGrant.from_record(artifact["live_execution_grant"])
    observed = LiveExecutionGrant.observe(
        decision=decision,
        pool=Path(grant.pool),
        pool_record_liveness_check=pool_record_is_live,
    )
    if observed.generation_sha256 != grant.generation_sha256:
        raise RuntimeError("live execution generation changed after snapshot admission")
    return artifact


def validate_snapshot_manifest(
    profile_root: Path,
    *,
    admission_request: SnapshotAdmissionRequest | None = None,
    required_publication_guard: JsonObject | None = None,
    include_controller_attempt: bool = True,
) -> JsonObject:
    path = profile_root / SNAPSHOT_MANIFEST_FILENAME
    failures = []
    try:
        data, manifest_sha256 = _read_manifest_snapshot(path)
    except FileNotFoundError:
        data = {}
        manifest_sha256 = None
        failures.append("manifest is missing")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        data = {}
        manifest_sha256 = None
        failures.append(f"manifest is not a valid JSON object: {exc}")
    if data.get("schema") != SNAPSHOT_MANIFEST_SCHEMA:
        failures.append("manifest schema is incompatible; authoritative loaded/runtime provenance is required")
    if data.get("status") != "ok" or not data.get("profile"):
        failures.append("manifest is not a successful per-profile snapshot")
    if data.get("admission_authority") is not True:
        failures.append("manifest is not the shelf admission authority")
    request = admission_request or default_snapshot_admission_request(purpose="direct_restore")
    decision = (
        static_snapshot_admission_decision(
            profile_root,
            data,
            request=request,
            manifest_sha256=manifest_sha256,
            include_controller_attempt=include_controller_attempt,
        )
        if manifest_sha256 is not None
        else None
    )
    if decision is not None and decision.mode is SnapshotAdmissionMode.REFUSED:
        failures.append(decision.reason)
    if data.get("image_dir") != "criu-image" or data.get("pool_dir") != "pool":
        failures.append("manifest artifact layout is invalid")
    if not (profile_root / "criu-image").is_dir() or not (profile_root / "pool").is_dir():
        failures.append("manifest artifact directories are missing")
    environment = data.get("snapshot_environment")
    failures.extend(snapshot_environment_failures(environment))
    if data.get("snapshot_environment_sha256") != snapshot_environment_sha256(environment):
        failures.append("snapshot environment identity digest is inconsistent")
    if data.get("compatibility_policy") != snapshot_compatibility_policy():
        failures.append("snapshot compatibility policy is missing or incompatible")
    failures.extend(
        publication_identity_guard_manifest_failures(
            data,
            expected_guard=required_publication_guard,
        )
    )
    publication_guard = data.get("publication_identity_guard")
    preflight_expected: JsonObject = {}
    if isinstance(publication_guard, dict):
        preflight = publication_guard.get("preflight")
        if isinstance(preflight, dict) and isinstance(preflight.get("expected"), dict):
            preflight_expected = preflight["expected"]
    publication_provenance = data.get("publication_provenance")
    if publication_provenance is not None or preflight_expected.get("require_publication_provenance") is True:
        failures.extend(
            publication_provenance_failures(
                profile_root,
                publication_provenance,
                expected_profile_basis_id=data.get("profile_basis_id"),
                expected_profile_sha256=data.get("profile_sha256"),
            )
        )
    environment_compatibility = snapshot_environment_compatibility(environment)
    failures.extend(environment_compatibility["failures"])
    failures.extend(
        _snapshot_provenance_failures(
            data.get("snapshot_provenance"),
            profile_base=data.get("profile_base"),
            profile_cwd=data.get("profile_cwd"),
            extra_preloads=data.get("extra_preloads"),
        )
    )
    if failures:
        raise RuntimeError(f"invalid CRIU snapshot manifest {path}: {'; '.join(failures)}")
    assert decision is not None
    return {
        **data,
        "runtime_compatibility": decision.runtime_compatibility,
        "runtime_admission_mode": decision.legacy_manifest_mode(),
        "static_admission_decision": decision.record(),
        "environment_compatibility": environment_compatibility,
    }


def validate_authoring_shelf_manifest(
    profile_root: Path,
    *,
    admission_request: SnapshotAdmissionRequest | None = None,
    required_publication_guard: JsonObject | None = None,
) -> JsonObject:
    """Validate an immutable warm basis without granting final-proof authority.

    The shelf's broker/runtime bytes, capabilities, profile identity, source
    closure, environment, and publication guard remain mandatory. The retired
    per-attempt final-controller review is deliberately outside this authoring
    boundary; final replay must establish its own independent evidence.
    """

    return validate_snapshot_manifest(
        profile_root,
        admission_request=admission_request,
        required_publication_guard=required_publication_guard,
        include_controller_attempt=False,
    )
