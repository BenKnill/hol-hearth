"""Small terminal-only formatting helpers for public compact surfaces."""

from __future__ import annotations

import shlex
from pathlib import Path

from hol_workbench.machine_client import preferred_tool_command


def display_path(value: str | Path, *, cwd: Path | None = None) -> str:
    """Use a cwd-relative path when it stays directly actionable."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        return str(path)
    base = (cwd or Path.cwd()).expanduser().resolve()
    try:
        relative = path.resolve().relative_to(base)
    except (OSError, ValueError):
        return str(path)
    return str(relative) if relative.parts else "."


def compact_tool_command(tool: str, target: str | Path, *args: str) -> str:
    """Render one tool command whose target survives the selected front door."""
    command = preferred_tool_command(tool)
    target_path = Path(target).expanduser()
    rendered_target = str(target_path.resolve()) if Path(command).is_absolute() else display_path(target_path)
    return shlex.join([command, rendered_target, *args])
