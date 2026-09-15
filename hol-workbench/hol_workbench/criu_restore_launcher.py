"""Signal-safe ownership of one detached CRIU restore launcher."""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import Any

from hol_workbench.process_groups import process_birth_identity, process_start_ticks

GATE_EXEC_CODE = """\
import os
import sys

if not sys.argv[1:] or sys.stdin.buffer.read(1) != b"1":
    raise SystemExit(125)
os.execvp(sys.argv[1], sys.argv[1:])
"""


class RestoreSignalInterruption(SystemExit):
    """Deferred non-SIGINT controller interruption with shell exit semantics."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(128 + signum)


@dataclass
class RestoreSignalState:
    """Latch terminal signals until the isolated CRIU launcher has settled."""

    interrupted_signal: int | None = None
    handlers_installed: bool = False

    def latch(self, signum: int) -> None:
        if self.interrupted_signal is None:
            self.interrupted_signal = signum

    def raise_if_interrupted(self) -> None:
        if self.interrupted_signal == signal.SIGINT:
            raise KeyboardInterrupt
        if self.interrupted_signal is not None:
            raise RestoreSignalInterruption(self.interrupted_signal)


@contextmanager
def restore_signal_guard() -> Iterator[RestoreSignalState]:
    """Defer SIGINT/SIGTERM across the caller's complete restore transaction.

    The caller must keep this context open through post-restore validation,
    publication, resume, and failure cleanup. ``run_restore_launcher`` uses the
    same state to turn Python-level ``KeyboardInterrupt`` exceptions from
    ``Popen.wait`` into deferred SIGINT as well.
    """

    state = RestoreSignalState()
    saved_handlers: dict[int, Any] = {}

    def latch_signal(signum: int, _frame: object) -> None:
        state.latch(signum)

    if threading.current_thread() is threading.main_thread():
        saved_handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
        for signum in saved_handlers:
            signal.signal(signum, latch_signal)
        state.handlers_installed = True

    failed = False
    try:
        yield state
    except BaseException:
        failed = True
        raise
    finally:
        for signum, handler in saved_handlers.items():
            signal.signal(signum, handler)
        state.handlers_installed = False
    if not failed:
        state.raise_if_interrupted()


@dataclass(frozen=True)
class RestoreLauncher:
    command: list[str]
    process: Any
    pid: int
    start_ticks: int
    identity: str


def _gate_command(command: Sequence[str]) -> list[str]:
    return [sys.executable, "-c", GATE_EXEC_CODE, *command]


def start_restore_launcher(
    command: Sequence[str],
    *,
    popen: Callable[..., Any] | None = None,
    start_ticks_reader: Callable[[Any], int | None] = process_start_ticks,
    identity_reader: Callable[[Any], str | None] = process_birth_identity,
) -> RestoreLauncher:
    """Start a gated launcher outside the controller's terminal process group."""

    argv = list(command)
    process = (popen or subprocess.Popen)(
        _gate_command(argv),
        stdin=subprocess.PIPE,
        start_new_session=True,
    )
    pid = int(process.pid)
    start_ticks = start_ticks_reader(pid)
    identity = identity_reader(pid)
    if not isinstance(start_ticks, int) or start_ticks <= 0 or not isinstance(identity, str) or not identity:
        if process.stdin is not None:
            process.stdin.close()
        process.wait()
        raise RuntimeError("CRIU restore launcher identity is unavailable before gate release")
    return RestoreLauncher(
        command=argv,
        process=process,
        pid=pid,
        start_ticks=start_ticks,
        identity=identity,
    )


def release_restore_launcher(launcher: RestoreLauncher) -> None:
    """Release the gate only after launcher ownership is durably published."""

    stream = launcher.process.stdin
    if stream is None:
        raise RuntimeError("CRIU restore launcher gate is unavailable")
    try:
        stream.write(b"1")
        stream.flush()
    finally:
        stream.close()


def wait_restore_launcher(launcher: RestoreLauncher, *, signals: RestoreSignalState) -> None:
    """Wait to settled exit without ever signalling the owned CRIU process."""

    while True:
        try:
            returncode = launcher.process.wait()
            break
        except KeyboardInterrupt:
            signals.latch(signal.SIGINT)
    signals.raise_if_interrupted()
    if returncode:
        raise subprocess.CalledProcessError(returncode, launcher.command)


def run_restore_launcher(
    command: Sequence[str],
    *,
    signals: RestoreSignalState,
    popen: Callable[..., Any] | None = None,
    on_started: Callable[[RestoreLauncher], None] | None = None,
    start_ticks_reader: Callable[[Any], int | None] = process_start_ticks,
    identity_reader: Callable[[Any], str | None] = process_birth_identity,
) -> RestoreLauncher:
    """Start and settle one restore while terminal interruption stays deferred."""

    launcher = start_restore_launcher(
        command,
        popen=popen,
        start_ticks_reader=start_ticks_reader,
        identity_reader=identity_reader,
    )
    try:
        if on_started is not None:
            on_started(launcher)
        release_restore_launcher(launcher)
    except BaseException:
        if launcher.process.stdin is not None and not launcher.process.stdin.closed:
            launcher.process.stdin.close()
        with suppress(subprocess.CalledProcessError):
            wait_restore_launcher(launcher, signals=signals)
        raise
    wait_restore_launcher(launcher, signals=signals)
    return launcher
