"""Explicit, receipt-admitted project bases layered on a published warm shelf.

Only transport changes: ordinary source preparation, HOL loading, fork phrases,
claim probes, and the mechanical broker remain the public replay machinery.
No profile is built and no instruction is evaluated in the shared shelf parent.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from hol_workbench.fork_child_ocaml import ocaml_string_literal
from hol_workbench.fork_broker_protocol import (
    BROKER_FORK_SNAPSHOT_ABI, BROKER_PROTOCOL, broker_runtime_sha256,
)
from hol_workbench.hashing import sha256_file
from hol_workbench.jsonio import atomic_write_json, read_json
from hol_workbench.process_groups import process_birth_identity, process_start_ticks, terminate_process_group
from hol_workbench.proof_run_fork_basis_safety import admit_single_threaded_basis
from hol_workbench.proof_run_fork_broker import ForkBasisBroker
from hol_workbench.proof_run_fork_broker_client import broker_control_request
from hol_workbench.source_dependency_closure import source_dependency_closure_identity_matches
from hol_workbench.source_dependency_package import dependency_transport_status
from hol_workbench.secure_tree_read import read_regular_file_beneath

SCHEMA = "hol-hearth.project-basis.v1"
ADOPTION_SECONDS = 45.0
MAX_LIVE_BASES = 2


class ProjectBasisBusy(RuntimeError):
    """A live owned generation is still evaluating and cannot be replaced."""


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def backend_sha256() -> str:
    root = Path(__file__).resolve().parent
    # Bind the complete public controller source, including transitive scanner
    # and accounting helpers. A hand-maintained import list can miss a changed
    # preparation contract. Tests, bytecode and machine data are not included.
    sources = {path.relative_to(root).as_posix(): sha256_file(path)
               for path in sorted(root.rglob("*.py"))
               if not any(part in {"tests", "__pycache__"} for part in path.relative_to(root).parts)}
    return _digest({"preparation_runtime": sources,
                    "broker": broker_runtime_sha256()})


@dataclass
class BasisPlan:
    source: Path
    profile_root: Path
    logical_profile: str
    cache_root: Path
    identity: dict[str, Any]
    key: str
    generation: Path | None = None
    nonce: str | None = None
    pending_identity: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def record_path(self) -> Path:
        return self.cache_root / self.key / "basis.json"


@dataclass(frozen=True)
class BasisHandle:
    session: Path
    record: dict[str, Any]
    record_path: Path


def plan_basis(
    basis_source: Path, *, profile_root: Path, logical_profile: str,
    run_root: Path, closure: dict[str, Any],
    profile_satisfaction: dict[str, Any] | None = None,
    profile_identity: dict[str, Any] | None = None,
) -> BasisPlan:
    """Make a content identity from the ordinary freshly captured closure."""
    source = basis_source.expanduser().resolve(strict=True)
    profile = profile_root.expanduser().resolve(strict=True)
    source_sha = sha256_file(source)
    if source_sha is None or closure.get("entrypoint", {}).get("sha256") != source_sha:
        raise ValueError("project basis source differs from its captured dependency closure")
    if not isinstance(closure.get("strict_sha256"), str) or len(closure["strict_sha256"]) != 64:
        raise ValueError("project basis requires an exact dependency closure identity")
    if not source_dependency_closure_identity_matches(closure):
        raise ValueError("project basis dependency closure identity is malformed")
    transport, reason = dependency_transport_status(closure, profile_satisfaction=profile_satisfaction)
    if not transport.startswith("packaged"):
        raise ValueError(f"project basis requires bounded literal dependencies: {reason}")
    manifest_sha = sha256_file(profile / "snapshot-manifest.json")
    if manifest_sha is None:
        raise ValueError("project basis requires an admitted published shelf manifest")
    satisfaction = profile_satisfaction or {}
    inputs = {str(source): source_sha}
    for row in [*(closure.get("records") or []), *(closure.get("artifacts") or [])]:
        path, digest = row.get("resolved_path"), row.get("sha256")
        if isinstance(path, str) and Path(path).is_absolute() and isinstance(digest, str):
            inputs[path] = digest
    for row in satisfaction.get("edges") or []:
        path, digest = row.get("host_path"), row.get("sha256")
        if isinstance(path, str) and Path(path).is_absolute() and isinstance(digest, str):
            inputs[path] = digest
    identity = {
        "schema": SCHEMA, "source": str(source), "source_sha256": source_sha,
        "source_md5": hashlib.md5(source.read_bytes(), usedforsecurity=False).hexdigest(),
        "source_dependency_closure_sha256": closure["strict_sha256"],
        "profile_root": str(profile), "logical_profile": logical_profile,
        "snapshot_manifest_sha256": manifest_sha,
        "profile_satisfaction": {key: satisfaction.get(key) for key in (
            "profile_basis_id", "profile_sha256", "loaded_closure_sha256", "snapshot_environment_sha256",
        )},
        "profile_identity": profile_identity or {}, "backend_sha256": backend_sha256(),
        "broker_runtime_sha256": broker_runtime_sha256(),
        "captured_inputs": inputs,
        "elf_loaders": sorted({row.get("loader") for row in closure.get("artifacts") or []
                               if row.get("loader") in {"define_from_elf", "define_assert_from_elf"}}),
    }
    return BasisPlan(source, profile, logical_profile,
                     run_root.expanduser().resolve() / ".project-bases", identity, _digest(identity))


@contextmanager
def project_basis_lock(run_root: Path) -> Iterator[None]:
    """Protect lookup, preparation, use and bounded eviction as one operation."""
    root = run_root.expanduser().resolve() / ".project-bases"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / ".lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _process_identity(pid: int) -> dict[str, Any]:
    return {"pid": pid, "pgid": os.getpgid(pid), "start_ticks": process_start_ticks(pid),
            "birth_identity": process_birth_identity(pid)}


def _live(identity: Any) -> bool:
    if (not isinstance(identity, dict) or type(identity.get("pid")) is not int
            or type(identity.get("pgid")) is not int or type(identity.get("start_ticks")) is not int
            or not isinstance(identity.get("birth_identity"), str)):
        return False
    pid = identity["pid"]
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        if raw.rpartition(")")[2].split()[0] in {"Z", "X", "x"}:
            return False
        return (pid > 0 and identity["start_ticks"] > 0 and identity.get("pgid") == pid and os.getpgid(pid) == pid
                and identity.get("birth_identity") is not None
                and process_birth_identity(pid) == identity["birth_identity"]
                and process_start_ticks(pid) == identity.get("start_ticks"))
    except (OSError, ValueError, IndexError):
        return False


def _terminate_owned(identity: dict[str, Any] | None) -> None:
    if identity is not None and _live(identity):
        terminate_process_group(identity["pgid"], identity_pid=identity["pid"],
                                expected_identity=identity["birth_identity"],
                                expected_start_ticks=identity["start_ticks"], require_identity=True,
                                term_grace_seconds=0.5, kill_grace_seconds=0.5)


def _current_inputs(plan: BasisPlan) -> None:
    for path, expected in plan.identity["captured_inputs"].items():
        if sha256_file(Path(path)) != expected:
            raise ValueError(f"project basis input changed after capture: {path}")
    if sha256_file(plan.profile_root / "snapshot-manifest.json") != plan.identity["snapshot_manifest_sha256"]:
        raise ValueError("project basis shelf manifest changed after capture")
    if backend_sha256() != plan.identity["backend_sha256"]:
        raise ValueError("project basis backend changed after capture")


def lookup_basis(plan: BasisPlan) -> BasisHandle | None:
    """Accept only the current exact identity and responding owned generation."""
    _current_inputs(plan)
    record = read_json(plan.record_path)
    if record.get("schema") != SCHEMA or record.get("status") != "ready" or record.get("identity") != plan.identity:
        return None
    if not _live(record.get("broker")) or not _live(record.get("basis")):
        _terminate_owned(record.get("broker"))
        _terminate_owned(record.get("basis"))
        record.update(status="retired", retirement_reason="owned_generation_disappeared")
        atomic_write_json(plan.record_path, record)
        return None
    session = Path(str(record.get("session") or ""))
    handle = BasisHandle(session, record, plan.record_path)
    try:
        session.resolve().relative_to(plan.cache_root)
        # Check for a live attempt before any stale-receipt retirement.
        assert_live_basis(handle)
        receipt_path = Path(str(record.get("preparation_receipt") or ""))
        if sha256_file(receipt_path) != record.get("preparation_receipt_sha256"):
            raise ValueError("project basis preparation receipt changed after admission")
        validate_preparation_receipt(plan, receipt_path)
    except ProjectBasisBusy:
        raise
    except (OSError, RuntimeError, ValueError):
        retire_basis(handle, reason="cached_generation_failed_revalidation")
        return None
    record["last_used_epoch_seconds"] = time.time()
    atomic_write_json(plan.record_path, record)
    return BasisHandle(session, record, plan.record_path)


def expected_basis(handle: BasisHandle) -> dict[str, Any]:
    row = handle.record
    return {"broker": row["broker"], "basis": row["basis"],
            "profile_basis_id": "hearth.project." + _digest(row["identity"]),
            "broker_runtime_sha256": row["identity"]["broker_runtime_sha256"]}


def assert_live_basis(handle: BasisHandle) -> None:
    """Bind the serving endpoint to the exact admitted owned process generation."""
    row = handle.record
    if row.get("schema") != SCHEMA or row.get("identity", {}).get("backend_sha256") != backend_sha256():
        raise ValueError("project basis backend identity changed")
    if not _live(row.get("broker")) or not _live(row.get("basis")):
        raise ValueError("project basis owned process identity is not live")
    admit_single_threaded_basis(row["basis"]["pid"])
    status = broker_control_request(handle.session, "status", expected=expected_basis(handle))
    if status.get("status") == "ready" and isinstance(status.get("active_children"), list) and status["active_children"]:
        raise ProjectBasisBusy("project basis generation is evaluating; keep the owned generation")
    if status.get("status") != "ready" or status.get("active_children") != []:
        raise RuntimeError("project basis generation is not idle and ready")


def validate_basis_use(
    handle: BasisHandle, closure: dict[str, Any], profile_satisfaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind the exact packaged leaf inputs to the inherited project state."""
    identity = handle.record["identity"]
    if not source_dependency_closure_identity_matches(closure):
        raise ValueError("project basis leaf dependency closure identity is malformed")
    records = closure.get("records") or []
    basis_edges = [row for row in records if row.get("resolved_path") == identity["source"]]
    if not basis_edges or any(row.get("loader") != "needs" for row in basis_edges):
        raise ValueError("project basis must be an exact literal needs dependency of the leaf")
    captured = {}
    for row in [*records, *(closure.get("artifacts") or [])]:
        path, digest = row.get("resolved_path"), row.get("sha256")
        if isinstance(path, str) and isinstance(digest, str):
            if path in captured and captured[path] != digest:
                raise ValueError("project basis leaf has inconsistent duplicate input identities")
            captured[path] = digest
    for row in (profile_satisfaction or {}).get("edges") or []:
        path, digest = row.get("host_path"), row.get("sha256")
        if isinstance(path, str) and isinstance(digest, str):
            if path in captured and captured[path] != digest:
                raise ValueError("project basis leaf has inconsistent profile input identities")
            captured[path] = digest
    for path, expected in identity["captured_inputs"].items():
        if captured.get(path) != expected or sha256_file(Path(path)) != expected:
            raise ValueError(f"project basis differs from the packaged leaf dependency: {path}")
    assert_live_basis(handle)
    return expected_basis(handle)


