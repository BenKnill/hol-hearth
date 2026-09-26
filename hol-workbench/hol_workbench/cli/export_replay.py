"""Write a plain HOL Light replay script from a recorded receipt; never run it."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from hol_workbench.cli.inspect import _replay_receipt
from hol_workbench.fork_child_ocaml import ocaml_string_literal
from hol_workbench.hashing import sha256_bytes, short_sha256
from hol_workbench.runtime_config import RuntimeConfigError, load_runtime_config


class ExportError(ValueError):
    """The receipt does not describe a plain replay that can be written without guessing."""


CAPTURED = {"source_local", "source_overlay", "holdir_source", "mounted_source"}


def _load_path_entries(receipt: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Directories that make every recorded relative load resolve as it did, plus notes."""
    closure = receipt.get("source_dependency_closure") or {}
    source = str(receipt.get("source") or "")
    entries: list[str] = []
    notes: list[str] = []

    def add(directory: str) -> None:
        if directory and directory not in entries:
            entries.append(directory)

    add(str(Path(source).parent))
    profile_edges = {
        (edge.get("declared_path"), edge.get("declaring_file"))
        for edge in receipt.get("profile_satisfied_dependencies") or []
    }
    for record in closure.get("records") or []:
        declared = str(record.get("declared_path") or "")
        resolution = record.get("resolution")
        resolved = record.get("resolved_path")
        if (declared, record.get("declaring_file")) in profile_edges:
            continue  # resolves through the profile cwd, which is the script's cwd
        if resolution in CAPTURED and isinstance(resolved, str) and declared and not Path(declared).is_absolute():
            resolved_posix = Path(resolved).as_posix()
            if resolved_posix.endswith("/" + declared):
                add(resolved_posix[: -len(declared) - 1])
            else:
                notes.append(f"{declared} resolved to {resolved}; its load-path base could not be derived")
        elif resolution not in CAPTURED:
            notes.append(f"{declared}: {resolution}; HOL must resolve it itself")
    return entries, notes


def build_script(receipt_path: Path, receipt: dict[str, Any], *, holdir_override: Path | None = None) -> str:
    if receipt.get("evidence") != "recorded_warm_replay":
        raise ExportError("requires a recorded warm replay receipt")
    source = receipt.get("source")
    if not isinstance(source, str) or not Path(source).is_absolute():
        raise ExportError("receipt has no absolute entrypoint source")
    profile_cwd = receipt.get("profile_cwd")
    profile_base = receipt.get("profile_base")
    satisfaction = receipt.get("profile_satisfaction") if isinstance(receipt.get("profile_satisfaction"), dict) else {}
    holdir = holdir_override or satisfaction.get("host_holdir")
    if holdir is None:
        try:
            holdir = load_runtime_config().hol_light_dir
        except RuntimeConfigError as exc:
            raise ExportError(f"no HOL directory is recorded and the runtime config is unavailable: {exc}") from exc
    holdir = str(holdir)
    if not profile_cwd:
        profile_cwd = str(Path(source).parent)
    entries, notes = _load_path_entries(receipt)
    proved = [row["name"] for row in receipt.get("bindings") or []
              if isinstance(row, dict) and row.get("status") == "proved" and row.get("name")]
    closure_sha = receipt.get("source_dependency_closure_sha256")
    lines = [
        "#!/usr/bin/env bash",
        f"# Plain HOL Light replay of {source}",
        f"# Written by hearth export-replay from {receipt_path}",
        f"# Receipt SHA-256: {sha256_bytes(json.dumps(receipt, sort_keys=True).encode())[:16]}...  "
        f"source sha={short_sha256(str(receipt.get('source_sha256') or '')) or '?'}  "
        f"closure sha={short_sha256(str(closure_sha or '')) or '?'}",
        f"# Profile: {receipt.get('logical_profile')} basis={receipt.get('profile_basis_id')} "
        f"recipe={profile_base}",
        "#",
        "# This is the independent check before publication: a cold HOL Light without",
        "# Hearth, CRIU or the warm broker loads the same profile recipe, then the exact",
        "# source. It reproduces the load environment; it does not read the receipt's",
        "# evidence. Compare the printed axiom count before and after, and the theorems.",
    ]
    for note in notes:
        lines.append(f"# NOTE: {note}")
    lines += [
        "set -euo pipefail",
        f"HOLDIR={shlex.quote(holdir)}",
        f"cd {shlex.quote(str(profile_cwd))}",
        f"export HOLLIGHT_LOAD_PATH={shlex.quote(':'.join(entries))}",
        "# hol.sh wraps the toplevel in ledit, which garbles piped input; env just execs it.",
        "export LINE_EDITOR=env",
        'exec "$HOLDIR/hol.sh" <<\'HOL\'',
        "let hearth_export_axioms_before = List.length (axioms());;",
    ]
    if isinstance(profile_base, str) and profile_base:
        lines.append(f"loadt {ocaml_string_literal(profile_base)};;")
    lines.append(f"loadt {ocaml_string_literal(source)};;")
    lines.append('Printf.printf "HEARTH_EXPORT_REPLAY: source loaded\\n%!";;')
    for name in proved:
        lines.append(f"{name};;")
    lines += [
        'Printf.printf "HEARTH_EXPORT_REPLAY: axioms before=%d after=%d\\n%!" '
        "hearth_export_axioms_before (List.length (axioms()));;",
        "HOL",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hearth export-replay",
        description="Write the plain HOL Light command that reproduces a receipt's load environment; never run HOL.",
        allow_abbrev=False,
    )
    parser.add_argument("run", type=Path, help="receipt, attempt directory, or root (newest receipt)")
    parser.add_argument("--out", type=Path, help="new executable .sh file; default prints the script")
    parser.add_argument("--holdir", type=Path, help="HOL Light directory when the receipt records none")
    args = parser.parse_args(argv)
    receipt_path = _replay_receipt(args.run.expanduser())
    if receipt_path is None:
        print("export-replay: refused: no recorded replay receipt found; pass a receipt, attempt directory or run root",
              file=sys.stderr)
        return 2
    try:
        receipt = json.loads(receipt_path.read_bytes())
        if not isinstance(receipt, dict):
            raise ExportError("receipt is not a JSON object")
        script = build_script(receipt_path.resolve(), receipt, holdir_override=args.holdir)
        if args.out is None:
            sys.stdout.write(script)
            return 0
        out = Path(os.path.abspath(args.out.expanduser()))
        if out.exists() or out.is_symlink():
            raise ExportError(f"--out already exists; choose a new file: {out}")
        out.write_text(script, encoding="utf-8")
        out.chmod(0o755)
    except (OSError, ValueError, TypeError) as exc:
        print(f"export-replay: refused: {exc}", file=sys.stderr)
        return 2
    print(f"EXPORTED: {out}")
    print(f"SOURCE: {receipt.get('source')} sha={short_sha256(str(receipt.get('source_sha256') or '')) or '?'}")
    print("NOTE: the script starts a cold HOL Light outside Hearth; it reproduces the load environment only")
    print(f"NEXT: bash {shlex.quote(str(out))} 2>&1 | tee {shlex.quote(str(out) + '.log')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
