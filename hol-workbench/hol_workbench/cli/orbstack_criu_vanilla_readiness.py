"""Targeted readiness matrix for warm raw-HOL CRIU routes."""

from __future__ import annotations

import io
import json
import secrets
import shlex
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hol_workbench.cli import orbstack_criu_restore, orbstack_criu_vanilla
from hol_workbench.cli.orbstack_criu_vanilla_readiness_lifecycle import (
    ReadinessControllerBusy,
    RecoverProfile,
    bounded_message,
    bounded_output_tail,
    controller_identity_complete,
    controller_is_live,
    controller_record,
    profile_runtime_state,
    readiness_controller_lock,
)
from hol_workbench.cli.orbstack_criu_vanilla_readiness_worker import supervised_group_runner
from hol_workbench.cli.prove_profiles import (
    developer_profile_operation_smoke_lines as profile_operation_smoke_lines,
)
from hol_workbench.jsonio import durable_atomic_write_json, read_json
from hol_workbench.pools import read_warm_pool
from hol_workbench.proof_run_fork_children import read_active_children
from hol_workbench.proof_run_public_handoffs import host_tool_command

DEFAULT_PROFILES = ("light", "heavy")
READINESS_SCHEMA = "hol-workbench.warm-vanilla-readiness.v2"
MARKER_PREFIX = "HOL_WORKBENCH_WARM_VANILLA_READY"
REPORT_FILENAME = "warm-vanilla-readiness.json"
WORKBENCH_BIN = Path(__file__).resolve().parents[2] / "bin"

GroupRunner = Callable[..., dict[str, Any]]


def _write_source(root: Path, logical_profile: str, phase: str) -> tuple[Path, str]:
    marker = f"{MARKER_PREFIX}:{logical_profile}:{phase}:{secrets.token_hex(4)}"
    operations = profile_operation_smoke_lines(Path(__file__).resolve().parents[2] / "bin", logical_profile)
    operation = operations[0] if operations else "let WARM_VANILLA_ARITH = prove(`1 + 1 = 2`,ARITH_TAC);;"
    source = root / f"{phase}.ml"
    source.write_text(f'{operation}\nprint_endline "{marker}";;\n', encoding="utf-8")
    return source, marker


def _cleanup_state(profile_root: Path) -> dict[str, Any]:
    pools = sorted((profile_root / "pool").glob("*"))
    if len(pools) != 1:
        return {
            "verified_quiescent": False,
            "basis_reusable": False,
            "reason": f"expected one pool, found {len(pools)}",
        }
    pool = pools[0]
    try:
        data = read_warm_pool(pool)
        children = read_active_children(pool)
        runtime_live = orbstack_criu_restore.pool_is_live(pool)
    except (OSError, RuntimeError, SystemExit, ValueError) as exc:
        return {"verified_quiescent": False, "basis_reusable": False, "reason": str(exc)}
    statuses = [str(item.get("status") or "unknown") for item in data.get("sessions") or []]
    result = {
        "pool": str(pool),
        "verified_quiescent": not children,
        "runtime_live": runtime_live,
        "basis_reusable": (
            data.get("status") == "ready" and runtime_live and not children and any(x == "idle" for x in statuses)
        ),
        "active_children": children,
        "seat_statuses": statuses,
    }
    if data.get("status") == "ready" and not runtime_live:
        result["reason"] = "pool is recorded ready but its process identities are not live"
    return result