def retire_basis(handle: BasisHandle, *, reason: str = "owned_basis_lifecycle_failure") -> None:
    """Retire only this authenticated project generation, never its shelf."""
    row = handle.record
    if row.get("schema") != SCHEMA:
        raise ValueError("cannot retire a non-project basis")
    if _live(row.get("broker")):
        try:
            broker_control_request(handle.session, "stop", expected=expected_basis(handle))
        except (OSError, RuntimeError, ValueError):
            pass
        deadline = time.monotonic() + 5
        while _live(row["broker"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        _terminate_owned(row["broker"])
    _terminate_owned(row.get("basis"))
    row.update(status="retired", retirement_reason=reason)
    atomic_write_json(handle.record_path, row)


def bootstrap_prelude(plan: BasisPlan) -> bytes:
    """Save only harness-managed transport functions before packaging wrappers."""
    loaders = ["needs", "loadt", "loads", "prove", *plan.identity["elf_loaders"]]
    lines = [f"let hearth_basis_original_{name} = {name};;" for name in loaders]
    lines.extend(["let hearth_basis_original_cwd = Sys.getcwd ();;",
                  "let hearth_basis_original_load_path = !load_path;;"])
    return ("\n".join(lines) + "\n").encode()


def _make_room(plan: BasisPlan) -> None:
    # A failed validation may otherwise replace the same index while leaving
    # its previous large HOL process alive and unreachable from the index.
    previous = read_json(plan.record_path)
    if previous.get("schema") == SCHEMA and previous.get("status") == "ready":
        previous_handle = BasisHandle(Path(str(previous.get("session") or "")), previous, plan.record_path)
        if _live(previous.get("broker")) and _live(previous.get("basis")):
            assert_live_basis(previous_handle)
        retire_basis(previous_handle, reason="superseded_owned_generation")
    live = []
    for path in plan.cache_root.glob("*/basis.json"):
        row = read_json(path)
        if row.get("schema") == SCHEMA and row.get("status") == "ready":
            if _live(row.get("broker")) and _live(row.get("basis")):
                live.append((float(row.get("last_used_epoch_seconds") or 0), path, row))
            else:
                _terminate_owned(row.get("broker"))
                _terminate_owned(row.get("basis"))
                row.update(status="retired", retirement_reason="owned_generation_disappeared")
                atomic_write_json(path, row)
    for _, path, row in sorted(live)[:max(0, len(live) - MAX_LIVE_BASES + 1)]:
        session = Path(str(row.get("session") or ""))
        session.resolve().relative_to(plan.cache_root)
        expected = expected_basis(BasisHandle(session, row, path))
        status = broker_control_request(session, "status", expected=expected)
        if status.get("status") != "ready" or status.get("active_children") != []:
            raise RuntimeError("project basis memory limit reached while another basis is in use")
        broker_control_request(session, "stop", expected=expected)
        deadline = time.monotonic() + 5
        while _live(row["broker"]) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _live(row["broker"]):
            raise RuntimeError("owned project basis did not retire within its shutdown budget")
        row.update(status="retired", retirement_reason="run_root_live_basis_limit")
        atomic_write_json(path, row)


def bootstrap_postlude(plan: BasisPlan) -> bytes:
    """Append after the ordinary preparation payload, never before its source."""
    _current_inputs(plan)
    _make_room(plan)
    nonce = secrets.token_hex(24)
    generation = plan.cache_root / plan.key / "generations" / nonce
    generation.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name in ("input.fifo", "output.fifo"):
        os.mkfifo(generation / name, 0o600)
    plan.generation, plan.nonce = generation, nonce
    spec = {"schema": SCHEMA, "identity": plan.identity, "generation": str(generation), "nonce": nonce}
    atomic_write_json(generation / "spec.json", spec)
    lit = ocaml_string_literal
    restore = "\n".join(f"let {name} = hearth_basis_original_{name};;"
                        for name in ["needs", "loadt", "loads", "prove", *plan.identity["elf_loaders"]])
    # These are transport operations. Proof source continues to be loaded by
    # the existing fork_eval_phrase and ordinary HOL file loader.
    text = f'''
{restore}
load_path := hearth_basis_original_load_path;;
Sys.chdir hearth_basis_original_cwd;;
external hearth_basis_exit : int -> 'a = "caml_unix_exit";;
external hearth_basis_close_descriptor : int -> unit = "caml_unix_close";;
let _ =
  Format.pp_print_flush Format.std_formatter ();
  Format.pp_print_flush Format.err_formatter ();
  flush stdout; flush stderr;
  let task_count = Array.fold_left (fun count name ->
    try ignore (int_of_string name); count + 1 with _ -> count)
    0 (Sys.readdir "/proc/self/task") in
  if task_count <> 1 then failwith "project basis preparation created runtime threads";
  let child = Unix.fork () in
  if child = 0 then (
    ignore (Unix.setsid ());
    Sys.set_signal Sys.sigchld Sys.Signal_ignore;
    loaded_files := ({lit(plan.source.name)}, Digest.from_hex {lit(plan.identity['source_md5'])}) :: !loaded_files;
    Array.iter (fun name ->
      try let descriptor = int_of_string name in
          if descriptor > 2 then (try hearth_basis_close_descriptor descriptor with _ -> ())
      with _ -> ()) (Sys.readdir "/proc/self/fd");
    let process_stat = open_in "/proc/self/stat" in
    let stat_line = input_line process_stat in close_in process_stat;
    let stat_tail = String.trim (String.sub stat_line (String.rindex stat_line ')' + 1)
      (String.length stat_line - String.rindex stat_line ')' - 1)) in
    let start_ticks = List.nth (String.split_on_char ' ' stat_tail) 19 in
    let ready = open_out_bin {lit(str(generation / 'child.ready'))} in
    Printf.fprintf ready "%d\\n%s\\n%s\\n" (Unix.getpid ()) start_ticks {lit(nonce)};
    close_out ready;
    let adopted () = try
      let channel = open_in_bin {lit(str(generation / 'adopt'))} in
      let token = input_line channel in close_in_noerr channel; token = {lit(nonce)}
      with _ -> false in
    let deadline = Unix.gettimeofday () +. {ADOPTION_SECONDS} in
    while not (adopted ()) && Unix.gettimeofday () < deadline do
      ignore (Unix.select [] [] [] 0.02)
    done;
    if not (adopted ()) then hearth_basis_exit 78;
    let input_channel = open_in_bin {lit(str(generation / 'input.fifo'))} in
    let output_channel = open_out_bin {lit(str(generation / 'output.fifo'))} in
    Unix.dup2 (Unix.descr_of_in_channel input_channel) Unix.stdin;
    close_in input_channel;
    Unix.dup2 (Unix.descr_of_out_channel output_channel) Unix.stdout;
    Unix.dup2 (Unix.descr_of_out_channel output_channel) Unix.stderr;
    close_out output_channel;
    let commands = Lexing.from_channel stdin in
    (try while true do
       let phrase = !Toploop.parse_toplevel_phrase commands in
       ignore (Toploop.execute_phrase true Format.std_formatter phrase);
       Format.pp_print_flush Format.std_formatter ();
       Format.pp_print_flush Format.err_formatter ();
       flush stdout; flush stderr
     done with End_of_file -> ());
    hearth_basis_exit 0
  ) else (
    let deadline = Unix.gettimeofday () +. 3.0 in
    while not (Sys.file_exists {lit(str(generation / 'child.ready'))}) && Unix.gettimeofday () < deadline do
      ignore (Unix.select [] [] [] 0.01)
    done;
    if not (Sys.file_exists {lit(str(generation / 'child.ready'))}) then
      failwith "project basis child did not acknowledge preparation"
  );;
'''
    return text.encode("utf-8")


def _pending_identity(plan: BasisPlan) -> dict[str, Any]:
    if plan.generation is None or plan.nonce is None:
        raise ValueError("project basis bootstrap was not prepared")
    lines = (plan.generation / "child.ready").read_text().splitlines()
    if len(lines) != 3 or lines[2] != plan.nonce:
        raise ValueError("project basis child acknowledgement is malformed")
    identity = _process_identity(int(lines[0]))
    if identity.get("start_ticks") != int(lines[1]) or not _live(identity):
        raise ValueError("project basis child is no longer alive")
    admit_single_threaded_basis(identity["pid"])
    plan.pending_identity = identity
    return identity


def abort_basis(plan: BasisPlan) -> None:
    """Stop only the nonce-acknowledged pending child owned by this attempt."""
    try:
        identity = plan.pending_identity or _pending_identity(plan)
    except (OSError, ValueError, RuntimeError):
        return
    _terminate_owned(identity)


def _validate_retained_package(plan: BasisPlan, receipt: dict[str, Any]) -> None:
    raw_root = receipt.get("preparation_package_root")
    if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
        raise ValueError("project basis preparation package location is missing")
    root = Path(raw_root)
    generations = plan.record_path.parent / "generations"
    try:
        relative = root.relative_to(generations)
    except ValueError as exc:
        raise ValueError("project basis preparation package is outside its owned generations") from exc
    if len(relative.parts) != 2 or relative.name != "source-package":
        raise ValueError("project basis preparation package is not an owned generation package")
    if plan.generation is not None and root != plan.generation / "source-package":
        raise ValueError("project basis preparation package belongs to another pending generation")
    rows = receipt.get("dependency_package_files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("project basis preparation package inventory is missing")
    seen: set[str] = set()
    entrypoints = 0
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("package_path"), str):
            raise ValueError("project basis preparation package inventory is malformed")
        relative_path = Path(item["package_path"])
        if (relative_path.is_absolute() or ".." in relative_path.parts
                or relative_path.as_posix() in seen):
            raise ValueError("project basis preparation package path is not unique and relative")
        seen.add(relative_path.as_posix())
        entrypoint = item.get("role") == "entrypoint"
        entrypoints += int(entrypoint)
        expected = receipt.get("executed_source_sha256") if entrypoint else item.get("sha256")
        captured = read_regular_file_beneath(root, root / relative_path)
        if (not isinstance(expected, str) or len(expected) != 64
                or hashlib.sha256(captured.data).hexdigest() != expected
                or (not entrypoint and captured.size != item.get("size_bytes"))):
            raise ValueError(f"project basis retained preparation bytes changed: {relative_path}")
    if entrypoints != 1:
        raise ValueError("project basis preparation package must have one instrumented entrypoint")


def validate_preparation_receipt(plan: BasisPlan, receipt_path: Path) -> dict[str, Any]:
    row = read_json(receipt_path)
    required = {
        "schema": "hol-workbench.warm-vanilla-artifact.v1", "evidence": "recorded_warm_replay",
        "source": str(plan.source), "source_sha256": plan.identity["source_sha256"],
        "source_dependency_closure_sha256": plan.identity["source_dependency_closure_sha256"],
        "logical_profile": plan.logical_profile, "source_completed": True,
        "claims_complete": True, "exit_status": 0, "transport_status": "completed",
        "included_file_error_observed": False,
        "completion_marker_valid": True, "semantic_exit_status": 0, "worker_exit_status": 0,
    }
    for name, value in required.items():
        if type(row.get(name)) is not type(value) or row.get(name) != value:
            raise ValueError(f"project basis preparation receipt refused: {name}")
    accounting = row.get("transcript_accounting") or {}
    for name in ("source_completed", "claims_complete", "included_file_error_observed",
                 "completion_marker_valid", "semantic_exit_status"):
        if type(accounting.get(name)) is not type(required[name]) or accounting.get(name) != required[name]:
            raise ValueError(f"project basis preparation accounting disagrees: {name}")
    foundation = row.get("foundation_delta") or {}
    if (foundation.get("status") != "observed" or type(foundation.get("new_axiom_count")) is not int
            or foundation.get("new_axiom_count") != 0 or foundation != accounting.get("foundation_delta")):
        raise ValueError("project basis preparation requires an observed zero axiom delta")
    raw = Path(str(row.get("raw_transcript") or ""))
    if not raw.is_file() or sha256_file(raw) != row.get("raw_transcript_sha256"):
        raise ValueError("project basis preparation raw transcript identity changed")
    _validate_retained_package(plan, row)
    _current_inputs(plan)
    return row


def adopt_basis(plan: BasisPlan, receipt_path: Path) -> BasisHandle:
    """Publish a new basis only after the ordinary recorded source check passed."""
    broker: subprocess.Popen[bytes] | None = None
    try:
        receipt = validate_preparation_receipt(plan, receipt_path)
        identity = _pending_identity(plan)
        assert plan.generation is not None and plan.nonce is not None
        generation = plan.generation
        spec_path = generation / "spec.json"
        spec = read_json(spec_path)
        # Keep sockets beneath the Unix pathname limit while retaining a private
        # per-generation directory owned by the current uid.
        socket_dir = Path("/tmp") / f"hearth-basis-{os.getuid()}-{plan.nonce[:20]}"
        socket_dir.mkdir(mode=0o700)
        spec.update(basis=identity, preparation_receipt=str(receipt_path.resolve()),
                    preparation_receipt_sha256=sha256_file(receipt_path),
                    socket=str(socket_dir / "broker.sock"))
        atomic_write_json(spec_path, spec)
        with (generation / "broker.log").open("ab") as log:
            environment = dict(os.environ)
            package_root = str(Path(__file__).resolve().parent.parent)
            environment["PYTHONPATH"] = package_root
            broker = subprocess.Popen([sys.executable, "-m", "hol_workbench.project_basis", "--worker", str(spec_path)],
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                      close_fds=True, start_new_session=True, env=environment)
        deadline = time.monotonic() + 10
        session = generation / "session"
        expected = {"broker": _process_identity(broker.pid), "basis": identity,
                    "profile_basis_id": "hearth.project." + plan.key,
                    "broker_runtime_sha256": plan.identity["broker_runtime_sha256"]}
        while time.monotonic() < deadline:
            if broker.poll() is not None:
                raise RuntimeError(f"project basis broker exited; see {generation / 'broker.log'}")
            if (session / "session.json").is_file():
                response = broker_control_request(session, "status", expected=expected)
                if response.get("status") == "ready":
                    break
            time.sleep(0.05)
        else:
            raise RuntimeError("project basis broker did not acknowledge adoption")
        record = {
            "schema": SCHEMA, "status": "ready", "identity": plan.identity,
            "generation": str(generation), "session": str(session),
            "broker": _process_identity(broker.pid), "basis": identity,
            "preparation_receipt": str(receipt_path.resolve()),
            "preparation_receipt_sha256": sha256_file(receipt_path),
            "preparation_eval_elapsed_seconds": receipt.get("eval_elapsed_seconds"),
            "created_epoch_seconds": time.time(), "last_used_epoch_seconds": time.time(),
            "retention": "run_root_owned_bounded_lru", "max_live_bases": MAX_LIVE_BASES,
        }
        atomic_write_json(plan.record_path, record)
        return BasisHandle(session, record, plan.record_path)
    except BaseException:
        if broker is not None and broker.poll() is None:
            _terminate_owned(_process_identity(broker.pid))
        abort_basis(plan)
        raise


class _AttachedProcess:
    """The small Popen interface used by the public mechanical broker."""

    def __init__(self, identity: dict[str, Any], generation: Path):
        self.identity = identity
        self.pid = identity["pid"]
        self.returncode: int | None = None
        for name in ("input.fifo", "output.fifo"):
            if not stat.S_ISFIFO((generation / name).stat().st_mode):
                raise ValueError("project basis transport is not an owned FIFO")
        self.stdin = os.fdopen(os.open(generation / "input.fifo", os.O_RDWR), "wb", buffering=0)
        self.stdout = os.fdopen(os.open(generation / "output.fifo", os.O_RDWR), "rb", buffering=0)

    def poll(self) -> int | None:
        if not _live(self.identity):
            self.returncode = 1
        return self.returncode


class _ProjectBasisBroker(ForkBasisBroker):
    """Attach an already checked HOL process; inherit all ordinary fork policy."""

    def __init__(self, spec: dict[str, Any]):
        self.spec = spec
        generation = Path(spec["generation"])
        super().__init__(argparse.Namespace(
            pool_dir=str(generation), holdir=spec["identity"]["profile_root"], cwd=str(generation),
            session_dirs_json=json.dumps([str(generation / "session")]),
            socket_paths_json=json.dumps([spec["socket"]]), startup_argv_json="[]",
            environment_json="{}", preloads_json="[]", preload_timeout=None,
            fork_spawn_timeout=15.0, profile_basis_id="hearth.project." + _digest(spec["identity"]),
            fork_snapshot_abi=BROKER_FORK_SNAPSHOT_ABI, broker_protocol=BROKER_PROTOCOL,
            broker_runtime_sha256=broker_runtime_sha256(), allow_non_linux_test_basis=False,
        ))

    def _start_basis(self) -> None:
        spec = self.spec
        if spec["identity"]["backend_sha256"] != backend_sha256():
            raise ValueError("project basis backend changed before adoption")
        if sha256_file(Path(spec["preparation_receipt"])) != spec["preparation_receipt_sha256"]:
            raise ValueError("project basis preparation receipt changed before adoption")
        identity = spec["basis"]
        if not _live(identity):
            raise ValueError("project basis process identity changed before adoption")
        admit_single_threaded_basis(identity["pid"])
        generation = Path(spec["generation"])
        self.basis = _AttachedProcess(identity, generation)  # type: ignore[assignment]
        self.basis_identity = identity
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.basis.stdout, selectors.EVENT_READ)
        (generation / "adopt").write_text(spec["nonce"] + "\n")
        token = "__HEARTH_PROJECT_BASIS_READY__:" + spec["nonce"]
        self._send_parent(("print_endline " + ocaml_string_literal(token) + ";;\n").encode())
        self._read_parent_until((token.encode(),), timeout=5, purpose="project basis adoption")

    def _publish_ready(self) -> None:
        super()._publish_ready()
        path = self.session_dirs[0] / "session.json"
        data = read_json(path)
        data["project_basis_backend_sha256"] = self.spec["identity"]["backend_sha256"]
        data["project_basis_identity_sha256"] = _digest(self.spec["identity"])
        atomic_write_json(path, data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = read_json(args.worker)
    if spec.get("schema") != SCHEMA:
        raise ValueError("project basis worker spec is invalid")
    broker = _ProjectBasisBroker(spec)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _signum, _frame: broker.stop_event.set())
    return broker.run()


if __name__ == "__main__":
    raise SystemExit(main())
