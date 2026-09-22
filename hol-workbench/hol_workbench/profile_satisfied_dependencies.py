"""Fail-closed satisfaction of literal HOL dependencies by a warm shelf."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from hol_workbench.criu_snapshot_admission import (
    LiveExecutionGrant,
    StaticSnapshotAdmissionDecision,
)
from hol_workbench.criu_snapshot_compat import (
    read_verified_snapshot_manifest,
    validate_snapshot_admission_artifact,
    validate_snapshot_manifest,
    verify_static_snapshot_admission_decision,
)
from hol_workbench.fork_child_ocaml import ocaml_string_literal
from hol_workbench.hashing import sha256_file
from hol_workbench.secure_tree_read import read_regular_file_beneath

PROFILE_SATISFACTION_SCHEMA = "hol-workbench.profile-satisfied-dependencies.v2"
PROFILE_SATISFACTION_EVIDENCE = "warm_development_only"


class ProfileSatisfactionError(RuntimeError):
    """A source dependency cannot be satisfied by the selected warm shelf."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _md5_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _validated_mapping_root(value: Path, *, role: str) -> Path:
    raw = value.expanduser()
    root = _lexical_absolute(raw)
    if not raw.is_absolute() or not root.is_dir():
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_mapping_invalid",
            f"{role} HOLDIR is missing or not an absolute directory: {root}",
        )
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_mapping_invalid",
            f"cannot resolve {role} HOLDIR {root}: {exc}",
        ) from exc
    if resolved != root:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_mapping_alias",
            f"{role} HOLDIR contains a filesystem alias: {root} -> {resolved}",
        )
    return root


def _candidate_relative_path(record: dict[str, Any], *, host_holdir: Path) -> Path | None:
    if record.get("loader") != "needs":
        return None
    declared = str(record.get("declared_path") or "")
    raw = Path(declared).expanduser()
    if not raw.is_absolute():
        return None
    lexical = _lexical_absolute(raw)
    try:
        relative = lexical.relative_to(host_holdir)
    except ValueError:
        return None
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_path_invalid",
            f"literal needs path has an unsafe HOL-relative identity: {declared}",
        )
    return relative


def _candidate_profile_relative_path(record: dict[str, Any]) -> Path | None:
    if record.get("loader") != "needs":
        return None
    declared = str(record.get("declared_path") or "")
    relative = Path(declared)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.as_posix() != declared
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return None
    return relative


def _candidate_logical_profile_relative_path(
    record: dict[str, Any],
    closure: dict[str, Any],
) -> tuple[Path, str] | None:
    """Map a packaged logical root back to its declared profile-cwd role."""

    if record.get("loader") != "needs" or record.get("resolution") != "mounted_source":
        return None
    alias = str(record.get("logical_source_root") or "")
    declarations = (closure.get("logical_source_roots") or {}).get("roots") or []
    if not any(
        isinstance(row, dict) and row.get("alias") == alias and row.get("execution_role") == "profile_cwd"
        for row in declarations
    ):
        return None
    resolved_file = Path(str(record.get("resolved_file") or ""))
    if not resolved_file.parts or resolved_file.parts[0] != alias:
        return None
    relative = Path(*resolved_file.parts[1:])
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return None
    return relative, alias


def _matched_loaded_entry(
    entries: list[dict[str, Any]],
    *,
    relative: Path,
    host_path: Path,
) -> dict[str, Any]:
    relative_text = relative.as_posix()
    matches = [
        entry
        for entry in entries
        if entry.get("path_kind") == "holdir_relative"
        and entry.get("path") == relative_text
        and entry.get("basename") == host_path.name
    ]
    if not matches:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_uninventoried",
            f"selected shelf loaded closure does not inventory exact HOL path: {relative_text}",
        )
    if len(matches) != 1:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_ambiguous",
            f"selected shelf loaded closure has multiple entries for exact HOL path: {relative_text}",
        )
    return matches[0]


def _matched_profile_cwd_entry(
    entries: list[dict[str, Any]],
    *,
    relative: Path,
    shelf_profile_cwd: Path,
) -> dict[str, Any] | None:
    shelf_path = shelf_profile_cwd / relative
    matches = [
        entry
        for entry in entries
        if entry.get("path_kind") == "absolute"
        and entry.get("path") == str(shelf_path)
        and entry.get("resolved_path") == str(shelf_path)
        and entry.get("basename") == relative.name
        and "profile_cwd" in (entry.get("roles") or [])
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_ambiguous",
            f"selected shelf loaded closure has multiple profile-cwd entries for exact path: {relative.as_posix()}",
        )
    return matches[0]


def _required_digest(value: object, *, length: int, field: str) -> str:
    digest = str(value or "")
    if not re.fullmatch(rf"[0-9a-f]{{{length}}}", digest):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_shelf_invalid",
            f"selected shelf has no valid {field} identity",
        )
    return digest


