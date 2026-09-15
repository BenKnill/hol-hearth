"""Durable, hash-bound provenance for one published CRIU authoring shelf."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hol_workbench.hashing import sha256_file

JsonObject = dict[str, Any]
GitRunner = Callable[..., subprocess.CompletedProcess[str]]
LOWER_SHA256 = re.compile(r"[0-9a-f]{64}")
LOWER_GIT_OID = re.compile(r"[0-9a-f]{40,64}")


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def _hash_bound(payload: JsonObject, *, field: str) -> JsonObject:
    return {**payload, field: _canonical_sha256(payload)}


def _hash_bound_failures(record: object, *, schema: str, field: str) -> list[str]:
    if not isinstance(record, dict) or record.get("schema") != schema:
        return [f"{schema} record is missing or incompatible"]
    payload = {key: value for key, value in record.items() if key != field}
    if record.get(field) != _canonical_sha256(payload):
        return [f"{schema} has an invalid {field}"]
    return []


def _git(root: Path, *arguments: str, run: GitRunner = subprocess.run) -> str:
    completed = run(
        ["git", "-C", str(root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        detail = " ".join((completed.stderr or completed.stdout or "").split())[-500:]
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail or f'exit {completed.returncode}'}")
    return completed.stdout.strip()


def committed_source_identity(root: Path, *, run: GitRunner = subprocess.run) -> JsonObject:
    """Require a clean checkout and bind its exact commit and tree."""

    resolved = root.expanduser().resolve()
    top = Path(_git(resolved, "rev-parse", "--show-toplevel", run=run)).resolve()
    if top != resolved:
        raise RuntimeError(f"Workbench source root {resolved} is not its Git top-level {top}")
    dirty = _git(resolved, "status", "--porcelain=v1", "--untracked-files=all", run=run)
    if dirty:
        entries = dirty.replace("\n", " | ")[:500]
        raise RuntimeError(f"CRIU shelf publication requires clean committed Workbench source: {entries}")
    revision = _git(resolved, "rev-parse", "HEAD", run=run)
    tree = _git(resolved, "rev-parse", "HEAD^{tree}", run=run)
    if LOWER_GIT_OID.fullmatch(revision) is None or LOWER_GIT_OID.fullmatch(tree) is None:
        raise RuntimeError("Workbench Git revision or tree is not a canonical object id")
    try:
        remote = _git(resolved, "config", "--get", "remote.origin.url", run=run)
    except RuntimeError:
        remote = None
    return _hash_bound(
        {
            "schema": "hol-workbench.committed-source-identity.v1",
            "repository_root": str(resolved),
            "remote": remote,
            "revision": revision,
            "tree": tree,
            "clean": True,
        },
        field="identity_sha256",
    )


def profile_recipe_record(base: Path, *, basis_id: str, expected_sha256: str) -> JsonObject:
    """Embed the exact flattened ML load sequence used to create the basis."""

    resolved = base.expanduser().resolve(strict=True)
    raw = resolved.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise RuntimeError(f"profile recipe SHA-256 {digest} != expected {expected_sha256}")
    try:
        exact_source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("profile recipe is not UTF-8") from exc
    return _hash_bound(
        {
            "schema": "hol-workbench.flattened-profile-recipe.v1",
            "basis_id": basis_id,
            "source_path": str(resolved),
            "source_sha256": digest,
            "source_size_bytes": len(raw),
            "exact_source_utf8": exact_source,
            "load_order_authority": "exact_source_order_evaluated_by_hol",
        },
        field="recipe_sha256",
    )


def image_inventory_record(image_dir: Path) -> JsonObject:
    """Hash every CRIU dump artifact present before the restore probe."""

    root = image_dir.expanduser().resolve(strict=True)
    entries: list[JsonObject] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"CRIU image inventory refuses symlink {path}")
        if not path.is_file():
            continue
        digest = sha256_file(path)
        if digest is None:
            raise RuntimeError(f"cannot hash CRIU image artifact {path}")
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": digest,
                "size_bytes": path.stat().st_size,
            }
        )
    if not entries:
        raise RuntimeError("CRIU image inventory is empty")
    return _hash_bound(
        {
            "schema": "hol-workbench.criu-image-inventory.v1",
            "file_count": len(entries),
            "size_bytes": sum(int(entry["size_bytes"]) for entry in entries),
            "entries": entries,
        },
        field="inventory_sha256",
    )


def post_restore_smoke_record(
    profile_root: Path,
    *,
    source: Path,
    contract: JsonObject,
    eval_evidence: JsonObject,
    broker_probe: JsonObject,
) -> JsonObject:
    """Bind the exact post-restore source, sentinel contract, and result."""

    root = profile_root.expanduser().resolve()
    resolved_source = source.expanduser().resolve(strict=True)
    try:
        relative_source = resolved_source.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError("post-restore smoke source is outside the profile shelf") from exc
    digest = sha256_file(resolved_source)
    if digest is None:
        raise RuntimeError("post-restore smoke source is unreadable")
    required_markers = contract.get("required_markers")
    markers = eval_evidence.get("markers")
    passed = (
        isinstance(required_markers, list)
        and bool(required_markers)
        and isinstance(markers, dict)
        and all(markers.get(marker) is True for marker in required_markers)
        and eval_evidence.get("proved") is True
        and broker_probe.get("status") == "passed"
    )
    if not passed:
        raise RuntimeError("post-restore smoke did not satisfy its exact sentinel contract")
    return _hash_bound(
        {
            "schema": "hol-workbench.post-restore-smoke.v1",
            "status": "passed",
            "evidence_boundary": "runtime_checked_authoring_basis_not_final_theorem_evidence",
            "source": {
                "path": relative_source,
                "sha256": digest,
                "size_bytes": resolved_source.stat().st_size,
            },
            "contract": contract,
            "eval_evidence": eval_evidence,
            "broker_probe": broker_probe,
        },
        field="smoke_sha256",
    )


def _source_identity_failures(record: object) -> list[str]:
    failures = _hash_bound_failures(
        record,
        schema="hol-workbench.committed-source-identity.v1",
        field="identity_sha256",
    )
    data = record if isinstance(record, dict) else {}
    if data.get("clean") is not True:
        failures.append("Workbench source identity is not clean")
    for field in ("revision", "tree"):
        if LOWER_GIT_OID.fullmatch(str(data.get(field) or "")) is None:
            failures.append(f"Workbench source identity {field} is invalid")
    return failures


def _recipe_failures(
    record: object,
    *,
    expected_basis_id: object,
    expected_sha256: object,
) -> list[str]:
    failures = _hash_bound_failures(
        record,
        schema="hol-workbench.flattened-profile-recipe.v1",
        field="recipe_sha256",
    )
    data = record if isinstance(record, dict) else {}
    source = data.get("exact_source_utf8")
    raw = source.encode("utf-8") if isinstance(source, str) else b""
    digest = hashlib.sha256(raw).hexdigest()
    if data.get("source_sha256") != digest or data.get("source_size_bytes") != len(raw):
        failures.append("flattened profile recipe bytes do not match their identity")
    if expected_sha256 != digest:
        failures.append("flattened profile recipe differs from the manifest profile SHA-256")
    if data.get("basis_id") != expected_basis_id:
        failures.append("flattened profile recipe basis ID differs from the manifest profile basis ID")
    if data.get("load_order_authority") != "exact_source_order_evaluated_by_hol":
        failures.append("flattened profile recipe has no exact load-order authority")
    return failures


def _image_inventory_failures(profile_root: Path, record: object, *, verify_files: bool) -> list[str]:
    failures = _hash_bound_failures(
        record,
        schema="hol-workbench.criu-image-inventory.v1",
        field="inventory_sha256",
    )
    data = record if isinstance(record, dict) else {}
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries or any(not isinstance(entry, dict) for entry in entries):
        failures.append("CRIU image inventory entries are missing or invalid")
        return failures
    if data.get("file_count") != len(entries):
        failures.append("CRIU image inventory file count is inconsistent")
    sizes = [entry.get("size_bytes") for entry in entries]
    if any(not isinstance(size, int) or isinstance(size, bool) or size < 0 for size in sizes):
        failures.append("CRIU image inventory contains an invalid size")
    elif data.get("size_bytes") != sum(sizes):
        failures.append("CRIU image inventory byte count is inconsistent")
    if len({entry.get("path") for entry in entries}) != len(entries):
        failures.append("CRIU image inventory contains duplicate paths")
    if verify_files:
        root = profile_root / "criu-image"
        for entry in entries:
            relative = Path(str(entry.get("path") or ""))
            path = (root / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(root.resolve()):
                failures.append("CRIU image inventory path escapes the image directory")
            elif sha256_file(path) != entry.get("sha256"):
                failures.append(f"CRIU image artifact differs from inventory: {relative}")
    return failures


def _smoke_failures(profile_root: Path, record: object) -> list[str]:
    failures = _hash_bound_failures(
        record,
        schema="hol-workbench.post-restore-smoke.v1",
        field="smoke_sha256",
    )
    data = record if isinstance(record, dict) else {}
    if data.get("status") != "passed":
        failures.append("post-restore smoke did not pass")
    if data.get("evidence_boundary") != "runtime_checked_authoring_basis_not_final_theorem_evidence":
        failures.append("post-restore smoke evidence boundary is invalid")
    source_value = data.get("source")
    source: JsonObject = source_value if isinstance(source_value, dict) else {}
    relative = Path(str(source.get("path") or ""))
    path = (profile_root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(profile_root.resolve()):
        failures.append("post-restore smoke source path escapes the profile shelf")
    elif not path.is_file() or sha256_file(path) != source.get("sha256"):
        failures.append("post-restore smoke source differs from its recorded identity")
    contract_value = data.get("contract")
    contract: JsonObject = contract_value if isinstance(contract_value, dict) else {}
    required = contract.get("required_markers")
    sentinels = contract.get("sentinels")
    if not isinstance(required, list) or not required or not isinstance(sentinels, list) or not sentinels:
        failures.append("post-restore smoke sentinel contract is missing")
    evidence_value = data.get("eval_evidence")
    evidence: JsonObject = evidence_value if isinstance(evidence_value, dict) else {}
    markers_value = evidence.get("markers")
    markers: JsonObject = markers_value if isinstance(markers_value, dict) else {}
    if (
        evidence.get("proved") is not True
        or not isinstance(required, list)
        or any(markers.get(item) is not True for item in required)
    ):
        failures.append("post-restore smoke evidence does not prove every required marker")
    probe_value = data.get("broker_probe")
    probe: JsonObject = probe_value if isinstance(probe_value, dict) else {}
    if probe.get("status") != "passed":
        failures.append("post-restore broker probe did not pass")
    return failures


def publication_provenance_failures(
    profile_root: Path,
    record: object,
    *,
    expected_profile_basis_id: object,
    expected_profile_sha256: object,
    verify_image_files: bool = False,
) -> list[str]:
    """Validate the committed source, recipe, image, and smoke publication chain."""

    failures = _hash_bound_failures(
        record,
        schema="hol-workbench.criu-publication-provenance.v1",
        field="provenance_sha256",
    )
    data = record if isinstance(record, dict) else {}
    failures.extend(_source_identity_failures(data.get("workbench_source")))
    failures.extend(
        _recipe_failures(
            data.get("profile_recipe"),
            expected_basis_id=expected_profile_basis_id,
            expected_sha256=expected_profile_sha256,
        )
    )
    failures.extend(
        _image_inventory_failures(
            profile_root.expanduser().resolve(),
            data.get("image_inventory"),
            verify_files=verify_image_files,
        )
    )
    failures.extend(_smoke_failures(profile_root.expanduser().resolve(), data.get("post_restore_smoke")))
    return failures


def publication_provenance_record(
    profile_root: Path,
    *,
    workbench_source: JsonObject,
    profile_recipe: JsonObject,
    image_inventory: JsonObject,
    post_restore_smoke: JsonObject,
    expected_profile_basis_id: str,
    expected_profile_sha256: str,
    verify_image_files: bool = True,
) -> JsonObject:
    record = _hash_bound(
        {
            "schema": "hol-workbench.criu-publication-provenance.v1",
            "workbench_source": workbench_source,
            "profile_recipe": profile_recipe,
            "image_inventory": image_inventory,
            "post_restore_smoke": post_restore_smoke,
        },
        field="provenance_sha256",
    )
    failures = publication_provenance_failures(
        profile_root,
        record,
        expected_profile_basis_id=expected_profile_basis_id,
        expected_profile_sha256=expected_profile_sha256,
        verify_image_files=verify_image_files,
    )
    if failures:
        raise RuntimeError("CRIU publication provenance is invalid: " + "; ".join(failures))
    return record
