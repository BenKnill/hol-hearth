"""Versioned logical namespaces resolved to exact machine-local source trees."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from hol_workbench.proofs.loader_scan import scan_ocaml_loaders
from hol_workbench.proofs.project_input_context import nearest_repository_root
from hol_workbench.secure_tree_read import open_directory_nofollow, read_regular_file_beneath

LOGICAL_SOURCE_ROOT_SCHEMA = "hol-workbench.logical-source-roots.v4"
SOURCE_GENERATION_SCHEMA = "hol-workbench.logical-source-generation.v1"
LOGICAL_SOURCE_SOURCE_ROLES = frozenset({"managed_mirror", "entrypoint_repository"})
LOGICAL_SOURCE_EXECUTION_ROLES = frozenset({"none", "profile_cwd"})
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,63}")
_REVISION = re.compile(r"[0-9a-f]{40}")
_TREE = re.compile(r"[0-9a-f]{40}")
_MAX_ROOTS = 16


class LogicalSourceRootError(ValueError):
    """A declared logical namespace cannot be resolved without guessing."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def logical_source_root_declarations(value: object, *, profile: str) -> tuple[dict[str, str], ...]:
    """Validate the portable source-layout contract stored in the profile registry."""

    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_ROOTS:
        raise LogicalSourceRootError(
            "refused_logical_source_root_declaration",
            f"profile {profile!r} logical_source_roots must be a list of at most {_MAX_ROOTS} entries",
        )
    rows: list[dict[str, str]] = []
    aliases: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise LogicalSourceRootError(
                "refused_logical_source_root_declaration",
                f"profile {profile!r} logical source root {index} must be an object",
            )
        source_role = str(raw.get("source_role") or "")
        expected = {"alias", "source_role", "execution_role"}
        if source_role == "managed_mirror":
            expected |= {"remote", "revision", "tree"}
        elif source_role == "entrypoint_repository":
            expected |= {"project_subdir", "source_subdir"}
        if set(raw) != expected:
            raise LogicalSourceRootError(
                "refused_logical_source_root_declaration",
                f"profile {profile!r} logical source root {index} must contain exactly {sorted(expected)}",
            )
        row = {key: str(raw[key]) for key in sorted(expected)}
        alias = row["alias"]
        execution_role = row["execution_role"]
        if not _ALIAS.fullmatch(alias) or alias in aliases:
            raise LogicalSourceRootError(
                "refused_logical_source_root_declaration",
                f"profile {profile!r} has unsafe or repeated logical alias {alias!r}",
            )
        if source_role not in LOGICAL_SOURCE_SOURCE_ROLES or execution_role not in LOGICAL_SOURCE_EXECUTION_ROLES:
            raise LogicalSourceRootError(
                "refused_logical_source_root_declaration",
                f"profile {profile!r} logical source root {alias!r} has unsupported roles",
            )
        if source_role == "managed_mirror" and (
            not row["remote"].startswith("https://")
            or not _REVISION.fullmatch(row["revision"])
            or not _TREE.fullmatch(row["tree"])
        ):
                raise LogicalSourceRootError(
                    "refused_logical_source_root_declaration",
                    f"profile {profile!r} managed source root {alias!r} has invalid remote/revision/tree identity",
                )
        if source_role == "entrypoint_repository":
            for field in ("source_subdir", "project_subdir"):
                subdir = row[field]
                portable = PurePosixPath(subdir)
                if (
                    subdir != "."
                    and (
                        portable.is_absolute()
                        or not portable.parts
                        or portable.as_posix() != subdir
                        or any(part in {"", ".", ".."} for part in portable.parts)
                    )
                ):
                    raise LogicalSourceRootError(
                        "refused_logical_source_root_declaration",
                        f"profile {profile!r} entrypoint source root {alias!r} has unsafe {field}",
                    )
        aliases.add(alias)
        rows.append(row)
    return tuple(rows)