def _attempt(
    *,
    profile_root: Path,
    logical_profile: str,
    artifact_root: Path,
    phase: str,
    timeout: float,
    vanilla_run: Callable[..., int],
) -> dict[str, Any]:
    source, marker = _write_source(artifact_root, logical_profile, phase)
    transcript = artifact_root / f"{phase}.log"
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_status = vanilla_run(
            profile_root=profile_root,
            source=source,
            timeout=timeout,
            idle_timeout=None,
            restore=lambda: orbstack_criu_restore.main([str(profile_root)]),
            transcript_output=transcript,
            logical_profile=logical_profile,
        )
    transcript_text = transcript.read_text(encoding="utf-8", errors="replace") if transcript.exists() else ""
    metadata = read_json(Path(f"{transcript}.json"))
    transport = metadata.get("transport")
    worker_status = metadata.get("worker_status")
    exception_observed = "Exception:" in transcript_text
    marker_observed = marker in transcript_text
    semantic_source_status = metadata.get("semantic_source_status")
    semantic_passed = (
        semantic_source_status == "succeeded" if semantic_source_status else marker_observed and not exception_observed
    )
    return {
        "phase": phase,
        "exit_status": exit_status,
        "transport": transport,
        "worker_status": worker_status,
        "marker": marker,
        "marker_observed": marker_observed,
        "exception_observed": exception_observed,
        "semantic_source_status": semantic_source_status,
        "eval_elapsed_seconds": metadata.get("eval_elapsed_seconds"),
        "admission_wait_seconds": metadata.get("admission_wait_seconds"),
        "transcript": str(transcript),
        "metadata": str(Path(f"{transcript}.json")),
        "stderr_tail": bounded_output_tail(stderr.getvalue()),
        "passed": (
            exit_status == 0
            and transport == "completed"
            and worker_status == "ok"
            and marker_observed
            and semantic_passed
        ),
    }


def _run_group(
    *,
    physical_run_root: str,
    physical_profile: str,
    group: list[dict[str, Any]],
    run_root: Path,
    timeout: float,
    vanilla_run: Callable[..., int],
) -> dict[str, Any]:
    logical_profiles = [str(row["logical_profile"]) for row in group]
    logical_profile = logical_profiles[0]
    profile_root = Path(physical_run_root) / physical_profile
    artifact_root = run_root / ("--".join(logical_profiles))
    artifact_root.mkdir(parents=True, exist_ok=True)
    primary = _attempt(
        profile_root=profile_root,
        logical_profile=logical_profile,
        artifact_root=artifact_root,
        phase="primary",
        timeout=timeout,
        vanilla_run=vanilla_run,
    )
    cleanup = _cleanup_state(profile_root)
    reuse = None
    if primary["passed"] and cleanup["verified_quiescent"] and cleanup["basis_reusable"]:
        reuse = _attempt(
            profile_root=profile_root,
            logical_profile=logical_profile,
            artifact_root=artifact_root,
            phase="reuse",
            timeout=timeout,
            vanilla_run=vanilla_run,
        )
    final_cleanup = _cleanup_state(profile_root)
    ready = bool(
        primary["passed"]
        and reuse
        and reuse["passed"]
        and final_cleanup["verified_quiescent"]
        and final_cleanup["basis_reusable"]
    )
    return {
        "logical_profiles": logical_profiles,
        "physical_profile": physical_profile,
        "profile_root": str(profile_root),
        "status": "ready" if ready else "failed",
        "primary": primary,
        "cleanup_after_primary": cleanup,
        "reuse_probe": reuse,
        "cleanup_after_reuse": final_cleanup,
        "evidence": "warm development only",
        "authoritative_result": "HOL transcript marker and exception scan; transport alone is insufficient",
    }


def _inline_group_runner(
    *,
    physical_run_root: str,
    physical_profile: str,
    group: list[dict[str, Any]],
    run_root: Path,
    timeout: float,
    vanilla_run: Callable[..., int],
    **_kwargs: Any,
) -> dict[str, Any]:
    return {
        "exit_status": 0,
        "row": _run_group(
            physical_run_root=physical_run_root,
            physical_profile=physical_profile,
            group=group,
            run_root=run_root,
            timeout=timeout,
            vanilla_run=vanilla_run,
        ),
    }


def _public_orbstack_criu() -> str:
    return host_tool_command("orbstack-criu", fallback=str(WORKBENCH_BIN / "orbstack-criu"))


def _worker_row_matches(
    row: Any,
    *,
    logical_profiles: list[str],
    physical_profile: str,
    profile_root: Path,
) -> bool:
    return (
        isinstance(row, dict)
        and [str(item) for item in row.get("logical_profiles") or []] == logical_profiles
        and str(row.get("physical_profile") or "") == physical_profile
        and Path(str(row.get("profile_root") or "")).resolve() == profile_root.resolve()
        and row.get("status") in {"ready", "failed"}
    )


