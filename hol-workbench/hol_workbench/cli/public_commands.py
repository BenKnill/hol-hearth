"""Short, host-safe commands for public result cards."""

from __future__ import annotations

import shlex
from pathlib import Path


def public_command(tool: str, *args: str | Path) -> str:
    return shlex.join(["./hearth", tool, *(str(arg) for arg in args)])


def replay_handoff(run_root: Path, *, succeeded: bool) -> tuple[str, str]:
    next_step = (
        "continue authoring; record another replay at the next milestone"
        if succeeded
        else "inspect DETAILS, edit the source, and rerun the same prove command"
    )
    return f"NEXT: {next_step}", f"DETAILS: {public_command('inspect', run_root)}"


def loop_remediation(card: str) -> str | None:
    if card.startswith("FAIL "):
        return "NEXT: edit the source and save again"
    if card.startswith("TIMEOUT "):
        return "NEXT: stop the loop and inspect Workbench status"
    if card.startswith("REFUSED "):
        return "NEXT: stop the loop; do not reload or rebuild the profile"
    return None