def logical_source_root_identity(declarations: tuple[dict[str, str], ...]) -> dict[str, Any]:
    return {"schema": LOGICAL_SOURCE_ROOT_SCHEMA, "roots": [dict(row) for row in declarations]}


def logical_source_root_identity_is_valid(value: object) -> bool:
    if not isinstance(value, dict) or value.get("schema") != LOGICAL_SOURCE_ROOT_SCHEMA:
        return False
    try:
        rows = logical_source_root_declarations(value.get("roots"), profile="persisted")
    except LogicalSourceRootError:
        return False
    return value == logical_source_root_identity(rows)


def source_layout_cache_root(environment: Mapping[str, str] | None = None) -> Path:
    active = os.environ if environment is None else environment
    raw = active.get("XDG_CACHE_HOME") or str(Path(active.get("HOME") or Path.home()) / ".cache")
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise LogicalSourceRootError("refused_logical_source_root", f"source-layout cache root is not absolute: {root}")
    return Path(os.path.abspath(root)) / "hol-workbench"


def managed_source_paths(row: dict[str, str], environment: Mapping[str, str] | None = None) -> tuple[Path, Path, Path]:
    cache = source_layout_cache_root(environment)
    alias = row["alias"]
    revision = row["revision"]
    generation = cache / "source-generations" / alias / revision
    return cache / "source-mirrors" / f"{alias}.git", generation / "tree", generation / "manifest.json"


def _generation_payload(row: dict[str, str], tree_root: Path, document: object) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise LogicalSourceRootError("refused_logical_source_generation", "source generation is not an object")
    payload = dict(document)
    digest = payload.pop("strict_sha256", None)
    local_tree_root = payload.pop("local_tree_root", None)
    expected = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if digest != expected or payload.get("schema") != SOURCE_GENERATION_SCHEMA:
        raise LogicalSourceRootError("refused_logical_source_generation", "source generation identity is invalid")
    for key in ("alias", "remote", "revision", "tree"):
        if payload.get(key) != row.get(key):
            raise LogicalSourceRootError("refused_logical_source_generation", f"source generation {key} does not match profile declaration")
    if local_tree_root != str(tree_root) or not isinstance(payload.get("files"), dict):
        raise LogicalSourceRootError("refused_logical_source_generation", "source generation tree/files are invalid")
    return {**payload, "local_tree_root": local_tree_root, "strict_sha256": digest}


def load_managed_source_generation(
    row: dict[str, str], environment: Mapping[str, str] | None = None
) -> tuple[Path, dict[str, Any]]:
    _bare, tree_root, generation_path = managed_source_paths(row, environment)
    command = f"hol-workbench/dev/reconcile-source-layout --materialize {row['alias']}"
    try:
        if tree_root.is_symlink() or generation_path.is_symlink():
            raise OSError("symlinked managed source state")
        cache = source_layout_cache_root(environment)
        data = read_regular_file_beneath(cache, generation_path).data
        payload = _generation_payload(row, tree_root, json.loads(data))
        descriptor = open_directory_nofollow(tree_root)
        os.close(descriptor)
    except (OSError, ValueError, json.JSONDecodeError, LogicalSourceRootError) as exc:
        raise LogicalSourceRootError(
            "refused_logical_source_generation",
            f"managed logical source {row['alias']!r} is absent or stale ({exc}); run `{command}` in Linux",
        ) from exc
    return tree_root, payload


def validate_managed_source_file(
    row: dict[str, str], root: Path, relative: Path, *, sha256: str, size: int
) -> None:
    if row.get("source_role") != "managed_mirror":
        return
    tree_root, generation = load_managed_source_generation(row)
    if tree_root != root:
        raise LogicalSourceRootError("refused_logical_source_generation", "managed source root changed during capture")
    file_row = generation["files"].get(relative.as_posix())
    if not isinstance(file_row, dict) or file_row.get("sha256") != sha256 or file_row.get("size_bytes") != size:
        raise LogicalSourceRootError(
            "refused_logical_source_generation",
            f"managed source file is not bound to declared commit/tree: {row['alias']}/{relative.as_posix()}",
        )


