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

The leaf's own proof still runs after each save. Use a small leaf and split
unnecessary imports out of it. Stable, completed imports can be retained with
the explicit project basis below.

## Reuse a completed project dependency

For a leaf that imports a substantial, stable dependency with literal `needs`:

```sh
./hearth prove /ABS/project/proofs/leaf.ml --profile s2n-arm-mlkem \
  --basis /ABS/project/proofs/completed_dependency.ml \
  --timeout 1500 --run-root /ABS/project/runs
```

On the first run, Hearth checks the dependency through ordinary recorded replay
on the existing profile. Only a complete source check with all discovered
bindings checked and an observed zero new axioms admits its retained HOL state.
It then checks the entire leaf in a fresh child of that state. Later invocations
with the same run root reuse it; `--loop` accepts the same option.

The requested timeout applies separately to preparation and leaf evaluation.
Both phases print and preserve their receipts. A failed preparation stops the
leaf. Leaf receipts identify the exact preparation receipt and inherited input
hashes. Inspection of that receipt describes what was prepared; it does not
claim that every transitive imported declaration had a separate named probe.

The cache identity includes the basis source, transitive source and ELF bytes,
selected shelf and current transport implementation. Changes require another
preparation. An unrelated file or a forced `loadt` cannot serve as the basis.
Ordinary admission capacity still applies to cached replays. At most two live
project bases are retained per run root; replacement retires only owned project
processes. Ctrl-C cancels the active child and preserves reusable warm state.

This reuses completed dependencies. It does not resume inside an unfinished
tactic or automatically find and splice a prefix of a changing source. The
shared profile and its snapshot are preserved.

## Multi-file sources

Use ordinary literal HOL loaders:

```ocaml
needs "support/definitions.ml";;
needs "support/lemmas.ml";;
```

A relative import resolves against its declaring file's directory first.
Within a Git checkout, bare paths such as `arm/proofs/helper.ml` then resolve
from that checkout's nearest repository root. Hearth captures those exact
sources and their transitive ELF inputs, even when the selected profile was
prepared in another checkout. A missing file inside a local directory such as
`arm/` is refused before evaluation; another checkout cannot fill the gap.

For exact relative ELF paths, Hearth can run from the captured package's
working directory. This lets ordinary imports define their ELF loaders before
the source uses them, including when those loaders were absent from the warm
profile. The source order and object bytes stay unchanged. Hearth selects this
transport only when every object resolves to its captured location and the
exact source closure has no native declarations. Native declarations or uncertain
artifact coordinates retain the existing ELF path wrappers. Receipts record
the selected `literal_elf_transport`; project-basis admission checks it too.

The configured HOL source and declared logical roots supply remaining library
imports, such as `Library/words.ml` when this checkout has no `Library/` tree.
For a narrower package boundary or a source archive without Git metadata, place
an ordinary `.hol-workbench-source-root` file at the intended package root;
the nearest such marker takes precedence. Without a marker or repository,
Hearth retains its bounded relative-import root inference. Nested imports and
spaces in filenames are supported. Dynamic or ambiguous loaders are refused
when exact input capture cannot be established.

Known file-loading routes around ordinary loaders are refused before execution,
including `Toploop.use_file`, `Topdirs` and `Dynlink` loading, and directory
changes such as `Sys.chdir` or `Unix.chdir`.
This applies to the entrypoint and its captured imports. Module-qualified loaders
such as `Hol.needs` and loaders under local module opens are also refused;
use ordinary literal `needs`, `loadt` or `loads` instead. The bounded lexical
scanner recognizes known references and simple aliases. Exact input identity
covers supported declared source loaders and ELF objects; it does not attest
arbitrary OCaml file I/O, subprocess effects, or source generated for in-memory
evaluation through APIs such as `Toploop.execute_phrase`. General effect
analysis is outside this contract: ordinary HOL libraries themselves define
interpreter and process helpers. This is not an operating-system sandbox.
Continue to run only source you trust.

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

