"""Development-only raw HOL entry point from an installed frozen client."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from hol_workbench.machine_client import (
    AUTHORITY_ENV,
    IDENTITY_SCHEMA,
    WARM_HOL_ESCAPE_MODE,
    WARM_HOL_INSTALL_SCHEMA,
    locked_checkout_python,
)


def _installed_manifest(client_root: Path) -> dict:
    path = client_root / "warm-hol-install.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(
            "warm-hol refused: this development escape hatch must be run from the installed frozen client",
            file=sys.stderr,
        )
        raise SystemExit(78) from None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warm-hol refused: installed client manifest is unreadable: {path}: {exc}", file=sys.stderr)
        raise SystemExit(78) from exc
    if manifest.get("schema") != WARM_HOL_INSTALL_SCHEMA or manifest.get("development_only") is not True:
        print(f"warm-hol refused: installed client manifest is invalid: {path}", file=sys.stderr)
        raise SystemExit(78)
    installed_client = Path(str(manifest.get("installed_client") or "")).expanduser().resolve()
    if installed_client != client_root:
        print(
            f"warm-hol refused: installed client is bound to {installed_client}, not {client_root}",
            file=sys.stderr,
        )
        raise SystemExit(78)
    expected_files = manifest.get("files")
    if not isinstance(expected_files, dict) or not expected_files:
        print(f"warm-hol refused: installed client manifest has no frozen file inventory: {path}", file=sys.stderr)
        raise SystemExit(78)
    actual_files = {
        item.relative_to(client_root).as_posix() for item in client_root.rglob("*") if item.is_file() and item != path
    }
    if actual_files != set(expected_files):
        print("warm-hol refused: installed frozen client file inventory has changed", file=sys.stderr)
        raise SystemExit(78)
    for relative, expected in expected_files.items():
        candidate = client_root / relative
        if candidate.is_symlink() or not candidate.is_file():
            print(f"warm-hol refused: frozen client file is unsafe or missing: {relative}", file=sys.stderr)
            raise SystemExit(78)
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        mode = candidate.stat().st_mode & 0o777
        if not isinstance(expected, dict) or digest != expected.get("sha256") or mode != expected.get("mode"):
            print(f"warm-hol refused: frozen client file has changed: {relative}", file=sys.stderr)
            raise SystemExit(78)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warm-hol",
        description="Run one source file as raw HOL from a frozen installed warm-profile client.",
        epilog=(
            "Development escape hatch only. This bypasses live Workbench checkout availability, "
            "but its output is never proof, semantic-probe, replay, or audit evidence."
        ),
    )
    parser.add_argument("profile", help="installed warm profile, for example light, heavy, or probability")
    parser.add_argument("source", type=Path, help="HOL Light source file to evaluate")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--idle-timeout", type=float)
    parser.add_argument("--transcript", type=Path)
    parser.add_argument("--run-root", type=Path, default=Path("runs/warm-hol"))
    return parser


def main(argv: list[str] | None = None) -> int:
    client_root = Path(__file__).resolve().parents[2]
    manifest = _installed_manifest(client_root)
    args = _parser().parse_args(argv)
    source_checkout = Path(str(manifest.get("source_checkout") or "")).expanduser().resolve()
    try:
        checkout_python = locked_checkout_python(source_checkout)
    except Exception as exc:
        print(f"warm-hol refused: {exc}", file=sys.stderr)
        raise SystemExit(78) from exc
    revision = str(manifest.get("revision") or "unknown")
    identity = {
        "schema": IDENTITY_SCHEMA,
        "mode": WARM_HOL_ESCAPE_MODE,
        "authoritative": False,
        "checkout": str(manifest.get("source_checkout") or ""),
        "installed_client": str(client_root),
        "revision": revision,
        "branch": str(manifest.get("branch") or "main"),
        "remote_ref": str(manifest.get("remote_ref") or "origin/main"),
        "remote_revision": revision,
        "remote_url": str(manifest.get("remote_url") or ""),
        "clean": True,
        "live_checkout_validated": False,
        "evidence": "warm_development_only",
        "launcher": str(Path(sys.argv[0]).expanduser().absolute()),
    }
    env = os.environ.copy()
    env[AUTHORITY_ENV] = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # The frozen client intentionally does not carry a virtual environment.
    # Never inherit either this launcher's interpreter or a caller override:
    # the manifest-bound source checkout owns the child interpreter.
    env["HOL_WORKBENCH_PYTHON"] = str(checkout_python)
    # Legacy absolute HOL literals are rebound only from the Linux machine
    # runtime configuration inside the frozen client. Never transport a
    # caller-provided host coordinate into that authority decision.
    env.pop("HOL_WORKBENCH_HOST_HOLDIR", None)

    command = [
        str(client_root / "bin" / "orbstack-criu"),
        "vanilla",
        args.profile,
        "--source",
        str(args.source.expanduser().resolve()),
        "--run-root",
        str(args.run_root.expanduser().resolve()),
    ]
    if args.timeout is not None:
        command.extend(["--timeout", str(args.timeout)])
    if args.idle_timeout is not None:
        command.extend(["--idle-timeout", str(args.idle_timeout)])
    if args.transcript is not None:
        command.extend(["--transcript", str(args.transcript.expanduser().resolve())])

    print(
        f"WARM HOL ESCAPE: profile={args.profile} frozen_client={revision[:12]} live_checkout=not_consulted",
        file=sys.stderr,
    )
    print(
        "EVIDENCE: development-only raw warm HOL; never proof, semantic-probe, replay, or audit evidence",
        file=sys.stderr,
    )
    os.execvpe(command[0], command, env)
    return 70


if __name__ == "__main__":
    raise SystemExit(main())
