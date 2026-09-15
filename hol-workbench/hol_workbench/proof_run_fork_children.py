"""Durable ownership registry for disposable fork-basis evaluations."""

from __future__ import annotations

import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, NoReturn

from hol_workbench.fork_child_ocaml import ocaml_string_literal
from hol_workbench.jsonio import atomic_write_json, read_json
from hol_workbench.process_groups import (
    process_birth_identity,
    process_group_is_alive,
    process_start_ticks,
    terminate_process_group,
)
from hol_workbench.processes import process_is_alive
from hol_workbench.proof_run_fork_budget import (
    FORK_SPAWN_RECOVERY_TIMEOUT_SECONDS,
    ForkSpawnHandshakeTimeout,
)
from hol_workbench.proof_run_runtime import utc_now

ACTIVE_CHILDREN_SCHEMA = "proof-run.fork-active-children.v1"
ACTIVE_CHILDREN_FILENAME = "fork-active-children.json"
FORK_REGISTRATION_FAILURE_CLEANUP_SECONDS = 2.0


class ForkWorkerStopping(RuntimeError):
    pass


class ForkSpawnGate:
    """Serialize stop against child spawn plus durable registration."""

    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event
        self._lock = threading.Lock()

    @contextmanager
    def spawning(self) -> Iterator[None]:
        with self._lock:
            if self.stop_event.is_set():
                raise ForkWorkerStopping("fork basis worker is stopping")
            yield

    def begin_stop(self) -> None:
        with self._lock:
            self.stop_event.set()


def active_children_path(pool_dir: Path) -> Path:
    return pool_dir / ACTIVE_CHILDREN_FILENAME


def read_active_children(pool_dir: Path, *, require_registry: bool = False) -> list[dict[str, Any]]:
    path = active_children_path(pool_dir)
    if not path.exists():
        if require_registry:
            raise RuntimeError(f"fork active-child registry not found: {path}")
        return []
    data = read_json(path)
    if data.get("schema") != ACTIVE_CHILDREN_SCHEMA or not isinstance(data.get("children"), list):
        raise RuntimeError(f"invalid fork active-child registry: {path}")
    return [item for item in data["children"] if isinstance(item, dict)]


def clear_active_children(pool_dir: Path) -> None:
    atomic_write_json(
        active_children_path(pool_dir),
        {
            "schema": ACTIVE_CHILDREN_SCHEMA,
            "pool_dir": str(pool_dir),
            "updated_utc": utc_now(),
            "children": [],
        },
    )


class ActiveForkChildren:
    def __init__(self, pool_dir: Path) -> None:
        self.pool_dir = pool_dir
        self.path = active_children_path(pool_dir)
        self._lock = threading.Lock()
        self._children: dict[int, dict[str, Any]] = {}
        self._write_locked()

    def _write_locked(self) -> None:
        if not self._children:
            clear_active_children(self.pool_dir)
            return
        atomic_write_json(
            self.path,
            {
                "schema": ACTIVE_CHILDREN_SCHEMA,
                "pool_dir": str(self.pool_dir),
                "updated_utc": utc_now(),
                "children": [self._children[pgid] for pgid in sorted(self._children)],
            },
        )

    def register(self, *, attempt_id: str, pid: int, pgid: int) -> dict[str, Any]:
        record = {
            "attempt_id": attempt_id,
            "pid": pid,
            "pgid": pgid,
            "process_start_ticks": process_start_ticks(pid),
            "process_identity": process_birth_identity(pid),
            "registered_utc": utc_now(),
        }
        with self._lock:
            self._children[pgid] = record
            self._write_locked()
        return record

    def unregister(self, pgid: int) -> None:
        with self._lock:
            self._children.pop(pgid, None)
            self._write_locked()

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._children[pgid]) for pgid in sorted(self._children)]


def terminate_registered_child(record: dict) -> dict:
    return terminate_process_group(
        record.get("pgid"),
        identity_pid=record.get("pid"),
        expected_identity=record.get("process_identity"),
        expected_start_ticks=record.get("process_start_ticks"),
        require_identity=True,
    )


def termination_marker(*, kind: str, pid: int, pgid: int, cleanup_status: str) -> str:
    return f"[fork-worker] {kind}; child pid={pid} pgid={pgid} cleanup={cleanup_status}\n"


def record_identity_is_alive(record: dict) -> bool:
    expected = record.get("process_identity")
    return expected is not None and process_birth_identity(record.get("pid")) == expected


def registered_child_quiescence(record: dict) -> dict[str, bool]:
    pid_live = (
        record_identity_is_alive(record)
        if record.get("process_identity") is not None
        else process_is_alive(record.get("pid"))
    )
    return {
        "pid_live": pid_live,
        "pgid_live": process_group_is_alive(record.get("pgid")),
    }


