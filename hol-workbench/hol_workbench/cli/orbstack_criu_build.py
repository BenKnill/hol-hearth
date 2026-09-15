#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
from argparse import Namespace
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hol_workbench.checkout_python import checkout_module_command
from hol_workbench.cli.orbstack_criu_profile_plan import (
    print_profile_build_preflight,
    profile_build_plan,
)
from hol_workbench.criu_invocation import criu_command
from hol_workbench.criu_loaded_capture import capture_loaded_provenance
from hol_workbench.criu_loaded_provenance import SemanticInput
from hol_workbench.criu_maintenance_preflight import (
    CriuMaintenancePreflightError,
    require_criu_maintenance_ready,
)
from hol_workbench.criu_pool_process_inventory import live_pool_role_pids
from hol_workbench.criu_pool_restore import restored_pool_lock
from hol_workbench.criu_publication_provenance import (
    committed_source_identity,
    image_inventory_record,
    post_restore_smoke_record,
    profile_recipe_record,
)
from hol_workbench.criu_shelf_capacity import configured_shelf_capacity
from hol_workbench.criu_shelf_publication import (
    build_publication_failures,
    publish_build_results,
)
from hol_workbench.criu_shelf_result_evidence import (
    EVAL_MARKERS,
    capture_eval_evidence,
    capture_new_restore_evidence,
)
from hol_workbench.criu_snapshot_compat import (
    snapshot_manifest_projection,
    validate_authoring_shelf_manifest,
    write_snapshot_manifest,
)
from hol_workbench.criu_snapshot_environment import (
    capture_snapshot_environment,
    snapshot_environment_sha256,
)
from hol_workbench.jsonio import durable_atomic_write_json, read_json
from hol_workbench.machine_client import repository_resource_root
from hol_workbench.proof_run_fork_broker_probe import run_broker_capture_request
from hol_workbench.restored_execution_topology import (
    RestoredExecutionTopology,
    fork_snapshot_abi_for_topology,
)
from hol_workbench.runtime_config import CriuExecutionMode
from hol_workbench.ubuntu_runtime_layout import UbuntuRuntimeLayoutError, resolve_ubuntu_runtime_layout

WORKBENCH_PKG = Path(__file__).resolve().parents[2]
WORKBENCH = repository_resource_root(WORKBENCH_PKG)
PROOF_RUN = WORKBENCH_PKG / "bin" / "proof-run"
BUDGET_BYTES = int(os.environ.get("CRIU_BUDGET_GB", "20")) * 1024**3
PUBLICATION_IDENTITY_GUARD_SCHEMA = "hol-workbench.criu-publication-identity-guard.v1"
PUBLICATION_GUARD_SHA256_FIELDS = ("profile_sha256",)
CAPTURED_PUBLICATION_GUARD_SHA256_FIELDS = (
    "loaded_strict_sha256",
    "ocaml_runtime_strict_sha256",
    "snapshot_environment_sha256",
)
DEFAULT_PRELOAD_FAILSAFE_SECONDS = 1200.0
PROFILE_PRELOAD_FAILSAFE_SECONDS = {
    "heavy": 7200.0,
    "probability": 10800.0,
}
COMMAND_TIMEOUT_PADDING_SECONDS = 300


def restore_log_may_be_root_owned(mode: CriuExecutionMode) -> bool:
    """Only sudo-launched CRIU can create controller evidence as root."""

    return mode is CriuExecutionMode.SUDO


def profile_build_timeouts(
    profiles: tuple[str, ...],
    *,
    environ: Mapping[str, str] | None = None,
) -> tuple[str, int]:
    active_environ = os.environ if environ is None else environ
    configured_preload = active_environ.get("CRIU_PRELOAD_TIMEOUT")
    if configured_preload is not None:
        preload_seconds = float(configured_preload)
    else:
        preload_seconds = max(
            DEFAULT_PRELOAD_FAILSAFE_SECONDS,
            *(PROFILE_PRELOAD_FAILSAFE_SECONDS.get(profile, DEFAULT_PRELOAD_FAILSAFE_SECONDS) for profile in profiles),
        )
    preload_timeout = str(int(preload_seconds)) if preload_seconds.is_integer() else str(preload_seconds)
    configured_command = active_environ.get("CRIU_COMMAND_TIMEOUT")
    command_timeout = (
        int(float(configured_command))
        if configured_command is not None
        else int(preload_seconds) + COMMAND_TIMEOUT_PADDING_SECONDS
    )
    return preload_timeout, command_timeout


