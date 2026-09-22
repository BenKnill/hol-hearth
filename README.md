# HOL Hearth

**A warm HOL Light workspace for substantial proof projects.**

Keep the mathematical basis loaded while editing a small proof leaf. Check
the complete project at milestones, inspect the intended theorem, and retain
the exact source and dependency bytes behind each result.

Hearth is Linux command-line orchestration around HOL Light and CRIU. One
execution path serves recorded checks and live project watching. Every attempt
uses a fresh HOL child; failed proofs do not contaminate the next attempt.

## Work on a project

On a host with an existing warm environment, first point Hearth at its runtime
configuration; see [reuse an existing environment](docs/setup.md#existing-hol-and-criu-environment).
Use [setup](docs/setup.md#create-the-first-profile) when provisioning a new host.
Then run the project on its intended basis:

```sh
./hearth prove /ABS/project/proofs/leaf.ml --profile light \
  --timeout 120 --run-root /ABS/project/runs
./hearth inspect /ABS/project/runs --binding TARGET_THEOREM
```

Use `s2n-arm` for ARM assembly, `s2n-arm-mlkem` for the larger NTT basis, or
`s2n-x86` for x86 assembly. `hearth profiles` lists recipes; installed shelves
depend on the host. The optional `hearth doctor --profile NAME` checks the
configured shelf and queue without running a proof. See the
[assembly workflow](docs/usage.md#assembly-projects-on-an-existing-profile).

For a read-only view of a large leaf's transitive source and ELF inputs, use
`./hearth leaf-needs /ABS/project/proofs/leaf.ml --profile s2n-arm --deep`.
It reports captured hashes and warm inventory separately, without starting HOL.

`--profile` selects the published warm environment. For a stable, expensive
project import, `--basis` checks and retains that completed dependency:

```sh
./hearth prove /ABS/project/proofs/leaf.ml --profile s2n-arm \
  --basis /ABS/project/proofs/completed_dependency.ml \
  --timeout 7200 --run-root /ABS/project/runs
```

The leaf must import that file through literal `needs`. Keep the same run root
to reuse its checked state. Edits to that dependency, its imports or its objects
require preparation again. Preparation and leaf checks each retain a receipt
and use the timeout separately. Every attempt still evaluates the whole leaf.
See the [project workflow](docs/usage.md#reuse-a-completed-project-dependency).

For a sustained edit session, add `--loop` to that same command:

```sh
./hearth prove /ABS/project/proofs/leaf.ml --loop --profile s2n-arm \
  --basis /ABS/project/proofs/completed_dependency.ml \
  --timeout 7200 --run-root /ABS/project/runs
```

The watcher follows transitive source and artifact dependencies, marks changed
inputs unchecked, and records each stable revision through the same proof
command. Ctrl-C stops its owned replay; the shared warm basis remains available.

At a milestone, check the complete root with an appropriate budget:

```sh
./hearth prove /ABS/project/proofs/complete.ml --profile s2n-arm \
  --timeout 7200 --run-root /ABS/project/milestones
./hearth inspect /ABS/project/milestones --verbose
./hearth inspect /ABS/project/milestones --json
```

Selected lines from actual target inspection of the recorded ML-KEM functional
replay:

```text
status: succeeded
source_acceptance: accepted
source_completed: true
claims_complete: true
  MLKEM_NTT_CLEAN_LAYER123_FUNCTIONAL_ENTRY: proved (source conclusion matched; hypotheses empty) source_line=108
```

Adding an unused false import produced this failure, even though the target
probe succeeded:

```text
status: failed
source_acceptance: not_accepted
source_completed: false
claims_complete: true
  MLKEM_NTT_CLEAN_LAYER123_FUNCTIONAL_ENTRY: proved (source conclusion matched; hypotheses empty) source_line=108
first_failure: transcript_line=231 Exception: Failure "TAC_PROOF: Unsolved goals".
```

These excerpts omit paths and other fields. The [assembly evidence](docs/assembly-authoring-evidence.json)
records receipts `59e81581d171` and `ae5219e7295f` in
`iterations[4].clean_checkout.controls[0]` and `controls[1]`, respectively.
Always inspect complete-source acceptance alongside the intended binding.

## Install

On macOS, follow the [OrbStack Linux setup](docs/macos.md).

```sh
git clone https://github.com/BenKnill/hol-hearth.git
cd hol-hearth
sudo apt-get update
sudo apt-get install --no-install-recommends python3 git make gcc ocaml-nox \
  ocaml-findlib camlp5 libzarith-ocaml-dev libcamlp-streams-ocaml-dev criu sudo
./hearth setup --check
./hearth setup
```

Setup builds the first local `light` profile from pinned HOL sources and checks
the public proof route. Existing compatible profiles are reused. Optional
calculus, probability and assembly profiles have [source recipes](profiles).
CRIU requires a compatible Linux kernel and appropriate privileges.
Recorded clean installation checks used Debian 13 ARM64 with CRIU 4.1.1 and
Ubuntu 26.04 ARM64 with CRIU 4.2; the [installation evidence](docs/validation.md#historical-clean-installation-evidence)
records their scope. Kernel versions were not retained in that record, so run
`./hearth setup --check` on the intended host.

The command layer uses Python 3.11+ standard library and Bash. No Node, Python
packages, OPAM, Dune or separate live evaluator is required.
`./hearth check` checks the tool without loading HOL.
For local development checks including pinned lint, run `./dev/check-all`.
It requires `uv`; the first run may fetch Ruff 0.15.7. See
[local checking instructions](docs/setup.md#tool-checks), including offline use.

## Why this exists

Hearth grew out of an earlier, private HOL authoring runtime (the "ancestor" in
the validation history). Its latest substantial work was an acoustic–elastic
interface: captured geometry, repeated-node scattering, a shared 320-incidence
table, operator work/power identities, and coefficient-error bounds. Its full
certificate took 821.696 seconds; wrong indices, normals and weights were
rejected. The useful authoring unit was a small pilot followed by the complete
certificate.

That is the acceptance bar: help develop and check consequential mathematical
contracts, preserve failing controls, and make the next proof edit clear.
Installation smoke proofs do not establish that bar.
See [the development basis and current validation](docs/validation.md).
The [soundness controls](docs/validation.md#soundness-controls) include a recorded
warm-cache identity bug and its rejection after the fix.

## Evidence and license

A recorded result distinguishes source completion, named theorem probes,
transport, input hashes and foundation changes. An accepted OCaml source can
contain unfinished interactive goals; inspect the actual target binding.
Warm authoring checks are real HOL checks. Independent publication replay and
review of the theorem's assumptions remain separate.

[MIT](LICENSE). External libraries retain their licenses. Hearth distributes
[reviewed recipe sources](docs/source-provenance.md), not third-party proof
corpora, runtime images, private projects or the ancestor's Git history.
See [third-party dependencies](THIRD_PARTY.md).