def _profile_commands(
    *,
    logical_profile: str,
    profile_root: Path | None,
    profiles: list[str],
    run_root: Path,
    timeout: float,
) -> dict[str, list[str]]:
    tool = _public_orbstack_criu()
    exact_stop = (
        [tool, "recover-stop-profile-root", str(profile_root)] if profile_root is not None else [tool, "status"]
    )
    return {
        "stop": exact_stop,
        "status": [tool, "status"],
        "retry": [
            tool,
            "vanilla-smoke",
            *profiles,
            "--run-root",
            str(run_root),
            "--timeout",
            str(timeout),
        ],
    }


def _recover_profile(
    *,
    logical_profile: str,
    profile_root: Path,
    profiles: list[str],
    run_root: Path,
    timeout: float,
    recover_profile: RecoverProfile | None,
) -> dict[str, Any]:
    commands = _profile_commands(
        logical_profile=logical_profile,
        profile_root=profile_root,
        profiles=profiles,
        run_root=run_root,
        timeout=timeout,
    )
    if recover_profile is None:
        return {
            "status": "not_attempted",
            "reason": "no recovery callback was supplied",
            "command": commands["stop"],
            "next_action": commands["stop"],
            "retry_command": commands["retry"],
        }
    try:
        result = recover_profile(profile_root)
    except (OSError, RuntimeError, SystemExit, ValueError) as exc:
        return {
            "status": "stop_failed",
            "reason": f"{type(exc).__name__}: {exc}",
            "command": commands["stop"],
            "next_action": commands["stop"],
            "retry_command": commands["retry"],
        }
    details = result if isinstance(result, dict) else {}
    raw_exit_status = details.get("exit_status") if isinstance(result, dict) else result
    try:
        if raw_exit_status is None:
            raise TypeError
        exit_status = int(raw_exit_status)
    except (TypeError, ValueError):
        exit_status = 1
        details = {
            **details,
            "reason": details.get("reason") or "exact recovery returned no valid exit status",
        }
    stopped = exit_status == 0 and (not details or details.get("status") == "stopped")
    return {
        "status": "stopped" if stopped else "stop_failed",
        "exit_status": exit_status,
        "profile_root": str(profile_root),
        "exact_recovery": details or None,
        "command": commands["stop"],
        "verification_command": commands["status"],
        "next_action": commands["retry"] if stopped else commands["stop"],
        "retry_command": commands["retry"],
    }


def _checkpoint(path: Path, report: dict[str, Any]) -> None:
    durable_atomic_write_json(path, report)


