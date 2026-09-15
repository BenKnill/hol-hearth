"""Typed, hash-bound admission records for one CRIU snapshot execution."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from hol_workbench.broker_connection_identity import (
    observe_broker_endpoint,
    validate_broker_endpoint_identity,
)
from hol_workbench.hashing import sha256_text
from hol_workbench.jsonio import JsonObject, atomic_write_json, read_json_strict
from hol_workbench.proof_run_fork_budget import PROOF_SEARCH_BUDGET_SCOPE
from hol_workbench.restored_execution_topology import RestoredExecutionTopology

SNAPSHOT_ADMISSION_POLICY_REVISION = "hol-workbench.snapshot-admission-policy.v2"
SNAPSHOT_ADMISSION_REQUEST_SCHEMA = "hol-workbench.snapshot-admission-request.v1"
STATIC_SNAPSHOT_ADMISSION_SCHEMA = "hol-workbench.static-snapshot-admission.v1"
LIVE_EXECUTION_GRANT_SCHEMA = "hol-workbench.live-execution-grant.v3"
SNAPSHOT_ADMISSION_ARTIFACT_SCHEMA = "hol-workbench.snapshot-admission-artifact.v1"
BINDING_BUDGET_RESET_CAPABILITY = "proof_search_budget_binding_resets"
BINDING_SCOPED_REQUIRED_CAPABILITIES = frozenset(
    {
        "proof_search_budget_seconds",
        BINDING_BUDGET_RESET_CAPABILITY,
        "seat_reusable",
    }
)
LIVE_EXECUTION_GRANT_CLAIM_BOUNDARY = (
    "recorded live pool generation, exact live Linux Unix-listener identity, reviewed controller executable "
    "identity, and controller-observed Linux peer credentials"
)


def canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = sha256_text(encoded)
    if not digest:
        raise RuntimeError("could not hash snapshot admission record")
    return digest


def _hash_bound_record(payload: JsonObject, *, digest_field: str) -> JsonObject:
    return {**payload, digest_field: canonical_sha256(payload)}


def _require_hash_bound_record(record: object, *, schema: str, digest_field: str) -> JsonObject:
    if not isinstance(record, dict) or record.get("schema") != schema:
        raise RuntimeError(f"snapshot admission record does not use {schema}")
    payload = {key: value for key, value in record.items() if key != digest_field}
    if record.get(digest_field) != canonical_sha256(payload):
        raise RuntimeError(f"snapshot admission record has an invalid {digest_field}")
    return record


def _json_record(value: object) -> JsonObject | None:
    """Narrow one decoded JSON value without changing its stored fields."""

    if isinstance(value, dict):
        return value
    return None


class SnapshotAdmissionMode(StrEnum):
    FULL_BINDING_SCOPED = "full_binding_scoped"
    REFUSED = "refused"


@dataclass(frozen=True)
class SnapshotAdmissionRequest:
    required_capabilities: tuple[str, ...]
    explicit_proof_search_budget_seconds: float | None = None
    purpose: str = "named_profile_evaluation"

    @classmethod
    def create(
        cls,
        *,
        required_capabilities: set[str] | frozenset[str] | tuple[str, ...],
        explicit_proof_search_budget_seconds: float | str | None = None,
        purpose: str = "named_profile_evaluation",
    ) -> SnapshotAdmissionRequest:
        capabilities = tuple(sorted({str(item) for item in required_capabilities if str(item)}))
        if not capabilities:
            raise ValueError("snapshot admission requires at least one capability")
        seconds = None
        if explicit_proof_search_budget_seconds is not None:
            seconds = float(explicit_proof_search_budget_seconds)
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError("explicit proof-search budget must be a finite positive number")
        if not purpose:
            raise ValueError("snapshot admission purpose must be non-empty")
        return cls(
            required_capabilities=capabilities,
            explicit_proof_search_budget_seconds=seconds,
            purpose=purpose,
        )

    def record(self) -> JsonObject:
        payload = {
            "schema": SNAPSHOT_ADMISSION_REQUEST_SCHEMA,
            "policy_revision": SNAPSHOT_ADMISSION_POLICY_REVISION,
            "purpose": self.purpose,
            "required_capabilities": list(self.required_capabilities),
            "explicit_proof_search_budget_seconds": self.explicit_proof_search_budget_seconds,
        }
        return _hash_bound_record(payload, digest_field="request_sha256")

    @classmethod
    def from_record(cls, record: object) -> SnapshotAdmissionRequest:
        data = _require_hash_bound_record(
            record,
            schema=SNAPSHOT_ADMISSION_REQUEST_SCHEMA,
            digest_field="request_sha256",
        )
        if data.get("policy_revision") != SNAPSHOT_ADMISSION_POLICY_REVISION:
            raise RuntimeError("snapshot admission request policy revision is incompatible")
        raw_capabilities = data.get("required_capabilities")
        if not isinstance(raw_capabilities, list) or any(not isinstance(item, str) for item in raw_capabilities):
            raise RuntimeError("snapshot admission request capabilities are invalid")
        try:
            rebuilt = cls.create(
                required_capabilities=tuple(raw_capabilities),
                explicit_proof_search_budget_seconds=data.get("explicit_proof_search_budget_seconds"),
                purpose=str(data.get("purpose") or ""),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"snapshot admission request is invalid: {exc}") from exc
        if rebuilt.record() != data:
            raise RuntimeError("snapshot admission request is not canonical")
        return rebuilt


@dataclass(frozen=True)
class StaticSnapshotAdmissionDecision:
    request: SnapshotAdmissionRequest
    manifest_path: str
    manifest_sha256: str
    profile_identity: JsonObject
    runtime_compatibility: JsonObject
    mode: SnapshotAdmissionMode
    reason: str

    @property
    def binding_budget_owner(self) -> str | None:
        recorded = self.runtime_compatibility.get("binding_budget_owner")
        if isinstance(recorded, str) and recorded:
            return recorded
        return None

    @property
    def effective_guarantees(self) -> JsonObject:
        scope: str | None = (
            PROOF_SEARCH_BUDGET_SCOPE if self.mode is SnapshotAdmissionMode.FULL_BINDING_SCOPED else None
        )
        return {
            "proof_search_budget_scope": scope,
            "binding_reset_guaranteed": self.mode is SnapshotAdmissionMode.FULL_BINDING_SCOPED,
            "binding_budget_owner": self.binding_budget_owner,
            "explicit_proof_search_budget_seconds": self.request.explicit_proof_search_budget_seconds,
        }

    def record(self) -> JsonObject:
        payload = {
            "schema": STATIC_SNAPSHOT_ADMISSION_SCHEMA,
            "policy_revision": SNAPSHOT_ADMISSION_POLICY_REVISION,
            "request": self.request.record(),
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "profile_identity": self.profile_identity,
            "runtime_compatibility": self.runtime_compatibility,
            "mode": self.mode.value,
            "admitted": self.mode is not SnapshotAdmissionMode.REFUSED,
            "reason": self.reason,
            "effective_guarantees": self.effective_guarantees,
        }
        return _hash_bound_record(payload, digest_field="decision_sha256")

    @property
    def decision_sha256(self) -> str:
        return str(self.record()["decision_sha256"])

    def budget_contract_record(self) -> JsonObject:
        seconds = self.request.explicit_proof_search_budget_seconds
        if self.mode is SnapshotAdmissionMode.FULL_BINDING_SCOPED:
            budget_mode = "binding_scoped_worker_budget"
        else:
            budget_mode = "refused"
        return {
            "admission_mode": self.mode.value,
            "execution_topology": self.profile_identity.get("execution_topology"),
            "profile_basis_id": self.profile_identity.get("profile_basis_id"),
            "fork_snapshot_abi": self.profile_identity.get("fork_snapshot_abi"),
            "broker_protocol": self.profile_identity.get("broker_protocol"),
            "broker_runtime_sha256": self.profile_identity.get("broker_runtime_sha256"),
            "mode": budget_mode,
            "seconds": seconds,
            "explicit_host_override": seconds is not None,
            "binding_reset_guaranteed": self.mode is SnapshotAdmissionMode.FULL_BINDING_SCOPED,
            "binding_budget_owner": self.binding_budget_owner,
            "static_admission_decision_sha256": self.decision_sha256,
        }

    def legacy_manifest_mode(self) -> str:
        return "required_capabilities"

    @classmethod
    def from_record(cls, record: object) -> StaticSnapshotAdmissionDecision:
        data = _require_hash_bound_record(
            record,
            schema=STATIC_SNAPSHOT_ADMISSION_SCHEMA,
            digest_field="decision_sha256",
        )
        if data.get("policy_revision") != SNAPSHOT_ADMISSION_POLICY_REVISION:
            raise RuntimeError("static snapshot admission policy revision is incompatible")
        request = SnapshotAdmissionRequest.from_record(data.get("request"))
        try:
            mode = SnapshotAdmissionMode(str(data.get("mode") or ""))
        except ValueError as exc:
            raise RuntimeError("static snapshot admission mode is invalid") from exc
        admitted = data.get("admitted")
        if admitted is not (mode is not SnapshotAdmissionMode.REFUSED):
            raise RuntimeError("static snapshot admission status contradicts its mode")
        profile_identity = _json_record(data.get("profile_identity"))
        runtime = _json_record(data.get("runtime_compatibility"))
        guarantees = _json_record(data.get("effective_guarantees"))
        if profile_identity is None or runtime is None or guarantees is None:
            raise RuntimeError("static snapshot admission payload is invalid")
        topology_raw = str(profile_identity.get("execution_topology") or "")
        topology = None
        if mode is SnapshotAdmissionMode.FULL_BINDING_SCOPED:
            try:
                topology = RestoredExecutionTopology(topology_raw)
            except ValueError as exc:
                raise RuntimeError("static snapshot admission execution topology is invalid") from exc
            if (
                topology is not RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3
                or runtime.get("binding_budget_owner") != "current_controller"
            ):
                raise RuntimeError("static snapshot admission budget owner contradicts its topology")
        rebuilt = cls(
            request=request,
            manifest_path=str(data.get("manifest_path") or ""),
            manifest_sha256=str(data.get("manifest_sha256") or ""),
            profile_identity=dict(profile_identity),
            runtime_compatibility=dict(runtime),
            mode=mode,
            reason=str(data.get("reason") or ""),
        )
        if not rebuilt.manifest_path or len(rebuilt.manifest_sha256) != 64 or not rebuilt.reason:
            raise RuntimeError("static snapshot admission identity is incomplete")
        full = mode is SnapshotAdmissionMode.FULL_BINDING_SCOPED
        if guarantees != rebuilt.effective_guarantees or (
            full
            and (
                BINDING_BUDGET_RESET_CAPABILITY not in request.required_capabilities
                or runtime.get("compatible") is not True
                or runtime.get("status") != "compatible"
                or runtime.get("binding_budget_owner") != "current_controller"
            )
        ):
            raise RuntimeError("static snapshot admission guarantees contradict its mode")
        if rebuilt.record() != data:
            raise RuntimeError("static snapshot admission decision is not canonical")
        return rebuilt


def _live_process_identity(data: JsonObject, role: str) -> JsonObject:
    raw_pid = data.get(f"{role}_pid")
    try:
        if raw_pid is None:
            raise TypeError("missing pid")
        pid = int(raw_pid)
        pgid = int(data.get(f"{role}_pgid") or pid)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"live {role} process identity is incomplete") from exc
    raw_start_ticks = data.get(f"{role}_start_ticks")
    try:
        start_ticks = None if raw_start_ticks is None else int(raw_start_ticks)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"live {role} process identity is incomplete") from exc
    identity = str(data.get(f"{role}_identity") or "")
    if pid <= 0 or pgid <= 0 or (start_ticks is not None and start_ticks <= 0) or not identity:
        raise RuntimeError(f"live {role} process identity is incomplete")
    return {
        "pid": pid,
        "pgid": pgid,
        "start_ticks": start_ticks,
        "birth_identity": identity,
    }


def _valid_process_identity(identity: JsonObject) -> bool:
    raw_pid = identity.get("pid")
    raw_pgid = identity.get("pgid")
    if raw_pid is None or raw_pgid is None:
        return False
    raw_start_ticks = identity.get("start_ticks")
    try:
        return (
            int(raw_pid) > 0
            and int(raw_pgid) > 0
            and (raw_start_ticks is None or int(raw_start_ticks) > 0)
            and bool(identity.get("birth_identity"))
        )
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class LiveExecutionGrant:
    """Final-evidence grant layered above ordinary broker shelf admission."""

    static_decision_sha256: str
    pool: str
    worker: JsonObject
    basis: JsonObject
    session_dirs: tuple[str, ...]
    connections: tuple[JsonObject, ...]
    controller_executable_identity_sha256: str
    generation_sha256: str

    @classmethod
    def observe(
        cls,
        *,
        decision: StaticSnapshotAdmissionDecision,
        pool: Path,
        pool_record_liveness_check: Callable[[JsonObject], bool],
    ) -> LiveExecutionGrant:
        decision = StaticSnapshotAdmissionDecision.from_record(decision.record())
        if decision.mode is SnapshotAdmissionMode.REFUSED:
            raise RuntimeError("cannot grant live execution for a refused snapshot")
        pool_path = pool.expanduser().resolve()
        manifest_root = Path(decision.manifest_path).expanduser().resolve().parent
        expected_pool_root = manifest_root / "pool"
        if not pool_path.is_relative_to(expected_pool_root):
            raise RuntimeError("live pool is outside the admitted snapshot")
        data = read_json_strict(pool_path / "pool.json")
        if not pool_record_liveness_check(data):
            raise RuntimeError("live execution grant requires matching live worker and basis PID/birth identities")
        if data.get("status") != "ready":
            raise RuntimeError(f"live execution grant requires pool status ready, found {data.get('status')!r}")
        worker = _live_process_identity(data, "worker")
        basis = _live_process_identity(data, "basis")
        controller_attempt = _json_record(decision.runtime_compatibility.get("controller_attempt_compatibility"))
        if controller_attempt is None or controller_attempt.get("compatible") is not True:
            status = controller_attempt.get("status") if controller_attempt is not None else "missing"
            raise RuntimeError(
                f"live execution grant requires a reviewed controller attempt; controller attempt status is {status}"
            )
        try:
            from hol_workbench.controller_runtime_bundle import verified_active_controller_runtime_attempt

            active_attempt = verified_active_controller_runtime_attempt()
        except RuntimeError as exc:
            raise RuntimeError(
                f"live execution grant requires the current immutable controller attempt: {exc}"
            ) from exc
        controller_identity = str(controller_attempt.get("controller_executable_identity_sha256") or "")
        active_identity = str(active_attempt.get("executable_identity_sha256") or "")
        active_authorization_id = active_attempt.get("authorization_id")
        active_authorization_sha256 = active_attempt.get("authorization_sha256")
        if (
            len(controller_identity) != 64
            or active_identity != controller_identity
            or decision.runtime_compatibility.get("controller_executable_identity_sha256") != active_identity
            or active_authorization_id != controller_attempt.get("controller_authorization_id")
            or active_authorization_sha256 != controller_attempt.get("controller_authorization_sha256")
        ):
            raise RuntimeError("live execution grant controller authorization changed after static admission")
        raw_sessions = data.get("sessions")
        if not isinstance(raw_sessions, list) or not raw_sessions:
            raise RuntimeError("live execution grant requires at least one pool session")
        session_dirs: list[str] = []
        connections: list[JsonObject] = []
        for session in raw_sessions:
            if not isinstance(session, dict):
                raise RuntimeError("live execution grant session inventory is invalid")
            session_dir = Path(str(session.get("session_dir") or "")).expanduser()
            if not session_dir.is_absolute():
                raise RuntimeError("live execution grant session path is not absolute")
            resolved_session = session_dir.resolve()
            session_record = read_json_strict(resolved_session / "session.json")
            if str(Path(str(session_record.get("session_dir") or "")).expanduser().resolve()) != str(resolved_session):
                raise RuntimeError("live execution grant session metadata changed its own path")
            for role, identity in (("worker", worker), ("basis", basis)):
                observed = _live_process_identity(session_record, role)
                if observed != identity:
                    raise RuntimeError(f"live execution grant session changed its {role} process identity")
            endpoint = observe_broker_endpoint(Path(str(session_record.get("socket") or "")))
            session_dirs.append(str(resolved_session))
            connections.append(
                {
                    "session_dir": str(resolved_session),
                    "endpoint": endpoint,
                    "broker": worker,
                }
            )
        generation_payload = {
            "static_decision_sha256": decision.decision_sha256,
            "pool": str(pool_path),
            "worker": worker,
            "basis": basis,
            "session_dirs": session_dirs,
            "connections": connections,
            "controller_executable_identity_sha256": controller_identity,
        }
        return cls(
            static_decision_sha256=decision.decision_sha256,
            pool=str(pool_path),
            worker=worker,
            basis=basis,
            session_dirs=tuple(session_dirs),
            connections=tuple(connections),
            controller_executable_identity_sha256=controller_identity,
            generation_sha256=canonical_sha256(generation_payload),
        )

    def record(self) -> JsonObject:
        payload = {
            "schema": LIVE_EXECUTION_GRANT_SCHEMA,
            "policy_revision": SNAPSHOT_ADMISSION_POLICY_REVISION,
            "claim_boundary": LIVE_EXECUTION_GRANT_CLAIM_BOUNDARY,
            "static_decision_sha256": self.static_decision_sha256,
            "pool": self.pool,
            "worker": self.worker,
            "basis": self.basis,
            "session_dirs": list(self.session_dirs),
            "connections": list(self.connections),
            "controller_executable_identity_sha256": self.controller_executable_identity_sha256,
            "generation_sha256": self.generation_sha256,
        }
        return _hash_bound_record(payload, digest_field="grant_sha256")

    @classmethod
    def from_record(cls, record: object) -> LiveExecutionGrant:
        data = _require_hash_bound_record(
            record,
            schema=LIVE_EXECUTION_GRANT_SCHEMA,
            digest_field="grant_sha256",
        )
        if data.get("policy_revision") != SNAPSHOT_ADMISSION_POLICY_REVISION:
            raise RuntimeError("live execution grant policy revision is incompatible")
        if data.get("claim_boundary") != LIVE_EXECUTION_GRANT_CLAIM_BOUNDARY:
            raise RuntimeError("live execution grant claim boundary is incompatible")
        raw_sessions = data.get("session_dirs")
        if not isinstance(raw_sessions, list) or any(not isinstance(item, str) for item in raw_sessions):
            raise RuntimeError("live execution grant session inventory is invalid")
        worker = _json_record(data.get("worker"))
        basis = _json_record(data.get("basis"))
        raw_connections = data.get("connections")
        if not isinstance(raw_connections, list) or any(not isinstance(item, dict) for item in raw_connections):
            raise RuntimeError("live execution grant connection inventory is invalid")
        if worker is None or basis is None:
            raise RuntimeError("live execution grant process identity is invalid")
        rebuilt = cls(
            static_decision_sha256=str(data.get("static_decision_sha256") or ""),
            pool=str(data.get("pool") or ""),
            worker=dict(worker),
            basis=dict(basis),
            session_dirs=tuple(raw_sessions),
            connections=tuple(dict(item) for item in raw_connections),
            controller_executable_identity_sha256=str(data.get("controller_executable_identity_sha256") or ""),
            generation_sha256=str(data.get("generation_sha256") or ""),
        )
        if (
            len(rebuilt.static_decision_sha256) != 64
            or not Path(rebuilt.pool).is_absolute()
            or not rebuilt.session_dirs
            or any(not Path(item).is_absolute() for item in rebuilt.session_dirs)
            or len(rebuilt.controller_executable_identity_sha256) != 64
            or len(rebuilt.connections) != len(rebuilt.session_dirs)
        ):
            raise RuntimeError("live execution grant identity is incomplete")
        for role, identity in (("worker", rebuilt.worker), ("basis", rebuilt.basis)):
            if not _valid_process_identity(identity):
                raise RuntimeError(f"live execution grant {role} identity is invalid")
        connection_sessions: list[str] = []
        for connection in rebuilt.connections:
            session_dir = str(connection.get("session_dir") or "")
            endpoint = connection.get("endpoint")
            broker = connection.get("broker")
            if session_dir not in rebuilt.session_dirs or not isinstance(endpoint, dict) or broker != rebuilt.worker:
                raise RuntimeError("live execution grant connection identity is invalid")
            validate_broker_endpoint_identity(endpoint)
            connection_sessions.append(session_dir)
        if sorted(connection_sessions) != sorted(rebuilt.session_dirs):
            raise RuntimeError("live execution grant connection inventory is incomplete")
        generation_payload = {
            "static_decision_sha256": rebuilt.static_decision_sha256,
            "pool": rebuilt.pool,
            "worker": rebuilt.worker,
            "basis": rebuilt.basis,
            "session_dirs": list(rebuilt.session_dirs),
            "connections": list(rebuilt.connections),
            "controller_executable_identity_sha256": rebuilt.controller_executable_identity_sha256,
        }
        if rebuilt.generation_sha256 != canonical_sha256(generation_payload):
            raise RuntimeError("live execution grant generation identity is invalid")
        if rebuilt.record() != data:
            raise RuntimeError("live execution grant is not canonical")
        return rebuilt


def snapshot_admission_artifact_record(
    *,
    decision: StaticSnapshotAdmissionDecision,
    grant: LiveExecutionGrant,
) -> JsonObject:
    StaticSnapshotAdmissionDecision.from_record(decision.record())
    LiveExecutionGrant.from_record(grant.record())
    if grant.static_decision_sha256 != decision.decision_sha256:
        raise RuntimeError("live execution grant is bound to a different static admission decision")
    payload = {
        "schema": SNAPSHOT_ADMISSION_ARTIFACT_SCHEMA,
        "policy_revision": SNAPSHOT_ADMISSION_POLICY_REVISION,
        "static_decision": decision.record(),
        "live_execution_grant": grant.record(),
    }
    return _hash_bound_record(payload, digest_field="artifact_sha256")


def write_snapshot_admission_artifact(
    path: Path,
    *,
    decision: StaticSnapshotAdmissionDecision,
    grant: LiveExecutionGrant,
) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(resolved, snapshot_admission_artifact_record(decision=decision, grant=grant))
    return resolved


def write_content_addressed_snapshot_admission_artifact(
    directory: Path,
    *,
    decision: StaticSnapshotAdmissionDecision,
    grant: LiveExecutionGrant,
) -> tuple[Path, str]:
    record = snapshot_admission_artifact_record(decision=decision, grant=grant)
    artifact_sha256 = str(record["artifact_sha256"])
    root = directory.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"snapshot-admission-{artifact_sha256}.json"
    atomic_write_json(path, record)
    return path, artifact_sha256


def read_snapshot_admission_artifact(path: Path) -> JsonObject:
    data = _require_hash_bound_record(
        read_json_strict(path.expanduser().resolve()),
        schema=SNAPSHOT_ADMISSION_ARTIFACT_SCHEMA,
        digest_field="artifact_sha256",
    )
    if data.get("policy_revision") != SNAPSHOT_ADMISSION_POLICY_REVISION:
        raise RuntimeError("snapshot admission artifact policy revision is incompatible")
    decision = StaticSnapshotAdmissionDecision.from_record(data.get("static_decision"))
    grant = LiveExecutionGrant.from_record(data.get("live_execution_grant"))
    if grant.static_decision_sha256 != decision.decision_sha256:
        raise RuntimeError("snapshot admission artifact binds different static and live decisions")
    return data
