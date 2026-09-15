"""Deterministic CRIU command construction."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from hol_workbench.runtime_config import (
    CRIU_MODE_ENVIRONMENT,
    CriuExecutionMode,
    RuntimeConfigError,
    load_runtime_config,
    parse_criu_execution_mode,
)

SUDO_EXECUTABLE: Final = "/usr/bin/sudo"
ENV_EXECUTABLE: Final = "/usr/bin/env"
CRIU_CONFIG_ISOLATED_ENVIRONMENT_PREFIX: Final = (
    ENV_EXECUTABLE,
    "-u",
    "CRIU_CONFIG_FILE",
)
CRIU_BIN_ENVIRONMENT: Final = "HOL_WORKBENCH_CRIU_BIN"
CRIU_INFORMATION_ACTIONS: Final = frozenset({"--help", "--version", "-h", "-V"})


def criu_execution_mode() -> CriuExecutionMode:
    """Resolve the typed maintenance authority independently of the CRIU path."""

    configured = os.environ.get(CRIU_MODE_ENVIRONMENT)
    source_label = CRIU_MODE_ENVIRONMENT
    if configured is None:
        if os.environ.get(CRIU_BIN_ENVIRONMENT) is not None:
            # A process-local binary substitution must not silently inherit
            # durable file-capability authority from an unrelated TOML path.
            return CriuExecutionMode.SUDO
        try:
            runtime_config = load_runtime_config()
        except RuntimeConfigError:
            # A path-only override was the historical developer contract.
            # Keep that isolated use sudo-backed until a mode is explicit.
            return CriuExecutionMode.SUDO
        return runtime_config.criu_mode
    try:
        return parse_criu_execution_mode(configured, label=source_label)
    except RuntimeConfigError as exc:
        raise RuntimeError(str(exc)) from exc


def criu_launcher_prefix(*, mode: CriuExecutionMode | None = None) -> list[str]:
    """Return the exact argv prefix shared by CRIU and its status-fd shim."""

    selected = criu_execution_mode() if mode is None else mode
    if selected is CriuExecutionMode.SUDO:
        return [SUDO_EXECUTABLE, "-n", *CRIU_CONFIG_ISOLATED_ENVIRONMENT_PREFIX]
    return [*CRIU_CONFIG_ISOLATED_ENVIRONMENT_PREFIX]


def criu_executable() -> str:
    """Return the validated CRIU executable without shell interpretation."""

    configured = os.environ.get(CRIU_BIN_ENVIRONMENT)
    source_label = CRIU_BIN_ENVIRONMENT
    if configured is None:
        try:
            runtime_config = load_runtime_config()
        except RuntimeConfigError as exc:
            raise RuntimeError(str(exc)) from exc
        configured = str(runtime_config.criu_bin)
        source_label = f"{runtime_config.path} [runtime].criu_bin"

    executable = Path(configured)
    if not executable.is_absolute():
        raise RuntimeError(f"{source_label} must be an absolute path: {configured!r}")
    if not executable.exists():
        raise RuntimeError(f"{source_label} does not exist: {configured}")
    if not executable.is_file():
        raise RuntimeError(f"{source_label} is not a regular file: {configured}")
    if not os.access(executable, os.X_OK):
        raise RuntimeError(f"{source_label} is not executable: {configured}")
    return str(executable.resolve())


def criu_command(*args: str, environment: tuple[str, ...] = ()) -> list[str]:
    """Build one config-isolated CRIU argv from the typed authority mode."""

    if not args:
        raise RuntimeError("CRIU command requires an action")
    mode = criu_execution_mode()
    action, *options = args
    if mode is CriuExecutionMode.CAPABILITY and action not in CRIU_INFORMATION_ACTIONS:
        options.insert(0, "--unprivileged")
    return [*criu_launcher_prefix(mode=mode), *environment, criu_executable(), action, *options]
