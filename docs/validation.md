# Validation and development basis

## Acceptance follows substantial proof work

The most recent ancestor campaign examined was the September 8 acoustic–elastic
shared-table certificate, after the September Laghos interleaved-authoring work.
The acoustic source checkpoint was
`f5ef30706624577d20d5fac8a3210fe96a99b2e0`; the ancestor runtime checkout is
`e5bcfa1a7dc19d477fbc90ec447b82bd59a32fab`.

The useful workflow was a modular pilot, exact transitive imports, a full root
at milestones, bounded inspection, and serial admission for long work. The
ancestor full root took 821.696 seconds in HOL. The preceding Laghos campaign
also improved edit latency by reducing imports; neither result justifies
promising arbitrary proof-prefix reuse.

The certificate concerns a captured real operator with 320 incidences over 64
faces, repeated destinations, geometric coefficient identities, work/power
identities and coefficient-error bounds. It is not verification of production
time stepping, native floating-point execution, or an entire discrete adjoint.
Wrong node indices, reversed normals and wrong weights are meaningful controls:
an internally consistent dot-product test alone can miss these geometric errors.

## Hearth acceptance, September 16

Real runs used existing compatible `light` profiles in Ubuntu 26.04 ARM64.
No profile was rebuilt for this campaign. The full root and three original
controls used the ancestor's serial shelf; watcher tests also used Hearth's
source-built distribution-toolchain shelf from the earlier setup acceptance.

| Check | Observed result |
| --- | --- |
| Full original shared-table root | **642.844 s HOL evaluation; accepted**; 73 discovered root bindings proved, literal source check of 138 empty-hypothesis theorem values passed, zero new axioms |
| Intended targets beyond the first 12 bindings | Work and transpose-error targets inspected by name, with source locations and exact dependency identity |
| Wrong node index / reversed normal / wrong weight | All three rejected by HOL with the expected arithmetic contradiction failure; not transport failures |
| Admission during the full proof | Three controls queued; one effective proof seat despite three runtime processes; queue wait did not consume their proof budgets |
| Acoustic pilot through project watcher | Accepted, dependency-only false proof rejected, dependency-only repair accepted; unchanged root bytes and changed closure identities |
| Dependency/edit regressions | Seven real events: initial success, failed unused helper, repair, deletion, restoration, older captured revision, subsequent rejection of concurrent edit |
| Active Ctrl-C | Cancelled before completion in 0.132 s; exit 130, failure receipt retained, active count returned to zero, warm basis PID preserved; subsequent proof succeeded |
| Clean checkout of runtime commit a2eb135 | Repeated setup reused the profile, its public proof passed, the acoustic pilot passed, and the printed target-inspection command worked from another directory |
| Portable checks | Standard-library import check, source provenance, 7 setup regressions, 3 project/inspection regressions, public command contract, source/dependency/logical-root checks, 7 CRIU contracts, 82 source / 22 artifact / 11 convergence loader cases, 10 shared lifecycle cases |

The [sanitized acceptance record](hard-authoring-evidence.json) records exact
source, profile and receipt hashes. Original project sources and raw machine
receipts remain outside this repository. They have not been copied into Hearth
or relicensed as MIT. The hard run occurred during implementation; its client
metadata does not identify a committed Hearth revision. Its mathematical inputs,
profile and transcript are independently hash-bound in the receipt.

## Changes made from these results

- Removed the separate native live evaluator and compiler/build requirement.
  Watching now schedules the ordinary receipt-producing replay. An imported
  failure rejects the complete source, including an unused failed theorem.
- Removed the toy demo command, recordings and demo-led onboarding. The one
  retained small proof is explicitly an installation fixture.
- Removed the separate theorem-signature lookup backend and its broken query
  route. HOL search and interactive tactic exploration use ordinary source.
- Added full receipt JSON, exact target selection, complete verbose binding
  lists, source locations and bounded multiline failure details.
- Display effective admission capacity and commands that run from a project
  directory outside the Hearth checkout.

Every watched attempt evaluates its complete source in a fresh child. This
preserves isolation but does not make a ten-minute root recheck instantaneous.
Use a focused leaf and a suitable warm library basis. Open interactive goals are
not completed theorems; source acceptance must not be confused with proving the
intended target. Inspect that target explicitly.

## Historical clean installation evidence

On September 15, revision `fb575fc7503446d5ef87636ce0f5b77a77d573db` was installed
from clean clones and empty HOL/runtime directories on **Debian 13 ARM64** and
**Ubuntu 26.04 ARM64**, using distribution OCaml 5.3 / 5.4 and CRIU 4.1.1 / 4.2.
Pinned HOL fetch, build, first light load/dump/restore, public proof check,
repeated setup, relative paths containing spaces, and failure journeys passed.
Profile construction took about 155–157 seconds; source fetch and compilation
were separate. The [historical record](onboarding-evidence.json) retains these
measurements, including the now-removed demo/evaluator tests.

These historical results do not validate the new watcher on Debian. Current
changes were tested on Ubuntu; x86-64 and optional heavy, probability and
assembly profile provisioning were not tested in this campaign. Warm checks
are authoring evidence, not an independent final publication audit.

The provenance inventory covers nine shipped ML files, two profile manifests
and the HOL build lock. External sources and generated images retain their own
licenses. See [source provenance](source-provenance.md).
