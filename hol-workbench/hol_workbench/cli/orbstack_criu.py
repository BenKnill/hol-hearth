"""First-class OrbStack/CRIU warm-profile shelf support."""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stderr
from pathlib import Path

from hol_workbench.cli import (
    orbstack_criu_build,
    orbstack_criu_restore,
    orbstack_criu_vanilla,
    orbstack_criu_vanilla_readiness,
    orbstack_criu_vanilla_readiness_lifecycle,
    orbstack_criu_vanilla_readiness_worker,
)
from hol_workbench.cli.orbstack_criu_profile_plan import known_profile_names
from hol_workbench.cli.orbstack_criu_vanilla_artifacts import default_transcript_path
from hol_workbench.cli.prove_profiles import (
    developer_warmup_profile_identity_assignments as warmup_profile_assignments,
)
from hol_workbench.cli.prove_profiles import (
    developer_warmup_profile_publication_guard as warmup_profile_publication_guard,
)
from hol_workbench.criu_shelf_capacity import restored_shelf_capacity
from hol_workbench.criu_shelf_demand import read_shelf_demands
from hol_workbench.criu_shelf_owner import (
    cancel_shelf_owner,
    owner_cancel_command,
    progress_summary,
    read_shelf_owner,
    read_shelf_owners,
    shelf_owner_progress,
)
from hol_workbench.criu_snapshot_admission import (
    SnapshotAdmissionRequest,
    StaticSnapshotAdmissionDecision,
)
from hol_workbench.criu_snapshot_compat import (
    SNAPSHOT_MANIFEST_FILENAME,
    default_snapshot_admission_request,
    publication_identity_guard_failures,
    snapshot_projection_compatibility,
    snapshot_runtime_compatibility,
    validate_snapshot_manifest,
)
from hol_workbench.fork_pool_capacity import ensure_profile_logical_capacity
from hol_workbench.fork_pool_lifecycle import stop_pool_runtime
from hol_workbench.orbstack_idle_retirement import runtime_cache_state, schedule_profile_retirement
from hol_workbench.orbstack_runtime_inventory import (
    guest_memory_summary,
    profile_runtime_inventory,
    summarize_profile_memory,
)
from hol_workbench.profile_project_context import profile_context_problem
from hol_workbench.restored_execution_topology import (
    RestoredExecutionTopology,
    restored_execution_topology,
)
from hol_workbench.ubuntu_runtime_layout import (
    UbuntuRuntimeLayoutError,
    resolve_profile_cwd,
    resolve_ubuntu_runtime_layout,
)

WORKBENCH_BIN = Path(__file__).resolve().parents[2] / "bin"


def _successful_rows(root: Path) -> list[dict]:
    rows: list[dict] = []
    for result in sorted(root.glob("criu-warm-profiles-*/results.json")):
        try:
            candidates = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in candidates if isinstance(candidates, list) else []:
            if isinstance(row, dict) and row.get("status") == "ok":
                rows.append({**row, "run_root": str(result.parent)})
    return rows