def unregister_quiescent_child(active_children: ActiveForkChildren, record: dict) -> bool:
    state = registered_child_quiescence(record)
    if state["pid_live"] or state["pgid_live"]:
        return False
    active_children.unregister(int(record["pgid"]))
    return True


def wait_for_child_ownership(record: dict, ownership_path: Path, *, timeout_seconds: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    pid = int(record["pid"])
    while True:
        if not record_identity_is_alive(record):
            raise RuntimeError("fork child exited before publishing process-group ownership")
        if ownership_path.is_file() and os.getpgid(pid) == pid:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("fork child timed out publishing process-group ownership")
        time.sleep(0.01)


def remove_lifecycle_marker(path: Path) -> None:
    """Remove a fork handshake marker or fail before ownership can advance."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    if path.exists():
        raise RuntimeError(f"fork lifecycle marker remains after removal: {path}")


def register_and_ack_child(
    active_children: ActiveForkChildren,
    *,
    attempt_id: str,
    pid: int,
    ack_path: Path,
    ownership_path: Path,
    ack_writer=atomic_write_json,
    ownership_waiter=wait_for_child_ownership,
    marker_remover=remove_lifecycle_marker,
    blocked_terminator: Callable[[dict], str],
    cleanup_timeout_seconds: float = FORK_REGISTRATION_FAILURE_CLEANUP_SECONDS,
    cleanup_monotonic=time.monotonic,
    cleanup_sleep=time.sleep,
) -> dict:
    fallback = {
        "attempt_id": attempt_id,
        "pid": pid,
        "pgid": pid,
        "process_start_ticks": process_start_ticks(pid),
        "process_identity": process_birth_identity(pid),
    }
    try:
        child = active_children.register(attempt_id=attempt_id, pid=pid, pgid=pid)
        if child.get("process_identity") is None:
            raise RuntimeError("fork child birth identity is unavailable")
        ack_writer(ack_path, {"status": "registered", "attempt_id": attempt_id, "pid": pid})
        ownership_waiter(child, ownership_path)
        marker_remover(ownership_path)
        return child
    except Exception as exc:
        child = next((item for item in active_children.snapshot() if item.get("pgid") == pid), fallback)
        cleanup = terminate_registered_child(child)
        with suppress(OSError):
            ack_path.unlink()
        with suppress(OSError):
            ownership_path.unlink()
        blocked_cleanup = None
        if child.get("process_identity") is not None and record_identity_is_alive(child):
            blocked_cleanup = blocked_terminator(child)
        deadline = cleanup_monotonic() + max(0.0, cleanup_timeout_seconds)
        while True:
            quiescence = registered_child_quiescence(child)
            if not quiescence["pid_live"] and not quiescence["pgid_live"]:
                with suppress(OSError):
                    active_children.unregister(pid)
                break
            now = cleanup_monotonic()
            if now >= deadline:
                break
            cleanup_sleep(min(0.01, max(0.0, deadline - now)))
        survivors = [name for name, live in quiescence.items() if live]
        raise RuntimeError(
            f"fork child registration ACK failed for {attempt_id}; lifecycle_error; "
            f"cleanup={cleanup['status']}; blocked_cleanup={blocked_cleanup}; survivors={survivors}"
        ) from exc


def recover_fork_spawn_after_deadline(
    *,
    attempt_id: str,
    deadline_seconds: float,
    handshake_started: float,
    spawn_token: str,
    refusal_token: str,
    ack_path: Path,
    ownership_path: Path,
    read_parent_until: Callable[..., list[str]],
    read_timeout: type[Exception],
    deadline_error: Exception,
    blocked_terminator: Callable[[dict], str],
    runtime_refusal: Callable[[str], Exception],
    monotonic: Callable[[], float] = time.monotonic,
) -> NoReturn:
    """Recover an unregistered child after the configured spawn deadline."""
    transcript_stem = ack_path.name.removesuffix(".registration-ack.json")
    if transcript_stem != ack_path.name:
        (ack_path.parent / f"{transcript_stem}.log").touch(exist_ok=True)
    try:
        lines = read_parent_until(
            (spawn_token, refusal_token),
            timeout=FORK_SPAWN_RECOVERY_TIMEOUT_SECONDS,
            purpose=f"fork spawn recovery {attempt_id}",
        )
    except read_timeout as exc:
        raise ForkSpawnHandshakeTimeout(
            attempt_id=attempt_id,
            deadline_seconds=deadline_seconds,
            elapsed_seconds=monotonic() - handshake_started,
            cleanup_status="spawn_token_unrecovered",
            seat_reusable=False,
        ) from exc
    elapsed = monotonic() - handshake_started
    for line in lines:
        if refusal_token in line:
            detail = line.split(refusal_token, 1)[1].strip() or "runtime returned no detail"
            raise runtime_refusal(detail) from deadline_error
        if spawn_token not in line:
            continue
        child_pid = int(line.split(spawn_token, 1)[1].strip())
        child_record = {
            "attempt_id": attempt_id,
            "pid": child_pid,
            "pgid": child_pid,
            "process_start_ticks": process_start_ticks(child_pid),
            "process_identity": process_birth_identity(child_pid),
        }
        if not record_identity_is_alive(child_record):
            cleanup_status = "already_quiescent"
            seat_reusable = not process_group_is_alive(child_pid)
        else:
            cleanup_status = blocked_terminator(child_record)
            seat_reusable = not record_identity_is_alive(child_record)
        with suppress(OSError):
            ack_path.unlink()
        with suppress(OSError):
            ownership_path.unlink()
        raise ForkSpawnHandshakeTimeout(
            attempt_id=attempt_id,
            deadline_seconds=deadline_seconds,
            elapsed_seconds=elapsed,
            cleanup_status=cleanup_status,
            seat_reusable=seat_reusable,
        ) from deadline_error
    raise ForkSpawnHandshakeTimeout(
        attempt_id=attempt_id,
        deadline_seconds=deadline_seconds,
        elapsed_seconds=elapsed,
        cleanup_status="spawn_token_missing_after_recovery",
        seat_reusable=False,
    ) from deadline_error


def prepare_fork_spawn(
    request: dict[str, Any],
    *,
    source_phrase_builder: Callable[..., str],
    fork_phrase_builder: Callable[..., str],
) -> dict[str, Any]:
    """Build one fork-child phrase and its lifecycle marker paths."""
    attempt_id = str(request.get("attempt_id") or secrets.token_hex(8))
    source_path = Path(request["source"]).resolve()
    transcript_path = Path(request["transcript_path"]).resolve()
    load_wrapper = transcript_path.with_name(f"{transcript_path.stem}.load.ml")
    load_phrase = (
        f"loadt {ocaml_string_literal(str(source_path))};;"
        if request.get("raw_source")
        else source_phrase_builder(source_path, use_loadt=True)
    )
    load_wrapper.write_text(load_phrase + "\n", encoding="utf-8")
    done_token = f"__PROOF_RUN_FORK_CHILD_DONE__:{attempt_id}:{secrets.token_hex(8)}"
    spawn_token = f"__PROOF_RUN_FORK_SPAWNED__:{attempt_id}:"
    refusal_token = f"__PROOF_RUN_FORK_REFUSED__:{attempt_id}:"
    ack_path = transcript_path.with_name(f"{transcript_path.stem}.registration-ack.json")
    ownership_path = transcript_path.with_name(f"{transcript_path.stem}.ownership-ready")
    remove_lifecycle_marker(ack_path)
    remove_lifecycle_marker(ownership_path)
    phrase = fork_phrase_builder(
        attempt_id=attempt_id,
        source=str(source_path),
        transcript=str(transcript_path),
        load_wrapper=load_wrapper,
        done_token=done_token,
        spawn_token=spawn_token,
        ack_path=ack_path,
        ownership_path=ownership_path,
        refusal_token=refusal_token,
    )
    return {
        "attempt_id": attempt_id,
        "transcript_path": transcript_path,
        "done_token": done_token,
        "spawn_token": spawn_token,
        "refusal_token": refusal_token,
        "ack_path": ack_path,
        "ownership_path": ownership_path,
        "phrase": phrase,
    }


def complete_fork_spawn(
    *,
    lines: list[str],
    spawn: dict[str, Any],
    active_children: ActiveForkChildren,
    handshake: dict[str, Any],
    blocked_terminator: Callable[[dict], str],
    runtime_refusal: Callable[[str], Exception],
) -> tuple[dict, str, dict]:
    """Interpret a successful parent handshake and register its live child."""
    attempt_id = spawn["attempt_id"]
    spawn_token = spawn["spawn_token"]
    refusal_token = spawn["refusal_token"]
    for line in lines:
        if refusal_token in line:
            detail = line.split(refusal_token, 1)[1].strip() or "runtime returned no detail"
            raise runtime_refusal(detail)
        if spawn_token not in line:
            continue
        child_pid = int(line.split(spawn_token, 1)[1].strip())
        if (
            spawn["transcript_path"].is_file()
            and not process_is_alive(child_pid)
            and not process_group_is_alive(child_pid)
        ):
            child = {
                "attempt_id": attempt_id,
                "pid": child_pid,
                "pgid": child_pid,
                "process_start_ticks": None,
                "process_identity": None,
            }
            return child, spawn["done_token"], {**handshake, "fork_spawn_child_registered": False}
        child = register_and_ack_child(
            active_children,
            attempt_id=attempt_id,
            pid=child_pid,
            ack_path=spawn["ack_path"],
            ownership_path=spawn["ownership_path"],
            blocked_terminator=blocked_terminator,
        )
        return child, spawn["done_token"], handshake
    raise RuntimeError(f"fork child spawn token not found for {attempt_id}")
