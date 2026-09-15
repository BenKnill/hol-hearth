#!/usr/bin/env python3
"""Compile every Workbench Python source as text without writing bytecode."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def _python_launchers(bin_dir: Path) -> list[Path]:
    launchers: list[Path] = []
    for candidate in sorted(bin_dir.iterdir()):
        if not candidate.is_file():
            continue
        with candidate.open("r", encoding="utf-8") as handle:
            first_line = handle.readline()
        if first_line.startswith("#!") and "python" in first_line:
            launchers.append(candidate)
    return launchers


def _label(workbench: Path, path: Path) -> str:
    try:
        return str(path.relative_to(workbench))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "linux":
        print("python-syntax-selftest: Linux-only; refusing to run outside Linux", file=sys.stderr)
        return 2

    args = list(argv or [])
    if len(args) != 1:
        print("usage: python_syntax_selftest.py WORKBENCH", file=sys.stderr)
        return 2
    workbench = Path(args[0]).expanduser().resolve()
    roots = [
        workbench / "hol_workbench",
        workbench / "dev",
        workbench / "fake_hol",
    ]
    missing = [str(root) for root in roots if not root.is_dir()]
    if missing:
        print(f"python-syntax-selftest: missing source roots: {', '.join(missing)}", file=sys.stderr)
        return 2

    modules = sorted({path for root in roots for path in root.rglob("*.py")})
    launchers = _python_launchers(workbench / "bin")
    sources = sorted(set(modules) | set(launchers))
    failures: list[str] = []
    for source in sources:
        label = _label(workbench, source)
        try:
            text = source.read_text(encoding="utf-8")
            ast.parse(text, filename=label, feature_version=(3, 11))
            compile(text, label, "exec")
        except UnicodeDecodeError as exc:
            failures.append(f"{label}: invalid UTF-8 at byte {exc.start}: {exc.reason}")
        except SyntaxError as exc:
            line = exc.lineno or 1
            column = exc.offset or 1
            failures.append(f"{label}:{line}:{column}: {exc.msg}")

    if failures:
        for failure in failures:
            print(f"SYNTAX ERROR {failure}", file=sys.stderr)
        print(f"python_syntax_selftest=failed files={len(sources)} errors={len(failures)}", file=sys.stderr)
        return 1

    print(
        "python_syntax_selftest=passed "
        f"files={len(sources)} modules={len(modules)} python_launchers={len(launchers)} grammar=3.11"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
