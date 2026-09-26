"""Run an ordinary HOL source file from a restored CRIU fork basis."""

from __future__ import annotations

import os
import secrets
import sys
import tempfile
from collections.abc import Callable
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hol_workbench.project_basis import BasisHandle

from hol_workbench.cli.hol_syntax_preflight import hol_syntax_preflight as _hol_syntax_preflight
from hol_workbench.cli.orbstack_criu_vanilla_artifacts import (
    metadata_path,
    raw_transcript_path,
    write_vanilla_artifacts,
)
from hol_workbench.cli.orbstack_criu_vanilla_semantics import (
    analyze_vanilla_transcript,
    displayed_transcript,
    instrumented_source_bytes,
)
from hol_workbench.criu_shelf_admission import ShelfAdmissionInterrupted, criu_shelf_admission
from hol_workbench.criu_shelf_capacity import restored_shelf_capacity
from hol_workbench.criu_shelf_owner import (
    new_shelf_owner,
    owner_cancel_command,
    progress_summary,
    read_shelf_owner,
    read_shelf_owners,
    shelf_owner_progress,
    update_shelf_owner,
)
from hol_workbench.hashing import normalized_sha256, sha256_bytes, sha256_file, short_sha256
from hol_workbench.jsonio import read_json
from hol_workbench.logical_source_roots import LogicalSourceRootError
from hol_workbench.pools import read_warm_pool, warm_pool_lock
from hol_workbench.pools.seat_lifecycle import checkout_pool_seat_locked, release_pool_seat_locked
from hol_workbench.profile_satisfied_dependencies import (
    ProfileSatisfactionError,
    revalidate_profile_satisfaction,
)
from hol_workbench.proof_run_fork_broker_probe import run_broker_capture_request
from hol_workbench.proof_run_warm_pool_interrupt import wait_for_fork_attempt_cleanup
from hol_workbench.proof_run_warm_session import warm_send_request
from hol_workbench.proofs.theorem_scan import extract_hol_theorems_bytes
from hol_workbench.restored_execution_topology import RestoredExecutionTopology
from hol_workbench.source_dependency_closure import SourceDependencyInferenceError
from hol_workbench.source_dependency_package import (
    DependencyPackageError,
    dependency_package_entrypoint,
    elf_package_transport,
    materialize_dependency_package,
)
from hol_workbench.source_execution_plan import (
    capture_source_dependency_closure,
    decide_profile_satisfaction,
    source_execution_prelude,
)
from hol_workbench.terminal_output import display_path
from hol_workbench.warm_eval_timeout import warm_eval_response_timeout


def _one_pool(profile_root: Path) -> Path:
    pools = sorted((profile_root / "pool").glob("*"))
    if len(pools) != 1:
        raise RuntimeError(f"expected one pool under {profile_root / 'pool'}, found {len(pools)}")
    return pools[0]


def _checkout(pool: Path) -> Path:
    with warm_pool_lock(pool):
        data = read_warm_pool(pool)
        item = checkout_pool_seat_locked(
            pool,
            data,
            agent=os.environ.get("HOL_WORKBENCH_AGENT"),
            owner_kind="vanilla_eval",
            owner_command="orbstack-criu vanilla",
        )
    return Path(str(item["session_dir"]))


def _release(pool: Path, session: Path, *, clean: bool, reason: str) -> dict:
    with warm_pool_lock(pool):
        data = read_warm_pool(pool)
        return release_pool_seat_locked(
            pool,
            data,
            session=session,
            status="idle" if clean else "dirty",
            reason=reason,
        )


def _vanilla_transcript(raw: bytes) -> bytes:
    """Remove the fork transport's private framing, not HOL output."""
    lines = raw.splitlines(keepends=True)
    return b"".join(
        line
        for line in lines
        if not line.startswith(b"[fork-worker] attempt ") and not line.startswith(b"__PROOF_RUN_FORK_CHILD_DONE__:")
    )


def _transport_label(response: dict | None, *, interrupted: bool) -> str:
    if interrupted:
        return "interrupted"
    if response is None:
        return "client-error"
    return "completed" if response.get("status") == "ok" else str(response.get("status") or "unknown")


