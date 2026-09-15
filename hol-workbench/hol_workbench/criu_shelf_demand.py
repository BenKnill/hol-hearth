"""Live demand records for queued or admitted CRIU profile routes."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hol_workbench.jsonio import atomic_write_json, read_json
from hol_workbench.process_groups import process_birth_identity
from hol_workbench.proof_run_runtime import utc_now
from hol_workbench.runtime_cache import workbench_cache_root

SHELF_DEMAND_SCHEMA = "hol-workbench.criu-shelf-demand.v1"
HOST_PROOF_DEMAND_SCHEMA = "hol-workbench.host-proof-demand.v1"


def host_proof_demand_root(environment: dict[str, str] | None = None) -> Path:
    return workbench_cache_root(environment=environment) / "active-proof-demands"


@contextmanager
def host_proof_demand(*, source: str, profile: str, run_root: str) -> Iterator[Path]:
    pid = os.getpid()
    root = host_proof_demand_root()
    path = root / f"proof-{pid}-{uuid.uuid4().hex}.json"
    atomic_write_json(
        path,
        {
            "schema": HOST_PROOF_DEMAND_SCHEMA,
            "pid": pid,
            "process_identity": process_birth_identity(pid),
            "source": source,
            "profile": profile,
            "run_root": run_root,
            "created_utc": utc_now(),
        },
    )
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def read_host_proof_demands(*, require_live: bool = True) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(host_proof_demand_root().glob("*.json")):
        try:
            row = read_json(path)
        except (OSError, TypeError, ValueError):
            continue
        if row.get("schema") != HOST_PROOF_DEMAND_SCHEMA:
            continue
        if require_live:
            expected = row.get("process_identity")
            if not expected or process_birth_identity(row.get("pid")) != expected:
                continue
        rows.append({**row, "path": str(path)})
    return rows


def shelf_demand_path(profile_root: Path, attempt_id: str) -> Path:
    safe = "".join(character for character in attempt_id if character.isalnum() or character in {"-", "_"})
    if not safe:
        raise ValueError("shelf demand attempt id is empty")
    return profile_root / "admission-demands" / f"{safe}.json"


def write_shelf_demand(profile_root: Path, owner: dict[str, Any]) -> Path:
    attempt_id = str(owner.get("attempt_id") or "")
    path = shelf_demand_path(profile_root, attempt_id)
    atomic_write_json(
        path,
        {
            "schema": SHELF_DEMAND_SCHEMA,
            "attempt_id": attempt_id,
            "logical_profile": owner.get("logical_profile"),
            "owner_kind": owner.get("owner_kind"),
            "source": owner.get("source"),
            "pid": owner.get("pid"),
            "process_identity": owner.get("process_identity"),
            "created_utc": utc_now(),
        },
    )
    return path


def clear_shelf_demand(profile_root: Path, attempt_id: str) -> None:
    shelf_demand_path(profile_root, attempt_id).unlink(missing_ok=True)


def read_shelf_demands(profile_root: Path, *, require_live: bool = True) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    root = profile_root / "admission-demands"
    for path in sorted(root.glob("*.json")):
        try:
            row = read_json(path)
        except (OSError, TypeError, ValueError):
            continue
        if row.get("schema") != SHELF_DEMAND_SCHEMA:
            continue
        if require_live:
            expected = row.get("process_identity")
            if not expected or process_birth_identity(row.get("pid")) != expected:
                continue
        rows.append(row)
    return rows
