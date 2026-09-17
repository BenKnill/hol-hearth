"""One fresh receipt-producing warm replay for ``prove SOURCE``."""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

from hol_workbench.authoring_source_path import (
    AuthoringSourcePathError,
    resolve_authoring_run_root,
    resolve_authoring_source,
)
from hol_workbench.cli.orbstack_criu_vanilla_artifacts import default_transcript_path
from hol_workbench.cli.prove_profiles import public_authoring_profile_names
from hol_workbench.cli.public_commands import public_command, replay_handoff
from hol_workbench.cli.published_profile import resolve_published_warm_profile
from hol_workbench.cli.published_profile_replay import run_published_warm_replay
from hol_workbench.cli.replay_progress import ReplayProgress, add_progress_argument
from hol_workbench.hashing import sha256_file, short_sha256
from hol_workbench.jsonio import read_json
from hol_workbench.proofs.profile_inference import infer_public_profile_for_source


def _parse(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="prove",
        description="Replay one source once from a fresh child of a published warm profile.",
    )
    parser.add_argument("positional_source", nargs="?")
    parser.add_argument("--source")
    parser.add_argument("--profile")
    parser.add_argument("--run-root", default=None)
    parser.add_argument("--timeout", type=float, default=120.0)
    add_progress_argument(parser)
    parsed = parser.parse_args(args)
    source = parsed.source or parsed.positional_source
    if not source:
        parser.error("SOURCE.ml is required")
    if parsed.source and parsed.positional_source:
        parser.error("specify SOURCE.ml once")
    if not math.isfinite(parsed.timeout) or parsed.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    parsed.source = source
    return parsed


def _profile(source: Path, explicit: str | None, script_dir: Path) -> str:
    public = public_authoring_profile_names(script_dir)
    if explicit:
        if explicit not in public:
            raise RuntimeError(f"profile {explicit!r} is not public; available: {', '.join(public)}")
        return explicit
    decision = infer_public_profile_for_source(source, public)
    if decision.profile is None:
        raise RuntimeError(decision.reason)
    return decision.profile


def main(
    args: list[str],
    *,
    script_dir: str | os.PathLike[str],
    cwd: str | os.PathLike[str],
) -> int:
    if sys.platform != "linux":
        print("prove replay is Linux-only", file=sys.stderr)
        return 2
    options = _parse(args)
    command_cwd = Path(cwd).expanduser().resolve()
    try:
        resolution = resolve_authoring_source(options.source, legacy_cwd=command_cwd)
    except AuthoringSourcePathError as exc:
        print(f"prove: {exc}", file=sys.stderr)
        return 2
    source = resolution.source
    if not source.is_file():
        print(f"prove: source is not a file: {source}", file=sys.stderr)
        return 2
    run_root = resolve_authoring_run_root(
        options.run_root,
        legacy_cwd=command_cwd,
        source_resolution=resolution,
    )
    scripts = Path(script_dir).expanduser().resolve()
    try:
        name = _profile(source, options.profile, scripts)
        profile = resolve_published_warm_profile(scripts, name)
    except (OSError, RuntimeError, SystemExit, ValueError) as exc:
        print(f"prove: {exc}", file=sys.stderr)
        return 2
    transcript = default_transcript_path(run_root, source)
    # Pin the banner digest privately: the replay refuses before HOL if the bytes it
    # reads are not these, so a printed sha always names the bytes that were checked.
    expected_sha256 = sha256_file(source)
    short = short_sha256(expected_sha256)
    if expected_sha256 is None or short is None:
        print(f"prove: source digest unavailable: {source}", file=sys.stderr)
        return 2
    print(f"REPLAY: profile={name} source={source} sha={short}", flush=True)
    with ReplayProgress(timeout=options.timeout, interval=options.progress_interval) as progress:
        status = run_published_warm_replay(
            profile,
            source,
            timeout=options.timeout,
            transcript=transcript,
            evidence_role="recorded_warm_replay",
            expected_source_sha256=expected_sha256,
            on_phase=progress.set_phase,
        )
    receipt = Path(f"{transcript}.json")
    if receipt.is_file():
        print(f"RECEIPT: {receipt}", flush=True)
        if status == 0:
            print(
                "SOURCE CHECK: passed; complete source evaluated and discovered named theorem bindings checked",
                flush=True,
            )
        recorded = read_json(receipt)
        if recorded.get("transport_status") in {"timeout", "interrupted", "cancelled"}:
            reason = "timeout" if recorded.get("transport_status") == "timeout" else "cancelled"
            print(f"INCOMPLETE: {reason}; this attempt did not complete the source check. "
                  "This is no conclusion about whether the theorem is true or false.", flush=True)
            print("NEXT: inspect this attempt's diagnostics, isolate the slow proof in a small "
                  "leaf, then rerun with an explicit budget.", flush=True)
            print(f"DETAILS: {public_command('inspect', receipt.parent, '--tail', '40')}", flush=True)
        else:
            for line in replay_handoff(run_root, succeeded=status == 0):
                print(line, flush=True)
    elif status == 0:
        print("prove: replay succeeded without its required receipt", file=sys.stderr)
        return 1
    else:
        print("RECEIPT: unavailable; replay did not reach recorded evaluation", file=sys.stderr)
        print("NEXT: review the refusal details above, resolve the cause, then rerun prove SOURCE", file=sys.stderr)
    return status