def _validated_edge(
    record: dict[str, Any],
    *,
    record_index: int,
    relative: Path,
    host_holdir: Path,
    shelf_holdir: Path,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    declared = str(record.get("declared_path") or "")
    host_path = host_holdir / relative
    declared_path = Path(declared).expanduser()
    if declared_path != _lexical_absolute(declared_path) or declared_path != host_path:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_mapping_alias",
            f"literal needs spelling does not map exactly under host HOLDIR: {declared}",
        )
    if record.get("symlinked") is not False or host_path.is_symlink() or not host_path.is_file():
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_dependency_missing",
            f"mapped host dependency is missing, aliased, or not a regular file: {host_path}",
        )
    try:
        resolved = host_path.resolve(strict=True)
    except OSError as exc:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_dependency_missing",
            f"cannot resolve mapped host dependency {host_path}: {exc}",
        ) from exc
    if resolved != host_path or record.get("resolved_path") != str(host_path):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_mapping_alias",
            f"mapped host dependency does not preserve exact lexical identity: {declared}",
        )

    entry = _matched_loaded_entry(entries, relative=relative, host_path=host_path)
    loader_md5 = str(entry.get("loader_md5") or "")
    entry_sha256 = str(entry.get("sha256") or "")
    entry_size = entry.get("size_bytes")
    if not re.fullmatch(r"[0-9a-f]{32}", loader_md5) or not re.fullmatch(r"[0-9a-f]{64}", entry_sha256):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_inventory_invalid",
            f"selected shelf loaded entry has invalid digests: {relative.as_posix()}",
        )
    if not isinstance(entry_size, int) or isinstance(entry_size, bool) or entry_size < 0:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_inventory_invalid",
            f"selected shelf loaded entry has invalid size: {relative.as_posix()}",
        )
    try:
        current_size = host_path.stat().st_size
        current_md5 = _md5_file(host_path)
        current_sha256 = sha256_file(host_path)
    except OSError as exc:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_dependency_changed",
            f"cannot revalidate mapped host dependency {host_path}: {exc}",
        ) from exc
    if (
        current_md5 != loader_md5
        or current_sha256 != entry_sha256
        or current_size != entry_size
        or record.get("sha256") != entry_sha256
        or record.get("size_bytes") != entry_size
    ):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_dependency_changed",
            f"mapped host dependency no longer matches the closure and shelf inventory: {relative.as_posix()}",
        )
    return {
        "record_index": record_index,
        "loader": "needs",
        "declared_path": declared,
        "declaring_file": record.get("declaring_file"),
        "source_line": record.get("source_line"),
        "resolution": "profile_satisfied",
        "hol_relative_path": relative.as_posix(),
        "host_path": str(host_path),
        "shelf_path": str(shelf_holdir / relative),
        "basename": host_path.name,
        "loader_md5": loader_md5,
        "sha256": entry_sha256,
        "size_bytes": entry_size,
        "transported_bytes": 0,
    }


def _validated_profile_cwd_edge(
    record: dict[str, Any],
    *,
    record_index: int,
    relative: Path,
    host_profile_cwd: Path,
    shelf_profile_cwd: Path,
    entries: list[dict[str, Any]],
    logical_alias: str | None = None,
) -> dict[str, Any] | None:
    declared = str(record.get("declared_path") or "")
    if logical_alias is None and declared != relative.as_posix():
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_mapping_alias",
            f"literal needs spelling does not map exactly under the profile cwd: {declared}",
        )
    host_path = (
        Path(str(record.get("resolved_path") or ""))
        if logical_alias is not None
        else host_profile_cwd / relative
    )
    if host_path.is_symlink() or not host_path.is_file():
        return None
    try:
        resolved = host_path.resolve(strict=True)
    except OSError as exc:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_dependency_missing",
            f"cannot resolve mapped profile dependency {host_path}: {exc}",
        ) from exc
    if resolved != host_path:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_mapping_alias",
            f"mapped profile dependency does not preserve exact lexical identity: {declared}",
        )
    try:
        current_size = host_path.stat().st_size
        current_md5 = _md5_file(host_path)
        current_sha256 = sha256_file(host_path)
    except OSError as exc:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_dependency_changed",
            f"cannot revalidate mapped profile dependency {host_path}: {exc}",
        ) from exc
    common = {
        "record_index": record_index,
        "loader": "needs",
        "declared_path": declared,
        "declaring_file": record.get("declaring_file"),
        "source_line": record.get("source_line"),
        "mapping_root": f"logical_source_root:{logical_alias}" if logical_alias is not None else "profile_cwd",
        "hol_relative_path": relative.as_posix(),
        "host_path": str(host_path),
        "shelf_path": str(shelf_profile_cwd / relative),
        "basename": relative.name,
        "loader_md5": current_md5,
        "sha256": current_sha256,
        "size_bytes": current_size,
        "transported_bytes": 0,
    }
    entry = _matched_profile_cwd_entry(
        entries,
        relative=relative,
        shelf_profile_cwd=shelf_profile_cwd,
    )
    if entry is None:
        if logical_alias is not None:
            return None
        return {**common, "resolution": "profile_runtime_fallback"}
    loader_md5 = str(entry.get("loader_md5") or "")
    entry_sha256 = str(entry.get("sha256") or "")
    entry_size = entry.get("size_bytes")
    if not re.fullmatch(r"[0-9a-f]{32}", loader_md5) or not re.fullmatch(r"[0-9a-f]{64}", entry_sha256):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_inventory_invalid",
            f"selected shelf loaded entry has invalid digests: {relative.as_posix()}",
        )
    if not isinstance(entry_size, int) or isinstance(entry_size, bool) or entry_size < 0:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_inventory_invalid",
            f"selected shelf loaded entry has invalid size: {relative.as_posix()}",
        )
    if (
        current_md5 != loader_md5
        or current_sha256 != entry_sha256
        or current_size != entry_size
        or (logical_alias is not None and record.get("resolved_path") != str(host_path))
        or (logical_alias is not None and record.get("sha256") != entry_sha256)
        or (logical_alias is not None and record.get("size_bytes") != entry_size)
    ):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_dependency_changed",
            f"mapped profile dependency no longer matches the shelf inventory: {relative.as_posix()}",
        )
    return {
        **common,
        "resolution": "profile_satisfied",
        "loader_md5": loader_md5,
        "sha256": entry_sha256,
        "size_bytes": entry_size,
    }