def _interrupted_run_recovery(
    *,
    prior: dict[str, Any],
    report_path: Path,
    root: Path,
    profiles: list[str],
    run_root: Path,
    timeout: float,
    recover_profile: RecoverProfile | None,
) -> tuple[int, dict[str, Any]] | None:
    if prior.get("status") not in {"running", "recovery_failed"} or not isinstance(prior.get("active_profile"), dict):
        return None
    safe_next = [_public_orbstack_criu(), "status"]

    def refuse(reason: str) -> tuple[int, dict[str, Any]]:
        refusal = dict(prior)
        refusal["refusal"] = reason
        refusal["next_action"] = safe_next
        return 1, refusal

    if prior.get("schema") != READINESS_SCHEMA:
        return refuse("the prior readiness receipt schema is not trusted for automatic recovery")
    try:
        recorded_run_root_path = Path(str(prior.get("run_root") or ""))
        recorded_shelf_root_path = Path(str(prior.get("shelf_root") or ""))
        if not recorded_run_root_path.is_absolute() or not recorded_shelf_root_path.is_absolute():
            return refuse("the prior readiness receipt roots are not exact absolute paths")
        recorded_run_root = recorded_run_root_path.resolve()
        recorded_shelf_root = recorded_shelf_root_path.resolve()
        expected_run_root = run_root.resolve()
        expected_shelf_root = root.resolve()
    except (OSError, RuntimeError, ValueError):
        return refuse("the prior readiness receipt roots could not be resolved safely")
    if recorded_run_root != expected_run_root:
        return refuse("the prior readiness receipt belongs to a different run root")
    if recorded_shelf_root != expected_shelf_root:
        return refuse("the prior readiness receipt belongs to a different shelf root")

    active = dict(prior["active_profile"])
    logical_profiles = [str(item) for item in active.get("logical_profiles") or []]
    logical_profile = logical_profiles[0] if logical_profiles else ""
    profile_root_value = str(active.get("profile_root") or "")
    try:
        profile_root_path = Path(profile_root_value)
        recorded_profile_root = (
            profile_root_path.resolve() if profile_root_value and profile_root_path.is_absolute() else None
        )
    except (OSError, RuntimeError, ValueError):
        recorded_profile_root = None
    if (
        recorded_profile_root is None
        or recorded_profile_root == expected_shelf_root
        or not recorded_profile_root.is_relative_to(expected_shelf_root)
    ):
        return refuse("the prior readiness receipt has no exact physical profile root beneath this shelf root")
    if controller_is_live(prior.get("controller")):
        return refuse("a readiness controller is still live for this run root")
    if not controller_identity_complete(prior.get("controller")):
        return refuse("the prior readiness controller identity is incomplete; automatic recovery is unsafe")
    recovery = _recover_profile(
        logical_profile=logical_profile,
        profile_root=recorded_profile_root,
        profiles=logical_profiles or profiles,
        run_root=run_root,
        timeout=timeout,
        recover_profile=recover_profile,
    )
    recovered = recovery["status"] == "stopped"
    report = {
        **prior,
        "status": "interrupted_recovered" if recovered else "recovery_failed",
        "finished_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "active_profile": None if recovered else active,
        "prior_interruption": {
            "profile": active,
            "controller": prior.get("controller"),
            "recovery": recovery,
        },
        "ready": False,
        "next_action": recovery["retry_command"] if recovered else recovery["next_action"],
    }
    _checkpoint(report_path, report)
    return 1, report


def run(
    *,
    root: Path,
    profiles: list[str],
    run_root: Path,
    timeout: float,
    compatibility: Callable[[str], dict[str, Any]],
    vanilla_run: Callable[..., int] = orbstack_criu_vanilla.run,
    group_runner: GroupRunner | None = None,
    recover_profile: RecoverProfile | None = None,
) -> tuple[int, dict[str, Any]]:
    """Probe selected compatible physical shelves under one run-root controller lock."""
    root = root.expanduser().resolve()
    run_root = run_root.expanduser().resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    report_path = run_root / REPORT_FILENAME
    try:
        with readiness_controller_lock(run_root):
            return _run_locked(
                root=root,
                profiles=profiles,
                run_root=run_root,
                timeout=timeout,
                compatibility=compatibility,
                vanilla_run=vanilla_run,
                group_runner=group_runner,
                recover_profile=recover_profile,
            )
    except ReadinessControllerBusy as exc:
        prior = read_json(report_path)
        results = prior.get("results") if isinstance(prior.get("results"), list) else []
        return 1, {
            **prior,
            "schema": prior.get("schema") or READINESS_SCHEMA,
            "status": "busy",
            "ready": False,
            "results": results,
            "report_path": str(report_path),
            "refusal": str(exc),
            "next_action": [_public_orbstack_criu(), "status"],
        }


