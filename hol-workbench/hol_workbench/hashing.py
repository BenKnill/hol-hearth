"""Hashing helpers used by workbench artifacts and proof metadata."""

from __future__ import annotations

import hashlib
from pathlib import Path

SHORT_SHA256_CHARS = 12
_HEX_DIGITS = frozenset("0123456789abcdef")


def normalized_sha256(digest: str | None) -> str | None:
    """Return one lowercase 64-hex digest, or None when the value is unusable."""
    if not isinstance(digest, str):
        return None
    value = digest.strip().lower()
    if len(value) != 64 or not _HEX_DIGITS.issuperset(value):
        return None
    return value


def short_sha256(digest: str | None) -> str | None:
    """Return the compact display prefix shared by public byte-identity fields."""
    value = normalized_sha256(digest)
    return None if value is None else value[:SHORT_SHA256_CHARS]


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def sha256_file_strict(path: Path, *, chunk_size: int = 1024 * 1024) -> str | None:
    """Hash one existing file while allowing read failures to reach the caller."""
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