def _publication_guard_config(profile: str) -> tuple[dict, dict, dict] | None:
    from hol_workbench.cli.prove_profiles import (
        developer_warmup_profile_publication_guard,
        load_developer_manifest,
    )

    profiles = load_developer_manifest(WORKBENCH_PKG / "bin").get("profiles")
    if not isinstance(profiles, dict):
        raise RuntimeError("warmup profile manifest has no profiles")
    entry = profiles.get(profile)
    if not isinstance(entry, dict):
        raise RuntimeError(f"warmup profile {profile!r} is missing")
    raw_guard = entry.get("publication_guard")
    if raw_guard is None:
        return None
    if not isinstance(raw_guard, dict):
        raise RuntimeError(f"profile {profile} publication_guard must be an object")
    reference_profile = raw_guard.get("reference_profile")
    if not isinstance(reference_profile, str) or not reference_profile:
        raise RuntimeError(f"profile {profile} publication_guard requires reference_profile")
    reference = profiles.get(reference_profile)
    if not isinstance(reference, dict):
        raise RuntimeError(f"profile {profile} publication_guard reference profile {reference_profile!r} is missing")
    guard = developer_warmup_profile_publication_guard(WORKBENCH_PKG / "bin", profile)
    if guard is None:
        raise RuntimeError(f"warmup profile {profile!r} lost its publication_guard during resolution")
    return entry, reference, guard


def publication_guard_preflight(
    profile: str,
    assignments: dict[str, str],
    *,
    extra_preloads: list[str],
    workbench_source: dict[str, Any],
) -> dict | None:
    """Compare guarded build inputs before starting the expensive profile load."""

    configured = _publication_guard_config(profile)
    if configured is None:
        return None
    entry, reference, guard = configured
    reference_profile = str(guard["reference_profile"])
    reference_assignments = profile_assignments(reference_profile)
    required_extra_preloads = guard.get("required_extra_preloads")
    require_publication_provenance = guard.get("require_publication_provenance", False)
    failures: list[str] = []
    if not isinstance(required_extra_preloads, list) or not all(
        isinstance(item, str) for item in required_extra_preloads
    ):
        failures.append("required_extra_preloads must be a list of strings")
        required_extra_preloads = []
    if not isinstance(require_publication_provenance, bool):
        failures.append("require_publication_provenance must be a Boolean")
        require_publication_provenance = False
    if require_publication_provenance and workbench_source.get("clean") is not True:
        failures.append("committed Workbench source identity is not clean")

    for key in PUBLICATION_GUARD_SHA256_FIELDS:
        value = guard.get(key)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            failures.append(f"{key} must be a lowercase SHA-256")

    if reference_profile == profile:
        if entry.get("alias_of"):
            failures.append("a self-guarded canonical profile must not be an alias")
    elif entry.get("alias_of") != reference_profile:
        failures.append(f"alias_of must be {reference_profile}")
    for key in ("basis_id", "cwd", "cwd_policy", "base_lines", "useful_operations"):
        if entry.get(key) != reference.get(key):
            failures.append(f"{key} differs from reference profile {reference_profile}")
    for key in (
        "PROFILE_BASIS_ID",
        "PROFILE_SHA256",
        "PROFILE_BASE",
        "PROFILE_CWD",
        "PROFILE_CWD_DECLARED",
        "PROFILE_CWD_POLICY",
    ):
        if assignments.get(key) != reference_assignments.get(key):
            failures.append(f"{key} differs from expanded reference profile {reference_profile}")
    if assignments.get("PROFILE_EXECUTION_TOPOLOGY") != guard.get("execution_topology"):
        failures.append("execution topology differs from publication guard")
    if assignments.get("PROFILE_RECOMMENDED_POOL_SIZE") != str(guard.get("recommended_pool_size")):
        failures.append("recommended pool size differs from publication guard")
    if assignments.get("PROFILE_ORBSTACK_PUBLIC_CAPACITY") != str(guard.get("orbstack_public_capacity")):
        failures.append("public capacity differs from publication guard")
    if assignments.get("PROFILE_SHA256") != guard.get("profile_sha256"):
        failures.append("expanded profile SHA-256 differs from publication guard")
    if extra_preloads != required_extra_preloads:
        failures.append(
            "extra preloads differ from publication guard: "
            f"expected {required_extra_preloads!r}, observed {extra_preloads!r}"
        )

    return {
        "schema": PUBLICATION_IDENTITY_GUARD_SCHEMA,
        "phase": "pre_pool_start",
        "status": "passed" if not failures else "failed",
        "profile": profile,
        "reference_profile": reference_profile,
        "expected": {
            "basis_id": reference_assignments.get("PROFILE_BASIS_ID"),
            "profile_sha256": guard.get("profile_sha256"),
            "profile_base": reference_assignments.get("PROFILE_BASE"),
            "profile_cwd": reference_assignments.get("PROFILE_CWD"),
            "execution_topology": guard.get("execution_topology"),
            "recommended_pool_size": guard.get("recommended_pool_size"),
            "orbstack_public_capacity": guard.get("orbstack_public_capacity"),
            "extra_preloads": required_extra_preloads,
            "require_publication_provenance": require_publication_provenance,
        },
        "observed": {
            "basis_id": assignments.get("PROFILE_BASIS_ID"),
            "profile_sha256": assignments.get("PROFILE_SHA256"),
            "profile_base": assignments.get("PROFILE_BASE"),
            "profile_cwd": assignments.get("PROFILE_CWD"),
            "execution_topology": assignments.get("PROFILE_EXECUTION_TOPOLOGY"),
            "recommended_pool_size": assignments.get("PROFILE_RECOMMENDED_POOL_SIZE"),
            "orbstack_public_capacity": assignments.get("PROFILE_ORBSTACK_PUBLIC_CAPACITY"),
            "extra_preloads": extra_preloads,
            "workbench_source": workbench_source,
        },
        "failures": failures,
    }


