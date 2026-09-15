"""One safety-bounded profile child used only to start a memory-first session."""

from __future__ import annotations

import os
import secrets
import signal
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

from hol_workbench.cli import orbstack_criu_restore
from hol_workbench.criu_shelf_admission import ShelfAdmissionInterrupted, criu_shelf_admission
from hol_workbench.criu_shelf_capacity import restored_shelf_capacity
from hol_workbench.criu_shelf_owner import new_shelf_owner, update_shelf_owner
from hol_workbench.jsonio import read_json
from hol_workbench.pools.fork_attempt_lifecycle import wait_for_fork_attempt_cleanup
from hol_workbench.pools.seat_lifecycle import (
    WarmPoolLeaseIdCollision,
    checkout_pool_seat_locked,
    finalize_fork_pool_seat_locked,
    quarantine_fork_basis_lease_locked,
)
from hol_workbench.pools.store import read_warm_pool, warm_pool_lock
from hol_workbench.proof_run_fork_broker_probe import run_broker_capture_request
from hol_workbench.proof_run_warm_pool_interrupt import (
    WarmPoolEvalInterrupted,
    WarmPoolEvalSignalState,
    warm_pool_eval_signal_guard,
)
from hol_workbench.restored_execution_topology import RestoredExecutionTopology

if TYPE_CHECKING:
    from hol_workbench.cli.published_profile import PublishedWarmProfile


class HotBootstrapInterrupted(Exception):
    """A terminal signal observed before the hot loop became ready."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"signal {signum}")


def _one_pool(profile_root: Path) -> Path:
    pools = sorted((profile_root / "pool").glob("*"))
    if len(pools) != 1:
        raise RuntimeError(f"expected one pool under {profile_root / 'pool'}, found {len(pools)}")
    return pools[0]


def _checkout(pool: Path, *, lease_id: str) -> Path:
    with warm_pool_lock(pool):
        data = read_warm_pool(pool)
        item = checkout_pool_seat_locked(
            pool,
            data,
            agent=os.environ.get("HOL_WORKBENCH_AGENT"),
            lease_id=lease_id,
            owner_kind="memory_first_bootstrap",
            owner_command="prove --loop",
        )
    return Path(str(item["session_dir"]))


def _lease_matches(data: dict, lease_id: str) -> list[dict]:
    return [
        item
        for item in data.get("sessions") or []
        if isinstance(item, dict)
        and isinstance(item.get("lease"), dict)
        and item["lease"].get("lease_id") == lease_id
    ]


def _quarantine_ambiguous_lease(pool: Path, data: dict, lease_id: str, matches: list[dict]) -> None:
    reason = f"warm pool lease ownership is ambiguous or malformed: {lease_id}"
    if not matches:
        raise RuntimeError(reason)
    quarantine_fork_basis_lease_locked(
        pool,
        data,
        lease_id=lease_id,
        reason=reason,
    )


def _session_for_lease(pool: Path, lease_id: str) -> Path | None:
    """Recover the exact seat if checkout persisted before ownership returned."""

    with warm_pool_lock(pool):
        data = read_warm_pool(pool)
        matches = _lease_matches(data, lease_id)
        if len(matches) == 1 and isinstance(matches[0].get("session_dir"), str) and matches[0]["session_dir"]:
            return Path(matches[0]["session_dir"])
        if not matches:
            return None
        reason = f"warm pool lease ownership is ambiguous or malformed: {lease_id}"
        _quarantine_ambiguous_lease(pool, data, lease_id, matches)
        raise RuntimeError(reason)


def _finalize_lease(pool: Path, lease_id: str, *, reusable: bool, reason: str) -> dict:
    """Finalize only while the exact persisted lease still owns its seat."""

    with warm_pool_lock(pool):
        data = read_warm_pool(pool)
        matches = _lease_matches(data, lease_id)
        if not matches:
            raise RuntimeError(f"warm pool lease no longer owns a session: {lease_id}")
        if len(matches) != 1 or not isinstance(matches[0].get("session_dir"), str) or not matches[0]["session_dir"]:
            _quarantine_ambiguous_lease(pool, data, lease_id, matches)
            raise RuntimeError(f"warm pool lease ownership is ambiguous or malformed: {lease_id}")
        session = Path(matches[0]["session_dir"])
        same_session = [
            item
            for item in data.get("sessions") or []
            if isinstance(item, dict)
            and isinstance(item.get("session_dir"), str)
            and item["session_dir"]
            and Path(item["session_dir"]).resolve() == session.resolve()
        ]
        if len(same_session) != 1 or same_session[0] is not matches[0]:
            _quarantine_ambiguous_lease(pool, data, lease_id, matches)
            raise RuntimeError(f"warm pool session ownership is ambiguous for lease: {lease_id}")
        return finalize_fork_pool_seat_locked(
            pool,
            data,
            session=session,
            reusable=reusable,
            reason=reason,
        )


def _defer_interrupts(signal_state: WarmPoolEvalSignalState) -> int | None:
    """Close the armed-to-deferred race before lifecycle finalization."""

    try:
        signal_state.defer()
    except WarmPoolEvalInterrupted as exc:
        signal_state.defer()
        return exc.signum
    except KeyboardInterrupt:
        signal_state.defer()
        return signal.SIGINT
    return None


def broker_response_proves_reusable(response: object) -> bool:
    """Accept only the controller's explicit three-part quiescence proof."""

    return (
        isinstance(response, dict)
        and response.get("seat_reusable") is True
        and response.get("child_quiescent") is True
        and response.get("cleanup_verified") is True
    )


