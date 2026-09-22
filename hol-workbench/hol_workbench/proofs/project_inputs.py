"""Shared conservative readiness projection for project-owned proof inputs."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from hol_workbench.hashing import sha256_bytes, sha256_file
from hol_workbench.proofs.project_input_context import (
    project_input_context_identity_payload,
    project_input_context_is_consistent,
    resolve_project_input_context,
)

PROJECT_INPUT_READINESS_SCHEMA = "hol-workbench.project-input-readiness.v2"
PROJECT_INPUT_DECLARATION_SCHEMA = "hol-workbench.project-inputs.v1"
PROJECT_INPUT_DECLARATION = ".hol-workbench-project-inputs.json"
PROJECT_INPUT_MISSING = "project_input_missing"
PROJECT_INPUT_INVALID = "project_input_invalid"
PROJECT_INPUT_INVALID_PATH = "project_input_invalid_path"
PROJECT_INPUT_SYMLINK = "project_input_symlink"
PROJECT_INPUT_NON_REGULAR = "project_input_non_regular"
PROJECT_INPUT_UNREADABLE = "project_input_unreadable"
PROJECT_INPUT_IDENTITY_MISMATCH = "project_input_identity_mismatch"
PROJECT_INPUT_COLLISION = "project_input_collision"
PROJECT_INPUT_UNKNOWN = "project_input_unknown"
_MAX_DECLARATION_BYTES = 16_384
_MAX_BUILD_HINT_CHARS = 500


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return sha256_bytes(raw)


def _project_declaration(project_root: Path) -> dict[str, Any]:
    declaration = project_root / PROJECT_INPUT_DECLARATION
    base: dict[str, Any] = {
        "path": str(declaration),
        "status": "absent",
        "meaning": "optional display-only project metadata; Hearth never executes it",
    }
    if not declaration.exists():
        return base
    if not declaration.is_file() or declaration.is_symlink():
        return {**base, "status": "invalid", "reason": "declaration must be a regular non-symlink file"}
    if declaration.stat().st_size > _MAX_DECLARATION_BYTES:
        return {**base, "status": "invalid", "reason": "declaration exceeds the bounded size limit"}
    try:
        data = json.loads(declaration.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {**base, "status": "invalid", "reason": f"declaration is unreadable or invalid JSON: {exc}"}
    if not isinstance(data, dict) or data.get("schema") != PROJECT_INPUT_DECLARATION_SCHEMA:
        return {**base, "status": "invalid", "reason": "declaration schema is unsupported"}
    raw_hint = data.get("build_hint")
    if raw_hint is None:
        return {**base, "status": "valid", "sha256": sha256_file(declaration)}
    if not isinstance(raw_hint, str):
        return {**base, "status": "invalid", "reason": "build_hint must be a string"}
    build_hint = " ".join(raw_hint.split())
    if not build_hint or len(build_hint) > _MAX_BUILD_HINT_CHARS:
        return {**base, "status": "invalid", "reason": "build_hint must be a bounded non-empty string"}
    return {
        **base,
        "status": "valid",
        "sha256": sha256_file(declaration),
        "build_hint": build_hint,
    }


def _artifact_outcome(artifact: dict[str, Any]) -> tuple[str, str, str]:
    resolution = str(artifact.get("resolution") or "")
    if resolution == "source_local":
        return (
            "ready",
            "project_input_ready",
            "exact persisted project input is a readable non-symlink regular file with a SHA-256 identity",
        )
    if resolution == "unresolved":
        return "missing", PROJECT_INPUT_MISSING, "literal project input does not exist"
    if resolution == "invalid_path":
        return "invalid", PROJECT_INPUT_INVALID_PATH, "literal project input path escapes its canonical project root"
    if resolution == "symlinked_path":
        return "invalid", PROJECT_INPUT_SYMLINK, "literal project input path contains a symlink"
    if resolution == "non_regular":
        return "invalid", PROJECT_INPUT_NON_REGULAR, "literal project input is not a regular file"
    if resolution == "unreadable":
        return "invalid", PROJECT_INPUT_UNREADABLE, "literal project input is unreadable"
    return "invalid", PROJECT_INPUT_IDENTITY_MISMATCH, "literal project input identity is invalid"


def _input_record(artifact: dict[str, Any], project_root: Path) -> dict[str, Any]:
    status, outcome, summary = _artifact_outcome(artifact)
    source = str(artifact.get("declaring_path") or artifact.get("declaring_file") or "")
    input_path = str(artifact.get("input_path") or artifact.get("resolved_path") or "")
    name = str(artifact.get("name") or "-")
    reason = (
        f"{PROJECT_INPUT_MISSING}: missing {artifact.get('loader')} object for {name}; "
        f"source={source}; input={input_path}"
        if outcome == PROJECT_INPUT_MISSING
        else f"{outcome}: {summary}; source={source}; input={input_path}; name={name}"
    )
    return {
        "kind": "literal_elf_object",
        "loader": artifact.get("loader"),
        "name": artifact.get("name"),
        "source": source,
        "source_ref": artifact.get("declaring_file"),
        "source_line": artifact.get("source_line"),
        "declared_path": artifact.get("declared_path"),
        "input_path": input_path,
        "resolved_path": artifact.get("resolved_path"),
        "project_root": str(project_root),
        "resolution": artifact.get("resolution"),
        "resolution_base": artifact.get("resolution_base"),
        "package_path": artifact.get("package_path"),
        "status": status,
        "outcome": outcome,
        "reason": reason,
        "exists": artifact.get("exists"),
        "is_file": artifact.get("is_file"),
        "readable": artifact.get("readable"),
        "sha256": artifact.get("sha256"),
        "size_bytes": artifact.get("size_bytes"),
        "symlinked": artifact.get("symlinked"),
        "lexical_parent_component": artifact.get("lexical_parent_component"),
    }


def _collision_record(kind: str, *, key: str, owners: list[dict[str, Any]]) -> dict[str, Any]:
    rendered = ", ".join(
        f"{item.get('role')}:{item.get('source_ref') or item.get('resolved_path') or '-'}" for item in owners
    )
    return {
        "kind": kind,
        "key": key,
        "status": "invalid",
        "outcome": PROJECT_INPUT_COLLISION,
        "reason": f"{PROJECT_INPUT_COLLISION}: {kind} at {key}: {rendered}",
        "owners": owners,
    }


def _logical_name_collisions(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in inputs:
        name = item.get("name")
        if isinstance(name, str) and name:
            grouped.setdefault(name, []).append(item)
    collisions: list[dict[str, Any]] = []
    for name, rows in grouped.items():
        if len(rows) < 2:
            continue
        known_digests = {row.get("sha256") for row in rows if row.get("sha256")}
        kind = "logical_name_content_conflict" if len(known_digests) > 1 else "duplicate_logical_name"
        owners = [
            {
                "role": "literal_elf_artifact" if row.get("status") != "unknown" else "dynamic_elf_artifact",
                "source_ref": row.get("source_ref"),
                "source_line": row.get("source_line"),
                "declared_path": row.get("declared_path"),
                "package_path": row.get("package_path"),
                "sha256": row.get("sha256"),
                "size_bytes": row.get("size_bytes"),
            }
            for row in rows
        ]
        # Even byte-identical duplicate bindings are ambiguous logical claims.
        collisions.append(_collision_record(kind, key=name, owners=owners))
    return collisions


def _package_role(record: dict[str, Any]) -> str:
    resolution = record.get("resolution")
    if resolution == "holdir_source":
        return "holdir_dependency"
    if resolution == "mounted_source":
        return "mounted_source_dependency"
    if resolution == "source_overlay":
        return "source_overlay_dependency"
    return "dependency"


def _package_collisions(closure: dict[str, Any], inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claimed: dict[str, tuple[tuple[object, ...], dict[str, Any]]] = {}
    collisions: list[dict[str, Any]] = []

    def claim(portable: str, identity: tuple[object, ...], owner: dict[str, Any]) -> None:
        if not portable:
            return
        previous = claimed.get(portable)
        if previous is not None and previous[0] != identity:
            collisions.append(_collision_record("package_path_collision", key=portable, owners=[previous[1], owner]))
            return
        claimed[portable] = (identity, owner)

    entrypoint = closure.get("entrypoint") or {}
    claim(
        str(entrypoint.get("package_path") or ""),
        ("entrypoint", entrypoint.get("sha256"), entrypoint.get("size_bytes"), entrypoint.get("path")),
        {
            "role": "entrypoint",
            "source_ref": "<entrypoint>",
            "sha256": entrypoint.get("sha256"),
            "size_bytes": entrypoint.get("size_bytes"),
        },
    )
    for record in closure.get("records") or []:
        if record.get("resolution") not in {"source_local", "source_overlay", "holdir_source", "mounted_source"}:
            continue
        if record.get("traversal") not in {"followed", "already_seen", "cycle"}:
            continue
        role = _package_role(record)
        claim(
            str(record.get("package_path") or ""),
            (role, record.get("sha256"), record.get("size_bytes"), record.get("resolved_path")),
            {
                "role": role,
                "source_ref": record.get("declaring_file"),
                "declared_path": record.get("declared_path"),
                "resolved_path": record.get("resolved_path"),
                "sha256": record.get("sha256"),
                "size_bytes": record.get("size_bytes"),
            },
        )
    for item in inputs:
        if item.get("status") != "ready":
            continue
        claim(
            str(item.get("package_path") or ""),
            (
                "literal_elf_artifact",
                item.get("sha256"),
                item.get("size_bytes"),
                item.get("resolved_path"),
            ),
            {
                "role": "literal_elf_artifact",
                "source_ref": item.get("source_ref"),
                "declared_path": item.get("declared_path"),
                "resolved_path": item.get("resolved_path"),
                "sha256": item.get("sha256"),
                "size_bytes": item.get("size_bytes"),
            },
        )
    return collisions


def project_input_projection_identity_payload(report: dict[str, Any]) -> dict[str, Any]:
    """Return the portable, persisted subset bound into the closure identity."""

    declaration = report.get("declaration") or {}
    return {
        "schema": report.get("schema"),
        "project_input_context": project_input_context_identity_payload(report.get("project_input_context") or {}),
        "status": report.get("status"),
        "outcome": report.get("outcome"),
        "counts": report.get("counts"),
        "inputs": [
            {
                key: item.get(key)
                for key in (
                    "loader",
                    "name",
                    "source_ref",
                    "source_line",
                    "declared_path",
                    "package_path",
                    "resolution",
                    "resolution_base",
                    "status",
                    "outcome",
                    "sha256",
                    "size_bytes",
                    "symlinked",
                    "lexical_parent_component",
                )
            }
            for item in report.get("inputs") or []
        ],
        "unknown_inputs": [
            {
                key: item.get(key)
                for key in (
                    "loader",
                    "name",
                    "source_ref",
                    "source_line",
                    "expression",
                    "complete_path_argument",
                    "outcome",
                )
            }
            for item in report.get("unknown_inputs") or []
        ],
        "collisions": [
            {
                "kind": collision.get("kind"),
                "key": collision.get("key"),
                "outcome": collision.get("outcome"),
                "owners": [
                    {
                        key: owner.get(key)
                        for key in (
                            "role",
                            "source_ref",
                            "source_line",
                            "declared_path",
                            "package_path",
                            "sha256",
                            "size_bytes",
                        )
                    }
                    for owner in collision.get("owners") or []
                ],
            }
            for collision in report.get("collisions") or []
        ],
        "declaration": {
            key: declaration.get(key) for key in ("status", "sha256", "build_hint", "reason") if key in declaration
        },
    }


def project_input_projection_sha256(report: dict[str, Any]) -> str:
    return _canonical_sha256(project_input_projection_identity_payload(report))


def build_project_input_projection(closure: dict[str, Any]) -> dict[str, Any]:
    """Build one closure-owned readiness record without rescanning source text."""

    source = str((closure.get("entrypoint") or {}).get("path") or "")
    project_input_context = closure.get("project_input_context")
    if (
        not isinstance(project_input_context, dict)
        or not project_input_context_is_consistent(project_input_context)
        or project_input_context.get("source") != source
        or project_input_context.get("project_root") != closure.get("project_root")
        or project_input_context.get("root_facts") != closure.get("project_root_boundary")
    ):
        raise ValueError("source dependency closure has no consistent canonical project-input context")
    project_root = Path(str(project_input_context["project_root"])).expanduser().resolve()
    declaration = _project_declaration(project_root)
    inputs = [_input_record(item, project_root) for item in closure.get("artifacts") or []]
    unknown = [
        {
            "kind": "literal_elf_object",
            "loader": item.get("loader"),
            "name": item.get("name"),
            "source": item.get("declaring_path"),
            "source_ref": item.get("declaring_file"),
            "source_line": item.get("source_line"),
            "expression": item.get("expression"),
            "complete_path_argument": item.get("complete_path_argument"),
            "status": "unknown",
            "outcome": PROJECT_INPUT_UNKNOWN,
            "reason": item.get("reason"),
        }
        for item in closure.get("dynamic_artifacts") or []
    ]
    collisions = [*_logical_name_collisions([*inputs, *unknown]), *_package_collisions(closure, inputs)]
    missing = [item for item in inputs if item["outcome"] == PROJECT_INPUT_MISSING]
    invalid = [item for item in inputs if item["status"] == "invalid"]
    if collisions:
        status = "blocked"
        outcome = PROJECT_INPUT_COLLISION
    elif invalid:
        status = "blocked"
        invalid_outcomes = {str(item["outcome"]) for item in invalid}
        outcome = invalid_outcomes.pop() if len(invalid_outcomes) == 1 else PROJECT_INPUT_INVALID
    elif missing:
        status = "blocked"
        outcome = PROJECT_INPUT_MISSING
    elif unknown:
        status = "unknown"
        outcome = PROJECT_INPUT_UNKNOWN
    elif inputs:
        status = "ready"
        outcome = "project_inputs_ready"
    else:
        status = "not_declared"
        outcome = "project_inputs_not_declared"
    blockers = [item["reason"] for item in [*collisions, *invalid, *missing]]
    next_action = None
    if blockers:
        if missing and not collisions and not invalid:
            missing_paths = [str(item["input_path"]) for item in missing]
            target = missing_paths[0] if len(missing_paths) == 1 else ", ".join(missing_paths)
            next_action = f"provide the declared project input at {target}"
            build_hint = declaration.get("build_hint")
            if isinstance(build_hint, str) and build_hint:
                next_action += f"; project-declared build hint (not run): {build_hint}"
            next_action += "; then rerun the same prove command"
        elif collisions:
            next_action = (
                "resolve the reported project-input name or package-path collision, then rerun the same prove command"
            )
        else:
            next_action = (
                "replace the reported input with a readable non-symlink regular file inside the persisted project root, "
                "then rerun the same prove command"
            )
    report: dict[str, Any] = {
        "schema": PROJECT_INPUT_READINESS_SCHEMA,
        "source": source,
        "project_root": str(project_root),
        "project_root_boundary": project_input_context["root_facts"],
        "project_input_context": copy.deepcopy(project_input_context),
        "persisted_in_source_dependency_closure": True,
        "status": status,
        "outcome": outcome,
        "transport_blocker_status": outcome if status == "blocked" else None,
        "meaning": (
            "bounded static readiness for project-owned ELF/object inputs; unknown expressions may proceed "
            "to HOL and this record is not theorem or ELF-semantic evidence"
        ),
        "counts": {
            "literal": len(inputs),
            "ready": sum(item["status"] == "ready" for item in inputs),
            "missing": len(missing),
            "invalid": len(invalid),
            "unknown": len(unknown),
            "collision": len(collisions),
        },
        "inputs": inputs,
        "unknown_inputs": unknown,
        "collisions": collisions,
        "blockers": blockers,
        "next_action": next_action,
        "declaration": declaration,
        "checks": {
            "expression_extractor": "shared bounded OCaml expression boundary",
            "literal_resolution": "persisted project root in bounded source dependency closure",
            "lexical_and_resolved_containment": True,
            "lexical_parent_components_allowed_only_within_root": True,
            "symlinks_refused": True,
            "regular_file": True,
            "readable": True,
            "sha256": True,
            "logical_name_collisions": True,
            "package_path_collisions": True,
            "build_execution": False,
            "input_mutation": False,
        },
        "limitations": {
            "dynamic_expression_disposition": "unknown_may_proceed_to_hol",
            "dynamic_expression_scan": "same bounded resolved source closure",
            "literal_dependency_scan": "same bounded resolved source closure",
            "elf_semantics_checked": False,
        },
    }
    if isinstance(declaration.get("build_hint"), str):
        report["build_hint"] = declaration["build_hint"]
    report["projection_sha256"] = project_input_projection_sha256(report)
    return report


def project_input_analysis_unknown(
    source: Path,
    *,
    reason: str,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Return a nonblocking unknown record when no closure can be persisted."""

    source = source.expanduser().resolve()
    root = (project_root or source.parent).expanduser().resolve()
    project_input_context = resolve_project_input_context(
        entrypoint=source,
        source_root=source.parent,
        source_boundary={"kind": "entrypoint_parent"},
        declared_artifact_paths=[],
        explicit_project_root=root,
        max_parent_ascents=0,
    )
    report: dict[str, Any] = {
        "schema": PROJECT_INPUT_READINESS_SCHEMA,
        "source": str(source),
        "project_root": str(root),
        "project_root_boundary": project_input_context["root_facts"],
        "project_input_context": project_input_context,
        "persisted_in_source_dependency_closure": False,
        "status": "unknown",
        "outcome": PROJECT_INPUT_UNKNOWN,
        "transport_blocker_status": None,
        "meaning": "project-input analysis is unknown; HOL retains existing behavior and this is not proof evidence",
        "counts": {"literal": 0, "ready": 0, "missing": 0, "invalid": 0, "unknown": 1, "collision": 0},
        "inputs": [],
        "unknown_inputs": [
            {
                "kind": "project_input_analysis",
                "source": str(source),
                "source_ref": "<entrypoint>",
                "status": "unknown",
                "outcome": PROJECT_INPUT_UNKNOWN,
                "reason": reason,
            }
        ],
        "collisions": [],
        "blockers": [],
        "next_action": None,
        "declaration": {
            "path": str(root / PROJECT_INPUT_DECLARATION),
            "status": "not_read",
            "meaning": "closure unavailable; no project metadata was inferred or executed",
        },
        "checks": {"build_execution": False, "input_mutation": False},
        "limitations": {
            "dynamic_expression_disposition": "unknown_may_proceed_to_hol",
            "closure_analysis": "unavailable",
            "elf_semantics_checked": False,
        },
        "source_dependency_closure_sha256": None,
        "source_dependency_closure_schema": None,
    }
    report["projection_sha256"] = project_input_projection_sha256(report)
    return report


