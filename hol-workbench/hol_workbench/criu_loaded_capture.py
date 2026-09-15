"""Disposable fork-child capture of CRIU shelf loaded/runtime provenance."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hol_workbench.criu_loaded_capture_schema import CAPTURE_FILENAME, CAPTURE_SCHEMA
from hol_workbench.criu_loaded_provenance import (
    LoadedProvenanceError,
    SemanticInput,
    build_runtime_closure,
    loader_export_source,
    parse_loader_export,
    resolve_loaded_closure,
    validate_loaded_closure,
    validate_runtime_closure,
)
from hol_workbench.jsonio import durable_atomic_write_json
from hol_workbench.pools import read_warm_pool, warm_pool_lock
from hol_workbench.pools.seat_lifecycle import checkout_pool_seat_locked, release_pool_seat_locked
from hol_workbench.proof_run_warm_session import warm_send_request
from hol_workbench.warm_eval_timeout import warm_eval_response_timeout


@dataclass(frozen=True)
class RuntimeProcessPaths:
    ocaml_hol: Path
    ocamlrun: Path
    argv: tuple[str, ...]


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _checkout(pool: Path) -> Path:
    with warm_pool_lock(pool):
        data = read_warm_pool(pool)
        item = checkout_pool_seat_locked(
            pool,
            data,
            agent=os.environ.get("HOL_WORKBENCH_AGENT"),
            owner_kind="criu_provenance_capture",
            owner_command="orbstack-criu build provenance capture",
        )
    return Path(str(item["session_dir"]))


def _release(pool: Path, session: Path, *, clean: bool, reason: str) -> None:
    with warm_pool_lock(pool):
        data = read_warm_pool(pool)
        release_pool_seat_locked(
            pool,
            data,
            session=session,
            status="idle" if clean else "dirty",
            reason=reason,
        )


def _proc_vector(path: Path) -> tuple[str, ...]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LoadedProvenanceError(f"cannot read process metadata {path}: {exc}") from exc
    return tuple(os.fsdecode(item) for item in raw.split(b"\0") if item)


def _process_environment(process_root: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for entry in _proc_vector(process_root / "environ"):
        key, separator, value = entry.partition("=")
        if separator:
            environment[key] = value
    return environment


def _resolve_process_argument(argument: str, *, cwd: Path, path_value: str) -> list[Path]:
    path = Path(argument)
    if path.is_absolute():
        return [path]
    if "/" in argument:
        return [Path(os.path.abspath(cwd / path))]
    candidates = []
    for directory in path_value.split(os.pathsep):
        base = cwd if not directory else Path(directory)
        if not base.is_absolute():
            base = cwd / base
        candidate = Path(os.path.abspath(base / argument))
        if candidate.is_file():
            candidates.append(candidate)
    return candidates


def runtime_paths_from_process(pid: int, *, proc_root: Path = Path("/proc")) -> RuntimeProcessPaths:
    """Derive the exact bytecode and interpreter paths from one live OCaml process."""

    if pid <= 0:
        raise LoadedProvenanceError(f"OCaml process PID is not positive: {pid}")
    process_root = proc_root / str(pid)
    try:
        cwd = (process_root / "cwd").resolve(strict=True)
        ocamlrun = Path(os.readlink(process_root / "exe"))
    except OSError as exc:
        raise LoadedProvenanceError(f"cannot resolve OCaml process {pid} runtime metadata: {exc}") from exc
    if not ocamlrun.is_absolute():
        ocamlrun = Path(os.path.abspath(cwd / ocamlrun))
    if not ocamlrun.is_file() or not ocamlrun.name.startswith("ocamlrun"):
        raise LoadedProvenanceError(f"OCaml process {pid} executable is not ocamlrun: {ocamlrun}")
    argv = _proc_vector(process_root / "cmdline")
    if not argv:
        raise LoadedProvenanceError(f"OCaml process {pid} has an empty command line")
    environment = _process_environment(process_root)
    candidates: set[Path] = set()
    for argument in argv:
        if not Path(argument).name.startswith("ocaml-hol"):
            continue
        candidates.update(
            path
            for path in _resolve_process_argument(
                argument,
                cwd=cwd,
                path_value=environment.get("PATH", ""),
            )
            if path.is_file()
        )
    if len(candidates) != 1:
        rendered = ", ".join(str(path) for path in sorted(candidates)) or "none"
        raise LoadedProvenanceError(
            f"OCaml process {pid} must expose exactly one ocaml-hol command path; found {rendered}"
        )
    return RuntimeProcessPaths(ocaml_hol=next(iter(candidates)), ocamlrun=ocamlrun, argv=argv)


def _capture_response_succeeded(response: dict[str, Any] | None) -> bool:
    return bool(
        response is not None
        and response.get("status") == "ok"
        and response.get("exit_status") == 0
        and response.get("sentinel_observed") is True
        and response.get("child_quiescent") is True
    )


def _validate_capture_response(response: dict[str, Any]) -> None:
    if not _capture_response_succeeded(response):
        raise LoadedProvenanceError(
            "loader provenance child did not complete cleanly: "
            f"status={response.get('status')} exit={response.get('exit_status')} "
            f"sentinel={response.get('sentinel_observed')} quiescent={response.get('child_quiescent')}"
        )


def capture_loaded_provenance(
    *,
    pool: Path,
    output_dir: Path,
    holdir: Path,
    semantic_inputs: list[SemanticInput],
    ocaml_pid: int,
    timeout_seconds: float = 30.0,
    checkout: Callable[[Path], Path] = _checkout,
    release: Callable[..., None] = _release,
    send: Callable[..., dict[str, Any]] = warm_send_request,
    runtime_paths: Callable[[int], RuntimeProcessPaths] = runtime_paths_from_process,
) -> dict[str, Any]:
    """Capture exact provenance and release the disposable child before returning."""

    pool = pool.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source = output_dir / "loaded-files-export.ml"
    transcript = output_dir / "loaded-files-export.log"
    source.write_text(loader_export_source(), encoding="utf-8")
    transcript.unlink(missing_ok=True)

    session = checkout(pool)
    response: dict[str, Any] | None = None
    try:
        response = send(
            session,
            {
                "action": "eval",
                "attempt_id": f"criu-provenance-{secrets.token_hex(6)}",
                "source": str(source),
                "transcript_path": str(transcript),
                "timeout_seconds": timeout_seconds,
                "idle_timeout_seconds": min(timeout_seconds, 10.0),
                "raw_source": True,
            },
            timeout_seconds=warm_eval_response_timeout(timeout_seconds),
        )
    finally:
        clean = _capture_response_succeeded(response)
        release(
            pool,
            session,
            clean=clean,
            reason=(
                f"CRIU provenance child {response.get('status')}"
                if response is not None
                else "CRIU provenance client ended before a worker response"
            ),
        )
    if response is None:
        raise LoadedProvenanceError("loader provenance worker returned no response")
    _validate_capture_response(response)
    if not transcript.is_file():
        raise LoadedProvenanceError(f"loader provenance transcript is missing: {transcript}")

    records = parse_loader_export(transcript.read_text(encoding="utf-8", errors="surrogateescape"))
    loaded_closure = resolve_loaded_closure(
        records,
        holdir=holdir,
        semantic_inputs=semantic_inputs,
    )
    process_paths = runtime_paths(ocaml_pid)
    runtime_closure = build_runtime_closure(
        ocaml_hol=process_paths.ocaml_hol,
        ocamlrun=process_paths.ocamlrun,
    )
    failures = [
        *validate_loaded_closure(loaded_closure, holdir=holdir),
        *validate_runtime_closure(runtime_closure),
    ]
    if failures:
        raise LoadedProvenanceError("captured snapshot provenance is not self-validating: " + "; ".join(failures))

    path = output_dir / CAPTURE_FILENAME
    payload = {
        "schema": CAPTURE_SCHEMA,
        "status": "captured_pre_dump",
        "admission_authority": False,
        "evidence_boundary": (
            "pre-dump build provenance only; shelf admission remains snapshot-manifest plus successful build row"
        ),
        "pool": str(pool),
        "holdir": str(holdir.expanduser().resolve()),
        "loaded_closure": loaded_closure,
        "runtime_closure": runtime_closure,
        "runtime_process": {
            "pid": ocaml_pid,
            "argv": list(process_paths.argv),
            "ocaml_hol": str(process_paths.ocaml_hol),
            "ocamlrun": str(process_paths.ocamlrun),
        },
        "capture": {
            "source": str(source),
            "transcript": str(transcript),
            "child_pid": response.get("hol_pid"),
            "child_pgid": response.get("hol_pgid"),
            "child_quiescent": True,
            "sentinel_observed": True,
            "eval_elapsed_seconds": response.get("eval_elapsed_seconds"),
        },
        "created_utc": _utc_now(),
    }
    durable_atomic_write_json(path, payload)
    return {**payload, "path": str(path)}