def _run_locked(
    *,
    root: Path,
    profiles: list[str],
    run_root: Path,
    timeout: float,
    compatibility: Callable[[str], dict[str, Any]],
    vanilla_run: Callable[..., int] = orbstack_criu_vanilla.run,
    group_runner: GroupRunner | None = None,
    recover_profile: RecoverProfile | None = None,
) -> tuple[int, dict[str, Any]]:
    """Run after the caller has acquired the run-root controller lock."""
    started_utc = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_root.mkdir(parents=True, exist_ok=True)
    report_path = run_root / REPORT_FILENAME
    prior = read_json(report_path)
    interrupted = _interrupted_run_recovery(
        prior=prior,
        report_path=report_path,
        root=root,
        profiles=profiles,
        run_root=run_root,
        timeout=timeout,
        recover_profile=recover_profile,
    )
    if interrupted is not None:
        return interrupted
    rows = [compatibility(profile) for profile in profiles]
    results: list[dict[str, Any]] = []
    physical_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("compatibility_status") != "compatible":
            results.append(
                {
                    "logical_profiles": [row.get("logical_profile")],
                    "status": "skipped",
                    "compatibility_status": row.get("compatibility_status"),
                    "reason": row.get("reason"),
                    "rebuild_command": row.get("rebuild_command"),
                    "evidence": "warm development only",
                }
            )
            continue
        key = (str(row["run_root"]), str(row["physical_profile"]))
        physical_groups.setdefault(key, []).append(row)

    report = {
        "schema": READINESS_SCHEMA,
        "status": "running",
        "started_utc": started_utc,
        "finished_utc": None,
        "controller": controller_record(),
        "active_profile": None,
        "shelf_root": str(root),
        "run_root": str(run_root),
        "report_path": str(report_path),
        "requested_profiles": profiles,
        "default_profiles": list(DEFAULT_PROFILES),
        "cold_builds_started": False,
        "results": results,
        "ready": False,
    }
    _checkpoint(report_path, report)
    runner = group_runner
    if runner is None:
        runner = supervised_group_runner if vanilla_run is orbstack_criu_vanilla.run else _inline_group_runner
    grouped = list(physical_groups.items())
    for group_index, ((physical_run_root, physical_profile), group) in enumerate(grouped):
        logical_profiles = [str(row["logical_profile"]) for row in group]
        logical_profile = logical_profiles[0]
        physical_profile_path = Path(physical_profile)
        if (
            not physical_profile
            or physical_profile_path.is_absolute()
            or physical_profile_path.name != physical_profile
        ):
            profile_root = None
            profile_root_reason = "physical profile name is not one exact path component"
        else:
            try:
                profile_root = (Path(physical_run_root).expanduser() / physical_profile_path).resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                profile_root = None
                profile_root_reason = f"physical profile root could not be resolved safely: {exc}"
            else:
                profile_root_reason = ""
                if profile_root.name != physical_profile:
                    profile_root_reason = "resolved physical profile name does not match its exact component"
                elif profile_root == root or not profile_root.is_relative_to(root):
                    profile_root_reason = "physical profile root is not strictly beneath the configured shelf root"
        if profile_root is None or profile_root_reason:
            results.append(
                {
                    "logical_profiles": logical_profiles,
                    "physical_profile": physical_profile,
                    "profile_root": str(profile_root or Path(physical_run_root) / physical_profile),
                    "status": "failed",
                    "failure_kind": "untrusted_profile_root",
                    "reason": profile_root_reason,
                    "evidence": "warm development only",
                }
            )
            for _remaining_key, remaining_group in grouped[group_index + 1 :]:
                results.append(
                    {
                        "logical_profiles": [str(item["logical_profile"]) for item in remaining_group],
                        "physical_profile": str(remaining_group[0]["physical_profile"]),
                        "status": "not_run",
                        "reason": f"fail-closed after {physical_profile}",
                        "evidence": "warm development only",
                    }
                )
            report["status"] = "failed"
            report["finished_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            report["next_action"] = [_public_orbstack_criu(), "status"]
            _checkpoint(report_path, report)
            return 1, report
        physical_run_root = str(profile_root.parent)
        initial_runtime = profile_runtime_state(profile_root)
        commands = _profile_commands(
            logical_profile=logical_profile,
            profile_root=profile_root,
            profiles=logical_profiles,
            run_root=run_root,
            timeout=timeout,
        )
        if not initial_runtime.get("known"):
            results.append(
                {
                    "logical_profiles": logical_profiles,
                    "physical_profile": physical_profile,
                    "profile_root": str(profile_root),
                    "status": "failed",
                    "failure_kind": "runtime_prestate_unavailable",
                    "reason": initial_runtime.get("reason"),
                    "initial_runtime": initial_runtime,
                    "evidence": "warm development only",
                }
            )
            for _remaining_key, remaining_group in grouped[group_index + 1 :]:
                results.append(
                    {
                        "logical_profiles": [str(item["logical_profile"]) for item in remaining_group],
                        "physical_profile": str(remaining_group[0]["physical_profile"]),
                        "status": "not_run",
                        "reason": f"fail-closed after {physical_profile}",
                        "evidence": "warm development only",
                    }
                )
            report["status"] = "failed"
            report["finished_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            report["next_action"] = commands["status"]
            _checkpoint(report_path, report)
            return 1, report
        report["active_profile"] = {
            "logical_profiles": logical_profiles,
            "physical_profile": physical_profile,
            "profile_root": str(profile_root),
            "initial_runtime": initial_runtime,
            "started_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recovery_action": commands["stop"],
        }
        _checkpoint(report_path, report)
        try:
            outcome = runner(
                physical_run_root=physical_run_root,
                physical_profile=physical_profile,
                group=group,
                run_root=run_root,
                timeout=timeout,
                vanilla_run=vanilla_run,
                report_path=report_path,
                controller=report["controller"],
            )
        except (OSError, RuntimeError, ValueError) as exc:
            outcome = {
                "exit_status": 1,
                "row": None,
                "worker_error": f"{type(exc).__name__}: {exc}",
            }
        worker_exit = int(outcome.get("exit_status") or 0)
        raw_returncode = int(outcome.get("raw_returncode", worker_exit))
        signal_number = outcome.get("signal")
        row = outcome.get("row")
        if not isinstance(row, dict) or not _worker_row_matches(
            row,
            logical_profiles=logical_profiles,
            physical_profile=physical_profile,
            profile_root=profile_root,
        ):
            if isinstance(row, dict) and not outcome.get("worker_error"):
                outcome["worker_error"] = "worker result identity does not match the requested physical profile"
            reason = bounded_message(str(outcome.get("worker_error") or ""))
            if not reason:
                reason = (
                    f"physical-profile worker exited {worker_exit} before publishing a result"
                    if worker_exit
                    else "physical-profile worker published no valid result"
                )
            row = {
                "logical_profiles": logical_profiles,
                "physical_profile": physical_profile,
                "profile_root": str(profile_root),
                "status": "failed",
                "failure_kind": "worker_terminated",
                "reason": reason,
                "worker": {
                    **outcome,
                    "exit_status": worker_exit,
                    "raw_returncode": raw_returncode,
                    "signal": signal_number,
                },
                "initial_runtime": initial_runtime,
                "evidence": "warm development only",
            }
        else:
            row["initial_runtime"] = initial_runtime
            row["worker"] = {
                "exit_status": worker_exit,
                "raw_returncode": raw_returncode,
                "signal": signal_number,
                "stderr_tail": outcome.get("stderr_tail") or [],
                "stdout_tail": outcome.get("stdout_tail") or [],
                "spec": outcome.get("spec"),
                "result": outcome.get("result"),
            }
            if worker_exit != 0:
                row["status"] = "failed"
                row["failure_kind"] = "worker_exit_after_result"
                row["reason"] = f"physical-profile worker exited {worker_exit} after publishing its row"
        results.append(row)
        if worker_exit != 0 or row.get("status") != "ready":
            recovery = _recover_profile(
                logical_profile=logical_profile,
                profile_root=profile_root,
                profiles=logical_profiles,
                run_root=run_root,
                timeout=timeout,
                recover_profile=recover_profile,
            )
            row["recovery"] = recovery
            for _remaining_key, remaining_group in grouped[group_index + 1 :]:
                results.append(
                    {
                        "logical_profiles": [str(item["logical_profile"]) for item in remaining_group],
                        "physical_profile": str(remaining_group[0]["physical_profile"]),
                        "status": "not_run",
                        "reason": f"fail-closed after {physical_profile}",
                        "evidence": "warm development only",
                    }
                )
            recovery_failed = recovery["status"] != "stopped"
            report["status"] = "recovery_failed" if recovery_failed else "failed"
            report["finished_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if not recovery_failed:
                report["active_profile"] = None
            report["next_action"] = recovery["next_action"]
            _checkpoint(report_path, report)
            return 1, report
        if initial_runtime.get("runtime_live") is True:
            row["post_smoke_retention"] = "retained_preexisting_live_basis"
        else:
            retirement = _recover_profile(
                logical_profile=logical_profile,
                profile_root=profile_root,
                profiles=logical_profiles,
                run_root=run_root,
                timeout=timeout,
                recover_profile=recover_profile,
            )
            row["post_smoke_cleanup"] = retirement
            if retirement["status"] != "stopped":
                row["status"] = "failed"
                row["failure_kind"] = "post_smoke_stop_failed"
                row["reason"] = "readiness restored a previously stopped basis but could not verify its stop"
                for _remaining_key, remaining_group in grouped[group_index + 1 :]:
                    results.append(
                        {
                            "logical_profiles": [str(item["logical_profile"]) for item in remaining_group],
                            "physical_profile": str(remaining_group[0]["physical_profile"]),
                            "status": "not_run",
                            "reason": f"fail-closed after {physical_profile}",
                            "evidence": "warm development only",
                        }
                    )
                report["status"] = "recovery_failed"
                report["finished_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                report["next_action"] = retirement["next_action"]
                _checkpoint(report_path, report)
                return 1, report
            row["post_smoke_retention"] = "stopped_after_smoke"
        report["active_profile"] = None
        _checkpoint(report_path, report)

    report["finished_utc"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    report["ready"] = bool(results) and all(row["status"] == "ready" for row in results)
    report["status"] = "ready" if report["ready"] else "failed"
    report["active_profile"] = None
    if not report["ready"]:
        report["next_action"] = [_public_orbstack_criu(), "status"]
    _checkpoint(report_path, report)
    return (0 if report["ready"] else 1), report


def print_report(report: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print("Warm vanilla readiness (warm development only)")
    if report.get("refusal"):
        print(f"REFUSAL: {report['refusal']}")
    for row in report["results"]:
        names = ",".join(row["logical_profiles"])
        print(f"- {names}: {row['status']}")
        if row["status"] in {"skipped", "not_run"}:
            print(f"  reason: {row.get('reason')}")
            if row.get("rebuild_command"):
                print(f"  rebuild: {row.get('rebuild_command')}")
        elif row["status"] == "failed" and not isinstance(row.get("primary"), dict):
            print(f"  reason: {row.get('reason')}")
            worker = row.get("worker") or {}
            if worker.get("exit_status") is not None:
                print(f"  worker_exit: {worker.get('exit_status')}")
            recovery = row.get("recovery") or {}
            if recovery:
                print(f"  recovery: {recovery.get('status')}")
        else:
            primary = row["primary"]
            reuse = row.get("reuse_probe") or {}
            print(
                f"  physical={row['physical_profile']} primary={primary.get('eval_elapsed_seconds')}s "
                f"reuse={reuse.get('eval_elapsed_seconds')}s wait={primary.get('admission_wait_seconds')}s"
            )
            print(
                f"  cleanup: quiescent={row['cleanup_after_reuse']['verified_quiescent']} "
                f"basis_reusable={row['cleanup_after_reuse']['basis_reusable']}"
            )
            if row["status"] == "failed":
                print(
                    f"  failure: primary_exit={primary.get('exit_status')} transport={primary.get('transport') or '-'}"
                )
                recovery = row.get("recovery") or {}
                if recovery:
                    print(f"  recovery: {recovery.get('status')}")
            if row.get("post_smoke_retention"):
                print(f"  retention: {row['post_smoke_retention']}")
    next_action = report.get("next_action")
    if isinstance(next_action, list) and next_action:
        print(f"NEXT: {shlex.join(str(item) for item in next_action)}")
    if report.get("report_path"):
        print(f"DETAILS: {report['report_path']}")