def _captured_warm_sources(
    closure: dict[str, Any], *, entries: list[dict[str, Any]],
    host_holdir: Path | None, shelf_holdir: Path,
    shelf_profile_cwd: Path | None,
    satisfied_external_indexes: set[int],
) -> list[dict[str, Any]]:
    """Attest captured subtrees that HOL's basename/digest needs cache skips.

    Relocation is by an exact project- or HOL-relative inventory coordinate.
    Neither a matching basename nor a matching parent's bytes can establish the
    identity of that parent's imported children.
    """
    from hol_workbench.source_dependency_closure import _portable_path

    source_kinds = {"source_local", "source_overlay", "holdir_source", "mounted_source"}
    records = closure.get("records") or []
    project_root = Path(str(closure.get("project_root") or ""))
    candidates: dict[int, dict[str, Any] | None] = {}
    root_indexes: set[int] = set()

    def source_key(record: dict[str, Any]) -> str:
        # declaring_path is reporting-only and deliberately outside the closure
        # digest. Link the graph using the portable coordinates that are bound.
        resolution = record.get("resolution")
        relative = Path(str(record.get("resolved_file") or ""))
        source_root = Path(str(closure.get("root") or ""))
        entrypoint = Path(str((closure.get("entrypoint") or {}).get("path") or ""))
        trusted_root = Path(str(record.get("trusted_root_path") or ""))
        overlay_root = None
        logical_roots = None
        if resolution == "source_local":
            expected = entrypoint if str(relative) == "<entrypoint>" else source_root / relative
        elif resolution == "holdir_source" and host_holdir is not None:
            expected = host_holdir / relative
        elif resolution == "source_overlay":
            overlay_root = trusted_root
            expected = trusted_root / relative
        elif resolution == "mounted_source":
            alias = str(record.get("logical_source_root") or "")
            if not relative.parts or relative.parts[0] != alias:
                raise ProfileSatisfactionError("refused_profile_satisfaction_mapping_alias",
                    "captured logical source does not preserve its bound coordinate")
            expected = trusted_root.joinpath(*relative.parts[1:])
            logical_roots = {alias: trusted_root}
        else:
            raise ProfileSatisfactionError("refused_profile_satisfaction_mapping_alias",
                "captured warm source has no bound portable coordinate")
        if (relative.is_absolute() or ".." in relative.parts or not expected.is_absolute()
                or str(expected) != record.get("resolved_path")):
            raise ProfileSatisfactionError("refused_profile_satisfaction_mapping_alias",
                "captured warm source does not preserve its bound portable coordinate")
        return _portable_path(expected, entrypoint=entrypoint, root=source_root,
            holdir_root=host_holdir, source_overlay_root=overlay_root, logical_source_roots=logical_roots)

    def matched_inventory(record: dict[str, Any]) -> tuple[dict[str, Any], Path] | None:
        source = Path(str(record.get("resolved_path") or ""))
        if not source.is_absolute():
            return None
        relative = None
        mapping = None
        if host_holdir is not None:
            try:
                relative = source.relative_to(host_holdir)
                mapping = "holdir"
            except ValueError:
                pass
        if relative is None and record.get("resolution") == "source_overlay" and closure.get("source_overlay_enabled"):
            relative = Path(str(record.get("resolved_file") or ""))
            mapping = "holdir"
        if relative is None and record.get("resolution") == "mounted_source":
            logical = _candidate_logical_profile_relative_path({**record, "loader": "needs"}, closure)
            if logical is not None:
                relative, _alias = logical
                mapping = "profile_cwd"
        if relative is None and record.get("resolution") == "source_local" and project_root.is_absolute():
            try:
                relative = source.relative_to(project_root)
                mapping = "profile_cwd"
            except ValueError:
                pass
        if relative is None or relative.is_absolute() or not relative.parts or ".." in relative.parts:
            return None
        if mapping == "holdir":
            shelf_path = shelf_holdir / relative
            matches = [item for item in entries if item.get("path_kind") == "holdir_relative"
                       and item.get("path") == relative.as_posix() and item.get("basename") == relative.name]
        elif shelf_profile_cwd is not None:
            shelf_path = shelf_profile_cwd / relative
            entry = _matched_profile_cwd_entry(entries, relative=relative, shelf_profile_cwd=shelf_profile_cwd)
            matches = [entry] if entry is not None else []
        else:
            return None
        if len(matches) > 1:
            raise ProfileSatisfactionError("refused_profile_satisfaction_ambiguous",
                f"multiple loaded inputs match captured source coordinate: {relative}")
        return (matches[0], shelf_path) if matches else None

    def validate(index: int, *, required: bool) -> dict[str, Any] | None:
        if index in candidates:
            candidate = candidates[index]
        else:
            record = records[index]
            match = matched_inventory(record)
            source = Path(str(record.get("resolved_path") or ""))
            try:
                if record.get("symlinked") is not False:
                    raise OSError("aliased captured source")
                root = Path(str(record.get("trusted_root_path") or ""))
                if not source.is_absolute() or not root.is_absolute():
                    raise OSError("captured source has no trusted root")
                data = read_regular_file_beneath(root, source).data
            except OSError as exc:
                raise ProfileSatisfactionError("refused_profile_satisfaction_dependency_missing",
                    f"cannot revalidate captured warm dependency: {source}: {exc}") from exc
            current_sha = hashlib.sha256(data).hexdigest()
            current_md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
            if current_sha != record.get("sha256") or len(data) != record.get("size_bytes"):
                raise ProfileSatisfactionError("refused_profile_satisfaction_dependency_changed",
                    f"captured source changed before warm dependency comparison: {source}")
            if match is None:
                if record.get("loader") == "needs" and any(
                    item.get("basename") == source.name and item.get("loader_md5") == current_md5
                    for item in entries
                ):
                    raise ProfileSatisfactionError("refused_profile_satisfaction_mapping_alias",
                        f"needs would skip a loaded basename/digest without an exact source coordinate: {source}")
                candidate = None
            else:
                entry, shelf_path = match
                if (entry.get("sha256") != current_sha or entry.get("loader_md5") != current_md5
                        or type(entry.get("size_bytes")) is not int or entry["size_bytes"] != len(data)):
                    raise ProfileSatisfactionError("refused_profile_satisfaction_dependency_changed",
                        f"captured source differs from the admitted warm inventory: {source}")
                candidate = {
                    "record_index": index, "loader": record.get("loader"),
                    "host_path": str(source), "shelf_path": str(shelf_path),
                    "sha256": current_sha, "loader_md5": current_md5, "size_bytes": len(data),
                    "authority": "exact_loaded_source_inventory",
                }
            candidates[index] = candidate
        if required and candidate is None:
            raise ProfileSatisfactionError("refused_profile_satisfaction_uninventoried",
                "a captured descendant of a warm-loaded needs has no exact shelf source inventory: "
                + str(records[index].get("declared_path")))
        return candidate

    for index, record in enumerate(records):
        if (isinstance(record, dict) and record.get("resolution") in source_kinds
                and record.get("loader") == "needs"):
            if validate(index, required=False) is not None:
                root_indexes.add(index)
    selected = set(root_indexes)
    pending = [source_key(records[index]) for index in root_indexes]
    visited: set[str] = set()
    while pending:
        source = pending.pop()
        if source in visited:
            continue
        visited.add(source)
        for artifact in closure.get("artifacts") or []:
            if artifact.get("declaring_file") == source:
                raise ProfileSatisfactionError("refused_profile_satisfaction_uninventoried_artifact",
                    "a warm-loaded source would hide an ELF input without shelf object attestation: "
                    + str(artifact.get("declared_path")))
        for index, record in enumerate(records):
            if not isinstance(record, dict) or record.get("declaring_file") != source:
                continue
            if record.get("resolution") not in source_kinds:
                if index in satisfied_external_indexes:
                    # Existing absolute-HOLDIR edges were already validated
                    # against the same inventory; closure capture deliberately
                    # does not descend into these shelf-satisfied inputs.
                    continue
                raise ProfileSatisfactionError("refused_profile_satisfaction_uninventoried",
                    "a captured warm-loaded source has an unresolved or external descendant: "
                    + str(record.get("declared_path")))
            validate(index, required=True)
            selected.add(index)
            pending.append(source_key(record))
    return [{**candidates[index], "subtree_root": index in root_indexes} for index in sorted(selected)]


