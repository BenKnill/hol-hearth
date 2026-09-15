#!/usr/bin/env python3
"""Cheap fake-first workbench smoke command."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TextIO

SCRIPT_DIR = Path(__file__).resolve().parents[2] / "bin"
WORKBENCH_DIR = SCRIPT_DIR.parent
REPOSITORY_ROOT = WORKBENCH_DIR.parent

HELP = """\
usage: hol-workbench/bin/smoke

Cheap current-contract smoke. This checks the public command boundary and
bounded receipt reader without loading HOL or touching a warm profile.
"""

SUCCESS = """\
SMOKE: passed current public contract
EVIDENCE: harness health only; no theorem claim
NEXT: ./hearth doctor (or ./hearth setup for a new environment)
DETAILS: ./hearth --help
"""

SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"


def smoke_command(
    *,
    workbench_dir: Path = WORKBENCH_DIR,
    repository_root: Path = REPOSITORY_ROOT,
) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-B",
        str(workbench_dir / "hol_workbench" / "public_surface_contract.py"),
        str(repository_root),
    ]


def smoke_subprocess_environment(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Normalize transitive fake-harness tools onto the Linux system path."""

    env = dict(os.environ if environ is None else environ)
    env["PATH"] = SYSTEM_PATH
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for variable in ("PYTHONHOME", "PYTHONPATH", "PYTHONPYCACHEPREFIX"):
        env.pop(variable, None)
    return env


def main(
    argv: list[str] | None = None,
    *,
    execv=os.execv,
    runner=subprocess.run,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    script_dir: Path = SCRIPT_DIR,
) -> int:
    args = sys.argv[1:] if argv is None else argv
    if sys.platform != "linux":
        print("smoke: Linux-only runtime; no host-side action was started", file=stderr)
        return 2
    if args[:1] in (["-h"], ["--help"]):
        print(HELP, end="", file=stdout)
        return 0
    if args:
        print(
            "smoke: public smoke accepts no tier options; "
            "real-HOL, integration, heavy, and stress tiers are developer-only",
            file=stderr,
        )
        return 2

    workbench_dir = script_dir.parent
    command = smoke_command(workbench_dir=workbench_dir, repository_root=workbench_dir.parent)
    _ = execv

    completed = runner(
        command,
        check=False,
        env=smoke_subprocess_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if completed.returncode != 0:
        print(completed.stdout or "smoke failed without captured output", end="", file=stdout)
        return int(completed.returncode)
    print(SUCCESS, end="", file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
