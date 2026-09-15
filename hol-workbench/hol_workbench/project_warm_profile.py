"""Validated project-owned inputs for developer-built warm profiles."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hol_workbench.hashing import sha256_bytes, sha256_file, sha256_text
from hol_workbench.jsonio import read_json_strict

PROJECT_WARM_PROFILE_SCHEMA = "hol-workbench.project-warm-profile.v1"
_PROFILE_NAME = re.compile(r"[a-z][a-z0-9-]{1,63}")
_BASIS_ID = re.compile(r"[A-Za-z0-9_.-]{3,128}")
_THEOREM_NAME = re.compile(r"[A-Z][A-Z0-9_']{2,127}")
_SOURCE_PATH = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*\.ml")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_COMMIT = re.compile(r"[0-9a-f]{40,64}")


class ProjectWarmProfileError(ValueError):
    """Raised before any profile build when a project identity is not exact."""


@dataclass(frozen=True)
class ProjectWarmProfile:
    manifest: Path
    manifest_sha256: str
    profile: str
    basis_id: str
    parent_profile: str
    project_root: Path
    base_commit: str
    repository_clean: bool
    ordered_sources: tuple[dict[str, Any], ...]
    active_source: str
    sentinel_theorem: str
    sentinel_expression: str
    identity_sha256: str

    def base_lines(self, parent_lines: list[str]) -> list[str]:
        identity = {
            "schema": PROJECT_WARM_PROFILE_SCHEMA,
            "profile": self.profile,
            "basis_id": self.basis_id,
            "parent_profile": self.parent_profile,
            "manifest_sha256": self.manifest_sha256,
            "base_commit": self.base_commit,
            "ordered_sources": list(self.ordered_sources),
            "active_source": self.active_source,
            "sentinel_theorem": self.sentinel_theorem,
            "identity_sha256": self.identity_sha256,
        }
        identity_text = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return [
            *parent_lines,
            f"(* HOL_WORKBENCH_PROJECT_PROFILE_IDENTITY {identity_text} *)",
            *(f'needs "{row["path"]}";;' for row in self.ordered_sources),
            self.sentinel_expression,
        ]


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ProjectWarmProfileError(
            f"project profile Git query failed in {root}: {' '.join(args)}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _relative_source(root: Path, raw: object) -> tuple[str, Path]:
    text = str(raw or "")
    if not _SOURCE_PATH.fullmatch(text) or Path(text).is_absolute() or ".." in Path(text).parts:
        raise ProjectWarmProfileError(f"project profile source path is unsafe: {text!r}")
    resolved = (root / text).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ProjectWarmProfileError(f"project profile source escapes project root: {text}") from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise ProjectWarmProfileError(f"project profile source is not a regular file: {resolved}")
    return text, resolved


def load_project_warm_profile(
    manifest_path: Path,
    *,
    expected_profile: str | None = None,
    require_clean: bool = False,
) -> ProjectWarmProfile:
    manifest = manifest_path.expanduser().resolve()
    data = read_json_strict(manifest)
    if data.get("schema") != PROJECT_WARM_PROFILE_SCHEMA:
        raise ProjectWarmProfileError(f"project profile manifest has unsupported schema: {data.get('schema')!r}")
    profile = str(data.get("profile") or "")
    basis_id = str(data.get("basis_id") or "")
    parent_profile = str(data.get("parent_profile") or "")
    if not _PROFILE_NAME.fullmatch(profile) or (expected_profile and profile != expected_profile):
        raise ProjectWarmProfileError(
            f"project profile name mismatch: expected {expected_profile or 'a valid name'}, observed {profile!r}"
        )
    if not _BASIS_ID.fullmatch(basis_id):
        raise ProjectWarmProfileError(f"project profile basis_id is invalid: {basis_id!r}")
    if not _PROFILE_NAME.fullmatch(parent_profile) or parent_profile == profile:
        raise ProjectWarmProfileError(f"project profile parent_profile is invalid: {parent_profile!r}")

    root_value = str(data.get("project_root") or "")
    requested_root = Path(root_value).expanduser()
    project_root = (
        requested_root.resolve() if requested_root.is_absolute() else (manifest.parent / requested_root).resolve()
    )
    if not project_root.is_dir():
        raise ProjectWarmProfileError(f"project profile root is missing: {project_root}")
    base_commit = str(data.get("base_commit") or "")
    if not _GIT_COMMIT.fullmatch(base_commit):
        raise ProjectWarmProfileError("project profile base_commit must be a full lowercase commit id")
    _git(project_root, "cat-file", "-e", f"{base_commit}^{{commit}}")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_commit, "HEAD"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestor.returncode != 0:
        raise ProjectWarmProfileError(f"project profile base_commit is not an ancestor of the checkout: {base_commit}")
    repository_clean = not bool(_git(project_root, "status", "--porcelain=v1", "--untracked-files=all"))
    if require_clean and not repository_clean:
        raise ProjectWarmProfileError(
            f"project profile build requires a clean repository containing base commit {base_commit}: {project_root}"
        )

    raw_sources = data.get("ordered_sources")
    if not isinstance(raw_sources, list) or not raw_sources or len(raw_sources) > 200:
        raise ProjectWarmProfileError("project profile ordered_sources must contain 1..200 entries")
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_sources, start=1):
        if not isinstance(item, dict):
            raise ProjectWarmProfileError(f"project profile source {index} must be an object")
        relative, resolved = _relative_source(project_root, item.get("path"))
        expected_sha = str(item.get("sha256") or "")
        if relative in seen or not _SHA256.fullmatch(expected_sha):
            raise ProjectWarmProfileError(f"project profile source {index} has duplicate path or invalid SHA-256")
        observed_sha = sha256_file(resolved)
        if observed_sha != expected_sha:
            raise ProjectWarmProfileError(
                f"project profile source changed: {relative}: manifest={expected_sha}, checkout={observed_sha}"
            )
        committed = subprocess.run(
            ["git", "show", f"{base_commit}:{relative}"],
            cwd=project_root,
            check=False,
            capture_output=True,
        )
        committed_sha = sha256_bytes(committed.stdout) if committed.returncode == 0 else None
        if committed_sha != expected_sha:
            raise ProjectWarmProfileError(
                f"project profile source is not bound to base_commit: {relative}: "
                f"manifest={expected_sha}, base_commit={committed_sha or 'missing'}"
            )
        seen.add(relative)
        ordered.append({"path": relative, "sha256": expected_sha, "size_bytes": resolved.stat().st_size})

    cutoff = data.get("cutoff")
    if not isinstance(cutoff, dict):
        raise ProjectWarmProfileError("project profile cutoff must be an object")
    last_source = str(cutoff.get("last_source") or "")
    active_source = str(cutoff.get("active_source") or "")
    if last_source != ordered[-1]["path"]:
        raise ProjectWarmProfileError("project profile cutoff.last_source must equal the final ordered source")
    _relative_source(project_root, active_source)
    if active_source in seen:
        raise ProjectWarmProfileError("project profile active_source must remain outside the warm cutoff")

    sentinel = data.get("sentinel")
    if not isinstance(sentinel, dict):
        raise ProjectWarmProfileError("project profile sentinel must be an object")
    sentinel_theorem = str(sentinel.get("theorem") or "")
    sentinel_expression = str(sentinel.get("expression") or "").strip()
    if not _THEOREM_NAME.fullmatch(sentinel_theorem):
        raise ProjectWarmProfileError("project profile sentinel theorem name is invalid")
    if (
        not sentinel_expression.endswith(";;")
        or sentinel_theorem not in sentinel_expression
        or len(sentinel_expression) > 2_000
    ):
        raise ProjectWarmProfileError(
            "project profile sentinel expression must be bounded, terminate with ;;, and name the sentinel theorem"
        )

    manifest_sha256 = sha256_file(manifest)
    if manifest_sha256 is None:
        raise ProjectWarmProfileError(f"project profile manifest is unreadable: {manifest}")
    identity_payload = {
        "schema": PROJECT_WARM_PROFILE_SCHEMA,
        "profile": profile,
        "basis_id": basis_id,
        "parent_profile": parent_profile,
        "manifest_sha256": manifest_sha256,
        "base_commit": base_commit,
        "ordered_sources": ordered,
        "active_source": active_source,
        "sentinel_theorem": sentinel_theorem,
        "sentinel_expression": sentinel_expression,
    }
    identity_sha256 = sha256_text(json.dumps(identity_payload, sort_keys=True, separators=(",", ":")))
    assert identity_sha256 is not None
    return ProjectWarmProfile(
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        profile=profile,
        basis_id=basis_id,
        parent_profile=parent_profile,
        project_root=project_root,
        base_commit=base_commit,
        repository_clean=repository_clean,
        ordered_sources=tuple(ordered),
        active_source=active_source,
        sentinel_theorem=sentinel_theorem,
        sentinel_expression=sentinel_expression,
        identity_sha256=identity_sha256,
    )
