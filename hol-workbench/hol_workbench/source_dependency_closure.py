"""Ordered, content-addressed static closure for literal source loads."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hol_workbench.hashing import sha256_bytes, sha256_file
from hol_workbench.jsonio import atomic_write_json, read_json
from hol_workbench.logical_source_roots import (
    classify_logical_literal,
    logical_source_root_identity,
    logical_source_root_identity_is_valid,
    validate_managed_source_file,
)
from hol_workbench.proofs.loader_scan import LoaderScanResult, scan_ocaml_loaders
from hol_workbench.proofs.project_input_context import (
    nearest_repository_root,
    project_input_context_identity_payload,
    project_input_context_is_consistent,
    resolve_project_input_context,
)
from hol_workbench.proofs.project_inputs import (
    build_project_input_projection,
    project_input_projection_sha256,
)
from hol_workbench.proofs.source import (
    DIRECT_THEOREM_RE,
    THEOREM_RE,
    mask_hol_backquote_interiors,
    mask_ocaml_comments_and_strings,
)
from hol_workbench.secure_tree_read import read_regular_file_beneath

DEPENDENCY_CLOSURE_SCHEMA = "proof-run.source-dependency-closure.v16"
DEPENDENCY_IDENTITY_SCOPE = (
    "declared source package entrypoint plus ordered statically resolved per-declaring-file source-local, "
    "profile-declared logical source-root, explicit-marker or nearest-repository-bounded package source, registered same-repository "
    "linked-worktree overlay, and exact "
    "cold-HOLDIR strict-token literal needs/loadt/loads/load/#use closure plus the same exact-literal, canonical-"
    "project-input-context-bound define_from_elf/define_assert_from_elf projection including lexical refusal, "
    "dynamic, and collision records; "
    "absolute and dynamic source-loader paths plus profile/basis files are excluded"
)
SOURCE_ROOT_MARKER = ".hol-workbench-source-root"
DEFAULT_MAX_DEPTH = 32
DEFAULT_MAX_FILES = 200
DEFAULT_MAX_IMPLICIT_PARENT_ASCENTS = 8
SOURCE_ANALYSIS_CACHE_SCHEMA = "hol-workbench.source-lexical-analysis-cache.v1"
SOURCE_ANALYSIS_REVISION = "strict-token-loader-project-input-theorem-v4"


class SourceDependencyInferenceError(OSError):
    """Bounded source-package inference cannot safely admit the source."""

    status = "refused_source_dependency_inference_limit"

    def __init__(
        self,
        *,
        limit_kind: str,
        maximum: int,
        observed: int,
        dependency: Path,
    ) -> None:
        self.limit_kind = limit_kind
        self.maximum = maximum
        self.observed = observed
        self.dependency = dependency
        marker_guidance = (
            f"add {SOURCE_ROOT_MARKER} at the exact source package root"
            if limit_kind == "parent_ascent"
            else "inspect the literal source closure and its configured bound"
        )
        super().__init__(
            "source dependency inference refused: "
            f"{limit_kind} bound exceeded while resolving {dependency} "
            f"(observed {observed}, maximum {maximum}); no proof evaluation was started. "
            "Send the source path plus the bounded DETAILS receipt to the Hearth development lane; "
            f"developers must {marker_guidance}."
        )

    def record(self) -> dict[str, object]:
        return {
            "status": self.status,
            "limit_kind": self.limit_kind,
            "maximum": self.maximum,
            "observed": self.observed,
            "dependency": str(self.dependency),
            "development_lane_required": True,
        }


def _analysis_cache_path(root: Path, source_sha256: str) -> Path:
    return root / SOURCE_ANALYSIS_REVISION / source_sha256[:2] / f"{source_sha256}.json"


def _cached_source_analysis(root: Path, source_sha256: str) -> dict[str, Any] | None:
    try:
        payload = read_json(_analysis_cache_path(root, source_sha256))
    except UnicodeError:
        # Cache damage is a miss; the exact source bytes still go through the
        # ordinary strict scanner before replacement analysis is written.
        return None
    analysis = payload.get("analysis")
    if (
        payload.get("schema") != SOURCE_ANALYSIS_CACHE_SCHEMA
        or payload.get("analyzer_revision") != SOURCE_ANALYSIS_REVISION
        or payload.get("source_sha256") != source_sha256
        or not isinstance(analysis, dict)
    ):
        return None
    edges = analysis.get("edges")
    dynamic = analysis.get("dynamic")
    artifacts = analysis.get("artifact_refs")
    dynamic_artifacts = analysis.get("dynamic_artifact_refs")
    theorems = analysis.get("declared_theorems")
    if (
        not isinstance(edges, list)
        or not all(isinstance(item, dict) for item in edges)
        or not isinstance(dynamic, list)
        or not all(isinstance(item, dict) for item in dynamic)
        or not isinstance(artifacts, list)
        or not all(isinstance(item, dict) for item in artifacts)
        or not isinstance(dynamic_artifacts, list)
        or not all(isinstance(item, dict) for item in dynamic_artifacts)
        or not isinstance(theorems, list)
        or not all(isinstance(item, str) for item in theorems)
    ):
        return None
    return {
        "edges": edges,
        "dynamic": dynamic,
        "artifact_refs": artifacts,
        "dynamic_artifact_refs": dynamic_artifacts,
        "declared_theorems": theorems,
    }


def _write_source_analysis_cache(root: Path, source_sha256: str, analysis: dict[str, Any]) -> bool:
    try:
        atomic_write_json(
            _analysis_cache_path(root, source_sha256),
            {
                "schema": SOURCE_ANALYSIS_CACHE_SCHEMA,
                "analyzer_revision": SOURCE_ANALYSIS_REVISION,
                "source_sha256": source_sha256,
                "analysis": analysis,
            },
        )
    except OSError:
        return False
    return True


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _portable_path(
    path: Path,
    *,
    entrypoint: Path,
    root: Path,
    holdir_root: Path | None = None,
    source_overlay_root: Path | None = None,
    logical_source_roots: dict[str, Path] | None = None,
) -> str:
    if path == entrypoint:
        return "<entrypoint>"
    if _path_within(path, root):
        return path.relative_to(root).as_posix()
    if holdir_root is not None and _path_within(path, holdir_root):
        return f"<holdir>/{path.relative_to(holdir_root).as_posix()}"
    if source_overlay_root is not None and _path_within(path, source_overlay_root):
        return f"<source-overlay>/{path.relative_to(source_overlay_root).as_posix()}"
    for alias, logical_root in sorted((logical_source_roots or {}).items()):
        if _path_within(path, logical_root):
            return f"<logical:{alias}>/{path.relative_to(logical_root).as_posix()}"
    return "<external>"


def _declared_theorem_names(masked: str) -> list[str]:
    """Extract reporting-only theorem names from one shared lexical pass."""

    matches = [*THEOREM_RE.finditer(masked), *DIRECT_THEOREM_RE.finditer(masked)]
    names: list[str] = []
    for match in sorted(matches, key=lambda item: item.start()):
        name = match.group(1)
        if name not in names:
            names.append(name)
    return names


def _implicit_literal_source_boundary(
    entrypoint: Path,
    *,
    scan_entrypoint: Path,
    analysis_cache_root: Path | None,
    max_depth: int,
    max_files: int,
) -> tuple[Path, dict[str, Any]]:
    """Infer the smallest bounded root containing existing relative literal loads."""

    initial_root = entrypoint.parent.resolve()
    root = initial_root
    pending = [(scan_entrypoint, entrypoint, 1)]
    visited = {entrypoint}
    followed_files = 0
    observed_parent_ascents = 0

    def scan(path: Path) -> list[dict[str, Any]]:
        data = path.read_bytes()
        if analysis_cache_root is not None:
            cached = _cached_source_analysis(analysis_cache_root, sha256_bytes(data))
            if cached is not None:
                return cached["edges"]
        edges, _dynamic = _source_load_records(scan_ocaml_loaders(data))
        return edges

    while pending:
        scan_path, declaring_file, depth = pending.pop()
        try:
            edges = scan(scan_path)
        except OSError:
            continue
        for edge in edges:
            declared = str(edge.get("declared_path") or "")
            raw = Path(declared).expanduser()
            if raw.is_absolute() or declared in {"", ".", ".."}:
                continue
            candidate = declaring_file.parent / raw
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not resolved.is_file() or Path(os.path.abspath(candidate)) != resolved:
                continue
            common = Path(os.path.commonpath((str(root), str(resolved))))
            try:
                parent_ascents = len(initial_root.relative_to(common).parts)
            except ValueError:
                continue
            if parent_ascents > DEFAULT_MAX_IMPLICIT_PARENT_ASCENTS:
                raise SourceDependencyInferenceError(
                    limit_kind="parent_ascent",
                    maximum=DEFAULT_MAX_IMPLICIT_PARENT_ASCENTS,
                    observed=parent_ascents,
                    dependency=resolved,
                )
            root = common
            observed_parent_ascents = max(observed_parent_ascents, parent_ascents)
            if resolved in visited:
                continue
            if depth > max_depth:
                raise SourceDependencyInferenceError(
                    limit_kind="depth",
                    maximum=max_depth,
                    observed=depth,
                    dependency=resolved,
                )
            if followed_files >= max_files:
                raise SourceDependencyInferenceError(
                    limit_kind="files",
                    maximum=max_files,
                    observed=followed_files + 1,
                    dependency=resolved,
                )
            visited.add(resolved)
            followed_files += 1
            pending.append((resolved, resolved, depth + 1))

    if root == initial_root:
        return root, {"kind": "entrypoint_parent"}
    return root, {
        "kind": "inferred_literal_closure",
        "policy": "smallest bounded root containing existing nonsymlinked relative literal loads",
        "max_parent_ascents": DEFAULT_MAX_IMPLICIT_PARENT_ASCENTS,
        "observed_parent_ascents": observed_parent_ascents,
        "followed_file_count": followed_files,
    }


def _source_boundary(
    entrypoint: Path,
    *,
    scan_entrypoint: Path,
    analysis_cache_root: Path | None,
    max_depth: int,
    max_files: int,
) -> tuple[Path, dict[str, Any]]:
    """Prefer an explicit package marker, then the canonical repository boundary."""
    for candidate in (entrypoint.parent, *entrypoint.parent.parents):
        marker = candidate / SOURCE_ROOT_MARKER
        if marker.is_file() and not marker.is_symlink():
            digest = sha256_file(marker)
            if digest is not None:
                return candidate.resolve(), {
                    "kind": "source_root_marker",
                    "marker": SOURCE_ROOT_MARKER,
                    "policy": "nearest ancestor marker directory is the exact source package root",
                    "marker_sha256": digest,
                    "marker_size_bytes": marker.stat().st_size,
                }
    repository = nearest_repository_root(entrypoint)
    if repository is not None:
        return repository, {
            "kind": "nearest_repository_root",
            "policy": "nearest nonsymlink repository root is the source package boundary",
        }
    return _implicit_literal_source_boundary(
        entrypoint,
        scan_entrypoint=scan_entrypoint,
        analysis_cache_root=analysis_cache_root,
        max_depth=max_depth,
        max_files=max_files,
    )


def _git_directory(repository_root: Path) -> Path | None:
    marker = repository_root / ".git"
    if marker.is_symlink():
        return None
    if marker.is_dir():
        return marker.resolve()
    if not marker.is_file():
        return None
    try:
        line = marker.read_text(encoding="utf-8", errors="strict").strip()
    except OSError:
        return None
    prefix = "gitdir: "
    if not line.startswith(prefix):
        return None
    raw = Path(line.removeprefix(prefix)).expanduser()
    candidate = raw if raw.is_absolute() else repository_root / raw
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _git_common_directory(git_directory: Path) -> Path | None:
    marker = git_directory / "commondir"
    if marker.is_symlink():
        return None
    if not marker.exists():
        return git_directory.resolve()
    if not marker.is_file():
        return None
    try:
        raw = Path(marker.read_text(encoding="utf-8", errors="strict").strip()).expanduser()
        candidate = raw if raw.is_absolute() else git_directory / raw
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _registered_source_overlay_root(entrypoint: Path, holdir_root: Path | None) -> Path | None:
    """Return a linked worktree root registered in the exact HOLDIR repository."""
    if holdir_root is None:
        return None
    repository_root = nearest_repository_root(entrypoint)
    if repository_root is None or repository_root == holdir_root:
        return None
    topic_marker = repository_root / ".git"
    if topic_marker.is_symlink() or not topic_marker.is_file():
        return None
    topic_git = _git_directory(repository_root)
    holdir_git = _git_directory(holdir_root)
    if topic_git is None or holdir_git is None:
        return None
    topic_common = _git_common_directory(topic_git)
    holdir_common = _git_common_directory(holdir_git)
    if topic_common is None or topic_common != holdir_common or topic_git == topic_common:
        return None
    if not _path_within(topic_git, topic_common / "worktrees"):
        return None
    registration = topic_git / "gitdir"
    if registration.is_symlink() or not registration.is_file():
        return None
    try:
        registered_marker = Path(registration.read_text(encoding="utf-8", errors="strict").strip()).expanduser()
        if not registered_marker.is_absolute():
            registered_marker = topic_git / registered_marker
        if registered_marker.resolve() != topic_marker.resolve():
            return None
    except OSError:
        return None
    return repository_root


def _artifact_record(
    ref: dict[str, Any],
    *,
    declaring_file: Path,
    entrypoint: Path,
    source_root: Path,
    project_root: Path,
    logical_source_roots: dict[str, Path],
    logical_project_roots: dict[str, Path],
) -> dict[str, Any]:
    declared = str(ref.get("path") or "")
    raw = Path(declared).expanduser()
    logical_owner = next(
        (
            (alias, logical_root)
            for alias, logical_root in sorted(logical_source_roots.items())
            if _path_within(declaring_file, logical_root)
        ),
        None,
    )
    artifact_root = (
        logical_project_roots.get(logical_owner[0], logical_owner[1])
        if logical_owner is not None
        else project_root
    )
    record: dict[str, Any] = {
        "loader": ref.get("loader"),
        "name": ref.get("name"),
        "declared_path": declared,
        "declaring_file": _portable_path(
            declaring_file,
            entrypoint=entrypoint,
            root=source_root,
            logical_source_roots=logical_source_roots,
        ),
        "declaring_path": str(declaring_file),
        "source_line": ref.get("source_line"),
        "complete_path_argument": ref.get("complete_path_argument"),
        "project_root": str(artifact_root),
    }
    if logical_owner is not None:
        record["logical_source_root"] = logical_owner[0]
    if raw.is_absolute():
        candidate = raw
        record["resolution_base"] = "absolute_literal"
    elif declared.startswith(("./", "../")):
        candidate = declaring_file.parent / raw
        record["resolution_base"] = "declaring_file_parent"
    else:
        candidate = artifact_root / raw
        record["resolution_base"] = (
            f"logical_source_root:{logical_owner[0]}" if logical_owner is not None else "project_root"
        )
    lexical = Path(os.path.abspath(candidate))
    record["input_path"] = str(lexical)
    record["exists"] = False
    record["is_file"] = False
    record["readable"] = False
    record["symlinked"] = False
    record["lexical_parent_component"] = ".." in raw.parts
    if declared in {"", ".", ".."} or not _path_within(lexical, artifact_root):
        record["resolution"] = "invalid_path"
        return record
    try:
        resolved = lexical.resolve(strict=False)
    except OSError:
        record["resolution"] = "unreadable"
        return record
    record["resolved_path"] = str(resolved)
    if resolved != lexical:
        record.update({"resolution": "symlinked_path", "symlinked": True})
        return record
    if not _path_within(resolved, artifact_root):
        record["resolution"] = "invalid_path"
        return record
    try:
        secure = read_regular_file_beneath(artifact_root, lexical)
    except FileNotFoundError:
        record["resolution"] = "unresolved"
        return record
    except OSError:
        record["resolution"] = "unreadable"
        return record
    record["exists"] = True
    record["is_file"] = True
    digest = sha256_bytes(secure.data)
    record.update(
        {
            "resolution": "source_local",
            "package_path": (
                (Path("__logical_roots__") / logical_owner[0] / resolved.relative_to(artifact_root)).as_posix()
                if logical_owner is not None
                else resolved.relative_to(project_root).as_posix()
            ),
            "runtime_literal_path": declared,
            "sha256": digest,
            "size_bytes": secure.size,
            "readable": True,
        }
    )
    return record


def _source_load_records(
    scan: LoaderScanResult,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    edges: list[dict[str, Any]] = []
    dynamic: list[dict[str, Any]] = []
    if scan.status == "refused":
        assert scan.refusal is not None
        dynamic.append(
            {
                "loader": "<source-lexical>",
                "source_line": scan.refusal.source_line,
                "source_offset": scan.refusal.span.start,
                "reason": scan.refusal.reason,
                "refusal_kind": scan.refusal.kind,
            }
        )
        return edges, dynamic

    for item in scan.occurrences:
        if item.family != "source":
            continue
        if item.outcome == "dynamic":
            dynamic.append(
                {
                    "loader": item.loader,
                    "source_line": item.source_line,
                    "source_offset": item.loader_span.start,
                    "reason": item.reason,
                }
            )
            continue
        assert item.path is not None
        edges.append(
            {
                "loader": item.loader,
                "declared_path": item.path,
                "source_line": item.source_line,
                "source_offset": item.loader_span.start,
            }
        )
    return edges, dynamic


def _source_load_scan(source: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return _source_load_records(scan_ocaml_loaders(source.read_bytes()))


def literal_source_loads(source: Path) -> list[dict[str, Any]]:
    """Return literal source-load calls in lexical order without pretty-text interpretation."""
    return _source_load_scan(source)[0]


def _artifact_load_records(
    scan: LoaderScanResult,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    literal: list[dict[str, Any]] = []
    dynamic: list[dict[str, Any]] = []
    if scan.status == "refused":
        return literal, dynamic
    for item in scan.occurrences:
        if item.family != "artifact":
            continue
        base = {
            "loader": item.loader,
            "source_line": item.source_line,
            "source_offset": item.loader_span.start,
        }
        if item.outcome == "literal":
            literal.append(
                {
                    **base,
                    "expression": "literal",
                    "complete_path_argument": True,
                    "name": item.name,
                    "path": item.path,
                }
            )
        else:
            dynamic.append(
                {
                    **base,
                    "expression": "unknown",
                    "complete_path_argument": False,
                    "name": item.name,
                    "reason": item.reason,
                }
            )
    return literal, dynamic


def _safe_holdir_literal(declared_path: str) -> bool:
    raw = Path(declared_path).expanduser()
    return bool(
        declared_path
        and not raw.is_absolute()
        and declared_path not in {".", ".."}
        and not declared_path.startswith(("./", "../"))
        and all(part not in {"", ".", ".."} for part in raw.parts)
    )


def _resolved_path(candidate: Path | None) -> Path | None:
    if candidate is None:
        return None
    try:
        return candidate.resolve()
    except OSError:
        return None


def _validated_legacy_holdir_roots(values: tuple[Path, ...]) -> tuple[Path, ...]:
    """Canonicalize explicit historical roots without accepting traversal."""
    roots: list[Path] = []
    for value in values:
        raw = value.expanduser()
        if not raw.is_absolute() or any(part in {"", ".", ".."} for part in raw.parts):
            continue
        lexical = Path(os.path.abspath(raw))
        if lexical not in roots:
            roots.append(lexical)
    return tuple(roots)


def _legacy_holdir_coordinate(
    declared_path: str,
    holdir_root: Path | None,
    legacy_holdir_roots: tuple[Path, ...],
) -> tuple[Path, Path] | None:
    """Translate one legacy absolute HOL coordinate into the active Linux HOLDIR.

    The old source coordinate is provenance only. It must be relative to an
    exact profile-owned legacy root, never inferred from a basename, and its
    suffix must resolve inside the current Linux HOLDIR as a regular file.
    """
    if holdir_root is None:
        return None
    raw = Path(declared_path).expanduser()
    if not raw.is_absolute():
        return None
    # An absolute path already in this runtime is not a legacy coordinate and
    # remains subject to the ordinary external-absolute refusal.
    if _path_within(raw, holdir_root):
        return None
    for legacy_root in legacy_holdir_roots:
        try:
            suffix = raw.relative_to(legacy_root)
        except ValueError:
            continue
        if not suffix.parts or any(part in {"", ".", ".."} for part in suffix.parts):
            continue
        candidate = holdir_root / suffix
        resolved = _resolved_path(candidate)
        if resolved is not None and resolved.is_file() and _path_within(resolved, holdir_root):
            return candidate, resolved
    return None


def _resolve_edge(
    declaring_file: Path,
    declared_path: str,
    *,
    root: Path,
    holdir_root: Path | None,
    legacy_holdir_roots: tuple[Path, ...],
    source_overlay_root: Path | None,
    declared_logical_aliases: set[str],
    logical_source_roots: dict[str, Path],
    declaring_logical_alias: str | None,
    declaring_logical_root: Path | None,
) -> tuple[str, Path | None, bool, str | None]:
    raw = Path(declared_path).expanduser()
    literal_kind, selected_alias, logical_suffix = classify_logical_literal(
        declared_path, declared_logical_aliases
    )
    if literal_kind == "invalid_logical":
        return "invalid_logical_source_path", None, False, selected_alias
    if literal_kind == "logical":
        assert selected_alias is not None and logical_suffix is not None
        if selected_alias not in logical_source_roots:
            return "unavailable_logical_source_root", None, False, selected_alias
        logical_root = logical_source_roots[selected_alias]
        logical_candidate = logical_root / logical_suffix
        logical_resolved = _resolved_path(logical_candidate)
        if logical_resolved is None or not logical_resolved.is_file():
            return "unresolved_mounted_source", None, logical_candidate.is_symlink(), selected_alias
        logical_symlinked = Path(os.path.abspath(logical_candidate)) != logical_resolved
        if logical_symlinked or not _path_within(logical_resolved, logical_root):
            return "external_resolved", logical_resolved, True, selected_alias
        return "mounted_source", logical_resolved, False, selected_alias
    candidate = raw if raw.is_absolute() else declaring_file.parent / raw
    resolved = _resolved_path(candidate)
    if raw.is_absolute():
        legacy_holdir = _legacy_holdir_coordinate(declared_path, holdir_root, legacy_holdir_roots)
        if legacy_holdir is not None:
            holdir_candidate, holdir_resolved = legacy_holdir
            return (
                "holdir_source",
                holdir_resolved,
                Path(os.path.abspath(holdir_candidate)) != holdir_resolved,
                None,
            )
        if resolved is not None and resolved.is_file():
            return "external_resolved", resolved, Path(os.path.abspath(candidate)) != resolved, None
        return "unresolved", None, False, None
    if resolved is not None and resolved.is_file():
        normalized = Path(os.path.abspath(candidate))
        symlinked = normalized != resolved
        if declaring_logical_root is not None and _path_within(resolved, declaring_logical_root):
            return (
                ("external_resolved", resolved, True, declaring_logical_alias)
                if symlinked
                else ("mounted_source", resolved, False, declaring_logical_alias)
            )
        if _path_within(resolved, root):
            return "source_local", resolved, symlinked, None
        if source_overlay_root is not None and _path_within(resolved, source_overlay_root):
            overlay_relative = resolved.relative_to(source_overlay_root)
            holdir_candidate = holdir_root / overlay_relative if holdir_root is not None else None
            holdir_resolved = _resolved_path(holdir_candidate)
            if (
                holdir_candidate is not None
                and holdir_candidate.is_symlink()
                and (holdir_resolved is None or not holdir_resolved.is_file())
            ):
                return "external_resolved", holdir_resolved, True, None
            if holdir_resolved is not None and holdir_resolved.is_file():
                assert holdir_candidate is not None and holdir_root is not None
                holdir_symlinked = Path(os.path.abspath(holdir_candidate)) != holdir_resolved
                if not _path_within(holdir_resolved, holdir_root):
                    return "external_resolved", holdir_resolved, holdir_symlinked, None
                if (
                    sha256_file(resolved) == sha256_file(holdir_resolved)
                    and resolved.stat().st_size == holdir_resolved.stat().st_size
                ):
                    return "holdir_source", holdir_resolved, holdir_symlinked, None
                return "source_overlay_conflict", resolved, symlinked, None
            return "source_overlay", resolved, symlinked, None
        if holdir_root is not None and _path_within(resolved, holdir_root):
            holdir_relative = resolved.relative_to(holdir_root)
            overlay_candidate = source_overlay_root / holdir_relative if source_overlay_root is not None else None
            overlay_resolved = _resolved_path(overlay_candidate)
            if (
                overlay_candidate is not None
                and overlay_candidate.is_symlink()
                and (overlay_resolved is None or not overlay_resolved.is_file())
            ):
                return "external_resolved", overlay_resolved, True, None
            if overlay_resolved is not None and overlay_resolved.is_file():
                assert overlay_candidate is not None and source_overlay_root is not None
                overlay_symlinked = Path(os.path.abspath(overlay_candidate)) != overlay_resolved
                if not _path_within(overlay_resolved, source_overlay_root):
                    return "external_resolved", overlay_resolved, overlay_symlinked, None
                if (
                    sha256_file(overlay_resolved) == sha256_file(resolved)
                    and overlay_resolved.stat().st_size == resolved.stat().st_size
                ):
                    return "holdir_source", resolved, symlinked, None
                return "source_overlay_conflict", overlay_resolved, overlay_symlinked, None
            return "holdir_source", resolved, symlinked, None
        return "external_resolved", resolved, symlinked, None
    if candidate.is_symlink():
        return "external_resolved", resolved, True, declaring_logical_alias

    if (
        declaring_logical_root is not None
        and _safe_holdir_literal(declared_path)
    ):
        logical_candidate = declaring_logical_root / raw
        logical_resolved = _resolved_path(logical_candidate)
        if logical_candidate.is_symlink() and (logical_resolved is None or not logical_resolved.is_file()):
            return "external_resolved", logical_resolved, True, declaring_logical_alias
        if logical_resolved is not None and logical_resolved.is_file():
            logical_symlinked = Path(os.path.abspath(logical_candidate)) != logical_resolved
            if logical_symlinked or not _path_within(logical_resolved, declaring_logical_root):
                return "external_resolved", logical_resolved, True, declaring_logical_alias
            return "mounted_source", logical_resolved, False, declaring_logical_alias

    if _path_within(declaring_file, root) and _safe_holdir_literal(declared_path):
        package_candidate = root / raw
        package_resolved = _resolved_path(package_candidate)
        package_symlinked = (
            package_resolved is not None and Path(os.path.abspath(package_candidate)) != package_resolved
        )
        if package_candidate.is_symlink() or package_symlinked:
            return "external_resolved", package_resolved, True, None
        if package_resolved is not None and package_resolved.is_file():
            if not _path_within(package_resolved, root):
                return "external_resolved", package_resolved, True, None
            return "source_local", package_resolved, False, None
        # An existing local namespace owns its missing descendants too. An old
        # checkout in profile cwd must not fill holes in the selected repository.
        # Unowned Library/... names can still use the exact HOLDIR contract.
        namespace = root / raw.parts[0]
        if namespace.exists() or namespace.is_symlink():
            return "unresolved_source_root", package_candidate, False, None

    if holdir_root is None or not _safe_holdir_literal(declared_path):
        if declaring_logical_root is not None:
            return "unresolved_mounted_source", None, False, declaring_logical_alias
        return "unresolved", None, False, None
    overlay_resolved: Path | None = None
    overlay_symlinked = False
    if source_overlay_root is not None:
        overlay_candidate = source_overlay_root / raw
        overlay_resolved = _resolved_path(overlay_candidate)
        if overlay_candidate.is_symlink() and (overlay_resolved is None or not overlay_resolved.is_file()):
            return "external_resolved", overlay_resolved, True, None
        if overlay_resolved is not None and overlay_resolved.is_file():
            overlay_symlinked = Path(os.path.abspath(overlay_candidate)) != overlay_resolved
            if not _path_within(overlay_resolved, source_overlay_root):
                return "external_resolved", overlay_resolved, overlay_symlinked, None
        else:
            overlay_resolved = None
    holdir_candidate = holdir_root / raw
    holdir_resolved = _resolved_path(holdir_candidate)
    holdir_symlinked = False
    if holdir_candidate.is_symlink() and (holdir_resolved is None or not holdir_resolved.is_file()):
        return "external_resolved", holdir_resolved, True, None
    if holdir_resolved is not None and holdir_resolved.is_file():
        holdir_symlinked = Path(os.path.abspath(holdir_candidate)) != holdir_resolved
        if not _path_within(holdir_resolved, holdir_root):
            return "external_resolved", holdir_resolved, holdir_symlinked, None
    else:
        holdir_resolved = None
    if overlay_resolved is not None and holdir_resolved is not None:
        overlay_digest = sha256_file(overlay_resolved)
        holdir_digest = sha256_file(holdir_resolved)
        if (
            overlay_digest is not None
            and overlay_digest == holdir_digest
            and overlay_resolved.stat().st_size == holdir_resolved.stat().st_size
        ):
            return "holdir_source", holdir_resolved, holdir_symlinked, None
        return "source_overlay_conflict", overlay_resolved, overlay_symlinked, None
    if overlay_resolved is not None:
        return "source_overlay", overlay_resolved, overlay_symlinked, None
    if holdir_resolved is not None:
        return "holdir_source", holdir_resolved, holdir_symlinked, None
    if declaring_logical_root is not None:
        return "unresolved_mounted_source", None, False, declaring_logical_alias
    return "unresolved", None, False, None


def _strict_digest(
    entrypoint: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    boundary: dict[str, Any],
    project_input_context: dict[str, Any],
    cold_holdir_resolution_enabled: bool,
    source_overlay_enabled: bool,
    source_overlay_validation: str,
    logical_source_root_identity: dict[str, Any],
    dynamic_loaders: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    dynamic_artifacts: list[dict[str, Any]],
    project_input_projection_sha256: str,
) -> str:
    portable_records = [
        {
            key: record.get(key)
            for key in (
                "loader",
                "declared_path",
                "declaring_file",
                "source_line",
                "depth",
                "via",
                "resolution",
                "resolved_file",
                "package_path",
                "runtime_literal_path",
                "resolution_base",
                "logical_source_root",
                "sha256",
                "size_bytes",
                "traversal",
                "symlinked",
                "declared_theorems",
            )
        }
        for record in records
    ]
    payload = {
        "schema": DEPENDENCY_CLOSURE_SCHEMA,
        "identity_scope": DEPENDENCY_IDENTITY_SCOPE,
        "boundary": boundary,
        "project_input_context": project_input_context,
        "cold_holdir_resolution_enabled": cold_holdir_resolution_enabled,
        "source_overlay_enabled": source_overlay_enabled,
        "source_overlay_validation": source_overlay_validation,
        "logical_source_roots": logical_source_root_identity,
        "entrypoint": {
            "package_path": entrypoint["package_path"],
            "sha256": entrypoint["sha256"],
            "size_bytes": entrypoint["size_bytes"],
        },
        "records": portable_records,
        "dynamic_loaders": dynamic_loaders,
        "artifacts": [
            {
                key: artifact.get(key)
                for key in (
                    "loader",
                    "name",
                    "declared_path",
                    "declaring_file",
                    "source_line",
                    "resolution",
                    "resolution_base",
                    "package_path",
                    "sha256",
                    "size_bytes",
                    "symlinked",
                    "lexical_parent_component",
                    "complete_path_argument",
                )
            }
            for artifact in artifacts
        ],
        "dynamic_artifacts": [
            {
                key: artifact.get(key)
                for key in (
                    "loader",
                    "name",
                    "declaring_file",
                    "source_line",
                    "expression",
                    "complete_path_argument",
                    "reason",
                )
            }
            for artifact in dynamic_artifacts
        ],
        "project_input_projection_sha256": project_input_projection_sha256,
    }
    return sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _runtime_load_rule(*, logical_roots: bool, source_overlay: bool, cold_holdir: bool) -> str:
    parts = ["declaring_file_directory", "captured_source_package_root"]
    if logical_roots:
        parts.append("profile_declared_logical_source_roots")
    if source_overlay:
        parts.append("registered_source_overlay")
    parts.append("exact_cold_holdir" if cold_holdir else "profile_fallback")
    return "_then_".join(parts)


def source_dependency_closure_identity_matches(closure: dict[str, Any]) -> bool:
    """Recompute and validate the content-addressed portion of a closure document."""
    if (
        closure.get("schema") != DEPENDENCY_CLOSURE_SCHEMA
        or closure.get("identity_scope") != DEPENDENCY_IDENTITY_SCOPE
        or not isinstance(closure.get("entrypoint"), dict)
        or not isinstance(closure.get("records"), list)
        or not isinstance(closure.get("boundary"), dict)
        or not isinstance(closure.get("project_root"), str)
        or not isinstance(closure.get("project_root_boundary"), dict)
        or not isinstance(closure.get("project_input_context"), dict)
        or not isinstance(closure.get("dynamic_loaders"), list)
        or not isinstance(closure.get("artifacts"), list)
        or not isinstance(closure.get("dynamic_artifacts"), list)
        or not isinstance(closure.get("project_inputs"), dict)
        or not isinstance(closure.get("limit_reasons"), list)
        or not isinstance(closure.get("cold_holdir_resolution_enabled"), bool)
        or not isinstance(closure.get("source_overlay_enabled"), bool)
        or not isinstance(closure.get("source_overlay_validation"), str)
        or not isinstance(closure.get("logical_source_roots"), dict)
        or not logical_source_root_identity_is_valid(closure.get("logical_source_roots"))
    ):
        return False
    records = closure["records"]
    dynamic_loaders = closure["dynamic_loaders"]
    artifacts = closure["artifacts"]
    dynamic_artifacts = closure["dynamic_artifacts"]
    project_inputs = closure["project_inputs"]
    project_input_context = closure["project_input_context"]
    if (
        not project_input_context_is_consistent(project_input_context)
        or project_input_context.get("source") != (closure.get("entrypoint") or {}).get("path")
        or project_input_context.get("source_dependency_root") != closure.get("root")
        or project_input_context.get("project_root") != closure.get("project_root")
        or project_input_context.get("root_facts") != closure.get("project_root_boundary")
    ):
        return False
    if not all(isinstance(item, dict) for item in [*records, *dynamic_loaders, *artifacts, *dynamic_artifacts]):
        return False
    resolved_source_kinds = {"source_local", "source_overlay", "holdir_source", "mounted_source"}
    unresolved = sum(record.get("resolution") not in resolved_source_kinds for record in records)
    unresolved_artifacts = sum(artifact.get("resolution") != "source_local" for artifact in artifacts)
    local_files = {
        record.get("resolved_file")
        for record in records
        if record.get("resolution") == "source_local" and record.get("resolved_file") != "<entrypoint>"
    }
    holdir_files = {record.get("resolved_file") for record in records if record.get("resolution") == "holdir_source"}
    overlay_files = {record.get("resolved_file") for record in records if record.get("resolution") == "source_overlay"}
    mounted_files = {record.get("resolved_file") for record in records if record.get("resolution") == "mounted_source"}
    try:
        rebuilt_project_inputs = build_project_input_projection(closure)
    except (OSError, TypeError, ValueError):
        return False
    stored_project_inputs = {
        key: value
        for key, value in project_inputs.items()
        if key not in {"source_dependency_closure_sha256", "source_dependency_closure_schema"}
    }
    if stored_project_inputs != rebuilt_project_inputs:
        return False
    projection_digest = project_input_projection_sha256(project_inputs)
    if (
        project_inputs.get("projection_sha256") != projection_digest
        or closure.get("project_input_projection_sha256") != projection_digest
        or project_inputs.get("source_dependency_closure_sha256") != closure.get("strict_sha256")
        or project_inputs.get("source_dependency_closure_schema") != DEPENDENCY_CLOSURE_SCHEMA
    ):
        return False
    resolved_literal_closure = (
        unresolved == 0
        and unresolved_artifacts == 0
        and not closure["limit_reasons"]
        and project_inputs.get("status") != "blocked"
    )
    if (
        closure.get("runtime_load_rule")
        != _runtime_load_rule(
            logical_roots=bool(closure["logical_source_roots"].get("roots") or []),
            source_overlay=closure["source_overlay_enabled"],
            cold_holdir=closure["cold_holdir_resolution_enabled"],
        )
        or closure.get("source_overlay_validation")
        != ("same_git_common_dir_registered_worktree" if closure["source_overlay_enabled"] else "disabled")
        or closure.get("literal_edge_count") != len(records)
        or closure.get("source_local_file_count") != len(local_files)
        or closure.get("source_overlay_file_count") != len(overlay_files)
        or closure.get("mounted_source_file_count") != len(mounted_files)
        or closure.get("holdir_source_file_count") != len(holdir_files)
        or closure.get("unresolved_or_external_count") != unresolved
        or closure.get("dynamic_loader_count") != len(dynamic_loaders)
        or closure.get("literal_artifact_count") != len(artifacts)
        or closure.get("dynamic_artifact_count") != len(dynamic_artifacts)
        or closure.get("unresolved_artifact_count") != unresolved_artifacts
        or closure.get("resolved_literal_closure") is not resolved_literal_closure
        or closure.get("semantic_identity_complete")
        is not (resolved_literal_closure and not dynamic_loaders and not dynamic_artifacts)
    ):
        return False
    try:
        digest = _strict_digest(
            closure["entrypoint"],
            records,
            boundary=closure["boundary"],
            project_input_context=project_input_context_identity_payload(project_input_context),
            cold_holdir_resolution_enabled=closure["cold_holdir_resolution_enabled"],
            source_overlay_enabled=closure["source_overlay_enabled"],
            source_overlay_validation=closure["source_overlay_validation"],
            logical_source_root_identity=closure["logical_source_roots"],
            dynamic_loaders=dynamic_loaders,
            artifacts=artifacts,
            dynamic_artifacts=dynamic_artifacts,
            project_input_projection_sha256=projection_digest,
        )
    except (AttributeError, KeyError, TypeError):
        return False
    return closure.get("strict_sha256") == digest


def build_source_dependency_closure(
    source: Path,
    *,
    source_context: Path | None = None,
    holdir_root: Path | None = None,
    legacy_holdir_roots: tuple[Path, ...] = (),
    analysis_cache_root: Path | None = None,
    project_root: Path | None = None,
    logical_source_root_declarations: tuple[dict[str, str], ...] = (),
    logical_source_roots: dict[str, Path] | None = None,
    logical_project_roots: dict[str, Path] | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_files: int = DEFAULT_MAX_FILES,
) -> dict[str, Any]:
    """Build a deterministic depth-first manifest of literal source-local loads."""
    entrypoint = source.expanduser().resolve()
    entrypoint_identity = (source_context or entrypoint).expanduser().resolve()
    root, boundary = _source_boundary(
        entrypoint_identity,
        scan_entrypoint=entrypoint,
        analysis_cache_root=(analysis_cache_root.expanduser().resolve() if analysis_cache_root is not None else None),
        max_depth=max_depth,
        max_files=max_files,
    )
    cold_holdir = holdir_root.expanduser().resolve() if holdir_root is not None else None
    legacy_holdir_roots = _validated_legacy_holdir_roots(legacy_holdir_roots)
    source_overlay = _registered_source_overlay_root(entrypoint_identity, cold_holdir)
    source_overlay_enabled = source_overlay is not None
    source_overlay_validation = "same_git_common_dir_registered_worktree" if source_overlay_enabled else "disabled"
    logical_roots = {
        alias: path.expanduser().resolve()
        for alias, path in sorted((logical_source_roots or {}).items())
    }
    logical_projects = {
        alias: path.expanduser().resolve()
        for alias, path in sorted((logical_project_roots or logical_roots).items())
    }
    logical_root_identity = logical_source_root_identity(logical_source_root_declarations)
    logical_declarations = {row["alias"]: row for row in logical_source_root_declarations}
    declared_aliases = {row["alias"] for row in logical_source_root_declarations}
    if set(logical_roots) - declared_aliases:
        raise ValueError("logical source-root runtime mapping is not declared by the portable profile contract")
    if set(logical_projects) != set(logical_roots):
        raise ValueError("logical project-root mapping must match the resolved logical source roots")
    entry_sha = sha256_file(entrypoint)
    if entry_sha is None:
        raise OSError(f"source dependency closure entrypoint is unreadable: {entrypoint}")
    entry = {
        "path": str(entrypoint_identity),
        "package_path": entrypoint_identity.relative_to(root).as_posix(),
        "sha256": entry_sha,
        "size_bytes": entrypoint.stat().st_size,
    }
    records: list[dict[str, Any]] = []
    visited = {entrypoint_identity}
    active = {entrypoint_identity}
    scanned_files = 0
    limit_reasons: list[str] = []
    dynamic_loaders: list[dict[str, Any]] = []
    artifact_references: list[tuple[dict[str, Any], Path]] = []
    dynamic_artifact_records: list[dict[str, Any]] = []
    analysis_cache: dict[Path, dict[str, Any]] = {}
    persistent_cache = analysis_cache_root.expanduser().resolve() if analysis_cache_root is not None else None
    persistent_cache_hits = 0
    persistent_cache_misses = 0
    persistent_cache_writes = 0

    def analyze(source: Path, trusted_root: Path) -> dict[str, Any]:
        nonlocal persistent_cache_hits, persistent_cache_misses, persistent_cache_writes
        cached = analysis_cache.get(source)
        if cached is not None:
            return cached
        data = read_regular_file_beneath(trusted_root, source).data
        source_sha256 = sha256_bytes(data)
        if persistent_cache is not None:
            cached = _cached_source_analysis(persistent_cache, source_sha256)
            if cached is not None:
                persistent_cache_hits += 1
                analysis_cache[source] = cached
                return cached
            persistent_cache_misses += 1
        loader_scan = scan_ocaml_loaders(data)
        edges, dynamic = _source_load_records(loader_scan)
        artifact_refs, dynamic_artifact_refs = _artifact_load_records(loader_scan)
        if loader_scan.status == "ok":
            text = data.decode("utf-8", errors="strict")
            theorem_candidate = THEOREM_RE.search(text) is not None or DIRECT_THEOREM_RE.search(text) is not None
            masked = (
                mask_hol_backquote_interiors(mask_ocaml_comments_and_strings(text)) if theorem_candidate else text
            )
        else:
            masked = ""
        result = {
            "edges": edges,
            "dynamic": dynamic,
            "artifact_refs": artifact_refs,
            "dynamic_artifact_refs": dynamic_artifact_refs,
            "declared_theorems": _declared_theorem_names(masked),
        }
        if persistent_cache is not None and _write_source_analysis_cache(persistent_cache, source_sha256, result):
            persistent_cache_writes += 1
        analysis_cache[source] = result
        return result

    def visit(
        current: Path,
        *,
        resolution_source: Path,
        package_source: Path,
        depth: int,
        via: list[str],
        logical_alias: str | None,
        trusted_root: Path,
    ) -> None:
        nonlocal scanned_files
        try:
            analysis = analyze(current, trusted_root)
            edges = analysis["edges"]
            dynamic = analysis["dynamic"]
            artifact_refs = analysis["artifact_refs"]
            dynamic_artifact_refs = analysis["dynamic_artifact_refs"]
        except OSError:
            limit_reasons.append(
                f"unreadable:{_portable_path(current, entrypoint=entrypoint, root=root, holdir_root=cold_holdir, source_overlay_root=source_overlay)}"
            )
            return
        dynamic_loaders.extend(
            {
                **item,
                "declaring_file": _portable_path(
                    resolution_source,
                    entrypoint=entrypoint_identity,
                    root=root,
                    holdir_root=cold_holdir,
                    source_overlay_root=source_overlay,
                    logical_source_roots=logical_roots,
                ),
            }
            for item in dynamic
        )
        artifact_references.extend((ref, resolution_source) for ref in artifact_refs)
        dynamic_artifact_records.extend(
            {
                **ref,
                "declaring_file": _portable_path(
                    resolution_source,
                    entrypoint=entrypoint_identity,
                    root=root,
                    holdir_root=cold_holdir,
                    source_overlay_root=source_overlay,
                ),
                "declaring_path": str(resolution_source),
            }
            for ref in dynamic_artifact_refs
        )
        for edge in edges:
            resolution, resolved, symlinked, resolved_alias = _resolve_edge(
                resolution_source,
                edge["declared_path"],
                root=root,
                holdir_root=cold_holdir,
                legacy_holdir_roots=legacy_holdir_roots,
                source_overlay_root=source_overlay,
                declared_logical_aliases=declared_aliases,
                logical_source_roots=logical_roots,
                declaring_logical_alias=logical_alias,
                declaring_logical_root=logical_roots.get(logical_alias) if logical_alias is not None else None,
            )
            record = {
                "loader": edge["loader"],
                "declared_path": edge["declared_path"],
                "declaring_file": _portable_path(
                    resolution_source,
                    entrypoint=entrypoint_identity,
                    root=root,
                    holdir_root=cold_holdir,
                    source_overlay_root=source_overlay,
                    logical_source_roots=logical_roots,
                ),
                "declaring_path": str(resolution_source),
                "source_line": edge["source_line"],
                "depth": depth,
                "via": via,
                "resolution": resolution,
                "symlinked": symlinked,
            }
            if resolution in {
                "unresolved_mounted_source",
                "unavailable_logical_source_root",
                "invalid_logical_source_path",
            } and resolved_alias is not None:
                record["logical_source_root"] = resolved_alias
                if resolved_alias in logical_roots:
                    record["logical_source_root_path"] = str(logical_roots[resolved_alias])
            if resolved is not None:
                record["resolved_path"] = str(resolved)
                if resolution == "mounted_source":
                    assert resolved_alias is not None
                    alias = resolved_alias
                    logical_root = logical_roots[alias]
                    logical_relative = resolved.relative_to(logical_root)
                    record["resolved_file"] = f"{alias}/{logical_relative.as_posix()}"
                    record["logical_source_root"] = alias
                    record["logical_source_root_path"] = str(logical_root)
                    record["runtime_literal_path"] = str(edge["declared_path"])
                    record["package_path"] = (Path("__logical_roots__") / alias / logical_relative).as_posix()
                elif resolution == "holdir_source":
                    assert cold_holdir is not None
                    holdir_relative = resolved.relative_to(cold_holdir)
                    record["resolved_file"] = holdir_relative.as_posix()
                    declared = str(edge["declared_path"])
                    if Path(declared).is_absolute():
                        # Preserve the old coordinate for reporting and bind
                        # its runtime lookup to the immutable Linux package.
                        record["legacy_holdir_coordinate"] = True
                        record["runtime_literal_path"] = declared
                        record["package_path"] = (Path("__holdir__") / holdir_relative).as_posix()
                    else:
                        record["package_path"] = os.path.normpath((package_source.parent / Path(declared)).as_posix())
                elif resolution in {"source_overlay", "source_overlay_conflict"}:
                    assert source_overlay is not None
                    record["resolved_file"] = resolved.relative_to(source_overlay).as_posix()
                    if resolution == "source_overlay":
                        record["package_path"] = os.path.normpath(
                            (package_source.parent / Path(edge["declared_path"])).as_posix()
                        )
                else:
                    record["resolved_file"] = _portable_path(
                        resolved,
                        entrypoint=entrypoint_identity,
                        root=root,
                        holdir_root=cold_holdir,
                        source_overlay_root=source_overlay,
                        logical_source_roots=logical_roots,
                    )
                    if resolution == "source_local":
                        record["package_path"] = resolved.relative_to(root).as_posix()
                        declared = str(edge["declared_path"])
                        if (_safe_holdir_literal(declared)
                                and Path(os.path.abspath(resolution_source.parent / declared)) != resolved
                                and root / declared == resolved):
                            record["resolution_base"] = "source_package_root"
                record_root = (
                    logical_roots[resolved_alias]
                    if resolution == "mounted_source" and resolved_alias is not None
                    else cold_holdir
                    if resolution == "holdir_source" and cold_holdir is not None
                    else source_overlay
                    if resolution in {"source_overlay", "source_overlay_conflict"} and source_overlay is not None
                    else root
                )
                if (
                    resolution == "external_resolved"
                    and edge["loader"] == "needs"
                    and cold_holdir is not None
                    and not symlinked
                    and edge["declared_path"] == str(resolved)
                    and _path_within(resolved, cold_holdir)
                ):
                    # Capture exact absolute HOL bytes for shelf comparison.
                    # The edge remains external and untraversed: only an
                    # admitted shelf inventory can satisfy it for execution.
                    record_root = cold_holdir
                record["trusted_root_path"] = str(record_root)
                try:
                    secure = read_regular_file_beneath(record_root, resolved)
                except OSError:
                    digest = None
                else:
                    digest = sha256_bytes(secure.data)
                    record["sha256"] = digest
                    record["size_bytes"] = secure.size
                    if resolution == "mounted_source" and resolved_alias is not None:
                        managed_root = logical_roots[resolved_alias]
                        managed_relative = resolved.relative_to(managed_root)
                        validate_managed_source_file(
                            logical_declarations[resolved_alias],
                            managed_root,
                            managed_relative,
                            sha256=digest,
                            size=record["size_bytes"],
                        )
                    try:
                        record["declared_theorems"] = analyze(resolved, record_root)["declared_theorems"]
                    except OSError:
                        record["declared_theorems"] = []
            if resolution not in {"source_local", "source_overlay", "holdir_source", "mounted_source"} or resolved is None:
                record["traversal"] = "not_followed"
                records.append(record)
                continue
            digest = record.get("sha256")
            if resolved in active:
                record["traversal"] = "cycle"
            elif resolved in visited:
                record["traversal"] = "already_seen"
            elif depth > max_depth:
                raise SourceDependencyInferenceError(
                    limit_kind="depth",
                    maximum=max_depth,
                    observed=depth,
                    dependency=resolved,
                )
            elif scanned_files >= max_files:
                raise SourceDependencyInferenceError(
                    limit_kind="files",
                    maximum=max_files,
                    observed=scanned_files + 1,
                    dependency=resolved,
                )
            elif digest is None:
                record["traversal"] = "unreadable"
                limit_reasons.append(f"unreadable:{record['resolved_file']}")
            else:
                record["traversal"] = "followed"
            records.append(record)
            if record["traversal"] != "followed":
                continue
            visited.add(resolved)
            active.add(resolved)
            scanned_files += 1
            visit(
                resolved,
                resolution_source=resolved,
                package_source=Path(str(record["package_path"])),
                depth=depth + 1,
                via=[*via, edge["declared_path"]],
                logical_alias=str(record.get("logical_source_root")) if resolution == "mounted_source" else None,
                trusted_root=(
                    logical_roots[str(record["logical_source_root"])]
                    if resolution == "mounted_source"
                    else cold_holdir
                    if resolution == "holdir_source" and cold_holdir is not None
                    else source_overlay
                    if resolution == "source_overlay" and source_overlay is not None
                    else root
                ),
            )
            active.remove(resolved)

    visit(
        entrypoint,
        resolution_source=entrypoint_identity,
        package_source=Path(str(entry["package_path"])),
        depth=1,
        via=[],
        logical_alias=None,
        trusted_root=root,
    )
    project_input_context = resolve_project_input_context(
        entrypoint=entrypoint_identity,
        source_root=root,
        source_boundary=boundary,
        declared_artifact_paths=[str(ref.get("path") or "") for ref, _declaring_file in artifact_references],
        explicit_project_root=project_root,
        max_parent_ascents=DEFAULT_MAX_IMPLICIT_PARENT_ASCENTS,
    )
    project_root = Path(str(project_input_context["project_root"]))
    project_root_boundary = project_input_context["root_facts"]
    artifact_records = [
        _artifact_record(
            ref,
            declaring_file=declaring_file,
            entrypoint=entrypoint_identity,
            source_root=root,
            project_root=project_root,
            logical_source_roots=logical_roots,
            logical_project_roots=logical_projects,
        )
        for ref, declaring_file in artifact_references
    ]
    for artifact in artifact_records:
        alias = artifact.get("logical_source_root")
        if artifact.get("resolution") != "source_local" or not isinstance(alias, str):
            continue
        if logical_declarations[alias].get("source_role") != "managed_mirror":
            continue
        logical_root = logical_roots[alias]
        resolved_artifact = Path(str(artifact["resolved_path"]))
        validate_managed_source_file(
            logical_declarations[alias],
            logical_root,
            resolved_artifact.relative_to(logical_root),
            sha256=str(artifact["sha256"]),
            size=int(artifact["size_bytes"]),
        )
    resolved_source_kinds = {"source_local", "source_overlay", "holdir_source", "mounted_source"}
    unresolved = sum(record["resolution"] not in resolved_source_kinds for record in records)
    unresolved_artifacts = sum(record["resolution"] != "source_local" for record in artifact_records)
    local_files = {
        record.get("resolved_file")
        for record in records
        if record["resolution"] == "source_local" and record.get("resolved_file") != "<entrypoint>"
    }
    holdir_files = {record.get("resolved_file") for record in records if record["resolution"] == "holdir_source"}
    overlay_files = {record.get("resolved_file") for record in records if record["resolution"] == "source_overlay"}
    mounted_files = {record.get("resolved_file") for record in records if record["resolution"] == "mounted_source"}
    legacy_holdir_coordinates = sum(bool(record.get("legacy_holdir_coordinate")) for record in records)
    closure = {
        "schema": DEPENDENCY_CLOSURE_SCHEMA,
        "identity_scope": DEPENDENCY_IDENTITY_SCOPE,
        "root": str(root),
        "boundary": boundary,
        "project_root": str(project_root),
        "project_root_boundary": project_root_boundary,
        "project_input_context": project_input_context,
        "cold_holdir_resolution_enabled": cold_holdir is not None,
        "source_overlay_enabled": source_overlay_enabled,
        "source_overlay_validation": source_overlay_validation,
        "logical_source_roots": logical_root_identity,
        "runtime_load_rule": _runtime_load_rule(
            logical_roots=bool(logical_source_root_declarations),
            source_overlay=source_overlay_enabled,
            cold_holdir=cold_holdir is not None,
        ),
        "entrypoint": entry,
        "records": records,
        "literal_edge_count": len(records),
        "source_local_file_count": len(local_files),
        "source_overlay_file_count": len(overlay_files),
        "mounted_source_file_count": len(mounted_files),
        "holdir_source_file_count": len(holdir_files),
        "legacy_holdir_coordinate_count": legacy_holdir_coordinates,
        "legacy_holdir_coordinate_notice": (
            "legacy absolute HOLDIR coordinates were rebound to the current Linux HOLDIR; "
            "normalize source literals before the next developer shelf refresh"
            if legacy_holdir_coordinates
            else None
        ),
        "unresolved_or_external_count": unresolved,
        "dynamic_loader_count": len(dynamic_loaders),
        "dynamic_loaders": dynamic_loaders,
        "literal_artifact_count": len(artifact_records),
        "dynamic_artifact_count": len(dynamic_artifact_records),
        "unresolved_artifact_count": unresolved_artifacts,
        "artifacts": artifact_records,
        "dynamic_artifacts": dynamic_artifact_records,
        "limit_reasons": limit_reasons,
    }
    project_inputs = build_project_input_projection(closure)
    projection_digest = str(project_inputs["projection_sha256"])
    resolved_literal_closure = (
        unresolved == 0
        and unresolved_artifacts == 0
        and not limit_reasons
        and project_inputs.get("status") != "blocked"
    )
    strict_sha256 = _strict_digest(
        entry,
        records,
        boundary=boundary,
        project_input_context=project_input_context_identity_payload(project_input_context),
        cold_holdir_resolution_enabled=cold_holdir is not None,
        source_overlay_enabled=source_overlay_enabled,
        source_overlay_validation=source_overlay_validation,
        logical_source_root_identity=logical_root_identity,
        dynamic_loaders=dynamic_loaders,
        artifacts=artifact_records,
        dynamic_artifacts=dynamic_artifact_records,
        project_input_projection_sha256=projection_digest,
    )
    project_inputs["source_dependency_closure_sha256"] = strict_sha256
    project_inputs["source_dependency_closure_schema"] = DEPENDENCY_CLOSURE_SCHEMA
    closure.update(
        {
            "strict_sha256": strict_sha256,
            "project_input_projection_sha256": projection_digest,
            "project_inputs": project_inputs,
            "resolved_literal_closure": resolved_literal_closure,
            "semantic_identity_complete": (
                resolved_literal_closure and not dynamic_loaders and not dynamic_artifact_records
            ),
        }
    )
    if persistent_cache is not None:
        closure["analysis_cache"] = {
            "schema": SOURCE_ANALYSIS_CACHE_SCHEMA,
            "analyzer_revision": SOURCE_ANALYSIS_REVISION,
            "scope": "warm_authoring_only",
            "content_verification": "exact file bytes are SHA-256 hashed before every lookup",
            "hits": persistent_cache_hits,
            "misses": persistent_cache_misses,
            "writes": persistent_cache_writes,
        }
    return closure