def _send_vanilla_request(
    session: Path, request: dict, *, timeout_seconds: float | None,
    expected: dict | None = None,
) -> dict:
    session_path = session / "session.json"
    session_metadata = read_json(session_path) if session_path.is_file() else {}
    if session_metadata.get("execution_topology") == RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3:
        return run_broker_capture_request(
            session,
            request,
            timeout_seconds=timeout_seconds,
            expected=expected,
        )
    if expected is not None:
        raise RuntimeError("project basis requires the mechanical fork broker")
    return warm_send_request(
        session,
        request,
        timeout_seconds=timeout_seconds,
    )


def _print_artifact_locations(transcript: Path, metadata: Path) -> None:
    print(f"TRANSCRIPT: {display_path(transcript)}", file=sys.stderr)
    print(f"METADATA: {display_path(metadata)}", file=sys.stderr)


def _print_semantic_summary(
    *,
    profile: str,
    transport: str,
    semantic: dict,
    response: dict,
    seat: int,
    capacity: int,
    wait: float,
    evidence_role: str,
) -> None:
    print(
        "WARM VANILLA: "
        f"profile={profile} transport={transport} source={semantic.get('source_status') or 'unknown'} "
        f"eval={response.get('eval_elapsed_seconds', '-')}s seat={seat}/{capacity} wait={wait:.3f}s",
        file=sys.stderr,
    )
    print(f"MODE: {evidence_role}", file=sys.stderr)
    print("NOTE: source completion and transcript bindings are separate from transport", file=sys.stderr)
    foundation = semantic.get("foundation_delta")
    if not isinstance(foundation, dict):
        if semantic.get("first_failure"):
            print(f"FIRST_FAILURE: {semantic['first_failure']}", file=sys.stderr)
        return
    deltas = foundation.get("deltas") if isinstance(foundation, dict) else None
    if isinstance(deltas, dict):
        print(
            "FOUNDATION DELTA: "
            f"status={foundation.get('status') or 'unknown'} "
            f"axioms={deltas.get('axioms')} definitions={deltas.get('definitions')} "
            f"types={deltas.get('types')} constants={deltas.get('constants')}",
            file=sys.stderr,
        )
    else:
        print(f"FOUNDATION DELTA: {foundation.get('status') or 'unavailable'}", file=sys.stderr)
    advisory = semantic.get("promotion_advisory") or {}
    recommendation = advisory.get("recommendation") or "manual_review"
    if recommendation == "no_foundation_objection":
        print("FOUNDATION ADVISORY: no foundation objection (not publication authority)", file=sys.stderr)
    else:
        reasons = "; ".join(str(value) for value in advisory.get("reasons") or ["unknown reason"])
        print(
            f"FOUNDATION ADVISORY: {recommendation} (not publication authority) — {reasons}",
            file=sys.stderr,
        )
    if semantic.get("first_failure"):
        print(f"FIRST_FAILURE: {semantic['first_failure']}", file=sys.stderr)
    attribution = semantic.get("failing_binding") or {}
    if attribution.get("status") == "identified":
        print(f"FAILED_BINDING: {attribution['name']} "
              f"{attribution['source']}:{attribution['source_line']}", file=sys.stderr)


def _active_shelf_owners(profile_root: Path) -> list[dict]:
    primary = read_shelf_owner(profile_root)
    owners = [primary] if primary else []
    owners.extend(read_shelf_owners(profile_root))
    return list({str(owner.get("attempt_id")): owner for owner in owners if owner.get("attempt_id")}.values())


def _write_pre_eval_dependency_artifact(
    *,
    transcript_output: Path | None,
    source: Path,
    source_sha256: str,
    profile_root: Path,
    logical_profile: str,
    closure: dict,
    package: dict,
    profile_restore_requested: bool = False,
    evidence_role: str = "warm_development_only",
    refusal_stage: str = "dependency transport",
) -> None:
    if transcript_output is None:
        return
    reason = str(package.get("dependency_transport_reason") or "source dependency package is not transportable")
    boundary = (
        "profile restore may have completed; no HOL source evaluation was requested"
        if profile_restore_requested
        else "no HOL evaluation or profile restore was requested"
    )
    semantic = {
        "source_status": "not_started",
        "source_completed": False,
        "claims_complete": False,
        "semantic_exit_status": 2,
        "effective_exit_status": 2,
        "transport_status": "not_started",
        "process_exit_status": None,
        "first_failure": f"{refusal_stage}: {reason}",
        "observed_bindings": [],
        "bindings": [],
        "evidence": "pre_eval_dependency_transport_refusal",
        "authoritative_result": f"{refusal_stage} refused before HOL source evaluation",
        "evidence_boundary": boundary,
    }
    metadata = write_vanilla_artifacts(
        transcript_output,
        displayed_transcript="",
        raw_transcript="",
        source=source,
        source_sha256=source_sha256,
        executed_source_sha256=None,
        profile_root=profile_root,
        logical_profile=logical_profile,
        response=None,
        transport="not_started",
        admission_wait_seconds=0.0,
        semantic=semantic,
        source_dependency_closure=closure,
        dependency_package=package,
        evidence_role=evidence_role,
    )
    _print_artifact_locations(transcript_output, metadata)


