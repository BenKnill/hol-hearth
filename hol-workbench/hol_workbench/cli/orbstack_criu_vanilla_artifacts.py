"""Optional self-contained artifacts for the raw warm-vanilla route."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hol_workbench.hashing import sha256_file
from hol_workbench.ids import run_id
from hol_workbench.jsonio import atomic_write_json, read_json
from hol_workbench.machine_client import client_identity

VANILLA_ARTIFACT_SCHEMA = "hol-workbench.warm-vanilla-artifact.v1"


def metadata_path(transcript: Path) -> Path:
    return Path(f"{transcript}.json")


def raw_transcript_path(transcript: Path) -> Path:
    return Path(f"{transcript}.raw")


def default_transcript_path(run_root: Path, source: Path) -> Path:
    attempt = run_id(source.stem, fallback="source", nonce_bytes=3)
    return run_root.expanduser().resolve() / attempt / "transcript.log"


def write_vanilla_artifacts(
    transcript: Path,
    *,
    displayed_transcript: str,
    raw_transcript: str,
    source: Path,
    source_sha256: str,
    executed_source_sha256: str | None,
    profile_root: Path,
    logical_profile: str,
    response: dict[str, Any] | None,
    transport: str,
    admission_wait_seconds: float,
    admission_slot: int = 1,
    effective_capacity: int = 1,
    semantic: dict[str, Any] | None = None,
    cleanup: dict[str, Any] | None = None,
    source_dependency_closure: dict[str, Any] | None = None,
    dependency_package: dict[str, Any] | None = None,
    evidence_role: str = "warm_development_only",
) -> Path:
    """Preserve exactly the displayed HOL text and an adjacent provenance record."""

    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(displayed_transcript, encoding="utf-8")
    raw_transcript_output = raw_transcript_path(transcript)
    raw_transcript_output.write_text(raw_transcript, encoding="utf-8")
    manifest = read_json(profile_root / "snapshot-manifest.json")
    metadata = metadata_path(transcript)
    closure = source_dependency_closure or {}
    package = dependency_package or {}
    atomic_write_json(
        metadata,
        {
            "schema": VANILLA_ARTIFACT_SCHEMA,
            "evidence": evidence_role,
            "client": client_identity(),
            "authoritative_result": (semantic or {}).get("authoritative_result")
            or "source completion marker plus transcript binding accounting; transport is separate",
            "source": str(source),
            "source_sha256": source_sha256,
            "executed_source_sha256": executed_source_sha256,
            "source_dependency_closure_sha256": closure.get("strict_sha256"),
            "source_dependency_resolved_literal_closure": closure.get("resolved_literal_closure"),
            "source_dependency_semantic_identity_complete": closure.get("semantic_identity_complete"),
            "source_dependency_boundary": closure.get("boundary"),
            "source_dependency_closure": closure,
            "dependency_transport_status": package.get("dependency_transport_status"),
            "dependency_transport_reason": package.get("dependency_transport_reason"),
            "source_preflight_status": package.get("source_preflight_status"),
            "source_pin": package.get("source_pin"),
        "project_basis": package.get("project_basis"),
        "preparation_package_root": package.get("preparation_package_root"),
            "dependency_package_files": package.get("files") or [],
            "literal_elf_transport": package.get("literal_elf_transport"),
            "profile_satisfaction": package.get("profile_satisfaction"),
            "profile_satisfied_dependencies": package.get("profile_satisfied_dependencies") or [],
            "transcript": str(transcript),
            "transcript_sha256": sha256_file(transcript),
            "raw_transcript": str(raw_transcript_output),
            "raw_transcript_sha256": sha256_file(raw_transcript_output),
            "logical_profile": logical_profile,
            "physical_profile": manifest.get("profile") or profile_root.name,
            "profile_basis_id": manifest.get("profile_basis_id"),
            "profile_base": manifest.get("profile_base"),
            "profile_sha256": manifest.get("profile_sha256"),
            "profile_cwd": manifest.get("profile_cwd"),
            "fork_snapshot_abi": manifest.get("fork_snapshot_abi"),
            "transport": transport,
            "transport_status": transport,
            "cleanup": cleanup,
            "worker_status": response.get("status") if response else None,
            "timeout_kind": response.get("timeout_kind") if response else None,
            "controller_cancellation_reason": response.get("controller_cancellation_reason") if response else None,
            "controller_response_timeout_seconds": response.get("controller_response_timeout_seconds") if response else None,
            "requested_timeout_seconds": response.get("requested_timeout_seconds") if response else None,
            "termination_kind": response.get("termination_kind") if response else None,
            "broker_child_quiescent": response.get("broker_child_quiescent") if response else None,
            "broker_seat_reusable": response.get("broker_seat_reusable") if response else None,
            "exit_status": (semantic or {}).get("effective_exit_status"),
            "worker_exit_status": response.get("exit_status") if response else None,
            "process_exit_status": response.get("exit_status") if response else None,
            "semantic_source_status": (semantic or {}).get("source_status"),
            "source_completed": (semantic or {}).get("source_completed"),
            "semantic_exit_status": (semantic or {}).get("semantic_exit_status"),
            "claims_complete": (semantic or {}).get("claims_complete"),
            "proof_diagnostics": (semantic or {}).get("proof_diagnostics"),
            "proof_activity": (semantic or {}).get("proof_activity"),
            "running_binding": (semantic or {}).get("running_binding"),
            "failing_binding": (semantic or {}).get("failing_binding"),
            "first_failure": (semantic or {}).get("first_failure"),
            "first_failure_transcript_line": (semantic or {}).get("first_failure_transcript_line"),
            "observed_bindings": (semantic or {}).get("observed_bindings") or [],
            "bindings": (semantic or {}).get("bindings") or [],
            "binding_counts": (semantic or {}).get("binding_counts") or {},
            "completion_marker_observed": (semantic or {}).get("completion_marker_observed"),
            "completion_marker_valid": (semantic or {}).get("completion_marker_valid"),
            "included_file_error_observed": (semantic or {}).get("included_file_error_observed"),
            **(
                {
                    "foundation_delta": semantic["foundation_delta"],
                    "promotion_advisory": semantic["promotion_advisory"],
                }
                if semantic is not None and "foundation_delta" in semantic
                else {}
            ),
            "transcript_accounting": semantic,
            "eval_elapsed_seconds": response.get("eval_elapsed_seconds") if response else None,
            "admission_wait_seconds": round(admission_wait_seconds, 3),
            "admission_slot": admission_slot,
            "effective_capacity": effective_capacity,
            "capacity_model": "bounded_multi_seat" if effective_capacity > 1 else "serialized_single_shelf",
            "evidence_boundary": (semantic or {}).get("evidence_boundary")
            or "raw transcript completion and binding accounting; not semantic_probe evidence",
        },
    )
    return metadata