def _entrypoint_repository_subdir(
    repository: Path,
    row: dict[str, str],
    *,
    field: str,
) -> Path:
    """Open one declared repository subdirectory without following path symlinks."""

    subdir = row[field]
    candidate = repository if subdir == "." else repository / Path(*PurePosixPath(subdir).parts)
    lexical = Path(os.path.abspath(candidate))
    descriptor: int | None = None
    try:
        descriptor = open_directory_nofollow(lexical)
        canonical = candidate.resolve(strict=True)
    except OSError as exc:
        raise LogicalSourceRootError(
            "refused_logical_source_root",
            f"logical {field} for {row['alias']!r} is not a non-symlink directory: {lexical}",
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if lexical != canonical:
        raise LogicalSourceRootError(
            "refused_logical_source_root",
            f"logical {field} for {row['alias']!r} is not a non-symlink directory: {lexical}",
        )
    return canonical


def resolve_logical_source_roots(
    source: Path,
    declarations: tuple[dict[str, str], ...],
    *,
    profile_cwd: Path | None,
) -> dict[str, Path]:
    """Resolve source roles; execution/profile roots are deliberately not source roots."""

    del profile_cwd
    entrypoint = source.expanduser().resolve()
    resolved: dict[str, Path] = {}
    for row in declarations:
        if row["source_role"] == "managed_mirror":
            physical, _generation = load_managed_source_generation(row)
        else:
            repository = nearest_repository_root(entrypoint)
            if repository is None:
                continue
            subdir = row["source_subdir"]
            candidate = repository if subdir == "." else repository / Path(*PurePosixPath(subdir).parts)
            if not candidate.exists() and not candidate.is_symlink():
                continue
            physical = _entrypoint_repository_subdir(repository, row, field="source_subdir")
        lexical = Path(os.path.abspath(physical))
        canonical = physical.resolve()
        if lexical != canonical or not canonical.is_dir():
            raise LogicalSourceRootError(
                "refused_logical_source_root",
                f"logical source root {row['alias']!r} is not a non-symlink directory: {lexical}",
            )
        if row["source_role"] == "entrypoint_repository" and not entrypoint.is_relative_to(canonical):
            continue
        resolved[row["alias"]] = canonical
    items = sorted(resolved.items())
    for index, (left_alias, left) in enumerate(items):
        for right_alias, right in items[index + 1 :]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise LogicalSourceRootError(
                    "refused_overlapping_logical_source_roots",
                    f"logical roots {left_alias!r} and {right_alias!r} overlap physically",
                )
    return resolved


def resolve_logical_project_roots(
    source: Path,
    declarations: tuple[dict[str, str], ...],
    source_roots: dict[str, Path],
) -> dict[str, Path]:
    """Resolve artifact/project roots without conflating them with source subdirectories."""

    entrypoint = source.expanduser().resolve()
    projects: dict[str, Path] = {}
    for row in declarations:
        alias = row["alias"]
        source_root = source_roots.get(alias)
        if source_root is None:
            continue
        if row["source_role"] == "managed_mirror":
            project = source_root
        else:
            repository = nearest_repository_root(entrypoint)
            if repository is None:
                continue
            project = _entrypoint_repository_subdir(repository, row, field="project_subdir")
            subdir = row["source_subdir"]
            expected_source = (
                repository if subdir == "." else repository / Path(*PurePosixPath(subdir).parts)
            )
            if Path(os.path.abspath(expected_source)) != source_root:
                raise LogicalSourceRootError(
                    "refused_logical_source_root",
                    f"logical source root {alias!r} changed while resolving its project root",
                )
        lexical = Path(os.path.abspath(project))
        if lexical != project or not project.is_dir():
            raise LogicalSourceRootError(
                "refused_logical_source_root",
                f"logical project root {alias!r} is not a non-symlink directory: {lexical}",
            )
        projects[alias] = project
    items = sorted(projects.items())
    for index, (left_alias, left) in enumerate(items):
        for right_alias, right in items[index + 1 :]:
            if left == right or left.is_relative_to(right) or right.is_relative_to(left):
                raise LogicalSourceRootError(
                    "refused_overlapping_logical_source_roots",
                    f"logical project roots {left_alias!r} and {right_alias!r} overlap physically",
                )
    return projects


def classify_logical_literal(declared: str, declared_aliases: set[str]) -> tuple[str, str | None, Path | None]:
    """Classify one literal without allowing Path normalization to change precedence."""

    raw = Path(declared).expanduser()
    if raw.is_absolute():
        return "absolute", None, None
    if declared.startswith(("./", "../")):
        return "declaring_relative", None, None
    first = declared.split("/", 1)[0]
    if first not in declared_aliases:
        return "ordinary_relative", None, None
    if "/" not in declared:
        return "invalid_logical", first, None
    suffix_text = declared.split("/", 1)[1]
    suffix = Path(suffix_text)
    canonical = f"{first}/{suffix.as_posix()}"
    if (
        not suffix.parts
        or any(part in {"", ".", ".."} for part in suffix.parts)
        or canonical != declared
        or "//" in declared
    ):
        return "invalid_logical", first, None
    return "logical", first, suffix


def requested_logical_source_paths(source: Path, declarations: tuple[dict[str, str], ...]) -> dict[str, set[Path]]:
    requested: dict[str, set[Path]] = {}
    aliases = {row["alias"] for row in declarations}
    scan = scan_ocaml_loaders(source.expanduser().resolve().read_bytes())
    if scan.status != "ok":
        return requested
    for item in scan.literal_occurrences:
        if item.family != "source" or item.path is None:
            continue
        kind, alias, suffix = classify_logical_literal(item.path, aliases)
        if kind == "logical" and alias is not None and suffix is not None:
            requested.setdefault(alias, set()).add(suffix)
    return requested


def validate_requested_logical_source_roots(
    source: Path,
    declarations: tuple[dict[str, str], ...],
    roots: dict[str, Path],
) -> None:
    """Refuse malformed, unavailable, or missing direct mappings in both public modes."""

    declared = {row["alias"]: row for row in declarations}
    aliases = set(declared)
    scan = scan_ocaml_loaders(source.expanduser().resolve().read_bytes())
    if scan.status != "ok":
        return
    for item in scan.literal_occurrences:
        if item.family != "source" or item.path is None:
            continue
        kind, alias, suffix = classify_logical_literal(item.path, aliases)
        if kind == "invalid_logical":
            raise LogicalSourceRootError(
                "refused_invalid_logical_source_path",
                f"logical source literal must use canonical ALIAS/nonempty/clean/path spelling: {item.path}",
            )
        if kind != "logical" or alias is None or suffix is None:
            continue
        if item.loader == "load":
            raise LogicalSourceRootError(
                "refused_mapped_bare_load",
                f"mapped bare load is unsupported; replace it with needs, loadt, or loads: {item.path}",
            )
        root = roots.get(alias)
        if root is None:
            raise LogicalSourceRootError(
                "refused_logical_source_root",
                f"logical source root {alias!r} is unavailable; reconcile its declared source role",
            )
        target = root / suffix
        try:
            read_regular_file_beneath(root, target)
        except OSError as exc:
            command = (
                f"hol-workbench/dev/reconcile-source-layout --materialize {alias}"
                if declared[alias]["source_role"] == "managed_mirror"
                else f"restore {alias}/{suffix.as_posix()} in the entrypoint repository"
            )
            raise LogicalSourceRootError(
                "refused_missing_logical_source_dependency",
                f"declared logical source root {alias!r} has no safe regular target {suffix.as_posix()} ({exc}); "
                f"run `{command}`, then rerun the same prove command",
            ) from exc
