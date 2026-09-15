"""Fail-fast authority and round-trip checks for developer CRIU maintenance."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from hol_workbench.criu_invocation import criu_command, criu_executable, criu_execution_mode
from hol_workbench.runtime_config import CriuExecutionMode

SUDO_EXECUTABLE: Final = "/usr/bin/sudo"
GETCAP_EXECUTABLE: Final = "/usr/sbin/getcap"
SLEEP_EXECUTABLE: Final = "/usr/bin/sleep"
REQUIRED_CAPABILITIES: Final = frozenset(
    {
        "cap_sys_ptrace",
        # CAP_SYS_ADMIN is CRIU's pre-5.9-compatible alternative to
        # CAP_CHECKPOINT_RESTORE. Workbench also needs it to rejoin the guest
        # control-plane time namespace so /proc start ticks remain comparable.
        "cap_sys_admin",
    }
)
CAPABILITY_RECORD_RE: Final = re.compile(r"((?:cap_[a-z0-9_]+,?)+)[=+]([eip]+)")


class CriuMaintenancePreflightError(RuntimeError):
    """Raised before shelf allocation or HOL startup when CRIU is unusable."""


@dataclass(frozen=True)
class CriuMaintenanceReadiness:
    mode: CriuExecutionMode
    executable: Path
    functional_probe: str

    def record(self) -> dict[str, str]:
        return {
            "mode": self.mode.value,
            "executable": str(self.executable),
            "functional_probe": self.functional_probe,
        }

    def json(self) -> str:
        return json.dumps(self.record(), sort_keys=True)


def parse_effective_file_capabilities(output: str) -> frozenset[str]:
    effective: set[str] = set()
    for match in CAPABILITY_RECORD_RE.finditer(output.lower()):
        names, flags = match.groups()
        if "e" in flags:
            effective.update(name for name in names.split(",") if name)
    return frozenset(effective)


def _bounded_output(completed: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())
    lines = output.splitlines()
    return " | ".join(lines[-8:]) if lines else "no diagnostic output"


def _bounded_log(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return " | ".join(lines[-12:])


def _root_owned_coordinate(path: Path) -> None:
    current = path
    while True:
        metadata = current.stat()
        if metadata.st_uid != 0:
            raise CriuMaintenancePreflightError(
                f"capability CRIU coordinate must be root-owned: {current} is owned by uid {metadata.st_uid}"
            )
        if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise CriuMaintenancePreflightError(
                f"capability CRIU coordinate must not be group/other writable: {current}"
            )
        if current.parent == current:
            return
        current = current.parent


def _effective_file_capabilities(executable: Path) -> frozenset[str]:
    getcap = Path(GETCAP_EXECUTABLE)
    if not getcap.is_file() or not os.access(getcap, os.X_OK):
        raise CriuMaintenancePreflightError(
            f"capability inspection tool is missing: {GETCAP_EXECUTABLE}; install Linux package libcap2-bin"
        )
    try:
        completed = subprocess.run(
            [GETCAP_EXECUTABLE, "-n", str(executable)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CriuMaintenancePreflightError(f"cannot inspect CRIU file capabilities: {exc}") from exc
    if completed.returncode != 0:
        raise CriuMaintenancePreflightError(
            f"cannot inspect CRIU file capabilities: exit {completed.returncode}: {_bounded_output(completed)}"
        )
    return parse_effective_file_capabilities(completed.stdout)


def validate_capability_executable(executable: Path) -> None:
    _root_owned_coordinate(executable)
    capabilities = _effective_file_capabilities(executable)
    missing = sorted(REQUIRED_CAPABILITIES - capabilities)
    if missing:
        raise CriuMaintenancePreflightError(
            "capability CRIU is missing effective file capabilities "
            f"{','.join(missing)}: {executable}; run dev/provision-criu-capability once as root"
        )


def _run_probe_command(command: list[str], *, phase: str, log: Path) -> None:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CriuMaintenancePreflightError(f"CRIU {phase} probe could not run: {exc}") from exc
    if completed.returncode != 0:
        diagnostic = _bounded_output(completed)
        log_tail = _bounded_log(log)
        if log_tail:
            diagnostic = f"{diagnostic}; {log.name}: {log_tail}"
        raise CriuMaintenancePreflightError(f"CRIU {phase} probe failed with exit {completed.returncode}: {diagnostic}")


def _kill_if_present(pid: int | None) -> None:
    if pid is None or pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise CriuMaintenancePreflightError(f"cannot clean CRIU probe process {pid}: {exc}") from exc


def run_capability_round_trip_probe() -> None:
    """Dump and restore one inert process using the same authority shape as shelves."""

    if not Path(SLEEP_EXECUTABLE).is_file():
        raise CriuMaintenancePreflightError(f"CRIU probe executable is missing: {SLEEP_EXECUTABLE}")
    sleeper: subprocess.Popen[bytes] | None = None
    restored_pid: int | None = None
    with TemporaryDirectory(prefix="hol-workbench-criu-probe-") as raw_root:
        root = Path(raw_root)
        image_dir = root / "image"
        image_dir.mkdir()
        pidfile = root / "restore.pid"
        try:
            sleeper = subprocess.Popen(
                [SLEEP_EXECUTABLE, "300"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _run_probe_command(
                criu_command(
                    "dump",
                    "--no-default-config",
                    "--shell-job",
                    "-t",
                    str(sleeper.pid),
                    "-D",
                    str(image_dir),
                    "-o",
                    "dump.log",
                ),
                phase="dump",
                log=image_dir / "dump.log",
            )
            try:
                sleeper.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise CriuMaintenancePreflightError("CRIU dump probe did not terminate its source process") from exc
            _run_probe_command(
                criu_command(
                    "restore",
                    "--no-default-config",
                    "--shell-job",
                    "--join-ns",
                    "time:1",
                    "-D",
                    str(image_dir),
                    "-d",
                    "--leave-stopped",
                    "--pidfile",
                    str(pidfile),
                    "-o",
                    "restore.log",
                ),
                phase="restore",
                log=image_dir / "restore.log",
            )
            try:
                restored_pid = int(pidfile.read_text(encoding="utf-8").strip())
            except (OSError, ValueError) as exc:
                raise CriuMaintenancePreflightError(f"CRIU restore probe produced no valid pidfile: {exc}") from exc
            if not Path(f"/proc/{restored_pid}").is_dir():
                raise CriuMaintenancePreflightError(
                    f"CRIU restore probe reported process {restored_pid}, but it is not live"
                )
        finally:
            _kill_if_present(restored_pid)
            if sleeper is not None and sleeper.poll() is None:
                _kill_if_present(sleeper.pid)
                with suppress(subprocess.TimeoutExpired):
                    sleeper.wait(timeout=5)
            if restored_pid is not None:
                deadline = time.monotonic() + 2
                while Path(f"/proc/{restored_pid}").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                if Path(f"/proc/{restored_pid}").exists():
                    raise CriuMaintenancePreflightError(
                        f"CRIU round-trip probe could not verify cleanup of restored process {restored_pid}"
                    )


def _require_sudo_timestamp() -> None:
    try:
        completed = subprocess.run(
            [SUDO_EXECUTABLE, "-n", "-v"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CriuMaintenancePreflightError(f"cannot validate legacy sudo CRIU authority: {exc}") from exc
    if completed.returncode != 0:
        raise CriuMaintenancePreflightError(
            "criu_mode=sudo has no noninteractive authorization; run sudo -v for this maintenance shell, "
            "or provision criu_mode=capability once so future builds need no sudo timestamp"
        )


def require_criu_maintenance_ready(*, functional_probe: bool = True) -> CriuMaintenanceReadiness:
    executable = Path(criu_executable())
    mode = criu_execution_mode()
    if mode is CriuExecutionMode.SUDO:
        _require_sudo_timestamp()
        return CriuMaintenanceReadiness(mode=mode, executable=executable, functional_probe="not_applicable")
    validate_capability_executable(executable)
    if functional_probe:
        run_capability_round_trip_probe()
    return CriuMaintenanceReadiness(
        mode=mode,
        executable=executable,
        functional_probe="passed" if functional_probe else "skipped",
    )
