"""Time, slug, and run-id helpers for workbench artifacts."""

from __future__ import annotations

import os
import re
import secrets
from datetime import UTC, datetime


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def slugify(text: str, *, fallback: str = "item", max_chars: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", text.strip()).strip("-")
    return slug[:max_chars] or fallback


def run_id(label: str, *, fallback: str = "run", nonce_bytes: int = 0) -> str:
    parts = [stamp(), str(os.getpid()), slugify(label, fallback=fallback)]
    if nonce_bytes:
        parts.append(secrets.token_hex(nonce_bytes))
    return "-".join(parts)