The exact binding view includes its recorded status, probe strength, and original
source span. A literal statement's probe checks that the theorem's conclusion
matches the source quotation and that its hypotheses are empty. A computed
statement's probe checks only that the binding has type `thm`; inspection labels
its conclusion and hypotheses as unchecked. The recorded `proved` status alone
does not distinguish these checks. `--binding` and `--verbose` also show the exact
`verification_kind`; older receipts without it have unknown probe strength.
Verbose lists all discovered entrypoint bindings and captured input identities.
JSON exposes the complete, unchanged receipt, including each binding's recorded
`verification_kind`, for agents. An unrecorded binding is reported
as unrecorded; it is not inferred absent from the mathematical basis.

Failure inspection includes a bounded exception block. Compiler locations in
generated evaluation files are diagnostic coordinates, not editable source
locations. The prior successful-looking transcript binding does not identify
the next failed theorem. A raw goal or printed theorem is not a nonce-bound
binding check.

Inspect an unexpected runner failure before repeating expensive work. Preserve
its receipt, identify the cause, and make a concrete correction before retrying.
A harness defect should also gain a regression check. A planned negative control
followed by a repaired proof is a separate validation exercise; it is not an
automatic recovery path.

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
without guessing from the previous printed theorem. Attribution permits up to
eight blank output lines before a matching supported exception rendering;
substantive intervening output blocks the link. Unsupported call sites,
imported bindings, and caught failures followed by a different error remain
unattributed. This is diagnostic correlation, not proof of exception causality:
a silent catch followed immediately by the identical exception is indistinguishable
from propagation in the output. Exact source bytes are retained after the
generated prefix.

Diagnostics are bounded to eight failure events, eight goals per event, sixteen
assumptions per goal, and about 2 KiB per term. The ordinary HOL parser and
original `prove` still execute the source and validate the resulting theorem.
Custom redefinitions of `prove` and closures captured before the diagnostic
wrapper may bypass capture. A caught failure may have diagnostics even if the
source subsequently completes; inspect labels that case.

After timeout or cancellation, inspection can identify the last entered `prove`
call with no recorded return. This includes imported sources when their exact
captured bytes and compiler location agree. `running_at_interruption` names
where execution stopped; it does not declare that binding failed or was proved.
Unsupported or ambiguous call sites remain unknown.

Activity protocol v2 keeps recording after long sequences of completed calls.
Its receipt retains only the active stack, up to 64 calls; completed-call
history is not retained there. The raw transcript still grows with execution.
Deeper nesting emits an explicit overflow span and makes current attribution
unknown until the matching return restores the captured outer stack. Old v1
receipts retain their original terminal 8192-call capture limit. Neither
activity protocol changes source completion or theorem acceptance.

## Reopen a failed binding as a scratch proof

Prepare the same prefix and the selected goal without running HOL:

```sh
./hearth reopen /ABS/project/runs --binding TARGET_THEOREM \
  --out /ABS/project/scratch/TARGET_debug.ml
```

The output directory must already exist. Reopen writes a new scratch and its
adjacent `TARGET_debug.ml.reopen` directory. The directory contains the recorded
local dependencies, a prefix ending immediately before the selected binding,
and `origin.json` with the original receipt, source hash and byte spans.
The copied dependencies retain their relative layout and exact bytes. The
prefix retains the exact original bytes after a diagnostic provenance comment.

For portable local inputs, the scratch imports that prefix, states `g` with the
exact recorded quotation, and includes the complete original tactic in an
inactive comment. Copy selected
tactic steps into `e (...)` commands and run the printed ordinary prove command.
Inspect its transcript with `--tail 40` to see the assumptions and current goals.
Reopen executes nothing, and opening the goal does not establish the theorem.
It starts the original goal; it does not recover a failed tactic's residual.

