"""Authoritative public and developer-only warm-profile registries."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from hol_workbench.jsonio import read_json_strict
from hol_workbench.logical_source_roots import (
    LogicalSourceRootError,
    logical_source_root_declarations,
)

PUBLIC_PROFILE_SCHEMA = "hol-workbench.warmup-profiles.v1"
DEVELOPER_PROFILE_SCHEMA = "hol-workbench.developer-warmup-profiles.v1"
PUBLIC_PROFILE_MANIFEST = "warmup-profiles.json"
DEVELOPER_PROFILE_MANIFEST = Path("dev") / "warmup-profiles-developer.json"
PUBLIC_PROFILE_FIELDS = (
    "summary",
    "scratch_convention",
    "orbstack_public_capacity",
    "useful_operations",
    "logical_source_roots",
)


class ProfileRegistryError(ValueError):
    """A profile registry is malformed or crosses its authority boundary."""


def _profile_records(data: dict[str, Any], *, label: str) -> dict[str, dict[str, Any]]:
    profiles_object: object = data.get("profiles")
    if not isinstance(profiles_object, dict) or not profiles_object:
        raise ProfileRegistryError(f"{label} has no profiles")
    profiles = cast(dict[object, object], profiles_object)
    if not all(isinstance(name, str) and isinstance(record, dict) for name, record in profiles.items()):
        raise ProfileRegistryError(f"{label} profiles must be named objects")
    return cast(dict[str, dict[str, Any]], profiles)


def public_profile_names(data: dict[str, Any]) -> tuple[str, ...]:
    """Return the ordered public menu from a validated public manifest."""

    profiles = _profile_records(data, label="public profile manifest")
    names_object: object = data.get("public_authoring_profiles")
    if (
        not isinstance(names_object, list)
        or not names_object
        or not all(isinstance(name, str) for name in cast(list[object], names_object))
    ):
        raise ProfileRegistryError(
            "public profile manifest must contain exactly its declared public_authoring_profiles"
        )
    names = cast(list[str], names_object)
    if len(set(names)) != len(names) or set(names) != set(profiles):
        raise ProfileRegistryError(
            "public profile manifest must contain exactly its declared public_authoring_profiles"
        )
    return tuple(names)


def public_profile_records(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Project the public profiles in their declared author-facing order."""

    profiles = _profile_records(data, label="public profile manifest")
    return {name: profiles[name] for name in public_profile_names(data)}


def public_profile_document(data: dict[str, Any]) -> dict[str, Any]:
    """Build the only proof-author JSON projection of the profile registry."""

    profiles = public_profile_records(data)
    return {
        "schema": data.get("schema"),
        "public_authoring_profiles": list(profiles),
        "profiles": {
            name: {field: profile[field] for field in PUBLIC_PROFILE_FIELDS if field in profile}
            for name, profile in profiles.items()
        },
    }


def _require_portable_project_manifests(profiles: dict[str, dict[str, Any]], *, label: str) -> None:
    for name, record in profiles.items():
        manifest = record.get("project_manifest")
        if manifest is None:
            continue
        if not isinstance(manifest, str) or not manifest.strip():
            raise ProfileRegistryError(f"{label} profile {name!r} has an invalid project_manifest")
        if Path(manifest).expanduser().is_absolute():
            raise ProfileRegistryError(f"{label} profile {name!r} project_manifest must be workbench-relative")


def _require_portable_logical_source_roots(profiles: dict[str, dict[str, Any]], *, label: str) -> None:
    for name, record in profiles.items():
        try:
            logical_source_root_declarations(record.get("logical_source_roots"), profile=name)
        except LogicalSourceRootError as exc:
            raise ProfileRegistryError(f"{label} profile {name!r} has invalid logical_source_roots: {exc}") from exc


def load_public_profile_manifest(workbench_dir: str | Path) -> dict[str, Any]:
    """Load the proof-author registry and reject any hidden profile records."""

    path = Path(workbench_dir) / PUBLIC_PROFILE_MANIFEST
    data = read_json_strict(path)
    if data.get("schema") != PUBLIC_PROFILE_SCHEMA:
        raise ProfileRegistryError(f"public profile manifest has invalid schema: {path}")
    profiles = public_profile_records(data)
    _require_portable_project_manifests(profiles, label="public profile manifest")
    _require_portable_logical_source_roots(profiles, label="public profile manifest")
    return data


def load_developer_profile_manifest(workbench_dir: str | Path) -> dict[str, Any]:
    """Load the explicit developer registry merged over the public authority."""

    root = Path(workbench_dir)
    public = load_public_profile_manifest(root)
    developer_path = root / DEVELOPER_PROFILE_MANIFEST
    developer = read_json_strict(developer_path)
    if developer.get("schema") != DEVELOPER_PROFILE_SCHEMA:
        raise ProfileRegistryError(f"developer profile manifest has invalid schema: {developer_path}")
    developer_profiles = _profile_records(developer, label="developer profile manifest")
    _require_portable_project_manifests(developer_profiles, label="developer profile manifest")
    _require_portable_logical_source_roots(developer_profiles, label="developer profile manifest")
    public_profiles = _profile_records(public, label="public profile manifest")
    overlap = sorted(set(public_profiles) & set(developer_profiles))
    if overlap:
        raise ProfileRegistryError("developer profile manifest shadows public profile(s): " + ", ".join(overlap))
    return {
        "schema": public["schema"],
        "public_authoring_profiles": list(public["public_authoring_profiles"]),
        "profiles": {**public_profiles, **developer_profiles},
    }
