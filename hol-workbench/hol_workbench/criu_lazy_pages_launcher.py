"""Internal CRIU lazy-pages transaction; public restore remains disabled."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from hol_workbench.criu_lazy_pages_lifecycle import (
    abort_launcher,
    cleanup_failed_launch,
    clear_verified_daemon_artifacts,
    prepare_launch,
    recover_lazy_pages_launch,
    retire_prior_owner,
)
from hol_workbench.criu_lazy_pages_owner import (
    bind_daemon_identity,
    owner_record,
    terminate_bound_daemon,
    terminate_owned_daemon,
    write_owner,
)
from hol_workbench.criu_lazy_pages_protocol import (
    STATUS_FD,
    LazyPagesLauncher,
    _close_fd,
    _validate_paths,
    lazy_pages_command,
    lazy_pages_image_lock,
    observe_status_nul,
    release_gated_lazy_pages,
    start_gated_lazy_pages,
    wait_lazy_pages_parent,
)
from hol_workbench.criu_lazy_pages_records import (
    FAILURE_PHASES,
    FAILURE_SCHEMA,
    PENDING_NAME,
    PENDING_SCHEMA,
    RECOVERY_SCHEMA,
    write_bound_pending,
    write_launch_failure,
    write_pending_launch,
    write_recovery_receipt,
)
from hol_workbench.jsonio import durable_unlink
from hol_workbench.process_groups import process_birth_identity, process_start_ticks

__all__ = (
    "FAILURE_PHASES",
    "FAILURE_SCHEMA",
    "PENDING_NAME",
    "PENDING_SCHEMA",
    "RECOVERY_SCHEMA",
    "STATUS_FD",
    "LazyPagesLauncher",
    "_close_fd",
    "abort_launcher",
    "cleanup_failed_launch",
    "launch_lazy_pages_internal",
    "lazy_pages_command",
    "lazy_pages_image_lock",
    "observe_status_nul",
    "prepare_launch",
    "recover_lazy_pages_launch",
    "release_gated_lazy_pages",
    "retire_prior_owner",
    "start_gated_lazy_pages",
    "wait_lazy_pages_parent",
    "write_bound_pending",
    "write_launch_failure",
    "write_pending_launch",
    "write_recovery_receipt",
)


def launch_lazy_pages_internal(
    image_dir: Path,
    *,
    pidfile: Path,
    log: Path,
    status_timeout_seconds: float = 5.0,
    parent_timeout_seconds: float = 5.0,
    start: Callable[[Sequence[str]], LazyPagesLauncher] = start_gated_lazy_pages,
    observe: Callable[..., dict[str, Any]] = observe_status_nul,
    wait_parent: Callable[..., None] = wait_lazy_pages_parent,
    release: Callable[[LazyPagesLauncher], None] = release_gated_lazy_pages,
    bind: Callable[..., dict[str, Any]] = bind_daemon_identity,
    terminate_prior: Callable[..., dict[str, Any]] = terminate_owned_daemon,
    terminate_bound: Callable[..., dict[str, Any]] = terminate_bound_daemon,
    publish: Callable[[Path, dict[str, Any]], Path] = write_owner,
    controller_pid: int | None = None,
    controller_start_ticks_reader: Callable[[Any], int | None] = process_start_ticks,
    controller_identity_reader: Callable[[Any], str | None] = process_birth_identity,
) -> dict[str, Any]:
    """Run the internal launch transaction; callers must keep public restore disabled."""
    root, resolved_pidfile, resolved_log = _validate_paths(image_dir, pidfile, log)
    command = lazy_pages_command(root, pidfile=resolved_pidfile, log=resolved_log)
    launcher: LazyPagesLauncher | None = None
    daemon: dict[str, Any] | None = None
    pending: Path | None = None
    phase = "preflight"
    with lazy_pages_image_lock(root):
        try:
            prepare_launch(root, pidfile=resolved_pidfile, terminate=terminate_prior)
            phase = "spawn"
            launcher = start(command)
            phase = "pending_publication"
            effective_controller_pid = controller_pid or os.getpid()
            controller_ticks = controller_start_ticks_reader(effective_controller_pid)
            controller_identity = controller_identity_reader(effective_controller_pid)
            if (
                not isinstance(controller_ticks, int)
                or controller_ticks <= 0
                or not isinstance(controller_identity, str)
                or controller_identity != f"linux-proc:{controller_ticks}"
            ):
                raise RuntimeError("lazy-pages controller has no stable Linux birth identity")
            pending = write_pending_launch(
                root,
                pidfile=resolved_pidfile,
                log=resolved_log,
                launcher=launcher,
                controller_pid=effective_controller_pid,
                controller_start_ticks=controller_ticks,
                controller_identity=controller_identity,
            )
            phase = "gate_release"
            release(launcher)
            phase = "status_wait"
            readiness = observe(launcher, timeout_seconds=status_timeout_seconds)
            phase = "parent_reap"
            wait_parent(launcher, timeout_seconds=parent_timeout_seconds)
            phase = "daemon_bind"
            daemon = bind(resolved_pidfile)
            phase = "daemon_handoff"
            write_bound_pending(root, daemon)
            phase = "owner_publication"
            record = owner_record(
                image_dir=root,
                pidfile=resolved_pidfile,
                log=resolved_log,
                daemon=daemon,
                readiness=readiness,
            )
            publish(root, record)
        except BaseException as exc:
            daemon_attempt, cleanup = cleanup_failed_launch(
                launcher,
                pidfile=resolved_pidfile,
                bound_daemon=daemon,
                terminate=terminate_bound,
            )
            receipt = write_launch_failure(
                root,
                phase=phase,
                error=exc,
                launcher=launcher,
                pidfile=resolved_pidfile,
                log=resolved_log,
                daemon_attempt=daemon_attempt,
                cleanup=cleanup,
            )
            if cleanup.get("verified_daemon_quiescent") is True and cleanup.get("verified_quiescent") is True:
                clear_verified_daemon_artifacts(root, pidfile=resolved_pidfile, pending=pending)
            exc.add_note(f"lazy-pages failure receipt: {receipt}")
            raise
        if pending is None or launcher is None:
            raise AssertionError("published lazy-pages owner has no launch transaction anchors")
        try:
            durable_unlink(pending)
        except OSError as exc:
            with suppress(OSError, RuntimeError):
                write_recovery_receipt(
                    root,
                    status="owner_ready_pending_retained",
                    pending_record=json.loads(pending.read_text(encoding="utf-8")),
                    owner=record,
                    daemon_attempt=record["daemon"],
                    cleanup={
                        "status": "not_attempted",
                        "verified_quiescent": False,
                        "reason": f"pending intent clear failed after owner publication: {type(exc).__name__}: {exc}",
                    },
                )
        _close_fd(launcher, "status_read_fd")
        return record
