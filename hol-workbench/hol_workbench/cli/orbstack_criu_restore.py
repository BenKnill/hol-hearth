#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hol_workbench import criu_lazy_pages_dispatch, criu_restore_preflight, criu_snapshot_compat
from hol_workbench.criu_invocation import criu_command, criu_execution_mode
from hol_workbench.criu_pool_restore import (
    CRIU_GUEST_CONTROL_TIME_NAMESPACE_OPTIONS,
    PoolAuthorityError,
    RestoredPoolAuthority,
    RestoredProfileAuthority,
    begin_restored_pool_transaction,
    demote_stale_ready_pool_for_restore,
    publish_restored_pool_ready,
    record_restored_pool_launcher,
    reset_root_collision_restore,
    restored_pool_lock,
    restored_pool_session_json,
    restored_profile_authority,
    select_restored_pool_authority,
    stage_restored_pool_metadata,
)
from hol_workbench.criu_restore_launcher import restore_signal_guard, run_restore_launcher
from hol_workbench.criu_restore_transaction import (
    TERMINAL_PROCESS_STATES,
    capture_authoritative_tree,
    capture_recorded_processes,
    process_state,
    read_recorded_pool_processes,
    read_restored_root_pid,
    resume_restored_processes,
    terminate_restored_tree,
    tree_capture_problems,
    tree_receipt_records,
    validate_restored_tree,
)
from hol_workbench.criu_snapshot_admission import StaticSnapshotAdmissionDecision
from hol_workbench.criu_snapshot_compat import (
    validate_snapshot_manifest,
    verify_static_snapshot_admission_decision,
)
from hol_workbench.jsonio import atomic_write_json
from hol_workbench.process_groups import process_birth_identity, process_group_is_alive, process_start_ticks
from hol_workbench.runtime_config import CriuExecutionMode

EXPECTED_RESTORE_ERRORS = (OSError, json.JSONDecodeError, RuntimeError, TypeError, ValueError)
ROOT_COLLISION_RE = re.compile(r"Can't fork for ([1-9][0-9]*): File exists")
RESTORE_OUTPUT_DIRECTORY_SUFFIX = ".outputs"
ALLOWED_EXTRA_RESTORE_OPTIONS = frozenset({"--skip-file-rwx-check"})


class RestoreOutputAuthorityError(RuntimeError):
    """A CRIU-created output is not confined to its admitted descriptors."""


def _file_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _safe_restore_leaf(name: str, *, label: str, suffix: str) -> str:
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or Path(name).name != name
        or not name.startswith("restore-")
        or not name.endswith(suffix)
    ):
        raise RestoreOutputAuthorityError(f"{label} is not one supported restore artifact leaf: {name!r}")
    return name


def _open_exclusive_output_file(image_fd: int, name: str, *, label: str) -> tuple[int, tuple[int, int]]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, 0o600, dir_fd=image_fd)
    except OSError as exc:
        raise RestoreOutputAuthorityError(f"cannot exclusively reserve {label} {name!r}: {exc}") from exc
    observed = os.fstat(fd)
    if not stat.S_ISREG(observed.st_mode):
        os.close(fd)
        raise RestoreOutputAuthorityError(f"reserved {label} is not a regular file: {name!r}")
    return fd, _file_identity(observed)


def _sudo_output_authority_command(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["sudo", "-n", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RestoreOutputAuthorityError(f"cannot reserve root-owned CRIU restore output with sudo: {exc}") from exc


def _unlink_current_output_file(image_fd: int, name: str, identity: tuple[int, int]) -> None:
    with suppress(OSError):
        observed = os.stat(name, dir_fd=image_fd, follow_symlinks=False)
        if stat.S_ISREG(observed.st_mode) and _file_identity(observed) == identity:
            os.unlink(name, dir_fd=image_fd)


def _open_exclusive_root_output_file(
    image_fd: int,
    name: str,
    *,
    label: str,
) -> tuple[int, tuple[int, int], tuple[int, int, int]]:
    """Atomically create one mode-appropriate restore output and retain its anchor."""

    if criu_execution_mode() is CriuExecutionMode.CAPABILITY:
        fd, identity = _open_exclusive_output_file(image_fd, name, label=label)
        observed = os.fstat(fd)
        owner = (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode))
        if observed.st_uid != os.geteuid() or owner[2] != 0o600 or observed.st_nlink != 1:
            _unlink_current_output_file(image_fd, name, identity)
            os.close(fd)
            raise RestoreOutputAuthorityError(
                f"capability-mode {label} is not owned by the current user, mode 0600, with one link"
            )
        return fd, identity, owner
    bound_image = Path(f"/proc/{os.getpid()}/fd/{image_fd}")
    created = _sudo_output_authority_command(
        "/usr/bin/mktemp",
        f"--tmpdir={bound_image}",
        f".{name}.XXXXXXXX",
    ).stdout.strip()
    temporary = Path(created)
    expected_prefix = f".{name}."
    if (
        not created
        or "\n" in created
        or temporary.parent != bound_image
        or not temporary.name.startswith(expected_prefix)
        or len(temporary.name) != len(expected_prefix) + 8
    ):
        raise RestoreOutputAuthorityError(f"sudo returned an invalid temporary {label} path")
    fd = -1
    identity = (-1, -1)
    linked = False
    try:
        fd = os.open(
            temporary.name,
            os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=image_fd,
        )
        observed = os.fstat(fd)
        identity = _file_identity(observed)
        owner = (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode))
        if not stat.S_ISREG(observed.st_mode) or owner != (0, 0, 0o600) or observed.st_nlink != 1:
            raise RestoreOutputAuthorityError(f"temporary {label} is not root:root mode 0600 with one link")
        _sudo_output_authority_command(
            "/usr/bin/ln",
            "-L",
            "-T",
            "--",
            str(Path(f"/proc/{os.getpid()}/fd/{fd}")),
            str(bound_image / name),
        )
        linked = True
        linked_stat = os.stat(name, dir_fd=image_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(linked_stat.st_mode)
            or _file_identity(linked_stat) != identity
            or (linked_stat.st_uid, linked_stat.st_gid, stat.S_IMODE(linked_stat.st_mode)) != owner
        ):
            raise RestoreOutputAuthorityError(f"reserved {label} authority changed while linking its durable name")
        _sudo_output_authority_command("/usr/bin/rm", "-f", "--", str(temporary))
        final = os.fstat(fd)
        if _file_identity(final) != identity or final.st_nlink != 1:
            raise RestoreOutputAuthorityError(f"reserved {label} retained an unexpected namespace alias")
        return fd, identity, owner
    except BaseException:
        if linked:
            _unlink_current_output_file(image_fd, name, identity)
        if fd >= 0:
            os.close(fd)
        raise
    finally:
        with suppress(RestoreOutputAuthorityError):
            _sudo_output_authority_command("/usr/bin/rm", "-f", "--", str(temporary))


