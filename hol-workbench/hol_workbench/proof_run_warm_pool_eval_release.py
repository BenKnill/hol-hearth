"""Release-status decisions for warm-pool eval workers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WarmPoolEvalReleaseDecision:
    status: str
    reason: str


def decide_warm_pool_eval_release(
    *,
    pool_engine: str,
    exit_status: int,
    requested_status: str,
    eval_error: BaseException | None = None,
) -> WarmPoolEvalReleaseDecision:
    release_status = requested_status
    release_reason = "pool eval completed"
    if eval_error is not None:
        release_reason = f"pool eval raised {type(eval_error).__name__}: {eval_error}"
    if exit_status == 76:
        release_status = "poisoned"
        release_reason = "restored runtime execution contract violation; the worker is not reusable"
    elif exit_status in (124, 125, 137):
        if pool_engine == "fork_basis":
            release_status = "idle"
            release_reason = f"fork child returned timeout/prompt-loss status {exit_status}; basis remains clean"
        else:
            release_status = "poisoned"
            release_reason = f"pool eval returned timeout/prompt-loss status {exit_status}"
    elif pool_engine != "fork_basis" and exit_status != 0:
        release_status = "poisoned"
        release_reason = f"mutable process pool eval failed with status {exit_status}; worker state is not reusable"
    return WarmPoolEvalReleaseDecision(status=release_status, reason=release_reason)