def run_hot_profile_source(
    *,
    profile_root: Path,
    logical_profile: str,
    source: Path,
    transcript: Path,
    timeout: float,
) -> int:
    """Evaluate the trusted generated bootstrap, without proof receipts or packages."""

    source = source.expanduser().resolve()
    transcript = transcript.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"hot bootstrap source not found: {source}")
    capacity = restored_shelf_capacity(profile_root)
    owner = new_shelf_owner(
        profile_root,
        logical_profile=logical_profile,
        owner_kind="memory_first_bootstrap",
        source=source,
    )
    pool: Path | None = None
    session: Path | None = None
    response: dict | None = None
    attempt_id = f"hot-bootstrap-{secrets.token_hex(6)}"
    interrupted_signal: int | None = None
    result = 1
    with warm_pool_eval_signal_guard() as signal_state:
        try:
            with criu_shelf_admission(profile_root, capacity=capacity, owner=owner):
                update_shelf_owner(profile_root, owner["attempt_id"], progress="restoring")
                restore_output = StringIO()
                with redirect_stdout(restore_output):
                    restored = orbstack_criu_restore.main(
                        [str(profile_root), "--published-shelf-only"],
                        include_controller_attempt=False,
                    )
                if restored != 0:
                    sys.stderr.write(restore_output.getvalue())
                    return 1
                pool = _one_pool(profile_root)
                lease_id = secrets.token_hex(8)
                request_started = False
                checkout_may_have_persisted = True
                try:
                    signal_state.defer()
                    session = _checkout(pool, lease_id=lease_id)
                    signal_state.arm()
                    metadata = read_json(session / "session.json")
                    if metadata.get("execution_topology") != RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3:
                        raise RuntimeError("memory-first bootstrap requires the current mechanical fork broker")
                    update_shelf_owner(
                        profile_root,
                        owner["attempt_id"],
                        progress="bootstrapping_memory_session",
                        pool=str(pool),
                        session=str(session),
                    )
                    request_started = True
                    response = run_broker_capture_request(
                        session,
                        {
                            "action": "eval",
                            "attempt_id": attempt_id,
                            "source": str(source),
                            "transcript_path": str(transcript),
                            "raw_source": True,
                        },
                        timeout_seconds=timeout + 5.0,
                    )
                except WarmPoolEvalInterrupted as exc:
                    interrupted_signal = exc.signum
                except KeyboardInterrupt:
                    interrupted_signal = signal.SIGINT
                except WarmPoolLeaseIdCollision:
                    checkout_may_have_persisted = False
                    raise
                finally:
                    cleanup_signal = _defer_interrupts(signal_state)
                    interrupted_signal = interrupted_signal or cleanup_signal
                    if session is None and checkout_may_have_persisted:
                        session = _session_for_lease(pool, lease_id)
                    if session is not None and checkout_may_have_persisted:
                        reusable = broker_response_proves_reusable(response)
                        reason = (
                            f"hot bootstrap {response.get('status')} with verified child quiescence"
                            if reusable and response is not None
                            else "hot bootstrap ended without verified child quiescence"
                        )
                        if (
                            interrupted_signal is not None or signal_state.interrupted_signal is not None
                        ) and not reusable:
                            if request_started:
                                try:
                                    cleanup = wait_for_fork_attempt_cleanup(
                                        read_json(session / "session.json"),
                                        attempt_id=attempt_id,
                                        transcript=transcript,
                                    )
                                except BaseException as exc:
                                    cleanup = {
                                        "status": "unverified",
                                        "reason": f"cleanup probe raised {type(exc).__name__}: {exc}",
                                    }
                                reusable = cleanup.get("status") == "quiescent"
                                reason = f"hot bootstrap interrupt cleanup {cleanup.get('status')}"
                                if cleanup.get("reason"):
                                    reason += f": {cleanup['reason']}"
                            else:
                                reusable = True
                                reason = "hot bootstrap interrupted before broker dispatch; no child request started"
                        _finalize_lease(
                            pool,
                            lease_id,
                            reusable=reusable,
                            reason=reason,
                        )
                interrupted_signal = interrupted_signal or signal_state.interrupted_signal
                if interrupted_signal is not None:
                    result = 1
                else:
                    assert response is not None
                    result = int(response.get("exit_status") or 0)
        except ShelfAdmissionInterrupted:
            interrupted_signal = signal.SIGINT
        except WarmPoolEvalInterrupted as exc:
            interrupted_signal = exc.signum
        except KeyboardInterrupt:
            interrupted_signal = signal.SIGINT
    interrupted_signal = interrupted_signal or signal_state.interrupted_signal
    if interrupted_signal is not None:
        raise HotBootstrapInterrupted(interrupted_signal)
    return result


def run_published_hot_bootstrap(
    profile: PublishedWarmProfile,
    source: Path,
    *,
    timeout: float,
    transcript: Path,
) -> int:
    """Start a memory-first session and schedule only normal idle retirement."""

    from hol_workbench.orbstack_idle_retirement import schedule_profile_retirement

    try:
        return run_hot_profile_source(
            profile_root=profile.root,
            logical_profile=profile.name,
            source=source,
            transcript=transcript,
            timeout=timeout,
        )
    finally:
        if (profile.root / "pool").is_dir():
            schedule_profile_retirement(profile.root, logical_profile=profile.name)