def project_input_readiness(
    source: Path,
    *,
    dependency_closure: dict[str, Any] | None = None,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Return the persisted closure projection; never rescan or re-root it."""

    source = source.expanduser().resolve()
    from hol_workbench.source_dependency_closure import (
        build_source_dependency_closure,
        source_dependency_closure_identity_matches,
    )

    closure = dependency_closure
    if closure is None:
        try:
            closure = build_source_dependency_closure(source, project_root=project_root)
        except (OSError, ValueError) as exc:
            return project_input_analysis_unknown(source, reason=str(exc), project_root=project_root)
    elif project_root is not None and Path(str(closure.get("project_root") or "")).resolve() != project_root.resolve():
        raise ValueError("project root cannot override the project root persisted in the source dependency closure")
    stored = closure.get("project_inputs")
    if not isinstance(stored, dict) or stored.get("schema") != PROJECT_INPUT_READINESS_SCHEMA:
        raise ValueError("source dependency closure has no persisted project-input readiness projection")
    expected_source = str((closure.get("entrypoint") or {}).get("path") or "")
    if expected_source != str(source):
        raise ValueError("source dependency closure entrypoint does not match project-input readiness source")
    if not source_dependency_closure_identity_matches(closure):
        raise ValueError("source dependency closure identity is invalid for project-input readiness")
    report = copy.deepcopy(stored)
    report["source_dependency_closure_sha256"] = closure.get("strict_sha256")
    report["source_dependency_closure_schema"] = closure.get("schema")
    return report
