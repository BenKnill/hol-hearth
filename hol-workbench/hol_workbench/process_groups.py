"""Bounded process-group lifecycle helpers for owned warm workers."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from hol_workbench.jsonio import atomic_write_json
from hol_workbench.processes import pid_int, process_is_alive

PROCESS_IDENTITY_FIELDS = (
    "worker_pid",
    "worker_pgid",
    "worker_start_ticks",
    "worker_identity",
    "basis_pid",
    "basis_pgid",
    "basis_start_ticks",
    "basis_identity",
)
CONTEXT_OWNERSHIP_DIR_ENV = "HOL_WORKBENCH_CONTEXT_OWNERSHIP_DIR"
CONTEXT_OWNED_PROCESS_SCHEMA = "hol-workbench.context-owned-process.v1"
PROCESS_NAMESPACE_ENV = "HOL_WORKBENCH_PROCESS_NAMESPACE"
ORBSTACK_MACHINE_ENV = "HOL_WORKBENCH_ORB_MACHINE"


def _context_ownership_path(identity: ProcessIdentity) -> Path | None:
    root = os.environ.get(CONTEXT_OWNERSHIP_DIR_ENV)
    if not root:
        return None
    return Path(root).expanduser().resolve() / f"{identity.pid}.json"


def _write_context_ownership(owned: OwnedProcess, *, transferred: bool) -> None:
    path = _context_ownership_path(owned.identity)
    if path is None:
        return
    process_namespace = os.environ.get(PROCESS_NAMESPACE_ENV, "local")
    atomic_write_json(
        path,
        {
            "schema": CONTEXT_OWNED_PROCESS_SCHEMA,
            "pid": owned.identity.pid,
            "pgid": owned.identity.pgid,
            "birth_identity": owned.identity.birth_identity,
            "start_ticks": owned.identity.start_ticks,
            "registered_by_pid": os.getpid(),
            "ownership_transferred": transferred,
            "process_namespace": process_namespace,
            "orbstack_machine": (
                os.environ.get(ORBSTACK_MACHINE_ENV, "dev") if process_namespace == "orbstack" else None
            ),
        },
    )


def _remove_context_ownership(identity: ProcessIdentity) -> None:
    path = _context_ownership_path(identity)
    if path is not None:
        path.unlink(missing_ok=True)


def process_group_is_alive(pgid: Any) -> bool:
    value = pid_int(pgid)
    if value is None:
        return False
    try:
        os.killpg(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_start_ticks(pid: Any) -> int | None:
    """Return Linux /proc start ticks, which disambiguate reused PIDs."""
    value = pid_int(pid)
    if value is None:
        return None
    try:
        raw = Path(f"/proc/{value}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        _, separator, tail = raw.rpartition(")")
        if not separator:
            return None
        return int(tail.split()[19])
    except (IndexError, ValueError):
        return None


def process_birth_identity(
    pid: Any,
    *,
    start_ticks_reader: Callable[[Any], int | None] = process_start_ticks,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """Return a portable process-birth identity suitable for PID reuse checks."""
    value = pid_int(pid)
    if value is None:
        return None
    ticks = start_ticks_reader(value)
    if ticks is not None:
        return f"linux-proc:{ticks}"
    try:
        completed = run(
            ["ps", "-o", "lstart=", "-p", str(value)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    started = completed.stdout.strip() if completed.returncode == 0 else ""
    return f"ps-lstart:{started}" if started else None


def _positive_unique(values: Iterable[Any]) -> list[int]:
    return sorted({value for item in values if (value := pid_int(item)) is not None})


def wait_for_quiescence(
    *,
    pids: Iterable[Any] = (),
    pgids: Iterable[Any] = (),
    timeout_seconds: float,
    poll_seconds: float = 0.05,
    pid_alive: Callable[[Any], bool] = process_is_alive,
    group_alive: Callable[[Any], bool] = process_group_is_alive,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    targets_pids = _positive_unique(pids)
    targets_pgids = _positive_unique(pgids)
    started = monotonic()
    deadline = started + max(0.0, float(timeout_seconds))
    while True:
        live_pids = [pid for pid in targets_pids if pid_alive(pid)]
        live_pgids = [pgid for pgid in targets_pgids if group_alive(pgid)]
        if not live_pids and not live_pgids:
            return {
                "status": "quiescent",
                "elapsed_seconds": round(monotonic() - started, 3),
                "live_pids": [],
                "live_pgids": [],
            }
        now = monotonic()
        if now >= deadline:
            return {
                "status": "survivors",
                "elapsed_seconds": round(now - started, 3),
                "live_pids": live_pids,
                "live_pgids": live_pgids,
            }
        sleep(min(max(0.0, poll_seconds), max(0.0, deadline - now)))


def signal_process_group(pgid: Any, signum: int) -> bool:
    value = pid_int(pgid)
    if value is None:
        return False
    if value == os.getpgrp():
        raise RuntimeError(f"refusing to signal own process group {value}")
    try:
        os.killpg(value, signum)
    except ProcessLookupError:
        return False
    return True


def terminate_process_group(
    pgid: Any,
    *,
    identity_pid: Any = None,
    expected_identity: str | None = None,
    expected_start_ticks: Any = None,
    require_identity: bool = False,
    allow_dead_identity_group_continuity: bool = False,
    term_grace_seconds: float = 1.0,
    kill_grace_seconds: float = 1.0,
    signal_group: Callable[[Any, int], bool] = signal_process_group,
    group_alive: Callable[[Any], bool] = process_group_is_alive,
    identity_reader: Callable[[Any], int | None] = process_start_ticks,
    birth_identity_reader: Callable[[Any], str | None] = process_birth_identity,
) -> dict[str, Any]:
    value = pid_int(pgid)
    if value is None or not group_alive(value):
        return {
            "pgid": value,
            "status": "already_quiescent",
            "term_sent": False,
            "kill_sent": False,
            "live_pgids": [],
        }

    identity_mode = "not_required"

    def identity_failure() -> dict[str, Any] | None:
        nonlocal identity_mode
        if not require_identity:
            return None
        expected = pid_int(expected_start_ticks)
        current_ticks = identity_reader(identity_pid) if expected_identity is None else None
        current_identity = birth_identity_reader(identity_pid) if expected_identity is not None else None
        mismatch = (
            expected_identity is not None and current_identity is not None and current_identity != expected_identity
        ) or (
            expected_identity is None
            and expected is not None
            and current_ticks is not None
            and current_ticks != expected
        )
        unavailable = (expected_identity is not None and current_identity is None) or (
            expected_identity is None and (expected is None or current_ticks is None)
        )
        dead_leader_continuity = (
            unavailable
            and allow_dead_identity_group_continuity
            and pid_int(identity_pid) == value
            and group_alive(value)
        )
        if dead_leader_continuity:
            identity_mode = "dead_leader_live_group"
            return None
        if not mismatch and not unavailable:
            identity_mode = "live_leader"
        if mismatch or unavailable:
            return {
                "pgid": value,
                "status": "identity_mismatch" if mismatch else "identity_unavailable",
                "term_sent": False,
                "kill_sent": False,
                "live_pgids": [value],
                "identity_pid": pid_int(identity_pid),
                "expected_identity": expected_identity,
                "actual_identity": current_identity,
                "expected_start_ticks": expected,
                "actual_start_ticks": current_ticks,
            }
        return None

    failure = identity_failure()
    if failure:
        return failure
    try:
        term_sent = signal_group(value, signal.SIGTERM)
    except (OSError, RuntimeError) as exc:
        return {
            "pgid": value,
            "status": "signal_failed",
            "term_sent": False,
            "kill_sent": False,
            "live_pgids": [value] if group_alive(value) else [],
            "error": f"{type(exc).__name__}: {exc}",
        }
    after_term = wait_for_quiescence(pgids=[value], timeout_seconds=term_grace_seconds, group_alive=group_alive)
    kill_sent = False
    final = after_term
    if after_term["status"] != "quiescent":
        failure = identity_failure()
        if failure:
            failure["term_sent"] = term_sent
            return failure
        try:
            kill_sent = signal_group(value, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except (OSError, RuntimeError) as exc:
            return {
                "pgid": value,
                "status": "signal_failed",
                "term_sent": term_sent,
                "kill_sent": False,
                "live_pgids": [value] if group_alive(value) else [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        final = wait_for_quiescence(pgids=[value], timeout_seconds=kill_grace_seconds, group_alive=group_alive)
    return {
        "pgid": value,
        "status": "quiescent" if final["status"] == "quiescent" else "survivors",
        "term_sent": term_sent,
        "kill_sent": kill_sent,
        "live_pgids": final["live_pgids"],
        "identity_mode": identity_mode,
    }


class OwnedProcessError(RuntimeError):
    """An owned process could not be safely validated or cleaned up."""


class OwnedProcessReadinessError(OwnedProcessError):
    """A process failed liveness, birth-identity, or caller readiness validation."""


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    pgid: int
    birth_identity: str | None
    start_ticks: int | None


@dataclass(frozen=True)
class TerminationReceipt:
    status: str
    pid: int
    pgid: int
    term_sent: bool
    kill_sent: bool
    verified_quiescent: bool
    survivor_pids: tuple[int, ...] = ()
    survivor_pgids: tuple[int, ...] = ()
    signal_errors: tuple[str, ...] = ()
    expected_identity: str | None = None
    actual_identity: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class OwnedProcessCleanupError(OwnedProcessError):
    def __init__(self, receipts: Iterable[TerminationReceipt]):
        self.receipts = tuple(receipts)
        failures = [receipt for receipt in self.receipts if not receipt.verified_quiescent]
        detail = "; ".join(f"pid={receipt.pid} pgid={receipt.pgid} status={receipt.status}" for receipt in failures)
        super().__init__(f"owned process cleanup left unverified groups: {detail}")


@dataclass(eq=False)
class OwnedProcess:
    process: subprocess.Popen
    identity: ProcessIdentity
    handles: tuple[Any, ...] = ()

    def require_live(self) -> None:
        if self.process.poll() is not None:
            raise OwnedProcessReadinessError(
                f"owned process {self.identity.pid} exited before readiness transfer "
                f"with status {self.process.returncode}"
            )
        actual = process_birth_identity(self.identity.pid)
        if self.identity.birth_identity is None or actual != self.identity.birth_identity:
            raise OwnedProcessReadinessError(
                f"owned process {self.identity.pid} birth identity changed or is unavailable "
                f"(expected {self.identity.birth_identity!r}, actual {actual!r})"
            )

    def _receipt(
        self,
        status: str,
        *,
        term_sent: bool = False,
        kill_sent: bool = False,
        actual_identity: str | None = None,
        signal_errors: tuple[str, ...] = (),
    ) -> TerminationReceipt:
        process_live = self.process.poll() is None
        group_live = process_group_is_alive(self.identity.pgid)
        return TerminationReceipt(
            status=status,
            pid=self.identity.pid,
            pgid=self.identity.pgid,
            term_sent=term_sent,
            kill_sent=kill_sent,
            verified_quiescent=not process_live and not group_live,
            survivor_pids=(self.identity.pid,) if process_live else (),
            survivor_pgids=(self.identity.pgid,) if group_live else (),
            signal_errors=signal_errors,
            expected_identity=self.identity.birth_identity,
            actual_identity=actual_identity,
        )

    def _identity_failure(self, *, require_identity: bool, term_sent: bool) -> TerminationReceipt | None:
        process_live = self.process.poll() is None
        if not require_identity or not process_live:
            return None
        actual_identity = process_birth_identity(self.identity.pid)
        if self.identity.birth_identity is not None and actual_identity == self.identity.birth_identity:
            return None
        return self._receipt(
            "identity_mismatch" if actual_identity is not None else "identity_unavailable",
            term_sent=term_sent,
            actual_identity=actual_identity,
        )

    def terminate(
        self,
        *,
        term_grace_seconds: float = 1.0,
        kill_grace_seconds: float = 1.0,
        require_identity: bool = True,
    ) -> TerminationReceipt:
        pgid = self.identity.pgid
        process_live = self.process.poll() is None
        group_live = process_group_is_alive(pgid)
        if not process_live and not group_live:
            return self._receipt("already_quiescent")

        identity_failure = self._identity_failure(require_identity=require_identity, term_sent=False)
        if identity_failure is not None:
            return identity_failure
        actual_identity = self.identity.birth_identity if process_live else None

        term_sent, term_error = self._signal(signal.SIGTERM)
        if term_error:
            return self._receipt("signal_failed", actual_identity=actual_identity, signal_errors=(term_error,))
        after_term = self._wait_for_group(term_grace_seconds)
        if after_term[0]:
            return self._receipt(
                "quiescent",
                term_sent=term_sent,
                actual_identity=actual_identity,
            )

        identity_failure = self._identity_failure(require_identity=require_identity, term_sent=term_sent)
        if identity_failure is not None:
            return identity_failure

        kill_sent, kill_error = self._signal(signal.SIGKILL)
        if kill_error:
            return self._receipt(
                "signal_failed",
                term_sent=term_sent,
                actual_identity=actual_identity,
                signal_errors=(kill_error,),
            )
        quiescent, _survivor_pids, _survivor_pgids = self._wait_for_group(kill_grace_seconds)
        return self._receipt(
            "quiescent" if quiescent else "survivors",
            term_sent=term_sent,
            kill_sent=kill_sent,
            actual_identity=actual_identity,
        )

    def close_handles(self) -> None:
        for handle in reversed(self.handles):
            with suppress(Exception):
                handle.close()

    def _signal(self, signum: int) -> tuple[bool, str | None]:
        try:
            return signal_process_group(self.identity.pgid, signum), None
        except (OSError, RuntimeError) as exc:
            return False, f"{type(exc).__name__}: {exc}"

    def _wait_for_group(self, timeout_seconds: float) -> tuple[bool, tuple[int, ...], tuple[int, ...]]:
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        if self.process.poll() is None:
            with suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=max(0.0, timeout_seconds))
        remaining = max(0.0, deadline - time.monotonic())
        result = wait_for_quiescence(
            pids=[self.identity.pid] if self.process.poll() is None else [],
            pgids=[self.identity.pgid],
            timeout_seconds=remaining,
        )
        return (
            result["status"] == "quiescent",
            tuple(result["live_pids"]),
            tuple(result["live_pgids"]),
        )


class OwnedProcessSet:
    """Own process groups until validated readiness transfers their lifecycle."""

    def __init__(
        self,
        *,
        term_grace_seconds: float = 1.0,
        kill_grace_seconds: float = 1.0,
        popen: Callable[..., subprocess.Popen] | None = None,
    ) -> None:
        self.term_grace_seconds = term_grace_seconds
        self.kill_grace_seconds = kill_grace_seconds
        self._popen = popen or subprocess.Popen
        self._owned: list[OwnedProcess] = []
        self._lock = threading.Lock()
        self.last_cleanup_receipts: tuple[TerminationReceipt, ...] = ()

    def __enter__(self) -> OwnedProcessSet:
        return self

    def __exit__(self, _exc_type, exc, _traceback) -> bool:
        receipts = self.cleanup()
        failures = [receipt for receipt in receipts if not receipt.verified_quiescent]
        if failures:
            cleanup_error = OwnedProcessCleanupError(receipts)
            if exc is None:
                raise cleanup_error
            if hasattr(exc, "add_note"):
                exc.add_note(str(cleanup_error))
        return False

    def spawn(self, argv: list[str] | tuple[str, ...], *, handles: Iterable[Any] = (), **kwargs) -> OwnedProcess:
        if kwargs.get("start_new_session") is False:
            raise ValueError("owned processes require a new process-group session")
        kwargs["start_new_session"] = True
        owned_handles = tuple(handles)
        try:
            process = self._popen(argv, **kwargs)
        except BaseException:
            for handle in reversed(owned_handles):
                with suppress(Exception):
                    handle.close()
            raise
        process_handles = tuple(
            handle
            for handle in (
                getattr(process, "stdin", None),
                getattr(process, "stdout", None),
                getattr(process, "stderr", None),
            )
            if handle is not None and handle not in owned_handles
        )
        try:
            identity = ProcessIdentity(
                pid=process.pid,
                pgid=process.pid,
                birth_identity=process_birth_identity(process.pid),
                start_ticks=process_start_ticks(process.pid),
            )
        except BaseException as exc:
            provisional = OwnedProcess(
                process=process,
                identity=ProcessIdentity(process.pid, process.pid, None, None),
                handles=(*owned_handles, *process_handles),
            )
            receipt = provisional.terminate(
                term_grace_seconds=self.term_grace_seconds,
                kill_grace_seconds=self.kill_grace_seconds,
                require_identity=False,
            )
            provisional.close_handles()
            if not receipt.verified_quiescent and hasattr(exc, "add_note"):
                exc.add_note(str(OwnedProcessCleanupError((receipt,))))
            raise
        owned = OwnedProcess(process=process, identity=identity, handles=(*owned_handles, *process_handles))
        if identity.birth_identity is None:
            if process.poll() is not None and not process_group_is_alive(identity.pgid):
                with self._lock:
                    self._owned.append(owned)
                return owned
            receipt = owned.terminate(
                term_grace_seconds=self.term_grace_seconds,
                kill_grace_seconds=self.kill_grace_seconds,
                require_identity=False,
            )
            owned.close_handles()
            raise OwnedProcessReadinessError(
                f"could not capture birth identity for spawned process {process.pid}; cleanup={receipt.status}"
            )
        with self._lock:
            self._owned.append(owned)
        try:
            _write_context_ownership(owned, transferred=False)
        except BaseException as exc:
            receipt = owned.terminate(
                term_grace_seconds=self.term_grace_seconds,
                kill_grace_seconds=self.kill_grace_seconds,
            )
            owned.close_handles()
            with self._lock:
                self._owned.remove(owned)
            if not receipt.verified_quiescent:
                raise OwnedProcessCleanupError((receipt,)) from exc
            raise
        return owned

    def commit(
        self,
        owned: OwnedProcess,
        readiness_validator: Callable[[OwnedProcess], None] | None = None,
    ) -> OwnedProcess:
        owned.require_live()
        if readiness_validator is not None:
            readiness_validator(owned)
        _write_context_ownership(owned, transferred=True)
        with self._lock:
            if owned not in self._owned:
                raise OwnedProcessError(f"process {owned.identity.pid} is not owned by this set")
            self._owned.remove(owned)
        return owned

    def commit_all(
        self,
        readiness_validator: Callable[[OwnedProcess], None] | None = None,
    ) -> tuple[OwnedProcess, ...]:
        with self._lock:
            owned = tuple(self._owned)
        for process in owned:
            process.require_live()
            if readiness_validator is not None:
                readiness_validator(process)
            _write_context_ownership(process, transferred=True)
        with self._lock:
            if tuple(self._owned) != owned:
                raise OwnedProcessError("owned process set changed during readiness transfer")
            self._owned.clear()
        return owned

    def cleanup(self) -> tuple[TerminationReceipt, ...]:
        with self._lock:
            owned = tuple(reversed(self._owned))
            self._owned.clear()
        receipts = []
        for process in owned:
            try:
                receipt = process.terminate(
                    term_grace_seconds=self.term_grace_seconds,
                    kill_grace_seconds=self.kill_grace_seconds,
                )
            except Exception as exc:
                receipt = process._receipt("cleanup_error", signal_errors=(f"{type(exc).__name__}: {exc}",))
            finally:
                process.close_handles()
            if receipt.verified_quiescent:
                _remove_context_ownership(process.identity)
            receipts.append(receipt)
        self.last_cleanup_receipts = tuple(receipts)
        return self.last_cleanup_receipts
