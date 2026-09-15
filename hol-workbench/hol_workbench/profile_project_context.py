"""Resolve project-profile working directories from explicit Linux roots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProfileProjectContext:
    declared: str
    resolved: str
    resolution: str
    root_label: str = ""
    root: str = ""
    candidate: str = ""

    def assignments(self) -> dict[str, str]:
        return {
            "PROFILE_CWD": self.resolved,
            "PROFILE_CWD_DECLARED": self.declared,
            "PROFILE_CWD_RESOLUTION": self.resolution,
            "PROFILE_CWD_ROOT_LABEL": self.root_label,
            "PROFILE_CWD_ROOT": self.root,
            "PROFILE_CWD_CANDIDATE": self.candidate,
        }


@dataclass(frozen=True)
class ProfileContextProblem:
    profile: str
    reason: str
    next_action: str


def _path_entry_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def resolve_profile_project_context(
    configured_root: Path,
    declared: str,
    *,
    root_label: str = "checkout",
) -> ProfileProjectContext:
    """Resolve a manifest CWD without consulting Git, PATH, or host state."""
    if not declared:
        return ProfileProjectContext(declared="", resolved="", resolution="none")

    requested = Path(declared).expanduser()
    if requested.is_absolute():
        resolution = "absolute" if _path_entry_exists(requested) else "missing"
        return ProfileProjectContext(
            declared=declared,
            resolved=str(requested.resolve()),
            resolution=resolution,
            root_label="absolute",
            candidate=str(requested),
        )

    root = configured_root.expanduser().resolve()
    candidate = root / requested
    return ProfileProjectContext(
        declared=declared,
        resolved=str(candidate.resolve()),
        resolution=f"{root_label}_relative" if _path_entry_exists(candidate) else "missing",
        root_label=root_label,
        root=str(root),
        candidate=str(candidate),
    )


def profile_context_problem(profile: str, assignments: dict[str, Any]) -> ProfileContextProblem | None:
    if assignments.get("PROFILE_CWD_RESOLUTION") != "missing":
        return None
    declared = str(assignments.get("PROFILE_CWD_DECLARED") or assignments.get("PROFILE_CWD") or "-")
    candidate = str(assignments.get("PROFILE_CWD_CANDIDATE") or declared)
    root_label = str(assignments.get("PROFILE_CWD_ROOT_LABEL") or "configured")
    root = str(assignments.get("PROFILE_CWD_ROOT") or "-")
    return ProfileContextProblem(
        profile=profile,
        reason=(
            f"profile {profile} declares project cwd {declared}, but it is absent from "
            f"the Linux {root_label} root {root}; "
            "shelf compatibility was not evaluated"
        ),
        next_action=(
            f"restore or link the project checkout at {candidate}, then rerun "
            "`hol-workbench/bin/orbstack-criu status`; do not rebuild the shelf"
        ),
    )
