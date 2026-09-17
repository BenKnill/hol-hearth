"""Watch a proof project using the ordinary receipt-producing replay path."""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from hol_workbench.authoring_source_path import resolve_authoring_run_root, resolve_authoring_source
from hol_workbench.cli.inspect import _read_json, _replay_receipt
from hol_workbench.cli.prove_replay import _profile
from hol_workbench.cli.public_commands import public_command
from hol_workbench.cli.published_profile import PublishedWarmProfile, resolve_published_warm_profile
from hol_workbench.cli.replay_progress import add_progress_argument
from hol_workbench.ids import run_id
from hol_workbench.source_execution_plan import capture_source_dependency_closure


def project_revision(source: Path, profile: PublishedWarmProfile) -> str:
    """The existing loader scanner owns dependency discovery and content identity."""
    try:
        closure, _ = capture_source_dependency_closure(
            source, profile_cwd=profile.cwd,
            legacy_holdir_roots=profile.legacy_holdir_roots,
            logical_source_root_declarations=profile.logical_source_roots,
        )
        return str(closure["strict_sha256"])
    except (OSError, RuntimeError, ValueError, SystemExit) as exc:
        # Retry discovery after edits, including creation of a previously missing import.
        # This is a scheduling key, never proof evidence.
        try:
            payload = source.read_bytes()
        except OSError:
            payload = b""
        return "unavailable:" + hashlib.sha256(payload + str(exc).encode()).hexdigest()


def _parse(args: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hearth prove", description=__doc__)
    parser.add_argument("positional_source", nargs="?")
    parser.add_argument("--source")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--profile")
    parser.add_argument("--run-root")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--poll", type=float, default=1)
    add_progress_argument(parser)
    result = parser.parse_args(args)
    if not (result.source or result.positional_source) or (result.positional_source and result.source):
        parser.error("specify SOURCE.ml once")
    result.source = result.source or result.positional_source
    if not math.isfinite(result.timeout) or result.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if not math.isfinite(result.poll) or result.poll < 0.1:
        parser.error("--poll must be finite and at least 0.1 seconds")
    return result


def main(args: list[str], *, script_dir: str | os.PathLike[str], cwd: str | os.PathLike[str]) -> int:
    options = _parse(args)
    scripts = Path(script_dir).resolve()
    working = Path(cwd).resolve()
    try:
        resolution = resolve_authoring_source(options.source, legacy_cwd=working)
        source = resolution.source
        if not source.is_file():
            raise ValueError(f"source is not a file: {source}")
        name = _profile(source, options.profile, scripts)
        profile = resolve_published_warm_profile(scripts, name)
        root = resolve_authoring_run_root(options.run_root, legacy_cwd=working,
                                         source_resolution=resolution) / run_id("watch")
        root.mkdir(parents=True, exist_ok=False)
    except (OSError, RuntimeError, ValueError, SystemExit) as exc:
        print(f"WATCH: cannot start: {exc}", file=sys.stderr)
        return 2

    print(f"WATCH: {source}\nPROFILE: {name}\nRUNS: {root}", flush=True)
    print("Each stable source or dependency edit runs a fresh recorded check. Ctrl-C stops.", flush=True)
    print(f"DETAILS: {public_command('inspect', root)}", flush=True)
    process: subprocess.Popen[bytes] | None = None
    last_attempt: str | None = None
    pending: str | None = None
    launched: str | None = None
    previous_receipt: Path | None = None
    changed = False
    try:
        while True:
            revision = project_revision(source, profile)
            if process is not None:
                if revision != launched and not changed:
                    print("CHANGED: current edits are not checked; checking them after the running attempt.", flush=True)
                    changed = True
                status = process.poll()
                if status is not None:
                    path = _replay_receipt(root)
                    receipt = _read_json(path) if path and path != previous_receipt else {}
                    # Use the actual captured input identity, not a pre-launch observation.
                    last_attempt = receipt.get("source_dependency_closure_sha256") or launched
                    if revision != last_attempt:
                        print("STALE: the finished result is for earlier inputs; another check is pending.", flush=True)
                    else:
                        print(f"CURRENT: {'accepted' if status == 0 else 'not accepted'}; "
                              "see the recorded result above.", flush=True)
                    process = None
                    pending = None
                time.sleep(options.poll)
                continue
            if revision != last_attempt:
                if pending == revision:
                    previous_receipt = _replay_receipt(root)
                    launched = revision
                    changed = False
                    print("CHECKING: source and transitive dependencies; previous result is not current.", flush=True)
                    process = subprocess.Popen(
                        [str(scripts / "prove"), str(source), "--profile", name,
                         "--timeout", str(options.timeout), "--run-root", str(root),
                         "--progress-interval", str(options.progress_interval)],
                        cwd=working, start_new_session=True,
                    )
                else:
                    pending = revision
            time.sleep(options.poll)
    except KeyboardInterrupt:
        print("\nWATCH: stopping the owned replay; the shared warm seat stays.", flush=True)
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGINT)
            # The ordinary replay owns cancellation and child cleanup. Do not invent
            # a second broker cleanup protocol or kill a shared seat from the watcher.
            while process.poll() is None:
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    continue
                except KeyboardInterrupt:
                    print("WATCH: waiting for replay cancellation.", flush=True)
        print(f"WATCH: stopped (SIGINT, exit 130). Receipts: {root}", flush=True)
        return 130
