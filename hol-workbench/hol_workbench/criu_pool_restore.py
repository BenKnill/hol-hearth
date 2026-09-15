"""Locked metadata refresh shared by CRIU restore entry points."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hol_workbench.criu_restore_residue import prune_successful_restore_residue
from hol_workbench.jsonio import atomic_write_json
from hol_workbench.pools import warm_pool_lock
from hol_workbench.pools.lifecycle import warm_pool_lifecycle_lock
from hol_workbench.process_groups import process_birth_identity, process_group_is_alive
from hol_workbench.proof_run_fork_children import clear_active_children, read_active_children
from hol_workbench.proof_run_runtime import utc_now

# Linux renders /proc start ticks in the reader's time namespace. Keep restored
# workers with the guest control plane so durable PID-reuse checks stay exact.
CRIU_GUEST_CONTROL_TIME_NAMESPACE_OPTIONS = ("--join-ns", "time:1")
STALE_READY_DEMOTION_SCHEMA = "hol-workbench.criu-stale-ready-demotion.v1"
DirectoryIdentity = tuple[int, int]


class PoolAuthorityError(RuntimeError):
    """A selected restored-pool path no longer names its admitted directory."""


def _directory_identity(value: os.stat_result) -> DirectoryIdentity:
    return value.st_dev, value.st_ino


def _real_directory_identity(
    path: Path,
    *,
    label: str,
    expected: DirectoryIdentity | None = None,
    changed: bool = False,
) -> DirectoryIdentity:
    reason = "changed before lock acquisition" if changed else "must be a real non-symlink directory"
    try:
        first = path.lstat()
    except OSError as exc:
        raise PoolAuthorityError(f"{label} {reason}: {path}: {exc}") from exc
    identity = _directory_identity(first)
    if not stat.S_ISDIR(first.st_mode):
        kind = "symlink" if stat.S_ISLNK(first.st_mode) else "non-directory"
        raise PoolAuthorityError(f"{label} {reason}: {path} is a {kind}")
    if expected is not None and identity != expected:
        raise PoolAuthorityError(f"{label} changed before lock acquisition: {path}")
    try:
        resolved = path.resolve(strict=True)
        second = path.lstat()
    except OSError as exc:
        raise PoolAuthorityError(f"{label} {reason}: {path}: {exc}") from exc
    if resolved != path or _directory_identity(second) != identity:
        raise PoolAuthorityError(f"{label} {reason}: {path}")
    return identity


def _open_directory(
    path: str | Path,
    *,
    label: str,
    expected: DirectoryIdentity,
    dir_fd: int | None = None,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        raise PoolAuthorityError(f"{label} changed before lock acquisition: {path}: {exc}") from exc
    observed = os.fstat(fd)
    if not stat.S_ISDIR(observed.st_mode) or _directory_identity(observed) != expected:
        os.close(fd)
        raise PoolAuthorityError(f"{label} changed before lock acquisition: {path}")
    return fd


@dataclass(frozen=True)
class RestoredPoolAuthority:
    profile_root: Path
    profile_identity: DirectoryIdentity
    pool_root_identity: DirectoryIdentity
    pool_name: str
    pool_identity: DirectoryIdentity

    @property
    def pool_root(self) -> Path:
        return self.profile_root / "pool"

    @property
    def pool(self) -> Path:
        return self.pool_root / self.pool_name

    def assert_current(self, *, changed: bool = False) -> None:
        _real_directory_identity(
            self.profile_root,
            label="admitted profile root",
            expected=self.profile_identity,
            changed=changed,
        )
        _real_directory_identity(
            self.pool_root,
            label="pool root",
            expected=self.pool_root_identity,
            changed=changed,
        )
        _real_directory_identity(
            self.pool,
            label="selected pool",
            expected=self.pool_identity,
            changed=changed,
        )


@dataclass(frozen=True)
class RestoredProfileAuthority:
    """Open descriptor authority for one admitted profile and CRIU image."""

    profile_root: Path
    profile_identity: DirectoryIdentity
    image_identity: DirectoryIdentity
    profile_fd: int
    image_fd: int

    @property
    def image_dir(self) -> Path:
        return self.profile_root / "criu-image"

    @property
    def bound_profile_root(self) -> Path:
        return Path(f"/proc/{os.getpid()}/fd/{self.profile_fd}")

    @property
    def bound_image_dir(self) -> Path:
        return Path(f"/proc/{os.getpid()}/fd/{self.image_fd}")

    def assert_current(self, *, changed: bool = False) -> None:
        _real_directory_identity(
            self.profile_root,
            label="admitted profile root",
            expected=self.profile_identity,
            changed=changed,
        )
        _real_directory_identity(
            self.image_dir,
            label="CRIU image directory",
            expected=self.image_identity,
            changed=changed,
        )


def _open_child_directory(parent_fd: int, name: str, *, label: str) -> int:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or Path(name).name != name:
        raise PoolAuthorityError(f"{label} name is not one safe path leaf: {name!r}")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise PoolAuthorityError(f"{label} must be a real non-symlink directory: {name}: {exc}") from exc
    observed = os.fstat(fd)
    if not stat.S_ISDIR(observed.st_mode):
        os.close(fd)
        raise PoolAuthorityError(f"{label} must be a real non-symlink directory: {name}")
    return fd


@contextmanager
def restored_profile_authority(profile_root: Path) -> Iterator[RestoredProfileAuthority]:
    """Keep the admitted profile and image directory descriptor-bound."""

    profile_identity = _real_directory_identity(profile_root, label="admitted profile root")
    profile_fd = _open_directory(
        profile_root,
        label="admitted profile root",
        expected=profile_identity,
    )
    try:
        image_fd = _open_child_directory(profile_fd, "criu-image", label="CRIU image directory")
        try:
            authority = RestoredProfileAuthority(
                profile_root=profile_root,
                profile_identity=profile_identity,
                image_identity=_directory_identity(os.fstat(image_fd)),
                profile_fd=profile_fd,
                image_fd=image_fd,
            )
            authority.assert_current()
            yield authority
        finally:
            os.close(image_fd)
    finally:
        os.close(profile_fd)


def restored_pool_session_json(pool: Path, session: Mapping[str, object]) -> Path | None:
    """Map one recorded session back beneath the bound pool capability."""

    recorded_session_dir = session.get("session_dir")
    session_id = session.get("session_id")
    if session_id is None:
        if not isinstance(recorded_session_dir, str) or not recorded_session_dir:
            return None
        session_id = Path(recorded_session_dir).name
    if (
        not isinstance(session_id, str)
        or not session_id
        or session_id in {".", ".."}
        or "/" in session_id
        or "\\" in session_id
        or Path(session_id).name != session_id
    ):
        raise PoolAuthorityError(f"restored pool session id is not one safe path leaf: {session_id!r}")
    if isinstance(recorded_session_dir, str) and recorded_session_dir and Path(recorded_session_dir).name != session_id:
        raise PoolAuthorityError("restored pool session id disagrees with its recorded session directory")
    return pool / "sessions" / session_id / "session.json"


def select_restored_pool_authority(
    profile_root: Path,
    *,
    profile_fd: int | None = None,
    expected_profile_identity: DirectoryIdentity | None = None,
) -> RestoredPoolAuthority:
    """Select exactly one real pool child beneath one resolved admitted profile."""

    if profile_fd is None:
        profile_identity = _real_directory_identity(profile_root, label="admitted profile root")
        owned_profile_fd = _open_directory(
            profile_root,
            label="admitted profile root",
            expected=profile_identity,
        )
    else:
        observed_profile = _directory_identity(os.fstat(profile_fd))
        if expected_profile_identity is not None and observed_profile != expected_profile_identity:
            raise PoolAuthorityError("admitted profile descriptor identity changed before pool selection")
        profile_identity = expected_profile_identity or observed_profile
        owned_profile_fd = os.dup(profile_fd)
    try:
        pool_root_fd = _open_child_directory(owned_profile_fd, "pool", label="pool root")
        try:
            pool_root_identity = _directory_identity(os.fstat(pool_root_fd))
            pool_names = sorted(os.listdir(pool_root_fd))
            if len(pool_names) != 1:
                raise PoolAuthorityError(
                    f"expected exactly one real pool child under {profile_root / 'pool'}, found {len(pool_names)}"
                )
            pool_name = pool_names[0]
            pool_fd = _open_child_directory(pool_root_fd, pool_name, label="selected pool")
            try:
                pool_identity = _directory_identity(os.fstat(pool_fd))
            finally:
                os.close(pool_fd)
        finally:
            os.close(pool_root_fd)
    finally:
        os.close(owned_profile_fd)
    authority = RestoredPoolAuthority(
        profile_root=profile_root,
        profile_identity=profile_identity,
        pool_root_identity=pool_root_identity,
        pool_name=pool_name,
        pool_identity=pool_identity,
    )
    authority.assert_current()
    return authority


def _backup_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


@contextmanager
def restored_pool_lock(
    pool: Path,
    *,
    authority: RestoredPoolAuthority | None = None,
    profile_fd: int | None = None,
) -> Iterator[Path]:
    """Acquire CRIU lifecycle locks in the single supported order."""
    if authority is None:
        with warm_pool_lifecycle_lock(pool), warm_pool_lock(pool):
            yield pool
        return
    if pool != authority.pool:
        raise PoolAuthorityError(f"selected pool disagrees with admitted authority: {pool} != {authority.pool}")

    if profile_fd is None:
        owned_profile_fd = _open_directory(
            authority.profile_root,
            label="admitted profile root",
            expected=authority.profile_identity,
        )
    else:
        if _directory_identity(os.fstat(profile_fd)) != authority.profile_identity:
            raise PoolAuthorityError("admitted profile descriptor disagrees with selected pool authority")
        owned_profile_fd = os.dup(profile_fd)
    lock_fds: list[int] = []
    try:
        pool_root_fd = _open_directory(
            "pool",
            label="pool root",
            expected=authority.pool_root_identity,
            dir_fd=owned_profile_fd,
        )
        try:
            pool_fd = _open_directory(
                authority.pool_name,
                label="selected pool",
                expected=authority.pool_identity,
                dir_fd=pool_root_fd,
            )
            try:
                authority.assert_current(changed=True)
                for name in ("lifecycle.lock", "pool.lock"):
                    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW
                    try:
                        lock_fd = os.open(name, flags, 0o600, dir_fd=pool_fd)
                    except OSError as exc:
                        raise PoolAuthorityError(
                            f"selected pool lock is not authority-confined: {name}: {exc}"
                        ) from exc
                    if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                        os.close(lock_fd)
                        raise PoolAuthorityError(f"selected pool lock is not a regular file: {name}")
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    lock_fds.append(lock_fd)
                authority.assert_current(changed=True)
                bound_pool = Path(f"/proc/self/fd/{pool_fd}")
                if _directory_identity(bound_pool.stat()) != authority.pool_identity:
                    raise PoolAuthorityError("bound pool descriptor identity changed before restore")
                yield bound_pool
            finally:
                for lock_fd in reversed(lock_fds):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
                os.close(pool_fd)
        finally:
            os.close(pool_root_fd)
    finally:
        os.close(owned_profile_fd)


def _positive_metadata_int(value: object, *, field: str) -> int:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"stale ready pool {field} is not a positive integer") from exc
    if number <= 0:
        raise RuntimeError(f"stale ready pool {field} is not a positive integer")
    return number


def pid_is_conclusively_dead(
    pid: object,
    *,
    kill_probe: Callable[[int, int], None] | None = None,
) -> bool:
    """Return true only when the kernel conclusively reports that a PID is absent."""

    number = _positive_metadata_int(pid, field="pid")
    kill_probe = kill_probe or os.kill
    try:
        kill_probe(number, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def demote_stale_ready_pool_for_restore(
    pool: Path,
    *,
    identity_reader: Callable[[object], str | None] | None = None,
    pid_dead: Callable[[object], bool] | None = None,
    group_live: Callable[[object], bool] | None = None,
    active_children_reader: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Atomically reopen one quiescent dead-ready generation for shelf restore.

    The pool-level record is the authoritative checkout gate.  This transition
    is deliberately narrower than repair: it accepts only retained verified
    stop receipts and proves that no published process, process group, lease,
    or registered fork child remains before changing ``ready`` to ``stopped``.
    The existing restore transaction subsequently refreshes session files.
    """

    identity_reader = identity_reader or process_birth_identity
    pid_dead = pid_dead or pid_is_conclusively_dead
    group_live = group_live or process_group_is_alive
    active_children_reader = active_children_reader or read_active_children
    pool_json = pool / "pool.json"
    data = json.loads(pool_json.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("pool metadata is not a JSON object")
    if data.get("status") != "ready":
        return None

    transaction = data.get("restore_transaction")
    if transaction is not None and (not isinstance(transaction, dict) or transaction.get("state") != "ready"):
        raise RuntimeError("stale ready pool retains a non-ready restore transaction")

    owned_processes: list[tuple[str, int, int, str]] = []
    for role in ("worker", "basis"):
        pid = _positive_metadata_int(data.get(f"{role}_pid"), field=f"{role}_pid")
        pgid = _positive_metadata_int(data.get(f"{role}_pgid") or pid, field=f"{role}_pgid")
        expected_identity = data.get(f"{role}_identity")
        if not isinstance(expected_identity, str) or not expected_identity:
            raise RuntimeError(f"stale ready pool {role}_identity is unavailable")
        owned_processes.append((role, pid, pgid, expected_identity))

    sessions = data.get("sessions")
    if not isinstance(sessions, list) or not sessions or any(not isinstance(item, dict) for item in sessions):
        raise RuntimeError("stale ready pool has no complete session records")
    for session in sessions:
        status = str(session.get("status") or "unknown")
        if status not in {"idle", "ready", "dead"}:
            raise RuntimeError(f"stale ready pool session status {status} is not quiescent")
        if session.get("lease") is not None:
            raise RuntimeError("stale ready pool retains a session lease")
        stop_result = session.get("stop_result")
        if not isinstance(stop_result, dict) or stop_result.get("verified_quiescent") is not True:
            raise RuntimeError("stale ready pool has no retained verified-quiescent stop receipt")
        for role, pid, pgid, expected_identity in owned_processes:
            session_pid = _positive_metadata_int(session.get(f"{role}_pid"), field=f"session {role}_pid")
            session_pgid = _positive_metadata_int(
                session.get(f"{role}_pgid") or session_pid,
                field=f"session {role}_pgid",
            )
            if session_pid != pid or session_pgid != pgid or session.get(f"{role}_identity") != expected_identity:
                raise RuntimeError(f"stale ready pool session {role} process record disagrees with the pool")

    require_registry = data.get("engine") == "fork_basis"
    active_children = active_children_reader(pool, require_registry=require_registry)
    if active_children:
        raise RuntimeError("stale ready pool retains registered fork children")

    process_records: list[dict[str, Any]] = []
    for role, pid, pgid, expected_identity in owned_processes:
        observed_identity = identity_reader(pid)
        if observed_identity == expected_identity:
            raise RuntimeError(f"stale ready pool recorded {role} identity remains live")
        if not pid_dead(pid):
            if observed_identity is None:
                raise RuntimeError(
                    f"stale ready pool recorded {role} birth identity is unavailable and PID death is not conclusive"
                )
            raise RuntimeError(f"stale ready pool recorded {role} PID is live with a different birth identity")
        if group_live(pgid):
            raise RuntimeError(f"stale ready pool recorded {role} process group remains live")
        process_records.append(
            {
                "role": role,
                "pid": pid,
                "pgid": pgid,
                "expected_identity": expected_identity,
                "observed_identity": observed_identity,
                "process_group_live": False,
            }
        )

    updated_utc = utc_now()
    demotion = {
        "schema": STALE_READY_DEMOTION_SCHEMA,
        "status": "demoted",
        "reason": "recorded ready generation is identity- and process-group-quiescent",
        "previous_updated_utc": data.get("updated_utc"),
        "processes": process_records,
        "active_child_count": 0,
        "updated_utc": updated_utc,
    }
    for session in sessions:
        session["status"] = "stopped"
        session["lease"] = None
    data["status"] = "stopped"
    data["updated_utc"] = updated_utc
    data["last_stale_ready_demotion"] = demotion
    data.pop("restore_transaction", None)
    atomic_write_json(pool_json, data)
    return demotion


def begin_restored_pool_transaction(
    pool: Path,
    *,
    image_dir: Path,
    pidfile: Path,
    log: Path,
    durable_image_dir: Path | None = None,
    durable_pidfile: Path | None = None,
    durable_log: Path | None = None,
    output_authority: Mapping[str, object] | None = None,
    operation_stamp: str | None = None,
) -> None:
    """Close admission before launching CRIU and publish its recovery anchors."""
    pool_json = pool / "pool.json"
    original = pool_json.read_text(encoding="utf-8")
    data = json.loads(original)
    if not isinstance(data, dict):
        raise TypeError("pool metadata is not a JSON object")
    if data.get("status") != "stopped":
        raise RuntimeError(f"restore transaction requires status stopped, found {data.get('status')!r}")
    updated_utc = utc_now()
    exact_stamp = operation_stamp or _backup_stamp()
    image_identity = _directory_identity(image_dir.stat())
    transaction = {
        "state": "prelaunch",
        "operation_stamp": exact_stamp,
        "image_dir": str(durable_image_dir or image_dir),
        "image_directory_identity": {
            "device": image_identity[0],
            "inode": image_identity[1],
        },
        "pidfile": str(durable_pidfile or pidfile),
        "restore_log": str(durable_log or log),
        "started_utc": updated_utc,
    }
    if output_authority is not None:
        if output_authority.get("schema") != "hol-workbench.criu-restore-output-authority.v1":
            raise RuntimeError("CRIU restore output authority record has an unsupported schema")
        transaction["output_authority"] = dict(output_authority)
    session_updates: list[tuple[Path, dict]] = []
    sessions = data.get("sessions") or []
    if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
        raise TypeError("pool sessions metadata is not an array of JSON objects")
    for session in sessions:
        session["status"] = "restoring"
        session["lease"] = None
        session_json = restored_pool_session_json(pool, session)
        if session_json is not None and session_json.is_file():
            session_data = json.loads(session_json.read_text(encoding="utf-8"))
            if not isinstance(session_data, dict):
                raise TypeError(f"session metadata is not a JSON object: {session_json}")
            session_data["status"] = "restoring"
            session_data["lease"] = None
            session_data["updated_utc"] = updated_utc
            session_updates.append((session_json, session_data))
    data["status"] = "restoring"
    data["restore_transaction"] = transaction
    data["updated_utc"] = updated_utc
    backup = pool_json.with_suffix(f".json.before-criu-restore-{exact_stamp}")
    backup.write_text(original, encoding="utf-8")
    for session_json, session_data in session_updates:
        atomic_write_json(session_json, session_data)
    atomic_write_json(pool_json, data)
    prune_successful_restore_residue(image_dir=image_dir, pool=pool)


def reset_root_collision_restore(
    pool: Path,
    *,
    pidfile: Path,
    log: Path,
    durable_pidfile: Path | None = None,
    durable_log: Path | None = None,
    operation_stamp: str,
    retry_receipt: Path,
) -> None:
    """Roll one proven pre-root PID collision back to the verified stopped state."""

    pool_json = pool / "pool.json"
    data = json.loads(pool_json.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("status") != "restoring":
        raise RuntimeError("root-collision retry requires pool status restoring")
    transaction = data.get("restore_transaction")
    recorded_pidfile = durable_pidfile or pidfile
    recorded_log = durable_log or log
    if (
        not isinstance(transaction, dict)
        or transaction.get("operation_stamp") != operation_stamp
        or transaction.get("pidfile") != str(recorded_pidfile)
        or transaction.get("restore_log") != str(recorded_log)
        or transaction.get("state") != "launcher_owned"
    ):
        raise RuntimeError("root-collision retry does not match the exact active restore transaction")
    sessions = data.get("sessions") or []
    if not isinstance(sessions, list) or not sessions or any(not isinstance(item, dict) for item in sessions):
        raise TypeError("pool sessions metadata is not an array of JSON objects")
    updated_utc = utc_now()
    session_updates: list[tuple[Path, dict]] = []
    for session in sessions:
        stop_result = session.get("stop_result") or {}
        if (
            session.get("status") != "restoring"
            or not isinstance(stop_result, dict)
            or stop_result.get("verified_quiescent") is not True
        ):
            raise RuntimeError("root-collision retry requires retained verified-quiescent session receipts")
        session["status"] = "stopped"
        session["lease"] = None
        session_json = restored_pool_session_json(pool, session)
        if session_json is not None and session_json.is_file():
            session_data = json.loads(session_json.read_text(encoding="utf-8"))
            if not isinstance(session_data, dict) or session_data.get("status") != "restoring":
                raise RuntimeError(f"root-collision retry found non-restoring session metadata: {session_json}")
            session_stop = session_data.get("stop_result") or {}
            if not isinstance(session_stop, dict) or session_stop.get("verified_quiescent") is not True:
                raise RuntimeError(f"root-collision retry found no verified stop receipt: {session_json}")
            session_data["status"] = "stopped"
            session_data["lease"] = None
            session_data["updated_utc"] = updated_utc
            session_updates.append((session_json, session_data))
    data["status"] = "stopped"
    data["updated_utc"] = updated_utc
    data["last_restore_retry"] = {
        "status": "authorized",
        "reason": "exact_root_pid_collision_became_vacant",
        "operation_stamp": operation_stamp,
        "receipt": str(retry_receipt),
        "updated_utc": updated_utc,
    }
    data.pop("restore_transaction", None)
    for session_json, session_data in session_updates:
        atomic_write_json(session_json, session_data)
    atomic_write_json(pool_json, data)


def record_restored_pool_launcher(
    pool: Path,
    *,
    pid: int,
    start_ticks: int,
    identity: str,
) -> None:
    """Own the gated launcher durably before allowing it to exec CRIU."""

    pool_json = pool / "pool.json"
    data = json.loads(pool_json.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("status") != "restoring":
        raise RuntimeError("restore launcher publication requires pool status restoring")
    transaction = data.get("restore_transaction")
    if not isinstance(transaction, dict) or transaction.get("state") != "prelaunch":
        raise RuntimeError("restore launcher publication requires a prelaunch transaction")
    transaction["state"] = "launcher_owned"
    transaction["launcher"] = {
        "pid": pid,
        "start_ticks": start_ticks,
        "identity": identity,
    }
    transaction["launcher_owned_utc"] = utc_now()
    data["updated_utc"] = transaction["launcher_owned_utc"]
    atomic_write_json(pool_json, data)


def stage_restored_pool_metadata(pool: Path, roles: dict[str, dict]) -> None:
    """Publish restored identities in a non-checkout-ready lifecycle state."""
    pool_json = pool / "pool.json"
    original = pool_json.read_text(encoding="utf-8")
    data = json.loads(original)
    if not isinstance(data, dict):
        raise TypeError("pool metadata is not a JSON object")
    if data.get("status") != "restoring":
        raise RuntimeError(f"restored identity staging requires status restoring, found {data.get('status')!r}")
    identity_ticks: dict[str, int] = {}
    identities: dict[str, str] = {}
    for role in ("worker", "basis"):
        process = roles[role]
        ticks = process.get("start_ticks")
        identity = process.get("identity")
        if ticks is None or identity is None:
            raise RuntimeError(f"restored {role} process identity is unavailable")
        identity_ticks[f"{role}_start_ticks"] = ticks
        identities[f"{role}_identity"] = identity
    data.update(identity_ticks)
    data.update(identities)
    data.pop("restore_failure_cleanup", None)
    data["status"] = "restoring"
    data["updated_utc"] = utc_now()
    transaction = data.get("restore_transaction")
    if isinstance(transaction, dict):
        transaction["state"] = "validated"
        transaction["validated_utc"] = data["updated_utc"]
    session_updates: list[tuple[Path, dict]] = []
    sessions = data.get("sessions") or []
    if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
        raise TypeError("pool sessions metadata is not an array of JSON objects")
    for session in sessions:
        session["status"] = "restoring"
        session["lease"] = None
        session.pop("restore_failure_cleanup", None)
        session.update(identity_ticks)
        session.update(identities)
        session_json = restored_pool_session_json(pool, session)
        if session_json is not None and session_json.is_file():
            session_data = json.loads(session_json.read_text(encoding="utf-8"))
            if not isinstance(session_data, dict):
                raise TypeError(f"session metadata is not a JSON object: {session_json}")
            session_data.update(identity_ticks)
            session_data.update(identities)
            session_data.pop("restore_failure_cleanup", None)
            session_data["status"] = "restoring"
            session_data["updated_utc"] = data["updated_utc"]
            session_updates.append((session_json, session_data))
    clear_active_children(pool)
    for session_json, session_data in session_updates:
        atomic_write_json(session_json, session_data)
    atomic_write_json(pool_json, data)


def publish_restored_pool_ready(pool: Path) -> None:
    """Commit checkout readiness after every restored process resumed cleanly."""
    pool_json = pool / "pool.json"
    data = json.loads(pool_json.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError("pool metadata is not a JSON object")
    if data.get("status") != "restoring":
        raise RuntimeError(f"restored pool readiness requires status restoring, found {data.get('status')!r}")
    updated_utc = utc_now()
    session_updates: list[tuple[Path, dict]] = []
    sessions = data.get("sessions") or []
    if not isinstance(sessions, list) or any(not isinstance(item, dict) for item in sessions):
        raise TypeError("pool sessions metadata is not an array of JSON objects")
    for session in sessions:
        session["status"] = "idle"
        session["lease"] = None
        session_json = restored_pool_session_json(pool, session)
        if session_json is not None and session_json.is_file():
            session_data = json.loads(session_json.read_text(encoding="utf-8"))
            if not isinstance(session_data, dict):
                raise TypeError(f"session metadata is not a JSON object: {session_json}")
            if session_data.get("status") != "restoring":
                raise RuntimeError(
                    f"restored session readiness requires status restoring, found {session_data.get('status')!r}: "
                    f"{session_json}"
                )
            session_data["status"] = "ready"
            session_data["lease"] = None
            session_data["updated_utc"] = updated_utc
            session_updates.append((session_json, session_data))
    data["status"] = "ready"
    data["updated_utc"] = updated_utc
    transaction = data.get("restore_transaction")
    if isinstance(transaction, dict):
        transaction["state"] = "ready"
        transaction["ready_utc"] = updated_utc
    # Session metadata is committed first. The pool-level ready state is the
    # authoritative admission gate, so any partial write remains fail closed.
    for session_json, session_data in session_updates:
        atomic_write_json(session_json, session_data)
    atomic_write_json(pool_json, data)
