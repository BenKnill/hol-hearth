#!/usr/bin/env python3
"""Small public front door for HOL Workbench authoring and replay."""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEVELOPER_ONLY_COMMANDS = frozenset(
    {
        "audit",
        "cleanup",
        "continue",
        "dev",
        "evidence",
        "explain",
        "fix",
        "frontier",
        "profile-status",
        "scratch",
        "suggest",
    }
)
DEVELOPER_ONLY_OPTIONS = frozenset({"--all", "--audit", "--final", "--isolated", "--race-mode", "--sterile"})


def _source_request(args: list[str]) -> bool:
    return any(arg.endswith(".ml") or arg == "--source" or arg.startswith("--source=") for arg in args)


def main(
    argv: list[str] | None = None,
    *,
    script_dir: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    holdir: str | os.PathLike[str] | None = None,
    scratch_pool_size: str | None = None,
) -> int:
    """Dispatch only the two source modes and the read-only profile listing."""

    del holdir, scratch_pool_size
    args = list(sys.argv[1:] if argv is None else argv)
    if any(arg in {"-V", "--version"} for arg in args):
        print("prove: use smoke for the installed Hearth identity", file=sys.stderr)
        return 2
    if sys.platform != "linux":
        print("prove: Linux-only runtime; no proof run was started", file=sys.stderr)
        return 2

    scripts = str(Path(script_dir).resolve() if script_dir is not None else Path(__file__).resolve().parents[2] / "bin")
    working_directory = str(Path(cwd).resolve()) if cwd is not None else os.getcwd()

    if args and args[0] in PUBLIC_VERBS:
        # Verbs own their options (--source, --all, --json); none of them starts a proof.
        return PUBLIC_VERBS[args[0]](args[1:], scripts, working_directory)
    if args and args[0] == "watch":
        from hol_workbench.cli import prove_loop

        return prove_loop.main([*args[1:], "--loop"], script_dir=scripts, cwd=working_directory)

    if args and args[0] in DEVELOPER_ONLY_COMMANDS:
        print(
            f"prove {args[0]}: developer-only; public prove is the Linux warm-profile source loop",
            file=sys.stderr,
        )
        return 2
    if any(arg in DEVELOPER_ONLY_OPTIONS for arg in args):
        print(
            "prove: developer-only option; public prove is the Linux warm-profile source loop",
            file=sys.stderr,
        )
        return 2
    if "--loop" in args:
        from hol_workbench.cli import prove_loop

        return prove_loop.main(args, script_dir=scripts, cwd=working_directory)
    if not args or args[0] in {"-h", "--help"}:
        from hol_workbench import prove_help

        return prove_help.main(["public"])
    if args[0] == "help":
        if len(args) == 1 or args[1] in {"-h", "--help"}:
            from hol_workbench import prove_help

            return prove_help.main(["public"])
        if args[1] == "expert":
            print("prove help expert: developer-only; use the Hearth development lane", file=sys.stderr)
        else:
            print(f"prove help: unknown topic {args[1]!r}", file=sys.stderr)
        return 2
    if _source_request(args):
        from hol_workbench.cli import prove_replay

        return prove_replay.main(args, script_dir=scripts, cwd=working_directory)
    print(
        "prove: expected SOURCE.ml [--run-root], SOURCE.ml --loop, or one of "
        + ", ".join(sorted([*PUBLIC_VERBS, "watch"])),
        file=sys.stderr,
    )
    return 2


def _verb_profiles(args: list[str], scripts: str, cwd: str) -> int:
    from hol_workbench.cli import prove_profiles

    return prove_profiles.main(["profiles", scripts, *args])


def _verb_status(args: list[str], scripts: str, cwd: str) -> int:
    from hol_workbench.cli import prove_runtime_status

    return prove_runtime_status.main(args, script_dir=scripts)


def _verb_doctor(args: list[str], scripts: str, cwd: str) -> int:
    from hol_workbench.cli import prove_doctor

    return prove_doctor.main(args, script_dir=scripts)


def _verb_cancel(args: list[str], scripts: str, cwd: str) -> int:
    from hol_workbench.cli import prove_cancel

    return prove_cancel.main(args, script_dir=scripts, cwd=cwd)


def _verb_basis(args: list[str], scripts: str, cwd: str) -> int:
    from hol_workbench.cli import prove_basis

    return prove_basis.main(args, script_dir=scripts, cwd=cwd)


PUBLIC_VERBS = {
    "profiles": _verb_profiles,
    "status": _verb_status,
    "doctor": _verb_doctor,
    "cancel": _verb_cancel,
    "basis": _verb_basis,
}


if __name__ == "__main__":
    raise SystemExit(main())
