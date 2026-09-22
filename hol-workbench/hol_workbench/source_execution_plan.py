"""Shared source-resolution plan for durable replay and memory-first authoring."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hol_workbench.logical_source_roots import (
    resolve_logical_project_roots,
    resolve_logical_source_roots,
    validate_requested_logical_source_roots,
)
from hol_workbench.profile_satisfied_dependencies import (
    build_profile_satisfaction,
    profile_satisfied_needs_prelude,
)
from hol_workbench.runtime_config import RuntimeConfigError, load_runtime_config
from hol_workbench.source_dependency_closure import build_source_dependency_closure
from hol_workbench.source_dependency_package import (
    dependency_transport_status,
    literal_elf_artifact_package_prelude,
    mounted_source_package_prelude,
)
from hol_workbench.source_load_transport import source_local_needs_prelude, source_package_runtime_prelude


def machine_holdir_authority() -> Path | None:
    """Return the selected host runtime's Linux HOL root, never a caller mapping."""

    try:
        holdir = load_runtime_config().hol_light_dir
    except RuntimeConfigError:
        return None
    try:
        resolved = holdir.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def capture_source_dependency_closure(
    source: Path,
    *,
    profile_cwd: Path | None,
    legacy_holdir_roots: tuple[Path, ...],
    logical_source_root_declarations: tuple[dict[str, str], ...],
    holdir_root_override: Path | None = None,
) -> tuple[dict[str, Any], Path | None]:
    """Resolve and capture the exact recursive source/artifact closure once."""

    holdir_root = (
        holdir_root_override.expanduser().resolve(strict=True)
        if holdir_root_override is not None
        else machine_holdir_authority()
    )
    logical_source_roots = resolve_logical_source_roots(
        source,
        logical_source_root_declarations,
        profile_cwd=profile_cwd,
    )
    validate_requested_logical_source_roots(
        source,
        logical_source_root_declarations,
        logical_source_roots,
    )
    logical_project_roots = resolve_logical_project_roots(
        source,
        logical_source_root_declarations,
        logical_source_roots,
    )
    closure = build_source_dependency_closure(
        source,
        holdir_root=holdir_root,
        legacy_holdir_roots=legacy_holdir_roots,
        logical_source_root_declarations=logical_source_root_declarations,
        logical_source_roots=logical_source_roots,
        logical_project_roots=logical_project_roots,
    )
    return closure, holdir_root


def decide_profile_satisfaction(
    closure: dict[str, Any],
    *,
    profile_root: Path,
    logical_profile: str,
    profile_cwd: Path | None,
    holdir_root: Path | None,
) -> tuple[dict[str, Any] | None, str, str]:
    """Apply the shared exact warm-shelf decision to one captured closure."""

    profile_satisfaction: dict[str, Any] | None = None
    transport_status, transport_reason = dependency_transport_status(closure)
    has_profile_needs_candidate = any(
        isinstance(record, dict)
        and record.get("loader") == "needs"
        and record.get("resolution") in {
            "external_resolved", "mounted_source", "unresolved", "source_local", "source_overlay", "holdir_source",
        }
        for record in closure.get("records") or []
    )
    if (holdir_root is not None or profile_cwd is not None) and has_profile_needs_candidate:
        candidate = build_profile_satisfaction(
            closure,
            profile_root=profile_root,
            logical_profile=logical_profile,
            host_holdir=holdir_root,
            host_profile_cwd=profile_cwd,
        )
        if candidate.get("edges") or candidate.get("captured_warm_sources"):
            profile_satisfaction = candidate
            transport_status, transport_reason = dependency_transport_status(
                closure,
                profile_satisfaction=profile_satisfaction,
            )
    return profile_satisfaction, transport_status, transport_reason


def source_execution_prelude(
    *,
    package_root: Path,
    virtual_entrypoint: Path,
    closure: dict[str, Any],
    profile_satisfaction: dict[str, Any] | None,
    literal_elf_runtime_cwd: Path | None = None,
) -> bytes:
    """Build the one shared path/ELF prelude for replay or a disposable loop child."""

    root_loads = tuple(dict.fromkeys(
        ((package_root / (closure["entrypoint"]["package_path"] if record["declaring_file"] == "<entrypoint>"
                          else record["declaring_file"])).parent,
         str(record["declared_path"]), package_root / record["package_path"])
        for record in closure.get("records") or []
        if record.get("resolution") == "source_local" and record.get("resolution_base") == "source_package_root"
    ))
    lines = [
        *source_package_runtime_prelude(
            literal_elf_runtime_cwd
            if literal_elf_runtime_cwd is not None
            else package_root
            if closure.get("literal_artifact_count")
            else None
        ),
        *source_local_needs_prelude(virtual_entrypoint, source_context=virtual_entrypoint,
                                    source_root_loads=root_loads),
        *mounted_source_package_prelude(
            {
                "package_root": str(package_root),
                "files": [
                    {
                        "role": (
                            "holdir_dependency"
                            if record.get("resolution") == "holdir_source"
                            else "mounted_source_dependency"
                        ),
                        "package_path": record.get("package_path"),
                        "runtime_literal_path": record.get("runtime_literal_path"),
                    }
                    for record in closure.get("records") or []
                    if record.get("resolution") in {"mounted_source", "holdir_source"}
                ],
            }
        ),
        *(
            []
            if literal_elf_runtime_cwd is not None
            else literal_elf_artifact_package_prelude(
                {
                    "package_root": str(package_root),
                    "files": [
                        {
                            "role": "literal_elf_artifact",
                            "package_path": artifact.get("package_path"),
                            "runtime_literal_path": artifact.get("runtime_literal_path"),
                            "loader": artifact.get("loader"),
                        }
                        for artifact in closure.get("artifacts") or []
                        if artifact.get("resolution") == "source_local"
                    ],
                }
            )
        ),
        *profile_satisfied_needs_prelude(profile_satisfaction),
    ]
    return "\n".join(lines).encode("utf-8")
