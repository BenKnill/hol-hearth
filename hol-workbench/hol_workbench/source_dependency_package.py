"""Transport a captured literal source closure without widening its trust boundary."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from hol_workbench.fork_child_ocaml import ocaml_string_literal
from hol_workbench.hashing import sha256_bytes
from hol_workbench.profile_satisfied_dependencies import (
    profile_runtime_fallback_record_indexes,
    profile_satisfied_record_indexes,
)
from hol_workbench.secure_tree_read import read_regular_file_beneath
from hol_workbench.source_dependency_closure import source_dependency_closure_identity_matches


class DependencyPackageError(RuntimeError):
    """A source closure cannot be transported as the package it declares."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


SESSION_DEPENDENCY_PROJECTION_SCHEMA = "hol-workbench.session-dependency-projection.v1"


def dependency_transport_status(
    closure: dict[str, Any],
    *,
    profile_satisfaction: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """Classify package transport independently from semantic identity completeness."""
    if not source_dependency_closure_identity_matches(closure):
        return "refused_invalid_closure_identity", "source dependency closure identity is invalid"
    project_inputs = closure["project_inputs"]
    if project_inputs.get("status") == "blocked":
        blockers = project_inputs.get("blockers") or []
        transport_status = str(project_inputs.get("transport_blocker_status") or "project_input_invalid")
        return transport_status, str(blockers[0] if blockers else "project input readiness is blocked")
    if closure.get("dynamic_loader_count"):
        return "refused_dynamic", "dynamic source-loader calls are outside the literal package contract"
    if closure.get("dynamic_artifact_count"):
        return (
            "refused_dynamic_artifact",
            "dynamic define_from_elf/define_assert_from_elf calls are outside the literal package contract",
        )
    if closure.get("limit_reasons"):
        return "refused_limits", "the bounded literal dependency scan did not finish"
    for record in closure.get("records") or []:
        if record.get("loader") == "#use" and record.get("declaring_file") != "<entrypoint>":
            return (
                "refused_nested_use",
                "nested #use is not runtime-exact outside the package entrypoint; replace it with needs/loadt/loads",
            )

    satisfied_indexes = profile_satisfied_record_indexes(profile_satisfaction, closure)
    runtime_indexes = profile_runtime_fallback_record_indexes(profile_satisfaction, closure)
    declared_satisfaction_count = len((profile_satisfaction or {}).get("edges") or [])
    if declared_satisfaction_count and len(satisfied_indexes | runtime_indexes) != declared_satisfaction_count:
        return "refused_profile_satisfaction_identity_invalid", "profile-satisfaction decision identity is invalid"

    unresolved_profile_fallback = False
    runtime_paths: dict[str, str] = {}
    artifact_runtime_paths: dict[str, str] = {}
    for artifact in closure.get("artifacts") or []:
        if artifact.get("resolution") != "source_local":
            continue
        runtime_literal = str(artifact.get("runtime_literal_path") or "")
        package_path = str(artifact.get("package_path") or "")
        if not runtime_literal or not package_path:
            continue
        previous = artifact_runtime_paths.setdefault(runtime_literal, package_path)
        if previous != package_path:
            return (
                "refused_runtime_path_collision",
                f"literal ELF path resolves to multiple package files: {runtime_literal}",
            )
    for index, record in enumerate(closure.get("records") or []):
        resolution = record.get("resolution")
        declared = str(record.get("declared_path") or "")
        if record.get("loader") == "load" and resolution in {"mounted_source", "unresolved_mounted_source"}:
            return (
                "refused_mapped_bare_load",
                f"mapped bare load is unsupported; replace it with needs, loadt, or loads: {declared}",
            )
        runtime_literal = str(record.get("runtime_literal_path") or "")
        package_path = str(record.get("package_path") or "")
        if resolution == "mounted_source" and runtime_literal and package_path:
            previous = runtime_paths.setdefault(runtime_literal, package_path)
            if previous != package_path:
                return (
                    "refused_runtime_path_collision",
                    f"logical source literal resolves to multiple package files: {runtime_literal}",
                )
        if resolution == "external_resolved":
            if index in satisfied_indexes:
                continue
            return (
                "refused_external_runtime_dependency",
                f"literal dependency resolves outside the declared source root: {declared}",
            )
        if record.get("symlinked") is not False:
            return (
                "refused_symlinked_dependency",
                f"literal dependency traverses a symlinked path: {declared}",
            )
        if resolution == "source_overlay_conflict":
            return (
                "refused_source_overlay_conflict",
                f"registered source worktree and exact cold HOLDIR differ at: {declared}",
            )
        if resolution == "unresolved_mounted_source":
            alias = str(record.get("logical_source_root") or "")
            command = f"hol-workbench/dev/reconcile-source-layout --materialize {alias}"
            return (
                "refused_missing_logical_source_dependency",
                f"logical source root {alias!r} has no regular target for {declared}; "
                f"run `{command}`, then rerun the same prove command",
            )
        if resolution == "unavailable_logical_source_root":
            alias = str(record.get("logical_source_root") or "")
            return (
                "refused_logical_source_root",
                f"declared logical source root {alias!r} is unavailable; run "
                f"`hol-workbench/dev/reconcile-source-layout --materialize {alias}` if it is managed",
            )
        if resolution == "invalid_logical_source_path":
            return (
                "refused_invalid_logical_source_path",
                f"logical source literal is not canonical ALIAS/nonempty/clean/path spelling: {declared}",
            )
        if resolution != "unresolved":
            continue
        if index in satisfied_indexes:
            continue
        if index in runtime_indexes:
            unresolved_profile_fallback = True
            continue
        path = Path(declared).expanduser()
        if path.is_absolute() or declared in {".", ".."} or declared.startswith(("./", "../")):
            return (
                "refused_missing_source_dependency",
                f"explicit dependency path is missing from the declared source root: {declared}",
            )
        if closure.get("cold_holdir_resolution_enabled"):
            return (
                "refused_missing_holdir_dependency",
                f"literal dependency is missing from the exact cold HOLDIR: {declared}",
            )
        unresolved_profile_fallback = True
    if unresolved_profile_fallback:
        return (
            "packaged_with_profile_fallback",
            "source-local files are packaged; unresolved library-style loads remain profile/HOL fallbacks",
        )
    if satisfied_indexes:
        return (
            "packaged_with_profile_satisfaction",
            "source-local files are packaged; exact literal needs are satisfied by the validated warm shelf",
        )
    return "packaged", "the complete literal source closure is transportable"


def session_dependency_projection(
    closure: dict[str, Any],
    *,
    profile_satisfaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a root-free package key which deliberately excludes entrypoint bytes."""

    status, reason = dependency_transport_status(closure, profile_satisfaction=profile_satisfaction)
    if not status.startswith("packaged"):
        raise DependencyPackageError(status, reason)
    files: list[dict[str, Any]] = []
    for record in closure.get("records") or []:
        resolution = record.get("resolution")
        if resolution not in {"source_local", "source_overlay", "holdir_source", "mounted_source"}:
            continue
        if record.get("traversal") not in {"followed", "already_seen", "cycle"}:
            continue
        files.append(
            {
                "kind": "source",
                "loader": record.get("loader"),
                "resolution": resolution,
                "declared_path": record.get("declared_path"),
                "runtime_literal_path": record.get("runtime_literal_path"),
                "package_path": record.get("package_path"),
                "logical_source_root": record.get("logical_source_root"),
                "sha256": record.get("sha256"),
                "size_bytes": record.get("size_bytes"),
            }
        )
    for artifact in closure.get("artifacts") or []:
        if artifact.get("resolution") != "source_local":
            continue
        files.append(
            {
                "kind": "elf",
                "loader": artifact.get("loader"),
                "name": artifact.get("name"),
                "runtime_literal_path": artifact.get("runtime_literal_path"),
                "package_path": artifact.get("package_path"),
                "logical_source_root": artifact.get("logical_source_root"),
                "sha256": artifact.get("sha256"),
                "size_bytes": artifact.get("size_bytes"),
            }
        )
    profile_edges = [
        {
            key: edge.get(key)
            for key in (
                "loader",
                "declared_path",
                "resolution",
                "mapping_root",
                "hol_relative_path",
                "shelf_path",
                "basename",
                "loader_md5",
                "sha256",
                "size_bytes",
            )
        }
        for edge in (profile_satisfaction or {}).get("edges") or []
    ]
    payload = {
        "schema": SESSION_DEPENDENCY_PROJECTION_SCHEMA,
        "entrypoint_package_path": (closure.get("entrypoint") or {}).get("package_path"),
        "logical_source_roots": closure.get("logical_source_roots"),
        "transport_status": status,
        "files": files,
        "profile_edges": profile_edges,
    }
    payload["strict_sha256"] = sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return payload


def _safe_package_path(value: str) -> Path:
    portable = PurePosixPath(value)
    if portable.is_absolute() or not portable.parts or any(part in {"", ".", ".."} for part in portable.parts):
        raise DependencyPackageError("refused_unsafe_path", f"unsafe portable package path: {value!r}")
    return Path(*portable.parts)


def dependency_package_entrypoint(destination: Path, closure: dict[str, Any]) -> Path:
    """Return the validated destination path for a closure entrypoint."""
    entry = closure.get("entrypoint") or {}
    return destination.expanduser().resolve() / _safe_package_path(str(entry.get("package_path") or ""))


def mounted_source_package_prelude(manifest: dict[str, Any] | None) -> list[str]:
    """Remap absolute mounted-source paths to immutable Linux package copies."""

    if not isinstance(manifest, dict):
        return []
    root_value = manifest.get("package_root")
    if not root_value:
        return []
    root = Path(str(root_value)).expanduser().resolve()
    mappings: list[tuple[str, str]] = []
    for row in manifest.get("files") or []:
        if not isinstance(row, dict) or row.get("role") not in {
            "mounted_source_dependency",
            "holdir_dependency",
        }:
            continue
        literal = str(row.get("runtime_literal_path") or "")
        package_path = str(row.get("package_path") or "")
        if not literal or not package_path:
            continue
        packaged = (root / _safe_package_path(package_path)).resolve()
        try:
            packaged.relative_to(root)
        except ValueError:
            continue
        mapping = (literal, str(packaged))
        if mapping not in mappings:
            mappings.append(mapping)
    if not mappings:
        return []
    pairs = "; ".join(
        f"({ocaml_string_literal(source)},{ocaml_string_literal(packaged)})" for source, packaged in mappings
    )
    return [
        "(* Harness wrapper: load Linux-mounted literal sources from their immutable package copies. *)",
        f"let proof_run_mounted_source_paths = [{pairs}];;",
        "let proof_run_mounted_source_path path =",
        "  try List.assoc path proof_run_mounted_source_paths with Not_found -> path;;",
        "let proof_run_unmapped_needs = needs;;",
        "let needs path = proof_run_unmapped_needs (proof_run_mounted_source_path path);;",
        "let proof_run_unmapped_loadt = loadt;;",
        "let loadt path = proof_run_unmapped_loadt (proof_run_mounted_source_path path);;",
        "let proof_run_unmapped_loads = loads;;",
        "let loads path = proof_run_unmapped_loads (proof_run_mounted_source_path path);;",
        "",
    ]


def literal_elf_artifact_package_prelude(manifest: dict[str, Any] | None) -> list[str]:
    """Remap exact literal ELF paths to immutable packaged object bytes."""

    if not isinstance(manifest, dict) or not manifest.get("package_root"):
        return []
    root = Path(str(manifest["package_root"])).expanduser().resolve()
    mappings: list[tuple[str, str]] = []
    loaders: list[str] = []
    for row in manifest.get("files") or []:
        if not isinstance(row, dict) or row.get("role") != "literal_elf_artifact":
            continue
        literal = str(row.get("runtime_literal_path") or "")
        package_path = str(row.get("package_path") or "")
        loader = str(row.get("loader") or "")
        if not literal or not package_path or loader not in {"define_from_elf", "define_assert_from_elf"}:
            continue
        packaged = (root / _safe_package_path(package_path)).resolve()
        try:
            packaged.relative_to(root)
        except ValueError:
            continue
        mapping = (literal, str(packaged))
        if mapping not in mappings:
            mappings.append(mapping)
        if loader not in loaders:
            loaders.append(loader)
    if not mappings:
        return []
    pairs = "; ".join(
        f"({ocaml_string_literal(source)},{ocaml_string_literal(packaged)})" for source, packaged in mappings
    )
    lines = [
        "(* Harness wrapper: resolve literal ELF inputs from their immutable dependency package. *)",
        f"let proof_run_literal_elf_paths = [{pairs}];;",
        "let proof_run_literal_elf_path path =",
        "  try List.assoc path proof_run_literal_elf_paths with Not_found -> path;;",
    ]
    for loader in loaders:
        original = f"proof_run_unmapped_{loader}"
        lines.extend(
            [
                f"let {original} = {loader};;",
                f"let {loader} name path = {original} name (proof_run_literal_elf_path path);;",
            ]
        )
    return [*lines, ""]


def _write_read_only(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(path.stat().st_mode & ~0o222)


def _materialize_session_dependency_package_unpublished(
    *,
    closure: dict[str, Any],
    destination: Path,
    projection: dict[str, Any],
    profile_satisfaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture dependency and ELF bytes, but never materialize the watched entrypoint."""

    current_projection = session_dependency_projection(
        closure,
        profile_satisfaction=profile_satisfaction,
    )
    if current_projection != projection:
        raise DependencyPackageError(
            "refused_dependency_projection_changed",
            "session dependency projection changed before materialization",
        )
    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=False)
    files: list[dict[str, Any]] = []
    claimed_paths: dict[str, tuple[str, str, int, str]] = {}

    for record in closure.get("records") or []:
        resolution = record.get("resolution")
        if resolution not in {"source_local", "source_overlay", "holdir_source", "mounted_source"}:
            continue
        if record.get("traversal") not in {"followed", "already_seen", "cycle"}:
            continue
        portable = str(record.get("package_path") or "")
        resolved_value = record.get("resolved_path")
        if not isinstance(resolved_value, str) or not resolved_value:
            raise DependencyPackageError("refused_missing_manifest_path", f"dependency path missing: {portable}")
        resolved = Path(resolved_value)
        role = (
            "holdir_dependency"
            if resolution == "holdir_source"
            else "mounted_source_dependency"
            if resolution == "mounted_source"
            else "source_overlay_dependency"
            if resolution == "source_overlay"
            else "dependency"
        )
        identity = (
            role,
            str(record.get("sha256") or ""),
            int(record.get("size_bytes") or 0),
            str(Path(os.path.abspath(resolved))),
        )
        previous = claimed_paths.get(portable)
        if previous is not None:
            if previous == identity:
                continue
            raise DependencyPackageError(
                "refused_package_path_collision",
                f"dependency package path collision: {portable}",
            )
        claimed_paths[portable] = identity
        relative = _safe_package_path(portable)
        trusted_root_value = record.get("trusted_root_path")
        if not isinstance(trusted_root_value, str) or not trusted_root_value:
            raise DependencyPackageError("refused_missing_manifest_path", f"dependency trusted root missing: {portable}")
        try:
            data = read_regular_file_beneath(Path(trusted_root_value), resolved).data
        except OSError as exc:
            raise DependencyPackageError(
                "refused_dependency_changed",
                f"dependency path changed or became aliased after identity capture: {portable}",
            ) from exc
        if sha256_bytes(data) != record.get("sha256") or len(data) != record.get("size_bytes"):
            raise DependencyPackageError(
                "refused_dependency_changed",
                f"dependency bytes changed after identity capture: {portable}; rerun prove SOURCE",
            )
        _write_read_only(destination / relative, data)
        file_record = {
            "role": role,
            "package_path": relative.as_posix(),
            "sha256": record.get("sha256"),
            "size_bytes": record.get("size_bytes"),
            "declared_theorems": record.get("declared_theorems") or [],
            "materialized_from_symlink": bool(record.get("symlinked")),
        }
        if resolution in {"mounted_source", "holdir_source"} and record.get("runtime_literal_path"):
            file_record["runtime_literal_path"] = record["runtime_literal_path"]
        if resolution == "holdir_source" and record.get("runtime_literal_path"):
            file_record["legacy_holdir_coordinate"] = bool(record.get("legacy_holdir_coordinate"))
        files.append(file_record)

    for artifact in closure.get("artifacts") or []:
        if artifact.get("resolution") != "source_local":
            continue
        portable = str(artifact.get("package_path") or "")
        resolved_value = artifact.get("resolved_path")
        if not isinstance(resolved_value, str) or not resolved_value:
            raise DependencyPackageError("refused_missing_manifest_path", f"ELF artifact path missing: {portable}")
        resolved = Path(resolved_value)
        identity = (
            "literal_elf_artifact",
            str(artifact.get("sha256") or ""),
            int(artifact.get("size_bytes") or 0),
            str(Path(os.path.abspath(resolved))),
        )
        previous = claimed_paths.get(portable)
        if previous is not None:
            if previous == identity:
                continue
            raise DependencyPackageError(
                "refused_package_path_collision",
                f"dependency package path collision: {portable}",
            )
        claimed_paths[portable] = identity
        relative = _safe_package_path(portable)
        project_root = artifact.get("project_root")
        if not isinstance(project_root, str) or not project_root:
            raise DependencyPackageError("refused_missing_manifest_path", f"ELF trusted root missing: {portable}")
        try:
            data = read_regular_file_beneath(Path(project_root), resolved).data
        except OSError as exc:
            raise DependencyPackageError(
                "refused_dependency_changed",
                f"ELF artifact path changed or became aliased after identity capture: {portable}",
            ) from exc
        if sha256_bytes(data) != artifact.get("sha256") or len(data) != artifact.get("size_bytes"):
            raise DependencyPackageError(
                "refused_dependency_changed",
                f"ELF artifact bytes changed after identity capture: {portable}; rerun prove SOURCE",
            )
        _write_read_only(destination / relative, data)
        files.append(
            {
                "role": "literal_elf_artifact",
                "package_path": relative.as_posix(),
                "sha256": artifact.get("sha256"),
                "size_bytes": artifact.get("size_bytes"),
                "loader": artifact.get("loader"),
                "name": artifact.get("name"),
                "runtime_literal_path": artifact.get("runtime_literal_path"),
                "materialized_from_symlink": bool(artifact.get("symlinked")),
            }
        )

    virtual_entrypoint = destination / _safe_package_path(str(projection.get("entrypoint_package_path") or ""))
    if virtual_entrypoint.exists() or virtual_entrypoint.is_symlink():
        raise DependencyPackageError(
            "refused_package_path_collision",
            "a dependency occupied the virtual session entrypoint path",
        )
    return {
        "schema": SESSION_DEPENDENCY_PROJECTION_SCHEMA,
        "dependency_projection_sha256": projection["strict_sha256"],
        "package_root": str(destination),
        "virtual_entrypoint": str(virtual_entrypoint),
        "files": files,
        "profile_satisfaction": profile_satisfaction,
    }


def materialize_session_dependency_package(
    *,
    closure: dict[str, Any],
    destination: Path,
    projection: dict[str, Any],
    profile_satisfaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically publish one session-ephemeral package with no entrypoint file."""

    destination = destination.expanduser()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"session dependency package destination already exists: {destination}")
    destination = destination.parent.resolve() / destination.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)).resolve()
    staging = staging_parent / "package"
    try:
        manifest = _materialize_session_dependency_package_unpublished(
            closure=closure,
            destination=staging,
            projection=projection,
            profile_satisfaction=profile_satisfaction,
        )
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"session dependency package destination appeared: {destination}")
        virtual_relative = Path(str(manifest["virtual_entrypoint"])).relative_to(staging)
        staging.rename(destination)
        manifest["package_root"] = str(destination)
        manifest["virtual_entrypoint"] = str(destination / virtual_relative)
        return manifest
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


def _materialize_dependency_package_unpublished(
    *,
    source: Path,
    closure: dict[str, Any],
    destination: Path,
    entrypoint_output_bytes: bytes | None = None,
    profile_satisfaction: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Build verified package bytes at a private unpublished destination."""
    status, reason = dependency_transport_status(closure, profile_satisfaction=profile_satisfaction)
    if not status.startswith("packaged"):
        raise DependencyPackageError(status, reason)

    destination = destination.expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=False)
    source_path = Path(os.path.abspath(source.expanduser()))
    try:
        source_bytes = read_regular_file_beneath(Path(str(closure.get("root") or "")), source_path).data
    except OSError as exc:
        raise DependencyPackageError(
            "refused_source_changed",
            f"entrypoint is no longer a safe regular file below its captured root: {exc}",
        ) from exc
    entry = closure.get("entrypoint") or {}
    if sha256_bytes(source_bytes) != entry.get("sha256") or len(source_bytes) != entry.get("size_bytes"):
        raise DependencyPackageError(
            "refused_source_changed",
            "entrypoint bytes changed after dependency identity capture; rerun prove SOURCE",
        )

    entrypoint = dependency_package_entrypoint(destination, closure)
    entrypoint_relative = entrypoint.relative_to(destination)
    _write_read_only(entrypoint, entrypoint_output_bytes if entrypoint_output_bytes is not None else source_bytes)
    files = [
        {
            "role": "entrypoint",
            "package_path": entrypoint_relative.as_posix(),
            "sha256": entry.get("sha256"),
            "size_bytes": entry.get("size_bytes"),
            "executed_bytes_instrumented": entrypoint_output_bytes is not None,
        }
    ]

    claimed_paths: dict[str, tuple[str, str, int, str]] = {
        entrypoint_relative.as_posix(): (
            "entrypoint",
            str(entry.get("sha256") or ""),
            int(entry.get("size_bytes") or 0),
            str(source_path),
        )
    }
    for record in closure.get("records") or []:
        resolution = record.get("resolution")
        if resolution not in {"source_local", "source_overlay", "holdir_source", "mounted_source"} or record.get(
            "traversal"
        ) not in {
            "followed",
            "already_seen",
            "cycle",
        }:
            continue
        portable = str(record.get("package_path") or "")
        resolved_value = record.get("resolved_path")
        if not isinstance(resolved_value, str) or not resolved_value:
            raise DependencyPackageError("refused_missing_manifest_path", f"dependency path missing: {portable}")
        resolved = Path(resolved_value)
        role = (
            "holdir_dependency"
            if resolution == "holdir_source"
            else "mounted_source_dependency"
            if resolution == "mounted_source"
            else "source_overlay_dependency"
            if resolution == "source_overlay"
            else "dependency"
        )
        identity = (
            role,
            str(record.get("sha256") or ""),
            int(record.get("size_bytes") or 0),
            str(Path(os.path.abspath(resolved))),
        )
        previous = claimed_paths.get(portable)
        if previous is not None:
            if previous == identity:
                continue
            raise DependencyPackageError(
                "refused_package_path_collision",
                f"dependency package path collision: {portable}",
            )
        claimed_paths[portable] = identity
        relative = _safe_package_path(portable)
        trusted_root_value = record.get("trusted_root_path")
        if not isinstance(trusted_root_value, str) or not trusted_root_value:
            raise DependencyPackageError("refused_missing_manifest_path", f"dependency trusted root missing: {portable}")
        try:
            data = read_regular_file_beneath(Path(trusted_root_value), resolved).data
        except OSError as exc:
            raise DependencyPackageError(
                "refused_dependency_changed",
                f"dependency path changed or became aliased after identity capture: {portable}",
            ) from exc
        if sha256_bytes(data) != record.get("sha256") or len(data) != record.get("size_bytes"):
            raise DependencyPackageError(
                "refused_dependency_changed",
                f"dependency bytes changed after identity capture: {portable}; rerun prove SOURCE",
            )
        _write_read_only(destination / relative, data)
        file_record = {
            "role": role,
            "package_path": relative.as_posix(),
            "sha256": record.get("sha256"),
            "size_bytes": record.get("size_bytes"),
            "declared_theorems": record.get("declared_theorems") or [],
            "materialized_from_symlink": bool(record.get("symlinked")),
        }
        if resolution == "mounted_source" and record.get("runtime_literal_path"):
            file_record["runtime_literal_path"] = record["runtime_literal_path"]
        if resolution == "holdir_source" and record.get("runtime_literal_path"):
            file_record["runtime_literal_path"] = record["runtime_literal_path"]
            file_record["legacy_holdir_coordinate"] = bool(record.get("legacy_holdir_coordinate"))
        files.append(file_record)

    for artifact in closure.get("artifacts") or []:
        if artifact.get("resolution") != "source_local":
            continue
        portable = str(artifact.get("package_path") or "")
        resolved_value = artifact.get("resolved_path")
        if not isinstance(resolved_value, str) or not resolved_value:
            raise DependencyPackageError("refused_missing_manifest_path", f"ELF artifact path missing: {portable}")
        resolved = Path(resolved_value)
        identity = (
            "literal_elf_artifact",
            str(artifact.get("sha256") or ""),
            int(artifact.get("size_bytes") or 0),
            str(Path(os.path.abspath(resolved))),
        )
        previous = claimed_paths.get(portable)
        if previous is not None:
            if previous == identity:
                continue
            raise DependencyPackageError(
                "refused_package_path_collision",
                f"dependency package path collision: {portable}",
            )
        claimed_paths[portable] = identity
        relative = _safe_package_path(portable)
        project_root = artifact.get("project_root")
        if not isinstance(project_root, str) or not project_root:
            raise DependencyPackageError("refused_missing_manifest_path", f"ELF trusted root missing: {portable}")
        try:
            data = read_regular_file_beneath(Path(project_root), resolved).data
        except OSError as exc:
            raise DependencyPackageError(
                "refused_dependency_changed",
                f"ELF artifact path changed or became aliased after identity capture: {portable}",
            ) from exc
        if sha256_bytes(data) != artifact.get("sha256") or len(data) != artifact.get("size_bytes"):
            raise DependencyPackageError(
                "refused_dependency_changed",
                f"ELF artifact bytes changed after identity capture: {portable}; rerun prove SOURCE",
            )
        _write_read_only(destination / relative, data)
        files.append(
            {
                "role": "literal_elf_artifact",
                "package_path": relative.as_posix(),
                "sha256": artifact.get("sha256"),
                "size_bytes": artifact.get("size_bytes"),
                "loader": artifact.get("loader"),
                "name": artifact.get("name"),
                "runtime_literal_path": artifact.get("runtime_literal_path"),
                "materialized_from_symlink": bool(artifact.get("symlinked")),
            }
        )

    has_use_directive = any(record.get("loader") == "#use" for record in closure.get("records") or [])
    has_literal_elf = any(row[0] == "literal_elf_artifact" for row in claimed_paths.values())
    runtime_cwd = entrypoint.parent if has_use_directive else destination if has_literal_elf else None

    return entrypoint, {
        "dependency_transport_status": status,
        "dependency_transport_reason": reason,
        "source_dependency_closure_sha256": closure.get("strict_sha256"),
        "source_dependency_semantic_identity_complete": closure.get("semantic_identity_complete"),
        "logical_source_roots": closure.get("logical_source_roots"),
        "package_root": str(destination),
        "runtime_cwd": str(runtime_cwd.resolve()) if runtime_cwd is not None else None,
        "entrypoint": str(entrypoint),
        "files": files,
        "profile_satisfaction": profile_satisfaction,
        "profile_satisfied_dependencies": [
            edge
            for edge in (profile_satisfaction or {}).get("edges") or []
            if edge.get("resolution") == "profile_satisfied"
        ],
        "profile_runtime_dependencies": [
            edge
            for edge in (profile_satisfaction or {}).get("edges") or []
            if edge.get("resolution") == "profile_runtime_fallback"
        ],
        "exact_source_composition": {
            "evidence": "mixed_exact_file_bound_sources",
            "claim_boundary": (
                "each profile-supplied file matches the admitted loaded-file inventory; each packaged file "
                "matches the captured closure and any managed generation row; no repository-wide compatibility "
                "or final Git-object replay is claimed"
            ),
            "profile_supplied": [
                {
                    key: edge.get(key)
                    for key in ("declared_path", "sha256", "size_bytes", "mapping_root", "hol_relative_path")
                }
                for edge in (profile_satisfaction or {}).get("edges") or []
                if edge.get("resolution") == "profile_satisfied"
            ],
            "packaged": [
                {key: row.get(key) for key in ("role", "package_path", "sha256", "size_bytes")}
                for row in files
                if row.get("role") != "entrypoint"
            ],
        },
    }


def materialize_dependency_package(
    *,
    source: Path,
    closure: dict[str, Any],
    destination: Path,
    entrypoint_output_bytes: bytes | None = None,
    profile_satisfaction: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Stage, verify, and atomically publish one link-free source package."""
    destination = destination.expanduser()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"dependency package destination already exists: {destination}")
    destination = destination.parent.resolve() / destination.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"dependency package destination already exists: {destination}")
    staging_parent = Path(tempfile.mkdtemp(prefix=f".{destination.name}.staging-", dir=destination.parent)).resolve()
    staging = staging_parent / "package"
    try:
        staged_entrypoint, manifest = _materialize_dependency_package_unpublished(
            source=source,
            closure=closure,
            destination=staging,
            entrypoint_output_bytes=entrypoint_output_bytes,
            profile_satisfaction=profile_satisfaction,
        )
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"dependency package destination appeared during capture: {destination}")
        entrypoint_relative = staged_entrypoint.relative_to(staging)
        runtime_cwd_value = manifest.get("runtime_cwd")
        runtime_cwd_relative = (
            Path(str(runtime_cwd_value)).relative_to(staging) if runtime_cwd_value is not None else None
        )
        staging.rename(destination)
        entrypoint = destination / entrypoint_relative
        manifest["package_root"] = str(destination)
        manifest["entrypoint"] = str(entrypoint)
        if runtime_cwd_relative is not None:
            manifest["runtime_cwd"] = str((destination / runtime_cwd_relative).resolve())
        return entrypoint, manifest
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
