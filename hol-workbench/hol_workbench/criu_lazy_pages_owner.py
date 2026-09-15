"""Identity-bound ownership and cleanup for a CRIU lazy-pages daemon."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hol_workbench.jsonio import durable_atomic_write_json
from hol_workbench.process_groups import process_birth_identity, process_start_ticks, wait_for_quiescence
from hol_workbench.processes import process_is_alive

OWNER_SCHEMA = "hol-workbench.criu-lazy-pages-owner.v1"
FAILURE_SCHEMA = "hol-workbench.criu-lazy-pages-failure.v1"
OWNER_NAME = "lazy-pages-owner.json"
SOCKET_NAME = "lazy-pages.socket"
SIGNAL_HELPER = Path(__file__).with_name("criu_owned_process_signal.py")

SignalExact = Callable[[int, int, int], dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_pid(
    pidfile: Path,
    *,
    reader: Callable[[Path], str] = _read_text,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    try:
        raw = reader(pidfile)
    except PermissionError:
        try:
            completed = run(
                ["sudo", "-n", "cat", "--", str(pidfile)],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"lazy-pages pidfile is unavailable or malformed: {pidfile}") from exc
        raw = completed.stdout
    except OSError as exc:
        raise RuntimeError(f"lazy-pages pidfile is unavailable or malformed: {pidfile}") from exc
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"lazy-pages pidfile is unavailable or malformed: {pidfile}") from exc
    if value <= 0:
        raise RuntimeError(f"lazy-pages pidfile contains a non-positive PID: {pidfile}")
    return value


def bind_daemon_identity(
    pidfile: Path,
    *,
    start_ticks_reader: Callable[[Any], int | None] = process_start_ticks,
    identity_reader: Callable[[Any], str | None] = process_birth_identity,
    pid_alive: Callable[[Any], bool] = process_is_alive,
) -> dict[str, Any]:
    """Double-read one daemon identity and reject observed PID churn."""
    pid = _read_pid(pidfile)
    if not pid_alive(pid):
        raise RuntimeError(f"lazy-pages daemon {pid} is not live during identity binding")
    first_ticks = start_ticks_reader(pid)
    first_identity = identity_reader(pid)
    second_ticks = start_ticks_reader(pid)
    second_identity = identity_reader(pid)
    final_pid = _read_pid(pidfile)
    if (
        not isinstance(first_ticks, int)
        or first_ticks <= 0
        or not isinstance(first_identity, str)
        or not first_identity
        or first_ticks != second_ticks
        or first_identity != second_identity
        or first_identity != f"linux-proc:{first_ticks}"
        or final_pid != pid
    ):
        raise RuntimeError(f"lazy-pages daemon {pid} has no stable birth identity")
    return {
        "pid": pid,
        "start_ticks": first_ticks,
        "birth_identity": first_identity,
        "bound_utc": _utc_now(),
    }


def _validate_readiness(readiness: dict[str, Any]) -> dict[str, Any]:
    if (
        readiness.get("status_fd_observed") is not True
        or readiness.get("byte_hex") != "00"
        or not isinstance(readiness.get("observed_utc"), str)
        or not readiness.get("observed_utc")
    ):
        raise RuntimeError("lazy-pages readiness must record one observed CRIU status-fd NUL byte")
    return dict(readiness)


def _validate_daemon(daemon: object) -> dict[str, Any]:
    if not isinstance(daemon, dict):
        raise RuntimeError("lazy-pages owner has no daemon identity")
    pid = daemon.get("pid")
    start_ticks = daemon.get("start_ticks")
    birth_identity = daemon.get("birth_identity")
    if not isinstance(pid, int) or pid <= 0 or not isinstance(start_ticks, int) or start_ticks <= 0:
        raise RuntimeError("lazy-pages owner daemon PID/start identity is invalid")
    if birth_identity != f"linux-proc:{start_ticks}":
        raise RuntimeError("lazy-pages owner daemon birth identity does not match its Linux start ticks")
    return dict(daemon)


def validate_bound_daemon(daemon: object) -> dict[str, Any]:
    """Return one validated PID/start/birth daemon identity."""
    return _validate_daemon(daemon)


def owner_record(
    *,
    image_dir: Path,
    pidfile: Path,
    log: Path,
    daemon: dict[str, Any],
    readiness: dict[str, Any],
) -> dict[str, Any]:
    root = image_dir.resolve()
    for path in (pidfile, log):
        if path.resolve().parent != root:
            raise RuntimeError(f"lazy-pages artifact is outside the CRIU image directory: {path}")
    if not pidfile.name.startswith("lazy-pages-") or pidfile.suffix != ".pid":
        raise RuntimeError(f"lazy-pages pidfile has an unsupported name: {pidfile}")
    if not log.name.startswith("lazy-pages-") or log.suffix != ".log":
        raise RuntimeError(f"lazy-pages log has an unsupported name: {log}")
    return {
        "schema": OWNER_SCHEMA,
        "status": "ready",
        "image_dir": str(root),
        "socket": str(root / SOCKET_NAME),
        "pidfile": str(pidfile.resolve()),
        "log": str(log.resolve()),
        "daemon": _validate_daemon(daemon),
        "readiness": _validate_readiness(readiness),
        "created_utc": _utc_now(),
    }


def write_owner(image_dir: Path, record: dict[str, Any]) -> Path:
    root = image_dir.resolve()
    _validate_owner_record(root, record)
    path = root / OWNER_NAME
    durable_atomic_write_json(path, dict(record))
    return path


def _validate_owner_record(root: Path, record: object) -> dict[str, Any]:
    if not isinstance(record, dict) or record.get("schema") != OWNER_SCHEMA:
        raise RuntimeError("lazy-pages owner metadata has an unsupported schema")
    if Path(str(record.get("image_dir") or "")).resolve() != root:
        raise RuntimeError("lazy-pages owner image directory does not match the active image directory")
    expected_paths = {
        "socket": Path(str(record.get("socket") or "")),
        "pidfile": Path(str(record.get("pidfile") or "")),
        "log": Path(str(record.get("log") or "")),
    }
    for label, candidate in expected_paths.items():
        resolved = candidate.resolve()
        if resolved.parent != root:
            raise RuntimeError(f"lazy-pages owner {label} escapes the active image directory: {candidate}")
    if expected_paths["socket"].resolve() != root / SOCKET_NAME:
        raise RuntimeError("lazy-pages owner socket is not the canonical CRIU lazy-pages socket")
    if not expected_paths["pidfile"].name.startswith("lazy-pages-") or expected_paths["pidfile"].suffix != ".pid":
        raise RuntimeError("lazy-pages owner pidfile has an unsupported name")
    if not expected_paths["log"].name.startswith("lazy-pages-") or expected_paths["log"].suffix != ".log":
        raise RuntimeError("lazy-pages owner log has an unsupported name")
    _validate_daemon(record.get("daemon"))
    raw_readiness = record.get("readiness")
    _validate_readiness(raw_readiness if isinstance(raw_readiness, dict) else {})
    return record


def read_owner(image_dir: Path) -> dict[str, Any] | None:
    root = image_dir.resolve()
    path = root / OWNER_NAME
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(f"lazy-pages owner metadata is unreadable: {path}") from exc
    _validate_owner_record(root, record)
    return record


def daemon_liveness(
    daemon: dict[str, Any],
    *,
    start_ticks_reader: Callable[[Any], int | None],
    identity_reader: Callable[[Any], str | None],
    pid_alive: Callable[[Any], bool],
) -> str:
    pid = daemon["pid"]
    if not pid_alive(pid):
        return "gone"
    start_ticks = start_ticks_reader(pid)
    birth_identity = identity_reader(pid)
    if start_ticks is None or birth_identity is None:
        return "unavailable"
    if birth_identity != f"linux-proc:{start_ticks}":
        return "unavailable"
    if start_ticks != daemon["start_ticks"] or birth_identity != daemon["birth_identity"]:
        return "replaced"
    return "exact"


def privileged_pidfd_signal(
    pid: int,
    start_ticks: int,
    signum: int,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    command = [
        "sudo",
        "-n",
        str(Path(sys.executable).resolve()),
        str(SIGNAL_HELPER),
        "--pid",
        str(pid),
        "--start-ticks",
        str(start_ticks),
        "--signal",
        str(signum),
    ]
    try:
        completed = run(command, check=False, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "signal_failed", "error": f"{type(exc).__name__}: {exc}", "command": command}
    try:
        result = json.loads(completed.stdout)
    except (json.JSONDecodeError, TypeError):
        result = {
            "status": "signal_failed",
            "error": f"identity-bound signal helper returned invalid JSON (exit {completed.returncode})",
            "stdout_tail": (completed.stdout or "")[-1000:],
            "stderr_tail": (completed.stderr or "")[-1000:],
        }
    known = {"signalled", "already_quiescent", "identity_mismatch", "identity_unavailable"}
    status = result.get("status") if isinstance(result, dict) else None
    echoed = (
        result.get("pid") == pid
        and result.get("signal") == signum
        and (
            status not in {"signalled", "identity_mismatch", "identity_unavailable"}
            or result.get("expected_start_ticks", result.get("start_ticks")) == start_ticks
        )
    )
    exit_coherent = (completed.returncode == 0) == (status in {"signalled", "already_quiescent"})
    if status not in known or not echoed or not exit_coherent:
        result = {
            "status": "signal_failed",
            "error": "identity-bound signal helper result did not match the requested process, signal, or exit status",
            "helper_result": result,
        }
    return {
        **result,
        "command": command,
        "exit_status": completed.returncode,
    }


def terminate_owned_daemon(
    record: dict[str, Any],
    *,
    signal_exact: SignalExact = privileged_pidfd_signal,
    start_ticks_reader: Callable[[Any], int | None] = process_start_ticks,
    identity_reader: Callable[[Any], str | None] = process_birth_identity,
    pid_alive: Callable[[Any], bool] = process_is_alive,
    wait: Callable[..., dict[str, Any]] = wait_for_quiescence,
) -> dict[str, Any]:
    """Terminate only the bound daemon and verify that exact birth is gone."""
    daemon = record["daemon"]
    pid = daemon["pid"]
    initial_liveness = daemon_liveness(
        daemon,
        start_ticks_reader=start_ticks_reader,
        identity_reader=identity_reader,
        pid_alive=pid_alive,
    )
    if initial_liveness in {"gone", "replaced"}:
        return {
            "status": "quiescent",
            "verified_quiescent": True,
            "reason": f"bound daemon birth identity is {initial_liveness}",
            "signals": [],
            "surviving_pids": [],
        }
    if initial_liveness != "exact":
        return {
            "status": "unverified",
            "verified_quiescent": False,
            "reason": "bound daemon appears live but its birth identity is unavailable",
            "signals": [],
            "surviving_pids": [pid],
        }

    def classify_after_helper(
        helper_status: object, signal_name: str, signals: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        if helper_status not in {"already_quiescent", "identity_mismatch"}:
            return None
        rechecked = daemon_liveness(
            daemon,
            start_ticks_reader=start_ticks_reader,
            identity_reader=identity_reader,
            pid_alive=pid_alive,
        )
        if rechecked in {"gone", "replaced"}:
            return {
                "status": "quiescent",
                "verified_quiescent": True,
                "reason": (f"identity-bound {signal_name} reported {helper_status}; parent recheck found {rechecked}"),
                "signals": signals,
                "surviving_pids": [],
            }
        return {
            "status": "unverified",
            "verified_quiescent": False,
            "reason": f"identity-bound {signal_name} reported {helper_status}; parent recheck found {rechecked}",
            "signals": signals,
            "surviving_pids": [pid],
        }

    signals = [signal_exact(pid, daemon["start_ticks"], signal.SIGTERM)]
    classified = classify_after_helper(signals[0].get("status"), "TERM", signals)
    if classified is not None:
        return classified
    if signals[0].get("status") != "signalled":
        return {
            "status": "unverified",
            "verified_quiescent": False,
            "reason": f"identity-bound TERM reported {signals[0].get('status') or 'unknown'}",
            "signals": signals,
            "surviving_pids": [pid],
        }

    def owned_alive(candidate: Any) -> bool:
        if candidate != pid:
            return False
        liveness = daemon_liveness(
            daemon,
            start_ticks_reader=start_ticks_reader,
            identity_reader=identity_reader,
            pid_alive=pid_alive,
        )
        return liveness in {"exact", "unavailable"}

    after_term = wait(pids=[pid], timeout_seconds=1.0, pid_alive=owned_alive)
    final = after_term
    if after_term.get("status") != "quiescent":
        signals.append(signal_exact(pid, daemon["start_ticks"], signal.SIGKILL))
        classified = classify_after_helper(signals[-1].get("status"), "KILL", signals)
        if classified is not None:
            return classified
        if signals[-1].get("status") != "signalled":
            return {
                "status": "unverified",
                "verified_quiescent": False,
                "reason": f"identity-bound KILL reported {signals[-1].get('status') or 'unknown'}",
                "signals": signals,
                "surviving_pids": [pid],
                "final_wait": after_term,
            }
        final = wait(pids=[pid], timeout_seconds=1.0, pid_alive=owned_alive)
    quiescent = final.get("status") == "quiescent"
    return {
        "status": "quiescent" if quiescent else "survivors",
        "verified_quiescent": quiescent,
        "signals": signals,
        "surviving_pids": [] if quiescent else [pid],
        "final_wait": final,
    }


def terminate_bound_daemon(daemon: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Terminate one validated bound daemon without requiring ready-owner metadata."""
    return terminate_owned_daemon({"daemon": validate_bound_daemon(daemon)}, **kwargs)


