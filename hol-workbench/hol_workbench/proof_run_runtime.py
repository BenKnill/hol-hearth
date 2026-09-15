"""Runtime/startup helpers for the proof-run CLI."""

from __future__ import annotations

import os
import re
import secrets
import shlex
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from hol_workbench.jsonio import append_jsonl
from hol_workbench.runtime_cache import workbench_cache_root


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return slug[:80] or "proof"


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id(slug: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{os.getpid()}-{slugify(slug)}-{secrets.token_hex(3)}"


def write_event(
    path: Path, *, event_schema: str, sequence: int, kind: str, source: str = "proof-run", **fields
) -> None:
    event = {
        "schema": event_schema,
        "sequence": sequence,
        "kind": kind,
        "source": source,
        "unix_time": time.time(),
        **fields,
    }
    append_jsonl(path, event)


def shell_command(raw: str) -> list[str]:
    return ["bash", "-lc", raw]


def default_hol_command(holdir: Path, source: Path) -> list[str]:
    script = r"""
set -e
export HOLLIGHT_DIR="$1"
export HOLLIGHT_USE_MODULE=1
export LINE_EDITOR=cat
export PATH="$1/_opam/bin:$PATH"
export CAML_LD_LIBRARY_PATH="$1/_opam/lib/stublibs${CAML_LD_LIBRARY_PATH:+:$CAML_LD_LIBRARY_PATH}"
exec "$1"/ocaml-hol -I "$1" -init "$1"/hol.ml -I . < "$2"
""".strip()
    return ["bash", "-lc", script, "_", str(holdir), str(source)]


def default_warm_startup_command(holdir: Path) -> list[str]:
    return [str(holdir / "ocaml-hol"), "-I", str(holdir), "-init", str(holdir / "hol.ml"), "-I", "."]


def cached_fake_holdir_path() -> Path:
    return workbench_cache_root() / "fake-holdir"


def fake_holdir_install_command(*, workbench_dir: Path) -> str:
    return shlex.join(
        [
            "env",
            f"PYTHONPATH={workbench_dir}",
            sys.executable,
            "-m",
            "fake_hol.install",
            str(cached_fake_holdir_path()),
        ]
    )


def startup_executable_problem(startup_argv: list[str], *, cwd: Path) -> str | None:
    if not startup_argv:
        return "empty HOL startup command"
    executable = startup_argv[0]
    if os.sep in executable or (os.altsep and os.altsep in executable):
        path = Path(executable)
        candidate = path if path.is_absolute() else cwd / path
        if not candidate.exists():
            return f"missing startup executable: {candidate}"
        if not os.access(candidate, os.X_OK):
            return f"startup executable is not executable: {candidate}"
        return None
    if shutil.which(executable) is None:
        return f"startup executable not found on PATH: {executable}"
    return None


def render_hol_startup_blocked_lines(
    *,
    command_name: str,
    holdir: Path,
    cwd: Path,
    startup_argv: list[str],
    problem: str,
    workbench_dir: Path,
) -> list[str]:
    fake_holdir = cached_fake_holdir_path()
    return [
        f"{command_name} blocked before HOL launch",
        f"reason: {problem}",
        f"holdir: {holdir}",
        f"cwd: {cwd}",
        f"startup argv: {shlex.join(startup_argv) if startup_argv else '<empty>'}",
        "",
        "next steps:",
        "- for real proof work, install/build HOL Light or pass --holdir /path/to/hol-light",
        "- for VM/fake-HOL workflow testing only, install a fake HOLDIR explicitly:",
        f"  {fake_holdir_install_command(workbench_dir=workbench_dir)}",
        f"  rerun with: --holdir {shlex.quote(str(fake_holdir))}",
        "- fake HOL exercises harness behavior; it is not HOL theorem evidence",
    ]


def print_hol_startup_blocked(
    *,
    command_name: str,
    holdir: Path,
    cwd: Path,
    startup_argv: list[str],
    problem: str,
    workbench_dir: Path,
) -> None:
    for line in render_hol_startup_blocked_lines(
        command_name=command_name,
        holdir=holdir,
        cwd=cwd,
        startup_argv=startup_argv,
        problem=problem,
        workbench_dir=workbench_dir,
    ):
        print(line, file=sys.stderr)
