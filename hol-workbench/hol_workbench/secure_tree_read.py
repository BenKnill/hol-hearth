"""No-follow reads beneath one trusted Linux directory."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SecureFile:
    data: bytes
    size: int


def open_directory_nofollow(path: Path) -> int:
    """Open an absolute directory after rejecting symlinks in every component."""

    absolute = Path(os.path.abspath(path))
    if not absolute.is_absolute():
        raise OSError(f"trusted directory is not absolute: {path}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    current = os.open("/", flags)
    try:
        for component in absolute.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=current)
            os.close(current)
            current = next_descriptor
        return current
    except BaseException:
        os.close(current)
        raise


def read_regular_file_beneath(root: Path, path: Path) -> SecureFile:
    """Read ``path`` through dirfds without following any path-component symlink."""

    lexical_root = Path(os.path.abspath(root))
    lexical_path = Path(os.path.abspath(path))
    try:
        relative = lexical_path.relative_to(lexical_root)
    except ValueError as exc:
        raise OSError(f"path escapes trusted root: {lexical_path}") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError(f"path is not a clean file below trusted root: {lexical_path}")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptors: list[int] = []
    try:
        current = open_directory_nofollow(lexical_root)
        descriptors.append(current)
        for component in relative.parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
        leaf = os.open(relative.parts[-1], file_flags, dir_fd=current)
        descriptors.append(leaf)
        metadata = os.fstat(leaf)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"path is not a regular file: {lexical_path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(leaf, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        data = b"".join(chunks)
        if len(data) != metadata.st_size:
            raise OSError(f"file size changed during no-follow read: {lexical_path}")
        return SecureFile(data=data, size=metadata.st_size)
    finally:
        for descriptor in reversed(descriptors):
            with suppress(OSError):
                os.close(descriptor)
