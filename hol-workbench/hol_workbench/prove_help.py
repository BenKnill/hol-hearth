"""Help for the public HOL Hearth proof command."""
PUBLIC_HELP = """usage:
  hearth prove SOURCE.ml [--run-root DIR] [--profile PROFILE] [--timeout SECONDS] [--json|--verbose]
  hearth prove SOURCE.ml --loop [--run-root DIR] [--profile PROFILE] [--timeout SECONDS]
  hearth watch SOURCE.ml ...          same as prove --loop
  hearth profiles [--all | PROFILE [--verbose]]
  hearth status [--profile PROFILE] [--watch|--json]
  hearth doctor [--profile PROFILE|--all-profiles] [--json]
  hearth cancel ATTEMPT_ID | --source FILE.ml [--profile PROFILE] [--wait SECONDS]
  hearth basis [list|retire KEY...|retire --all] [--cache-root DIR]

Recorded replay:
  Evaluate the complete project in a fresh child of a warm HOL profile.
  Keep exact source/dependency hashes, completion and named theorem checks.
  Existing run roots retain each attempt in a new timestamped directory.
  The result is one verdict line, the receipt path and the next command:
    PASSED leaf.ml: 3/3 bindings proved, 0 new axioms, eval 4.2s (s2n-arm)
    FAILED leaf.ml at STEP_LEMMA (line 41): Exception: Failure "ARITH_RULE ..."
    INCOMPLETE leaf.ml: timeout after 900s; not a disproof (s2n-arm)
    REFUSED leaf.ml: source changed during capture (...); no HOL ran
  --json prints the same summary as JSON (about 25 fields, documented in
  docs/usage.md); --verbose adds the runtime's tagged lines. The receipt file
  keeps every observation under its verdict block.
  --timeout defaults to 900 seconds; set an explicit budget for long proofs.
  Queue wait is separate from proof time. A controller response deadline
  includes a 15-second allowance (minimum 30 seconds).
  Progress goes to stderr every 15 seconds during a replay; set
  --progress-interval SECONDS (minimum 1), or 0 to disable. Loop uses it too.
  Known phases, elapsed time and request countdowns do not measure tactic
  progress or predict completion. Queue time does not consume the proof budget.
  Ctrl-C cancels the owned child and releases admission; the warm seat stays.
  Timeout is an incomplete check, not a disproof. Inspect its recorded attempt.

Project dependency reuse:
  --basis FILE.ml prepares a completed literal needs dependency once on the
  selected warm profile. Reuse requires identical source, dependency and ELF
  bytes. Preparation and full leaf replay each retain a receipt and use the
  explicit timeout separately. Each leaf runs in a fresh child; --loop uses
  this same route. Prepared bases live in ~/.cache/hol-hearth/project-bases,
  keyed by input identity, so any run root reuses them; hearth basis lists
  or retires them (each live basis is a resident HOL process).

Cancelling:
  hearth status prints each active or queued attempt id. hearth cancel sends
  that attempt the same SIGINT as Ctrl-C after checking its process identity,
  so a shell pattern never signals the wrong process. Receipts are retained.

Authoring loop:
  --loop watches source and transitive dependencies and runs the same recorded
  check after stable edits. It keeps a receipt for every attempt, including failures.
  Edits during evaluation mark that result stale and schedule another check.
  A long proof still takes its full evaluation time after an edit. Work on a
  small leaf and keep expensive stable dependencies in the selected warm basis.
  Ctrl-C cancels the owned replay. The shared warm seat stays; do not kill it.

Inspection:
  hearth inspect DIR                  verdict card: bindings, failure, goal at the failing step, next command
  hearth inspect DIR --binding THEOREM
  hearth inspect DIR --json           the receipt summary; add --verbose for the complete receipt
  hearth inspect DIR --verbose        every recorded field
  Transcript/goal text is diagnostic; successful OCaml execution alone does
  not finish interactive goals.

Runtime:
  Linux and an existing compatible HOL/CRIU profile. Source and run-root paths
  may be absolute or relative to the calling directory. No cold-load fallback.

What an accepted receipt establishes:
  The same HOL Light kernel evaluated the exact source bytes in a fresh child
  forked from the published profile; the profile recipe (profiles/NAME.ml)
  and its loaded-file inventory are hash-verified; every discovered named
  binding was probed in the kernel; the axiom count did not grow. That is a
  complete check of the theorem. A from-scratch replay of the same recipe
  reproduces the same environment and adds only independence from the CRIU
  image; run one with hearth export-replay when the image itself is in
  question or an outside reader needs it, not as a milestone gate.
  light, heavy, probability, s2n-arm, s2n-arm-light, s2n-arm-mlkem, s2n-x86.
  Prefer an explicit profile for project work. Optional recipes are not installed
  merely because they are listed. Recipes are the exact basis a warm child sees.

Theorem exploration:
  Use ordinary HOL source, e.g. search [name "ITER"];; or print_thm ITER;;
  and inspect its recorded transcript. There is no second lookup execution path.
"""

def main(argv=None):
    print(PUBLIC_HELP, end="")
    return 0
