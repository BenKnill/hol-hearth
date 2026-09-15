"""Cwd-independent subprocess entry into this Workbench checkout."""

from __future__ import annotations

import sys
from os import PathLike
from pathlib import Path

CHECKOUT_MODULE_BOOTSTRAP = (
    "import runpy,sys; root=sys.argv.pop(1); module=sys.argv.pop(1); "
    "sys.path[:0]=[root]; runpy.run_module(module,run_name='__main__')"
)


def checkout_module_command(
    module: str,
    *arguments: str | PathLike[str],
    package_root: Path | None = None,
    python: str | PathLike[str] | None = None,
) -> list[str]:
    """Return an isolated command whose imports do not depend on cwd or PYTHONPATH.

    Preserve ``sys.executable`` exactly: resolving a virtual-environment symlink
    can silently leave that environment and lose its installed dependencies.
    """

    if not module.startswith("hol_workbench."):
        raise ValueError(f"checkout module must be inside hol_workbench: {module!r}")
    root = (package_root or Path(__file__).resolve().parent.parent).resolve()
    executable = str(python) if python is not None else sys.executable
    if not Path(executable).is_absolute():
        raise ValueError(f"checkout Python must be absolute: {executable!r}")
    return [
        executable,
        "-I",
        "-B",
        "-c",
        CHECKOUT_MODULE_BOOTSTRAP,
        str(root),
        module,
        *(str(argument) for argument in arguments),
    ]