def _sudo_output_directory_command(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["sudo", "-n", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RestoreOutputAuthorityError(f"cannot confine CRIU restore output directory with sudo: {exc}") from exc


def _confine_restore_output_directory(work_fd: int) -> tuple[int, int, int]:
    """Make one already-open empty directory mode-appropriate before publication."""

    if criu_execution_mode() is CriuExecutionMode.CAPABILITY:
        try:
            os.fchmod(work_fd, 0o700)
            observed = os.fstat(work_fd)
            listing = os.listdir(work_fd)
        except OSError as exc:
            raise RestoreOutputAuthorityError(
                f"cannot confine capability-mode CRIU restore output directory: {exc}"
            ) from exc
        if (
            not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != os.geteuid()
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise RestoreOutputAuthorityError(
                "capability-mode CRIU restore output directory is not owned by the current user at mode 0700"
            )
        if listing:
            raise RestoreOutputAuthorityError(
                "capability-mode CRIU restore output directory was not empty after confinement"
            )
        return observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode)
    bound = f"/proc/{os.getpid()}/fd/{work_fd}"
    _sudo_output_directory_command("/usr/bin/chown", "0:0", "--", bound)
    _sudo_output_directory_command("/usr/bin/chmod", "0700", "--", bound)
    observed = os.fstat(work_fd)
    if (
        not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != 0
        or observed.st_gid != 0
        or stat.S_IMODE(observed.st_mode) != 0o700
    ):
        raise RestoreOutputAuthorityError("CRIU restore output directory is not root:root mode 0700")
    listing = _sudo_output_directory_command(
        "/usr/bin/find",
        "-H",
        bound,
        "-mindepth",
        "1",
        "-maxdepth",
        "1",
        "-printf",
        "%f\\n",
    )
    if listing.stdout:
        raise RestoreOutputAuthorityError("CRIU restore output directory was not empty after confinement")
    return observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode)


def _validate_restore_options(options: list[str]) -> None:
    for option in options:
        if option not in ALLOWED_EXTRA_RESTORE_OPTIONS:
            raise RestoreOutputAuthorityError(
                f"CRIU restore option is outside the descriptor-confined allowlist and is forbidden: {option}"
            )


@dataclass(frozen=True)
class RestoreOutputAuthority:
    image_dir: Path
    durable_image_dir: Path
    image_fd: int
    log_name: str
    log_fd: int
    log_identity: tuple[int, int]
    log_owner: tuple[int, int, int]
    work_name: str
    work_fd: int
    work_identity: tuple[int, int]
    work_owner: tuple[int, int, int]
    pid_name: str

    @property
    def bound_log(self) -> Path:
        return Path(f"/proc/{os.getpid()}/fd/{self.log_fd}")

    @property
    def durable_log(self) -> Path:
        return self.durable_image_dir / self.log_name

    @property
    def bound_work_dir(self) -> Path:
        return Path(f"/proc/{os.getpid()}/fd/{self.work_fd}")

    @property
    def durable_work_dir(self) -> Path:
        return self.durable_image_dir / self.work_name

    @property
    def bound_pidfile(self) -> Path:
        return self.bound_work_dir / self.pid_name

    @property
    def durable_pidfile(self) -> Path:
        return self.durable_work_dir / self.pid_name

    def assert_current(self) -> None:
        try:
            log_stat = os.stat(self.log_name, dir_fd=self.image_fd, follow_symlinks=False)
            work_stat = os.stat(self.work_name, dir_fd=self.image_fd, follow_symlinks=False)
        except OSError as exc:
            raise RestoreOutputAuthorityError(f"reserved CRIU restore output namespace changed: {exc}") from exc
        held_log_stat = os.fstat(self.log_fd)
        if (
            not stat.S_ISREG(log_stat.st_mode)
            or not stat.S_ISREG(held_log_stat.st_mode)
            or _file_identity(log_stat) != self.log_identity
            or _file_identity(held_log_stat) != self.log_identity
            or (log_stat.st_uid, log_stat.st_gid, stat.S_IMODE(log_stat.st_mode)) != self.log_owner
            or (held_log_stat.st_uid, held_log_stat.st_gid, stat.S_IMODE(held_log_stat.st_mode)) != self.log_owner
            or log_stat.st_nlink != 1
            or held_log_stat.st_nlink != 1
        ):
            raise RestoreOutputAuthorityError("reserved CRIU restore log identity changed")
        if (
            not stat.S_ISDIR(work_stat.st_mode)
            or _file_identity(work_stat) != self.work_identity
            or (work_stat.st_uid, work_stat.st_gid, stat.S_IMODE(work_stat.st_mode)) != self.work_owner
        ):
            raise RestoreOutputAuthorityError("reserved CRIU restore output directory identity changed")

    def transaction_authority_record(self) -> dict[str, object]:
        return {
            "schema": "hol-workbench.criu-restore-output-authority.v1",
            "directory": str(self.durable_work_dir),
            "directory_identity": {"device": self.work_identity[0], "inode": self.work_identity[1]},
            "directory_owner": {
                "uid": self.work_owner[0],
                "gid": self.work_owner[1],
                "mode": f"{self.work_owner[2]:04o}",
            },
            "log": str(self.durable_log),
            "log_identity": {"device": self.log_identity[0], "inode": self.log_identity[1]},
            "log_owner": {
                "uid": self.log_owner[0],
                "gid": self.log_owner[1],
                "mode": f"{self.log_owner[2]:04o}",
            },
            "pidfile": str(self.durable_pidfile),
            "implicit_outputs": ["stats-restore"],
        }


def _remove_bound_output_directory(authority: RestoreOutputAuthority) -> None:
    """Delete only entries reached through the held directory, then its exact current name."""

    if authority.work_owner[:2] == (0, 0):
        bound = str(authority.bound_work_dir)
        with suppress(RestoreOutputAuthorityError):
            _sudo_output_directory_command(
                "/usr/bin/find",
                "-H",
                bound,
                "-mindepth",
                "1",
                "-delete",
            )
    else:
        with suppress(OSError):
            for name in os.listdir(authority.work_fd):
                observed = os.stat(name, dir_fd=authority.work_fd, follow_symlinks=False)
                if stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
                    os.unlink(name, dir_fd=authority.work_fd)
    try:
        observed = os.stat(authority.work_name, dir_fd=authority.image_fd, follow_symlinks=False)
    except OSError:
        return
    if stat.S_ISDIR(observed.st_mode) and _file_identity(observed) == authority.work_identity:
        with suppress(OSError):
            os.rmdir(authority.work_name, dir_fd=authority.image_fd)


@contextmanager
def prepared_restore_output_authority(
    *,
    image_dir: Path,
    durable_image_dir: Path,
    log: Path,
    pidfile: Path,
) -> Iterator[RestoreOutputAuthority]:
    """Reserve all explicit and implicit CRIU outputs before transaction publication."""

    log_name = _safe_restore_leaf(log.name, label="restore log", suffix=".log")
    pid_name = _safe_restore_leaf(pidfile.name, label="restore pidfile", suffix=".pid")
    work_name = _safe_restore_leaf(
        f"{pidfile.stem}{RESTORE_OUTPUT_DIRECTORY_SUFFIX}",
        label="restore output directory",
        suffix=RESTORE_OUTPUT_DIRECTORY_SUFFIX,
    )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    try:
        image_fd = os.open(image_dir, flags)
    except OSError as exc:
        raise RestoreOutputAuthorityError(f"cannot open descriptor-bound CRIU image directory: {exc}") from exc
    log_fd = -1
    work_fd = -1
    work_created = False
    authority: RestoreOutputAuthority | None = None
    try:
        log_fd, log_identity, log_owner = _open_exclusive_root_output_file(
            image_fd,
            log_name,
            label="restore log",
        )
        try:
            os.mkdir(work_name, 0o700, dir_fd=image_fd)
            work_created = True
        except OSError as exc:
            raise RestoreOutputAuthorityError(
                f"cannot exclusively reserve CRIU restore output directory {work_name!r}: {exc}"
            ) from exc
        try:
            work_fd = os.open(
                work_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=image_fd,
            )
        except OSError as exc:
            raise RestoreOutputAuthorityError(f"cannot bind CRIU restore output directory: {exc}") from exc
        work_owner = _confine_restore_output_directory(work_fd)
        authority = RestoreOutputAuthority(
            image_dir=image_dir,
            durable_image_dir=durable_image_dir,
            image_fd=image_fd,
            log_name=log_name,
            log_fd=log_fd,
            log_identity=log_identity,
            log_owner=log_owner,
            work_name=work_name,
            work_fd=work_fd,
            work_identity=_file_identity(os.fstat(work_fd)),
            work_owner=work_owner,
            pid_name=pid_name,
        )
        authority.assert_current()
        yield authority
    finally:
        if authority is None and work_created and work_fd >= 0:
            provisional = RestoreOutputAuthority(
                image_dir=image_dir,
                durable_image_dir=durable_image_dir,
                image_fd=image_fd,
                log_name=log_name,
                log_fd=log_fd,
                log_identity=_file_identity(os.fstat(log_fd)) if log_fd >= 0 else (-1, -1),
                log_owner=(
                    os.fstat(log_fd).st_uid,
                    os.fstat(log_fd).st_gid,
                    stat.S_IMODE(os.fstat(log_fd).st_mode),
                )
                if log_fd >= 0
                else (-1, -1, -1),
                work_name=work_name,
                work_fd=work_fd,
                work_identity=_file_identity(os.fstat(work_fd)),
                work_owner=(
                    os.fstat(work_fd).st_uid,
                    os.fstat(work_fd).st_gid,
                    stat.S_IMODE(os.fstat(work_fd).st_mode),
                ),
                pid_name=pid_name,
            )
            _remove_bound_output_directory(provisional)
        if authority is None and log_fd >= 0:
            with suppress(OSError):
                observed_log = os.stat(log_name, dir_fd=image_fd, follow_symlinks=False)
                if stat.S_ISREG(observed_log.st_mode) and _file_identity(observed_log) == _file_identity(
                    os.fstat(log_fd)
                ):
                    os.unlink(log_name, dir_fd=image_fd)
        if work_fd >= 0:
            os.close(work_fd)
        if log_fd >= 0:
            os.close(log_fd)
        os.close(image_fd)


def stamp() -> str:
    wall = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{wall}-{os.getpid()}-{time.monotonic_ns()}"


def _durable_image_artifact(
    *,
    image_dir: Path,
    durable_image_dir: Path,
    artifact: Path,
    label: str,
    suffix: str,
) -> Path:
    """Map one descriptor-bound artifact leaf to its durable namespace path."""

    if artifact.parent != image_dir:
        raise RuntimeError(f"{label} is outside the descriptor-bound CRIU image directory: {artifact}")
    if not artifact.name.startswith("restore-") or artifact.suffix != suffix:
        raise RuntimeError(f"{label} has an unsupported restore artifact name: {artifact.name}")
    if not durable_image_dir.is_absolute():
        raise RuntimeError(f"durable CRIU image directory is not absolute: {durable_image_dir}")
    return durable_image_dir / artifact.name


def _bound_recorded_image_artifact(
    *,
    image_dir: Path,
    durable_image_dir: Path,
    recorded: object,
    label: str,
    suffix: str,
) -> tuple[Path, Path]:
    """Rebind one durable metadata path beneath the already-open image fd."""

    if not isinstance(recorded, str) or not recorded:
        raise RuntimeError(f"restore transaction {label} is unavailable")
    durable = Path(recorded)
    if (
        not durable.is_absolute()
        or durable.parent != durable_image_dir
        or not durable.name.startswith("restore-")
        or durable.suffix != suffix
    ):
        raise RuntimeError(f"restore transaction {label} is outside the admitted CRIU image directory: {durable}")
    return image_dir / durable.name, durable


def _metadata_identity(raw: object, *, label: str) -> tuple[int, int]:
    if not isinstance(raw, dict):
        raise RuntimeError(f"restore transaction {label} identity is unavailable")
    device = raw.get("device")
    inode = raw.get("inode")
    if (
        isinstance(device, bool)
        or not isinstance(device, int)
        or device < 0
        or isinstance(inode, bool)
        or not isinstance(inode, int)
        or inode <= 0
    ):
        raise RuntimeError(f"restore transaction {label} identity is invalid")
    return device, inode


@contextmanager
def _bound_recorded_output_directory(
    *,
    image_dir: Path,
    durable_image_dir: Path,
    record: object,
) -> Iterator[tuple[Path, Path, Path, Path]]:
    """Reopen one transaction's confined CRIU work directory without following aliases."""

    if not isinstance(record, dict) or record.get("schema") != "hol-workbench.criu-restore-output-authority.v1":
        raise RuntimeError("restore transaction output authority is invalid")
    if record.get("implicit_outputs") != ["stats-restore"]:
        raise RuntimeError("restore transaction implicit output inventory is invalid")
    raw_directory = record.get("directory")
    if not isinstance(raw_directory, str) or not raw_directory:
        raise RuntimeError("restore transaction output directory is unavailable")
    durable = Path(raw_directory)
    if (
        not durable.is_absolute()
        or durable.parent != durable_image_dir
        or not durable.name.startswith("restore-")
        or durable.suffix != RESTORE_OUTPUT_DIRECTORY_SUFFIX
    ):
        raise RuntimeError(f"restore transaction output directory is outside the admitted CRIU image: {durable}")
    expected_identity = _metadata_identity(record.get("directory_identity"), label="output directory")
    raw_log = record.get("log")
    if not isinstance(raw_log, str) or not raw_log:
        raise RuntimeError("restore transaction output log is unavailable")
    durable_log = Path(raw_log)
    if (
        not durable_log.is_absolute()
        or durable_log.parent != durable_image_dir
        or not durable_log.name.startswith("restore-")
        or durable_log.suffix != ".log"
    ):
        raise RuntimeError("restore transaction output log is outside the admitted CRIU image")
    expected_log_identity = _metadata_identity(record.get("log_identity"), label="output log")
    log_owner = record.get("log_owner")
    if not isinstance(log_owner, dict):
        raise RuntimeError("restore transaction output log owner is invalid")
    expected_log_uid = log_owner.get("uid")
    expected_log_gid = log_owner.get("gid")
    expected_log_mode = log_owner.get("mode")
    log_owner_is_root = expected_log_uid == 0 and expected_log_gid == 0
    log_owner_is_current_user = expected_log_uid == os.geteuid() and isinstance(expected_log_gid, int)
    if not (log_owner_is_root or log_owner_is_current_user) or expected_log_mode != "0600":
        raise RuntimeError("restore transaction output log owner is invalid")
    owner = record.get("directory_owner")
    if not isinstance(owner, dict):
        raise RuntimeError("restore transaction output directory owner is invalid")
    expected_uid = owner.get("uid")
    expected_gid = owner.get("gid")
    expected_mode = owner.get("mode")
    directory_owner_is_root = expected_uid == 0 and expected_gid == 0
    directory_owner_is_current_user = expected_uid == os.geteuid() and isinstance(expected_gid, int)
    if not (directory_owner_is_root or directory_owner_is_current_user) or expected_mode != "0700":
        raise RuntimeError("restore transaction output directory owner is invalid")
    image_fd = os.open(image_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    work_fd = -1
    log_fd = -1
    try:
        work_fd = os.open(
            durable.name,
            os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=image_fd,
        )
        observed = os.fstat(work_fd)
        if (
            not stat.S_ISDIR(observed.st_mode)
            or _file_identity(observed) != expected_identity
            or observed.st_uid != expected_uid
            or observed.st_gid != expected_gid
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise RuntimeError("restore transaction output directory authority changed")
        log_fd = os.open(
            durable_log.name,
            os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=image_fd,
        )
        observed_log = os.fstat(log_fd)
        if (
            not stat.S_ISREG(observed_log.st_mode)
            or _file_identity(observed_log) != expected_log_identity
            or observed_log.st_uid != expected_log_uid
            or observed_log.st_gid != expected_log_gid
            or stat.S_IMODE(observed_log.st_mode) != 0o600
            or observed_log.st_nlink != 1
        ):
            raise RuntimeError("restore transaction output log authority changed")
        yield (
            Path(f"/proc/{os.getpid()}/fd/{work_fd}"),
            durable,
            Path(f"/proc/{os.getpid()}/fd/{log_fd}"),
            durable_log,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot bind restore transaction output directory: {exc}") from exc
    finally:
        if work_fd >= 0:
            os.close(work_fd)
        if log_fd >= 0:
            os.close(log_fd)
        os.close(image_fd)


def pid_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    state = process_state(pid)
    if state in TERMINAL_PROCESS_STATES:
        return False
    return not (state is None and Path("/proc").is_dir())


def pool_record_is_live(data: Mapping[str, object]) -> bool:
    """Check the process identities in one already-read pool record."""

    identities = (
        (data.get("worker_pid"), data.get("worker_identity")),
        (data.get("basis_pid"), data.get("basis_identity")),
    )
    return all(
        pid_is_alive(pid) and isinstance(expected_identity, str) and process_birth_identity(pid) == expected_identity
        for pid, expected_identity in identities
    )


def pool_is_live(pool: Path) -> bool:
    data = json.loads((pool / "pool.json").read_text(encoding="utf-8"))
    return isinstance(data, dict) and pool_record_is_live(data)


def restore_preflight_failure(pool: Path) -> str | None:
    data = json.loads((pool / "pool.json").read_text(encoding="utf-8"))
    pool_status = str(data.get("status") or "unknown")
    live = pool_is_live(pool)
    if pool_status == "ready":
        return None if live else "pool lifecycle status is ready but its recorded process identities are not live"
    if pool_status != "stopped":
        return f"pool lifecycle status is {pool_status}; only ready may be reused and only stopped may be restored"
    if live:
        return "pool lifecycle status is stopped but recorded process identities are still live"
    sessions = [item for item in data.get("sessions") or [] if isinstance(item, dict)]
    verified = bool(sessions) and all(
        item.get("status") == "stopped" and (item.get("stop_result") or {}).get("verified_quiescent") is True
        for item in sessions
    )
    if pool_status != "stopped" or not verified:
        return "pool has no verified quiescent stop receipt"
    return None


def print_preflight_failure(pool: Path, reason: str) -> None:
    print("status=failed")
    print("failing_phase=preflight_quiescence")
    print(f"pool={pool}")
    print(f"reason={reason}")
    print("retry_action=run stop-profile and resolve any stop_failed survivors before restore")


def print_pool_authority_failure(profile_root: Path, error: PoolAuthorityError) -> None:
    print("status=failed")
    print("failing_phase=pool_authority")
    print(f"profile_root={profile_root}")
    print(f"reason={error}")
    print("lifecycle_action=none")


def print_transaction_failure(
    pool: Path,
    *,
    phase: str,
    log: Path,
    error: BaseException,
    cleanup: dict,
    receipt: Path | None,
    receipt_error: str | None,
) -> None:
    final = cleanup.get("final_wait") or {}
    print("status=failed")
    print(f"failing_phase={phase}")
    print(f"pool={pool}")
    print(f"restore_log={log}")
    print(f"reason={type(error).__name__}: {error}")
    if isinstance(error, subprocess.CalledProcessError):
        print(f"exit_status={error.returncode}")
    print(f"cleanup_status={cleanup.get('status') or 'unverified'}")
    print(f"surviving_pids={final.get('live_pids') or []}")
    print(f"surviving_pgids={final.get('live_pgids') or []}")
    print(f"restore_failure_receipt={receipt or '-'}")
    if receipt_error:
        print(f"restore_failure_receipt_error={receipt_error}")
    print("retry_action=run stop-profile to verify quiescence, then retry restore")


def _mark_pool_restore_failed(pool: Path, cleanup: dict) -> str | None:
    try:
        path = pool / "pool.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("pool metadata is not a JSON object")
        data["status"] = "restore_failed"
        data["updated_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["restore_failure_cleanup"] = cleanup
        sessions = data.get("sessions") or []
        if not isinstance(sessions, list):
            raise TypeError("pool sessions metadata is not a JSON array")
        session_paths: list[Path] = []
        for session in sessions:
            if isinstance(session, dict):
                session["status"] = "restore_failed"
                session["lease"] = None
                session["restore_failure_cleanup"] = cleanup
                session_path = restored_pool_session_json(pool, session)
                if session_path is not None:
                    session_paths.append(session_path)
        atomic_write_json(path, data)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    errors: list[str] = []
    for session_path in session_paths:
        if not session_path.is_file():
            continue
        try:
            session_data = json.loads(session_path.read_text(encoding="utf-8"))
            if not isinstance(session_data, dict):
                raise TypeError("session metadata is not a JSON object")
            session_data["status"] = "restore_failed"
            session_data["lease"] = None
            session_data["updated_utc"] = data["updated_utc"]
            session_data["restore_failure_cleanup"] = cleanup
            atomic_write_json(session_path, session_data)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            errors.append(f"{session_path}: {type(exc).__name__}: {exc}")
    return "; ".join(errors) or None


def _write_restore_failure_receipt(
    *,
    image_dir: Path,
    operation_stamp: str,
    pool: Path,
    display_pool: Path,
    phase: str,
    log: Path,
    durable_log: Path,
    error: BaseException,
    root_pid: int | None,
    tree: list[dict],
    cleanup: dict,
) -> tuple[Path | None, str | None]:
    status_error = _mark_pool_restore_failed(pool, cleanup)
    receipt = image_dir / f"restore-failed-{operation_stamp}.json"
    final = cleanup.get("final_wait") or {}
    data = {
        "schema": "hol-workbench.criu-restore-failure.v1",
        "status": "restore_failed",
        "phase": phase,
        "pool": str(display_pool),
        "restore_log": str(durable_log),
        "reason": f"{type(error).__name__}: {error}",
        "authoritative_root_pid": root_pid,
        "restored_processes": tree_receipt_records(tree),
        "cleanup_status": cleanup.get("status") or "unverified",
        "verified_quiescent": cleanup.get("verified_quiescent") is True,
        "ownership_capture_complete": cleanup.get("ownership_capture_complete") is True,
        "ownership_capture_problems": cleanup.get("ownership_capture_problems") or [],
        "terminations": cleanup.get("terminations") or [],
        "surviving_pids": final.get("live_pids") or [],
        "surviving_pgids": final.get("live_pgids") or [],
        "pool_status_error": status_error,
        "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        atomic_write_json(receipt, data)
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return receipt, None


def print_manifest_failure(profile_root: Path, error: RuntimeError) -> None:
    print("\n".join(criu_snapshot_compat.snapshot_manifest_failure_lines(profile_root, error)))


def _fallback_tree(recorded: dict[str, dict[str, int]]) -> list[dict]:
    try:
        captured = list(capture_recorded_processes(recorded).values())
        return captured if all(item.get("state") in {"T", "t"} for item in captured) else []
    except (OSError, RuntimeError, TypeError, ValueError):
        return []


def _unlink_pidfile(pidfile: Path) -> None:
    pidfile.unlink(missing_ok=True)


def _remove_pidfile(
    pidfile: Path,
    *,
    unlinker=_unlink_pidfile,
    run=subprocess.run,
) -> None:
    try:
        unlinker(pidfile)
        return
    except FileNotFoundError:
        return
    except OSError:
        pass
    try:
        run(
            ["sudo", "-n", "rm", "-f", "--", str(pidfile)],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot remove CRIU root pidfile {pidfile}") from exc
    if _pidfile_is_file(pidfile):
        raise RuntimeError(f"CRIU root pidfile still exists after removal: {pidfile}")


def _best_effort_remove_pidfile(pidfile: Path) -> None:
    with suppress(RuntimeError):
        _remove_pidfile(pidfile)


def _capture_pidfile_tree(pidfile: Path) -> tuple[int | None, list[dict]]:
    try:
        root_pid = read_restored_root_pid(pidfile)
        return root_pid, capture_authoritative_tree(root_pid)
    except EXPECTED_RESTORE_ERRORS:
        return None, []


def _bounded_root_owned_log(log: Path) -> str:
    completed = subprocess.run(
        ["sudo", "-n", "tail", "-c", "8192", "--", str(log)],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout


def _root_collision_retry_record(
    *,
    log: Path,
    durable_log: Path | None = None,
    pidfile: Path,
    recorded: dict[str, dict[str, int]],
    signals,
) -> dict | None:
    """Authorize one no-signal retry for an exact, now-vacant image-root collision."""

    if _pidfile_is_file(pidfile):
        return None
    try:
        text = _bounded_root_owned_log(log)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    matches = ROOT_COLLISION_RE.findall(text)
    error_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("Error (")]
    if len(matches) != 1 or len(error_lines) != 2 or "Restoring FAILED" not in error_lines[-1]:
        return None
    collision_pid = int(matches[0])
    worker = recorded.get("worker") or {}
    if worker.get("pid") != collision_pid or worker.get("pgid") != collision_pid:
        return None
    deadline = time.monotonic() + 5.0
    while pid_is_alive(collision_pid) or process_group_is_alive(collision_pid):
        signals.raise_if_interrupted()
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.05)
    return {
        "schema": "hol-workbench.criu-restore-retry.v1",
        "status": "authorized",
        "reason": "exact_root_pid_collision_became_vacant",
        "collision_pid": collision_pid,
        "first_restore_log": str(durable_log or log),
        "pidfile_absent": True,
        "signals_sent": False,
        "wait_bound_seconds": 5.0,
        "authorized_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _root_collision_retry_from_current_outputs(
    *,
    outputs: RestoreOutputAuthority,
    log: Path,
    durable_log: Path,
    pidfile: Path,
    recorded: dict[str, dict[str, int]],
    signals,
) -> dict | None:
    """Trust collision text only while its descriptor and durable name retain authority."""

    try:
        outputs.assert_current()
    except (OSError, RestoreOutputAuthorityError):
        return None
    return _root_collision_retry_record(
        log=log,
        durable_log=durable_log,
        pidfile=pidfile,
        recorded=recorded,
        signals=signals,
    )


def _merge_captured_trees(*trees: list[dict]) -> list[dict]:
    """Retain every known PID while preferring more complete later records."""
    by_pid: dict[int, dict] = {}
    for tree in trees:
        for candidate in tree:
            pid = candidate.get("pid")
            if not isinstance(pid, int) or pid <= 0:
                continue
            current = by_pid.get(pid)
            if current is None:
                by_pid[pid] = dict(candidate)
                continue
            merged = dict(current)
            for field in ("pgid", "start_ticks", "identity", "state"):
                if merged.get(field) is None and candidate.get(field) is not None:
                    merged[field] = candidate[field]
            current_children = current.get("children")
            candidate_children = candidate.get("children")
            if isinstance(candidate_children, list):
                known_children = set(current_children) if isinstance(current_children, list) else set()
                merged["children"] = sorted(known_children | set(candidate_children))
                if not candidate.get("children_error"):
                    merged.pop("children_error", None)
                elif not isinstance(current_children, list):
                    merged["children_error"] = candidate["children_error"]
            by_pid[pid] = merged
    return list(by_pid.values())


def _cleanup_restored_tree(
    *,
    tree: list[dict],
    root_pid: int | None,
    recorded: dict[str, dict[str, int]],
    force_recapture: bool = False,
) -> tuple[list[dict], dict]:
    captured = list(tree)
    forced_recapture_problems: list[str] = []
    if force_recapture and root_pid is not None:
        try:
            recaptured = capture_authoritative_tree(root_pid)
        except EXPECTED_RESTORE_ERRORS as exc:
            recaptured = []
            forced_recapture_problems.append(f"post-resume authoritative recapture failed: {type(exc).__name__}: {exc}")
        else:
            forced_recapture_problems.extend(
                tree_capture_problems(
                    authoritative_root_pid=root_pid,
                    recorded=recorded,
                    tree=recaptured,
                )
            )
        captured = _merge_captured_trees(captured, recaptured)
    capture_problems = tree_capture_problems(
        authoritative_root_pid=root_pid,
        recorded=recorded,
        tree=captured,
    )
    if capture_problems and root_pid is not None and not force_recapture:
        try:
            recaptured = capture_authoritative_tree(root_pid)
        except EXPECTED_RESTORE_ERRORS:
            recaptured = []
        captured = _merge_captured_trees(captured, recaptured)
        capture_problems = tree_capture_problems(
            authoritative_root_pid=root_pid,
            recorded=recorded,
            tree=captured,
        )
    if capture_problems:
        captured = _merge_captured_trees(captured, _fallback_tree(recorded))
        capture_problems = tree_capture_problems(
            authoritative_root_pid=root_pid,
            recorded=recorded,
            tree=captured,
        )
    cleanup_root_pid = root_pid or recorded.get("worker", {}).get("pid")
    if not captured:
        cleanup = {
            "status": "unverified",
            "verified_quiescent": False,
            "final_wait": {"live_pids": [], "live_pgids": []},
        }
    else:
        try:
            cleanup = terminate_restored_tree(captured, root_pid=cleanup_root_pid)
        except Exception as exc:
            cleanup = {
                "status": "unverified",
                "verified_quiescent": False,
                "error": f"{type(exc).__name__}: {exc}",
                "final_wait": {"live_pids": [], "live_pgids": []},
            }
    all_capture_problems = list(dict.fromkeys([*capture_problems, *forced_recapture_problems]))
    cleanup["ownership_capture_complete"] = not all_capture_problems
    cleanup["ownership_capture_problems"] = all_capture_problems
    if all_capture_problems:
        cleanup["quiescence_observation"] = cleanup.get("status")
        cleanup["status"] = "unverified"
        cleanup["verified_quiescent"] = False
    return captured, cleanup


def _handle_transaction_failure(
    *,
    image_dir: Path,
    durable_image_dir: Path,
    operation_stamp: str,
    pool: Path,
    display_pool: Path,
    phase: str,
    log: Path,
    durable_log: Path,
    error: BaseException,
    root_pid: int | None,
    tree: list[dict],
    recorded: dict[str, dict[str, int]],
) -> dict:
    captured, cleanup = _cleanup_restored_tree(
        tree=tree,
        root_pid=root_pid,
        recorded=recorded,
        force_recapture=phase in {"restored_resume", "restored_ready_publication", "restored_pidfile_cleanup"},
    )
    receipt, receipt_error = _write_restore_failure_receipt(
        image_dir=image_dir,
        operation_stamp=operation_stamp,
        pool=pool,
        display_pool=display_pool,
        phase=phase,
        log=log,
        durable_log=durable_log,
        error=error,
        root_pid=root_pid,
        tree=captured,
        cleanup=cleanup,
    )
    durable_receipt = durable_image_dir / receipt.name if receipt is not None else None
    print_transaction_failure(
        display_pool,
        phase=phase,
        log=durable_log,
        error=error,
        cleanup=cleanup,
        receipt=durable_receipt,
        receipt_error=receipt_error,
    )
    return cleanup


def _pidfile_is_file(pidfile: Path) -> bool:
    if pidfile.is_file():
        return True
    try:
        completed = subprocess.run(
            ["sudo", "-n", "/usr/bin/test", "-f", str(pidfile)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _recover_incomplete_restore_from_pidfile(
    *,
    image_dir: Path,
    recorded_image_dir: Path,
    pool: Path,
    reported_pool: Path,
    status: str,
    transaction: dict,
    pidfile: Path,
    recovery_log: tuple[Path, Path] | None = None,
) -> bool:
    state = transaction.get("state")
    launcher = transaction.get("launcher")
    if isinstance(launcher, dict):
        launcher_pid = launcher.get("pid")
        launcher_ticks = launcher.get("start_ticks")
        launcher_identity = launcher.get("identity")
        if (
            not isinstance(launcher_pid, int)
            or launcher_pid <= 0
            or not isinstance(launcher_ticks, int)
            or launcher_ticks <= 0
            or not isinstance(launcher_identity, str)
            or not launcher_identity
        ):
            raise RuntimeError("incomplete restore has an invalid launcher ownership record")
        deadline = time.monotonic() + 30.0
        while (
            process_start_ticks(launcher_pid) == launcher_ticks
            and process_birth_identity(launcher_pid) == launcher_identity
        ):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"owned CRIU restore launcher {launcher_pid} has not settled; refusing recovery")
            time.sleep(0.05)
    elif state != "prelaunch" and not _pidfile_is_file(pidfile):
        raise RuntimeError("incomplete restore has neither durable launcher ownership nor an authoritative pidfile")
    if status == "restore_failed" and not _pidfile_is_file(pidfile):
        return False
    recorded = read_recorded_pool_processes(pool)
    root_pid, tree = _capture_pidfile_tree(pidfile) if _pidfile_is_file(pidfile) else (None, [])
    log_value = transaction.get("restore_log")
    if recovery_log is not None:
        log, durable_log = recovery_log
    elif log_value is None:
        log = image_dir / "restore-interrupted.log"
        durable_log = recorded_image_dir / log.name
    else:
        log, durable_log = _bound_recorded_image_artifact(
            image_dir=image_dir,
            durable_image_dir=recorded_image_dir,
            recorded=log_value,
            label="restore log",
            suffix=".log",
        )
    error = RuntimeError(
        "recovering an incomplete CRIU restore transaction"
        if root_pid is not None
        else "incomplete CRIU restore has no readable authoritative pidfile yet"
    )
    cleanup = _handle_transaction_failure(
        image_dir=image_dir,
        durable_image_dir=recorded_image_dir,
        operation_stamp=f"recovery-{stamp()}",
        pool=pool,
        display_pool=reported_pool,
        phase="interrupted_restore_recovery",
        log=log,
        durable_log=durable_log,
        error=error,
        root_pid=root_pid,
        tree=tree,
        recorded=recorded,
    )
    if cleanup.get("verified_quiescent") is True:
        _remove_pidfile(pidfile)
    return True


def _recover_incomplete_restore(
    *,
    image_dir: Path,
    pool: Path,
    display_pool: Path | None = None,
    durable_image_dir: Path | None = None,
) -> bool:
    """Recover a prior controller death from its descriptor-bound CRIU pidfile anchor."""

    reported_pool = display_pool or pool
    recorded_image_dir = durable_image_dir or image_dir
    data = json.loads((pool / "pool.json").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("pool metadata is not a JSON object")
    status = str(data.get("status") or "unknown")
    transaction = data.get("restore_transaction")
    if not isinstance(transaction, dict):
        return False
    recorded_image = transaction.get("image_dir")
    if recorded_image is not None and recorded_image != str(recorded_image_dir):
        raise RuntimeError("restore transaction names a different durable CRIU image directory")
    recorded_image_identity = transaction.get("image_directory_identity")
    if recorded_image_identity is not None:
        expected_image_identity = _metadata_identity(recorded_image_identity, label="CRIU image directory")
        observed_image = image_dir.stat()
        if _file_identity(observed_image) != expected_image_identity:
            raise RuntimeError("restore transaction CRIU image directory identity changed")
    if status == "ready" and pool_is_live(pool):
        return False
    if status not in {"restoring", "restore_failed"}:
        return False
    raw_pidfile = transaction.get("pidfile")
    if raw_pidfile is None:
        return False
    output_authority = transaction.get("output_authority")
    if output_authority is None:
        raise RuntimeError(
            "incomplete restore predates descriptor-bound CRIU output authority; refusing pathname-based recovery"
        )
    if not isinstance(output_authority, dict):
        raise RuntimeError("restore transaction output authority is invalid")
    with _bound_recorded_output_directory(
        image_dir=image_dir,
        durable_image_dir=recorded_image_dir,
        record=output_authority,
    ) as (bound_output_dir, durable_output_dir, bound_log, durable_log):
        if not isinstance(raw_pidfile, str) or not raw_pidfile:
            raise RuntimeError("restore transaction pidfile is unavailable")
        durable_pidfile = Path(raw_pidfile)
        if (
            not durable_pidfile.is_absolute()
            or durable_pidfile.parent != durable_output_dir
            or not durable_pidfile.name.startswith("restore-")
            or durable_pidfile.suffix != ".pid"
        ):
            raise RuntimeError("restore transaction pidfile is outside its descriptor-bound output directory")
        if output_authority.get("pidfile") != str(durable_pidfile):
            raise RuntimeError("restore transaction pidfile disagrees with its output authority")
        if transaction.get("restore_log") != str(durable_log):
            raise RuntimeError("restore transaction log disagrees with its output authority")
        if (
            durable_output_dir.name != f"{durable_pidfile.stem}{RESTORE_OUTPUT_DIRECTORY_SUFFIX}"
            or durable_log.stem != durable_pidfile.stem
        ):
            raise RuntimeError("restore transaction output artifact names disagree")
        return _recover_incomplete_restore_from_pidfile(
            image_dir=image_dir,
            recorded_image_dir=recorded_image_dir,
            pool=pool,
            reported_pool=reported_pool,
            status=status,
            transaction=transaction,
            pidfile=bound_output_dir / durable_pidfile.name,
            recovery_log=(bound_log, durable_log),
        )


def recover_incomplete_restore_locked(
    *,
    image_dir: Path,
    pool: Path,
    durable_image_dir: Path | None = None,
) -> bool:
    """Recover one exact profile's restore transaction while its lifecycle and metadata locks are held."""
    return _recover_incomplete_restore(
        image_dir=image_dir,
        pool=pool,
        durable_image_dir=durable_image_dir,
    )


def execute_restore_transaction(
    args: argparse.Namespace,
    *,
    image_dir: Path,
    pool: Path,
    display_pool: Path,
    recorded: dict[str, dict[str, int]],
    operation_stamp: str,
    log: Path,
    pidfile: Path,
    restore_options: list[str],
    durable_image_dir: Path | None = None,
    allow_root_collision_retry: bool = True,
) -> int:
    recorded_image_dir = durable_image_dir or image_dir
    _durable_image_artifact(
        image_dir=image_dir,
        durable_image_dir=recorded_image_dir,
        artifact=log,
        label="restore log",
        suffix=".log",
    )
    _durable_image_artifact(
        image_dir=image_dir,
        durable_image_dir=recorded_image_dir,
        artifact=pidfile,
        label="restore pidfile",
        suffix=".pid",
    )
    try:
        _validate_restore_options(list(getattr(args, "criu_option", [])))
        with prepared_restore_output_authority(
            image_dir=image_dir,
            durable_image_dir=recorded_image_dir,
            log=log,
            pidfile=pidfile,
        ) as outputs:
            return _execute_restore_transaction_with_outputs(
                args,
                image_dir=image_dir,
                pool=pool,
                display_pool=display_pool,
                recorded=recorded,
                operation_stamp=operation_stamp,
                restore_options=restore_options,
                outputs=outputs,
                allow_root_collision_retry=allow_root_collision_retry,
            )
    except RestoreOutputAuthorityError as exc:
        print("status=failed")
        print("failing_phase=restore_output_authority")
        print(f"pool={display_pool}")
        print(f"reason={type(exc).__name__}: {exc}")
        print("lifecycle_action=none")
        return 1


def _execute_restore_transaction_with_outputs(
    args: argparse.Namespace,
    *,
    image_dir: Path,
    pool: Path,
    display_pool: Path,
    recorded: dict[str, dict[str, int]],
    operation_stamp: str,
    restore_options: list[str],
    outputs: RestoreOutputAuthority,
    allow_root_collision_retry: bool,
) -> int:
    recorded_image_dir = outputs.durable_image_dir
    log = outputs.bound_log
    durable_log = outputs.durable_log
    pidfile = outputs.bound_pidfile
    durable_pidfile = outputs.durable_pidfile
    start = time.monotonic()
    restore_seconds = 0.0
    root_pid: int | None = None
    tree: list[dict] = []
    phase = "restore_transaction_publication"
    result = 0
    with restore_signal_guard() as signals:
        try:
            begin_restored_pool_transaction(
                pool,
                image_dir=image_dir,
                pidfile=pidfile,
                log=log,
                durable_image_dir=recorded_image_dir,
                durable_pidfile=durable_pidfile,
                durable_log=durable_log,
                output_authority=outputs.transaction_authority_record(),
                operation_stamp=operation_stamp,
            )
            outputs.assert_current()
            signals.raise_if_interrupted()
            phase = "criu_restore"
            confined_restore_options = [
                *restore_options,
                "-W",
                str(outputs.bound_work_dir),
                "--pidfile",
                str(pidfile),
            ]
            run_restore_launcher(
                criu_command(
                    "restore",
                    "-D",
                    str(image_dir),
                    "-d",
                    *confined_restore_options,
                    "-o",
                    str(log),
                ),
                signals=signals,
                on_started=lambda launcher: record_restored_pool_launcher(
                    pool,
                    pid=launcher.pid,
                    start_ticks=launcher.start_ticks,
                    identity=launcher.identity,
                ),
            )
            outputs.assert_current()
            restore_seconds = time.monotonic() - start
            signals.raise_if_interrupted()
            phase = "restored_root_validation"
            root_pid = read_restored_root_pid(pidfile)
            tree = capture_authoritative_tree(root_pid)
            roles = validate_restored_tree(
                authoritative_root_pid=root_pid,
                recorded=recorded,
                tree=tree,
            )
            signals.raise_if_interrupted()
            phase = "restored_identity_staging"
            stage_restored_pool_metadata(pool, roles)
            signals.raise_if_interrupted()
            phase = "restored_resume"
            resume_restored_processes(roles)
            signals.raise_if_interrupted()
            phase = "restored_ready_publication"
            publish_restored_pool_ready(pool)
            signals.raise_if_interrupted()
            phase = "restored_pidfile_cleanup"
            _remove_pidfile(pidfile)
            _remove_bound_output_directory(outputs)
            signals.raise_if_interrupted()
        except subprocess.CalledProcessError as exc:
            retry = (
                _root_collision_retry_from_current_outputs(
                    outputs=outputs,
                    log=log,
                    durable_log=durable_log,
                    pidfile=pidfile,
                    recorded=recorded,
                    signals=signals,
                )
                if allow_root_collision_retry and phase == "criu_restore"
                else None
            )
            if retry is not None:
                retry_receipt = image_dir / f"restore-retry-{operation_stamp}.json"
                durable_retry_receipt = recorded_image_dir / retry_receipt.name
                retry["receipt"] = str(durable_retry_receipt)
                atomic_write_json(retry_receipt, retry)
                reset_root_collision_restore(
                    pool,
                    pidfile=pidfile,
                    log=log,
                    durable_pidfile=durable_pidfile,
                    durable_log=durable_log,
                    operation_stamp=operation_stamp,
                    retry_receipt=durable_retry_receipt,
                )
                _remove_bound_output_directory(outputs)
                retry_stamp = stamp()
                retry_log = image_dir / f"restore-{retry_stamp}.log"
                retry_pidfile = image_dir / f"restore-{retry_stamp}.pid"
                retry_options = list(restore_options)
                result = execute_restore_transaction(
                    args,
                    image_dir=image_dir,
                    durable_image_dir=recorded_image_dir,
                    pool=pool,
                    display_pool=display_pool,
                    recorded=recorded,
                    operation_stamp=retry_stamp,
                    log=retry_log,
                    pidfile=retry_pidfile,
                    restore_options=retry_options,
                    allow_root_collision_retry=False,
                )
                if result == 0:
                    print("restore_attempts=2")
                    print(f"restore_retry_receipt={durable_retry_receipt}")
                return result
            root_pid, tree = _capture_pidfile_tree(pidfile)
            cleanup = _handle_transaction_failure(
                image_dir=image_dir,
                durable_image_dir=recorded_image_dir,
                operation_stamp=operation_stamp,
                pool=pool,
                display_pool=display_pool,
                phase=phase,
                log=log,
                durable_log=durable_log,
                error=exc,
                root_pid=root_pid,
                tree=tree,
                recorded=recorded,
            )
            if cleanup.get("verified_quiescent") is True:
                _best_effort_remove_pidfile(pidfile)
                _remove_bound_output_directory(outputs)
            signals.raise_if_interrupted()
            if args.debug_criu:
                raise
            result = 1
        except BaseException as exc:
            if not tree and _pidfile_is_file(pidfile):
                root_pid, tree = _capture_pidfile_tree(pidfile)
            cleanup = _handle_transaction_failure(
                image_dir=image_dir,
                durable_image_dir=recorded_image_dir,
                operation_stamp=operation_stamp,
                pool=pool,
                display_pool=display_pool,
                phase=phase,
                log=log,
                durable_log=durable_log,
                error=exc,
                root_pid=root_pid,
                tree=tree,
                recorded=recorded,
            )
            if cleanup.get("verified_quiescent") is True:
                _best_effort_remove_pidfile(pidfile)
                _remove_bound_output_directory(outputs)
            signals.raise_if_interrupted()
            if isinstance(exc, EXPECTED_RESTORE_ERRORS):
                result = 1
            else:
                raise
    if result:
        return result
    print(f"pool={display_pool}")
    print(f"restore_log={durable_log}")
    displayed_options = [
        *restore_options,
        "-W",
        str(outputs.durable_work_dir),
        "--pidfile",
        str(durable_pidfile),
    ]
    print(f"restore_options={' '.join(displayed_options)}")
    print(f"restore_seconds={restore_seconds:.3f}")
    return 0


def restore_pool_locked(
    args: argparse.Namespace,
    *,
    image_dir: Path,
    pool: Path,
    authority: RestoredPoolAuthority | None = None,
    durable_image_dir: Path | None = None,
) -> int:
    display_pool = authority.pool if authority is not None else pool
    recorded_image_dir = durable_image_dir or image_dir
    if authority is not None:
        authority.assert_current(changed=True)
    if (
        preflight := criu_restore_preflight.run(
            args,
            pool,
            pool_is_live=pool_is_live,
            display_pool=display_pool,
        )
    ) is not None:
        return preflight
    published_shelf_only = bool(getattr(args, "published_shelf_only", False))
    stale_ready_demotion: dict[str, object] | None = None
    if published_shelf_only:
        try:
            stale_ready_demotion = demote_stale_ready_pool_for_restore(pool)
        except EXPECTED_RESTORE_ERRORS as exc:
            print("status=unavailable")
            print("failing_phase=stale_ready_demotion")
            print(f"pool={display_pool}")
            print(f"reason={type(exc).__name__}: {exc}")
            print("lifecycle_action=none")
            return 75
    else:
        try:
            with restore_signal_guard() as signals:
                recovered = _recover_incomplete_restore(
                    image_dir=image_dir,
                    pool=pool,
                    display_pool=display_pool,
                    durable_image_dir=recorded_image_dir,
                )
                signals.raise_if_interrupted()
            if recovered:
                return 1
        except EXPECTED_RESTORE_ERRORS as exc:
            print_preflight_failure(display_pool, f"incomplete restore recovery failed: {type(exc).__name__}: {exc}")
            return 1
    try:
        preflight_failure = restore_preflight_failure(pool)
    except EXPECTED_RESTORE_ERRORS as exc:
        print_preflight_failure(display_pool, f"{type(exc).__name__}: {exc}")
        return 1
    if preflight_failure:
        print_preflight_failure(display_pool, preflight_failure)
        return 1
    try:
        live = pool_is_live(pool)
    except EXPECTED_RESTORE_ERRORS as exc:
        print_preflight_failure(display_pool, f"{type(exc).__name__}: {exc}")
        return 1
    if live:
        print(f"pool={display_pool}")
        print("restore_log=-")
        print("restore_options=already-live")
        print("restore_seconds=0.000")
        return 0
    try:
        recorded = read_recorded_pool_processes(pool)
    except EXPECTED_RESTORE_ERRORS as exc:
        print_preflight_failure(display_pool, f"{type(exc).__name__}: {exc}")
        return 1

    operation_stamp = stamp()
    log = image_dir / f"restore-{operation_stamp}.log"
    pidfile = image_dir / f"restore-{operation_stamp}.pid"
    verbosity = "-v4" if args.debug_criu else "-v1"
    restore_options = [
        verbosity,
        "--file-validation=buildid",
        "--no-default-config",
        *CRIU_GUEST_CONTROL_TIME_NAMESPACE_OPTIONS,
        *args.criu_option,
        "--leave-stopped",
    ]
    result = criu_lazy_pages_dispatch.dispatch_restore_transaction(
        args,
        image_dir=image_dir,
        durable_image_dir=recorded_image_dir,
        pool=pool,
        display_pool=display_pool,
        recorded=recorded,
        operation_stamp=operation_stamp,
        log=log,
        pidfile=pidfile,
        restore_options=restore_options,
        execute_restore_transaction=execute_restore_transaction,
    )
    if stale_ready_demotion is not None:
        print("profile_activation=started")
        print("stale_ready_demotion=demoted")
        print(f"stale_ready_demotion_schema={stale_ready_demotion['schema']}")
    return result


def _restore_locked(
    args: argparse.Namespace,
    *,
    image_dir: Path,
    pool: Path,
    authority: RestoredPoolAuthority | None = None,
    durable_image_dir: Path | None = None,
) -> int:
    return restore_pool_locked(
        args,
        image_dir=image_dir,
        pool=pool,
        authority=authority,
        durable_image_dir=durable_image_dir,
    )


def main(
    argv: list[str] | None = None,
    *,
    admission_decision: StaticSnapshotAdmissionDecision | None = None,
    include_controller_attempt: bool = True,
) -> int:
    parser = argparse.ArgumentParser(description="Restore a CRIU-backed HOL Workbench warm pool image.")
    parser.add_argument("profile_root", type=Path, help="Profile artifact root containing criu-image/ and pool/")
    parser.add_argument(
        "--debug-criu",
        action="store_true",
        help="Use verbose CRIU logging. Slower; useful when restore is failing.",
    )
    parser.add_argument(
        "--lazy-pages",
        action="store_true",
        help="Reserved; currently fails closed until the detached lazy-pages daemon has owned lifecycle metadata.",
    )
    parser.add_argument(
        "--criu-option",
        action="append",
        default=[],
        help="Allowlisted CRIU restore option; repeatable. Supported: --criu-option=--skip-file-rwx-check",
    )
    criu_restore_preflight.add_argument(parser)
    args = parser.parse_args(argv)

    profile_root = Path(os.path.abspath(args.profile_root.expanduser()))
    try:
        profile_authority: RestoredProfileAuthority
        with restored_profile_authority(profile_root) as profile_authority:
            bound_profile_root = profile_authority.bound_profile_root
            try:
                if admission_decision is None:
                    validate_snapshot_manifest(
                        bound_profile_root,
                        include_controller_attempt=include_controller_attempt,
                    )
                else:
                    verify_static_snapshot_admission_decision(bound_profile_root, admission_decision)
            except RuntimeError as exc:
                print_manifest_failure(profile_root, exc)
                return 1
            profile_authority.assert_current(changed=True)
            authority = select_restored_pool_authority(
                profile_root,
                profile_fd=profile_authority.profile_fd,
                expected_profile_identity=profile_authority.profile_identity,
            )
            profile_authority.assert_current(changed=True)
            with restored_pool_lock(
                authority.pool,
                authority=authority,
                profile_fd=profile_authority.profile_fd,
            ) as bound_pool:
                profile_authority.assert_current(changed=True)
                return _restore_locked(
                    args,
                    image_dir=profile_authority.bound_image_dir,
                    durable_image_dir=profile_authority.image_dir,
                    pool=bound_pool,
                    authority=authority,
                )
    except PoolAuthorityError as exc:
        print_pool_authority_failure(profile_root, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
