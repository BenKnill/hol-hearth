"""Help for the public HOL Hearth proof command."""
PUBLIC_HELP = """usage:
  hearth prove SOURCE.ml [--run-root DIR] [--profile PROFILE] [--timeout SECONDS]
  hearth prove SOURCE.ml --loop [--run-root DIR] [--profile PROFILE] [--timeout SECONDS]
  hearth profiles [--all | PROFILE [--verbose]]
  hearth status [--profile PROFILE] [--watch|--json]
  hearth doctor [--profile PROFILE|--all-profiles] [--json]

Recorded replay:
  Evaluate the complete project in a fresh child of a warm HOL profile.
  Keep exact source/dependency hashes, completion and named theorem checks.
  Existing run roots retain each attempt in a new timestamped directory.
  --timeout defaults to 120 seconds; set an explicit budget for long proofs.
  Queue wait is separate from proof time. A controller response deadline
  includes a 15-second allowance (minimum 30 seconds).

Authoring loop:
  --loop watches source and transitive dependencies and runs the same recorded
  check after stable edits. It keeps a receipt for every attempt, including failures.
  Edits during evaluation mark that result stale and schedule another check.
  A long proof still takes its full evaluation time after an edit. Work on a
  small leaf and keep expensive stable dependencies in the selected warm basis.
  Ctrl-C cancels the owned replay. The shared warm seat stays; do not kill it.

Inspection:
  hearth inspect DIR --binding THEOREM
  hearth inspect DIR --verbose
  hearth inspect DIR --json
  Failure details include a bounded exception block. Transcript/goal text is
  diagnostic; successful OCaml execution alone does not finish interactive goals.

Runtime:
  Linux and an existing compatible HOL/CRIU profile. Source and run-root paths
  may be absolute or relative to the calling directory. No cold-load fallback.
  light, heavy, probability, s2n-arm, s2n-arm-light, s2n-arm-mlkem, s2n-x86.
  Prefer an explicit profile for project work. Optional recipes are not installed
  merely because they are listed. Independent publication replay is a later step.

Theorem exploration:
  Use ordinary HOL source, e.g. search [name "ITER"];; or print_thm ITER;;
  and inspect its recorded transcript. There is no second lookup execution path.
"""

def main(argv=None):
    print(PUBLIC_HELP, end="")
    return 0