def write_failure_receipt(
    image_dir: Path,
    *,
    phase: str,
    record: dict[str, Any],
    cleanup: dict[str, Any],
    reason: str | None = None,
) -> Path:
    path = image_dir.resolve() / f"lazy-pages-failed-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S.%fZ')}.json"
    data = {
        "schema": FAILURE_SCHEMA,
        "status": "lazy_pages_failed",
        "phase": phase,
        "owner": record,
        "cleanup": cleanup,
        "verified_quiescent": cleanup.get("verified_quiescent") is True,
        "surviving_pids": cleanup.get("surviving_pids") or [],
        "created_utc": _utc_now(),
    }
    if reason is not None:
        data["reason"] = reason
    durable_atomic_write_json(
        path,
        data,
    )
    return path


def socket_reuse_preflight(image_dir: Path) -> dict[str, Any]:
    """Report metadata presence; callers must clean an owned daemon before reuse."""
    root = image_dir.resolve()
    record = read_owner(root)
    socket = root / SOCKET_NAME
    if socket.exists() and record is None:
        return {
            "status": "blocked",
            "reason": "lazy-pages socket exists without identity-bound owner metadata",
            "socket": str(socket),
        }
    return {
        "status": "clear" if record is None else "owned_requires_cleanup",
        "owner": record,
        "socket": str(socket),
    }