A run root selects its newest receipt. To reopen an earlier failed attempt,
pass that attempt directory or its `transcript.log.json`. Selection is explicit:
`--binding` identifies a recorded, unproved entrypoint claim, not an inferred
failure location. If an earlier phrase or import itself fails, the generated
prefix can fail too; reopen does not execute or repair it.

Reopen refuses changed entrypoint or dependency bytes, duplicate or ambiguous
bindings, imported bindings, nonliteral goals and unsupported phrase syntax.
The supported form is a standalone `let NAME = [time] prove (quoted_goal,
tactic);;`. It uses the existing strict source lexer; HOL remains the parser and
execution authority.

Assembly sources, project-root imports, and captured HOL library imports keep
their original path coordinates. Set `--out` beside the original source, for
example `/ABS/project/arm/proofs/TARGET_debug.ml`. In this mode the scratch
contains the exact prefix directly. Reopen verifies every recorded dependency
and ELF hash before publishing it; its companion directory holds verified
reference copies. The next ordinary `prove` captures the current project and
runtime inputs again, including any edits since reopening. Those archived
copies are not substituted for the live project, and reopening does not
establish that a different warm profile has the same basis.

The printed command preserves the recorded timeout. When a recorded project
basis is imported before the selected goal, it also preserves `--basis` and the
existing run root. Ordinary prove still checks all basis identities before
reuse; a changed runtime or source may require a new recorded preparation.

Reopen supports acyclic, captured relative `needs`, `loadt` and `loads`. It
refuses mapped or unresolved imports, `#use`, and bare `load`. Existing scratch
files and companion directories are never overwritten. A refused command
leaves no generated files. Choose a new output
name for another attempt.

## Compare a leaf's needs with a profile recipe

Before writing a leaf on a large warm profile, see which of its literal loads
the profile's checked-in recipe already names:

```sh
./hearth leaf-needs /ABS/project/leaf.ml --profile s2n-arm
./hearth leaf-needs /ABS/project/leaf.ml --profile s2n-arm --receipt /ABS/runs
./hearth leaf-needs /ABS/project/leaf.ml --profile s2n-arm --deep
./hearth leaf-needs /ABS/project/leaf.ml --profile s2n-arm --deep --json
```

The report scans the leaf and `profiles/PROFILE.ml` with the existing strict
loader scanner and compares literal `needs` paths as exact text. It lists needs
the recipe names, needs it does not name, other source loads (`loadt`, `loads`,
`#use`, `load`, which run regardless), ELF artifact loads and dynamic loads.
The default report does not follow the recipe's transitive loads or the leaf's
local imports.

`--deep` adds a read-only preflight of the leaf's full transitive source and ELF
inputs using the same dependency capture as `prove`. It uses the configured HOL
source root and the selected profile's declared logical roots, hashes the current
files, and reports declaring files, source lines, resolution outcomes, dynamic
loaders, missing inputs and scan bounds. Text output lists every captured edge
and object with its SHA-256; JSON includes the complete canonical closure and its
identity. For example, the P256 point-addition source has 106 source edges and
seven ELF inputs, including those reached through imported proofs.

The deep section is labeled `static_dependency_closure`. When the selected
published shelf is available, a separate `verified_published_source_inventory`
section applies the same exact loaded-source checks as `prove`. This resolves
imports already supplied by a warm profile without treating recipe text as
evidence. The disk closure retains its own completeness and identity; the warm
inventory has a separate identity and lists satisfied edges with their hashes.
If the shelf is unavailable, the report preserves the disk scan and the reason
the warm inventory could not be checked. A static mismatch remains a blocker.

This preflight cannot establish theorems, ISA semantics or caller contracts. It
follows the bounded literal-loader contract; generic OCaml effects remain outside
that contract. A complete scan with transportable inputs exits 0; unresolved,
dynamic, unsupported or bounded-out inputs exit 2. When capture cannot produce a
closure, the command prints the scanner refusal instead of a partial success
report. It starts no HOL or CRIU process, acquires no queue admission or live
execution grant, writes no lexical cache, and builds or fetches no missing inputs.

