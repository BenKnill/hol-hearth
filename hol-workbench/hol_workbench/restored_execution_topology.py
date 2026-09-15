"""Typed process topologies for restored HOL execution."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class RestoredExecutionTopology(StrEnum):
    """Who owns policy around one restored mathematical basis."""

    MECHANICAL_BASIS_BROKER_V3 = "mechanical_basis_broker_v3"


FORK_SNAPSHOT_ABI_V3 = "proof-run-fork-snapshot.v3"
LEGACY_V2_SNAPSHOT = "legacy_v2_snapshot"


def restored_execution_topology(data: dict[str, Any]) -> RestoredExecutionTopology:
    """Return the only executable topology and reject every legacy snapshot."""

    raw = data.get("execution_topology")
    if raw in (None, "", "embedded_fork_manager_v2"):
        raise RuntimeError(
            "legacy v2 snapshot is not executable; republish this profile once "
            "with the v3 mechanical broker, then rerun the same prove command"
        )
    try:
        return RestoredExecutionTopology(str(raw))
    except ValueError as exc:
        raise RuntimeError(f"unknown restored execution topology: {raw!r}") from exc


def fork_snapshot_abi_for_topology(topology: RestoredExecutionTopology) -> str:
    if topology is not RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3:
        raise RuntimeError(f"unsupported restored execution topology: {topology!r}")
    return FORK_SNAPSHOT_ABI_V3
