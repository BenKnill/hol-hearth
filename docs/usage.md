# Authoring a substantial proof project

## Work on a leaf; retain a complete milestone

Choose the smallest source that contains the obligation you are changing.
Its dependencies should state the reusable mathematics explicitly. After an edit:

```sh
./hearth prove /ABS/project/proofs/leaf.ml --profile light \
  --timeout 120 --run-root /ABS/project/runs
./hearth inspect /ABS/project/runs --binding TARGET_THEOREM
```

Use the project's complete entrypoint and a larger explicit budget for a
milestone. The acoustic interface acceptance case uses 1500 seconds. This
does not mean every authoring leaf should replay that complete certificate.

Paths may be absolute or relative to the calling directory. Existing run roots
preserve attempts in separate timestamped directories; inspect chooses the
newest receipt. Pass an individual attempt directory to inspect an older result.

## Watch the project

```sh
./hearth prove /ABS/project/proofs/leaf.ml --loop --profile light \
  --timeout 120 --run-root /ABS/project/runs
```

The watcher uses the same recorded replay as the command above. It tracks
literal transitive source and artifact dependencies through the shared loader
scanner. A dependency-only edit, removal or restoration triggers another check.
Changes during evaluation mark its result stale and schedule the stable next
revision. It never launches overlapping attempts within one watch session.

Each evaluation gets a durable receipt. The watcher prints its session run
directory; inspect that directory to select the latest attempt.
Ctrl-C cancels the owned replay and preserves receipts. The shared broker may
remain idle for reuse. No separate evaluator build is needed.

A 30-second source still takes about 30 seconds after a save. Use a small leaf,
split unnecessary imports out of the leaf, and select the appropriate
preloaded basis. Hearth does not silently build project snapshots or reuse
an arbitrary source prefix.

## Multi-file sources

Use ordinary literal HOL loaders:

```ocaml
needs "support/definitions.ml";;
needs "support/lemmas.ml";;
```

A relative import resolves against its declaring file's directory, with the
configured HOL source and declared logical roots supplying library imports.
Nested imports and spaces in filenames are supported. Dynamic or ambiguous
loaders are refused when exact input capture cannot be established.

An error in an imported file rejects the complete source, including when main
continues and some of its named theorems succeed. The receipt includes the
dependency closure identity. Imported declarations are not all independently
selected named probes; inspection describes this scope.

## Read the result you need

```sh
./hearth inspect /ABS/project/runs --binding TARGET_THEOREM
./hearth inspect /ABS/project/runs --verbose
./hearth inspect /ABS/project/runs --json
./hearth inspect /ABS/project/runs --tail 40
```

The exact binding view includes its recorded status and original source span.
Verbose lists all discovered entrypoint bindings and captured input identities.
JSON exposes the complete receipt for agents. An unrecorded binding is reported
as unrecorded; it is not inferred absent from the mathematical basis.

Failure inspection includes a bounded exception block. Compiler locations in
generated evaluation files are diagnostic coordinates, not editable source
locations. The prior successful-looking transcript binding does not identify
the next failed theorem. A raw goal or printed theorem is not a nonce-bound
binding check.

## Find facts and examine a difficult goal

Write exploratory HOL in a separate source that imports the same helpers:

```ocaml
search [name "ITER"];;
print_thm ITER;;
```

Run it through ordinary prove and inspect its transcript with --tail or --grep.
There is no separate profile theorem-query execution path.

For tactic diagnosis, use HOL's ordinary `g` and `e` commands in that scratch
source and inspect the printed assumptions and residual goals. Then put the
finished proof into a named `let TARGET = prove (...)` binding and check that
binding. Successful execution of a scratch source does not mean its interactive
goals are solved.

Recorded replay captures bounded residual goals (assumptions and conclusion)
when a tactic returns unsolved subgoals to the existing `prove`. Read them with
`hearth inspect RUN --verbose` or the structured `proof_diagnostics` field in
`--json`. If a tactic raises before returning, the diagnostic instead labels
its original input; intermediate subgoals are unavailable. These are diagnostic
snapshots, never theorem probes. They do not change source acceptance.

The disposable child compiles source with location information. A unique compiler
call site can identify an entrypoint's failing literal `let NAME = prove (...)`
without guessing from the previous printed theorem. Unsupported call sites,
imported bindings, and caught failures followed by a different error remain
unattributed. Exact source bytes are retained after the generated prefix.

Diagnostics are bounded to eight failure events, eight goals per event, sixteen
assumptions per goal, and about 2 KiB per term. The ordinary HOL parser and
original `prove` still execute the source and validate the resulting theorem.
Custom redefinitions of `prove` and closures captured before the diagnostic
wrapper may bypass capture. A caught failure may have diagnostics even if the
source subsequently completes; inspect labels that case.

## Runtime and evidence

`./hearth profiles` lists recipes, not installed environments.
Use an explicit known profile for project work and
`./hearth status --profile light` to inspect queueing. Effective capacity
reflects the physical broker; its processes and sockets are not proof slots.

The timeout is an explicit attempt budget. Queue wait is reported separately.
The broker response deadline has a 15-second allowance with a 30-second minimum;
a controller timeout does not establish an exact evaluator cutoff.

A complete-source result and named kernel probes are warm authoring evidence.
They are distinct from process transport, diagnostic text, native execution,
and independent publication replay. Review the actual theorem assumptions.

HOL source is executable OCaml. Run source you trust; a fresh child isolates
proof state and is not an operating-system security sandbox.
