#!/usr/bin/env python3
"""Portable, stdlib-only checks. This never starts HOL or CRIU."""
from __future__ import annotations
import ast
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WB = ROOT / "hol-workbench"

def main() -> int:
    if sys.platform != "linux" or sys.version_info < (3, 11):
        raise SystemExit("Linux and Python 3.11+ are required")
    unexpected = set()
    modules = list((WB / "hol_workbench").rglob("*.py"))
    for path in modules:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = [n.name for n in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else [])
            for name in names:
                first = name.split(".")[0]
                if first not in sys.stdlib_module_names and first not in {"hol_workbench", "fake_hol"}:
                    unexpected.add(first)
    if unexpected:
        raise SystemExit(f"Non-stdlib runtime imports: {sorted(unexpected)}")
    print(f"Runtime imports: {len(modules)} modules, standard library only", flush=True)
    commands = [
        [sys.executable, "-I", "-B", str(ROOT / "dev/progress-selftest.py")],
        [sys.executable, "-I", "-B", str(ROOT / "dev/check-provenance.py")],
        [sys.executable, "-I", "-B", str(ROOT / "dev/setup-selftest.py")],
        [sys.executable, "-I", "-B", str(ROOT / "dev/authoring-selftest.py")],
        [sys.executable, "-I", "-B", str(WB / "dev/python_syntax_selftest.py"), str(WB)],
        [sys.executable, "-I", "-B", str(WB / "dev/python_import_selftest.py"), str(WB)],
        [str(ROOT / "hearth"), "smoke"],
    ]
    for name in ("source_byte_identity", "source_dependency", "logical_source_roots",
                 "criu_contract", "loader_contract", "memory_first_lifecycle"):
        commands.append([sys.executable, "-I", "-B", "-c",
                         "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_path(sys.argv[1],run_name='__main__')",
                         str(WB), str(WB / f"dev/{name}_selftest.py")])
    for command in commands:
        print("CHECK " + Path(command[-1]).name, flush=True)
        result = subprocess.run(command, cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                check=False, timeout=120)
        if result.returncode:
            return result.returncode
    for path in [ROOT / "hearth", ROOT / "dev/linux", *(WB / "bin").iterdir()]:
        if path.is_file() and path.read_bytes().startswith(b"#!/"):
            subprocess.run(["bash", "-n", str(path)], check=True)
    print("HOL Hearth check: passed (harness evidence; HOL and CRIU were not started)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
