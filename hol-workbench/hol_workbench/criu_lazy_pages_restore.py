"""Internal owned lazy-pages/restore composition; public restore stays disabled."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from hol_workbench.criu_lazy_pages_launcher import launch_lazy_pages_internal
from hol_workbench.criu_lazy_pages_lifecycle import recover_lazy_pages_launch, retire_prior_owner
from hol_workbench.criu_lazy_pages_owner import terminate_owned_daemon, write_failure_receipt

LAZY_PAGES_RESTORE_OPTION = "--lazy-pages"
_SAFE_STAMP = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\Z")

RestoreCall = Callable[[Sequence[str]], int]


def _owner_acquired_noop(_record: dict[str, Any]) -> None:
    pass


def _interruption_checkpoint_noop() -> None:
    pass


def _require_recovery_allows_launch(recovery: dict[str, Any]) -> None:
    status = recovery.get("status")
    has_retained_artifacts = bool(recovery.get("artifact_clear_error") or recovery.get("pending_clear_error"))
    if status == "clear":
        return
    if status == "owner_ready" and not has_retained_artifacts:
        return
    if status == "quiescent" and recovery.get("verified_quiescent") is True and not has_retained_artifacts:
        return
    raise RuntimeError(
        "prior lazy-pages launch recovery is not safe to replace: "
        f"status={status or 'unknown'} reason={recovery.get('reason') or 'unavailable'}"
    )


def _settle_owner(
    image_dir: Path,
    record: dict[str, Any],
    *,
    terminate: Callable[[dict[str, Any]], dict[str, Any]],
    retire: Callable[..., dict[str, Any]],
) -> tuple[dict[str, Any], BaseException | None]:
    """Settle one owner, returning any interrupt so the caller can receipt and re-raise it."""
    try:
        cleanup = dict(terminate(record))
    except BaseException as exc:
        cleanup = {
            "status": "unverified",
            "verified_quiescent": False,
            "artifacts_cleared": False,
            "reason": f"lazy-pages daemon cleanup raised {type(exc).__name__}: {exc}",
            "surviving_pids": [record["daemon"]["pid"]],
        }
        return cleanup, exc if not isinstance(exc, Exception) else None
    cleanup["artifacts_cleared"] = False
    if cleanup.get("verified_quiescent") is not True:
        return cleanup, None
    try:
        retire(image_dir, record, terminate=lambda _record: cleanup)
    except BaseException as exc:
        cleanup["status"] = "cleanup_verified_artifacts_retained"
        cleanup["artifact_cleanup_error"] = f"{type(exc).__name__}: {exc}"
        return cleanup, exc if not isinstance(exc, Exception) else None
    cleanup["artifacts_cleared"] = True
    return cleanup, None


def _failure_reason(
    *,
    restore_result: int | None,
    operation_error: BaseException | None,
    cleanup_interrupt: BaseException | None,
) -> str:
    failures = []
    if operation_error is not None:
        failures.append(f"restore operation {type(operation_error).__name__}: {operation_error}")
    if cleanup_interrupt is not None:
        failures.append(f"lazy-pages cleanup {type(cleanup_interrupt).__name__}: {cleanup_interrupt}")
    if failures:
        return "; ".join(failures)
    if restore_result == 0:
        return "restore succeeded but lazy-pages cleanup was incomplete"
    return f"restore returned nonzero status {restore_result}"


def run_owned_lazy_pages_restore(
    image_dir: Path,
    *,
    operation_stamp: str,
    execute_restore: RestoreCall,
    recover: Callable[[Path], dict[str, Any]] = recover_lazy_pages_launch,
    launch: Callable[..., dict[str, Any]] = launch_lazy_pages_internal,
    terminate: Callable[[dict[str, Any]], dict[str, Any]] = terminate_owned_daemon,
    retire: Callable[..., dict[str, Any]] = retire_prior_owner,
    write_failure: Callable[..., Path] = write_failure_receipt,
    owner_acquired: Callable[[dict[str, Any]], None] = _owner_acquired_noop,
    check_interrupted: Callable[[], None] = _interruption_checkpoint_noop,
) -> int:
    """Compose the internal launcher with restore and exact-daemon teardown.

    The public dispatcher wires this helper only behind its disabled feature
    gate.  This keeps one tested transaction boundary without weakening the
    current fail-closed ``--lazy-pages`` preflight.
    """
    if not _SAFE_STAMP.fullmatch(operation_stamp):
        raise ValueError("lazy-pages operation stamp contains unsupported characters")
    root = image_dir.resolve()
    recovery = recover(root)
    _require_recovery_allows_launch(recovery)
    check_interrupted()
    record: dict[str, Any] | None = None
    restore_result: int | None = None
    operation_error: BaseException | None = None
    cleanup: dict[str, Any] | None = None
    cleanup_interrupt: BaseException | None = None
    try:
        record = launch(
            root,
            pidfile=root / f"lazy-pages-{operation_stamp}.pid",
            log=root / f"lazy-pages-{operation_stamp}.log",
        )
        owner_acquired(record)
        check_interrupted()
        restore_result = execute_restore((LAZY_PAGES_RESTORE_OPTION,))
        if isinstance(restore_result, bool) or not isinstance(restore_result, int):
            raise TypeError("owned lazy-pages restore callback must return an integer status")
        check_interrupted()
    except BaseException as exc:
        operation_error = exc
    finally:
        if record is not None:
            cleanup, cleanup_interrupt = _settle_owner(root, record, terminate=terminate, retire=retire)
            try:
                check_interrupted()
            except BaseException as exc:
                if operation_error is None and cleanup_interrupt is None:
                    operation_error = exc

    if record is None:
        if operation_error is None:
            raise AssertionError("lazy-pages launch returned without an owner record")
        raise operation_error
    if cleanup is None:
        raise AssertionError("lazy-pages owner escaped its settlement region")

    if (
        operation_error is None
        and cleanup_interrupt is None
        and restore_result == 0
        and cleanup.get("artifacts_cleared") is True
    ):
        return 0

    phase = "restore_cleanup"
    if cleanup_interrupt is not None:
        phase = "cleanup_interrupted"
    elif operation_error is not None:
        phase = "restore_interrupted" if not isinstance(operation_error, Exception) else "restore_exception"
    elif restore_result:
        phase = "restore_failed"
    reason = _failure_reason(
        restore_result=restore_result,
        operation_error=operation_error,
        cleanup_interrupt=cleanup_interrupt,
    )
    final_error = cleanup_interrupt or operation_error
    if cleanup_interrupt is not None and operation_error is not None:
        cleanup_interrupt.add_note(f"restore operation also raised {type(operation_error).__name__}: {operation_error}")
    try:
        receipt = write_failure(root, phase=phase, record=record, cleanup=cleanup, reason=reason)
    except BaseException as receipt_error:
        if not isinstance(receipt_error, Exception):
            if final_error is not None:
                receipt_error.add_note(f"pending lifecycle error was {type(final_error).__name__}: {final_error}")
            raise
        if final_error is not None:
            final_error.add_note(
                f"lazy-pages failure receipt could not be written: {type(receipt_error).__name__}: {receipt_error}"
            )
            raise final_error from receipt_error
        raise RuntimeError("lazy-pages failure receipt could not be written") from receipt_error

    if final_error is not None:
        final_error.add_note(f"lazy-pages failure receipt: {receipt}")
        raise final_error
    if restore_result:
        return restore_result
    raise RuntimeError(f"lazy-pages restore cleanup is incomplete; failure receipt: {receipt}")
