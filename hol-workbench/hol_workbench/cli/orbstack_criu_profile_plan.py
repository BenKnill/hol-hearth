"""Profile-selection policy for OrbStack/CRIU shelf builds."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from hol_workbench.cli.prove_profiles import load_developer_manifest

WORKBENCH_BIN = Path(__file__).resolve().parents[2] / "bin"
DEFAULT_PROFILES = ("light", "heavy")
PROFILE_SELECTION_ENV = "CRIU_PROFILES"
EXPLICIT_OPT_IN = "CRIU_PROFILES=PROFILE[,PROFILE...] hol-workbench/bin/orbstack-criu build"


@dataclass(frozen=True)
class ProfileBuildPlan:
    profiles: tuple[str, ...]
    known_profiles: tuple[str, ...]
    selection_source: str


def known_profile_names() -> tuple[str, ...]:
    profiles = load_developer_manifest(WORKBENCH_BIN).get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise SystemExit("OrbStack CRIU build: warmup profile manifest has no profiles")
    return tuple(str(name) for name in profiles)


def profile_build_plan(env: Mapping[str, str] | None = None) -> ProfileBuildPlan:
    environ = os.environ if env is None else env
    known_profiles = known_profile_names()
    raw = environ.get(PROFILE_SELECTION_ENV, "").strip()
    if not raw:
        return ProfileBuildPlan(
            profiles=DEFAULT_PROFILES,
            known_profiles=known_profiles,
            selection_source="default",
        )

    selected = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not selected:
        raise SystemExit(f"OrbStack CRIU build: {PROFILE_SELECTION_ENV} selects no profiles")
    unknown = tuple(name for name in selected if name not in known_profiles)
    if unknown:
        available = ",".join(known_profiles)
        raise SystemExit(
            f"OrbStack CRIU build: unknown profile(s): {','.join(unknown)}; available profiles: {available}"
        )
    return ProfileBuildPlan(
        profiles=selected,
        known_profiles=known_profiles,
        selection_source=PROFILE_SELECTION_ENV,
    )


def print_profile_build_preflight(plan: ProfileBuildPlan, *, out: TextIO) -> None:
    print("OrbStack CRIU build preflight", file=out)
    print(
        "Maintainer policy: large profile rebuilds require an otherwise idle machine; use only light during active work.",
        file=out,
    )
    print("resource_policy=large_profiles_require_idle_machine", file=out)
    print("active_work_rebuild_test=CRIU_PROFILES=light", file=out)
    print(f"selection_source={plan.selection_source}", file=out)
    print(f"profiles={','.join(plan.profiles)}", file=out)
    print(f"bare_build_default={','.join(DEFAULT_PROFILES)}", file=out)
    print(f"explicit_opt_in={EXPLICIT_OPT_IN}", file=out)
    print(f"known_profiles={','.join(plan.known_profiles)}", file=out)
