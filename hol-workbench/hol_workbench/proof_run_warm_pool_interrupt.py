"""Signal and cleanup helpers for interrupting leased warm-pool evals."""

from __future__ import annotations

import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from hol_workbench.jsonio import read_json
from hol_workbench.proof_run_warm_pool_eval_release import (
    WarmPoolEvalReleaseDecision,
    decide_warm_pool_eval_release,
)


class WarmPoolEvalInterrupted(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"signal {signum}")


class WarmPoolEvalSignalController(Protocol):
    """Minimal signal-transition interface accepted by guarded evaluation."""

    def arm(self) -> None: ...

    def defer(self) -> None: ...


@dataclass(frozen=True)
class GuardedWarmPoolEvalResult:
    status: int
    error: BaseException | None = None
    interruption_cleanup: str | None = None
    release_decision: WarmPoolEvalReleaseDecision | None = None


@dataclass
class WarmPoolEvalSignalState:
    interrupted_signal: int | None = None
    deferred: bool = False

    def defer(self) -> None:
        self.deferred = True

    def arm(self) -> None:
        self.deferred = False
        if self.interrupted_signal is not None:
            raise WarmPoolEvalInterrupted(self.interrupted_signal)


@contextmanager
def warm_pool_eval_signal_guard() -> Iterator[WarmPoolEvalSignalState]:
    saved_handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    state = WarmPoolEvalSignalState()

    def interrupt(signum: int, _frame: object) -> None:
        if state.interrupted_signal is None:
            state.interrupted_signal = signum
            if not state.deferred:
                raise WarmPoolEvalInterrupted(signum)

    for signum in saved_handlers:
        signal.signal(signum, interrupt)
    try:
        yield state
    finally:
        for signum, handler in saved_handlers.items():
            signal.signal(signum, handler)


def _attempt_children(registry: Path, attempt_id: str) -> list[dict[str, Any]]:
    try:
        data = read_json(registry)
    except (OSError, ValueError):
        return []
    children = data.get("children") if isinstance(data, dict) else []
    return [
        child
        for child in children or []
        if isinstance(child, dict) and str(child.get("attempt_id") or "") == attempt_id
    ]


def wait_for_fork_attempt_cleanup(
    session: dict[str, Any],
    *,
    attempt_id: str,
    transcript: Path,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """Wait until the fork worker has made one interrupted attempt quiescent."""
    registry_value = session.get("active_children_registry")
    if not registry_value:
        return {"status": "unverified", "reason": "active-child registry unavailable"}
    registry = Path(str(registry_value))
    started = time.monotonic()
    deadline = started + timeout_seconds
    observed = False
    while True:
        children = _attempt_children(registry, attempt_id)
        observed = observed or bool(children)
        try:
            transcript_text = transcript.read_text(encoding="utf-8", errors="replace")
        except OSError:
            transcript_text = ""
        cancellation_quiescent = "client_disconnected;" in transcript_text and any(
            marker in transcript_text for marker in ("cleanup=quiescent", "cleanup=already_quiescent")
        )
        if cancellation_quiescent or (observed and not children):
            return {"status": "quiescent", "registry": str(registry), "observed": observed}
        if time.monotonic() >= deadline:
            return {
                "status": "survivors" if children else "unverified",
                "registry": str(registry),
                "observed": observed,
                "children": children,
            }
        time.sleep(0.05)


def run_warm_pool_eval_under_signal_guard(
    eval_args: Any,
    *,
    pool_engine: str,
    session: Path,
    eval_command: Callable[[Any], int],
    signal_state: WarmPoolEvalSignalController | None = None,
) -> GuardedWarmPoolEvalResult:
    interruption: WarmPoolEvalInterrupted | None = None
    result = GuardedWarmPoolEvalResult(status=1)
    eval_returned = False
    try:
        if signal_state is not None:
            signal_state.arm()
        status = eval_command(eval_args)
        # For fork-basis evals, a normal return follows the worker response,
        # which is sent only after the exact disposable child is quiescent.
        # Remember that boundary before deferring signals so a TERM latched in
        # the post-eval transition cannot turn a completed child into an
        # unverified cleanup.
        eval_returned = True
        result = GuardedWarmPoolEvalResult(status=status)
    except WarmPoolEvalInterrupted as exc:
        interruption = exc
    except SystemExit as exc:
        status = exc.code if isinstance(exc.code, int) else 1
        error = exc if isinstance(exc.code, str) and exc.code else None
        result = GuardedWarmPoolEvalResult(status, error)
    except Exception as exc:
        result = GuardedWarmPoolEvalResult(1, exc)

    if signal_state is not None:
        try:
            signal_state.defer()
        except WarmPoolEvalInterrupted as exc:
            # The signal arrived in the armed-to-deferred transition. Its
            # first delivery is now latched, so the retry cannot raise.
            signal_state.defer()
            interruption = interruption or exc
    if interruption is None:
        return result

    request_started = bool(getattr(eval_args, "_active_warm_request_started", False))
    request_quiescent = bool(getattr(eval_args, "_active_warm_request_quiescent", False))
    cleanup = "not_started" if pool_engine == "fork_basis" and not request_started else "unverified"
    if pool_engine == "fork_basis" and request_started:
        if eval_returned or request_quiescent:
            cleanup = "request_completed"
        else:
            cleanup = str(
                wait_for_fork_attempt_cleanup(
                    read_json(session / "session.json"),
                    attempt_id=str(getattr(eval_args, "_active_warm_attempt_id", "")),
                    transcript=Path(str(getattr(eval_args, "_active_warm_transcript", ""))),
                )["status"]
            )
    reusable = pool_engine == "fork_basis" and cleanup in {"quiescent", "not_started", "request_completed"}
    if eval_returned and result.status == 76:
        decision = decide_warm_pool_eval_release(
            pool_engine=pool_engine,
            exit_status=result.status,
            requested_status="idle",
        )
    else:
        decision = WarmPoolEvalReleaseDecision(
            status="idle" if reusable else "poisoned",
            reason=(
                f"fork child interruption cleanup {cleanup}; basis remains clean"
                if reusable
                else f"interruption cleanup {cleanup}; seat is not reusable"
            ),
        )
    return GuardedWarmPoolEvalResult(128 + interruption.signum, interruption, cleanup, decision)


def run_guarded_warm_pool_eval(
    eval_args: Any,
    *,
    pool_engine: str,
    session: Path,
    eval_command: Callable[[Any], int],
) -> GuardedWarmPoolEvalResult:
    with warm_pool_eval_signal_guard() as signal_state:
        return run_warm_pool_eval_under_signal_guard(
            eval_args,
            pool_engine=pool_engine,
            session=session,
            eval_command=eval_command,
            signal_state=signal_state,
        )
