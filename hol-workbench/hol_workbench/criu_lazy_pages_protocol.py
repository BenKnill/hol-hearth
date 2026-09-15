"""Process and file-descriptor protocol for internal CRIU lazy-pages launch."""

from __future__ import annotations

import fcntl
import os
import select
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hol_workbench.criu_invocation import criu_command, criu_launcher_prefix
from hol_workbench.process_groups import process_birth_identity, process_start_ticks

LOCK_NAME = "lazy-pages.lock"
STATUS_FD = 3

GATE_EXEC_CODE = """\
import os
import sys

gate_fd = int(sys.argv[1])
command = sys.argv[2:]
try:
    release = os.read(gate_fd, 1)
finally:
    os.close(gate_fd)
if not command or release != b"1":
    raise SystemExit(125)
os.execvp(command[0], command)
"""

STATUS_EXEC_CODE = """\
import os
import sys

status_fd = int(sys.argv[1])
command = sys.argv[2:]
if status_fd <= 2 or not command:
    raise SystemExit(125)
os.dup2(0, status_fd, inheritable=True)
os.execvp(command[0], command)
"""


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_paths(image_dir: Path, pidfile: Path, log: Path) -> tuple[Path, Path, Path]:
    root = image_dir.resolve()
    resolved_pidfile = pidfile.resolve()
    resolved_log = log.resolve()
    for artifact in (resolved_pidfile, resolved_log):
        if artifact.parent != root:
            raise RuntimeError(f"lazy-pages launch artifact escapes the CRIU image directory: {artifact}")
    if not resolved_pidfile.name.startswith("lazy-pages-") or resolved_pidfile.suffix != ".pid":
        raise RuntimeError(f"lazy-pages launch pidfile has an unsupported name: {resolved_pidfile}")
    if not resolved_log.name.startswith("lazy-pages-") or resolved_log.suffix != ".log":
        raise RuntimeError(f"lazy-pages launch log has an unsupported name: {resolved_log}")
    return root, resolved_pidfile, resolved_log


def lazy_pages_command(image_dir: Path, *, pidfile: Path, log: Path) -> list[str]:
    """Build the config-isolated command that moves stdin to CRIU's status fd."""
    root, resolved_pidfile, resolved_log = _validate_paths(image_dir, pidfile, log)
    command = criu_command(
        "lazy-pages",
        "-D",
        str(root),
        "-d",
        "--pidfile",
        str(resolved_pidfile),
        "--status-fd",
        str(STATUS_FD),
        "--no-default-config",
        "-o",
        resolved_log.name,
    )
    launcher_prefix = criu_launcher_prefix()
    if command[: len(launcher_prefix)] != launcher_prefix:
        raise RuntimeError("CRIU command does not begin with its typed launcher prefix")
    return [
        *launcher_prefix,
        str(Path(sys.executable).resolve()),
        "-c",
        STATUS_EXEC_CODE,
        str(STATUS_FD),
        *command[len(launcher_prefix) :],
    ]


