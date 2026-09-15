"""Canonical project-input root selection shared by every readiness consumer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

PROJECT_INPUT_CONTEXT_SCHEMA = "hol-workbench.project-input-context.v1"
PROJECT_INPUT_ROOT_PRECEDENCE = (
    "explicit_project_or_profile_root",
    "nearest_nonsymlink_repository_root",
    "bounded_source_dependency_root",
    "nearest_bounded_literal_artifact_anchor",
    "source_entrypoint_parent",
)


def nearest_repository_root(entrypoint: Path) -> Path | None:
    """Return the nearest nonsymlink Git repository containing ``entrypoint``."""

    source = entrypoint.expanduser().resolve()
    for candidate in (source.parent, *source.parent.parents):
        marker = candidate / ".git"
        if not marker.is_symlink() and (marker.is_dir() or marker.is_file()):
            return candidate.resolve()
    return None


def project_root_from_profile_context(source: Path, profile_cwd: Path | None) -> Path | None:
    """Use a profile cwd as a project root only when it contains the source."""

    if profile_cwd is None:
        return None
    entrypoint = source.expanduser().resolve()
    candidate = profile_cwd.expanduser().resolve()
    try:
        entrypoint.relative_to(candidate)
    except ValueError:
        return None
    return candidate


def _portable_relative(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path, base)).as_posix()


def _canonical_root_facts(*, entrypoint: Path, source_root: Path, project_root: Path) -> dict[str, Any]:
    """Describe only root facts, never consumer-local selection provenance."""

    try:
        entrypoint_relative = entrypoint.relative_to(project_root).as_posix()
        contains_entrypoint = True
    except ValueError:
        entrypoint_relative = None
        contains_entrypoint = False
    return {
        "schema": PROJECT_INPUT_CONTEXT_SCHEMA,
        "kind": "canonical_project_input_root",
        "precedence": list(PROJECT_INPUT_ROOT_PRECEDENCE),
        "project_root_from_entrypoint_parent": _portable_relative(project_root, entrypoint.parent),
        "source_root_from_project_root": _portable_relative(source_root, project_root),
        "project_root_contains_entrypoint": contains_entrypoint,
        "entrypoint_relative_path": entrypoint_relative,
        "literal_path_policy": (
            "absolute and relative literals are bounded by canonical lexical and resolved containment in the "
            "project root; explicit ./ and ../ literals use the declaring-file parent"
        ),
    }


def _literal_artifact_anchor(
    entrypoint: Path,
    declared_artifact_paths: list[str],
    *,
    max_parent_ascents: int,
) -> Path | None:
    top_level_names = sorted(
        {
            raw.parts[0]
            for value in declared_artifact_paths
            if value
            for raw in [Path(value).expanduser()]
            if not raw.is_absolute() and raw.parts and all(part not in {"", ".", ".."} for part in raw.parts)
        }
    )
    if not top_level_names:
        return None
    for ascents, candidate in enumerate((entrypoint.parent, *entrypoint.parent.parents)):
        if ascents > max_parent_ascents:
            break
        if all((candidate / name).exists() and not (candidate / name).is_symlink() for name in top_level_names):
            return candidate.resolve()
    return None


def resolve_project_input_context(
    *,
    entrypoint: Path,
    source_root: Path,
    source_boundary: dict[str, Any],
    declared_artifact_paths: list[str],
    explicit_project_root: Path | None,
    max_parent_ascents: int,
) -> dict[str, Any]:
    """Resolve and serialize the one canonical context used by all consumers."""

    source = entrypoint.expanduser().resolve()
    bounded_source_root = source_root.expanduser().resolve()
    if explicit_project_root is not None:
        root = explicit_project_root.expanduser().resolve()
    else:
        root = nearest_repository_root(source)
        if root is None and source_boundary.get("kind") != "entrypoint_parent":
            root = bounded_source_root
        if root is None:
            root = _literal_artifact_anchor(
                source,
                declared_artifact_paths,
                max_parent_ascents=max_parent_ascents,
            )
        if root is None:
            root = bounded_source_root
    root_facts = _canonical_root_facts(
        entrypoint=source,
        source_root=bounded_source_root,
        project_root=root,
    )
    return {
        "schema": PROJECT_INPUT_CONTEXT_SCHEMA,
        "source": str(source),
        "source_dependency_root": str(bounded_source_root),
        "project_root": str(root),
        "root_facts": root_facts,
    }


def project_input_context_identity_payload(context: dict[str, Any]) -> dict[str, Any]:
    """Return the portable canonical-root facts bound into readiness identities."""

    return {
        "schema": context.get("schema"),
        "root_facts": context.get("root_facts"),
    }


def project_input_context_is_consistent(context: object) -> bool:
    if not isinstance(context, dict) or context.get("schema") != PROJECT_INPUT_CONTEXT_SCHEMA:
        return False
    source_value = context.get("source")
    source_root_value = context.get("source_dependency_root")
    project_root_value = context.get("project_root")
    if (
        not isinstance(source_value, str)
        or not source_value
        or not isinstance(source_root_value, str)
        or not source_root_value
        or not isinstance(project_root_value, str)
        or not project_root_value
    ):
        return False
    source = Path(source_value).expanduser().resolve()
    source_root = Path(source_root_value).expanduser().resolve()
    project_root = Path(project_root_value).expanduser().resolve()
    return context.get("root_facts") == _canonical_root_facts(
        entrypoint=source,
        source_root=source_root,
        project_root=project_root,
    )
