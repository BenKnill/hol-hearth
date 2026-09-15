"""Typed resolution for the configured Linux HOL Light and CRIU roots."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from hol_workbench.runtime_config import RuntimeConfig, RuntimeConfigError, load_runtime_config

ORB_HOLDIR_ENV = "HOL_WORKBENCH_ORB_HOLDIR"
CRIU_SHELF_ROOT_ENV = "HOL_WORKBENCH_CRIU_SHELF_ROOT"

RuntimePathSource = Literal["configuration", "environment", "profile"]


class UbuntuRuntimeLayoutError(ValueError):
    """Raised when the Linux runtime roots cannot be resolved safely."""


@dataclass(frozen=True)
class ResolvedRuntimePath:
    path: Path
    source: RuntimePathSource
    source_label: str

    def record(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "source": self.source,
            "source_label": self.source_label,
        }


@dataclass(frozen=True)
class UbuntuRuntimeLayout:
    holdir: ResolvedRuntimePath
    criu_shelf_root: ResolvedRuntimePath

    def record(self) -> dict[str, dict[str, str]]:
        return {
            "holdir": self.holdir.record(),
            "criu_shelf_root": self.criu_shelf_root.record(),
        }


def _resolve_runtime_path(
    *,
    variable: str,
    config_field: Literal["hol_light_dir", "criu_shelf_root"],
    environment: Mapping[str, str],
    config: RuntimeConfig | None = None,
) -> ResolvedRuntimePath:
    configured = environment.get(variable)
    if configured:
        candidate = Path(configured).expanduser()
        source: RuntimePathSource = "environment"
        source_label = variable
    else:
        try:
            selected_config = load_runtime_config(environment) if config is None else config
        except RuntimeConfigError as exc:
            raise UbuntuRuntimeLayoutError(str(exc)) from exc
        candidate = (
            selected_config.hol_light_dir if config_field == "hol_light_dir" else selected_config.criu_shelf_root
        )
        source = "configuration"
        source_label = f"{selected_config.path}#runtime.{config_field}"
    if not candidate.is_absolute():
        raise UbuntuRuntimeLayoutError(
            f"{variable} must be an absolute Linux path; got {str(candidate)!r} from {source_label}"
        )
    return ResolvedRuntimePath(path=candidate.resolve(), source=source, source_label=source_label)


def resolve_orb_holdir(environment: Mapping[str, str] | None = None) -> ResolvedRuntimePath:
    active_environment = os.environ if environment is None else environment
    return _resolve_runtime_path(
        variable=ORB_HOLDIR_ENV,
        config_field="hol_light_dir",
        environment=active_environment,
    )


def resolve_criu_shelf_root(environment: Mapping[str, str] | None = None) -> ResolvedRuntimePath:
    active_environment = os.environ if environment is None else environment
    return _resolve_runtime_path(
        variable=CRIU_SHELF_ROOT_ENV,
        config_field="criu_shelf_root",
        environment=active_environment,
    )


def resolve_profile_cwd(
    assignments: Mapping[str, str],
    *,
    holdir: ResolvedRuntimePath | None = None,
    environment: Mapping[str, str] | None = None,
) -> ResolvedRuntimePath:
    configured = assignments.get("PROFILE_CWD")
    if not configured:
        return holdir if holdir is not None else resolve_orb_holdir(environment)
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        raise UbuntuRuntimeLayoutError(
            f"PROFILE_CWD must be an absolute Linux path; got {str(candidate)!r} from profile manifest"
        )
    return ResolvedRuntimePath(path=candidate.resolve(), source="profile", source_label="PROFILE_CWD")


def resolve_ubuntu_runtime_layout(environment: Mapping[str, str] | None = None) -> UbuntuRuntimeLayout:
    active_environment = os.environ if environment is None else environment
    needs_config = not active_environment.get(ORB_HOLDIR_ENV) or not active_environment.get(CRIU_SHELF_ROOT_ENV)
    try:
        config = load_runtime_config(active_environment) if needs_config else None
    except RuntimeConfigError as exc:
        raise UbuntuRuntimeLayoutError(str(exc)) from exc
    return UbuntuRuntimeLayout(
        holdir=_resolve_runtime_path(
            variable=ORB_HOLDIR_ENV,
            config_field="hol_light_dir",
            environment=active_environment,
            config=config,
        ),
        criu_shelf_root=_resolve_runtime_path(
            variable=CRIU_SHELF_ROOT_ENV,
            config_field="criu_shelf_root",
            environment=active_environment,
            config=config,
        ),
    )
