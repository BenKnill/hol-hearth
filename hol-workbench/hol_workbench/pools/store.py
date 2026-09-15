"""Storage and locking primitives for warm pool metadata."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from hol_workbench.ids import utc_now
from hol_workbench.jsonio import atomic_write_json, read_json
from hol_workbench.pools.status import pool_status_summary as _pool_status_summary


def pool_json_path(pool_dir: Path) -> Path:
    return pool_dir / "pool.json"


@contextmanager
def warm_pool_lock(pool_dir: Path) -> Iterator[None]:
    pool_dir.mkdir(parents=True, exist_ok=True)
    with (pool_dir / "pool.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def read_warm_pool(pool_dir: Path) -> dict[str, Any]:
    data = read_json(pool_json_path(pool_dir))
    if not data:
        raise SystemExit(f"warm pool metadata not found: {pool_json_path(pool_dir)}")
    return data


def write_warm_pool_json(pool_dir: Path, pool: dict[str, Any], *, updated_utc: str | None = None) -> Path:
    pool["updated_utc"] = updated_utc or utc_now()
    path = pool_json_path(pool_dir)
    atomic_write_json(path, pool)
    return path


def pool_status_summary(pool: dict[str, Any]) -> dict[str, int]:
    return _pool_status_summary(pool)