def compatible_profile_row(
    root: Path,
    profile: str,
    *,
    expected_base_name: str,
    expected_cwd: str,
    expected_basis_id: str = "",
    expected_sha256: str = "",
    expected_capacity: int = 1,
    expected_execution_topology: str | None = None,
    admission_request: SnapshotAdmissionRequest | None = None,
) -> dict:
    required_publication_guard = warmup_profile_publication_guard(WORKBENCH_BIN, profile)
    result = profile_compatibility(
        root,
        profile,
        expected_base_name=expected_base_name,
        expected_cwd=expected_cwd,
        expected_basis_id=expected_basis_id,
        expected_sha256=expected_sha256,
        expected_capacity=expected_capacity,
        expected_execution_topology=expected_execution_topology,
    )
    request = admission_request or default_snapshot_admission_request()
    physical_profile = str(result.get("physical_profile") or "")
    run_root = str(result.get("run_root") or "")
    manifest_path = Path(run_root) / physical_profile / SNAPSHOT_MANIFEST_FILENAME
    authoritative_failure = "authoritative snapshot manifest is missing"
    if manifest_path.is_file():
        try:
            manifest = validate_snapshot_manifest(
                manifest_path.parent,
                admission_request=request,
                required_publication_guard=required_publication_guard,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            authoritative_failure = str(exc)
        else:
            decision = StaticSnapshotAdmissionDecision.from_record(manifest["static_admission_decision"])
            identity = decision.profile_identity
            identity_failures = []
            if identity.get("profile") != physical_profile:
                identity_failures.append(
                    f"manifest profile {identity.get('profile') or 'missing'} != selected {physical_profile}"
                )
            if Path(str(identity.get("profile_base") or "")).name != expected_base_name:
                identity_failures.append(
                    f"manifest base {Path(str(identity.get('profile_base') or '-')).name} != expected {expected_base_name}"
                )
            if str(identity.get("profile_cwd") or "") != expected_cwd:
                identity_failures.append(
                    f"manifest cwd {identity.get('profile_cwd') or 'missing'} != expected {expected_cwd}"
                )
            if expected_basis_id and str(identity.get("profile_basis_id") or "") != expected_basis_id:
                identity_failures.append(
                    f"manifest basis ID {identity.get('profile_basis_id') or 'missing'} != expected {expected_basis_id}"
                )
            if expected_sha256 and str(identity.get("profile_sha256") or "") != expected_sha256:
                identity_failures.append(
                    f"manifest SHA-256 {identity.get('profile_sha256') or 'missing'} != expected {expected_sha256}"
                )
            if expected_execution_topology and identity.get("execution_topology") != expected_execution_topology:
                identity_failures.append(
                    f"manifest topology {identity.get('execution_topology') or 'missing'} "
                    f"!= expected {expected_execution_topology}"
                )
            if identity_failures:
                authoritative_failure = "; ".join(identity_failures)
            else:
                return {
                    **result,
                    "compatibility_status": "compatible",
                    "reason": decision.reason,
                    "runtime_compatibility": decision.runtime_compatibility,
                    "runtime_drift": decision.runtime_compatibility.get("runtime_drift"),
                    "static_admission_decision": decision.record(),
                }
    raise SystemExit(
        f"no compatible CRIU shelf published for profile {profile}: "
        f"authoritative admission failed: {authoritative_failure}; "
        f"candidate projection: {result['reason']}; maintainer shelf publication is required"
    )


def _compact_identity_value(value: str) -> str:
    return value if len(value) <= 16 else f"{value[:12]}..."


def _matches_expected_topology(row: dict, expected_execution_topology: str | None) -> bool:
    if not expected_execution_topology:
        return True
    try:
        return restored_execution_topology(row).value == expected_execution_topology
    except RuntimeError:
        return False


def _matches_publication_guard(row: dict, required_publication_guard: dict | None) -> bool:
    if required_publication_guard is None:
        return True
    return not publication_identity_guard_failures(
        row.get("publication_identity_guard"),
        expected_guard=required_publication_guard,
    )


def _identity_mismatch_reason(
    row: dict,
    *,
    expected_base_name: str,
    expected_cwd: str,
    expected_basis_id: str,
    expected_sha256: str,
    expected_execution_topology: str | None,
    required_publication_guard: dict | None,
) -> str:
    problems: list[str] = []
    actual_base = Path(str(row.get("profile_base") or "-")).name
    if actual_base != expected_base_name:
        problems.append(f"expected base {expected_base_name}, latest is {actual_base}")

    actual_cwd = str(row.get("profile_cwd") or "")
    if actual_cwd != expected_cwd:
        if actual_cwd:
            problems.append(f"expected cwd {expected_cwd}, latest is {actual_cwd}")
        else:
            problems.append(f"expected cwd {expected_cwd}, latest shelf has no recorded cwd")

    actual_sha256 = str(row.get("profile_sha256") or "")
    if actual_sha256 and expected_sha256 and actual_sha256 != expected_sha256:
        problems.append(
            "expected SHA-256 "
            f"{_compact_identity_value(expected_sha256)}, latest is {_compact_identity_value(actual_sha256)}"
        )

    actual_basis_id = str(row.get("profile_basis_id") or "")
    if expected_basis_id and actual_basis_id != expected_basis_id:
        problems.append(f"expected basis ID {expected_basis_id}, latest is {actual_basis_id or 'missing'}")

    if expected_execution_topology:
        try:
            actual_topology = restored_execution_topology(row).value
        except RuntimeError:
            actual_topology = "invalid"
        if actual_topology != expected_execution_topology:
            problems.append(f"expected topology {expected_execution_topology}, latest is {actual_topology}")
    if not _matches_publication_guard(row, required_publication_guard):
        problems.append("latest shelf does not satisfy the current publication identity guard")

    return "; ".join(problems) or "recorded profile identity does not match the requested route"


def profile_compatibility(
    root: Path,
    profile: str,
    *,
    expected_base_name: str,
    expected_cwd: str,
    expected_basis_id: str = "",
    expected_sha256: str = "",
    expected_capacity: int = 1,
    expected_execution_topology: str | None = None,
) -> dict:
    """Describe current route compatibility without restoring a shelf."""

    required_publication_guard = warmup_profile_publication_guard(WORKBENCH_BIN, profile)
    rows = _successful_rows(root)
    matches = [row for row in rows if row.get("profile") == profile]
    same_base_rows = [row for row in rows if Path(str(row.get("profile_base") or "")).name == expected_base_name]
    expected_identity = [
        row
        for row in rows
        if Path(str(row.get("profile_base") or "")).name == expected_base_name
        and str(row.get("profile_cwd") or "") == expected_cwd
        and (not expected_basis_id or row.get("profile_basis_id") == expected_basis_id)
        and (not row.get("profile_sha256") or not expected_sha256 or row.get("profile_sha256") == expected_sha256)
        and _matches_publication_guard(row, required_publication_guard)
    ]
    contracts = [
        (row, snapshot_projection_compatibility(row), snapshot_runtime_compatibility(row)) for row in expected_identity
    ]
    compatible = [
        (row, projection, runtime)
        for row, projection, runtime in contracts
        if projection["compatible"] and runtime["compatible"]
    ]
    rebuild_command = f"CRIU_PROFILES={profile} hol-workbench/bin/orbstack-criu build"
    if compatible:
        exact = [(row, projection, runtime) for row, projection, runtime in compatible if row.get("profile") == profile]
        winner, projection, runtime = exact[-1] if exact else compatible[-1]
        reason = (
            "current profile identity, manifest authority, and mechanical broker contract match"
            if runtime.get("execution_topology") == RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3.value
            else "current profile identity, manifest authority, and fork-worker capabilities match"
        )
        if runtime["runtime_drift"]:
            reason += "; embedded runtime build differs but is ABI-compatible"
        published_capacity = int(winner.get("orbstack_public_capacity") or 1)
        capacity_expansion = max(0, expected_capacity - published_capacity)
        if capacity_expansion:
            reason += (
                f"; controller will add {capacity_expansion} logical seat(s) to the existing fork basis after restore"
            )
        return {
            **winner,
            "logical_profile": profile,
            "physical_profile": str(winner.get("profile") or profile),
            "historical_build_status": "ok",
            "compatibility_status": "compatible",
            "reason": reason,
            "manifest_compatibility": projection,
            "runtime_compatibility": runtime,
            "runtime_drift": runtime["runtime_drift"],
            "published_capacity": published_capacity,
            "requested_capacity": expected_capacity,
            "logical_capacity_expansion_required": capacity_expansion,
            "rebuild_command": None,
        }
    stale_runtime_rows = [
        (row, projection, runtime)
        for row, projection, runtime in contracts
        if not projection["compatible"] or not runtime["compatible"]
    ]
    selected_projection: dict | None = None
    selected_runtime: dict | None = None
    if stale_runtime_rows:
        status = "stale-runtime"
        historical, selected_projection, selected_runtime = stale_runtime_rows[-1]
        reason = str(
            selected_projection["reason"] if not selected_projection["compatible"] else selected_runtime["reason"]
        )
    elif matches or same_base_rows:
        status = "identity-mismatch"
        historical = matches[-1] if matches else same_base_rows[-1]
        reason = _identity_mismatch_reason(
            historical,
            expected_base_name=expected_base_name,
            expected_cwd=expected_cwd,
            expected_basis_id=expected_basis_id,
            expected_sha256=expected_sha256,
            expected_execution_topology=expected_execution_topology,
            required_publication_guard=required_publication_guard,
        )
    else:
        status = "missing"
        reason = f"no successful shelf found for expected base {expected_base_name}"
        historical = {}
    return {
        **historical,
        "logical_profile": profile,
        "physical_profile": str(historical.get("profile") or "") or None,
        "historical_build_status": "ok" if historical else "missing",
        "compatibility_status": status,
        "reason": reason,
        "manifest_compatibility": selected_projection,
        "runtime_compatibility": selected_runtime,
        "runtime_drift": selected_runtime.get("runtime_drift") if selected_runtime is not None else None,
        "rebuild_command": rebuild_command,
    }


def _resolved_profile_compatibility(root: Path, profile: str) -> dict:
    assignments = warmup_profile_assignments(WORKBENCH_BIN, profile)
    problem = profile_context_problem(profile, assignments)
    if problem is not None:
        return {
            "logical_profile": profile,
            "physical_profile": None,
            "historical_build_status": "not-evaluated",
            "compatibility_status": "project-context-missing",
            "reason": problem.reason,
            "next_action": problem.next_action,
            "rebuild_command": None,
        }
    expected_cwd = str(resolve_profile_cwd(assignments).path)
    return profile_compatibility(
        root,
        profile,
        expected_base_name=Path(assignments["PROFILE_BASE"]).name,
        expected_cwd=expected_cwd,
        expected_basis_id=assignments["PROFILE_BASIS_ID"],
        expected_sha256=assignments["PROFILE_SHA256"],
        expected_capacity=int(assignments.get("PROFILE_ORBSTACK_PUBLIC_CAPACITY") or 1),
        expected_execution_topology=assignments.get("PROFILE_EXECUTION_TOPOLOGY"),
    )


def _resolved_profile_row(root: Path, profile: str) -> dict:
    result = _resolved_profile_compatibility(root, profile)
    if result["compatibility_status"] == "compatible":
        return result
    next_action = result.get("next_action") or f"rebuild with `{result['rebuild_command']}`"
    raise SystemExit(f"no compatible CRIU shelf for profile {profile}: {result['reason']}; {next_action}")


def _latest_profile_root(root: Path, profile: str) -> Path:
    row = _resolved_profile_row(root, profile)
    return Path(str(row["run_root"])) / str(row["physical_profile"])


def _latest_stoppable_profile_root(root: Path, profile: str) -> Path:
    try:
        return _latest_profile_root(root, profile)
    except SystemExit:
        pass
    rows = _successful_rows(root)
    exact = [row for row in rows if row.get("profile") == profile]
    if exact:
        row = exact[-1]
        return Path(str(row["run_root"])) / str(row["profile"])
    assignments = warmup_profile_assignments(WORKBENCH_BIN, profile)
    expected_cwd = str(resolve_profile_cwd(assignments).path)
    expected_base = Path(assignments["PROFILE_BASE"]).name
    expected_sha256 = assignments["PROFILE_SHA256"]
    expected_execution_topology = assignments.get("PROFILE_EXECUTION_TOPOLOGY")
    aliases = [
        row
        for row in rows
        if Path(str(row.get("profile_base") or "")).name == expected_base
        and str(row.get("profile_cwd") or "") == expected_cwd
        and (not row.get("profile_sha256") or not expected_sha256 or row.get("profile_sha256") == expected_sha256)
        and _matches_expected_topology(row, expected_execution_topology)
    ]
    if not aliases:
        raise SystemExit(f"no CRIU shelf found to stop for profile {profile}")
    row = aliases[-1]
    return Path(str(row["run_root"])) / str(row["profile"])


def _stop_profile(root: Path, profile: str) -> int:
    # Stale embedded code must never be restored, but it must remain possible
    # to retire a live stale shelf during an ABI migration.
    profile_root = _latest_stoppable_profile_root(root, profile)
    pools = sorted((profile_root / "pool").glob("*"))
    if len(pools) != 1:
        raise SystemExit(f"expected one pool under {profile_root / 'pool'}, found {len(pools)}")
    return stop_pool_runtime(pools[0])


def _configuration_error_row(
    profile: str,
    exc: SystemExit | UbuntuRuntimeLayoutError,
    detail: str,
) -> dict:
    return {
        "logical_profile": profile,
        "physical_profile": None,
        "historical_build_status": "not-evaluated",
        "compatibility_status": "configuration-error",
        "reason": detail,
        "configuration_error": {
            "kind": "profile-configuration",
            "exception": type(exc).__name__,
            "exit_status": exc.code if isinstance(exc, SystemExit) else None,
            "detail": detail,
        },
        "next_action": "fix or remove this developer profile definition, then rerun status",
        "rebuild_command": None,
    }


def _status(root: Path, *, as_json: bool) -> int:
    selected: list[dict] = []
    for profile in known_profile_names():
        diagnostic = io.StringIO()
        try:
            with redirect_stderr(diagnostic):
                row = _resolved_profile_compatibility(root, profile)
        except (SystemExit, UbuntuRuntimeLayoutError) as exc:
            detail = diagnostic.getvalue().strip() or str(exc) or "profile configuration was rejected"
            row = _configuration_error_row(profile, exc, detail)
        if row.get("pool"):
            try:
                row["runtime_status"] = "live" if orbstack_criu_restore.pool_is_live(Path(row["pool"])) else "stopped"
            except orbstack_criu_restore.EXPECTED_RESTORE_ERRORS as exc:
                row["runtime_status"] = "unknown"
                row["runtime_reason"] = f"{type(exc).__name__}: {exc}"
        if row.get("run_root") and row.get("physical_profile"):
            profile_root = Path(str(row["run_root"])) / str(row["physical_profile"])
            try:
                row["effective_capacity"] = restored_shelf_capacity(profile_root)
            except (OSError, RuntimeError, ValueError) as exc:
                row["capacity_error"] = f"{type(exc).__name__}: {exc}"
            primary = read_shelf_owner(profile_root)
            owners = [primary] if primary else []
            owners.extend(read_shelf_owners(profile_root))
            owners = list({str(owner.get("attempt_id")): owner for owner in owners if owner.get("attempt_id")}.values())
            for owner in owners:
                owner["progress_signal"] = shelf_owner_progress(profile_root, owner)
                owner["scoped_cancel"] = owner_cancel_command(owner)
            if owners:
                row["active_owner"] = owners[0]
                row["active_owners"] = owners
            row["active_demands"] = read_shelf_demands(profile_root)
            row["queued_demands"] = max(0, len(row["active_demands"]) - len(owners))
            row["runtime_inventory"] = profile_runtime_inventory(profile_root)
            row["runtime_cache"] = runtime_cache_state(profile_root)
        row["historical_restore_seconds"] = row.get("restore_seconds")
        row["historical_eval_seconds"] = row.get("eval_seconds")
        selected.append(row)
    if as_json:
        print(
            json.dumps(
                {
                    "backend": "orbstack-criu",
                    "profiles": selected,
                    "memory": {
                        "profile_processes": summarize_profile_memory(selected),
                        "guest": guest_memory_summary(),
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    print("OrbStack CRIU warm-profile shelves")
    for row in selected:
        state = row["compatibility_status"]
        physical = row.get("physical_profile")
        alias = f" -> {physical}" if physical and physical != row["logical_profile"] else ""
        runtime = f"; runtime={row['runtime_status']}" if row.get("runtime_status") else ""
        print(f"- {row['logical_profile']}{alias}: {state}{runtime}")
        if state == "compatible":
            image_mib = int(row.get("image_bytes") or 0) / 1024**2
            print(
                f"  historical snapshot validation: restore={row.get('restore_seconds', '-')}s "
                f"eval={row.get('eval_seconds', '-')}s image={image_mib:.1f} MiB"
            )
            print(f"  root: {Path(row['run_root']) / str(row['physical_profile'])}")
            if row.get("effective_capacity"):
                print(
                    f"  capacity: current logical={row['effective_capacity']}; "
                    f"baseline={row.get('requested_capacity') or row.get('published_capacity') or '-'} "
                    f"(published={row.get('published_capacity') or '-'}, "
                    f"requested={row.get('requested_capacity') or '-'})"
                )
            elif row.get("capacity_error"):
                print(f"  capacity: unknown ({row['capacity_error']})")
            if row.get("runtime_drift"):
                print("  runtime: embedded build differs from checkout but remains ABI-compatible")
            inventory = row.get("runtime_inventory") or {}
            memory = inventory.get("memory") or {}
            cache = row.get("runtime_cache") or {}
            if inventory.get("process_count"):
                print(
                    f"  occupancy: {inventory.get('occupied_seats', 0)}/{inventory.get('logical_capacity', 0)} "
                    f"seat(s); children={inventory.get('active_child_count', 0)}; "
                    f"basis_parents={inventory.get('physical_basis_parents', 0)}"
                )
                print(
                    f"  memory: pss={int(memory.get('pss_kb') or 0) / 1024:.1f} MiB "
                    f"uss={int(memory.get('uss_kb') or 0) / 1024:.1f} MiB "
                    f"rss={int(memory.get('rss_kb') or 0) / 1024:.1f} MiB "
                    "(guest smaps; PSS is the attributable total)"
                )
            if cache.get("policy") != "not-yet-managed":
                retire_in = cache.get("retire_in_seconds")
                retire_text = f"{retire_in}s" if retire_in is not None else "-"
                print(
                    f"  cache: status={cache.get('runtime_status')}; idle={cache.get('idle_age_seconds')}s; "
                    f"retire_in={retire_text}; shelf={cache.get('shelf_status')}; "
                    f"data_preserved={str(cache.get('data_preserved')).lower()}"
                )
            for owner in row.get("active_owners") or ([row["active_owner"]] if row.get("active_owner") else []):
                progress = owner.get("progress_signal") or {}
                print(
                    f"  active: seat={owner.get('admission_slot') or '-'} attempt={owner.get('attempt_id')} "
                    f"source={owner.get('source')} "
                    f"elapsed={owner.get('elapsed_seconds')}s progress={progress_summary(progress)}"
                )
                print(f"  cancel: {owner.get('scoped_cancel')}")
        else:
            print(f"  reason: {row['reason']}")
            if row.get("next_action"):
                print(f"  next: {row['next_action']}")
            else:
                print(f"  rebuild: {row['rebuild_command']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hol-workbench/bin/orbstack-criu")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--json", action="store_true")
    build = subparsers.add_parser(
        "build",
        help="build warm-profile shelves",
        description="Build OrbStack/CRIU warm-profile shelves.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "A bare build targets exactly light,heavy.\n\n"
            "Maintainer policy: rebuild heavy and large/special profiles only when the machine is otherwise idle.\n"
            "During active parallel work, test rebuild machinery with CRIU_PROFILES=light only.\n\n"
            "Explicit opt-in example:\n"
            "  CRIU_PROFILES=flyspeck-geom hol-workbench/bin/orbstack-criu build\n\n"
            "List profile names with hol-workbench/bin/prove profiles."
        ),
    )
    subparsers.add_parser("restore", add_help=False)
    restore_profile = subparsers.add_parser("restore-profile")
    restore_profile.add_argument("profile")
    vanilla = subparsers.add_parser(
        "vanilla",
        help="run a source file as ordinary HOL from a restored warm profile",
    )
    vanilla.add_argument("profile")
    vanilla.add_argument("--source", required=True, type=Path)
    vanilla.add_argument("--timeout", type=float)
    vanilla.add_argument("--idle-timeout", type=float)
    vanilla.add_argument("--transcript", type=Path)
    vanilla.add_argument("--run-root", type=Path, default=Path("runs/warm-vanilla"))
    vanilla_smoke = subparsers.add_parser(
        "vanilla-smoke",
        help="probe selected warm-vanilla profiles without building cold shelves",
    )
    vanilla_smoke.add_argument("profiles", nargs="*", default=list(orbstack_criu_vanilla_readiness.DEFAULT_PROFILES))
    vanilla_smoke.add_argument("--run-root", type=Path, default=Path("runs/warm-vanilla-readiness"))
    vanilla_smoke.add_argument("--timeout", type=float, default=30.0)
    vanilla_smoke.add_argument("--json", action="store_true")
    vanilla_smoke.add_argument("--worker-spec", type=Path, help=argparse.SUPPRESS)
    vanilla_smoke.add_argument("--worker-result", type=Path, help=argparse.SUPPRESS)
    stop_profile = subparsers.add_parser("stop-profile")
    stop_profile.add_argument("profile")
    recover_stop_profile_root = subparsers.add_parser(
        "recover-stop-profile-root",
        help="recover and verified-stop one exact physical profile root",
    )
    recover_stop_profile_root.add_argument("profile_root", type=Path)
    cancel = subparsers.add_parser("cancel", help="interrupt one owning warm attempt without stopping its basis")
    cancel.add_argument("profile")
    cancel.add_argument("--attempt", required=True)
    args, rest = parser.parse_known_args(argv)
    runtime_commands = {"status", "restore-profile", "vanilla", "vanilla-smoke", "stop-profile", "cancel"}
    try:
        runtime_layout = resolve_ubuntu_runtime_layout() if args.command in runtime_commands else None
    except UbuntuRuntimeLayoutError as exc:
        print(f"orbstack-criu: {exc}", file=sys.stderr)
        return 2
    if args.command == "status":
        assert runtime_layout is not None
        return _status(runtime_layout.criu_shelf_root.path, as_json=args.json)
    if args.command == "build":
        if rest:
            build.error(f"unrecognized arguments: {' '.join(rest)}")
        return orbstack_criu_build.main()
    if args.command == "restore":
        return orbstack_criu_restore.main(rest)
    if args.command == "restore-profile":
        assert runtime_layout is not None
        root = runtime_layout.criu_shelf_root.path
        assignments = warmup_profile_assignments(WORKBENCH_BIN, args.profile)
        capacity = int(assignments.get("PROFILE_ORBSTACK_PUBLIC_CAPACITY") or 1)
        profile_root = _latest_profile_root(root, args.profile)
        restore_status = orbstack_criu_restore.main([str(profile_root)])
        if restore_status != 0:
            return restore_status
        ensure_profile_logical_capacity(profile_root, capacity)
        schedule_profile_retirement(profile_root, logical_profile=args.profile)
        return 0
    if args.command == "vanilla":
        assert runtime_layout is not None
        root = runtime_layout.criu_shelf_root.path
        profile_root = _latest_profile_root(root, args.profile)
        assignments = warmup_profile_assignments(WORKBENCH_BIN, args.profile)
        capacity = int(assignments.get("PROFILE_ORBSTACK_PUBLIC_CAPACITY") or 1)
        try:
            legacy_holdir_roots = tuple(
                Path(value) for value in json.loads(assignments.get("PROFILE_LEGACY_HOLDIR_ROOTS_JSON") or "[]")
            )
            logical_source_roots = tuple(
                dict(value) for value in json.loads(assignments.get("PROFILE_LOGICAL_SOURCE_ROOTS_JSON") or "[]")
            )
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            print(f"orbstack-criu: invalid profile source-root metadata: {exc}", file=sys.stderr)
            return 2
        result = orbstack_criu_vanilla.run(
            profile_root=profile_root,
            source=args.source,
            timeout=args.timeout,
            idle_timeout=args.idle_timeout,
            restore=lambda: orbstack_criu_restore.main([str(profile_root)]),
            transcript_output=args.transcript or default_transcript_path(args.run_root, args.source),
            logical_profile=args.profile,
            logical_capacity=capacity,
            profile_cwd=resolve_profile_cwd(assignments, holdir=runtime_layout.holdir).path,
            legacy_holdir_roots=legacy_holdir_roots,
            logical_source_root_declarations=logical_source_roots,
        )
        if (profile_root / "pool").is_dir():
            schedule_profile_retirement(profile_root, logical_profile=args.profile)
        return result
    if args.command == "vanilla-smoke":
        if args.worker_spec is not None or args.worker_result is not None:
            if args.worker_spec is None or args.worker_result is None:
                vanilla_smoke.error("--worker-spec and --worker-result must be supplied together")
            return orbstack_criu_vanilla_readiness_worker.run_worker(
                spec_path=args.worker_spec.expanduser().resolve(),
                result_path=args.worker_result.expanduser().resolve(),
            )
        assert runtime_layout is not None
        root = runtime_layout.criu_shelf_root.path
        status_code, report = orbstack_criu_vanilla_readiness.run(
            root=root,
            profiles=args.profiles,
            run_root=args.run_root.expanduser().resolve(),
            timeout=args.timeout,
            compatibility=lambda profile: _resolved_profile_compatibility(root, profile),
            recover_profile=orbstack_criu_vanilla_readiness_lifecycle.recover_and_stop_profile_root,
        )
        orbstack_criu_vanilla_readiness.print_report(report, as_json=args.json)
        return status_code
    if args.command == "stop-profile":
        assert runtime_layout is not None
        root = runtime_layout.criu_shelf_root.path
        return _stop_profile(root, args.profile)
    if args.command == "recover-stop-profile-root":
        receipt = orbstack_criu_vanilla_readiness_lifecycle.recover_and_stop_profile_root(
            args.profile_root.expanduser().resolve()
        )
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0 if receipt.get("status") == "stopped" else 1
    if args.command == "cancel":
        assert runtime_layout is not None
        root = runtime_layout.criu_shelf_root.path
        result = cancel_shelf_owner(_latest_profile_root(root, args.profile), args.attempt)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") == "interrupted" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
