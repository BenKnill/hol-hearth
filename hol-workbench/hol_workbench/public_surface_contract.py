#!/usr/bin/env python3
"""Executable contract for the contracted public Workbench surface."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

FORBIDDEN_HELP_MODULES = (
    "hol_workbench.hot_profile_session",
    "hol_workbench.criu_snapshot_admission",
    "hol_workbench.proof_run_fork_broker_client",
    "hol_workbench.cli.published_profile_replay",
)
PUBLIC_PROFILES = (
    "light",
    "heavy",
    "probability",
    "s2n-arm",
    "s2n-arm-light",
    "s2n-arm-mlkem",
    "s2n-x86",
)
RECORDED_FIRST_DOCS = ("AGENTS.md", "README.md", "docs/usage.md")


def _run(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=15.0,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _assert_help_isolated(root: Path, module: str, expression: str, maximum: int) -> None:
    code = (
        "import sys;"
        f"sys.path.insert(0,{str(root / 'hol-workbench')!r});"
        f"from hol_workbench.cli import {module} as cli;"
        f"{expression};"
        "loaded=sorted(name for name in sys.modules if name.startswith('hol_workbench'));"
        "print('WB_MODULES',len(loaded));"
        "print('WB_NAMES',','.join(loaded))"
    )
    completed = _run([sys.executable, "-I", "-B", "-c", code], cwd=root)
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    count = int(next(line.split()[1] for line in lines if line.startswith("WB_MODULES ")))
    assert count <= maximum, completed.stdout
    assert not any(name in completed.stdout for name in FORBIDDEN_HELP_MODULES), completed.stdout


def _assert_recorded_first_docs(root: Path) -> None:
    for relative in RECORDED_FIRST_DOCS:
        text = (root / relative).read_text(encoding="utf-8")
        recorded = text.find("--run-root")
        live = text.find("--loop")
        assert recorded >= 0 and live >= 0 and recorded < live, relative
        assert "--run-root runs" not in text and "--run-root RUNS" not in text, relative
        assert "/home/" not in text and "/Users/" not in text, relative
        for line in text.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("hol-workbench/bin/"), f"{relative}: {stripped}"
            assert not stripped.startswith("dev/linux hol-workbench/bin/"), f"{relative}: {stripped}"


def _receipt(
    path: Path,
    *,
    succeeded: bool,
    foundation_advisory: bool = False,
    recorded_exit_status: int | None = None,
    bindings: list[dict[str, object]] | None = None,
    first_failure: str | None = None,
    first_failure_transcript_line: int | None = None,
) -> None:
    raw = path.with_suffix(".raw")
    raw.write_text("first line\nException: bounded failure\nlast line\n", encoding="utf-8")
    if bindings is None:
        bindings = [{"name": "DEMO", "status": "proved"}] if succeeded else []
    if first_failure is None:
        first_failure = None if succeeded else "dependency input missing"
    receipt = {
        "schema": "hol-workbench.warm-vanilla-artifact.v1",
        "evidence": "recorded_warm_replay",
        "evidence_boundary": "exact source completion and named theorem binding checks",
        "source": "/work/proof.ml",
        "logical_profile": "light",
        "transport": "completed" if succeeded else "not_started",
        "semantic_source_status": "succeeded" if succeeded else "not_started",
        "source_completed": succeeded,
        "claims_complete": succeeded,
        "completion_marker_valid": succeeded,
        "semantic_exit_status": 0 if succeeded else 2,
        "bindings": bindings,
        "first_failure": first_failure,
        "first_failure_transcript_line": first_failure_transcript_line,
        "raw_transcript": str(raw),
    }
    if recorded_exit_status is not None:
        receipt.update(
            {
                "exit_status": recorded_exit_status,
                "worker_exit_status": recorded_exit_status,
                "process_exit_status": recorded_exit_status,
            }
        )
    if foundation_advisory:
        receipt.update(
            {
                "foundation_delta": {
                    "schema": "hol-workbench.runtime-foundation-delta.v1",
                    "status": "observed",
                    "deltas": {"axioms": 1, "definitions": 0, "types": 0, "constants": 0},
                },
                "promotion_advisory": {
                    "authority": "advisory_only",
                    "recommendation": "block",
                    "reasons": ["recorded replay payload introduced 1 runtime axiom(s)"],
                    "does_not_change_replay_result": True,
                },
            }
        )
    path.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str]) -> int:
    if sys.platform != "linux":
        raise SystemExit("public-surface selftest is Linux-only")
    if len(argv) != 2:
        raise SystemExit("usage: public_surface_contract.py REPOSITORY_ROOT")
    root = Path(argv[1]).resolve()
    workbench = root / "hol-workbench"
    sys.path.insert(0, str(workbench))
    from hol_workbench.cli.smoke import smoke_subprocess_environment

    for inherited in ({}, {"PATH": "/custom/bin", "HOL_WORKBENCH_PYTHON": "/other/python3"}):
        environment = smoke_subprocess_environment(inherited)
        assert environment["PATH"] == "/usr/local/bin:/usr/bin:/bin"
        assert environment["HOL_WORKBENCH_PYTHON"] == sys.executable
        assert Path(environment["HOL_WORKBENCH_PYTHON"]).is_absolute()

    prove = workbench / "bin" / "prove"
    inspect = workbench / "bin" / "inspect"
    smoke = workbench / "bin" / "smoke"

    prove_help = _run([str(prove), "--help"], cwd=root)
    assert prove_help.returncode == 0, prove_help.stderr
    assert "--loop" in prove_help.stdout and "Recorded replay:" in prove_help.stdout
    assert "same recorded" in prove_help.stdout
    assert "transitive dependencies" in prove_help.stdout
    assert "relative to the calling directory" in prove_help.stdout
    profiles_all = _run([str(prove), "profiles", "--all"], cwd=root)
    assert profiles_all.returncode == 0, profiles_all.stderr
    inspect_help = _run([str(inspect), "--help"], cwd=root)
    assert inspect_help.returncode == 0, inspect_help.stderr
    assert "bounded Workbench artifact inspection" not in inspect_help.stderr
    assert "--grep" in inspect_help.stdout and "--tail" in inspect_help.stdout
    smoke_help = _run([str(smoke), "--help"], cwd=root)
    assert smoke_help.returncode == 0, smoke_help.stderr
    assert "Cheap current-contract smoke" in smoke_help.stdout
    assert "without loading HOL or touching a warm profile" in smoke_help.stdout
    profiles = _run([str(prove), "profiles"], cwd=root)
    assert profiles.returncode == 0, profiles.stderr
    assert all(profile in profiles.stdout for profile in PUBLIC_PROFILES), profiles.stdout
    profile = _run([str(prove), "profiles", "light"], cwd=root)
    assert profile.returncode == 0, profile.stderr
    assert "/ABS/SOURCE.ml" in profile.stdout and "--run-root /ABS/runs" in profile.stdout

    _assert_help_isolated(
        root,
        "prove",
        f"cli.main(['--help'],script_dir={str(workbench / 'bin')!r})",
        8,
    )
    _assert_help_isolated(root, "inspect", "cli._parser().print_help()", 6)
    _assert_recorded_first_docs(root)

    with tempfile.TemporaryDirectory(prefix="workbench-public-surface-") as temp:
        fixture = Path(temp)
        success_dir = fixture / "success"
        advisory_dir = fixture / "advisory"
        worker_failure_dir = fixture / "worker-failure"
        failure_dir = fixture / "failure"
        residual_dir = fixture / "residual"
        unattributed_dir = fixture / "unattributed"
        success_dir.mkdir()
        advisory_dir.mkdir()
        worker_failure_dir.mkdir()
        failure_dir.mkdir()
        residual_dir.mkdir()
        unattributed_dir.mkdir()
        success = success_dir / "transcript.log.json"
        advisory = advisory_dir / "transcript.log.json"
        worker_failure = worker_failure_dir / "transcript.log.json"
        failure = failure_dir / "transcript.log.json"
        residual = residual_dir / "transcript.log.json"
        unattributed = unattributed_dir / "transcript.log.json"
        _receipt(success, succeeded=True)
        _receipt(advisory, succeeded=True, foundation_advisory=True, recorded_exit_status=0)
        _receipt(worker_failure, succeeded=True, recorded_exit_status=42)
        _receipt(failure, succeeded=False)
        _receipt(
            residual,
            succeeded=False,
            recorded_exit_status=1,
            bindings=[
                {
                    "name": "U2_GOOD",
                    "status": "missing",
                    "evidence": "nonce_probe_marker_missing",
                    "unverified_binding_like_text_observed": True,
                    "unverified_binding_like_transcript_lines": [13],
                },
                {
                    "name": "U2_REPAIR_ME",
                    "status": "missing",
                    "evidence": "nonce_probe_marker_missing",
                },
                {
                    "name": "U2_LATER",
                    "status": "missing",
                    "evidence": "nonce_probe_marker_missing",
                },
            ],
            first_failure='Exception: Failure "TAC_PROOF: Unsolved goals".',
            first_failure_transcript_line=17,
        )
        _receipt(
            unattributed,
            succeeded=False,
            bindings=[
                {"name": "U2_GOOD", "status": "missing"},
                {"name": "U2_REPAIR_ME", "status": "missing"},
                {"name": "U2_LATER", "status": "missing"},
            ],
            first_failure='Exception: Failure "TAC_PROOF: Unsolved goals".',
        )

        completed = _run([str(inspect), str(success)], cwd=fixture)
        assert completed.returncode == 0, completed.stderr
        assert "status: succeeded" in completed.stdout
        assert "source_acceptance: accepted" in completed.stdout
        assert "exit_status_check: unavailable (legacy receipt; semantic fallback)" in completed.stdout
        assert "DEMO: proved" in completed.stdout
        assert "foundation_advisory: unavailable (not recorded)" in completed.stdout
        assert "cleanup: not recorded (published warm seat may remain)" in completed.stdout
        advisory_completed = _run([str(inspect), str(advisory)], cwd=fixture)
        assert advisory_completed.returncode == 0, advisory_completed.stderr
        assert "status: succeeded" in advisory_completed.stdout
        assert "exit_status_check: passed" in advisory_completed.stdout
        assert (
            "foundation_delta: status=observed axioms=1 definitions=0 types=0 constants=0" in advisory_completed.stdout
        )
        assert "foundation_advisory: block (not publication authority)" in advisory_completed.stdout
        assert "advisory_reason: recorded replay payload introduced 1 runtime axiom(s)" in advisory_completed.stdout
        worker_failed = _run([str(inspect), str(worker_failure)], cwd=fixture)
        assert worker_failed.returncode == 1, worker_failed.stderr
        assert "status: failed" in worker_failed.stdout
        assert "source_acceptance: accepted" in worker_failed.stdout
        assert "exit_status_check: failed" in worker_failed.stdout
        assert "exit_status: 42" in worker_failed.stdout
        assert "worker_exit_status: 42" in worker_failed.stdout
        assert "process_exit_status: 42" in worker_failed.stdout
        failed = _run([str(inspect), str(failure)], cwd=fixture)
        assert failed.returncode == 1, failed.stderr
        assert "status: not_started" in failed.stdout
        assert "first_failure: dependency input missing" in failed.stdout
        residual_card = _run([str(inspect), str(residual)], cwd=fixture)
        assert residual_card.returncode == 1, residual_card.stderr
        assert "U2_GOOD: missing (unverified binding-like text observed)" in residual_card.stdout
        assert "U2_REPAIR_ME: missing" in residual_card.stdout
        assert "U2_LATER: missing" in residual_card.stdout
        assert 'first_failure: transcript_line=17 Exception: Failure "TAC_PROOF: Unsolved goals".' in (
            residual_card.stdout
        )
        assert "last_binding_like_text: U2_GOOD (unverified, transcript_line=13)" in residual_card.stdout
        assert "U2_REPAIR_ME: failed" not in residual_card.stdout
        assert "not_reached" not in residual_card.stdout
        assert ("binding_note: named theorem probes are missing; unverified printed theorem text "
                "does not establish which binding failed") in (
            residual_card.stdout
        )
        unattributed_card = _run([str(inspect), str(unattributed)], cwd=fixture)
        assert unattributed_card.returncode == 1, unattributed_card.stderr
        assert "U2_GOOD: missing" in unattributed_card.stdout
        assert "U2_REPAIR_ME: missing" in unattributed_card.stdout
        assert "U2_LATER: missing" in unattributed_card.stdout
        assert "last_binding_like_text:" not in unattributed_card.stdout
        assert ("binding_note: named theorem probes are missing; unverified printed theorem text "
                "does not establish which binding failed") in (
            unattributed_card.stdout
        )
        bounded = _run([str(inspect), str(success_dir), "--grep", "Exception"], cwd=fixture)
        assert bounded.returncode == 0, bounded.stderr
        assert "Exception: bounded failure" in bounded.stdout
        assert "first line" not in bounded.stdout

    print(
        "public_surface_contract=passed tools=prove,inspect,smoke "
        "profiles=7 prove_help_modules<=8 inspect_help_modules<=6 receipts=7 onboarding_docs=3"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
