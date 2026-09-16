#!/usr/bin/env python3
"""Executable stdlib contract for the memory-first process lifecycle."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

WORKBENCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKBENCH_ROOT))

from hol_workbench import (  # noqa: E402
    criu_shelf_admission as criu_shelf_admission_module,
)
from hol_workbench import (  # noqa: E402
    hot_profile_session,
    orbstack_idle_retirement,
)
from hol_workbench.cli import orbstack_criu_restore  # noqa: E402
from hol_workbench.fork_broker_protocol import (  # noqa: E402
    BROKER_FRAME_CONTROL,
    BROKER_FRAME_HEADER,
    BROKER_FRAME_RAW_OUTPUT,
    BrokerProtocolError,
    receive_frame,
    send_control,
    send_raw_output,
)
from hol_workbench.hot_profile_session import broker_response_proves_reusable  # noqa: E402
from hol_workbench.jsonio import atomic_write_json  # noqa: E402
from hol_workbench.pools import fork_attempt_lifecycle as fork_attempt_lifecycle_module  # noqa: E402
from hol_workbench.pools.fork_attempt_lifecycle import (  # noqa: E402
    ACTIVE_CHILDREN_SCHEMA,
    wait_for_fork_attempt_cleanup,
)
from hol_workbench.pools.leases import release_warm_pool_seat  # noqa: E402
from hol_workbench.pools.seat_lifecycle import (  # noqa: E402
    checkout_pool_seat_locked,
    finalize_fork_pool_seat_locked,
)
from hol_workbench.pools.session_lifecycle import (  # noqa: E402
    pool_session_alive,
    send_session_request,
    session_socket_path,
)
from hol_workbench.pools.store import (  # noqa: E402
    read_warm_pool,
    warm_pool_lock,
    write_warm_pool_json,
)
from hol_workbench.process_groups import (  # noqa: E402
    OwnedProcessSet,
    process_birth_identity,
    process_start_ticks,
    terminate_process_group,
)
from hol_workbench.proof_run_fork_broker_client import BufferedBrokerFrameReader  # noqa: E402
from hol_workbench.proof_run_fork_children import ForkSpawnGate  # noqa: E402


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def require_raises(error_type: type[BaseException], detail: str, action: Any) -> None:
    try:
        action()
    except error_type as exc:
        require(detail in str(exc), f"unexpected {error_type.__name__}: {exc}")
    else:
        raise AssertionError(f"expected {error_type.__name__}: {detail}")


def current_session(session_dir: Path) -> dict[str, Any]:
    pid = os.getpid()
    ticks = process_start_ticks(pid)
    identity = process_birth_identity(pid)
    require(ticks is not None, "current Linux process start ticks are unavailable")
    require(identity == f"linux-proc:{ticks}", "current Linux process birth identity is unavailable")
    return {
        "schema": "proof-run.warm-session.v1",
        "session_id": session_dir.name,
        "session_dir": str(session_dir),
        "status": "ready",
        "worker_pid": pid,
        "worker_pgid": os.getpgrp(),
        "worker_start_ticks": ticks,
        "worker_identity": identity,
        "hol_pid": pid,
        "basis_pid": pid,
        "basis_pgid": os.getpgrp(),
        "basis_start_ticks": ticks,
        "basis_identity": identity,
        "engine": "fork_basis",
        "execution_topology": "mechanical_basis_broker_v3",
    }


def test_process_identity(root: Path) -> None:
    session_dir = root / "identity-session"
    session_dir.mkdir()
    good = current_session(session_dir)
    atomic_write_json(session_dir / "session.json", good)
    item = {"session_dir": str(session_dir)}
    require(pool_session_alive(item), "exact current worker and basis identities were rejected")

    bad = dict(good)
    bad["worker_identity"] = "linux-proc:1"
    atomic_write_json(session_dir / "session.json", bad)
    require(not pool_session_alive(item), "worker birth-identity mismatch was accepted")

    bad = dict(good)
    bad["basis_start_ticks"] = int(good["basis_start_ticks"]) + 1
    atomic_write_json(session_dir / "session.json", bad)
    require(not pool_session_alive(item), "basis start-tick mismatch was accepted")

    bad = dict(good)
    bad["hol_pid"] = os.getpid() + 1
    atomic_write_json(session_dir / "session.json", bad)
    require(not pool_session_alive(item), "HOL PID alias mismatch was accepted")

    bad = dict(good)
    bad.pop("basis_identity")
    atomic_write_json(session_dir / "session.json", bad)
    require(not pool_session_alive(item), "missing basis identity was accepted")
    atomic_write_json(session_dir / "session.json", good)


def registry_payload(children: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema": ACTIVE_CHILDREN_SCHEMA, "children": children}


def test_registry_authority(root: Path) -> None:
    attempt_id = "attempt-a"
    registry = root / "active-children.json"
    transcript = root / "transcript.txt"
    transcript.write_text("client_disconnected; cleanup=quiescent\n", encoding="utf-8")
    missing = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.0,
    )
    require(missing["status"] == "unverified", "missing registry was accepted")

    registry.write_text("{malformed", encoding="utf-8")
    malformed = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.0,
    )
    require(malformed["status"] == "unverified", "malformed registry was accepted")

    ticks = process_start_ticks(os.getpid())
    identity = process_birth_identity(os.getpid())
    require(ticks is not None and identity is not None, "registry child identity is unavailable")
    child = {
        "attempt_id": attempt_id,
        "pid": os.getpid(),
        "pgid": os.getpgrp(),
        "process_start_ticks": ticks,
        "process_identity": identity,
    }
    atomic_write_json(registry, registry_payload([child]))
    survivors = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.0,
    )
    require(survivors["status"] == "survivors", "transcript overrode a nonempty registry")

    atomic_write_json(registry, registry_payload([]))
    unseen_empty = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.0,
    )
    require(unseen_empty["status"] == "unverified", "unobserved empty registry proved cleanup")

    atomic_write_json(registry, registry_payload([child]))

    def invalidate_registry() -> None:
        time.sleep(0.02)
        atomic_write_json(registry, registry_payload([{"pid": os.getpid()}]))

    invalidator = threading.Thread(target=invalidate_registry)
    invalidator.start()
    invalid_row = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.5,
    )
    invalidator.join()
    require(invalid_row["status"] == "unverified", "invalid post-observation child row proved cleanup")

    atomic_write_json(registry, registry_payload([child]))

    def clear_registry() -> None:
        time.sleep(0.02)
        atomic_write_json(registry, registry_payload([]))

    clearer = threading.Thread(target=clear_registry)
    clearer.start()
    quiescent = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.5,
    )
    clearer.join()
    require(quiescent["status"] == "quiescent", "observed registry transition was not accepted")

    atomic_write_json(registry, registry_payload([child]))

    def corrupt_registry() -> None:
        time.sleep(0.02)
        registry.write_text("not-json", encoding="utf-8")

    corrupter = threading.Thread(target=corrupt_registry)
    corrupter.start()
    corrupted = wait_for_fork_attempt_cleanup(
        {"active_children_registry": str(registry)},
        attempt_id=attempt_id,
        transcript=transcript,
        timeout_seconds=0.5,
    )
    corrupter.join()
    require(corrupted["status"] == "unverified", "malformed post-observation registry proved cleanup")


def test_broker_reuse_proof() -> None:
    explicit = {
        "status": "prompt_lost",
        "seat_reusable": True,
        "child_quiescent": True,
        "cleanup_verified": True,
    }
    require(broker_response_proves_reusable(explicit), "explicit lifecycle proof was rejected")
    for key in ("seat_reusable", "child_quiescent", "cleanup_verified"):
        incomplete = dict(explicit)
        incomplete.pop(key)
        require(not broker_response_proves_reusable(incomplete), f"missing {key} was accepted")
    require(not broker_response_proves_reusable({"status": "ok"}), "semantic success proved seat reuse")
    require(not broker_response_proves_reusable(None), "missing broker response proved seat reuse")


def new_pool(pool_dir: Path, session_dir: Path) -> dict[str, Any]:
    return {
        "schema": "proof-run.warm-pool.v1",
        "pool_id": pool_dir.name,
        "pool_dir": str(pool_dir),
        "status": "ready",
        "engine": "fork_basis",
        "preloads": [],
        "sessions": [
            {
                "worker_index": 1,
                "session_dir": str(session_dir),
                "status": "idle",
                "lease": None,
                "loaded_preloads": [],
            }
        ],
    }


def test_seat_finalization(root: Path) -> None:
    session_dir = root / "pool-session"
    pool_dir = root / "pool"
    session_dir.mkdir()
    atomic_write_json(session_dir / "session.json", current_session(session_dir))
    write_warm_pool_json(pool_dir, new_pool(pool_dir, session_dir))

    with warm_pool_lock(pool_dir):
        pool = read_warm_pool(pool_dir)
        checked_out = checkout_pool_seat_locked(pool_dir, pool, agent="selftest")
        require(checked_out["status"] == "busy", "checkout did not lease the idle seat")
    with warm_pool_lock(pool_dir):
        pool = read_warm_pool(pool_dir)
        finalized = finalize_fork_pool_seat_locked(
            pool_dir,
            pool,
            session=session_dir,
            reusable=True,
            reason="explicit selftest quiescence",
        )
        require(finalized["status"] == "idle", "proven reusable seat did not return idle")
    persisted = read_warm_pool(pool_dir)
    require(persisted["status"] == "ready", "idle release poisoned the pool")
    require(persisted["sessions"][0]["lease"] is None, "idle release retained its lease")

    with warm_pool_lock(pool_dir):
        pool = read_warm_pool(pool_dir)
        checkout_pool_seat_locked(pool_dir, pool, agent="selftest")
    with warm_pool_lock(pool_dir):
        pool = read_warm_pool(pool_dir)
        quarantined = finalize_fork_pool_seat_locked(
            pool_dir,
            pool,
            session=session_dir,
            reusable=False,
            reason="unverified selftest cleanup",
        )
        require(quarantined["status"] == "poisoned", "unverified seat was not quarantined")
    persisted = read_warm_pool(pool_dir)
    require(persisted["status"] == "poisoned", "unverified seat left pool ready")
    require(persisted["sessions"][0]["lease"] is None, "quarantine retained its lease")


def test_socket_session(root: Path) -> None:
    logical = root / "logical-session"
    physical = root / "physical-session"
    logical.mkdir()
    physical.mkdir()
    atomic_write_json(logical / "session.json", {"control_session_dir": str(physical)})
    endpoint = session_socket_path(logical)
    endpoint.unlink(missing_ok=True)
    ready = threading.Event()
    server_errors: list[BaseException] = []

    def serve_once() -> None:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(endpoint))
                server.listen(1)
                server.settimeout(1.0)
                ready.set()
                connection, _ = server.accept()
                with connection:
                    raw = connection.recv(4096).split(b"\n", 1)[0]
                    request = json.loads(raw.decode("utf-8"))
                    connection.sendall(json.dumps({"status": "ok", "echo": request["value"]}).encode() + b"\n")
        except BaseException as exc:
            server_errors.append(exc)
            ready.set()

    server = threading.Thread(target=serve_once)
    server.start()
    require(ready.wait(1.0), "socket fixture did not become ready")
    response = send_session_request(logical, {"value": "round-trip"}, timeout_seconds=1.0)
    server.join(timeout=1.0)
    endpoint.unlink(missing_ok=True)
    require(not server.is_alive(), "socket fixture did not stop")
    require(not server_errors, f"socket fixture failed: {server_errors}")
    require(response.get("echo") == "round-trip", "session socket response was not preserved")


def test_disposable_timeout_cleanup() -> None:
    child_code = (
        "import signal,time;signal.signal(signal.SIGTERM, lambda *_: None);print('ready', flush=True);time.sleep(30)"
    )
    owner = OwnedProcessSet(term_grace_seconds=0.05, kill_grace_seconds=0.5)
    child = owner.spawn(
        [sys.executable, "-I", "-B", "-c", child_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_stdout = child.process.stdout
    if child_stdout is None:
        raise AssertionError("disposable child stdout was not captured")
    require(child_stdout.readline().strip() == "ready", "disposable child did not become ready")
    receipts = owner.cleanup()
    require(len(receipts) == 1, "disposable cleanup returned an unexpected receipt count")
    receipt = receipts[0]
    require(receipt.verified_quiescent, f"disposable child survived cleanup: {receipt.as_dict()}")
    require(receipt.term_sent and receipt.kill_sent, "timeout cleanup did not escalate TERM to KILL")


def test_broker_frames() -> None:
    left, right = socket.socketpair()
    raw = b"\x00\xffHOL\n\x80"
    try:
        send_control(left, {"schema": "selftest", "operation": "describe"})
        frame_type, encoded = receive_frame(right)
        require(frame_type == BROKER_FRAME_CONTROL, "control frame type changed")
        require(
            json.loads(encoded) == {"operation": "describe", "schema": "selftest"},
            "control frame payload changed",
        )
        send_raw_output(right, raw)
        frame_type, observed = receive_frame(left)
        require(frame_type == BROKER_FRAME_RAW_OUTPUT, "raw-output frame type changed")
        require(observed == raw, "raw-output bytes were decoded or changed")
    finally:
        left.close()
        right.close()

    left, right = socket.socketpair()
    try:
        left.sendall(BROKER_FRAME_HEADER.pack(0x7F, 0))
        require_raises(BrokerProtocolError, "unknown broker frame type", lambda: receive_frame(right))
    finally:
        left.close()
        right.close()

    left, right = socket.socketpair()
    reader = BufferedBrokerFrameReader()
    payload = b"\x00\xfffragmented broker output\n"
    frame = BROKER_FRAME_HEADER.pack(BROKER_FRAME_RAW_OUTPUT, len(payload)) + payload
    right.settimeout(0.01)
    try:
        left.sendall(frame[:2])
        require(reader.receive(right) is None, "partial broker header was treated as complete")
        left.sendall(frame[2:6])
        require(reader.receive(right) is None, "partial broker payload was treated as complete")
        left.sendall(frame[6:])
        require(
            reader.receive(right) == (BROKER_FRAME_RAW_OUTPUT, payload),
            "fragmented broker frame was not reassembled byte-exactly",
        )
    finally:
        left.close()
        right.close()


def test_spawn_registration_gate() -> None:
    stop_event = threading.Event()
    gate = ForkSpawnGate(stop_event)
    spawn_entered = threading.Event()
    allow_registration = threading.Event()
    registration_finished = threading.Event()

    def spawn() -> None:
        with gate.spawning():
            spawn_entered.set()
            allow_registration.wait(timeout=1.0)
            registration_finished.set()

    spawn_thread = threading.Thread(target=spawn)
    stop_thread = threading.Thread(target=gate.begin_stop)
    spawn_thread.start()
    require(spawn_entered.wait(timeout=1.0), "spawn registration did not begin")
    stop_thread.start()
    time.sleep(0.02)
    require(not stop_event.is_set(), "stop became visible before spawn registration finished")
    allow_registration.set()
    spawn_thread.join(timeout=1.0)
    stop_thread.join(timeout=1.0)
    require(registration_finished.is_set(), "spawn registration did not finish")
    require(stop_event.is_set(), "stop was not published after spawn registration finished")


def test_identity_bound_signal() -> None:
    signals: list[int] = []
    mismatch = terminate_process_group(
        99,
        identity_pid=99,
        expected_start_ticks=100,
        require_identity=True,
        term_grace_seconds=0,
        kill_grace_seconds=0,
        signal_group=lambda _pgid, signum: signals.append(signum) or True,
        group_alive=lambda _pgid: True,
        identity_reader=lambda _pid: 200,
    )
    require(mismatch["status"] == "identity_mismatch", "reused PID identity was not refused")
    require(signals == [], "reused PID was signalled")

    signals.clear()
    identities = iter(("birth-a", "birth-b"))
    changed = terminate_process_group(
        99,
        identity_pid=99,
        expected_identity="birth-a",
        require_identity=True,
        term_grace_seconds=0,
        kill_grace_seconds=0,
        signal_group=lambda _pgid, signum: signals.append(signum) or True,
        group_alive=lambda _pgid: True,
        birth_identity_reader=lambda _pid: next(identities),
    )
    require(signals == [signal.SIGTERM], "identity change did not suppress delayed SIGKILL")
    require(changed["status"] == "identity_mismatch", "delayed identity change was not reported")
    require(changed["term_sent"] is True and changed["kill_sent"] is False, "signal receipt is incoherent")


def test_stale_release_fails_closed(root: Path) -> None:
    session = root / "stale-release-session"
    session.mkdir()
    lease = {"lease_id": "old-eval"}
    pool = {
        "engine": "fork_basis",
        "status": "restore_failed",
        "sessions": [
            {
                "worker_index": 1,
                "status": "restore_failed",
                "session_dir": str(session),
                "lease": lease,
            }
        ],
    }
    released = release_warm_pool_seat(
        pool,
        session=session,
        status="dirty",
        released_utc="2026-08-12T00:00:00Z",
    )
    require(released["status"] == "restore_failed", "delayed release reopened a failed seat")
    require(released["lease"] == lease, "delayed release erased lifecycle-owned lease evidence")
    require(
        released["release_reason"]
        == "eval release deferred because pool lifecycle status restore_failed owns seat state",
        "delayed release did not report lifecycle ownership",
    )


HOT_SERVER_FIXTURE = r"""
import os
import socket
import sys

