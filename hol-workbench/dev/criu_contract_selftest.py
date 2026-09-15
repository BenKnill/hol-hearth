#!/usr/bin/env python3
"""Pure executable contracts for retained CRIU admission and cleanup policy."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

WORKBENCH_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKBENCH_ROOT))

from hol_workbench.criu_lazy_pages_owner import (  # noqa: E402
    bind_daemon_identity,
    owner_record,
    terminate_owned_daemon,
)
from hol_workbench.criu_loaded_provenance import (  # noqa: E402
    LoadedProvenanceError,
    LoaderRecord,
    parse_loader_export,
    resolve_loaded_closure,
    validate_loaded_closure,
)
from hol_workbench.criu_pool_process_inventory import (  # noqa: E402
    command_pool_dir,
    pool_process_inventory,
)
from hol_workbench.criu_restore_residue import prune_successful_restore_residue  # noqa: E402
from hol_workbench.criu_restore_transaction import validate_restored_tree  # noqa: E402
from hol_workbench.criu_snapshot_admission import (  # noqa: E402
    BINDING_SCOPED_REQUIRED_CAPABILITIES,
    SnapshotAdmissionMode,
    SnapshotAdmissionRequest,
    StaticSnapshotAdmissionDecision,
    canonical_sha256,
)
from hol_workbench.criu_snapshot_environment import (  # noqa: E402
    DIAGNOSTIC_COMPATIBILITY_FIELDS,
    INTEGRITY_ONLY_FIELDS,
    SNAPSHOT_ENVIRONMENT_SCHEMA,
    STRICT_COMPATIBILITY_FIELDS,
    snapshot_compatibility_policy,
    snapshot_environment_failures,
)
from hol_workbench.fork_broker_protocol import BROKER_DESCRIBE_SCHEMA, BROKER_PROTOCOL  # noqa: E402
from hol_workbench.proof_run_fork_broker_client import broker_description_problem  # noqa: E402
from hol_workbench.restored_execution_topology import RestoredExecutionTopology  # noqa: E402


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def require_raises(
    error_type: type[BaseException],
    detail: str,
    action: Callable[[], object],
) -> None:
    try:
        action()
    except error_type as exc:
        require(detail in str(exc), f"unexpected {error_type.__name__}: {exc}")
    else:
        raise AssertionError(f"expected {error_type.__name__}: {detail}")


def rehash(record: dict[str, Any], digest_field: str) -> None:
    record[digest_field] = canonical_sha256({key: value for key, value in record.items() if key != digest_field})


def test_environment_policy() -> None:
    policy = snapshot_compatibility_policy()
    strict = set(STRICT_COMPATIBILITY_FIELDS)
    diagnostic = set(DIAGNOSTIC_COMPATIBILITY_FIELDS)
    integrity = set(INTEGRITY_ONLY_FIELDS)
    require(policy["strict"] == list(STRICT_COMPATIBILITY_FIELDS), "strict compatibility policy changed")
    require(policy["diagnostic"] == list(DIAGNOSTIC_COMPATIBILITY_FIELDS), "diagnostic policy changed")
    require(policy["integrity_only"] == list(INTEGRITY_ONLY_FIELDS), "integrity-only policy changed")
    require(not strict & diagnostic and not strict & integrity and not diagnostic & integrity, "policy fields overlap")
    require(
        policy["diagnostic_drift_effect"] == "does_not_invalidate_shelf",
        "diagnostic version drift became shelf invalidation",
    )

    environment = {
        "schema": SNAPSHOT_ENVIRONMENT_SCHEMA,
        "capture_context": {
            "same_process_domain": True,
            "target_runtime_path": "/opt/ocaml/bin/ocamlrun",
        },
        "criu": {"version": "Version: 4.2"},
        "host": {
            "architecture": "aarch64",
            "kernel_system": "Linux",
            "kernel_release": "6.12-orbstack",
        },
        "ocaml": {
            "runtime_path": "/opt/ocaml/bin/ocamlrun",
            "runtime_version": "OCaml 5.4",
        },
        "python": {
            "executable": "/usr/bin/python3",
            "implementation": "CPython",
            "version": "3.13.5",
        },
    }
    require(snapshot_environment_failures(environment) == [], "complete snapshot environment was rejected")
    drifted = copy.deepcopy(environment)
    drifted["criu"]["version"] = "Version: later"
    drifted["python"]["version"] = "later"
    require(snapshot_environment_failures(drifted) == [], "diagnostic version drift became structural failure")
    incomplete = copy.deepcopy(environment)
    del incomplete["ocaml"]["runtime_version"]
    require(
        "snapshot environment ocaml.runtime_version is missing" in snapshot_environment_failures(incomplete),
        "missing runtime version was accepted",
    )


def loader_export_text(name: str, digest: str, *, count: int = 1) -> str:
    return "\n".join(
        (
            "HOL chatter",
            "__HOL_WORKBENCH_LOADED_FILE_V1__:BEGIN",
            f"__HOL_WORKBENCH_LOADED_FILE_V1__:{name.encode().hex()}:{digest}",
            f"__HOL_WORKBENCH_LOADED_FILE_V1__:END:{count}",
            "more chatter",
        )
    )


def test_loaded_provenance(root: Path) -> None:
    holdir = root / "hol"
    source = holdir / "Library" / "loaded.ml"
    source.parent.mkdir(parents=True)
    source.write_text("loaded", encoding="utf-8")
    digest = hashlib.md5(b"loaded", usedforsecurity=False).hexdigest()
    records = parse_loader_export(loader_export_text(source.name, digest))
    require(records == [LoaderRecord(source.name, digest)], "loader export record changed")
    closure = resolve_loaded_closure(records, holdir=holdir, semantic_inputs=[])
    require(validate_loaded_closure(closure, holdir=holdir) == [], "exact loaded closure was rejected")

    source.write_text("changed", encoding="utf-8")
    failures = validate_loaded_closure(closure, holdir=holdir)
    require(any("MD5 mismatch" in failure for failure in failures), "loaded-file MD5 drift was missed")
    require(any("SHA-256 mismatch" in failure for failure in failures), "loaded-file SHA-256 drift was missed")
    require_raises(
        LoadedProvenanceError,
        "count mismatch",
        lambda: parse_loader_export(loader_export_text(source.name, digest, count=2)),
    )


def test_admission_hash_binding(root: Path) -> None:
    request = SnapshotAdmissionRequest.create(
        required_capabilities=BINDING_SCOPED_REQUIRED_CAPABILITIES,
        explicit_proof_search_budget_seconds="1",
    )
    request_record = request.record()
    require(SnapshotAdmissionRequest.from_record(request_record) == request, "admission request did not round-trip")
    noncanonical = copy.deepcopy(request_record)
    noncanonical["required_capabilities"] = list(reversed(noncanonical["required_capabilities"]))
    rehash(noncanonical, "request_sha256")
    require_raises(
        RuntimeError,
        "not canonical",
        lambda: SnapshotAdmissionRequest.from_record(noncanonical),
    )

    decision = StaticSnapshotAdmissionDecision(
        request=request,
        manifest_path=str((root / "snapshot-manifest.json").resolve()),
        manifest_sha256="a" * 64,
        profile_identity={
            "execution_topology": RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3.value,
            "profile_basis_id": "basis-selftest",
            "fork_snapshot_abi": "proof-run-fork-snapshot.v3",
            "broker_protocol": "hol-workbench.fork-basis-broker.v1",
            "broker_runtime_sha256": "b" * 64,
        },
        runtime_compatibility={
            "compatible": True,
            "status": "compatible",
            "binding_budget_owner": "current_controller",
        },
        mode=SnapshotAdmissionMode.FULL_BINDING_SCOPED,
        reason="exact selftest admission",
    )
    decision_record = decision.record()
    require(
        StaticSnapshotAdmissionDecision.from_record(decision_record) == decision,
        "static admission decision did not round-trip",
    )
    contradictory = copy.deepcopy(decision_record)
    contradictory["effective_guarantees"]["binding_reset_guaranteed"] = False
    rehash(contradictory, "decision_sha256")
    require_raises(
        RuntimeError,
        "guarantees contradict",
        lambda: StaticSnapshotAdmissionDecision.from_record(contradictory),
    )


def test_lazy_pages_identity(root: Path) -> None:
    image_dir = root / "criu-image"
    image_dir.mkdir()
    pidfile = image_dir / "lazy-pages-selftest.pid"
    log = image_dir / "lazy-pages-selftest.log"
    pidfile.write_text("123\n", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    daemon = bind_daemon_identity(
        pidfile,
        start_ticks_reader=lambda _pid: 55,
        identity_reader=lambda _pid: "linux-proc:55",
        pid_alive=lambda _pid: True,
    )
    require(daemon["pid"] == 123 and daemon["start_ticks"] == 55, "stable daemon identity was not bound")
    changing_ticks = iter((55, 56))
    require_raises(
        RuntimeError,
        "no stable birth identity",
        lambda: bind_daemon_identity(
            pidfile,
            start_ticks_reader=lambda _pid: next(changing_ticks),
            identity_reader=lambda _pid: "linux-proc:55",
            pid_alive=lambda _pid: True,
        ),
    )
    record = owner_record(
        image_dir=image_dir,
        pidfile=pidfile,
        log=log,
        daemon=daemon,
        readiness={"status_fd_observed": True, "byte_hex": "00", "observed_utc": "2026-08-12T00:00:00Z"},
    )

    signals: list[int] = []
    replaced = terminate_owned_daemon(
        record,
        signal_exact=lambda _pid, _ticks, signum: signals.append(signum) or {"status": "signalled"},
        start_ticks_reader=lambda _pid: 99,
        identity_reader=lambda _pid: "linux-proc:99",
        pid_alive=lambda _pid: True,
    )
    require(replaced["verified_quiescent"] is True, "replaced daemon identity was not classified safely")
    require(signals == [], "replacement PID was signalled")

    helper_mismatch = terminate_owned_daemon(
        record,
        signal_exact=lambda _pid, _ticks, _signum: {"status": "identity_mismatch"},
        start_ticks_reader=lambda _pid: 55,
        identity_reader=lambda _pid: "linux-proc:55",
        pid_alive=lambda _pid: True,
    )
    require(helper_mismatch["status"] == "unverified", "helper mismatch self-certified quiescence")
    require(helper_mismatch["surviving_pids"] == [123], "live daemon disappeared from failure receipt")


def stopped_process(pid: int, pgid: int, children: list[int]) -> dict[str, Any]:
    return {
        "pid": pid,
        "pgid": pgid,
        "start_ticks": pid * 10,
        "identity": f"linux-proc:{pid * 10}",
        "state": "T",
        "children": children,
    }


def test_restore_tree_ownership() -> None:
    recorded = {
        "worker": {"pid": 100, "recorded_pgid": 100},
        "basis": {"pid": 200, "recorded_pgid": 200},
    }
    tree = [stopped_process(100, 100, [200]), stopped_process(200, 200, [])]
    roles = validate_restored_tree(authoritative_root_pid=100, recorded=recorded, tree=tree)
    require(set(roles) == {"worker", "basis"}, "complete stopped restore tree was rejected")
    require_raises(
        RuntimeError,
        "does not match recorded worker pid",
        lambda: validate_restored_tree(authoritative_root_pid=101, recorded=recorded, tree=tree),
    )
    missing_child = copy.deepcopy(tree)
    missing_child[0]["children"].append(201)
    require_raises(
        RuntimeError,
        "captured child processes are missing records [201]",
        lambda: validate_restored_tree(authoritative_root_pid=100, recorded=recorded, tree=missing_child),
    )
    unrecorded_group = copy.deepcopy(tree)
    unrecorded_group[1]["pgid"] = 201
    require_raises(
        RuntimeError,
        "unrecorded process groups [201]",
        lambda: validate_restored_tree(authoritative_root_pid=100, recorded=recorded, tree=unrecorded_group),
    )
    running = copy.deepcopy(tree)
    running[1]["state"] = "R"
    require_raises(
        RuntimeError,
        "processes not held stopped [200]",
        lambda: validate_restored_tree(authoritative_root_pid=100, recorded=recorded, tree=running),
    )


def test_residue_and_inventory(root: Path) -> None:
    image_dir = root / "residue-image"
    pool = root / "residue-pool"
    image_dir.mkdir()
    pool.mkdir()
    failure_log = image_dir / "restore-failure.log"
    old_log = image_dir / "restore-old.log"
    new_log = image_dir / "restore-new.log"
    external = root / "external.log"
    for path in (failure_log, old_log, new_log, external):
        path.write_text(path.name, encoding="utf-8")
    os.utime(failure_log, (1, 1))
    os.utime(old_log, (2, 2))
    os.utime(new_log, (3, 3))
    symlink = image_dir / "restore-link.log"
    symlink.symlink_to(external)
    (image_dir / "restore-failed-selftest.json").write_text(
        json.dumps({"restore_log": str(failure_log)}),
        encoding="utf-8",
    )
    (pool / "pool.json").write_text("{}\n", encoding="utf-8")
    receipt = prune_successful_restore_residue(image_dir=image_dir, pool=pool, keep=1)
    require(failure_log.exists(), "failure-bound restore log was pruned")
    require(new_log.exists() and not old_log.exists(), "regular restore retention was not bounded")
    require(symlink.is_symlink() and external.read_text(encoding="utf-8") == "external.log", "log symlink was followed")
    require(str(failure_log) in receipt["protected_failure_logs"], "protected log was absent from receipt")

    exact = command_pool_dir(f"worker --pool-dir {pool}")
    equals = command_pool_dir(f"worker --pool-dir={pool}")
    prefix = command_pool_dir(f"worker --pool-dir={pool}-other")
    require(exact == pool.resolve() and equals == pool.resolve(), "exact pool argument was not parsed")
    require(prefix != pool.resolve(), "prefix sibling was mistaken for the selected pool")
    inventory = pool_process_inventory(pool, process_table={}, proc_root=root / "missing-proc")
    require(inventory["zero_survivors"] is False, "missing process observations falsely proved quiescence")
    require(inventory["errors"], "missing process observations produced no refusal reason")


def test_logical_seat_broker_identity() -> None:
    session = {
        "session_dir": "/pool/sessions/physical",
        "socket": "/tmp/exact-broker.sock",
        "fork_snapshot_abi": "selftest-abi",
        "broker_runtime_sha256": "a" * 64,
        "profile_basis_id": "selftest-basis",
        "worker_pid": 101, "worker_pgid": 101,
        "worker_start_ticks": 1001, "worker_identity": "linux-proc:1001",
        "basis_pid": 102, "basis_pgid": 102,
        "basis_start_ticks": 1002, "basis_identity": "linux-proc:1002",
    }
    generation = {
        "broker_protocol": BROKER_PROTOCOL,
        "broker_runtime_sha256": session["broker_runtime_sha256"],
        "fork_snapshot_abi": session["fork_snapshot_abi"],
        "profile_basis_id": session["profile_basis_id"],
        "endpoint": session["socket"],
        "session_dir": session["session_dir"],
        "broker": {"pid": 101, "pgid": 101, "start_ticks": 1001, "birth_identity": "linux-proc:1001"},
        "basis": {"pid": 102, "pgid": 102, "start_ticks": 1002, "birth_identity": "linux-proc:1002"},
    }
    description = {
        **generation, "schema": BROKER_DESCRIBE_SCHEMA, "status": "described", "nonce": "selftest",
        "generation_sha256": canonical_sha256(generation),
    }
    logical = {**session, "session_dir": "/pool/sessions/logical-seat-2", "control_session_dir": session["session_dir"]}
    for seat in (session, logical):
        require(
            broker_description_problem(description, nonce="selftest", session=seat, expected=None) is None,
            "physical broker generation was rejected for its own seat or logical alias",
        )
    for field, changed in (
        ("session_dir", logical["session_dir"]),
        ("endpoint", "/tmp/other.sock"),
        ("broker_runtime_sha256", "b" * 64),
        ("profile_basis_id", "other-basis"),
        ("broker", {**generation["broker"], "start_ticks": 9999}),
        ("basis", {**generation["basis"], "start_ticks": 9999}),
        ("generation_sha256", "0" * 64),
    ):
        require(
            broker_description_problem({**description, field: changed}, nonce="selftest", session=logical, expected=None),
            f"logical seat accepted changed broker identity field {field}",
        )
    require(
        broker_description_problem(
            description, nonce="selftest", session={**logical, "control_session_dir": "/other/control"}, expected=None,
        ),
        "logical seat accepted a different control session",
    )


def main() -> int:
    if sys.platform != "linux":
        raise SystemExit("CRIU contract selftest is Linux-only")
    test_environment_policy()
    test_logical_seat_broker_identity()
    with tempfile.TemporaryDirectory(prefix="criu-contract-") as temporary:
        root = Path(temporary)
        test_loaded_provenance(root)
        test_admission_hash_binding(root)
        test_lazy_pages_identity(root)
        test_restore_tree_ownership()
        test_residue_and_inventory(root)
    print("criu_contract_selftest=passed cases=7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
