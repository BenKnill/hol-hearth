"""Long-lived lifecycle lock shared by pool stop and CRIU restore."""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def warm_pool_lifecycle_lock(pool_dir: Path) -> Iterator[None]:
    pool_dir.mkdir(parents=True, exist_ok=True)
    with (pool_dir / "lifecycle.lock").open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