def _profile_satisfaction_refusal(
    *,
    exc: ProfileSatisfactionError,
    transcript_output: Path | None,
    source: Path,
    source_sha256: str,
    profile_root: Path,
    logical_profile: str,
    closure: dict,
    package: dict,
    profile_restore_requested: bool = False,
    evidence_role: str = "warm_development_only",
) -> int:
    package.update(
        {
            "dependency_transport_status": exc.status,
            "dependency_transport_reason": str(exc),
        }
    )
    _write_pre_eval_dependency_artifact(
        transcript_output=transcript_output,
        source=source,
        source_sha256=source_sha256,
        profile_root=profile_root,
        logical_profile=logical_profile,
        closure=closure,
        package=package,
        profile_restore_requested=profile_restore_requested,
        evidence_role=evidence_role,
    )
    suffix = (
        "profile restore may have completed; HOL evaluation not started"
        if profile_restore_requested
        else "HOL evaluation and profile restore not started"
    )
    print(
        f"warm vanilla HOL: dependency_transport_status={exc.status}; {exc}; {suffix}",
        file=sys.stderr,
    )
    return 2


def _pinned_source_refusal(expected: str | None, actual: str) -> str | None:
    """Refuse unless the bytes read for evaluation are the caller's expected bytes.

    Only ``expected is None`` is unpinned, so callers that pass no expectation keep
    their prior behavior. A malformed pin refuses rather than disabling enforcement.
    """

    if expected is None:
        return None
    if normalized_sha256(expected) == actual:
        return None
    return (
        f"warm vanilla HOL: source_pin=refused; expected sha={short_sha256(expected) or 'malformed'} "
        f"actual sha={short_sha256(actual)}; HOL evaluation and profile restore not started"
    )


