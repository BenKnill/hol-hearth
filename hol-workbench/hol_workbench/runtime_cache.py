"""Stable Linux-local cache paths for Workbench runtime state."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

WORKBENCH_CACHE_NAMESPACE = "hol-workbench"


def _absolute_home(value: Path, *, label: str) -> Path:
    candidate = value.expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{label} must be an absolute Linux path; got {str(value)!r}")
    return candidate.resolve()


def xdg_cache_home(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return an absolute XDG cache home without inheriting the caller's cwd.

    XDG requires ``XDG_CACHE_HOME`` to be absolute. A relative value is ignored
    instead of being resolved against whichever directory launched Workbench.
    """

    active = os.environ if environment is None else environment
    configured = str(active.get("XDG_CACHE_HOME") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
    configured_home = home
    if configured_home is None:
        environment_home = str(active.get("HOME") or "").strip()
        configured_home = Path(environment_home) if environment_home else Path.home()
    ubuntu_home = _absolute_home(configured_home, label="home" if home is not None else "HOME")
    return (ubuntu_home / ".cache").resolve()


def workbench_cache_root(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the single cache namespace owned by this Workbench install."""

    return xdg_cache_home(environment=environment, home=home) / WORKBENCH_CACHE_NAMESPACE
