#!/usr/bin/env python3
"""Import every production Python module in a fresh, isolated interpreter.

Syntax compilation cannot detect a renamed module, a missing imported symbol,
or an import cycle that only fails from a clean process.  Keep this probe
stdlib-only so Dune can run it before any developer environment is available.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOTS = ("fake_hol", "hol_workbench")
IMPORT_TIMEOUT_SECONDS = 8.0
MAX_WORKERS = 8
IMPORT_CODE = "import importlib,sys; sys.path.insert(0, sys.argv[1]); importlib.import_module(sys.argv[2])"


@dataclass(frozen=True)
class ImportFailure:
    module: str
    reason: str
    detail: str = ""


def _workbench_root() -> Path:
    if len(sys.argv) > 2:
        raise SystemExit("usage: python_import_selftest.py [WORKBENCH_ROOT]")
    if len(sys.argv) == 2:
        return Path(sys.argv[1]).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _module_name(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _production_modules(root: Path) -> list[str]:
    modules: set[str] = set()
    for package in PACKAGE_ROOTS:
        package_root = root / package
        if not package_root.is_dir():
            raise SystemExit(f"python-import-integrity: missing package directory: {package_root}")
        for path in package_root.rglob("*.py"):
            if "__pycache__" not in path.parts:
                modules.add(_module_name(root, path))
    if not modules:
        raise SystemExit("python-import-integrity: no production modules found")
    return sorted(modules)


def _probe(root: Path, module: str) -> ImportFailure | None:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    command = [sys.executable, "-I", "-B", "-c", IMPORT_CODE, str(root), module]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=IMPORT_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ImportFailure(module, f"timed out after {IMPORT_TIMEOUT_SECONDS:g}s")

    detail = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode != 0:
        return ImportFailure(module, f"exited {completed.returncode}", detail)
    if detail:
        return ImportFailure(module, "wrote output during import", detail)
    return None


def main() -> int:
    root = _workbench_root()
    modules = _production_modules(root)
    workers = min(MAX_WORKERS, len(modules))

    def probe(module: str) -> ImportFailure | None:
        return _probe(root, module)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        failures = [failure for failure in executor.map(probe, modules) if failure is not None]

    if failures:
        for failure in failures:
            print(
                f"python-import-integrity: module={failure.module} reason={failure.reason}",
                file=sys.stderr,
            )
            if failure.detail:
                for line in failure.detail.splitlines():
                    print(f"  {line}", file=sys.stderr)
        print(
            f"python-import-integrity: failures={len(failures)} modules={len(modules)}",
            file=sys.stderr,
        )
        return 1

    print(f"python-import-integrity: modules={len(modules)} isolated=yes status=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
