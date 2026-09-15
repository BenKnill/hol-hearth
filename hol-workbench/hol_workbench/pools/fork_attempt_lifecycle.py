"""Bounded quiescence check for one disposable fork attempt."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hol_workbench.jsonio import read_json_strict

ACTIVE_CHILDREN_SCHEMA = "proof-run.fork-active-children.v1"


@dataclass(frozen=True)
class AttemptRegistrySnapshot:
    valid: bool
    children: tuple[dict[str, Any], ...] = ()
    reason: str | None = None


def _attempt_children(registry: Path, attempt_id: str) -> AttemptRegistrySnapshot:
    try:
        data = read_json_strict(registry)
    except (OSError, TypeError, ValueError) as exc:
        return AttemptRegistrySnapshot(False, reason=f"{type(exc).__name__}: {exc}")
    children = data.get("children")
    if data.get("schema") != ACTIVE_CHILDREN_SCHEMA or not isinstance(children, list):
        return AttemptRegistrySnapshot(False, reason="invalid active-child registry schema")
    if any(not isinstance(child, dict) for child in children):
        return AttemptRegistrySnapshot(False, reason="active-child registry contains a non-object row")
    for child in children:
        assert isinstance(child, dict)
        try:
            child_pid = int(str(child.get("pid") or ""))
            child_pgid = int(str(child.get("pgid") or ""))
            child_ticks = int(str(child.get("process_start_ticks") or ""))
        except (TypeError, ValueError):
            return AttemptRegistrySnapshot(False, reason="active-child registry contains an invalid child row")
        identity = child.get("process_identity")
        if (
            not isinstance(child.get("attempt_id"), str)
            or not child["attempt_id"]
            or child_pid <= 0
            or child_pgid <= 0
            or child_ticks <= 0
            or identity != f"linux-proc:{child_ticks}"
        ):
            return AttemptRegistrySnapshot(False, reason="active-child registry contains an invalid child row")
    matching = tuple(child for child in children if str(child.get("attempt_id") or "") == attempt_id)
    return AttemptRegistrySnapshot(True, matching)


def wait_for_fork_attempt_cleanup(
    session: dict[str, Any],
    *,
    attempt_id: str,
    transcript: Path,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """Wait until the broker reports one disconnected child quiescent."""

    registry_value = session.get("active_children_registry")
    if not registry_value:
        return {"status": "unverified", "reason": "active-child registry unavailable"}
    registry = Path(str(registry_value))
    _ = transcript  # Raw proof output is never cleanup authority.
    deadline = time.monotonic() + timeout_seconds
    observed = False
    while True:
        snapshot = _attempt_children(registry, attempt_id)
        if not snapshot.valid:
            return {
                "status": "unverified",
                "registry": str(registry),
                "observed": observed,
                "reason": snapshot.reason,
            }
        children = list(snapshot.children)
        observed = observed or bool(children)
        if observed and not children:
            return {"status": "quiescent", "registry": str(registry), "observed": observed}
        if time.monotonic() >= deadline:
            return {
                "status": "survivors" if children else "unverified",
                "registry": str(registry),
                "observed": observed,
                "children": children,
            }
        time.sleep(0.05)
