"""Typed, host-local Linux runtime configuration.

The configuration is intentionally outside the checkout.  A clone contains
policy and parsers; each Linux host supplies its own absolute runtime paths.
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

RUNTIME_CONFIG_ENV: Final = "HOL_WORKBENCH_RUNTIME_CONFIG"
RUNTIME_CONFIG_SCHEMA: Final = "hol-workbench.ubuntu-runtime.v1"
RUNTIME_CONFIG_RELATIVE_PATH: Final = Path("hol-hearth/runtime.toml")
CRIU_MODE_ENVIRONMENT: Final = "HOL_WORKBENCH_CRIU_MODE"


class CriuExecutionMode(StrEnum):
    """How the configured CRIU executable receives maintenance authority."""

    SUDO = "sudo"
    CAPABILITY = "capability"


class RuntimeConfigError(ValueError):
    """Raised when host-local runtime configuration is absent or invalid."""


@dataclass(frozen=True)
class RuntimeConfig:
    path: Path
    hol_light_dir: Path
    criu_shelf_root: Path
    criu_bin: Path
    criu_mode: CriuExecutionMode

    def record(self) -> dict[str, str]:
        return {
            "schema": RUNTIME_CONFIG_SCHEMA,
            "path": str(self.path),
            "hol_light_dir": str(self.hol_light_dir),
            "criu_shelf_root": str(self.criu_shelf_root),
            "criu_bin": str(self.criu_bin),
            "criu_mode": self.criu_mode.value,
        }


def setup_command() -> str:
    return (
        "./hearth setup (new environment); "
        "see docs/setup.md for configuring an existing HOL/CRIU installation"
    )


def _absolute_path(value: str, *, label: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise RuntimeConfigError(f"{label} must be an absolute Linux path; got {value!r}")
    return candidate.resolve()


def runtime_config_path(environment: Mapping[str, str] | None = None) -> Path:
    active = os.environ if environment is None else environment
    override = active.get(RUNTIME_CONFIG_ENV)
    if override:
        return _absolute_path(override, label=RUNTIME_CONFIG_ENV)

    xdg_root = active.get("XDG_CONFIG_HOME")
    if xdg_root:
        base = _absolute_path(xdg_root, label="XDG_CONFIG_HOME")
    else:
        home = active.get("HOME") or str(Path.home())
        base = _absolute_path(home, label="HOME") / ".config"
    return (base / RUNTIME_CONFIG_RELATIVE_PATH).resolve()


def _runtime_table(payload: object, *, path: Path) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise RuntimeConfigError(f"runtime config must contain one TOML table: {path}")
    document = cast(dict[str, object], payload)
    unknown_top_level = set(document) - {"schema", "runtime"}
    if unknown_top_level:
        raise RuntimeConfigError(f"runtime config has unknown top-level keys {sorted(unknown_top_level)}: {path}")
    if document.get("schema") != RUNTIME_CONFIG_SCHEMA:
        raise RuntimeConfigError(f"runtime config schema must be {RUNTIME_CONFIG_SCHEMA!r}: {path}")
    runtime = document.get("runtime")
    if not isinstance(runtime, dict):
        raise RuntimeConfigError(f"runtime config is missing [runtime]: {path}")
    table = cast(dict[str, object], runtime)
    unknown_runtime = set(table) - {"hol_light_dir", "criu_shelf_root", "criu_bin", "criu_mode"}
    if unknown_runtime:
        raise RuntimeConfigError(f"runtime config has unknown [runtime] keys {sorted(unknown_runtime)}: {path}")
    return table


def _required_string(table: Mapping[str, object], key: str, *, path: Path) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeConfigError(f"runtime config [runtime].{key} must be a non-empty string: {path}")
    return value


def parse_criu_execution_mode(value: object, *, label: str) -> CriuExecutionMode:
    if not isinstance(value, str) or not value:
        raise RuntimeConfigError(f"{label} must be one of: sudo, capability")
    try:
        return CriuExecutionMode(value)
    except ValueError as exc:
        raise RuntimeConfigError(f"{label} must be one of: sudo, capability; got {value!r}") from exc


def load_runtime_config(environment: Mapping[str, str] | None = None) -> RuntimeConfig:
    path = runtime_config_path(environment)
    try:
        with path.open("rb") as handle:
            payload: object = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise RuntimeConfigError(
            f"Linux runtime config is missing: {path}; configure this host once with: {setup_command()}"
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeConfigError(f"cannot read Linux runtime config {path}: {exc}") from exc

    table = _runtime_table(payload, path=path)
    hol_light_dir = _absolute_path(
        _required_string(table, "hol_light_dir", path=path),
        label=f"{path} [runtime].hol_light_dir",
    )
    criu_shelf_root = _absolute_path(
        _required_string(table, "criu_shelf_root", path=path),
        label=f"{path} [runtime].criu_shelf_root",
    )
    criu_bin = _absolute_path(
        _required_string(table, "criu_bin", path=path),
        label=f"{path} [runtime].criu_bin",
    )
    # Version-one configs predate explicit privilege ownership. Preserve their
    # exact behavior until a maintainer deliberately selects capability mode.
    criu_mode = parse_criu_execution_mode(
        table.get("criu_mode", CriuExecutionMode.SUDO.value),
        label=f"{path} [runtime].criu_mode",
    )
    return RuntimeConfig(
        path=path,
        hol_light_dir=hol_light_dir,
        criu_shelf_root=criu_shelf_root,
        criu_bin=criu_bin,
        criu_mode=criu_mode,
    )


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_runtime_config(
    *,
    hol_light_dir: Path,
    criu_shelf_root: Path,
    criu_bin: Path,
    criu_mode: CriuExecutionMode | str = CriuExecutionMode.SUDO,
    environment: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    path = runtime_config_path(environment)
    hol_path = _absolute_path(str(hol_light_dir), label="hol_light_dir")
    shelf_path = _absolute_path(str(criu_shelf_root), label="criu_shelf_root")
    criu_path = _absolute_path(str(criu_bin), label="criu_bin")
    selected_criu_mode = parse_criu_execution_mode(
        criu_mode.value if isinstance(criu_mode, CriuExecutionMode) else criu_mode,
        label="criu_mode",
    )
    lines = [
        f"schema = {_toml_string(RUNTIME_CONFIG_SCHEMA)}",
        "",
        "[runtime]",
        f"hol_light_dir = {_toml_string(str(hol_path))}",
        f"criu_shelf_root = {_toml_string(str(shelf_path))}",
        f"criu_bin = {_toml_string(str(criu_path))}",
        f"criu_mode = {_toml_string(selected_criu_mode.value)}",
    ]
    text = "\n".join(lines) + "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    return load_runtime_config(environment)
