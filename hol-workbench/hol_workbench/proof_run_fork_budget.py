"""Termination classification for disposable fork-worker evaluations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hol_workbench.filter_output import semantic_line, strip_ansi
from hol_workbench.filter_output_events import (
    literal_load_completed_event,
    literal_load_started_event,
    val_binding_name,
)

TIMEOUT_KINDS = frozenset({"wall_timeout", "idle_timeout", "proof_search_budget"})
DEFAULT_FORK_SPAWN_TIMEOUT_SECONDS = 60.0
FORK_SPAWN_RECOVERY_TIMEOUT_SECONDS = 30.0
PROOF_SEARCH_BUDGET_SCOPE = "current_top_level_binding_after_fork_spawn"


@dataclass
class ProofSearchBudgetProgress:
    """Incrementally observe completed OCaml bindings in a growing transcript."""

    offset: int = 0
    pending: bytes = b""
    reset_count: int = 0
    last_completed_binding: str | None = None
    literal_load_depth: int = 0

    @property
    def literal_load_active(self) -> bool:
        return self.literal_load_depth > 0

    def scan(self, transcript: Path) -> list[str]:
        try:
            size = transcript.stat().st_size
        except OSError:
            return []
        if size < self.offset:
            self.offset = 0
            self.pending = b""
        if size == self.offset:
            return []
        try:
            with transcript.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
        except OSError:
            return []
        self.offset += len(chunk)
        parts = (self.pending + chunk).split(b"\n")
        self.pending = parts.pop()
        completed: list[str] = []
        for raw_line in parts:
            line = semantic_line(strip_ansi(raw_line.decode("utf-8", errors="replace").rstrip("\r")))
            started_load = literal_load_started_event(line)
            if started_load is not None:
                self.literal_load_depth += 1
                continue
            load = literal_load_completed_event(line)
            if load is not None:
                self.literal_load_depth = max(0, self.literal_load_depth - 1)
                completed.append(f'{load["loader"]} "{load["declared_path"]}"')
                continue
            name = val_binding_name(line)
            if name:
                completed.append(name)
        if completed:
            self.reset_count += len(completed)
            self.last_completed_binding = completed[-1]
        return completed


def validated_fork_spawn_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("fork spawn timeout must be numeric")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("fork spawn timeout must be a finite positive number")
    return timeout


class ForkSpawnHandshakeTimeout(RuntimeError):
    """The configured spawn deadline fired before the parent emitted its token."""

    def __init__(
        self,
        *,
        attempt_id: str,
        deadline_seconds: float,
        elapsed_seconds: float,
        cleanup_status: str,
        seat_reusable: bool,
    ) -> None:
        self.attempt_id = attempt_id
        self.deadline_seconds = deadline_seconds
        self.elapsed_seconds = elapsed_seconds
        self.cleanup_status = cleanup_status
        self.seat_reusable = seat_reusable
        super().__init__(
            f"fork basis HOL timed out during fork spawn {attempt_id} after "
            f"{elapsed_seconds:.3f}s (deadline {deadline_seconds:.3f}s); cleanup={cleanup_status}"
        )

    def response_fields(self) -> dict[str, Any]:
        return {
            "status": "fork_spawn_timeout",
            "exit_status": 125 if self.seat_reusable else 126,
            "fork_spawn_timeout_seconds": self.deadline_seconds,
            "fork_spawn_handshake_elapsed_seconds": round(self.elapsed_seconds, 3),
            "fork_spawn_handshake_status": "timed_out",
            "fork_spawn_cleanup_status": self.cleanup_status,
            "fork_spawn_child_registered": False,
            "seat_reusable": self.seat_reusable,
            "basis_poisoned": False,
        }


def fork_eval_termination_kind(
    *,
    request: dict[str, Any],
    stop_requested: bool,
    peer_closed: bool,
    elapsed: float,
    idle_elapsed: float,
    proof_search_elapsed: float | None = None,
) -> str | None:
    if stop_requested:
        return "pool_stop"
    if peer_closed:
        return "client_disconnected"
    budget = request.get("proof_search_budget_seconds")
    budget_elapsed = elapsed if proof_search_elapsed is None else proof_search_elapsed
    if budget is not None and budget_elapsed >= float(budget):
        return "proof_search_budget"
    timeout = request.get("timeout_seconds")
    if timeout is not None and elapsed >= float(timeout):
        return "wall_timeout"
    idle_timeout = request.get("idle_timeout_seconds")
    if idle_timeout is not None and idle_elapsed >= float(idle_timeout):
        return "idle_timeout"
    return None


def fork_eval_lifecycle_fields(
    *,
    termination_kind: str | None,
    child_cleanup: dict[str, Any] | None,
    child_quiescent: bool,
    request: dict[str, Any],
) -> dict[str, Any]:
    return {
        "timeout_kind": termination_kind if termination_kind in TIMEOUT_KINDS else None,
        "termination_kind": termination_kind,
        "child_cleanup": child_cleanup,
        "child_quiescent": child_quiescent,
        "cleanup_verified": child_quiescent
        and (child_cleanup or {}).get("status") in {"quiescent", "already_quiescent"},
        "seat_reusable": child_quiescent,
        "proof_search_budget_seconds": request.get("proof_search_budget_seconds"),
    }