endpoint, nonce = sys.argv[1:]
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(endpoint)
server.listen(4)
print("ready", flush=True)
try:
    while True:
        connection, _ = server.accept()
        with connection:
            request = connection.makefile("rb").readline().decode("ascii").rstrip("\n")
            if request == f"status\t{nonce}":
                connection.sendall(
                    f"__HOL_WORKBENCH_HOT__:{nonce}:ready:server:{os.getpid()}\n".encode("ascii")
                )
            elif request == f"stop\t{nonce}":
                connection.sendall(
                    f"__HOL_WORKBENCH_HOT__:{nonce}:stopping:server:0\n".encode("ascii")
                )
                break
finally:
    server.close()
    try:
        os.unlink(endpoint)
    except FileNotFoundError:
        pass
"""


def selftest_sibling_lease() -> dict[str, str]:
    return {
        "lease_id": "unrelated-sibling-lease",
        "owner_kind": "unrelated-selftest-owner",
    }



def main() -> int:
    if sys.platform != "linux":
        raise SystemExit("memory-first lifecycle selftest is Linux-only")
    with tempfile.TemporaryDirectory(prefix="memory-first-lifecycle-") as temporary:
        root = Path(temporary)
        test_process_identity(root)
        test_registry_authority(root)
        test_broker_reuse_proof()
        test_seat_finalization(root)
        test_socket_session(root)
        test_disposable_timeout_cleanup()
        test_broker_frames()
        test_spawn_registration_gate()
        test_identity_bound_signal()
        test_stale_release_fails_closed(root)
    print("memory_first_lifecycle_selftest=passed cases=10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