def run(
    *,
    profile_root: Path,
    source: Path,
    timeout: float | None,
    idle_timeout: float | None,
    restore: Callable[[], int],
    transcript_output: Path | None = None,
    logical_profile: str | None = None,
    logical_capacity: int | None = None,
    profile_cwd: Path | None = None,
    legacy_holdir_roots: tuple[Path, ...] = (),
    logical_source_root_declarations: tuple[dict[str, str], ...] = (),
    evidence_role: str = "warm_development_only",
    display_transcript: bool = True,
    expected_source_sha256: str | None = None,
    on_phase: Callable[[str], None] | None = None,
    preparation_prefix: bytes = b"",
    preparation_postlude: bytes = b"",
    preparation_package_root: Path | None = None,
    project_basis_handle: BasisHandle | None = None,
) -> int:
    report_phase = on_phase or (lambda phase: None)
    source = source.expanduser().resolve()
    if not source.is_file():
        print(f"warm vanilla HOL: source not found: {source}", file=sys.stderr)
        return 2
    if transcript_output is not None:
        transcript_output = transcript_output.expanduser().resolve()
        if source in {transcript_output, metadata_path(transcript_output), raw_transcript_path(transcript_output)}:
            print("warm vanilla HOL: --transcript and its metadata must not overwrite --source", file=sys.stderr)
            return 2
    logical_profile = logical_profile or profile_root.name
    source_bytes = source.read_bytes()
    source_sha256 = sha256_bytes(source_bytes)
    probe_nonce = secrets.token_hex(16)
    foundation_enabled = evidence_role == "recorded_warm_replay"

    def record_source_refusal(stage: str, reason: str, **extra: object) -> None:
        _write_pre_eval_dependency_artifact(
            transcript_output=transcript_output, source=source, source_sha256=source_sha256,
            profile_root=profile_root, logical_profile=logical_profile, closure={},
            package={"dependency_transport_status": "not_checked",
                     "dependency_transport_reason": reason,
                     "source_preflight_status": stage, **extra},
            evidence_role=evidence_role, refusal_stage=stage,
        )

    pin_refusal = _pinned_source_refusal(expected_source_sha256, source_sha256)
    if pin_refusal is not None:
        # The bytes read for evaluation are not the bytes the caller pinned: the
        # file changed between the two reads (an editor save or a host-to-guest
        # sync landing mid-capture). Nothing stale runs; the receipt names both.
        record_source_refusal(
            "source_changed_during_capture", pin_refusal,
            source_pin={"status": "refused", "pinned_sha256": expected_source_sha256,
                        "read_sha256": source_sha256,
                        "meaning": "the source changed between the pinned digest and the evaluation read; "
                                   "no HOL ran; rerun the same command once the file is stable"},
        )
        print(pin_refusal, file=sys.stderr)
        return 2

    try:
        claims = [claim for claim in extract_hol_theorems_bytes(source, source_bytes) if claim.get("name")]
        # Validate UTF-8, names, duplicates, and literal quotation hashes before
        # any parser, restore, or HOL process is launched.
        instrumented_source_bytes(
            source_bytes,
            claims,
            nonce=probe_nonce,
            include_foundation_delta=foundation_enabled,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        record_source_refusal("claim_probe_contract_refused", str(exc))
        print(
            f"warm vanilla HOL: claim_probe_contract=refused; {exc}; HOL evaluation and profile restore not started",
            file=sys.stderr,
        )
        return 2
    syntax_problem = _hol_syntax_preflight(source)
    if syntax_problem:
        record_source_refusal("syntax_preflight_failed", syntax_problem)
        print(
            f"warm vanilla HOL: syntax_preflight=failed; {syntax_problem}; "
            "HOL evaluation and profile restore not started",
            file=sys.stderr,
        )
        return 2
    try:
        closure, holdir_root = capture_source_dependency_closure(
            source,
            profile_cwd=profile_cwd,
            legacy_holdir_roots=legacy_holdir_roots,
            logical_source_root_declarations=logical_source_root_declarations,
        )
    except (LogicalSourceRootError, SourceDependencyInferenceError) as exc:
        closure = {
            "resolved_literal_closure": False,
            "semantic_identity_complete": False,
            "boundary": {
                "kind": "inference_refusal",
                **(exc.record() if isinstance(exc, SourceDependencyInferenceError) else {"status": exc.status}),
            },
        }
        package = {
            "dependency_transport_status": exc.status,
            "dependency_transport_reason": str(exc),
            "files": [],
        }
        _write_pre_eval_dependency_artifact(
            transcript_output=transcript_output,
            source=source,
            source_sha256=source_sha256,
            profile_root=profile_root,
            logical_profile=logical_profile,
            closure=closure,
            package=package,
            evidence_role=evidence_role,
        )
        print(
            f"warm vanilla HOL: dependency_transport_status={exc.status}; {exc}; "
            "HOL evaluation and profile restore not started",
            file=sys.stderr,
        )
        return 2
    try:
        profile_satisfaction, transport_status, transport_reason = decide_profile_satisfaction(
            closure,
            profile_root=profile_root,
            logical_profile=logical_profile,
            profile_cwd=profile_cwd,
            holdir_root=holdir_root,
        )
    except ProfileSatisfactionError as exc:
        package = {
            "dependency_transport_status": exc.status,
            "dependency_transport_reason": str(exc),
            "source_dependency_closure_sha256": closure.get("strict_sha256"),
            "files": [],
        }
        return _profile_satisfaction_refusal(
            exc=exc,
            transcript_output=transcript_output,
            source=source,
            source_sha256=source_sha256,
            profile_root=profile_root,
            logical_profile=logical_profile,
            closure=closure,
            package=package,
            evidence_role=evidence_role,
        )
    package: dict = {
        "dependency_transport_status": transport_status,
        "dependency_transport_reason": transport_reason,
        "source_dependency_closure_sha256": closure.get("strict_sha256"),
        "files": [],
        "profile_satisfaction": profile_satisfaction,
        "profile_satisfied_dependencies": [
            edge
            for edge in (profile_satisfaction or {}).get("edges") or []
            if edge.get("resolution") == "profile_satisfied"
        ],
        "profile_runtime_dependencies": [
            edge
            for edge in (profile_satisfaction or {}).get("edges") or []
            if edge.get("resolution") == "profile_runtime_fallback"
        ],
    }
    if not transport_status.startswith("packaged"):
        _write_pre_eval_dependency_artifact(
            transcript_output=transcript_output,
            source=source,
            source_sha256=source_sha256,
            profile_root=profile_root,
            logical_profile=logical_profile,
            closure=closure,
            package=package,
            evidence_role=evidence_role,
        )
        print(
            f"warm vanilla HOL: dependency_transport_status={transport_status}; {transport_reason}; "
            "HOL evaluation and profile restore not started",
            file=sys.stderr,
        )
        return 2

    temporary = tempfile.TemporaryDirectory(prefix="hol-warm-vanilla-")
    root = Path(temporary.name)
    transcript = root / "transcript.log"
    # Basis-defined closures can retain loaders bound to this exact package.
    # Keep preparation inputs in the owned generation beyond this attempt.
    package_root = preparation_package_root or (root / "source-package")
    expected_entrypoint = dependency_package_entrypoint(package_root, closure)
    elf_transport = elf_package_transport(closure)
    source_prelude = source_execution_prelude(
        package_root=package_root,
        virtual_entrypoint=expected_entrypoint,
        closure=closure,
        profile_satisfaction=profile_satisfaction,
        elf_transport=elf_transport,
    )
    source_prelude = preparation_prefix + source_prelude
    instrumented, probe_contract = instrumented_source_bytes(
        source_bytes,
        claims,
        nonce=probe_nonce,
        prefix_bytes=source_prelude,
        include_foundation_delta=foundation_enabled,
        diagnostic_source_path=str(expected_entrypoint),
    )
    if preparation_postlude:
        instrumented += b"\n" + preparation_postlude
        probe_contract["project_basis_postlude_sha256"] = sha256_bytes(preparation_postlude)
        probe_contract["executed_payload_sha256"] = sha256_bytes(instrumented)
    try:
        snapshot, package = materialize_dependency_package(
            source=source,
            closure=closure,
            destination=package_root,
            entrypoint_output_bytes=instrumented,
            profile_satisfaction=profile_satisfaction,
        )
    except (DependencyPackageError, OSError) as exc:
        temporary.cleanup()
        package = {
            **package,
            "dependency_transport_status": getattr(exc, "status", "refused_dependency_changed"),
            "dependency_transport_reason": str(exc),
        }
        _write_pre_eval_dependency_artifact(
            transcript_output=transcript_output,
            source=source,
            source_sha256=source_sha256,
            profile_root=profile_root,
            logical_profile=logical_profile,
            closure=closure,
            package=package,
            evidence_role=evidence_role,
        )
        print(
            f"warm vanilla HOL: dependency_transport_status={package['dependency_transport_status']}; "
            f"{exc}; HOL evaluation and profile restore not started",
            file=sys.stderr,
        )
        return 2
    package["literal_elf_transport"] = elf_transport
    if profile_satisfaction is not None:
        try:
            revalidate_profile_satisfaction(
                profile_satisfaction,
                closure,
                profile_root=profile_root,
            )
        except ProfileSatisfactionError as exc:
            temporary.cleanup()
            return _profile_satisfaction_refusal(
                exc=exc,
                transcript_output=transcript_output,
                source=source,
                source_sha256=source_sha256,
                profile_root=profile_root,
                logical_profile=logical_profile,
                closure=closure,
                package=package,
                evidence_role=evidence_role,
            )
    executed_source_sha256 = sha256_file(snapshot)
    if executed_source_sha256 != probe_contract["executed_payload_sha256"]:
        temporary.cleanup()
        print(
            "warm vanilla HOL: packaged entrypoint changed after exact-byte claim instrumentation; "
            "HOL evaluation and profile restore not started",
            file=sys.stderr,
        )
        return 2
    if preparation_package_root is not None:
        package["preparation_package_root"] = str(preparation_package_root)
    effective_capacity = restored_shelf_capacity(profile_root)
    if logical_capacity is not None and effective_capacity < logical_capacity:
        from hol_workbench.fork_pool_capacity import ensure_profile_logical_capacity

        try:
            ensure_profile_logical_capacity(profile_root, logical_capacity)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            print(
                f"warm vanilla HOL: failed to prepare {logical_capacity} logical seat(s): {exc}",
                file=sys.stderr,
            )
            return 1
        effective_capacity = restored_shelf_capacity(profile_root)
    owner = new_shelf_owner(
        profile_root,
        logical_profile=logical_profile,
        owner_kind="vanilla",
        source=source,
    )
    cleanup_receipt: dict | None = None

    def report_contention() -> None:
        active_owners = _active_shelf_owners(profile_root)
        if active_owners:
            for active in active_owners:
                progress = shelf_owner_progress(profile_root, active)
                print(
                    f"warm vanilla HOL: waiting for physical shelf {profile_root.name}; "
                    f"seat={active.get('admission_slot') or '-'} owner={active.get('attempt_id')} "
                    f"source={active.get('source')} elapsed={active.get('elapsed_seconds')}s "
                    f"progress={progress_summary(progress)}",
                    file=sys.stderr,
                    flush=True,
                )
                print(f"warm vanilla HOL: scoped cancel: {owner_cancel_command(active)}", file=sys.stderr, flush=True)
            return
        print(
            f"warm vanilla HOL: waiting for the physical shelf {profile_root.name}; "
            f"all {effective_capacity} seats are owned",
            file=sys.stderr,
            flush=True,
        )

    try:
        admission_options = {"on_contention": report_contention, "owner": owner}
        if effective_capacity > 1:
            admission_options["capacity"] = effective_capacity

        def expand_for_live_demand(target_capacity: int) -> int:
            current = restored_shelf_capacity(profile_root)
            if current < target_capacity:
                from hol_workbench.fork_pool_capacity import ensure_profile_logical_capacity

                ensure_profile_logical_capacity(profile_root, target_capacity)
            return restored_shelf_capacity(profile_root)

        admission_options["capacity_expander"] = expand_for_live_demand
        report_phase("admission")
        with criu_shelf_admission(profile_root, **admission_options) as admission:
            report_phase("restoring")
            effective_capacity = admission.capacity
            update_shelf_owner(profile_root, owner["attempt_id"], progress="restoring")
            restore_output = StringIO()
            with redirect_stdout(restore_output):
                restore_status = restore()
            if restore_status != 0:
                sys.stderr.write(restore_output.getvalue())
                return 1
            pool = _one_pool(profile_root)
            session = _checkout(pool)
            update_shelf_owner(
                profile_root,
                owner["attempt_id"],
                progress="evaluating",
                pool=str(pool),
                session=str(session),
            )
            if profile_satisfaction is not None:
                try:
                    revalidate_profile_satisfaction(
                        profile_satisfaction,
                        closure,
                        profile_root=profile_root,
                    )
                except ProfileSatisfactionError as exc:
                    _release(pool, session, clean=True, reason="profile satisfaction changed before evaluation")
                    return _profile_satisfaction_refusal(
                        exc=exc,
                        transcript_output=transcript_output,
                        source=source,
                        source_sha256=source_sha256,
                        profile_root=profile_root,
                        logical_profile=logical_profile,
                        closure=closure,
                        package=package,
                        profile_restore_requested=True,
                        evidence_role=evidence_role,
                    )
            response: dict | None = None
            evaluation_session = (
                project_basis_handle.session if project_basis_handle is not None else session
            )
            interrupted = False
            request = {
                "action": "eval",
                "attempt_id": f"vanilla-{secrets.token_hex(6)}",
                "source": str(snapshot),
                "transcript_path": str(transcript),
                "timeout_seconds": timeout,
                "idle_timeout_seconds": idle_timeout,
                "raw_source": True,
            }
            if foundation_enabled:
                request["foundation_marker_prefix"] = probe_contract["foundation_delta"]["marker_prefix"]
            semantic: dict = {}
            try:
                try:
                    report_phase("evaluation-request")
                    basis_expected = None
                    if project_basis_handle is not None:
                        from hol_workbench.project_basis import validate_basis_use
                        basis_expected = validate_basis_use(
                            project_basis_handle, closure, profile_satisfaction,
                        )
                    response = _send_vanilla_request(
                        evaluation_session,
                        request,
                        timeout_seconds=warm_eval_response_timeout(timeout),
                        expected=basis_expected,
                    )
                except (KeyboardInterrupt, ShelfAdmissionInterrupted):
                    interrupted = True
                    raise
            finally:
                report_phase("cancelling" if interrupted else "recording")
                if interrupted:
                    cleanup_status = wait_for_fork_attempt_cleanup(
                        read_json(evaluation_session / "session.json"),
                        attempt_id=str(request["attempt_id"]),
                        transcript=transcript,
                    )
                    cleanup_receipt = {
                        "scope": "one exact disposable fork child",
                        "status": cleanup_status.get("status"),
                        "verified_quiescent": cleanup_status.get("status") == "quiescent",
                        "active_children": cleanup_status.get("children") or [],
                    }
                clean = response is not None and response.get("status") in {"ok", "timeout", "cancelled"}
                if cleanup_receipt is not None:
                    clean = bool(cleanup_receipt["verified_quiescent"])
                if project_basis_handle is not None:
                    package["project_basis"] = project_basis_handle.record
                    if not clean:
                        from hol_workbench.project_basis import retire_basis
                        retire_basis(project_basis_handle)
                    # The global profile child was never used for this evaluation.
                    # Only the project-owned child can require retirement.
                    clean = True
                released = _release(
                    pool,
                    session,
                    clean=clean,
                    reason=(
                        f"warm vanilla eval {response.get('status')}"
                        if response is not None
                        else "warm vanilla client ended before a worker response"
                    ),
                )
                if cleanup_receipt is not None:
                    cleanup_receipt["seat_reusable"] = bool(released and released.get("status") == "idle")
                displayed = ""
                raw_transcript = ""
                raw_transcript_bytes = b""
                if transcript.is_file():
                    raw_transcript_bytes = _vanilla_transcript(transcript.read_bytes())
                    raw_transcript = raw_transcript_bytes.decode("utf-8", errors="replace")
                    displayed = displayed_transcript(raw_transcript_bytes, probe_contract)
                    if displayed and not displayed.endswith("\n"):
                        displayed += "\n"
                    if display_transcript:
                        sys.stdout.write(displayed)
                        sys.stdout.flush()
                # Map only compiler call sites actually needed by the recorded
                # diagnostics, while this attempt's exact package still exists.
                from hol_workbench.proof_diagnostics import attach_dependency_diagnostic_sources
                attach_dependency_diagnostic_sources(
                    probe_contract, closure, package_root, transcript=raw_transcript_bytes,
                )
                semantic = analyze_vanilla_transcript(
                    claims=claims,
                    transcript=raw_transcript_bytes,
                    contract=probe_contract,
                    transport=_transport_label(response, interrupted=interrupted),
                    response=response,
                )
                if transcript_output is not None:
                    metadata = write_vanilla_artifacts(
                        transcript_output,
                        displayed_transcript=displayed,
                        raw_transcript=raw_transcript,
                        source=source,
                        source_sha256=source_sha256,
                        executed_source_sha256=executed_source_sha256,
                        profile_root=profile_root,
                        logical_profile=logical_profile,
                        response=response,
                        transport=_transport_label(response, interrupted=interrupted),
                        admission_wait_seconds=admission.wait_seconds,
                        admission_slot=getattr(admission, "slot", 1),
                        effective_capacity=getattr(admission, "capacity", effective_capacity),
                        semantic=semantic,
                        cleanup=cleanup_receipt,
                        source_dependency_closure=closure,
                        dependency_package=package,
                        evidence_role=evidence_role,
                    )
                    _print_artifact_locations(transcript_output, metadata)
            assert response is not None
            exit_status = int(response.get("exit_status") or 0)
            transport = "completed" if response.get("status") == "ok" else str(response.get("status") or "unknown")
            _print_semantic_summary(
                profile=profile_root.name,
                transport=transport,
                semantic=semantic,
                response=response,
                seat=getattr(admission, "slot", 1),
                capacity=getattr(admission, "capacity", effective_capacity),
                wait=admission.wait_seconds,
                evidence_role=evidence_role,
            )
            return int(semantic.get("effective_exit_status") or exit_status)
    except ShelfAdmissionInterrupted:
        print("warm vanilla HOL: interrupted while waiting for the physical shelf", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        cleanup = cleanup_receipt or {
            "status": "unverified",
            "verified_quiescent": False,
            "seat_reusable": False,
        }
        print(
            "warm vanilla HOL: interrupted; disposable child cancellation requested; "
            f"cleanup={cleanup.get('status')}; seat_reusable={str(cleanup.get('seat_reusable')).lower()}",
            file=sys.stderr,
        )
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"warm vanilla HOL: {exc}", file=sys.stderr)
        return 1
    finally:
        temporary.cleanup()
