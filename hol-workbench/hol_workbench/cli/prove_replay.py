"""One fresh receipt-producing warm replay for ``prove SOURCE``."""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
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
    parser.add_argument("--basis", metavar="FILE.ml",
                        help="reuse a checked project basis imported by literal needs; prepared once per identical "
                             "inputs in the shared per-user cache")
    parser.add_argument("--basis-cache-root", metavar="DIR",
                        help="keep prepared bases under DIR/.project-bases instead of the shared "
                             "~/.cache/hol-hearth/project-bases")
    parser.add_argument("--run-root", default=None)
    parser.add_argument("--timeout", type=float, default=120.0)
    add_progress_argument(parser)
    parsed = parser.parse_args(args)
    source = parsed.source or parsed.positional_source
    if not source:
        parser.error("SOURCE.ml is required")
    if parsed.source and parsed.positional_source:
        parser.error("specify SOURCE.ml once")
    if parsed.basis_cache_root and not parsed.basis:
        parser.error("--basis-cache-root requires --basis")
    if not math.isfinite(parsed.timeout) or parsed.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    parsed.source = source
    return parsed


def _settled_source_sha256(source: Path, *, attempts: int = 20, interval: float = 0.1) -> tuple[str | None, float]:
    """Digest the source once two consecutive reads agree.

    Editors and the macOS-to-guest file sync write in stages. A digest taken
    mid-write is refused later as a source pin mismatch; waiting for the bytes
    to settle removes that race without weakening the pin.
    """
    previous = sha256_file(source)
    waited = 0.0
    for _ in range(attempts):
        time.sleep(interval)
        waited += interval
        current = sha256_file(source)
        if current == previous:
            return current, waited
        previous = current
    return previous, waited


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
        basis = resolve_authoring_source(options.basis, legacy_cwd=command_cwd).source if options.basis else None
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
    basis_cache_root = resolve_authoring_run_root(
        options.basis_cache_root, legacy_cwd=command_cwd, source_resolution=resolution,
    ) if options.basis_cache_root else None
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
    expected_sha256, settle_seconds = _settled_source_sha256(source)
    short = short_sha256(expected_sha256)
    if expected_sha256 is None or short is None:
        print(f"prove: source digest unavailable: {source}", file=sys.stderr)
        return 2
    if settle_seconds > 0.15:
        print(f"SOURCE: waited {settle_seconds:.1f}s for the file to stop changing before pinning its digest",
              flush=True)
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
            basis_source=basis,
            run_root=run_root,
            basis_cache_root=basis_cache_root,
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
        if recorded.get("source_preflight_status") in {"source_changed_during_capture", "source_pin_refused"}:
            current = short_sha256(sha256_file(source))
            print("SOURCE CHANGED: the file was rewritten between the pinned digest and the read for evaluation "
                  f"(pinned sha={short}, now sha={current or 'unavailable'}); no HOL ran and no profile was "
                  "restored. An editor save or the macOS-to-guest sync landed mid-capture.", flush=True)
            print("NEXT: rerun the same prove command; the receipt above records only the refusal.", flush=True)
        elif recorded.get("transport_status") in {"timeout", "interrupted", "cancelled"}:
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
        print("LEAF RECEIPT: unavailable; see the project basis preparation or refusal above"
              if basis is not None else "RECEIPT: unavailable; replay did not reach recorded evaluation", file=sys.stderr)
        print("NEXT: review the refusal details above, resolve the cause, then rerun prove SOURCE", file=sys.stderr)
    return status
