#!/usr/bin/env python3
"""Check reviewed ML/recipe bytes, not the legal status of arbitrary new code."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = (
    "hol-workbench/warmup-profiles.json",
    "hol-workbench/dev/warmup-profiles-developer.json",
    "dev/setup-lock.json",
)
IGNORED = {".git", "_build", "_opam", ".venv", "runs", ".cache", ".ruff_cache", "__pycache__"}


def source_files() -> set[str]:
    # Git includes tracked files even when an ignore rule would hide them.
    # Source archives have no Git metadata, so inspect their packaged tree.
    if (ROOT / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT, check=True, stdout=subprocess.PIPE,
        )
        return {os.fsdecode(name) for name in result.stdout.split(b"\0") if name}
    files = set()
    for directory, dirs, names in os.walk(ROOT):
        dirs[:] = [name for name in dirs if name not in IGNORED]
        files.update(str((Path(directory) / name).relative_to(ROOT)) for name in names)
    return files


def main() -> int:
    inventory = json.loads((ROOT / "docs/source-provenance.json").read_text())
    reviewed = inventory["files"]
    ml_files = {name for name in source_files() if Path(name).suffix in {".ml", ".mli", ".hl"}}
    expected = ml_files | set(MANIFESTS)
    if expected != set(reviewed):
        raise SystemExit(
            "Provenance inventory differs: "
            f"unreviewed={sorted(expected - set(reviewed))}, "
            f"missing={sorted(set(reviewed) - expected)}"
        )
    for name, entry in reviewed.items():
        path = ROOT / name
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"Reviewed source must be a regular file: {name}")
        if not entry.get("origin") or not entry.get("license"):
            raise SystemExit(f"Missing provenance: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise SystemExit(f"Source changed since provenance review: {name}")
    public = json.loads((ROOT / MANIFESTS[0]).read_text())
    for name, profile in public["profiles"].items():
        expected_bytes = ("\n".join(profile["base_lines"]) + "\n").encode()
        if (ROOT / "profiles" / f"{name}.ml").read_bytes() != expected_bytes:
            raise SystemExit(f"Published ML recipe differs from runtime manifest: {name}")
    print(f"Provenance inventory: {len(ml_files)} ML files and {len(MANIFESTS)} profile/source manifests match reviewed bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
