"""Run the cold-free HOL parser with the selected HOLDIR toolchain."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from hol_workbench.ubuntu_runtime_layout import UbuntuRuntimeLayoutError, resolve_orb_holdir

SYSTEM_TOOL_DIRS = (Path("/usr/local/bin"), Path("/usr/bin"), Path("/bin"))


def _unique_paths(paths: list[str]) -> str:
    return os.pathsep.join(dict.fromkeys(path for path in paths if path))


def syntax_preflight_environment(
    holdir: Path,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a deterministic selected-switch environment for parser tools."""
    source = os.environ if environ is None else environ
    env = dict(source)
    switch_prefix = holdir / "_opam"
    tool_bin = switch_prefix / "bin"
    stublibs = switch_prefix / "lib" / "stublibs"

    # Never allow another active switch or a hostile caller PATH to select the
    # compiler/preprocessor.  A system compiler is the deliberate fallback for
    # HOLDIR switches based on ocaml-system.
    env["PATH"] = _unique_paths([str(tool_bin), *(str(path) for path in SYSTEM_TOOL_DIRS)])
    env["OPAM_SWITCH_PREFIX"] = str(switch_prefix)
    env["CAML_LD_LIBRARY_PATH"] = _unique_paths(
        [str(stublibs), *source.get("CAML_LD_LIBRARY_PATH", "").split(os.pathsep)]
    )
    local_toplevel = switch_prefix / "lib" / "toplevel"
    if local_toplevel.is_dir():
        env["OCAML_TOPLEVEL_PATH"] = str(local_toplevel)
    return env


def _selected_tool(name: str, *, env: Mapping[str, str]) -> Path | None:
    resolved = shutil.which(name, path=env["PATH"])
    if resolved is None:
        return None
    tool = Path(resolved)
    return tool if tool.is_file() else None


def _failure_lines(completed: subprocess.CompletedProcess[str]) -> str:
    lines = [line for line in ((completed.stderr or "") + "\n" + (completed.stdout or "")).splitlines() if line.strip()]
    return " | ".join(lines[-3:])


def hol_syntax_preflight(source: Path, *, environ: Mapping[str, str] | None = None) -> str | None:
    """Return a compact upstream HOL parser failure without starting a warm run."""
    active_environ = os.environ if environ is None else environ
    source = source.expanduser().resolve()
    try:
        holdir = resolve_orb_holdir(active_environ).path
    except UbuntuRuntimeLayoutError as exc:
        return f"HOL syntax preflight configuration invalid: {exc}"
    parser_extension = holdir / "pa_j.cmo"
    stublibs = holdir / "_opam" / "lib" / "stublibs"
    env = syntax_preflight_environment(holdir, environ=active_environ)
    tools = {name: _selected_tool(name, env=env) for name in ("camlp5", "camlp5r", "ocamlc")}
    missing = [name for name, tool in tools.items() if tool is None]
    if missing:
        return (
            f"HOL syntax toolchain unavailable for HOLDIR {holdir}: missing {', '.join(missing)}; "
            f"searched selected switch first: {holdir / '_opam' / 'bin'}"
        )
    if not stublibs.is_dir():
        return f"HOL syntax stublibs unavailable: {stublibs}"
    if not parser_extension.is_file():
        return f"HOL parser extension unavailable: {parser_extension}"

    camlp5 = tools["camlp5"]
    camlp5r = tools["camlp5r"]
    ocamlc = tools["ocamlc"]
    assert camlp5 is not None and camlp5r is not None and ocamlc is not None
    try:
        camlp5_where = subprocess.run(
            [str(camlp5), "-where"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"HOL syntax preflight tool unavailable: {type(exc).__name__}: {exc}"
    if camlp5_where.returncode != 0:
        detail = (camlp5_where.stderr or "").strip().splitlines()
        return "HOL syntax preflight setup failed" + (f": {detail[-1]}" if detail else "")

    preprocessor = shlex.join(
        [str(camlp5r), "pa_lexer.cmo", "pa_extend.cmo", "q_MLast.cmo", "-I", str(holdir), "pa_j.cmo"]
    )
    try:
        camlp_parse = subprocess.run(
            [
                str(camlp5r),
                "pa_lexer.cmo",
                "pa_extend.cmo",
                "q_MLast.cmo",
                "-I",
                str(holdir),
                "pa_j.cmo",
                str(source),
            ],
            cwd=source.parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"HOL syntax preflight did not run: {type(exc).__name__}: {exc}"
    if camlp_parse.returncode != 0:
        return _failure_lines(camlp_parse) or f"HOL parser exited {camlp_parse.returncode}"

    try:
        parsed = subprocess.run(
            [
                str(ocamlc),
                "-stop-after",
                "parsing",
                "-safe-string",
                "-pp",
                preprocessor,
                "-I",
                camlp5_where.stdout.strip(),
                str(source),
            ],
            cwd=source.parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"HOL syntax preflight did not run: {type(exc).__name__}: {exc}"
    if parsed.returncode == 0:
        return None
    return _failure_lines(parsed) or f"HOL parser exited {parsed.returncode}"
