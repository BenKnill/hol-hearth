"""Bound successful CRIU restore residue without deleting failure evidence."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

from hol_workbench.jsonio import read_json

DEFAULT_KEEP = 8


def _protected_restore_logs(image_dir: Path, pool: Path) -> set[Path]:
    protected: set[Path] = set()

    def bind_recorded_log(raw: object) -> None:
        if not isinstance(raw, str) or not raw:
            return
        name = Path(raw).name
        if name.startswith("restore-") and Path(name).suffix == ".log":
            protected.add(image_dir / name)

    for receipt in image_dir.glob("restore-failed-*.json"):
        row = read_json(receipt)
        bind_recorded_log(row.get("restore_log"))
    for receipt in image_dir.glob("restore-retry-*.json"):
        row = read_json(receipt)
        bind_recorded_log(row.get("first_restore_log"))
    transaction = read_json(pool / "pool.json").get("restore_transaction") or {}
    raw = transaction.get("restore_log") if isinstance(transaction, dict) else None
    bind_recorded_log(raw)
    return protected


def _prune(paths: list[Path], *, keep: int, protected: set[Path]) -> list[str]:
    if keep < 1:
        raise ValueError("restore residue retention must keep at least one file")
    regular: list[tuple[Path, int]] = []
    for path in paths:
        try:
            observed = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(observed.st_mode):
            regular.append((path, observed.st_mtime_ns))
    ordered = sorted(regular, key=lambda item: (item[1], item[0].name), reverse=True)
    deleted: list[str] = []
    retained_unprotected = 0
    for path, _mtime_ns in ordered:
        if path in protected:
            continue
        retained_unprotected += 1
        if retained_unprotected <= keep:
            continue
        path.unlink(missing_ok=True)
        deleted.append(str(path))
    return deleted


def prune_successful_restore_residue(*, image_dir: Path, pool: Path, keep: int = DEFAULT_KEEP) -> dict[str, Any]:
    """Keep recent success logs/backups; preserve every referenced failure log."""

    protected = _protected_restore_logs(image_dir, pool)
    deleted_logs = _prune(list(image_dir.glob("restore-*.log")), keep=keep, protected=protected)
    deleted_backups = _prune(
        list(pool.glob("pool.json.before-criu-restore-*")),
        keep=keep,
        protected=set(),
    )
    return {
        "keep": keep,
        "deleted_restore_logs": deleted_logs,
        "deleted_pool_backups": deleted_backups,
        "protected_failure_logs": sorted(str(path) for path in protected),
    }
