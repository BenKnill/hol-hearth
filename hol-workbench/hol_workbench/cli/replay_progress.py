"""Rate-limited, diagnostic-only progress for the recorded replay controller."""
from __future__ import annotations

import argparse
import math
import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

from hol_workbench.warm_eval_timeout import warm_eval_response_timeout

REPORTER_JOIN_TIMEOUT = 0.1


def progress_interval(value: str) -> float:
    interval = float(value)
    if not math.isfinite(interval) or (interval != 0 and interval < 1):
        raise argparse.ArgumentTypeError("use 0 to disable progress, or at least 1 second")
    return interval


def add_progress_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--progress-interval", type=progress_interval, default=15.0, metavar="SECONDS",
        help="stderr progress interval (default: 15; minimum: 1; 0 disables)",
    )


class ReplayProgress:
    """Report known client phases; never infer kernel activity from silence.

    Request countdowns start immediately before dispatch, so include dispatch
    overhead. They are not kernel CPU time or an estimate of proof completion.
    Neither the reporter nor its clock controls the replay's timeout.
    """

    def __init__(
        self, *, timeout: float, interval: float, stream: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout = timeout
        self.response_limit = warm_eval_response_timeout(timeout)
        self.interval = interval
        self.stream = sys.stderr if stream is None else stream
        self.clock = clock
        self.started = clock()
        self.phase_started = self.started
        self.phase = "preparing"
        self.request_started: float | None = None
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None
        self.hint_shown = False

    def __enter__(self) -> ReplayProgress:
        if self.interval:
            self.thread = threading.Thread(target=self._run, name="replay-progress", daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stopped.set()
        self.wake.set()
        if self.thread is not None:
            # A stalled diagnostic sink must not hold proof completion. The
            # daemon may finish its pending write later; no replay owns it.
            self.thread.join(timeout=REPORTER_JOIN_TIMEOUT)

    def set_phase(self, phase: str) -> None:
        with self.lock:
            self.phase = phase
            self.phase_started = self.clock()
            if phase == "evaluation-request":
                self.request_started = self.phase_started
        if phase == "cancelling" and self.interval:
            # Only publish state here: this callback precedes HOL child cleanup.
            # Wake the reporter without waiting for its output sink.
            self.wake.set()

    def _line(self, now: float) -> str:
        # Called with the lock held so one line uses one coherent phase snapshot.
        elapsed = max(0.0, now - self.started)
        phase_elapsed = max(0.0, now - self.phase_started)
        line = (f"PROGRESS: phase={self.phase} elapsed={elapsed:.1f}s "
                f"phase_elapsed={phase_elapsed:.1f}s")
        if self.phase == "evaluation-request" and self.request_started is not None:
            age = max(0.0, now - self.request_started)
            line += (f" requested_budget={self.timeout:g}s"
                     f" requested_remaining={max(0.0, self.timeout - age):.1f}s"
                     f" response_limit={self.response_limit:g}s"
                     f" response_remaining={max(0.0, self.response_limit - age):.1f}s")
        elif self.request_started is None:
            line += f" requested_budget={self.timeout:g}s (evaluation request not started)"
        return line

    def emit(self) -> None:
        with self.lock:
            if self.stopped.is_set():
                return
            lines = []
            if not self.hint_shown:
                lines.append(
                    "WAIT: Ctrl-C cancels this attempt's child and releases admission; "
                    "the shared warm seat stays. Request countdowns use the client dispatch "
                    "clock, not kernel/tactic progress; the response limit includes controller allowance."
                )
                self.hint_shown = True
            lines.append(self._line(self.clock()))
        # Never hold the phase lock during I/O. A full pipe or stalled log
        # collector may block this daemon, but cannot block phase callbacks,
        # child cleanup, admission release, or the bounded context exit.
        if self.stopped.is_set():
            return
        try:
            self.stream.write("\n".join(lines) + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            # Diagnostics must not turn a valid proof into a failed attempt.
            self.stopped.set()
            self.wake.set()

    def _run(self) -> None:
        while not self.stopped.is_set():
            self.wake.wait(self.interval)
            self.wake.clear()
            if self.stopped.is_set():
                return
            self.emit()
