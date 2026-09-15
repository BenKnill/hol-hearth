"""Memory-first live authoring loop behind the public ``prove`` command."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import signal
import socket
import stat
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from hol_workbench.authoring_source_path import AuthoringSourcePathError, resolve_authoring_source
from hol_workbench.cli.prove_profiles import public_authoring_profile_names
from hol_workbench.cli.public_commands import loop_remediation
from hol_workbench.cli.published_profile import PublishedWarmProfile, resolve_published_warm_profile
from hol_workbench.fork_child_ocaml import ocaml_string_literal
from hol_workbench.hot_profile_session import HotBootstrapInterrupted, run_published_hot_bootstrap
from hol_workbench.jsonio import _remove_tree as remove_tree
from hol_workbench.logical_source_roots import (
    LogicalSourceRootError,
    classify_logical_literal,
    resolve_logical_source_roots,
    validate_requested_logical_source_roots,
)
from hol_workbench.profile_satisfied_dependencies import ProfileSatisfactionError
from hol_workbench.proof_run_warm_pool_interrupt import (
    WarmPoolEvalInterrupted,
    WarmPoolEvalSignalState,
    warm_pool_eval_signal_guard,
)
from hol_workbench.proofs.loader_scan import scan_ocaml_loaders
from hol_workbench.proofs.profile_inference import infer_public_profile_for_source
from hol_workbench.proofs.project_input_context import nearest_repository_root
from hol_workbench.proofs.source import extract_define_from_elf_paths, extract_hol_needs
from hol_workbench.secure_tree_read import read_regular_file_beneath
from hol_workbench.source_dependency_closure import SourceDependencyInferenceError
from hol_workbench.source_dependency_package import (
    DependencyPackageError,
    materialize_session_dependency_package,
    session_dependency_projection,
)
from hol_workbench.source_execution_plan import (
    capture_source_dependency_closure,
    decide_profile_satisfaction,
    source_execution_prelude,
)

RING_BYTES = 64 * 1024
DEFAULT_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    pgrp: int
    session: int
    start_ticks: int


@dataclass
class SessionDependencyState:
    projection_sha256: str = ""
    package_root: Path | None = None
    virtual_entrypoint: Path | None = None
    materialization_count: int = 0
    files: tuple[dict[str, object], ...] = ()


def process_identity(pid: int, *, require_isolated: bool = True) -> ProcessIdentity:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    fields = raw[raw.rfind(")") + 2 :].split()
    identity = ProcessIdentity(
        pid=pid,
        pgrp=int(fields[2]),
        session=int(fields[3]),
        start_ticks=int(fields[19]),
    )
    if require_isolated and (identity.pgrp != pid or identity.session != pid):
        raise RuntimeError(f"hot child {pid} did not establish an isolated process group/session")
    return identity


def _same_process(identity: ProcessIdentity) -> bool:
    try:
        current = process_identity(identity.pid, require_isolated=False)
    except (FileNotFoundError, ProcessLookupError, ValueError):
        return False
    return current.start_ticks == identity.start_ticks


def _append_ring(ring: deque[bytes], payload: bytes, size: int) -> int:
    ring.append(payload)
    size += len(payload)
    while size > RING_BYTES and ring:
        size -= len(ring.popleft())
    return size


@dataclass(frozen=True)
class HotChildStatus:
    kind: str
    code: int
    signal_name: str | None = None

    @property
    def completed_code(self) -> int:
        return 128 + self.code if self.kind == "signaled" else self.code

    def diagnostic(self) -> str:
        if self.kind == "signaled":
            return f"terminated by {self.signal_name} (signal {self.code})"
        return f"exited {self.code}"


def _parse_hot_child_status(payload: str) -> HotChildStatus:
    fields = payload.split(":", 2)
    if len(fields) not in {2, 3} or fields[0] not in {"exited", "signaled"}:
        raise RuntimeError(f"hot session returned an invalid child status: {payload!r}")
    kind, code_text = fields[:2]
    if not code_text.isascii() or not code_text.isdigit():
        raise RuntimeError(f"hot session returned an invalid child status: {payload!r}")
    code = int(code_text)
    if kind == "exited":
        if len(fields) != 2 or not 0 <= code <= 255:
            raise RuntimeError(f"hot session returned an invalid child status: {payload!r}")
        return HotChildStatus(kind, code)
    if len(fields) != 3 or not 1 <= code <= 127:
        raise RuntimeError(f"hot session returned an invalid child status: {payload!r}")
    signal_name = fields[2]
    if not signal_name.startswith("SIG") or not signal_name[3:].replace("_", "").isalnum():
        raise RuntimeError(f"hot session returned an invalid child status: {payload!r}")
    return HotChildStatus(kind, code, signal_name)


def evaluate(socket_path: Path, nonce: str, source: bytes, timeout: int) -> tuple[str, float]:
    attempt = secrets.token_hex(12)
    prefix = f"__HOL_WORKBENCH_HOT__:{nonce}:"
    spawned = f"{prefix}spawned:{attempt}:"
    result = f"{prefix}result:{attempt}:"
    status = f"{prefix}status:{attempt}:"
    completed = f"{prefix}completed:{attempt}:"
    error = f"{prefix}error:{attempt}:"
    ring: deque[bytes] = deque()
    ring_size = 0
    card = ""
    child: ProcessIdentity | None = None
    child_status: HotChildStatus | None = None
    started = time.monotonic()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout + 5.0)
        client.connect(str(socket_path))
        header = f"eval\t{nonce}\t{attempt}\t{timeout}\t{len(source)}\n".encode("ascii")
        client.sendall(header + source)
        with client.makefile("rb") as incoming:
            while True:
                raw = incoming.readline(RING_BYTES + 1)
                if not raw:
                    raise RuntimeError("hot session disconnected before completion")
                ring_size = _append_ring(ring, raw, ring_size)
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                if spawned in line:
                    child = process_identity(int(line.split(spawned, 1)[1]))
                    client.sendall(f"ack\t{nonce}\t{attempt}\n".encode("ascii"))
                elif result in line:
                    card = line.split(result, 1)[1]
                elif status in line:
                    if child_status is not None:
                        raise RuntimeError("hot session returned duplicate child status")
                    child_status = _parse_hot_child_status(line.split(status, 1)[1])
                elif completed in line:
                    code = int(line.split(completed, 1)[1])
                    if child is None:
                        raise RuntimeError("hot session completed without registered child identity")
                    if child_status is not None and child_status.completed_code != code:
                        raise RuntimeError(
                            "hot session child status disagrees with its completion code: "
                            f"{child_status.diagnostic()}; completed={code}"
                        )
                    if code != 0 and not card.startswith("TIMEOUT "):
                        tail = b"".join(ring)[-4096:].decode("utf-8", errors="replace")
                        detail = child_status.diagnostic() if child_status is not None else f"exited {code}"
                        raise RuntimeError(
                            f"hot child {detail}; completed={code}; bounded output:\n{tail}"
                        )
                    if not card:
                        tail = b"".join(ring)[-4096:].decode("utf-8", errors="replace")
                        raise RuntimeError(f"hot child completed without a compact result; bounded output:\n{tail}")
                    return card, time.monotonic() - started
                elif error in line:
                    raise RuntimeError(f"hot session refused request: {line.split(error, 1)[1]}")


def _server_identity(socket_path: Path, nonce: str) -> ProcessIdentity:
    prefix = f"__HOL_WORKBENCH_HOT__:{nonce}:ready:server:"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2.0)
        client.connect(str(socket_path))
        client.sendall(f"status\t{nonce}\n".encode("ascii"))
        with client.makefile("rb") as incoming:
            raw = incoming.readline(4097)
    line = raw.decode("utf-8", errors="replace").rstrip("\n")
    if not line.startswith(prefix):
        raise RuntimeError(f"hot session returned an invalid readiness marker: {line!r}")
    pid_text = line.removeprefix(prefix)
    if not pid_text.isascii() or not pid_text.isdigit():
        raise RuntimeError(f"hot session returned an invalid server PID: {pid_text!r}")
    return process_identity(int(pid_text))


def _stop(socket_path: Path, nonce: str, server: ProcessIdentity) -> None:
    acknowledgement = f"__HOL_WORKBENCH_HOT__:{nonce}:stopping:server:0"
    if _same_process(server):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3.0)
            client.connect(str(socket_path))
            client.sendall(f"stop\t{nonce}\n".encode("ascii"))
            with client.makefile("rb") as incoming:
                raw = incoming.readline(4097)
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if line != acknowledgement:
            raise RuntimeError(f"hot session returned an invalid stop acknowledgement: {line!r}")

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not _same_process(server) and not socket_path.exists():
            return
        time.sleep(0.01)
    raise RuntimeError(f"hot server {server.pid} did not quiesce or remove {socket_path} after stop")


def _cleanup_session(
    session_dir: Path,
    socket_path: Path,
    nonce: str,
    server: ProcessIdentity | None,
) -> str | None:
    try:
        if server is None and socket_path.exists():
            server = _server_identity(socket_path, nonce)
        if server is not None:
            _stop(socket_path, nonce, server)
        elif socket_path.exists():
            raise RuntimeError("hot session socket exists without a verified server identity")
        _remove_tree(session_dir)
    except (OSError, RuntimeError) as exc:
        return str(exc)
    return None


def _defer_interrupts(signal_state: WarmPoolEvalSignalState) -> int | None:
    """Close the armed-to-deferred race before synchronous loop cleanup."""

    try:
        signal_state.defer()
    except WarmPoolEvalInterrupted as exc:
        signal_state.defer()
        return exc.signum
    except KeyboardInterrupt:
        signal_state.defer()
        return signal.SIGINT
    return None


def stable_source(path: Path) -> tuple[bytes, tuple[int, int, int]]:
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    before_key = (before.st_ino, before.st_size, before.st_mtime_ns)
    after_key = (after.st_ino, after.st_size, after.st_mtime_ns)
    if before_key != after_key or len(payload) != after.st_size:
        raise BlockingIOError("source changed while it was being read")
    return payload, after_key


def _native_eval_candidate_problem(checkout: Path, candidate: Path) -> str | None:
    source_dir = checkout / "hol-workbench" / "native-eval"
    artifact = candidate / "workbench_hot_session.cma"
    built_sources: list[Path] = []
    for name in ("eval.ml", "hot_session.ml"):
        source = source_dir / name
        built = candidate / name
        try:
            if source.read_bytes() != built.read_bytes():
                return f"built {name} differs from the checkout source"
        except OSError as exc:
            return f"cannot compare {name}: {exc}"
        built_sources.append(built)
    try:
        if artifact.stat().st_mtime_ns < max(path.stat().st_mtime_ns for path in built_sources):
            return "CMA predates its exact built sources"
    except OSError as exc:
        return f"cannot inspect the CMA: {exc}"
    return None


def _native_eval_dir() -> Path:
    checkout = Path(__file__).resolve().parents[3]
    candidates = (
        checkout / "_build" / "default" / "hol-workbench" / "native-eval",
        checkout / "hol-workbench" / "_build" / "default" / "native-eval",
    )
    for candidate in candidates:
        if (candidate / "workbench_hot_session.cma").is_file() and (
            candidate / ".workbench_hot_session.objs" / "byte"
        ).is_dir():
            if problem := _native_eval_candidate_problem(checkout, candidate):
                raise RuntimeError(
                    "hot-session evaluator is stale: "
                    f"{problem}; a Workbench developer must build "
                    "hol-workbench/native-eval/workbench_hot_session.cma"
                )
            return candidate
    raise RuntimeError("hot-session evaluator is not built; a Workbench developer must build its exact CMA target")


def _source_import_roots(
    source: Path,
    profile_cwd: Path | None,
    declarations: tuple[dict[str, str], ...] = (),
    profile_root: Path | None = None,
) -> dict[str, Path]:
    """Resolve the Phase-1 loop subset without mixing mirror and execution bytes."""

    source_roots = resolve_logical_source_roots(source, declarations, profile_cwd=profile_cwd)
    validate_requested_logical_source_roots(source, declarations, source_roots)
    rows = {row["alias"]: row for row in declarations}
    aliases = set(rows)
    effective = dict(source_roots)
    scan = scan_ocaml_loaders(source.read_bytes())
    if scan.status != "ok":
        return effective
    loaded_entries: list[dict[str, object]] = []
    if any(row["execution_role"] == "profile_cwd" for row in declarations):
        if profile_root is None or profile_cwd is None:
            raise LogicalSourceRootError(
                "refused_loop_logical_source_subset",
                "loop logical source execution requires the selected published profile inventory",
            )
        provenance_path = profile_root / "provenance" / "snapshot-provenance.json"
        try:
            provenance = json.loads(provenance_path.read_bytes())
            raw_entries = provenance["loaded_closure"]["entries"]
            if not isinstance(raw_entries, list):
                raise TypeError("entries")
            loaded_entries = [entry for entry in raw_entries if isinstance(entry, dict)]
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise LogicalSourceRootError(
                "refused_loop_logical_source_subset",
                f"selected profile has no usable loaded-file inventory: {exc}",
            ) from exc
    for item in scan.literal_occurrences:
        if item.family != "source" or item.path is None:
            continue
        kind, alias, suffix = classify_logical_literal(item.path, aliases)
        if kind != "logical" or alias is None or suffix is None:
            continue
        row = rows[alias]
        if row["execution_role"] != "profile_cwd":
            raise LogicalSourceRootError(
                "refused_loop_recursive_logical_source_dependency",
                f"hot loop Phase 1 supports only profile-inventoried direct logical needs; replay supports {item.path}",
            )
        if item.loader != "needs" or profile_cwd is None:
            raise LogicalSourceRootError(
                "refused_loop_recursive_logical_source_dependency",
                f"hot loop Phase 1 supports only profile-inventoried direct logical needs: {item.path}",
            )
        mirror_file = source_roots[alias] / suffix
        execution_file = profile_cwd / suffix
        mirror_bytes = read_regular_file_beneath(source_roots[alias], mirror_file).data
        execution_bytes = read_regular_file_beneath(profile_cwd, execution_file).data
        digest = hashlib.sha256(mirror_bytes).hexdigest()
        matches = []
        for entry in loaded_entries:
            roles = entry.get("roles")
            if (
                entry.get("path") == str(execution_file)
                and entry.get("resolved_path") == str(execution_file)
                and entry.get("sha256") == digest
                and entry.get("size_bytes") == len(mirror_bytes)
                and isinstance(roles, list)
                and "profile_cwd" in roles
            ):
                matches.append(entry)
        if mirror_bytes != execution_bytes or len(matches) != 1:
            raise LogicalSourceRootError(
                "refused_loop_recursive_logical_source_dependency",
                f"logical need is not an exact profile-inventoried mirror/execution match: {item.path}; use ordinary prove",
            )
        effective[alias] = profile_cwd
    return effective


def _source_runtime_cwd(source: Path, import_roots: dict[str, Path]) -> Path | None:
    """Find one exact directory for source-relative inputs, without building them."""

    relative_needs = [
        Path(str(item.get("path") or ""))
        for item in extract_hol_needs(source)
        if str(item.get("path") or "").startswith(("./", "../"))
    ]
    if relative_needs and all((source.parent / item).is_file() for item in relative_needs):
        return source.parent

    relative_inputs = []
    has_parent_traversal = False
    for item in extract_define_from_elf_paths(source):
        raw = Path(str(item.get("path") or ""))
        if raw.is_absolute() or any(part == "" for part in raw.parts):
            continue
        relative_inputs.append(raw)
        has_parent_traversal = has_parent_traversal or ".." in raw.parts
    if not relative_inputs:
        return None

    repository = nearest_repository_root(source)
    if has_parent_traversal:
        resolved_inputs = tuple((source.parent / item).resolve() for item in relative_inputs)
        if repository is not None:
            repository = repository.resolve()
            if any(not item.is_relative_to(repository) for item in resolved_inputs):
                return None
        return source.parent if all(item.is_file() for item in resolved_inputs) else None

    candidates: set[Path] = set(import_roots.values())
    for candidate in (source.parent, *source.parents):
        candidates.add(candidate.resolve())
        if repository is not None and candidate == repository:
            break
    exact = [root for root in candidates if all((root / item).is_file() for item in relative_inputs)]
    if len(exact) == 1:
        return exact[0]
    parent_only = [root for root in candidates if all((root / item).parent.is_dir() for item in relative_inputs)]
    return parent_only[0] if len(parent_only) == 1 else None


def _source_context_lines(import_dir: Path | None, runtime_cwd: Path | None) -> list[str]:
    literal = ocaml_string_literal
    lines: list[str] = []
    if import_dir is not None:
        lines.extend(
            (
                f"load_path := {literal(str(import_dir))} :: !load_path;;",
                "",
            )
        )
    if runtime_cwd is not None:
        lines.extend(
            (
                "let HOL_WORKBENCH_HOT_PROFILE_CWD = Sys.getcwd ();;",
                "load_path := HOL_WORKBENCH_HOT_PROFILE_CWD :: !load_path;;",
                f"Sys.chdir {literal(str(runtime_cwd))};;",
                "",
            )
        )
    return lines


def _materialize_import_dir(session_dir: Path, import_roots: dict[str, Path]) -> Path | None:
    if not import_roots:
        return None
    import_dir = session_dir / "imports"
    import_dir.mkdir(mode=0o700)
    for alias, root in sorted(import_roots.items()):
        (import_dir / alias).symlink_to(root, target_is_directory=True)
    return import_dir


def _bootstrap_source(
    *,
    cma_dir: Path,
    socket_path: Path,
    nonce: str,
    owner: ProcessIdentity,
    import_dir: Path | None = None,
    runtime_cwd: Path | None = None,
) -> str:
    from hol_workbench.native_signals import ocaml_signal_configuration

    try:
        native_build = json.loads((cma_dir / "build.json").read_text())
        signal_configuration = ocaml_signal_configuration(native_build["ocaml"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("live evaluator build metadata is missing; run hearth build-native") from exc
    literal = ocaml_string_literal
    return "\n".join(
        (
            f"Topdirs.dir_directory {literal(str(cma_dir))};;",
            f"Topdirs.dir_directory {literal(str(cma_dir / '.workbench_hot_session.objs' / 'byte'))};;",
            'Topdirs.dir_load Format.std_formatter "workbench_hot_session.cma";;',
            signal_configuration,
            "",
            *_source_context_lines(import_dir, runtime_cwd),
            "let HOT_LOOP_SERVER_PID =",
            "  Hot_session.start",
            f"    {literal(str(socket_path))}",
            f"    {literal(nonce)}",
            f"    {owner.pid}",
            f"    {owner.start_ticks}L;;",
            "",
            "let HOT_LOOP_BOOTSTRAP = prove (`T`, MESON_TAC[]);;",
            "",
        )
    )


def _parse(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="prove",
        description="Watch one source against one already-published warm profile.",
    )
    parser.add_argument("positional_source", nargs="?")
    parser.add_argument("--source")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--run-root")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    parsed = parser.parse_args(args)
    source = parsed.source or parsed.positional_source
    if not source:
        parser.error("--loop requires SOURCE.ml")
    if parsed.source and parsed.positional_source:
        parser.error("specify SOURCE.ml once")
    if parsed.timeout < 1:
        parser.error("--timeout must be positive")
    if parsed.poll < 0.01:
        parser.error("--poll must be at least 0.01 seconds")
    if parsed.run_root is not None:
        parser.error(
            "--loop cannot be combined with --run-root; "
            "use ordinary prove --run-root for a recorded check"
        )
    parsed.source = source
    return parsed


def loop_stop_banner(exit_status: int) -> str:
    """Describe a requested stop using only the signal/exit the process observed."""

    if exit_status >= 128:
        signum = exit_status - 128
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = f"signal {signum}"
        return f"HOT LOOP: stopped ({name}, exit {exit_status})"
    return f"HOT LOOP: stopped (exit {exit_status})"


def _profile(source: Path, explicit: str | None, script_dir: Path) -> str:
    public = public_authoring_profile_names(script_dir)
    if explicit:
        if explicit not in public:
            raise RuntimeError(f"profile {explicit!r} is not public; available: {', '.join(public)}")
        return explicit
    decision = infer_public_profile_for_source(source, public)
    if decision.profile is None:
        raise RuntimeError(decision.reason)
    return decision.profile


def _chmod_for_cleanup(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode, follow_symlinks=False)
        return
    except NotImplementedError:
        pass
    except OSError:
        return
    try:
        if stat.S_ISLNK(path.lstat().st_mode):
            return
        os.chmod(path, mode)
    except OSError:
        return


def _remove_tree(path: Path) -> None:
    if not path.exists():
        return
    for root, directories, files in os.walk(path):
        for name in directories:
            _chmod_for_cleanup(Path(root) / name, 0o700)
        for name in files:
            _chmod_for_cleanup(Path(root) / name, 0o600)
    _chmod_for_cleanup(path, 0o700)
    remove_tree(path)


def _prepare_hot_evaluation(
    *,
    source: Path,
    source_bytes: bytes,
    profile: PublishedWarmProfile,
    session_dir: Path,
    state: SessionDependencyState,
) -> bytes:
    """Capture one exact plan while keeping source bytes and stable packages session-local."""

    closure, holdir_root = capture_source_dependency_closure(
        source,
        profile_cwd=profile.cwd,
        legacy_holdir_roots=profile.legacy_holdir_roots,
        logical_source_root_declarations=profile.logical_source_roots,
    )
    entrypoint = closure.get("entrypoint") or {}
    if (
        entrypoint.get("sha256") != hashlib.sha256(source_bytes).hexdigest()
        or entrypoint.get("size_bytes") != len(source_bytes)
    ):
        raise BlockingIOError("source changed while its dependency plan was captured")
    profile_satisfaction, transport_status, transport_reason = decide_profile_satisfaction(
        closure,
        profile_root=profile.root,
        logical_profile=profile.name,
        profile_cwd=profile.cwd,
        holdir_root=holdir_root,
    )
    if not transport_status.startswith("packaged"):
        raise DependencyPackageError(transport_status, transport_reason)
    projection = session_dependency_projection(
        closure,
        profile_satisfaction=profile_satisfaction,
    )
    projection_sha256 = str(projection["strict_sha256"])
    if projection_sha256 != state.projection_sha256:
        next_root = session_dir / f"dependencies-{projection_sha256}"
        manifest = materialize_session_dependency_package(
            closure=closure,
            destination=next_root,
            projection=projection,
            profile_satisfaction=profile_satisfaction,
        )
        previous_root = state.package_root
        state.projection_sha256 = projection_sha256
        state.package_root = next_root
        state.virtual_entrypoint = Path(str(manifest["virtual_entrypoint"]))
        state.materialization_count += 1
        state.files = tuple(dict(row) for row in manifest["files"])
        if previous_root is not None and previous_root != next_root:
            _remove_tree(previous_root)
    if (
        state.package_root is None
        or state.virtual_entrypoint is None
        or not state.package_root.is_dir()
        or state.virtual_entrypoint.exists()
        or state.virtual_entrypoint.is_symlink()
    ):
        raise DependencyPackageError(
            "refused_session_dependency_package",
            "session dependency package is absent or its virtual entrypoint was materialized",
        )
    prelude = source_execution_prelude(
        package_root=state.package_root,
        virtual_entrypoint=state.virtual_entrypoint,
        closure=closure,
        profile_satisfaction=profile_satisfaction,
    )
    return prelude + source_bytes


def _compact_refusal(error: BaseException) -> str:
    message = " ".join(str(error).split())
    return message if len(message) <= 300 else message[:300] + " ..."


def _entrypoint_failure_card(card: str, *, generated_prefix: bytes) -> str:
    """Translate evaluator byte-stream lines back to the editable entrypoint."""

    marker = "FAIL line="
    if not card.startswith(marker):
        return card
    label, separator, detail = card[len(marker) :].partition(" ")
    try:
        generated_line = int(label)
    except ValueError:
        return card
    source_line = generated_line - generated_prefix.count(b"\n")
    if source_line < 1:
        return card
    translated = f"FAIL line={source_line}"
    return translated + separator + detail


def _watch(
    source: Path,
    socket_path: Path,
    nonce: str,
    *,
    profile: PublishedWarmProfile,
    session_dir: Path,
    timeout: int,
    poll: float,
) -> None:
    prior_digest = ""
    pending_key: tuple[int, int, int] | None = None
    dependency_state = SessionDependencyState()
    print(f"HOT LOOP: watching {source}", flush=True)
    while True:
        try:
            payload, source_key = stable_source(source)
        except (BlockingIOError, FileNotFoundError):
            time.sleep(poll)
            continue
        digest = hashlib.sha256(payload).hexdigest()
        if digest != prior_digest and source_key == pending_key:
            started = time.monotonic()
            try:
                evaluation_bytes = _prepare_hot_evaluation(
                    source=source,
                    source_bytes=payload,
                    profile=profile,
                    session_dir=session_dir,
                    state=dependency_state,
                )
                current_payload, current_key = stable_source(source)
                if current_key != source_key or hashlib.sha256(current_payload).hexdigest() != digest:
                    pending_key = current_key
                    continue
                prefix_size = len(evaluation_bytes) - len(payload)
                if prefix_size < 0 or evaluation_bytes[prefix_size:] != payload:
                    raise RuntimeError("hot evaluation did not preserve exact entrypoint source bytes")
                card, _elapsed = evaluate(socket_path, nonce, evaluation_bytes, timeout)
                card = _entrypoint_failure_card(
                    card,
                    generated_prefix=evaluation_bytes[:prefix_size],
                )
            except BlockingIOError:
                pending_key = None
                continue
            except (
                DependencyPackageError,
                LogicalSourceRootError,
                ProfileSatisfactionError,
                SourceDependencyInferenceError,
            ) as exc:
                status = str(getattr(exc, "status", "refused_source_execution_plan"))
                card = f"REFUSED status={status} {_compact_refusal(exc)}"
            print(f"{time.monotonic() - started:.3f}s {card}", flush=True)
            if remediation := loop_remediation(card):
                print(remediation, flush=True)
            prior_digest = digest
            pending_key = None
        elif digest != prior_digest:
            pending_key = source_key
        time.sleep(poll)


def main(
    args: list[str],
    *,
    script_dir: str | os.PathLike[str],
    cwd: str | os.PathLike[str],
) -> int:
    if sys.platform != "linux":
        print("prove --loop is Linux-only", file=sys.stderr)
        return 2
    options = _parse(args)
    legacy_cwd = Path(cwd).expanduser().resolve()
    try:
        resolution = resolve_authoring_source(options.source, legacy_cwd=legacy_cwd)
    except AuthoringSourcePathError as exc:
        print(f"prove --loop: {exc}", file=sys.stderr)
        return 2
    source = resolution.source
    if not source.is_file():
        print(f"prove --loop: source is not a file: {source}", file=sys.stderr)
        return 2
    scripts = Path(script_dir).expanduser().resolve()
    try:
        profile = _profile(source, options.profile, scripts)
        cma_dir = _native_eval_dir()
        published_profile = resolve_published_warm_profile(scripts, profile)
    except (LogicalSourceRootError, RuntimeError) as exc:
        print(f"prove --loop: {exc}", file=sys.stderr)
        return 2
    session_dir: Path | None = None
    socket_path: Path | None = None
    nonce = ""
    server: ProcessIdentity | None = None
    result = 1
    interrupted = False
    cleanup_error: str | None = None
    with warm_pool_eval_signal_guard() as signal_state:
        try:
            signal_state.defer()
            session_dir = Path(tempfile.mkdtemp(prefix=f"hol-workbench-loop-{os.getuid()}-"))
            socket_path = session_dir / "session.sock"
            bootstrap = session_dir / "bootstrap.ml"
            nonce = secrets.token_hex(24)
            signal_state.arm()
            owner = process_identity(os.getpid(), require_isolated=False)
            bootstrap.write_text(
                _bootstrap_source(
                    cma_dir=cma_dir,
                    socket_path=socket_path,
                    nonce=nonce,
                    owner=owner,
                    import_dir=None,
                    runtime_cwd=None,
                ),
                encoding="utf-8",
            )
            print(
                f"HOT LOOP: starting profile={profile} "
                "(profile startup is paid once; every save evaluates the complete source)",
                flush=True,
            )
            started = time.monotonic()
            bootstrap_status = run_published_hot_bootstrap(
                published_profile,
                bootstrap,
                timeout=float(options.timeout),
                transcript=session_dir / "bootstrap.raw",
            )
            result = bootstrap_status
            if bootstrap_status == 0:
                mode = socket_path.stat().st_mode
                if not stat.S_ISSOCK(mode):
                    raise RuntimeError("bootstrap completed without a session socket")
                server = _server_identity(socket_path, nonce)
                print(
                    f"HOT LOOP: ready in {time.monotonic() - started:.1f}s; "
                    "complete-source edit loop active; Ctrl-C stops",
                    flush=True,
                )
                bootstrap.unlink(missing_ok=True)
                _watch(
                    source,
                    socket_path,
                    nonce,
                    profile=published_profile,
                    session_dir=session_dir,
                    timeout=options.timeout,
                    poll=options.poll,
                )
        except HotBootstrapInterrupted as exc:
            interrupted = True
            result = 128 + exc.signum
        except WarmPoolEvalInterrupted as exc:
            interrupted = True
            result = 128 + exc.signum
        except KeyboardInterrupt:
            interrupted = True
            result = 128 + signal.SIGINT
        except (OSError, RuntimeError) as exc:
            print(f"prove --loop: {exc}", file=sys.stderr)
            result = 1
        finally:
            cleanup_signal = _defer_interrupts(signal_state)
            if cleanup_signal is not None:
                interrupted = True
                result = 128 + cleanup_signal
            if interrupted:
                print("HOT LOOP: stopping", flush=True)
            if session_dir is not None and socket_path is not None:
                try:
                    cleanup_error = _cleanup_session(session_dir, socket_path, nonce, server)
                except WarmPoolEvalInterrupted as exc:
                    interrupted = True
                    result = 128 + exc.signum
                    cleanup_error = "cleanup interrupted before completion"
                except KeyboardInterrupt:
                    interrupted = True
                    result = 128 + signal.SIGINT
                    cleanup_error = "cleanup interrupted before completion"
    if signal_state.interrupted_signal is not None:
        interrupted = True
        result = 128 + signal_state.interrupted_signal
    if cleanup_error is not None:
        assert session_dir is not None
        print(
            f"prove --loop: stop incomplete; preserved {session_dir}: {cleanup_error}",
            file=sys.stderr,
        )
        return 1
    if interrupted:
        print(loop_stop_banner(result), flush=True)
    return result
