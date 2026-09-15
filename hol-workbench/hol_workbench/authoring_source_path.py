"""Caller-cwd vs legacy checkout resolution for public authoring SOURCE paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from hol_workbench.hashing import sha256_bytes

CALLER_CWD_ENV = "HOL_WORKBENCH_CALLER_CWD"
_SHA_PREFIX = 12


class AuthoringSourcePathError(Exception):
    """Relative SOURCE cannot be resolved without profile or HOL work."""


@dataclass(frozen=True)
class AuthoringSourceResolution:
    source: Path
    selection: str  # absolute | caller | legacy
    caller_cwd: Path | None


def guest_visible_caller_cwd(
    env: dict[str, str] | None = None,
    *,
    require_dir: bool = True,
) -> Path | None:
    """Return the absolute guest-visible caller cwd, or None when unusable."""

    raw = (env if env is not None else os.environ).get(CALLER_CWD_ENV)
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if require_dir and not resolved.is_dir():
        return None
    return resolved


def _regular_file(path: Path) -> Path | None:
    try:
        if path.is_file():
            return path
    except OSError:
        return None
    return None


def _candidate_digest(path: Path, *, role: str) -> str:
    """Hash one ambiguity candidate, refusing rather than raising a raw OSError."""

    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise AuthoringSourcePathError(
            f"relative SOURCE is ambiguous and the {role} candidate is unreadable; "
            f"{role}={path} error={exc.strerror or exc.__class__.__name__}"
        ) from exc


def resolve_authoring_source(
    source_arg: str | os.PathLike[str],
    *,
    legacy_cwd: str | os.PathLike[str],
    env: dict[str, str] | None = None,
) -> AuthoringSourceResolution:
    """Resolve SOURCE for public prove/loop without inventing paths.

    Absolute SOURCE is unchanged. Relative SOURCE compares a guest-visible
    caller-cwd candidate against the legacy guest-checkout cwd candidate.
    """

    legacy_root = Path(legacy_cwd).expanduser().resolve()
    source = Path(source_arg).expanduser()
    if source.is_absolute():
        resolved = source.resolve()
        return AuthoringSourceResolution(
            source=resolved,
            selection="absolute",
            caller_cwd=guest_visible_caller_cwd(env),
        )

    caller_root = guest_visible_caller_cwd(env)
    legacy_candidate = (legacy_root / source).resolve()
    caller_candidate = (caller_root / source).resolve() if caller_root is not None else None

    legacy_file = _regular_file(legacy_candidate)
    caller_file = _regular_file(caller_candidate) if caller_candidate is not None else None

    if caller_file is not None and legacy_file is None:
        return AuthoringSourceResolution(
            source=caller_file,
            selection="caller",
            caller_cwd=caller_root,
        )
    if legacy_file is not None and caller_file is None:
        return AuthoringSourceResolution(
            source=legacy_file,
            selection="legacy",
            caller_cwd=caller_root,
        )
    if caller_file is not None and legacy_file is not None:
        if caller_file == legacy_file:
            return AuthoringSourceResolution(
                source=caller_file,
                selection="caller",
                caller_cwd=caller_root,
            )
        caller_digest = _candidate_digest(caller_file, role="caller")
        legacy_digest = _candidate_digest(legacy_file, role="checkout")
        if caller_digest == legacy_digest:
            return AuthoringSourceResolution(
                source=caller_file,
                selection="caller",
                caller_cwd=caller_root,
            )
        raise AuthoringSourcePathError(
            "relative SOURCE resolves to different bytes at caller and checkout paths; "
            f"caller={caller_file} sha256={caller_digest[:_SHA_PREFIX]} "
            f"checkout={legacy_file} sha256={legacy_digest[:_SHA_PREFIX]}"
        )

    # Preserve legacy candidate behavior when caller is unusable or neither exists.
    return AuthoringSourceResolution(
        source=legacy_candidate,
        selection="legacy",
        caller_cwd=caller_root,
    )


def resolve_authoring_run_root(
    run_root_arg: str | os.PathLike[str] | None,
    *,
    legacy_cwd: str | os.PathLike[str],
    source_resolution: AuthoringSourceResolution,
) -> Path:
    """Resolve --run-root; default moves only for caller-selected relative SOURCE."""

    legacy_root = Path(legacy_cwd).expanduser().resolve()
    if run_root_arg is not None:
        root = Path(run_root_arg).expanduser()
        if not root.is_absolute():
            root = legacy_root / root
        return root.resolve()
    if source_resolution.selection == "caller" and source_resolution.caller_cwd is not None:
        return (source_resolution.caller_cwd / "runs").resolve()
    return (legacy_root / "runs").resolve()
