"""Import shim for modules stored under the workbench source tree."""

from pathlib import Path

_WORKBENCH = Path(__file__).resolve().parents[1]
if _WORKBENCH.exists():
    __path__.append(str(_WORKBENCH))