def build_profile_satisfaction(
    closure: dict[str, Any],
    *,
    profile_root: Path,
    logical_profile: str,
    host_holdir: Path | None = None,
    host_profile_cwd: Path | None = None,
    snapshot_admission_artifact: Path | None = None,
    snapshot_admission_artifact_sha256: str | None = None,
    snapshot_admission_decision_sha256: str | None = None,
    snapshot_admission_request_sha256: str | None = None,
    snapshot_admission_decision: StaticSnapshotAdmissionDecision | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind eligible external literal ``needs`` edges to one validated shelf."""

    host_root = _validated_mapping_root(host_holdir, role="host") if host_holdir is not None else None
    manifest: dict[str, Any] | None = None
    try:
        resolved_profile_root = profile_root.expanduser().resolve()
        if snapshot_admission_artifact is not None and snapshot_admission_decision is not None:
            raise RuntimeError("profile satisfaction received both an admission artifact and a static decision")
        if snapshot_admission_artifact is None and snapshot_admission_decision is None:
            # Dependency satisfaction is part of controller-independent warm
            # authoring. The selected shelf still receives full static runtime,
            # profile, provenance, and loaded-closure validation; deleted or
            # changed controller files do not invalidate its HOL basis.
            manifest = validate_snapshot_manifest(
                resolved_profile_root,
                include_controller_attempt=False,
            )
            admission = manifest["static_admission_decision"]
            live_grant = None
            admission_artifact_path = None
            admission_artifact_sha256 = None
        elif snapshot_admission_decision is not None:
            parsed = verify_static_snapshot_admission_decision(
                resolved_profile_root,
                snapshot_admission_decision,
                include_controller_attempt=False,
            )
            admission = parsed.record()
            live_grant = None
            admission_artifact_path = None
            admission_artifact_sha256 = None
        else:
            assert snapshot_admission_artifact is not None
            expected_digests = {
                "artifact": snapshot_admission_artifact_sha256,
                "decision": snapshot_admission_decision_sha256,
                "request": snapshot_admission_request_sha256,
            }
            if any(not isinstance(value, str) or len(value) != 64 for value in expected_digests.values()):
                raise RuntimeError("profile satisfaction requires routed artifact, decision, and request digests")
            admission_artifact_path = snapshot_admission_artifact.expanduser().resolve()
            artifact = validate_snapshot_admission_artifact(
                admission_artifact_path,
                profile_root=resolved_profile_root,
                expected_artifact_sha256=snapshot_admission_artifact_sha256,
                expected_decision_sha256=snapshot_admission_decision_sha256,
                expected_request_sha256=snapshot_admission_request_sha256,
            )
            admission = artifact["static_decision"]
            live_grant = artifact["live_execution_grant"]
            admission_artifact_sha256 = artifact["artifact_sha256"]
        parsed_admission = StaticSnapshotAdmissionDecision.from_record(admission)
        if snapshot_admission_artifact is not None or snapshot_admission_decision is not None:
            manifest = read_verified_snapshot_manifest(
                resolved_profile_root,
                expected_sha256=parsed_admission.manifest_sha256,
            )
        if manifest is None:
            raise RuntimeError("profile satisfaction could not resolve the admitted shelf manifest")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_shelf_invalid",
            f"selected shelf manifest is not valid: {exc}",
        ) from exc
    provenance = manifest.get("snapshot_provenance")
    if not isinstance(provenance, dict):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_shelf_invalid",
            "selected shelf manifest has no snapshot provenance",
        )
    shelf_root = _validated_mapping_root(Path(str(provenance.get("holdir") or "")), role="shelf")
    host_project_root = (
        _validated_mapping_root(host_profile_cwd, role="host profile cwd") if host_profile_cwd is not None else None
    )
    shelf_project_root = None
    if host_project_root is not None:
        shelf_project_root = _validated_mapping_root(
            Path(str(manifest.get("profile_cwd") or "")),
            role="shelf profile cwd",
        )
        if shelf_project_root != host_project_root:
            raise ProfileSatisfactionError(
                "refused_profile_satisfaction_mapping_invalid",
                f"host profile cwd {host_project_root} does not match shelf profile cwd {shelf_project_root}",
            )
    loaded = provenance.get("loaded_closure")
    if not isinstance(loaded, dict):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_shelf_invalid",
            "selected shelf manifest has no loaded-closure identity",
        )
    loaded_closure_sha256 = _required_digest(
        loaded.get("strict_sha256"),
        length=64,
        field="loaded-closure digest",
    )
    physical_profile = str(manifest.get("profile") or "")
    profile_basis_id = str(manifest.get("profile_basis_id") or "")
    if not logical_profile or not physical_profile or not profile_basis_id:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_shelf_invalid",
            "selected shelf has incomplete logical, physical, or basis identity",
        )
    profile_sha256 = _required_digest(manifest.get("profile_sha256"), length=64, field="profile digest")
    environment_sha256 = _required_digest(
        manifest.get("snapshot_environment_sha256"),
        length=64,
        field="snapshot-environment digest",
    )
    raw_entries = loaded.get("entries")
    if not isinstance(raw_entries, list) or not all(isinstance(entry, dict) for entry in raw_entries):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_shelf_invalid",
            "selected shelf loaded closure has invalid entries",
        )
    records = closure.get("records")
    if not isinstance(records, list):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_closure_invalid",
            "source dependency closure has invalid records",
        )
    edges = []
    for index, raw_record in enumerate(records):
        if not isinstance(raw_record, dict) or raw_record.get("resolution") not in {
            "external_resolved",
            "mounted_source",
            "unresolved",
        }:
            continue
        if host_root is not None:
            relative = _candidate_relative_path(raw_record, host_holdir=host_root)
            if relative is not None:
                edges.append(
                    _validated_edge(
                        raw_record,
                        record_index=index,
                        relative=relative,
                        host_holdir=host_root,
                        shelf_holdir=shelf_root,
                        entries=raw_entries,
                    )
                )
                continue
        if host_project_root is not None and shelf_project_root is not None:
            logical_candidate = _candidate_logical_profile_relative_path(raw_record, closure)
            if logical_candidate is not None:
                relative, alias = logical_candidate
                edge = _validated_profile_cwd_edge(
                    raw_record,
                    record_index=index,
                    relative=relative,
                    host_profile_cwd=host_project_root,
                    shelf_profile_cwd=shelf_project_root,
                    entries=raw_entries,
                    logical_alias=alias,
                )
                if edge is not None:
                    edges.append(edge)
                continue
            relative = _candidate_profile_relative_path(raw_record)
            if relative is not None:
                edge = _validated_profile_cwd_edge(
                    raw_record,
                    record_index=index,
                    relative=relative,
                    host_profile_cwd=host_project_root,
                    shelf_profile_cwd=shelf_project_root,
                    entries=raw_entries,
                )
                if edge is not None:
                    edges.append(edge)
    captured_warm_sources = _captured_warm_sources(
        closure, entries=raw_entries, host_holdir=host_root, shelf_holdir=shelf_root,
        shelf_profile_cwd=shelf_project_root,
        satisfied_external_indexes={edge["record_index"] for edge in edges},
    )
    document = {
        "schema": PROFILE_SATISFACTION_SCHEMA,
        "evidence": PROFILE_SATISFACTION_EVIDENCE,
        "source_dependency_closure_sha256": closure.get("strict_sha256"),
        "logical_profile": logical_profile,
        "physical_profile": physical_profile,
        "profile_root": str(profile_root.expanduser().resolve()),
        "profile_basis_id": profile_basis_id,
        "profile_sha256": profile_sha256,
        "snapshot_environment_sha256": environment_sha256,
        "snapshot_runtime_admission_mode": parsed_admission.legacy_manifest_mode(),
        "snapshot_admission": parsed_admission.record(),
        "live_execution_grant": live_grant,
        "snapshot_admission_artifact": str(admission_artifact_path) if admission_artifact_path is not None else None,
        "snapshot_admission_artifact_sha256": admission_artifact_sha256,
        "host_holdir": str(host_root) if host_root is not None else None,
        "shelf_holdir": str(shelf_root),
        "host_profile_cwd": str(host_project_root) if host_project_root is not None else None,
        "shelf_profile_cwd": str(shelf_project_root) if shelf_project_root is not None else None,
        "loaded_closure_sha256": loaded_closure_sha256,
        "edges": edges,
        "captured_warm_sources": captured_warm_sources,
        "transported_bytes": 0,
    }
    document["strict_sha256"] = _canonical_digest(document)
    return document


def profile_satisfaction_identity_matches(document: object, closure: dict[str, Any]) -> bool:
    if not isinstance(document, dict):
        return False
    try:
        admission = StaticSnapshotAdmissionDecision.from_record(document.get("snapshot_admission"))
        raw_grant = document.get("live_execution_grant")
        grant = LiveExecutionGrant.from_record(raw_grant) if raw_grant is not None else None
    except RuntimeError:
        return False
    if (
        document.get("snapshot_runtime_admission_mode") != admission.legacy_manifest_mode()
        or (grant is not None and grant.static_decision_sha256 != admission.decision_sha256)
        or (grant is None) != (document.get("snapshot_admission_artifact") is None)
        or (
            grant is not None
            and not re.fullmatch(
                r"[0-9a-f]{64}",
                str(document.get("snapshot_admission_artifact_sha256") or ""),
            )
        )
    ):
        return False
    digest_fields = ("profile_sha256", "snapshot_environment_sha256", "loaded_closure_sha256")
    if (
        document.get("schema") != PROFILE_SATISFACTION_SCHEMA
        or document.get("evidence") != PROFILE_SATISFACTION_EVIDENCE
        or document.get("source_dependency_closure_sha256") != closure.get("strict_sha256")
        or document.get("transported_bytes") != 0
        or not isinstance(document.get("edges"), list)
        or not all(isinstance(edge, dict) for edge in document["edges"])
        or not isinstance(document.get("captured_warm_sources", []), list)
        or not all(re.fullmatch(r"[0-9a-f]{64}", str(document.get(field) or "")) for field in digest_fields)
        or not all(
            str(document.get(field) or "") for field in ("logical_profile", "physical_profile", "profile_basis_id")
        )
    ):
        return False
    seen: set[int] = set()
    records = closure.get("records") or []
    for row in document.get("captured_warm_sources", []):
        if not isinstance(row, dict):
            return False
        index = row.get("record_index")
        if type(index) is not int or not 0 <= index < len(records) or index in seen:
            return False
        seen.add(index)
        record = records[index]
        if (row.get("authority") != "exact_loaded_source_inventory" or type(row.get("subtree_root")) is not bool
                or record.get("symlinked") is not False or row.get("host_path") != record.get("resolved_path")
                or any(row.get(key) != record.get(key) for key in ("loader", "sha256", "size_bytes"))
                or not re.fullmatch(r"[0-9a-f]{32}", str(row.get("loader_md5") or ""))):
            return False
    payload = {key: value for key, value in document.items() if key != "strict_sha256"}
    return document.get("strict_sha256") == _canonical_digest(payload)


def _profile_record_index_sets(document: object, closure: dict[str, Any]) -> tuple[set[int], set[int]]:
    if not profile_satisfaction_identity_matches(document, closure):
        return set(), set()
    assert isinstance(document, dict)
    records = closure.get("records") or []
    host_holdir = Path(str(document.get("host_holdir") or ""))
    shelf_holdir = Path(str(document.get("shelf_holdir") or ""))
    host_profile_cwd = Path(str(document.get("host_profile_cwd") or ""))
    shelf_profile_cwd = Path(str(document.get("shelf_profile_cwd") or ""))
    satisfied: set[int] = set()
    runtime: set[int] = set()
    for edge in document.get("edges") or []:
        index = edge.get("record_index")
        resolution = edge.get("resolution")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 0
            or index >= len(records)
            or index in satisfied
            or index in runtime
        ):
            return set(), set()
        record = records[index]
        if not isinstance(record, dict) or any(
            edge.get(key) != record.get(key) for key in ("loader", "declared_path", "declaring_file", "source_line")
        ):
            return set(), set()
        if resolution not in {"profile_satisfied", "profile_runtime_fallback"} or edge.get("transported_bytes") != 0:
            return set(), set()
        relative = Path(str(edge.get("hol_relative_path") or ""))
        if edge.get("loader") != "needs" or relative.is_absolute() or not relative.parts:
            return set(), set()
        if any(part in {"", ".", ".."} for part in relative.parts) or edge.get("basename") != relative.name:
            return set(), set()
        if not re.fullmatch(r"[0-9a-f]{32}", str(edge.get("loader_md5") or "")):
            return set(), set()
        if not re.fullmatch(r"[0-9a-f]{64}", str(edge.get("sha256") or "")):
            return set(), set()
        if (
            not isinstance(edge.get("size_bytes"), int)
            or isinstance(edge.get("size_bytes"), bool)
            or edge.get("size_bytes") < 0
        ):
            return set(), set()
        mapping_root = edge.get("mapping_root") or "holdir"
        if mapping_root == "holdir":
            if (
                resolution != "profile_satisfied"
                or not host_holdir.is_absolute()
                or not shelf_holdir.is_absolute()
                or record.get("resolution") not in {"external_resolved", "unresolved"}
                or record.get("symlinked") is not False
                or edge.get("host_path") != str(host_holdir / relative)
                or edge.get("shelf_path") != str(shelf_holdir / relative)
                or record.get("resolved_path") != edge.get("host_path")
                or edge.get("sha256") != record.get("sha256")
                or edge.get("size_bytes") != record.get("size_bytes")
            ):
                return set(), set()
        elif mapping_root == "profile_cwd":
            if (
                not host_profile_cwd.is_absolute()
                or not shelf_profile_cwd.is_absolute()
                or record.get("resolution") != "unresolved"
                or record.get("declared_path") != relative.as_posix()
                or record.get("resolved_path")
                or record.get("sha256")
                or record.get("size_bytes") is not None
                or edge.get("host_path") != str(host_profile_cwd / relative)
                or edge.get("shelf_path") != str(shelf_profile_cwd / relative)
            ):
                return set(), set()
        elif isinstance(mapping_root, str) and mapping_root.startswith("logical_source_root:"):
            alias = mapping_root.removeprefix("logical_source_root:")
            declarations = (closure.get("logical_source_roots") or {}).get("roots") or []
            if (
                resolution != "profile_satisfied"
                or not any(
                    isinstance(row, dict)
                    and row.get("alias") == alias
                    and row.get("execution_role") == "profile_cwd"
                    for row in declarations
                )
                or not host_profile_cwd.is_absolute()
                or not shelf_profile_cwd.is_absolute()
                or record.get("resolution") != "mounted_source"
                or record.get("logical_source_root") != alias
                or record.get("resolved_file") != (Path(alias) / relative).as_posix()
                or edge.get("host_path") != record.get("resolved_path")
                or edge.get("shelf_path") != str(shelf_profile_cwd / relative)
                or record.get("resolved_path") != edge.get("host_path")
                or edge.get("sha256") != record.get("sha256")
                or edge.get("size_bytes") != record.get("size_bytes")
            ):
                return set(), set()
        else:
            return set(), set()
        (satisfied if resolution == "profile_satisfied" else runtime).add(index)
    return satisfied, runtime


def profile_satisfied_record_indexes(document: object, closure: dict[str, Any]) -> set[int]:
    """Return exact preloaded record indexes after checking record-bound fields."""
    return _profile_record_index_sets(document, closure)[0]


def profile_runtime_fallback_record_indexes(document: object, closure: dict[str, Any]) -> set[int]:
    """Return exact profile-cwd runtime record indexes after checking identity."""
    return _profile_record_index_sets(document, closure)[1]


def profile_satisfied_needs_prelude(document: dict[str, Any] | None) -> list[str]:
    """Guard both preloaded and exact profile-cwd runtime ``needs`` edges."""

    satisfied = [edge for edge in (document or {}).get("edges") or [] if edge.get("resolution") == "profile_satisfied"]
    runtime = [
        edge for edge in (document or {}).get("edges") or [] if edge.get("resolution") == "profile_runtime_fallback"
    ]
    if not satisfied and not runtime:
        return []

    def guards(edges: list[dict[str, Any]]) -> str:
        return "; ".join(
            f"({ocaml_string_literal(str(edge['declared_path']))}, "
            f"({ocaml_string_literal(str(edge['basename']))}, {ocaml_string_literal(str(edge['loader_md5']))}, "
            f"{ocaml_string_literal(str(edge['shelf_path']))}))"
            for edge in edges
        )

    return [
        "(* Harness guard: exact profile-satisfied needs may no-op, but may never reload. *)",
        "(* Exact unprimed profile-cwd needs may load only their hash-bound runtime path. *)",
        f"let proof_run_profile_satisfied_needs = [{guards(satisfied)}];;",
        f"let proof_run_profile_runtime_needs = [{guards(runtime)}];;",
        "let proof_run_profile_need entries path =",
        "  try Some (List.assoc path entries) with Not_found -> None;;",
        "let proof_run_validate_profile_need path (expected_basename,expected_md5,expected_path) =",
        "  let actual_basename = Filename.basename path in",
        "  let actual_digest = Digest.file expected_path in",
        "  if actual_basename <> expected_basename || Digest.to_hex actual_digest <> expected_md5",
        '  then failwith ("profile needs changed before evaluation: " ^ path)',
        "  else (actual_basename,actual_digest);;",
        "let proof_run_pre_profile_satisfied_needs = needs;;",
        "let needs path =",
        '  Format.print_string ("__PROOF_RUN_LITERAL_LOAD_STARTED__:needs:" ^ path ^ "\\n");',
        "  Format.print_flush ();",
        "  (match proof_run_profile_need proof_run_profile_satisfied_needs path with",
        "    Some expected ->",
        "      let actual_basename,actual_digest = proof_run_validate_profile_need path expected in",
        "      if not (List.mem (actual_basename,actual_digest) !loaded_files)",
        '    then failwith ("profile-satisfied needs absent from restored loaded_files: " ^ path)',
        "      else",
        '      Format.print_string ("File \\"" ^ path ^ "\\" already loaded\\n")',
        "  | None ->",
        "      (match proof_run_profile_need proof_run_profile_runtime_needs path with",
        "         Some ((_,_,expected_path) as expected) ->",
        "           ignore (proof_run_validate_profile_need path expected);",
        "           proof_run_pre_profile_satisfied_needs expected_path",
        "       | None -> proof_run_pre_profile_satisfied_needs path));",
        '  Format.print_string ("__PROOF_RUN_LITERAL_LOAD_COMPLETED__:needs:" ^ path ^ "\\n");',
        "  Format.print_flush ();;",
        "",
    ]


def _revalidate_live_edge_files(document: dict[str, Any]) -> None:
    for edge in [*(document.get("edges") or []), *(document.get("captured_warm_sources") or [])]:
        host_path = Path(str(edge.get("host_path") or ""))
        try:
            resolved = host_path.resolve(strict=True)
            current_size = host_path.stat().st_size
            current_md5 = _md5_file(host_path)
            current_sha256 = sha256_file(host_path)
        except OSError as exc:
            raise ProfileSatisfactionError(
                "refused_profile_satisfaction_dependency_changed",
                f"cannot revalidate profile-satisfied dependency {host_path}: {exc}",
            ) from exc
        if (
            not host_path.is_absolute()
            or host_path.is_symlink()
            or resolved != host_path
            or current_size != edge.get("size_bytes")
            or current_md5 != edge.get("loader_md5")
            or current_sha256 != edge.get("sha256")
        ):
            raise ProfileSatisfactionError(
                "refused_profile_satisfaction_dependency_changed",
                f"profile-satisfied dependency changed before evaluation: {host_path}",
            )


def revalidate_profile_satisfaction(
    document: dict[str, Any],
    closure: dict[str, Any],
    *,
    profile_root: Path,
) -> None:
    """Rebuild the decision from live files and require exact document equality."""

    if not profile_satisfaction_identity_matches(document, closure):
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_identity_invalid",
            "profile-satisfaction decision identity is invalid",
        )
    _revalidate_live_edge_files(document)
    rebuilt = build_profile_satisfaction(
        closure,
        profile_root=profile_root,
        logical_profile=str(document.get("logical_profile") or ""),
        host_holdir=Path(str(document["host_holdir"])) if document.get("host_holdir") else None,
        host_profile_cwd=Path(str(document["host_profile_cwd"])) if document.get("host_profile_cwd") else None,
        snapshot_admission_artifact=(
            Path(str(document["snapshot_admission_artifact"])) if document.get("snapshot_admission_artifact") else None
        ),
        snapshot_admission_artifact_sha256=(
            str(document["snapshot_admission_artifact_sha256"]) if document.get("snapshot_admission_artifact") else None
        ),
        snapshot_admission_decision_sha256=(
            str((document.get("snapshot_admission") or {}).get("decision_sha256"))
            if document.get("snapshot_admission_artifact")
            else None
        ),
        snapshot_admission_request_sha256=(
            str(((document.get("snapshot_admission") or {}).get("request") or {}).get("request_sha256"))
            if document.get("snapshot_admission_artifact")
            else None
        ),
        snapshot_admission_decision=(
            document.get("snapshot_admission") if not document.get("snapshot_admission_artifact") else None
        ),
    )
    if rebuilt != document:
        raise ProfileSatisfactionError(
            "refused_profile_satisfaction_changed",
            "profile-satisfaction decision changed during pre-evaluation revalidation",
        )
