"""Warm-evaluation response timeout policy."""

from __future__ import annotations


def warm_eval_response_timeout(timeout: float | int | None) -> float | None:
    if timeout is None:
        return None
    return max(float(timeout) + 15.0, 30.0)
