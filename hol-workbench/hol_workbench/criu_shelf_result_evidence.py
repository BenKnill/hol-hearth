"""Path- and digest-bound operational evidence for one CRIU shelf build."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hol_workbench.hashing import sha256_file

RESTORE_EVIDENCE_SCHEMA = "hol-workbench.criu-restore-evidence.v1"
EVAL_EVIDENCE_SCHEMA = "hol-workbench.criu-eval-evidence.v1"
EVAL_MARKERS = ("status: proved", "CRIU_SMOKE")


def _file_evidence(path: Path, *, schema: str) -> dict[str, Any]:
    path = path.expanduser().resolve(strict=True)
    digest = sha256_file(path)
    if digest is None:
        raise RuntimeError(f"cannot hash CRIU shelf evidence file {path}")
    return {
        "schema": schema,
        "path": str(path),
        "sha256": digest,
        "size_bytes": path.stat().st_size,
    }


def publish_restore_log_for_controller(
    path: Path,
    *,
    expected_parent: Path,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Transfer one root-created CRIU log to the unprivileged build controller."""

    path = path.expanduser()
    expected_parent = expected_parent.expanduser().resolve()
    if not path.is_absolute() or path.parent.resolve() != expected_parent:
        raise RuntimeError("CRIU restore evidence ownership target is outside the shelf image directory")
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"CRIU restore evidence is missing or not a file: {path}")
    uid = os.getuid()
    gid = os.getgid()
    metadata = path.stat()
    if metadata.st_uid != uid or metadata.st_gid != gid:
        owner = f"{uid}:{gid}"
        try:
            proc = run_command(
                ["sudo", "-n", "chown", "--no-dereference", owner, "--", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"cannot publish CRIU restore evidence ownership: {exc}") from exc
        if proc.returncode != 0:
            detail = " ".join((proc.stderr or "").split())[-500:]
            raise RuntimeError(
                f"cannot publish CRIU restore evidence ownership: chown exit {proc.returncode}: {detail or 'no output'}"
            )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RuntimeError(f"cannot restrict CRIU restore evidence permissions: {exc}") from exc


def capture_new_restore_evidence(
    image_dir: Path,
    *,
    previous_logs: set[Path],
    publish_root_owned: bool = False,
) -> dict[str, Any]:
    """Bind the one restore log created by a successful restore transaction."""

    before = {path.expanduser().resolve() for path in previous_logs}
    image_dir = image_dir.expanduser().resolve()
    current = sorted(path.resolve() for path in image_dir.glob("restore-*.log") if path.is_file())
    if any(path.parent != image_dir for path in current):
        raise RuntimeError("CRIU restore log resolves outside the shelf image directory")
    created = [path for path in current if path not in before]
    if len(created) != 1:
        raise RuntimeError(f"expected one new CRIU restore log, found {len(created)}")
    if publish_root_owned:
        publish_restore_log_for_controller(created[0], expected_parent=image_dir)
    return {**_file_evidence(created[0], schema=RESTORE_EVIDENCE_SCHEMA), "restore_status": 0}


def publish_dump_artifacts_for_controller(image_dir: Path) -> None:
    """Make sudo-created dump files readable by their unprivileged controller."""
    if image_dir.is_symlink():
        raise RuntimeError("CRIU dump directory must not be a symlink")
    root = image_dir.resolve(strict=True)
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"CRIU dump ownership transfer refuses symlink {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError(f"CRIU dump ownership transfer refuses special file {path}")
        publish_restore_log_for_controller(path, expected_parent=path.parent)


def capture_eval_evidence(
    log: Path,
    *,
    exit_status: int,
    required_markers: tuple[str, ...] = EVAL_MARKERS,
) -> dict[str, Any]:
    """Bind the semantic smoke log and the exact markers used to accept it."""

    if not required_markers or any(not marker for marker in required_markers):
        raise ValueError("CRIU eval evidence requires nonempty markers")
    evidence = _file_evidence(log, schema=EVAL_EVIDENCE_SCHEMA)
    text = log.read_text(encoding="utf-8", errors="replace")
    markers = {marker: marker in text for marker in required_markers}
    return {
        **evidence,
        "exit_status": exit_status,
        "required_markers": list(required_markers),
        "markers": markers,
        "proved": exit_status == 0 and all(markers.values()),
    }


def _validated_file(
    raw: object,
    *,
    schema: str,
    expected_path: Path | None = None,
    expected_parent: Path | None = None,
) -> tuple[Path | None, list[str]]:
    evidence = raw if isinstance(raw, dict) else {}
    failures: list[str] = []
    if evidence.get("schema") != schema:
        failures.append(f"{schema} record is missing or incompatible")
    recorded = Path(str(evidence.get("path") or "")).expanduser()
    if not recorded.is_absolute():
        failures.append(f"{schema} path is not absolute")
        return None, failures
    path = recorded.resolve()
    if expected_path is not None and path != expected_path.resolve():
        failures.append(f"{schema} path is not the expected shelf evidence path")
    if expected_parent is not None and path.parent != expected_parent.resolve():
        failures.append(f"{schema} path is outside the expected shelf evidence directory")
    if not path.is_file():
        failures.append(f"{schema} file is missing")
        return path, failures
    digest = sha256_file(path)
    if digest != evidence.get("sha256"):
        failures.append(f"{schema} SHA-256 does not match the evidence file")
    try:
        size = path.stat().st_size
    except OSError as exc:
        failures.append(f"{schema} file cannot be statted: {exc}")
    else:
        if size != evidence.get("size_bytes"):
            failures.append(f"{schema} byte count does not match the evidence file")
        if size <= 0:
            failures.append(f"{schema} evidence file is empty")
    return path, failures


def shelf_result_evidence_failures(profile_root: Path, row: dict[str, Any]) -> list[str]:
    """Revalidate the operational restore/eval evidence projected into a result row."""

    profile_root = profile_root.expanduser().resolve()
    failures: list[str] = []
    if row.get("restore_status") != 0 or row.get("restored_identity_transaction") != "ok":
        failures.append("successful restored-identity transaction is not recorded")
    restore_path, restore_failures = _validated_file(
        row.get("restore_evidence"),
        schema=RESTORE_EVIDENCE_SCHEMA,
        expected_parent=profile_root / "criu-image",
    )
    failures.extend(restore_failures)
    if restore_path is not None and not (
        restore_path.name.startswith("restore-") and restore_path.name.endswith(".log")
    ):
        failures.append("restore evidence is not a CRIU restore log")
    raw_restore = row.get("restore_evidence")
    restore = raw_restore if isinstance(raw_restore, dict) else {}
    if restore.get("restore_status") != 0:
        failures.append("restore evidence does not record a successful transaction")

    if row.get("eval_status") != 0 or row.get("eval_proved") is not True:
        failures.append("successful post-restore semantic evaluation is not recorded")
    eval_path, eval_failures = _validated_file(
        row.get("eval_evidence"),
        schema=EVAL_EVIDENCE_SCHEMA,
        expected_path=profile_root / "logs" / "post-restore-eval.log",
    )
    failures.extend(eval_failures)
    raw_evaluation = row.get("eval_evidence")
    evaluation = raw_evaluation if isinstance(raw_evaluation, dict) else {}
    if evaluation.get("exit_status") != 0 or evaluation.get("proved") is not True:
        failures.append("eval evidence does not record a proved zero-exit semantic check")
    if eval_path is not None and eval_path.is_file():
        try:
            text = eval_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            failures.append(f"eval evidence file cannot be read: {exc}")
        else:
            raw_required_markers = evaluation.get("required_markers")
            required_markers = (
                tuple(raw_required_markers)
                if isinstance(raw_required_markers, list)
                and raw_required_markers
                and all(isinstance(marker, str) and marker for marker in raw_required_markers)
                else EVAL_MARKERS
            )
            raw_markers = evaluation.get("markers")
            markers = raw_markers if isinstance(raw_markers, dict) else {}
            for marker in required_markers:
                if marker not in text or markers.get(marker) is not True:
                    failures.append(f"eval evidence is missing required marker {marker!r}")
    return failures
