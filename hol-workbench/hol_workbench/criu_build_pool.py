"""Internal fork-basis pool lifecycle used only by CRIU profile builds."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from hol_workbench.fork_broker_protocol import (
    BROKER_FORK_SNAPSHOT_ABI,
    BROKER_PROTOCOL,
    materialize_broker_runtime_bundle,
)
from hol_workbench.hashing import sha256_file
from hol_workbench.jsonio import read_json
from hol_workbench.pools import annotate_warm_pool_basis_state
from hol_workbench.process_groups import OwnedProcess, OwnedProcessSet, process_birth_identity
from hol_workbench.proof_run_fork_broker import BROKER_READY_SCHEMA
from hol_workbench.proof_run_fork_broker_client import broker_control_request
from hol_workbench.proof_run_runtime import default_warm_startup_command, run_id, slugify, utc_now
from hol_workbench.proof_run_warm_session import warm_socket_path, write_warm_pool
from hol_workbench.proof_run_warm_stop import run_warm_stop_command

DEFAULT_FORK_SPAWN_TIMEOUT_SECONDS = 60.0
WARM_POOL_SCHEMA = "proof-run.warm-pool.v1"


def _hol_environment(holdir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    bindir = holdir / "_opam" / "bin"
    stublibs = holdir / "_opam" / "lib" / "stublibs"
    environment["HOLLIGHT_DIR"] = str(holdir)
    environment["HOLLIGHT_USE_MODULE"] = "1"
    environment["LINE_EDITOR"] = "cat"
    environment["PATH"] = f"{bindir}{os.pathsep}{environment.get('PATH', '')}"
    existing = environment.get("CAML_LD_LIBRARY_PATH")
    environment["CAML_LD_LIBRARY_PATH"] = str(stublibs) if not existing else f"{stublibs}{os.pathsep}{existing}"
    return environment


def _preload_record(path: Path, *, status: str) -> dict[str, Any]:
    digest = sha256_file(path)
    if digest is None:
        raise RuntimeError(f"preload source is unreadable: {path}")
    return {"path": str(path), "sha256": digest, "status": status, "attempts": []}


def _build_pool_document(
    *,
    pool_dir: Path,
    holdir: Path,
    cwd: Path,
    startup_argv: list[str],
    session_dirs: list[Path],
    preloads: list[Path],
    profile_basis_id: str,
    broker_manifest: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": WARM_POOL_SCHEMA,
        "pool_id": pool_dir.name,
        "pool_dir": str(pool_dir),
        "status": "starting",
        "created_utc": utc_now(),
        "updated_utc": utc_now(),
        "holdir": str(holdir),
        "cwd": str(cwd),
        "startup_argv": startup_argv,
        "size_requested": len(session_dirs),
        "sessions": [
            {
                "worker_index": index + 1,
                "status": "starting",
                "session_dir": str(session_dir),
                "session_id": session_dir.name,
                "worker_pid": None,
                "hol_pid": None,
                "basis_pid": None,
                "created_utc": None,
                "lease": None,
                "last_attempt": None,
                "loaded_preloads": [],
            }
            for index, session_dir in enumerate(session_dirs)
        ],
        "preloads": [_preload_record(path, status="loading") for path in preloads],
        "raw_log_policy": "do not read wholesale; inspect compact summaries first",
        "evidence_grade": "warm_exploration",
        "engine": "fork_basis",
        "execution_topology": "mechanical_basis_broker_v3",
        "profile_basis_id": profile_basis_id,
        "broker_protocol": BROKER_PROTOCOL,
        "broker_runtime_sha256": broker_manifest["broker_runtime_sha256"],
        "broker_runtime_bundle": str(pool_dir / "broker-runtime"),
        "fork_spawn_timeout_seconds": DEFAULT_FORK_SPAWN_TIMEOUT_SECONDS,
        "lease_policy": "exclusive logical seats; each eval forks a clean child from the loaded basis",
    }


def _validate_ready(
    owned: OwnedProcess,
    ready: dict[str, Any],
    *,
    pool_dir: Path,
    session_dirs: list[Path],
) -> None:
    if ready.get("schema") != BROKER_READY_SCHEMA or ready.get("status") != "ready":
        raise RuntimeError("fork broker readiness has the wrong schema or status")
    if Path(str(ready.get("pool_dir") or "")).resolve() != pool_dir:
        raise RuntimeError("fork broker readiness names a different pool")
    observed_manager = (
        ready.get("worker_pid"),
        ready.get("worker_pgid"),
        ready.get("worker_identity"),
    )
    expected_manager = (
        owned.identity.pid,
        owned.identity.pgid,
        owned.identity.birth_identity,
    )
    if observed_manager != expected_manager:
        raise RuntimeError("fork broker readiness has a different manager identity")
    basis_pid = int(ready.get("basis_pid") or 0)
    observed_basis = (
        basis_pid,
        ready.get("basis_pgid"),
        ready.get("basis_identity"),
    )
    expected_basis = (basis_pid, basis_pid, process_birth_identity(basis_pid))
    if basis_pid <= 0 or observed_basis != expected_basis:
        raise RuntimeError("fork broker readiness has a different basis identity")
    observed_sessions = [Path(value).resolve() for value in ready.get("session_dirs") or []]
    if observed_sessions != session_dirs:
        raise RuntimeError("fork broker readiness names different sessions")


def _mark_ready(pool: dict[str, Any], ready: dict[str, Any], ready_path: Path) -> None:
    loaded_utc = utc_now()
    identity_keys = (
        "worker_pid",
        "worker_pgid",
        "worker_start_ticks",
        "worker_identity",
        "basis_pid",
        "basis_pgid",
        "basis_start_ticks",
        "basis_identity",
        "fork_safety",
        "execution_topology",
        "fork_snapshot_abi",
        "broker_protocol",
        "broker_runtime_sha256",
        "profile_basis_id",
    )
    for item in pool["sessions"]:
        item["status"] = "idle"
        item["hol_pid"] = ready["basis_pid"]
        item["created_utc"] = pool["created_utc"]
        item["fork_spawn_timeout_seconds"] = DEFAULT_FORK_SPAWN_TIMEOUT_SECONDS
        for key in identity_keys:
            item[key] = ready.get(key) or pool.get(key)
        item["loaded_preloads"] = [
            {
                "path": preload["path"],
                "sha256": preload["sha256"],
                "status": "loaded",
                "attempt_dir": None,
                "summary_json": None,
                "log": ready.get("raw_log"),
                "loaded_utc": loaded_utc,
            }
            for preload in pool["preloads"]
        ]
    for preload in pool["preloads"]:
        preload["status"] = "loaded"
    pool["status"] = "ready"
    for key in identity_keys:
        pool[key] = ready.get(key) or pool.get(key)
    pool["fork_ready"] = str(ready_path)
    pool["fork_raw_log"] = ready.get("raw_log")
    annotate_warm_pool_basis_state(pool)


def start_pool(args: argparse.Namespace) -> int:
    if args.size < 1:
        raise SystemExit("--size must be at least 1")
    holdir = args.holdir.expanduser().resolve()
    cwd = args.cwd.expanduser().resolve()
    pool_root = args.pool_root.expanduser().resolve()
    preloads = [path.expanduser().resolve() for path in args.preload]
    pool_dir = pool_root / run_id(slugify(args.label))
    sessions_root = pool_dir / "sessions"
    session_dirs = [sessions_root / f"seat-{index + 1}" for index in range(args.size)]
    for session_dir in session_dirs:
        session_dir.mkdir(parents=True)
    socket_paths = [warm_socket_path(session_dir) for session_dir in session_dirs]
    broker_bundle = pool_dir / "broker-runtime"
    broker_manifest = materialize_broker_runtime_bundle(broker_bundle)
    startup_argv = default_warm_startup_command(holdir)
    pool = _build_pool_document(
        pool_dir=pool_dir,
        holdir=holdir,
        cwd=cwd,
        startup_argv=startup_argv,
        session_dirs=session_dirs,
        preloads=preloads,
        profile_basis_id=args.profile_basis_id,
        broker_manifest=broker_manifest,
    )
    write_warm_pool(pool_dir, pool)
    ready_path = pool_dir / "fork-worker-ready.json"
    bootstrap = (
        "import runpy,sys;"
        "sys.path.insert(0,sys.argv.pop(1));"
        "runpy.run_module('hol_workbench.proof_run_fork_broker',run_name='__main__')"
    )
    command = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        bootstrap,
        str(broker_bundle),
        "--pool-dir",
        str(pool_dir),
        "--holdir",
        str(holdir),
        "--cwd",
        str(cwd),
        "--session-dirs-json",
        json.dumps([str(path) for path in session_dirs]),
        "--socket-paths-json",
        json.dumps([str(path) for path in socket_paths]),
        "--startup-argv-json",
        json.dumps(startup_argv),
        "--environment-json",
        json.dumps(_hol_environment(holdir)),
        "--preloads-json",
        json.dumps([str(path) for path in preloads]),
        "--preload-timeout",
        str(args.preload_timeout),
        "--fork-spawn-timeout",
        str(DEFAULT_FORK_SPAWN_TIMEOUT_SECONDS),
        "--profile-basis-id",
        args.profile_basis_id,
        "--fork-snapshot-abi",
        BROKER_FORK_SNAPSHOT_ABI,
        "--broker-protocol",
        BROKER_PROTOCOL,
        "--broker-runtime-sha256",
        str(broker_manifest["broker_runtime_sha256"]),
    ]
    deadline = time.monotonic() + max(args.startup_timeout, args.preload_timeout + 30.0)
    workers = OwnedProcessSet(term_grace_seconds=1.0, kill_grace_seconds=1.0)
    stdout_path = pool_dir / "fork-worker.out"
    stderr_path = pool_dir / "fork-worker.err"
    owned: OwnedProcess | None = None
    try:
        with workers, stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            owned = workers.spawn(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                close_fds=True,
            )
            while not ready_path.is_file():
                if owned.process.poll() is not None:
                    raise RuntimeError(f"fork broker exited with status {owned.process.returncode} before readiness")
                if time.monotonic() >= deadline:
                    raise RuntimeError("fork broker did not become ready before timeout")
                time.sleep(0.1)
            ready = read_json(ready_path)
            _validate_ready(owned, ready, pool_dir=pool_dir, session_dirs=session_dirs)
            _mark_ready(pool, ready, ready_path)
            write_warm_pool(pool_dir, pool)
            workers.commit(
                owned,
                lambda process: _validate_ready(
                    process,
                    ready,
                    pool_dir=pool_dir,
                    session_dirs=session_dirs,
                ),
            )
    except BaseException as exc:
        cleanup_receipts = [receipt.as_dict() for receipt in workers.last_cleanup_receipts]
        process_started = owned is not None
        pool["startup_cleanup"] = {
            "schema": "hol-workbench.criu-build-pool-startup-cleanup.v1",
            "process_started": process_started,
            "verified_quiescent": (
                not process_started
                or (bool(cleanup_receipts) and all(receipt["verified_quiescent"] for receipt in cleanup_receipts))
            ),
            "receipts": cleanup_receipts,
            "failure_type": type(exc).__name__,
        }
        pool["status"] = "failed"
        for preload in pool["preloads"]:
            preload["status"] = "failed"
        with suppress(OSError):
            write_warm_pool(pool_dir, pool)
        raise
    print(f"PARALLEL_SAFE_WARM_POOL={pool_dir}")
    return 0


def stop_pool(args: argparse.Namespace) -> int:
    pool_dir = args.pool.expanduser().resolve()
    pool = read_json(pool_dir / "pool.json")
    sessions = [item for item in pool.get("sessions") or [] if isinstance(item, dict)]
    if not sessions:
        raise RuntimeError("CRIU build pool has no recorded session")
    session_dir = Path(str(sessions[0]["session_dir"])).resolve()
    session = read_json(session_dir / "session.json")
    status = run_warm_stop_command(
        session_dir=session_dir,
        session=session,
        request_stop=lambda: broker_control_request(session_dir, "stop"),
    )
    stopped_session = read_json(session_dir / "session.json")
    for item in sessions:
        item["status"] = stopped_session.get("status")
        item["stop_result"] = stopped_session.get("stop_result")
        if item["status"] == "stopped":
            item["lease"] = None
    pool["sessions"] = sessions
    pool["status"] = "stopped" if status == 0 else "stop_failed"
    write_warm_pool(pool_dir, pool)
    return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--size", type=int, required=True)
    start.add_argument("--label", required=True)
    start.add_argument("--holdir", type=Path, required=True)
    start.add_argument("--cwd", type=Path, required=True)
    start.add_argument("--pool-root", type=Path, required=True)
    start.add_argument("--startup-timeout", type=float, required=True)
    start.add_argument("--preload", type=Path, action="append", default=[])
    start.add_argument("--preload-timeout", type=float, required=True)
    start.add_argument("--profile-basis-id", required=True)
    stop = commands.add_parser("stop")
    stop.add_argument("--pool", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return start_pool(args) if args.command == "start" else stop_pool(args)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"criu-build-pool: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
