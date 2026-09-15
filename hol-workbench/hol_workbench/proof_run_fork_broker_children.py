"""Mechanical child registration, identity-bound cleanup, and quiescence."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import time
from pathlib import Path
from threading import Lock
from typing import Any

ACTIVE_CHILDREN_SCHEMA = "proof-run.fork-active-children.v1"
ACTIVE_CHILDREN_FILENAME = "fork-active-children.json"


def process_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        _, separator, tail = raw.rpartition(")")
        return int(tail.split()[19]) if separator else None
    except (OSError, IndexError, ValueError):
        return None


def process_birth_identity(pid: int) -> str | None:
    if (ticks := process_start_ticks(pid)) is not None:
        return f"linux-proc:{ticks}"
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(completed.stdout.split())
    return f"ps-lstart:{value}" if completed.returncode == 0 and value else None


def process_group_is_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def record_is_alive(record: dict[str, Any]) -> bool:
    expected = record.get("process_identity")
    return bool(expected) and process_birth_identity(int(record["pid"])) == expected


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ActiveBrokerChildren:
    def __init__(self, pool_dir: Path) -> None:
        self.path = pool_dir / ACTIVE_CHILDREN_FILENAME
        self._lock = Lock()
        self._children: dict[int, dict[str, Any]] = {}
        self._write_locked()

    def _write_locked(self) -> None:
        _atomic_json(
            self.path,
            {
                "schema": ACTIVE_CHILDREN_SCHEMA,
                "children": [self._children[key] for key in sorted(self._children)],
            },
        )

    def register(self, *, attempt_id: str, pid: int) -> dict[str, Any]:
        record = {
            "attempt_id": attempt_id,
            "pid": pid,
            "pgid": pid,
            "process_start_ticks": process_start_ticks(pid),
            "process_identity": process_birth_identity(pid),
        }
        if not record["process_identity"]:
            raise RuntimeError("fork child birth identity is unavailable")
        with self._lock:
            self._children[pid] = record
            self._write_locked()
        return dict(record)

    def unregister(self, pgid: int) -> None:
        with self._lock:
            self._children.pop(pgid, None)
            self._write_locked()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._children[key]) for key in sorted(self._children)]


def wait_for_child_ownership(
    record: dict[str, Any],
    ownership_path: Path,
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    pid = int(record["pid"])
    while True:
        if not record_is_alive(record):
            raise RuntimeError("fork child exited before publishing process-group ownership")
        if ownership_path.is_file() and os.getpgid(pid) == pid:
            ownership_path.unlink()
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("fork child timed out publishing process-group ownership")
        time.sleep(0.01)


def acknowledge_registered_child(
    registry: ActiveBrokerChildren,
    *,
    attempt_id: str,
    pid: int,
    ack_path: Path,
    ownership_path: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    record = registry.register(attempt_id=attempt_id, pid=pid)
    try:
        _atomic_json(ack_path, {"status": "registered", "attempt_id": attempt_id, "pid": pid})
        wait_for_child_ownership(record, ownership_path, timeout_seconds=timeout_seconds)
        return record
    except BaseException:
        terminate_child(record)
        if not process_group_is_alive(pid):
            registry.unregister(pid)
        ack_path.unlink(missing_ok=True)
        ownership_path.unlink(missing_ok=True)
        raise


def wait_for_quiescence(record: dict[str, Any], *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while record_is_alive(record) or process_group_is_alive(int(record["pgid"])):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def terminate_child(record: dict[str, Any], *, timeout_seconds: float = 2.0) -> dict[str, Any]:
    pgid = int(record["pgid"])
    leader_live = record_is_alive(record)
    if not leader_live and not process_group_is_alive(pgid):
        return {"status": "already_quiescent", "verified_quiescent": True}
    # A live POSIX process group retains its PGID until its final member exits;
    # another group cannot acquire that PGID in the meantime. Once broker
    # registration has identity-bound the original leader, group continuity is
    # therefore sufficient authority to clean descendants after the leader
    # exits. This closes the former fail-closed-but-permanent orphan wedge.
    orphaned_group = not leader_live
    term_sent = False
    kill_sent = False
    try:
        os.killpg(pgid, signal.SIGTERM)
        term_sent = True
    except ProcessLookupError:
        pass
    except OSError as exc:
        return {"status": "signal_failed", "verified_quiescent": False, "error": str(exc)}
    if not wait_for_quiescence(record, timeout_seconds=min(timeout_seconds, 0.5)):
        try:
            os.killpg(pgid, signal.SIGKILL)
            kill_sent = True
        except ProcessLookupError:
            pass
        except OSError as exc:
            return {"status": "signal_failed", "verified_quiescent": False, "error": str(exc)}
    quiescent = wait_for_quiescence(record, timeout_seconds=max(0.0, timeout_seconds - 0.5))
    if not quiescent:
        status = "survivors"
    elif orphaned_group:
        status = "orphaned_group_quiescent"
    else:
        status = "quiescent"
    return {
        "status": status,
        "verified_quiescent": quiescent,
        "term_sent": term_sent,
        "kill_sent": kill_sent,
        "leader_identity_live_at_cleanup": leader_live,
    }


def reap_orphaned_children(registry: ActiveBrokerChildren) -> list[dict[str, Any]]:
    """Quiesce children no longer owned by an active broker handler."""
    reaped: list[dict[str, Any]] = []
    for child in registry.snapshot():
        was_live = record_is_alive(child) or process_group_is_alive(int(child["pgid"]))
        cleanup = terminate_child(child)
        verified = (
            cleanup.get("verified_quiescent") is True
            and not record_is_alive(child)
            and not process_group_is_alive(int(child["pgid"]))
        )
        record = {
            "attempt_id": child.get("attempt_id"),
            "pid": child.get("pid"),
            "pgid": child.get("pgid"),
            "termination_reason": "orphaned_before_next_admission",
            "was_live": was_live,
            "child_cleanup": cleanup,
            "verified_quiescent": verified,
        }
        reaped.append(record)
        if not verified:
            raise RuntimeError(
                "broker could not quiesce prior disposable child "
                f"{child.get('attempt_id') or child.get('pgid')}: {cleanup.get('status')}"
            )
        registry.unregister(int(child["pgid"]))
    return reaped
