"""Supervised physical-profile worker transport for warm readiness probes."""

from __future__ import annotations

import secrets
import subprocess
from pathlib import Path
from typing import Any

from hol_workbench.cli.orbstack_criu_vanilla_readiness_lifecycle import (
    bounded_message,
    bounded_output_tail,
    controller_identity_complete,
    controller_is_live,
)
from hol_workbench.jsonio import durable_atomic_write_json, read_json, read_json_strict

WORKER_SPEC_SCHEMA = "hol-workbench.warm-vanilla-readiness-worker-spec.v1"
WORKER_RESULT_SCHEMA = "hol-workbench.warm-vanilla-readiness-worker-result.v1"
WORKBENCH_BIN = Path(__file__).resolve().parents[2] / "bin"


def normalize_worker_returncode(returncode: int) -> dict[str, int | None]:
    signal_number = -returncode if returncode < 0 else None
    return {
        "raw_returncode": returncode,
        "exit_status": 128 + signal_number if signal_number is not None else returncode,
        "signal": signal_number,
    }


def run_worker(*, spec_path: Path, result_path: Path) -> int:
    controller_nonce: str | None = None
    worker_token: str | None = None
    try:
        spec = read_json_strict(spec_path)
        if spec.get("schema") != WORKER_SPEC_SCHEMA:
            raise ValueError(f"unsupported readiness worker spec: {spec.get('schema') or 'missing'}")
        controller = spec.get("controller")
        worker_token = spec.get("worker_token")
        controller_nonce = controller.get("nonce") if isinstance(controller, dict) else None
        if not controller_identity_complete(controller) or not controller_is_live(controller):
            raise RuntimeError("readiness worker controller identity is not live")
        if not isinstance(worker_token, str) or len(worker_token) < 16:
            raise ValueError("readiness worker token is missing or invalid")
        from hol_workbench.cli import orbstack_criu_vanilla_readiness as readiness

        row = readiness._run_group(
            physical_run_root=str(spec["physical_run_root"]),
            physical_profile=str(spec["physical_profile"]),
            group=list(spec["group"]),
            run_root=Path(str(spec["run_root"])),
            timeout=float(spec["timeout"]),
            vanilla_run=readiness.orbstack_criu_vanilla.run,
        )
        durable_atomic_write_json(
            result_path,
            {
                "schema": WORKER_RESULT_SCHEMA,
                "controller_nonce": controller_nonce,
                "worker_token": worker_token,
                "row": row,
            },
        )
        return 0
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        durable_atomic_write_json(
            result_path,
            {
                "schema": WORKER_RESULT_SCHEMA,
                "controller_nonce": controller_nonce,
                "worker_token": worker_token,
                "error": bounded_message(f"{type(exc).__name__}: {exc}"),
            },
        )
        return 1


def supervised_group_runner(
    *,
    physical_run_root: str,
    physical_profile: str,
    group: list[dict[str, Any]],
    run_root: Path,
    timeout: float,
    controller: dict[str, Any],
    **_kwargs: Any,
) -> dict[str, Any]:
    logical_profiles = [str(row["logical_profile"]) for row in group]
    artifact_root = run_root / ("--".join(logical_profiles))
    artifact_root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    spec_path = artifact_root / f"worker-{token}.json"
    result_path = artifact_root / f"worker-{token}-result.json"
    durable_atomic_write_json(
        spec_path,
        {
            "schema": WORKER_SPEC_SCHEMA,
            "controller": controller,
            "worker_token": token,
            "physical_run_root": physical_run_root,
            "physical_profile": physical_profile,
            "group": group,
            "run_root": str(run_root),
            "timeout": timeout,
        },
    )
    completed = subprocess.run(
        [
            str(WORKBENCH_BIN / "orbstack-criu"),
            "vanilla-smoke",
            "--worker-spec",
            str(spec_path),
            "--worker-result",
            str(result_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    payload = read_json(result_path)
    identity_matches = (
        payload.get("schema") == WORKER_RESULT_SCHEMA
        and payload.get("controller_nonce") == controller.get("nonce")
        and payload.get("worker_token") == token
    )
    row = payload.get("row") if identity_matches else None
    normalized = normalize_worker_returncode(completed.returncode)
    worker_error = payload.get("error")
    if payload and not identity_matches:
        worker_error = "worker result controller nonce or worker token mismatch"
    return {
        **normalized,
        "row": row if isinstance(row, dict) else None,
        "worker_error": worker_error,
        "stdout_tail": bounded_output_tail(completed.stdout),
        "stderr_tail": bounded_output_tail(completed.stderr),
        "spec": str(spec_path),
        "result": str(result_path),
    }
