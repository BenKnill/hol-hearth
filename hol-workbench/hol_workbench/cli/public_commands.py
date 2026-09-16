"""Runnable public commands, independent of the proof project's working directory."""
from __future__ import annotations
import os
import shlex
from pathlib import Path

def public_command(tool: str, *args: str | Path) -> str:
    launcher = Path(os.environ.get("HOL_HEARTH_LAUNCHER", Path(__file__).resolve().parents[3] / "hearth"))
    return shlex.join([str(launcher), tool, *(str(arg) for arg in args)])

def replay_handoff(run_root: Path, *, succeeded: bool) -> tuple[str, str]:
    next_step = ("continue authoring; rerun at the next milestone" if succeeded
                 else "inspect DETAILS, edit the original source, and rerun the same prove command")
    return f"NEXT: {next_step}", f"DETAILS: {public_command('inspect', run_root)}"
