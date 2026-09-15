"""Small filesystem, JSON, and text helpers for workbench artifacts."""

from __future__ import annotations

import errno
import json
import os
import secrets
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

JsonObject = dict[str, Any]


class PathContainmentError(ValueError):
    """A requested publication path is not one safe child of its root."""


class TreePublicationError(RuntimeError):
    """A staged tree could not be committed or rolled back safely."""


@dataclass(frozen=True)
class StagedTree:
    root: Path
    final: Path
    path: Path


@dataclass(frozen=True)
class PublishReceipt:
    final: Path
    replaced: bool
    quarantine: Path | None
    cleanup_error: str | None


def confined_child(root: Path, leaf: str) -> Path:
    """Return one non-symlink direct child confined beneath an existing root."""
    resolved_root = root.expanduser().resolve()
    if not resolved_root.is_dir():
        raise PathContainmentError(f"publication root is not a directory: {resolved_root}")
    if not leaf or leaf in {".", ".."} or "/" in leaf or "\\" in leaf or Path(leaf).name != leaf:
        raise PathContainmentError(f"publication leaf is unsafe: {leaf!r}")
    child = resolved_root / leaf
    if child.is_symlink():
        raise PathContainmentError(f"publication target must not be a symlink: {child}")
    if child.resolve(strict=False).parent != resolved_root:
        raise PathContainmentError(f"publication target escapes root {resolved_root}: {child}")
    return child


def _remove_tree(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


@contextmanager
def stage_tree(root: Path, leaf: str) -> Iterator[StagedTree]:
    """Create a collision-resistant candidate tree beside its final destination."""
    requested_root = root.expanduser()
    requested_root.mkdir(parents=True, exist_ok=True)
    resolved_root = requested_root.resolve()
    final = confined_child(resolved_root, leaf)
    staging = Path(tempfile.mkdtemp(prefix=f".{leaf}.staging-", dir=resolved_root)).resolve()
    staged = StagedTree(root=resolved_root, final=final, path=staging)
    try:
        yield staged
    finally:
        _remove_tree(staging)


def publish_tree(
    staged: StagedTree,
    *,
    validate: Callable[[Path], None],
    replace: bool = False,
) -> PublishReceipt:
    """Validate and commit a staged tree, restoring the prior tree on failure."""
    root = staged.root.resolve()
    final = confined_child(root, staged.final.name)
    staging = staged.path
    if staging.is_symlink() or not staging.is_dir() or staging.parent.resolve() != root:
        raise PathContainmentError(f"staging tree is not a real direct child of {root}: {staging}")
    if final != staged.final:
        raise PathContainmentError(f"staged final path changed: {staged.final} -> {final}")

    validate(staging)
    if staging.is_symlink() or not staging.is_dir() or staging.parent.resolve() != root:
        raise PathContainmentError(f"validator changed the staging tree identity: {staging}")
    final = confined_child(root, staged.final.name)
    final_exists = final.exists() or final.is_symlink()
    if final_exists and not replace:
        raise FileExistsError(f"publication target already exists: {final}")
    if final_exists and not final.is_dir():
        raise TreePublicationError(f"publication target is not a directory: {final}")

    quarantine: Path | None = None
    replaced = False
    try:
        if final_exists:
            quarantine = confined_child(root, f".{final.name}.previous-{secrets.token_hex(12)}")
            os.replace(final, quarantine)
            replaced = True
        os.replace(staging, final)
    except BaseException as publish_error:
        if (
            isinstance(publish_error, OSError)
            and publish_error.errno in {errno.EEXIST, errno.ENOTEMPTY}
            and not replace
            and quarantine is None
            and (final.exists() or final.is_symlink())
        ):
            raise FileExistsError(errno.EEXIST, f"publication target already exists: {final}", final) from publish_error
        if quarantine is not None and quarantine.exists() and not final.exists():
            try:
                os.replace(quarantine, final)
            except BaseException as rollback_error:
                raise TreePublicationError(
                    f"publication failed and rollback also failed for {final}: "
                    f"publish={publish_error}; rollback={rollback_error}"
                ) from rollback_error
        raise

    cleanup_error = None
    if quarantine is not None:
        try:
            _remove_tree(quarantine)
        except OSError as exc:
            cleanup_error = f"{type(exc).__name__}: {exc}"
    return PublishReceipt(
        final=final,
        replaced=replaced,
        quarantine=quarantine if cleanup_error else None,
        cleanup_error=cleanup_error,
    )


def read_json(path: Path, *, default: JsonObject | None = None) -> JsonObject:
    """Read a JSON object, returning ``default`` for missing or invalid files."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {} if default is None else dict(default)
    return data if isinstance(data, dict) else ({} if default is None else dict(default))


def read_json_strict(path: Path) -> JsonObject:
    """Read a JSON object and propagate file/parse/type errors."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"expected JSON object in {path}")
    return data


def read_jsonl(path: Path) -> list[JsonObject]:
    """Read JSONL objects, skipping blank lines and invalid/non-object rows."""
    rows: list[JsonObject] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def append_jsonl(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(data), sort_keys=True) + "\n")


def json_text(data: Mapping[str, Any]) -> str:
    return json.dumps(dict(data), indent=2, sort_keys=True) + "\n"


def write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_text(data), encoding="utf-8")


def _write_unique_atomic_temp(path: Path, text: str, *, durable: bool) -> Path:
    """Create one collision-resistant same-directory staging file."""
    for _attempt in range(10):
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            with tmp.open("x", encoding="utf-8") as handle:
                handle.write(text)
                if durable:
                    handle.flush()
                    os.fsync(handle.fileno())
            return tmp
        except FileExistsError:
            continue
    raise FileExistsError(f"could not allocate unique atomic staging path for {path}")


def atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_unique_atomic_temp(path, json_text(data), durable=False)
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def durable_atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    """Atomically replace JSON after syncing both file contents and directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _write_unique_atomic_temp(path, json_text(data), durable=True)
    try:
        tmp.replace(path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        tmp.unlink(missing_ok=True)


def durable_unlink(path: Path) -> bool:
    """Remove one path and sync its parent directory; return false if absent."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return True


def write_text_lines(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
