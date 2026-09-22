"""Per-user lexical analysis cache, bound to the installed parser runtime."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import unicodedata

from hol_workbench.runtime_cache import xdg_cache_home


def _runtime_parser_digest(root: Path) -> str | None:
    # Bind the complete public Python runtime rather than maintaining a second
    # list of the strict scanner's transitive imports. Paths are relative so
    # identical installations share lexical results; source/project paths and
    # resolution decisions are never cached here.
    try:
        sources = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*.py"))
            if not any(part in {"tests", "__pycache__"} for part in path.relative_to(root).parts)
        }
    except OSError:
        return None
    if not sources:
        return None
    identity = {
        "schema": "hol-hearth.source-analysis-runtime.v1",
        "sources": sources,
        "python": sys.implementation.cache_tag,
        "unicode": unicodedata.unidata_version,
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# Keep a running watcher in its loaded parser generation. Rehashing after an
# on-disk runtime edit would let old imported code publish into the new parser's
# namespace. Restarting the command loads the new code and selects a new cache.
_RUNTIME_PARSER_DIGEST = _runtime_parser_digest(Path(__file__).resolve().parent)


def source_analysis_cache_root() -> Path | None:
    """Return an optional lexical-only cache; the scanner still hashes all inputs."""

    if _RUNTIME_PARSER_DIGEST is None:
        return None
    try:
        return xdg_cache_home() / "hol-hearth" / "source-analysis" / _RUNTIME_PARSER_DIGEST
    except (OSError, ValueError):
        # Cache location problems must not prevent the ordinary uncached scan.
        return None
