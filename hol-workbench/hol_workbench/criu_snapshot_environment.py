"""Recorded build environment and explicit CRIU shelf compatibility policy."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hol_workbench.criu_invocation import criu_command

SNAPSHOT_ENVIRONMENT_SCHEMA = "hol-workbench.criu-snapshot-environment.v1"
STRICT_COMPATIBILITY_FIELDS = (
    "schema",
    "admission_authority",
    "profile",
    "profile_base",
    "profile_sha256",
    "profile_basis_id",
    "profile_cwd",
    "extra_preloads",
    "fork_snapshot_abi",
    "fork_snapshot_capabilities",
    "snapshot_provenance.loaded_closure",
    "snapshot_provenance.runtime_closure",
    "snapshot_environment.host.architecture",
    "snapshot_environment.host.kernel_system",
)
DIAGNOSTIC_COMPATIBILITY_FIELDS = (
    "snapshot_environment.criu.version",
    "snapshot_environment.host.kernel_release",
    "snapshot_environment.ocaml.runtime_path",
    "snapshot_environment.ocaml.runtime_version",
    "snapshot_environment.python.executable",
    "snapshot_environment.python.implementation",
    "snapshot_environment.python.version",
)
INTEGRITY_ONLY_FIELDS = (
    "snapshot_environment.schema",
    "snapshot_environment.capture_context.same_process_domain",
    "snapshot_environment.capture_context.target_runtime_path",
    "snapshot_environment_sha256",
    "compatibility_policy",
)


class SnapshotEnvironmentError(RuntimeError):
    """Raised when a required diagnostic identity cannot be captured."""


def snapshot_compatibility_policy() -> dict[str, Any]:
    """Return a fresh strict-versus-diagnostic compatibility declaration."""

    return {
        "schema": "hol-workbench.criu-compatibility-policy.v1",
        "strict": list(STRICT_COMPATIBILITY_FIELDS),
        "diagnostic": list(DIAGNOSTIC_COMPATIBILITY_FIELDS),
        "integrity_only": list(INTEGRITY_ONLY_FIELDS),
        "diagnostic_drift_effect": "does_not_invalidate_shelf",
        "diagnostic_policy": (
            "exact loaded/runtime bytes, architecture, OS, worker ABI, and capabilities are strict; "
            "recorded version strings are forensic while the real restore transaction and post-restore eval "
            "remain the practical CRIU compatibility gate"
        ),
        "policy_revision_effect": "requires_new_shelf_publication",
    }


def _version_output(
    argv: list[str],
    *,
    label: str,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    try:
        proc = run_command(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SnapshotEnvironmentError(f"cannot capture {label} version: {exc}") from exc
    output = " ".join((proc.stdout or "").split())
    if proc.returncode != 0 or not output:
        raise SnapshotEnvironmentError(
            f"cannot capture {label} version: exit {proc.returncode}, output {output or 'missing'}"
        )
    return output


def _runtime_path(runtime_closure: dict[str, Any], role: str) -> Path:
    raw_entries = runtime_closure.get("entries")
    entries = raw_entries if isinstance(raw_entries, list) else []
    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get("role") == role]
    if len(matches) != 1:
        raise SnapshotEnvironmentError(f"runtime closure does not contain exactly one {role} entry")
    path = Path(str(matches[0].get("resolved_path") or matches[0].get("path") or ""))
    if not path.is_absolute():
        raise SnapshotEnvironmentError(f"runtime closure {role} path is not absolute")
    return path


def _target_executable(pid: int) -> Path:
    try:
        return (Path("/proc") / str(pid) / "exe").resolve(strict=True)
    except OSError as exc:
        raise SnapshotEnvironmentError(
            f"cannot resolve target OCaml process {pid} in this process domain: {exc}"
        ) from exc


def current_host_identity() -> dict[str, str]:
    uname = platform.uname()
    return {
        "architecture": uname.machine,
        "kernel_system": uname.system,
        "kernel_release": uname.release,
    }


def capture_snapshot_environment(
    runtime_closure: dict[str, Any],
    *,
    target_pid: int,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    target_executable: Callable[[int], Path] = _target_executable,
) -> dict[str, Any]:
    """Capture diagnostic build identities after strict runtime capture."""

    ocamlrun = _runtime_path(runtime_closure, "ocamlrun")
    host = current_host_identity()
    if host["kernel_system"] != "Linux":
        raise SnapshotEnvironmentError("CRIU snapshot environment capture must run inside the Linux guest")
    target_runtime = target_executable(target_pid).resolve()
    if target_runtime != ocamlrun.resolve():
        raise SnapshotEnvironmentError(
            f"target OCaml runtime {target_runtime} does not match captured runtime closure {ocamlrun}"
        )
    return {
        "schema": SNAPSHOT_ENVIRONMENT_SCHEMA,
        "capture_context": {
            "same_process_domain": True,
            "target_runtime_path": str(target_runtime),
        },
        "criu": {
            "version": _version_output(
                criu_command("--version"),
                label="CRIU",
                run_command=run_command,
            ),
        },
        "host": host,
        "ocaml": {
            "runtime_path": str(ocamlrun),
            "runtime_version": _version_output(
                [str(ocamlrun), "-version"],
                label="OCaml runtime",
                run_command=run_command,
            ),
        },
        "python": {
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
    }


def snapshot_environment_sha256(environment: object) -> str:
    """Hash the recorded diagnostic identity without making it strict compatibility."""

    encoded = json.dumps(environment, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_environment_failures(environment: object) -> list[str]:
    """Validate record completeness, not equality with the current host."""

    if not isinstance(environment, dict):
        return ["snapshot environment identity is missing"]

    def field(section: str, name: str) -> object:
        record = environment.get(section)
        return record.get(name) if isinstance(record, dict) else None

    required = {
        "capture_context.same_process_domain": field("capture_context", "same_process_domain"),
        "capture_context.target_runtime_path": field("capture_context", "target_runtime_path"),
        "criu.version": field("criu", "version"),
        "host.architecture": field("host", "architecture"),
        "host.kernel_system": field("host", "kernel_system"),
        "host.kernel_release": field("host", "kernel_release"),
        "ocaml.runtime_path": field("ocaml", "runtime_path"),
        "ocaml.runtime_version": field("ocaml", "runtime_version"),
        "python.executable": field("python", "executable"),
        "python.implementation": field("python", "implementation"),
        "python.version": field("python", "version"),
    }
    failures = []
    if environment.get("schema") != SNAPSHOT_ENVIRONMENT_SCHEMA:
        failures.append("snapshot environment schema is incompatible")
    failures.extend(f"snapshot environment {name} is missing" for name, value in required.items() if not value)
    runtime_path = Path(str(required["ocaml.runtime_path"] or ""))
    if required["ocaml.runtime_path"] and not runtime_path.is_absolute():
        failures.append("snapshot environment ocaml.runtime_path is not absolute")
    target_runtime_path = Path(str(required["capture_context.target_runtime_path"] or ""))
    if required["capture_context.target_runtime_path"] and not target_runtime_path.is_absolute():
        failures.append("snapshot environment capture_context.target_runtime_path is not absolute")
    if required["capture_context.same_process_domain"] is not True:
        failures.append("snapshot environment was not captured in the target process domain")
    if runtime_path.is_absolute() and target_runtime_path.is_absolute() and runtime_path != target_runtime_path:
        failures.append("snapshot environment target runtime does not match the OCaml runtime identity")
    return failures


def snapshot_environment_compatibility(environment: object) -> dict[str, Any]:
    """Apply the executable strict host gates and report cheap diagnostic drift."""

    record: dict[str, Any] = environment if isinstance(environment, dict) else {}
    raw_host = record.get("host")
    recorded_host: dict[str, Any] = raw_host if isinstance(raw_host, dict) else {}
    current_host = current_host_identity()
    failures = []
    for key in ("architecture", "kernel_system"):
        if recorded_host.get(key) != current_host[key]:
            failures.append(
                f"snapshot environment {key} {recorded_host.get(key) or 'missing'} != current {current_host[key]}"
            )

    diagnostic_drift: dict[str, dict[str, str]] = {}
    if recorded_host.get("kernel_release") != current_host["kernel_release"]:
        diagnostic_drift["host.kernel_release"] = {
            "recorded": str(recorded_host.get("kernel_release") or "missing"),
            "current": current_host["kernel_release"],
        }
    raw_python = record.get("python")
    recorded_python: dict[str, Any] = raw_python if isinstance(raw_python, dict) else {}
    current_python = {
        "executable": sys.executable,
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
    }
    for key, current in current_python.items():
        if recorded_python.get(key) != current:
            diagnostic_drift[f"python.{key}"] = {
                "recorded": str(recorded_python.get(key) or "missing"),
                "current": current,
            }
    return {
        "compatible": not failures,
        "status": "compatible" if not failures else "strict_host_mismatch",
        "failures": failures,
        "diagnostic_drift": diagnostic_drift,
        "unprobed_diagnostics": ["criu.version", "ocaml.runtime_path", "ocaml.runtime_version"],
    }
