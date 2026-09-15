"""Resolve one compatible, already-published public warm profile."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from hol_workbench.cli.profile_route_compatibility import compatible_profile_row
from hol_workbench.cli.prove_profiles import warmup_profile_identity_assignments
from hol_workbench.criu_snapshot_compat import default_snapshot_admission_request
from hol_workbench.logical_source_roots import logical_source_root_declarations
from hol_workbench.profile_project_context import profile_context_problem
from hol_workbench.ubuntu_runtime_layout import resolve_profile_cwd, resolve_ubuntu_runtime_layout


@dataclass(frozen=True)
class PublishedWarmProfile:
    name: str
    root: Path
    cwd: Path
    capacity: int
    legacy_holdir_roots: tuple[Path, ...]
    logical_source_roots: tuple[dict[str, str], ...]


def resolve_published_warm_profile(script_dir: Path, name: str) -> PublishedWarmProfile:
    layout = resolve_ubuntu_runtime_layout()
    assignments = warmup_profile_identity_assignments(script_dir, name)
    if problem := profile_context_problem(name, assignments):
        raise RuntimeError(f"{problem.reason}; {problem.next_action}")
    cwd = resolve_profile_cwd(assignments, holdir=layout.holdir).path
    capacity = int(assignments.get("PROFILE_ORBSTACK_PUBLIC_CAPACITY") or 1)
    row = compatible_profile_row(
        layout.criu_shelf_root.path,
        name,
        expected_base_name=Path(assignments["PROFILE_BASE"]).name,
        expected_cwd=str(cwd),
        expected_basis_id=assignments["PROFILE_BASIS_ID"],
        expected_sha256=assignments["PROFILE_SHA256"],
        expected_capacity=capacity,
        expected_execution_topology=assignments.get("PROFILE_EXECUTION_TOPOLOGY"),
        admission_request=default_snapshot_admission_request(),
        include_controller_attempt=False,
    )
    try:
        legacy_roots = tuple(
            Path(value) for value in json.loads(assignments.get("PROFILE_LEGACY_HOLDIR_ROOTS_JSON") or "[]")
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"profile {name!r} has invalid legacy HOL roots: {exc}") from exc
    try:
        logical_roots = logical_source_root_declarations(
            json.loads(assignments.get("PROFILE_LOGICAL_SOURCE_ROOTS_JSON") or "[]"),
            profile=name,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"profile {name!r} has invalid logical source roots: {exc}") from exc
    root = Path(str(row["run_root"])) / str(row["physical_profile"])
    return PublishedWarmProfile(
        name=name,
        root=root.resolve(),
        cwd=cwd,
        capacity=capacity,
        legacy_holdir_roots=legacy_roots,
        logical_source_roots=logical_roots,
    )