def publication_guard_capture(preflight: dict | None, item: dict) -> dict | None:
    """Validate captured semantic identities before CRIU dump and publication."""

    if preflight is None:
        return None
    raw_provenance = item.get("provenance_capture")
    provenance = raw_provenance if isinstance(raw_provenance, dict) else {}
    environment = item.get("snapshot_environment")
    observed = {
        "loaded_strict_sha256": provenance.get("loaded_strict_sha256"),
        "ocaml_runtime_strict_sha256": provenance.get("runtime_strict_sha256"),
        "snapshot_environment_sha256": (
            snapshot_environment_sha256(environment) if isinstance(environment, dict) else None
        ),
    }
    failures = [
        f"{key} must be a captured lowercase SHA-256, observed {observed.get(key) or 'missing'}"
        for key in CAPTURED_PUBLICATION_GUARD_SHA256_FIELDS
        if not isinstance(observed.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", observed[key])
    ]
    return {
        "schema": PUBLICATION_IDENTITY_GUARD_SCHEMA,
        "phase": "pre_criu_dump",
        "status": "passed" if not failures else "failed",
        "profile": preflight.get("profile"),
        "reference_profile": preflight.get("reference_profile"),
        "expected": {},
        "observed": observed,
        "failures": failures,
    }


def extra_preloads(profile: str | None = None) -> list[str]:
    raw = os.environ.get("CRIU_EXTRA_PRELOADS", "").strip()
    if not raw:
        if profile in {"s2n-arm", "s2n-arm-light", "s2n-arm-mlkem"}:
            return []
        return [str(WORKBENCH_PKG / "profile-support" / "compact_after_preload.ml")]
    paths = []
    for item in raw.split(","):
        text = item.strip()
        if not text:
            continue
        path = Path(text).expanduser()
        if not path.is_absolute():
            path = WORKBENCH / path
        paths.append(str(Path(os.path.abspath(path))))
    return paths


def extra_preload_failure_lines(paths: list[str]) -> list[str]:
    missing = [path for path in paths if not Path(path).is_file()]
    if not missing:
        return []
    return [
        "OrbStack CRIU build result:",
        "STATUS: refused_missing_extra_preload",
        *(f"FAILURE: extra preload source not found: {path}" for path in missing),
        "NEXT: install/package the default resource or correct CRIU_EXTRA_PRELOADS, then rerun the build",
    ]


def utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def allocate_build_root(
    shelf_parent: Path,
    *,
    stamp: str | None = None,
    token_factory: Callable[[], str] = lambda: secrets.token_hex(4),
    attempts: int = 100,
) -> Path:
    """Atomically allocate one sortable root; never join an existing build."""
    shelf_parent.mkdir(parents=True, exist_ok=True)
    base = shelf_parent / f"criu-warm-profiles-{stamp or utc_stamp()}"
    for index in range(attempts):
        candidate = base if index == 0 else Path(f"{base}-{os.getpid()}-{token_factory()}")
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(
        f"could not allocate an exclusive CRIU build root under {shelf_parent} after {attempts} attempts"
    )


def run(
    argv: list[str],
    *,
    cwd: Path = WORKBENCH,
    log: Path,
    timeout: int,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8", errors="replace") as out:
        out.write("$ " + " ".join(argv) + "\n\n")
        out.flush()
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, argv)
    return proc


def du_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    proc = subprocess.run(["du", "-sb", str(path)], text=True, stdout=subprocess.PIPE, check=True)
    return int(proc.stdout.split()[0])


def pss_kb(pid: int) -> int:
    total = 0
    smaps = Path(f"/proc/{pid}/smaps_rollup")
    if not smaps.exists():
        return 0
    for line in smaps.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("Pss:"):
            total += int(line.split()[1])
    return total


def profile_assignments(profile: str) -> dict[str, str]:
    from hol_workbench.cli.prove_profiles import developer_warmup_profile_assignments

    return developer_warmup_profile_assignments(WORKBENCH_PKG / "bin", profile)


def parse_pool(log: Path) -> str:
    text = log.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^PARALLEL_SAFE_WARM_POOL=(.+)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def find_pool_pids(pool: str) -> dict[str, int]:
    return live_pool_role_pids(Path(pool))


def criu_dump_command(pid: int, image_dir: Path) -> list[str]:
    """Disable CRIU's pagemap cache; it can assert on large aarch64 OCaml heaps."""
    return criu_command(
        "dump",
        "--no-default-config",
        "-t",
        str(pid),
        "-D",
        str(image_dir),
        "-v4",
        "-o",
        "dump.log",
        environment=("CRIU_PMC_OFF=1",),
    )


def criu_check_command() -> list[str]:
    return criu_command("check", "--all")


def restore_internal_pool(pool: Path, image_dir: Path) -> int:
    """Run the public fail-closed restore transaction for a freshly dumped pool."""

    from hol_workbench.cli.orbstack_criu_restore import restore_pool_locked

    args = Namespace(debug_criu=True, criu_option=[], lazy_pages=False)
    with restored_pool_lock(pool):
        return restore_pool_locked(args, image_dir=image_dir, pool=pool)


def write_smoke_source(root: Path, profile: str) -> tuple[Path, dict[str, Any]]:
    from hol_workbench.cli.prove_profiles import (
        developer_profile_operation_smoke_lines,
        developer_profile_post_restore_sentinels,
    )

    source = root / "criu_smoke.ml"
    sentinels = developer_profile_post_restore_sentinels(WORKBENCH_PKG / "bin", profile)
    if sentinels:
        lines = []
        for sentinel in sentinels:
            lines.append(sentinel["smoke"])
            lines.append(f"let _ = print_endline {json.dumps(sentinel['marker'])};;")
        required_markers = ["status: proved", *(sentinel["marker"] for sentinel in sentinels)]
    else:
        lines = [
            "let CRIU_SMOKE = prove(`1 + 1 = 2`,ARITH_TAC);;",
            *developer_profile_operation_smoke_lines(WORKBENCH_PKG / "bin", profile),
        ]
        required_markers = list(EVAL_MARKERS)
    source.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return source, {
        "schema": "hol-workbench.post-restore-smoke-contract.v1",
        "profile": profile,
        "sentinels": sentinels
        or [
            {
                "capability": "generic_criu_evaluation",
                "binding": "CRIU_SMOKE",
                "smoke": lines[0],
                "marker": "CRIU_SMOKE",
            }
        ],
        "required_markers": required_markers,
    }


def pool_stop_is_verified(pool: Path) -> bool:
    try:
        data = json.loads((pool / "pool.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    sessions = [item for item in data.get("sessions") or [] if isinstance(item, dict)]
    return (
        data.get("status") == "stopped"
        and bool(sessions)
        and all(
            item.get("status") == "stopped" and (item.get("stop_result") or {}).get("verified_quiescent") is True
            for item in sessions
        )
    )


def stop_dumped_pool(pool: Path, logs: Path) -> tuple[int, bool, float]:
    """Publish the verified stopped precondition required by restore."""

    started = time.monotonic()
    stopped = run(
        checkout_module_command("hol_workbench.criu_build_pool", "stop", "--pool", str(pool)),
        log=logs / "post-dump-pool-stop.log",
        timeout=60,
    )
    verified = stopped.returncode == 0 and pool_stop_is_verified(pool)
    return stopped.returncode, verified, round(time.monotonic() - started, 3)


def capture_profile_provenance(
    item: dict,
    *,
    profile_root: Path,
    pool: str,
    holdir: Path,
    profile_base: str,
    profile_cwd: str,
    extra_preloads: list[str],
    ocaml_pid: int,
    execution_topology: RestoredExecutionTopology = RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3,
) -> None:
    semantic_inputs = [SemanticInput(Path(profile_base), "profile_base")]
    if Path(profile_cwd).resolve() != holdir:
        semantic_inputs.append(SemanticInput(Path(profile_cwd), "profile_cwd"))
    semantic_inputs.extend(SemanticInput(Path(path), "extra_preload") for path in extra_preloads)
    started = time.monotonic()
    if execution_topology is not RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3:
        raise RuntimeError("profile capture requires the v3 mechanical broker")
    capture = capture_loaded_provenance(
        pool=Path(pool),
        output_dir=profile_root / "provenance",
        holdir=holdir,
        semantic_inputs=semantic_inputs,
        ocaml_pid=ocaml_pid,
        send=run_broker_capture_request,
    )
    loaded = capture["loaded_closure"]
    runtime = capture["runtime_closure"]
    item["provenance_capture"] = {
        "path": capture["path"],
        "status": capture["status"],
        "admission_authority": capture["admission_authority"],
        "loaded_record_count": loaded["record_count"],
        "loaded_unique_record_count": loaded["unique_record_count"],
        "loaded_strict_sha256": loaded["strict_sha256"],
        "runtime_strict_sha256": runtime["strict_sha256"],
        "child_quiescent": capture["capture"]["child_quiescent"],
    }
    item["snapshot_environment"] = capture_snapshot_environment(runtime, target_pid=ocaml_pid)
    item["provenance_capture_seconds"] = round(time.monotonic() - started, 3)


def finalize_profile_after_stop(
    item: dict,
    *,
    profile: str,
    profile_root: Path,
    pool: str,
    logs: Path,
) -> None:
    stop_status = None
    stop_error = None
    if pool:
        try:
            stop_proc = run(
                checkout_module_command("hol_workbench.criu_build_pool", "stop", "--pool", pool),
                log=logs / "pool-stop.log",
                timeout=60,
            )
            stop_status = stop_proc.returncode
        except Exception as exc:
            stop_error = repr(exc)
    item["stop_status"] = stop_status
    item["stop_verified_quiescent"] = stop_status == 0 and bool(pool) and pool_stop_is_verified(Path(pool))
    if stop_error:
        item["stop_exception"] = stop_error
    if item.get("status") != "ok":
        return
    if not item["stop_verified_quiescent"]:
        item["status"] = "stop_failed"
        return
    try:
        manifest_path = write_snapshot_manifest(profile_root, profile=profile, metadata=item)
        manifest = validate_authoring_shelf_manifest(profile_root)
        item.update(snapshot_manifest_projection(manifest))
        item["snapshot_manifest"] = str(manifest_path)
    except Exception as exc:
        item["status"] = "exception"
        item["exception"] = repr(exc)


def main() -> int:
    try:
        runtime_layout = resolve_ubuntu_runtime_layout()
    except UbuntuRuntimeLayoutError as exc:
        print(f"orbstack-criu build: {exc}", file=sys.stderr)
        return 2
    holdir = runtime_layout.holdir.path
    shelf_parent = runtime_layout.criu_shelf_root.path
    plan = profile_build_plan()
    print_profile_build_preflight(plan, out=sys.stdout)
    extra_by_profile = {profile: extra_preloads(profile) for profile in plan.profiles}
    preload_failures = [
        line for profile in plan.profiles for line in extra_preload_failure_lines(extra_by_profile[profile])
    ]
    if preload_failures:
        print("\n".join(preload_failures), file=sys.stderr)
        return 78
    try:
        criu_readiness = require_criu_maintenance_ready()
    except CriuMaintenancePreflightError as exc:
        print(
            f"orbstack-criu build: CRIU maintenance preflight failed before shelf allocation or HOL startup: {exc}",
            file=sys.stderr,
        )
        return 77
    try:
        workbench_source = committed_source_identity(WORKBENCH)
    except RuntimeError as exc:
        print(f"orbstack-criu build: committed Workbench source preflight failed: {exc}", file=sys.stderr)
        return 76
    print(f"criu_maintenance_preflight={criu_readiness.json()}")
    print(f"workbench_revision={workbench_source['revision']}")
    root = allocate_build_root(shelf_parent)
    rows: list[dict] = []
    total_images = 0
    preload_timeout, command_timeout = profile_build_timeouts(plan.profiles)
    print(f"preload_timeout_seconds={preload_timeout}")
    print(f"command_timeout_seconds={command_timeout}")
    check_log = root / "criu-check-all.log"
    check = run(criu_check_command(), log=check_log, timeout=60)

    for profile in plan.profiles:
        if total_images >= BUDGET_BYTES:
            break
        extra = extra_by_profile[profile]
        item: dict = {
            "profile": profile,
            "started_utc": utc_now(),
            "ubuntu_runtime_layout": runtime_layout.record(),
            "workbench_source": workbench_source,
        }
        profile_root = root / profile
        logs = profile_root / "logs"
        pool_root = profile_root / "pool"
        replay_root = profile_root / "replays"
        image_dir = profile_root / "criu-image"
        for path in (logs, pool_root, replay_root, image_dir):
            path.mkdir(parents=True, exist_ok=True)
        source, smoke_contract = write_smoke_source(profile_root, profile)
        item["smoke_contract"] = smoke_contract
        pool = ""
        try:
            assignments = profile_assignments(profile)
            if (
                assignments.get("PROFILE_PROJECT_MANIFEST")
                and assignments.get("PROFILE_PROJECT_REPOSITORY_CLEAN") != "true"
            ):
                raise RuntimeError(
                    "project warm profile build requires a clean checkout containing the manifest base commit"
                )
            guard_preflight = publication_guard_preflight(
                profile,
                assignments,
                extra_preloads=extra,
                workbench_source=workbench_source,
            )
            if guard_preflight is not None:
                item["publication_identity_guard"] = {"preflight": guard_preflight}
                if guard_preflight["status"] != "passed":
                    raise RuntimeError(
                        "publication identity guard rejected build inputs: " + "; ".join(guard_preflight["failures"])
                    )
            topology = RestoredExecutionTopology(assignments["PROFILE_EXECUTION_TOPOLOGY"])
            item["execution_topology"] = topology.value
            item["fork_snapshot_abi"] = fork_snapshot_abi_for_topology(topology)
            public_capacity = configured_shelf_capacity(assignments)
            base = assignments["PROFILE_BASE"]
            profile_cwd = assignments.get("PROFILE_CWD") or str(holdir)
            output_profile = assignments.get("PROFILE_OUTPUT_PROFILE") or "ringtheory"
            item["profile_base"] = base
            item["profile_sha256"] = assignments["PROFILE_SHA256"]
            item["profile_basis_id"] = assignments["PROFILE_BASIS_ID"]
            item["profile_recipe"] = profile_recipe_record(
                Path(base),
                basis_id=assignments["PROFILE_BASIS_ID"],
                expected_sha256=assignments["PROFILE_SHA256"],
            )
            item["profile_cwd"] = profile_cwd
            item["output_profile"] = output_profile
            item["orbstack_public_capacity"] = public_capacity
            item["extra_preloads"] = extra
            preload_args = ["--preload", base]
            for extra_preload in extra:
                preload_args.extend(["--preload", extra_preload])
            start = time.monotonic()
            start_proc = run(
                checkout_module_command(
                    "hol_workbench.criu_build_pool",
                    "start",
                    "--size",
                    str(public_capacity),
                    "--label",
                    f"criu-{profile}",
                    "--holdir",
                    str(holdir),
                    "--cwd",
                    profile_cwd,
                    "--pool-root",
                    str(pool_root),
                    "--startup-timeout",
                    preload_timeout,
                    *preload_args,
                    "--preload-timeout",
                    preload_timeout,
                    "--profile-basis-id",
                    assignments["PROFILE_BASIS_ID"],
                ),
                log=logs / "pool-start.log",
                timeout=command_timeout,
            )
            item["pool_start_status"] = start_proc.returncode
            item["pool_start_seconds"] = round(time.monotonic() - start, 3)
            pool = parse_pool(logs / "pool-start.log")
            item["pool"] = pool
            pool_document = read_json(Path(pool) / "pool.json") if pool else {}
            item["broker_protocol"] = pool_document.get("broker_protocol")
            item["broker_runtime_sha256"] = pool_document.get("broker_runtime_sha256")
            item["broker_runtime_bundle"] = pool_document.get("broker_runtime_bundle")
            pids = find_pool_pids(pool) if pool else {"manager": 0, "ocaml": 0}
            item["manager_pid"] = pids["manager"]
            item["ocaml_pid"] = pids["ocaml"]
            item["manager_pss_kb"] = pss_kb(pids["manager"]) if pids["manager"] else 0
            item["ocaml_pss_kb"] = pss_kb(pids["ocaml"]) if pids["ocaml"] else 0
            if start_proc.returncode != 0 or not pids["manager"] or not pids["ocaml"]:
                item["status"] = "pool_start_failed"
                continue

            capture_profile_provenance(
                item,
                profile_root=profile_root,
                pool=pool,
                holdir=holdir,
                profile_base=base,
                profile_cwd=profile_cwd,
                extra_preloads=extra,
                ocaml_pid=pids["ocaml"],
                execution_topology=topology,
            )
            guard_capture = publication_guard_capture(guard_preflight, item)
            if guard_capture is not None:
                item["publication_identity_guard"]["capture"] = guard_capture
                if guard_capture["status"] != "passed":
                    raise RuntimeError(
                        "publication identity guard rejected captured runtime: " + "; ".join(guard_capture["failures"])
                    )

            dump_start = time.monotonic()
            dump = run(
                criu_dump_command(pids["manager"], image_dir),
                log=logs / "criu-dump-wrapper.log",
                timeout=900,
            )
            item["dump_environment"] = {"CRIU_PMC_OFF": "1"}
            item["dump_status"] = dump.returncode
            item["dump_seconds"] = round(time.monotonic() - dump_start, 3)
            item["image_bytes"] = du_bytes(image_dir)
            inventory_start = time.monotonic()
            item["image_inventory"] = image_inventory_record(image_dir)
            item["image_inventory_seconds"] = round(time.monotonic() - inventory_start, 3)
            total_images += item["image_bytes"]
            item["total_image_bytes_after"] = total_images
            if dump.returncode != 0:
                item["status"] = "dump_failed"
                break

            (
                item["post_dump_stop_status"],
                item["post_dump_stop_verified_quiescent"],
                item["post_dump_stop_seconds"],
            ) = stop_dumped_pool(Path(pool), logs)
            if not item["post_dump_stop_verified_quiescent"]:
                item["status"] = "post_dump_stop_failed"
                break

            restore_logs_before = {path.resolve() for path in image_dir.glob("restore-*.log")}
            restore_start = time.monotonic()
            item["restore_status"] = restore_internal_pool(Path(pool), image_dir)
            item["restore_seconds"] = round(time.monotonic() - restore_start, 3)
            if item["restore_status"] != 0:
                item["status"] = "restore_failed"
                break

            item["restored_identity_transaction"] = "ok"
            try:
                item["restore_evidence"] = capture_new_restore_evidence(
                    image_dir,
                    previous_logs=restore_logs_before,
                    publish_root_owned=restore_log_may_be_root_owned(criu_readiness.mode),
                )
            except RuntimeError as exc:
                item["status"] = "restore_evidence_failed"
                item["exception"] = repr(exc)
                break
            time.sleep(1)
            restored = find_pool_pids(pool)
            item["restored_manager_pid"] = restored["manager"]
            item["restored_ocaml_pid"] = restored["ocaml"]
            item["restored_ocaml_pss_kb"] = pss_kb(restored["ocaml"]) if restored["ocaml"] else 0

            eval_start = time.monotonic()
            if topology is RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3:
                restored_pool = read_json(Path(pool) / "pool.json")
                sessions = restored_pool.get("sessions") or []
                if not sessions or not isinstance(sessions[0], dict):
                    raise RuntimeError("restored broker pool has no session")
                broker_receipt = logs / "post-restore-broker-probe.json"
                eval_proc = run(
                    checkout_module_command(
                        "hol_workbench.proof_run_fork_broker_probe",
                        "--session",
                        str(sessions[0]["session_dir"]),
                        "--source",
                        str(source),
                        "--transcript",
                        str(logs / "post-restore-broker-probe.raw"),
                        "--receipt",
                        str(broker_receipt),
                    ),
                    log=logs / "post-restore-eval.log",
                    timeout=120,
                )
                item["broker_acceptance_probe"] = read_json(broker_receipt) if broker_receipt.is_file() else None
            else:
                eval_proc = run(
                    [
                        str(PROOF_RUN),
                        "warm",
                        "pool",
                        "eval",
                        "--pool",
                        pool,
                        "--source",
                        str(source),
                        "--all",
                        "--timeout",
                        "60",
                        "--replay-run-root",
                        str(replay_root),
                    ],
                    log=logs / "post-restore-eval.log",
                    timeout=120,
                )
            item["eval_status"] = eval_proc.returncode
            item["eval_seconds"] = round(time.monotonic() - eval_start, 3)
            item["eval_evidence"] = capture_eval_evidence(
                logs / "post-restore-eval.log",
                exit_status=eval_proc.returncode,
                required_markers=tuple(str(marker) for marker in smoke_contract["required_markers"]),
            )
            item["eval_proved"] = item["eval_evidence"]["proved"]
            item["status"] = "ok" if item["eval_proved"] else "eval_failed"
            if item["status"] != "ok":
                break
            broker_probe = item.get("broker_acceptance_probe")
            if not isinstance(broker_probe, dict):
                raise RuntimeError("post-restore smoke has no broker acceptance receipt")
            item["post_restore_smoke"] = post_restore_smoke_record(
                profile_root,
                source=source,
                contract=smoke_contract,
                eval_evidence=item["eval_evidence"],
                broker_probe=broker_probe,
            )
        except Exception as exc:
            item["status"] = "exception"
            item["exception"] = repr(exc)
            break
        finally:
            finalize_profile_after_stop(
                item,
                profile=profile,
                profile_root=profile_root,
                pool=pool,
                logs=logs,
            )
            guard_record = item.get("publication_identity_guard")
            if isinstance(guard_record, dict):
                guard_record["cleanup"] = {
                    "pool_started": bool(pool),
                    "verified_quiescent": item.get("stop_verified_quiescent") is True if pool else True,
                    "reason": (
                        "stopped pool was verified quiescent"
                        if pool
                        else "publication guard refused before any pool process started"
                    ),
                }
            rows.append(item)
        if item.get("status") != "ok":
            break

    publication_failures = build_publication_failures(root, rows, plan.profiles)
    publication_gate = "passed" if not publication_failures else "failed"
    report = root / "proof-receipt.md"
    lines = [
        "# CRIU Warm Profile Probe",
        "",
        f"- run_root: `{root}`",
        f"- budget_bytes: `{BUDGET_BYTES}`",
        f"- extra_preloads_by_profile: `{extra_by_profile}`",
        f"- criu_check_status: `{check.returncode}`",
        f"- criu_check_log: `{check_log}`",
        f"- publication_gate: `{publication_gate}`",
        f"- publication_marker: `{root / 'results.json'}`",
        "",
        "| profile | status | start | dump | restore | eval | image | ocaml PSS before/after | pool |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        image_mib = row.get("image_bytes", 0) / 1024**2
        pss = f"{row.get('ocaml_pss_kb', 0) / 1024:.1f}/{row.get('restored_ocaml_pss_kb', 0) / 1024:.1f} MiB"
        lines.append(
            "| `{profile}` | `{status}` | {start}s | {dump}s | {restore}s | {eval}s | {image:.1f} MiB | {pss} | `{pool}` |".format(
                profile=row.get("profile", ""),
                status=row.get("status", ""),
                start=row.get("pool_start_seconds", ""),
                dump=row.get("dump_seconds", ""),
                restore=row.get("restore_seconds", ""),
                eval=row.get("eval_seconds", ""),
                image=image_mib,
                pss=pss,
                pool=row.get("pool", ""),
            )
        )
    lines.extend(["", "## Snapshot identities", ""])
    for row in rows:
        profile = row.get("profile", "")
        restore_evidence = row.get("restore_evidence") or {}
        eval_evidence = row.get("eval_evidence") or {}
        lines.extend(
            [
                f"- `{profile}` loaded_closure_sha256: `{row.get('loaded_strict_sha256') or '-'}`",
                f"- `{profile}` ocaml_runtime_sha256: `{row.get('ocaml_runtime_strict_sha256') or '-'}`",
                f"- `{profile}` diagnostic_environment_sha256: `{row.get('snapshot_environment_sha256') or '-'}`",
                f"- `{profile}` workbench_revision: `{row.get('workbench_revision') or '-'}`",
                f"- `{profile}` workbench_tree: `{row.get('workbench_tree') or '-'}`",
                f"- `{profile}` profile_recipe_sha256: `{row.get('profile_recipe_sha256') or '-'}`",
                f"- `{profile}` image_inventory_sha256: `{row.get('image_inventory_sha256') or '-'}`",
                f"- `{profile}` image_inventory_bytes: `{row.get('image_inventory_bytes') or '-'}`",
                f"- `{profile}` post_restore_smoke_sha256: `{row.get('post_restore_smoke_sha256') or '-'}`",
                f"- `{profile}` restore_log: `{restore_evidence.get('path') or '-'}`",
                f"- `{profile}` restore_log_sha256: `{restore_evidence.get('sha256') or '-'}`",
                f"- `{profile}` eval_log: `{eval_evidence.get('path') or '-'}`",
                f"- `{profile}` eval_log_sha256: `{eval_evidence.get('sha256') or '-'}`",
                f"- `{profile}` snapshot_manifest: `{row.get('snapshot_manifest') or '-'}`",
            ]
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- CRIU images are development accelerators, not final proof witnesses.",
            "- `criu check --all` may warn about optional kernel features; the post-restore eval is the practical pass/fail gate here.",
            "- The pool is stopped after each probe so the image is the durable artifact being tested.",
            "- The build remains undiscoverable until the complete requested batch is revalidated and `results.json` is durably replaced.",
        ]
    )
    if publication_failures:
        lines.extend(["", "## Publication failures", "", *[f"- {failure}" for failure in publication_failures]])
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    durable_atomic_write_json(
        root / "build-results.json",
        {
            "schema": "hol-workbench.criu-build-results.v1",
            "status": "publication_ready" if not publication_failures else "unpublished",
            "requested_profiles": list(plan.profiles),
            "publication_failures": publication_failures,
            "rows": rows,
        },
    )
    if not publication_failures:
        publish_build_results(root, rows, plan.profiles)
    print(report)
    print(root)
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0 if not publication_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