The recipe comparison is text evidence, not live shelf admission, a proof, or
evidence that the warm image loaded those bytes.
`--receipt` optionally attaches an existing prove receipt's identities as
`warm_exploration`. It refuses a receipt whose profile, source SHA-256 or
recipe SHA-256 differs from the selected profile and current leaf bytes.
This attachment does not compare the receipt's dependency identity to a deep
preflight; it remains an identity check for the leaf and profile alone.

## Assembly projects on an existing profile

Select the existing runtime as described in [setup](setup.md#existing-hol-and-criu-environment).
Use `s2n-arm` for the ARM proof base, `s2n-arm-mlkem` for the shared NTT
development, and `s2n-x86` for the x86 proof base. `s2n-arm-light` is a separate
optional recipe adding arithmetic and ring theory; its absence does not prevent
using an installed `s2n-arm` shelf.

`doctor` checks whether a shelf is compatible with the selected runtime. Its
loaded-source inventory determines which project imports are already present.
For example, the examined ARM shelves contained the ARM base but lacked P256's
ring and group theory; `heavy` contained that algebra but lacked the ARM base.
Choose by the proof's dependencies and measured replay results. A larger recipe
or a successful compatibility check alone does not establish better coverage.
The recorded attempt to load ARM infrastructure on `heavy` failed in imported
ARM support proofs, so use the validated ARM profiles for this assembly route.
Warm state also includes overload priorities and global bindings: skipping an
already-loaded import can skip its initialization effects, and unrelated theorem
names can shadow functions. Source inventory is necessary but does not establish
that independently built mathematical environments compose safely.

Run the actual project leaf and inspect its intended theorem:

```sh
./hearth prove /ABS/project/proof/leaf.ml --profile s2n-arm-mlkem \
  --timeout 300 --run-root /ABS/project/runs
./hearth inspect /ABS/project/runs --binding TARGET_THEOREM
```

Choose a budget for the complete dependency replay and proof. A fast restore
removes basis startup time; dependencies outside that basis still execute in
each fresh child. Keep literal `needs` declarations in the source. Replay
compares their exact bytes with the admitted shelf's loaded-file inventory.
The `profile_satisfied_dependencies` field in receipt JSON records which imports
the shelf supplied. A direct recipe-text comparison alone cannot establish this.

Assembly profiles also declare a pinned s2n source generation. Check it with
`./hol-workbench/dev/reconcile-source-layout --check`. If it reports the
`s2n_bignum` generation missing or stale, run
`./hol-workbench/dev/reconcile-source-layout --materialize s2n_bignum`.
This fetches the declared source revision; it does not build a HOL profile.
Retain project dependencies and the exact ELF objects alongside the proof as
required by its loader paths. Shelf compatibility does not establish that those
project inputs exist or that their contents match.

## Runtime and evidence

`./hearth profiles` lists recipes, not installed environments.
Use an explicit known profile for project work and
`./hearth status --profile light` to inspect queueing. Effective capacity
reflects the physical broker; its processes and sockets are not proof slots.

`status` is an informational snapshot: exit 0 means the report was collected,
including when it reports `degraded`; inspect `--json` for individual profiles.
For a readiness check, use `./hearth doctor --profile light`: it exits 0 when
healthy, 1 when blocked or degraded, and 2 if diagnosis cannot be completed.
`./hearth smoke` checks the installation's public command contract without
starting HOL or CRIU; it does not establish runtime readiness or prove a theorem.

The timeout is an explicit attempt budget. Queue wait is reported separately.
The broker response deadline has a 15-second allowance with a 30-second minimum;
a controller timeout does not establish an exact evaluator cutoff.

A complete-source result and named kernel probes are warm authoring evidence.
They are distinct from process transport, diagnostic text, native execution,
and independent publication replay. Review the actual theorem assumptions.

HOL source is executable OCaml. Run source you trust; a fresh child isolates
proof state and is not an operating-system security sandbox.