@contextmanager
def lazy_pages_image_lock(image_dir: Path, *, blocking: bool = False) -> Iterator[None]:
    """Serialize launch, recovery, and socket reuse for one CRIU image directory."""
    root = image_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / LOCK_NAME).open("a+", encoding="utf-8") as lock:
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(lock.fileno(), operation)
        except BlockingIOError as exc:
            raise RuntimeError(f"another lazy-pages lifecycle transaction owns {root}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@dataclass
class LazyPagesLauncher:
    command: list[str]
    process: Any
    pid: int
    start_ticks: int
    identity: str
    gate_write_fd: int | None
    status_read_fd: int | None
    released: bool = False


def _gate_command(command: Sequence[str], gate_read_fd: int) -> list[str]:
    return [sys.executable, "-c", GATE_EXEC_CODE, str(gate_read_fd), *command]


def start_gated_lazy_pages(
    command: Sequence[str],
    *,
    popen: Callable[..., Any] | None = None,
    start_ticks_reader: Callable[[Any], int | None] = process_start_ticks,
    identity_reader: Callable[[Any], str | None] = process_birth_identity,
) -> LazyPagesLauncher:
    """Spawn a dual-pipe launcher that cannot exec CRIU before publication."""
    gate_read, gate_write = os.pipe()
    status_read, status_write = os.pipe()
    try:
        process = (popen or subprocess.Popen)(
            _gate_command(list(command), gate_read),
            stdin=status_write,
            pass_fds=(gate_read,),
            start_new_session=True,
        )
    except BaseException:
        for fd in (gate_read, gate_write, status_read, status_write):
            with suppress(OSError):
                os.close(fd)
        raise
    os.close(gate_read)
    os.close(status_write)
    pid = int(process.pid)
    ticks = start_ticks_reader(pid)
    identity = identity_reader(pid)
    if not isinstance(ticks, int) or ticks <= 0 or not isinstance(identity, str) or identity != f"linux-proc:{ticks}":
        os.close(gate_write)
        os.close(status_read)
        with suppress(subprocess.SubprocessError):
            process.wait(timeout=2)
        raise RuntimeError("lazy-pages gated launcher has no stable Linux birth identity")
    return LazyPagesLauncher(
        command=list(command),
        process=process,
        pid=pid,
        start_ticks=ticks,
        identity=identity,
        gate_write_fd=gate_write,
        status_read_fd=status_read,
    )


def _close_fd(launcher: LazyPagesLauncher, field: str) -> None:
    fd = getattr(launcher, field)
    if fd is None:
        return
    with suppress(OSError):
        os.close(fd)
    setattr(launcher, field, None)


def release_gated_lazy_pages(launcher: LazyPagesLauncher) -> None:
    """Release CRIU only after the durable pending record authorizes it."""
    fd = launcher.gate_write_fd
    if fd is None:
        raise RuntimeError("lazy-pages launcher gate is unavailable")
    try:
        if os.write(fd, b"1") != 1:
            raise RuntimeError("lazy-pages launcher gate release was incomplete")
        launcher.released = True
    finally:
        _close_fd(launcher, "gate_write_fd")


def observe_status_nul(
    launcher: LazyPagesLauncher,
    *,
    timeout_seconds: float,
    selector: Callable[..., tuple[list[Any], list[Any], list[Any]]] = select.select,
    reader: Callable[[int, int], bytes] = os.read,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Observe CRIU's one-byte ready protocol while also watching parent exit."""
    fd = launcher.status_read_fd
    if fd is None:
        raise RuntimeError("lazy-pages status channel is unavailable")
    deadline = monotonic() + max(0.0, timeout_seconds)
    while True:
        returncode = launcher.process.poll()
        if isinstance(returncode, int) and returncode != 0:
            raise subprocess.CalledProcessError(returncode, launcher.command)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for CRIU lazy-pages status-fd NUL byte")
        readable, _, _ = selector([fd], [], [], min(0.05, remaining))
        if not readable:
            continue
        observed = reader(fd, 2)
        if observed != b"\x00":
            label = "EOF" if not observed else observed.hex()
            raise RuntimeError(f"CRIU lazy-pages status channel returned {label}, expected one NUL byte")
        extra_readable, _, _ = selector([fd], [], [], 0)
        if extra_readable:
            extra = reader(fd, 1)
            if extra:
                raise RuntimeError(f"CRIU lazy-pages status channel returned an extra byte: {extra.hex()}")
        return {
            "status_fd_observed": True,
            "byte_hex": "00",
            "observed_utc": _utc_now(),
        }


def wait_lazy_pages_parent(launcher: LazyPagesLauncher, *, timeout_seconds: float) -> None:
    returncode = launcher.process.wait(timeout=timeout_seconds)
    if returncode:
        raise subprocess.CalledProcessError(returncode, launcher.command)


def launcher_record(launcher: LazyPagesLauncher) -> dict[str, Any]:
    return {
        "pid": launcher.pid,
        "start_ticks": launcher.start_ticks,
        "birth_identity": launcher.identity,
    }
