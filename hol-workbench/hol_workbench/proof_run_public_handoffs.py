"""Linux-local handoffs emitted by the public proof path."""

from __future__ import annotations

from hol_workbench.machine_client import preferred_tool_command


def host_tool_command(tool: str, *, fallback: str | None = None) -> str:
    """Return the Linux-local public tool spelling."""

    return fallback or preferred_tool_command(tool, platform="linux")


def host_handoff_shell(shell: str) -> str:
    return shell


def displayed_cold_replay(replay_shell: str) -> str:
    _ = replay_shell
    return "developer replay plan retained in internal JSON; send DETAILS to the Workbench dev lane"
