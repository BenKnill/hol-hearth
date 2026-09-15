"""Dormant dispatcher composition for an owned CRIU lazy-pages restore."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from hol_workbench.criu_lazy_pages_restore import run_owned_lazy_pages_restore
from hol_workbench.criu_restore_launcher import restore_signal_guard

PUBLIC_LAZY_PAGES_ENABLED = False

RestoreTransaction = Callable[..., int]


def dispatch_restore_transaction(
    args: Any,
    *,
    image_dir: Path,
    pool: Path,
    recorded: dict[str, dict[str, int]],
    operation_stamp: str,
    log: Path,
    pidfile: Path,
    restore_options: list[str],
    execute_restore_transaction: RestoreTransaction,
    display_pool: Path | None = None,
    durable_image_dir: Path | None = None,
) -> int:
    """Dispatch eager or owned lazy restore while the caller holds the pool lock."""

    def transaction(options: list[str]) -> int:
        return execute_restore_transaction(
            args,
            image_dir=image_dir,
            durable_image_dir=durable_image_dir or image_dir,
            pool=pool,
            display_pool=display_pool or pool,
            recorded=recorded,
            operation_stamp=operation_stamp,
            log=log,
            pidfile=pidfile,
            restore_options=options,
        )

    if not args.lazy_pages:
        return transaction(restore_options)
    with restore_signal_guard() as signals:
        return run_owned_lazy_pages_restore(
            image_dir,
            operation_stamp=operation_stamp,
            execute_restore=lambda lazy_pages_options: transaction([*restore_options, *lazy_pages_options]),
            check_interrupted=signals.raise_if_interrupted,
        )
