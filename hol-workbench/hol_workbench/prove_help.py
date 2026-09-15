"""User-facing help text for ``hol-workbench/bin/prove``."""

import sys

PUBLIC_HELP = """usage:
  hol-workbench/bin/prove /ABS/SOURCE.ml [--run-root /ABS/runs] [--profile PROFILE] [--timeout SECONDS]
  hol-workbench/bin/prove /ABS/SOURCE.ml --loop [--profile PROFILE] [--timeout SECONDS]
  hol-workbench/bin/prove profiles
  hol-workbench/bin/prove status [--watch|--json]
  hol-workbench/bin/prove doctor [--profile PROFILE|--json]

--loop and --run-root are mutually exclusive. Recorded --run-root is the
ordinary one-shot check; --loop is a dedicated live session (edit in another
window). Neither is publication evidence.
After either mode, a published-profile broker and idle monitor may remain.
That is the warm seat, not a leaked prove child. Do not kill it.

Recorded replay:
  Ordinary prove evaluates the current source in a fresh warm child and writes
  one compact receipt under --run-root. It does not reuse live-loop state and is
  the recorded proof check; independent publication replay is separate.
  If execution stops before nonce-bound probes, bindings remain missing;
  binding-like transcript text is unverified and cannot identify the residual.
  --timeout SECONDS defaults to 120. For longer sources, choose a larger budget.
  The mechanical broker response deadline includes a 15-second allowance
  (minimum 30 seconds); a controller response timeout is transport evidence,
  not a HOL theorem failure or an evaluator deadline measurement.

Authoring loop:
  --loop restores one published profile once, then sends each stable saved edit
  as source bytes to disposable HOL children. It creates no per-edit evidence
  files. OK means HOL Light evaluated the complete source successfully. A child
  killed by a signal is reported with its explicit name and number; Ctrl-C stops
  the session. Loop children and the session socket must go away; the published
  warm seat stays.

Runtime:
  Linux-only. Both source modes require a published warm CRIU profile; neither
  mode cold-loads, rebuilds, repairs, or exposes backend controls.
  Source and run-root paths are absolute paths visible inside Linux.

Profiles:
  light, heavy, probability, s2n-arm, s2n-arm-light, s2n-arm-mlkem, or
  s2n-x86. The source route infers one unless --profile is explicit. light is
  the ordinary arithmetic profile; s2n-arm-light combines it with the ARM
  basis and should be selected explicitly for mixed work; s2n-arm-mlkem is
  only for ML-KEM/ML-DSA work.

Status:
  status reads live profile owners and admission demand without starting HOL,
  restoring a profile, or changing worker state. --watch repeats the snapshot.
  doctor checks configuration, compatibility, saturation, and stale ownership
  without repairing, rebuilding, reloading, killing, or starting HOL.
"""


def help_text(topic: str = "public") -> str:
    if topic in {"public", "default", ""}:
        return PUBLIC_HELP
    raise ValueError(f"unknown prove help topic: {topic}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    topic = args[0] if args else "public"
    try:
        sys.stdout.write(help_text(topic))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
