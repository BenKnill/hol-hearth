"""Mechanical single-thread admission for a restored Unix.fork basis."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

FORK_BASIS_PRELOAD_CONTRACT = "basis preloads must not create OCaml Domains or Thread threads before Unix.fork"


class ForkBasisSafetyError(RuntimeError):
    def __init__(self, pid: int, *, thread_count: int | None, detail: str) -> None:
        self.pid = pid
        self.thread_count = thread_count
        super().__init__(
            f"fork basis admission refused: basis_pid={pid} "
            f"os_thread_count={thread_count if thread_count is not None else 'unavailable'}; {detail}"
        )


def admit_single_threaded_basis(
    pid: int,
    *,
    proc_root: Path = Path("/proc"),
    platform: str = sys.platform,
    allow_non_linux_test_basis: bool = False,
) -> dict[str, Any]:
    if platform != "linux":
        if allow_non_linux_test_basis:
            return {
                "status": "test_basis_non_linux",
                "basis_pid": pid,
                "os_thread_count": None,
                "required_os_thread_count": 1,
            }
        raise ForkBasisSafetyError(pid, thread_count=None, detail=f"platform {platform!r} has no /proc task check")
    task_dir = proc_root / str(pid) / "task"
    try:
        thread_count = sum(1 for item in task_dir.iterdir() if item.name.isdigit())
    except OSError as exc:
        raise ForkBasisSafetyError(pid, thread_count=None, detail=f"cannot inspect {task_dir}: {exc}") from exc
    if thread_count != 1:
        raise ForkBasisSafetyError(
            pid,
            thread_count=thread_count,
            detail="basis preloads may have spawned an OCaml Domain or Thread",
        )
    return {
        "status": "passed_active_thread_check",
        "basis_pid": pid,
        "os_thread_count": thread_count,
        "required_os_thread_count": 1,
        "preload_contract": FORK_BASIS_PRELOAD_CONTRACT,
    }
