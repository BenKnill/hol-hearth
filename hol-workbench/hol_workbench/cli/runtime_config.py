"""Developer CLI for the host-local Linux runtime contract."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from hol_workbench.criu_invocation import CRIU_BIN_ENVIRONMENT
from hol_workbench.runtime_config import (
    CRIU_MODE_ENVIRONMENT,
    CriuExecutionMode,
    RuntimeConfig,
    RuntimeConfigError,
    load_runtime_config,
    parse_criu_execution_mode,
    runtime_config_path,
    write_runtime_config,
)
from hol_workbench.ubuntu_runtime_layout import CRIU_SHELF_ROOT_ENV, ORB_HOLDIR_ENV


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Configure this Linux host for HOL Light Workbench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    configure = subparsers.add_parser("configure", help="atomically write the host-local runtime TOML")
    _add_runtime_arguments(configure)
    configure.add_argument("--if-missing", action="store_true")
    validate = subparsers.add_parser("validate", help="validate candidate host paths without writing anything")
    _add_runtime_arguments(validate)
    subparsers.add_parser("show", help="validate and print the effective host-local runtime TOML")
    return parser


def _add_runtime_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hol-light-dir", type=Path)
    parser.add_argument("--criu-shelf-root", type=Path)
    parser.add_argument("--criu-bin", type=Path)
    parser.add_argument(
        "--criu-mode",
        choices=tuple(mode.value for mode in CriuExecutionMode),
        help="sudo (legacy interactive maintenance) or capability (ordinary-user CRIU)",
    )


def _home(environment: Mapping[str, str]) -> Path:
    raw = environment.get("HOME") or str(Path.home())
    home = Path(raw).expanduser()
    if not home.is_absolute():
        raise RuntimeConfigError(f"HOME must be an absolute Linux path; got {raw!r}")
    return home.resolve()


def _existing_config(environment: Mapping[str, str]) -> RuntimeConfig | None:
    if not runtime_config_path(environment).exists():
        return None
    return load_runtime_config(environment)


def _selected_path(
    explicit: Path | None,
    *,
    variable: str,
    previous: Path | None,
    fallback: Path,
    environment: Mapping[str, str],
) -> Path:
    if explicit is not None:
        return explicit
    configured = environment.get(variable)
    if configured:
        return Path(configured)
    if previous is not None:
        return previous
    return fallback


def _validate_hol_light(path: Path) -> None:
    if not path.is_dir() or not (path / "hol.ml").is_file():
        raise RuntimeConfigError(
            f"HOL Light checkout is not usable at {path}; pass --hol-light-dir for a directory containing hol.ml"
        )


def _validate_criu(path: Path) -> None:
    if not path.is_absolute():
        raise RuntimeConfigError(f"criu_bin must be an absolute Linux path; got {str(path)!r}")
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeConfigError(f"CRIU executable is not an executable regular file: {path}")


def _candidate(
    args: argparse.Namespace,
    environment: Mapping[str, str],
    *,
    reuse_existing: bool,
) -> tuple[RuntimeConfig, bool]:
    config_path = runtime_config_path(environment)
    if reuse_existing and config_path.exists():
        config = load_runtime_config(environment)
        _validate_hol_light(config.hol_light_dir)
        _validate_criu(config.criu_bin)
        return config, True

    previous = _existing_config(environment)
    home = _home(environment)
    hol_light_dir = (
        _selected_path(
            args.hol_light_dir,
            variable=ORB_HOLDIR_ENV,
            previous=previous.hol_light_dir if previous is not None else None,
            fallback=home / "src" / "hol-light",
            environment=environment,
        )
        .expanduser()
        .resolve()
    )
    criu_shelf_root = (
        _selected_path(
            args.criu_shelf_root,
            variable=CRIU_SHELF_ROOT_ENV,
            previous=previous.criu_shelf_root if previous is not None else None,
            fallback=home / ".cache" / "hol-light-workbench" / "criu",
            environment=environment,
        )
        .expanduser()
        .resolve()
    )

    raw_criu = args.criu_bin
    if raw_criu is None and environment.get(CRIU_BIN_ENVIRONMENT):
        raw_criu = Path(environment[CRIU_BIN_ENVIRONMENT])
    if raw_criu is None and previous is not None:
        raw_criu = previous.criu_bin
    if raw_criu is None and (detected_criu := shutil.which("criu", path=environment.get("PATH", ""))) is not None:
        raw_criu = Path(detected_criu)
    if raw_criu is None:
        raise RuntimeConfigError("CRIU executable is unavailable; pass --criu-bin /absolute/path/to/criu")
    criu_bin = raw_criu.expanduser().resolve()

    raw_criu_mode = args.criu_mode
    if raw_criu_mode is None:
        raw_criu_mode = environment.get(CRIU_MODE_ENVIRONMENT)
    if raw_criu_mode is None and previous is not None:
        raw_criu_mode = previous.criu_mode.value
    if raw_criu_mode is None:
        raw_criu_mode = CriuExecutionMode.SUDO.value
    criu_mode = parse_criu_execution_mode(raw_criu_mode, label="criu_mode")

    _validate_hol_light(hol_light_dir)
    _validate_criu(criu_bin)
    return (
        RuntimeConfig(
            path=config_path,
            hol_light_dir=hol_light_dir,
            criu_shelf_root=criu_shelf_root,
            criu_bin=criu_bin,
            criu_mode=criu_mode,
        ),
        False,
    )


def _write_candidate(candidate: RuntimeConfig, environment: Mapping[str, str]) -> RuntimeConfig:
    return write_runtime_config(
        hol_light_dir=candidate.hol_light_dir,
        criu_shelf_root=candidate.criu_shelf_root,
        criu_bin=candidate.criu_bin,
        criu_mode=candidate.criu_mode,
        environment=environment,
    )


def main(argv: Sequence[str] | None = None, *, environment: Mapping[str, str] | None = None) -> int:
    active = os.environ if environment is None else environment
    args = _parser().parse_args(argv)
    try:
        if args.command == "configure":
            candidate, reused = _candidate(args, active, reuse_existing=args.if_missing)
            config = candidate if reused else _write_candidate(candidate, active)
        elif args.command == "validate":
            config, _reused = _candidate(args, active, reuse_existing=False)
        else:
            config = load_runtime_config(active)
    except RuntimeConfigError as exc:
        print(f"configure-runtime: {exc}", file=sys.stderr)
        return 2
    record: dict[str, object] = {}
    record.update(config.record())
    record.update(
        {
            "mode": args.command,
            "config_exists": config.path.is_file(),
            "hol_light_usable": config.hol_light_dir.is_dir() and (config.hol_light_dir / "hol.ml").is_file(),
            "criu_shelf_exists": config.criu_shelf_root.is_dir(),
            "criu_executable_usable": config.criu_bin.is_file() and os.access(config.criu_bin, os.X_OK),
        }
    )
    print(json.dumps(record, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
