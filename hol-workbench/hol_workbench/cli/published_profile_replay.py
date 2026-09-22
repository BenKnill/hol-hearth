"""Small shared route from one published warm profile to one fresh HOL child."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from hol_workbench.cli.published_profile import PublishedWarmProfile


def run_published_warm_replay(
    profile: PublishedWarmProfile,
    source: Path,
    *,
    timeout: float | None,
    transcript: Path | None,
    evidence_role: str = "warm_development_only",
    display_transcript: bool = False,
    expected_source_sha256: str | None = None,
    on_phase: Callable[[str], None] | None = None,
    basis_source: Path | None = None,
    run_root: Path | None = None,
    basis_cache_root: Path | None = None,
) -> int:
    """Evaluate once in a fresh child; persist only when a transcript is requested."""

    from hol_workbench.cli import orbstack_criu_restore, orbstack_criu_vanilla
    from hol_workbench.orbstack_idle_retirement import schedule_profile_retirement

    def replay(selected_source: Path, **overrides: object) -> int:
        options = dict(
            profile_root=profile.root,
            timeout=timeout,
            idle_timeout=None,
            restore=lambda: orbstack_criu_restore.main(
                [str(profile.root), "--published-shelf-only"],
                include_controller_attempt=False,
            ),
            transcript_output=transcript,
            logical_profile=profile.name,
            logical_capacity=None,
            profile_cwd=profile.cwd,
            legacy_holdir_roots=profile.legacy_holdir_roots,
            logical_source_root_declarations=profile.logical_source_roots,
            evidence_role=evidence_role,
            display_transcript=display_transcript,
            expected_source_sha256=expected_source_sha256,
            on_phase=on_phase,
        )
        options.update(overrides)
        return orbstack_criu_vanilla.run(source=selected_source, **options)

    try:
        if basis_source is not None:
            from hol_workbench.cli.project_basis_replay import run_project_basis_replay
            if run_root is None or transcript is None or evidence_role != "recorded_warm_replay":
                raise ValueError("project basis reuse requires a recorded replay and an explicit run root")
            return run_project_basis_replay(
                profile, source, basis_source=basis_source, run_root=run_root,
                replay=replay, timeout=timeout, on_phase=on_phase, transcript=transcript,
                cache_root=basis_cache_root, expected_source_sha256=expected_source_sha256,
            )
        return replay(source)
    finally:
        if (profile.root / "pool").is_dir():
            schedule_profile_retirement(profile.root, logical_profile=profile.name)
