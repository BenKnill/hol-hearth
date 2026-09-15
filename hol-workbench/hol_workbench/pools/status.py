"""Warm-pool status and seat-summary helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def pool_session_counts(sessions: Iterable[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for session in sessions:
        if not isinstance(session, dict):
            continue
        status = str(session.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def pool_status_summary(pool: dict[str, Any]) -> dict[str, int]:
    return pool_session_counts(pool.get("sessions") or [])


def pool_seat_summary_from_counts(counts: dict[str, int]) -> dict[str, int]:
    usable = int(counts.get("idle", 0)) + int(counts.get("dirty", 0))
    poisoned = sum(int(counts.get(key, 0)) for key in ("poisoned", "failed", "preload_failed", "blocked"))
    leased = int(counts.get("busy", 0)) + int(counts.get("leased", 0))
    dirty = int(counts.get("dirty", 0))
    total = sum(int(value) for value in counts.values())
    return {
        "total_seats": total,
        "usable_seats": usable,
        "poisoned_seats": poisoned,
        "leased_seats": leased,
        "dirty_seats": dirty,
    }
