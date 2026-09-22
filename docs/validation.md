# Validation and development basis

## Recorded results at a glance

These are recorded authoring results, with the failed hard cases retained.
Times are the receipt's **HOL evaluation durations**, not end-to-end wall times;
queueing and project-basis preparation are separate costs. A timeout can include
the controller response allowance and cleanup. A dash means no duration was
recorded, not a zero-second run. Hashes and commits below are abbreviated; the
linked JSON records retain their full values and exact inputs.

| Case | Profile | Recorded result | HOL time (s) | Receipt SHA-256 prefix / evidence entry | Runtime commit |
| --- | --- | --- | --- | --- | --- |
| Complete ARM subtraction after alias-rebinding correction | `light` | Accepted; both literal targets, identical source/closure/profile identities | 281.084 | `9472ad5464d8` — [file-loader guards](file-loader-guard-evidence.json), `/final_repeat` | `0ed77f5116e5` |
| Complete ARM subtraction after file-loader guards | `light` | Accepted; both literal targets, zero new axioms | 301.901 | `a7acf8f9cf9fa` — [file-loader guards](file-loader-guard-evidence.json), `/positive` | `5af553486dfd` |
| Acoustic–elastic full shared-table root | `light` | Accepted; 73 discovered root bindings, zero new axioms | 642.844 | `d38c2e999ef0b` — [hard-authoring](hard-authoring-evidence.json), `/full_root` | Unknown during integration |
| ML-KEM NTT layers 1–3, complete functional replay | `s2n-arm-mlkem` | Accepted; all four targets | 336.679 | `0f598abf97f9` — [assembly](assembly-authoring-evidence.json), `/iterations/0/functional_baseline` | Unknown during integration |
| ML-KEM dependency preparation, ELF bootstrap campaign | `s2n-arm-mlkem` | Accepted; two targets, zero new axioms | 230.263 | `6fb72fe1cf94` — [ELF bootstrap](elf-bootstrap-evidence.json), `/attempts/7` | `564a0d2426a8` |
| ML-KEM functional leaf on that prepared basis | `s2n-arm-mlkem` | Accepted; four targets, zero new axioms | 26.720 | `004e58099a48` — [ELF bootstrap](elf-bootstrap-evidence.json), `/attempts/6` | `564a0d2426a8` |
| ML-KEM functional leaf, reuse | `s2n-arm-mlkem` | Accepted; same four targets | 33.679 | `6fc9126d3d8c` — [ELF bootstrap](elf-bootstrap-evidence.json), `/attempts/8` | `564a0d2426a8` |
| P256 point addition, initial full attempt | `s2n-arm` | Rejected: imported missing object; no target accepted | 1,733.717 | `524d5c28f892` — [assembly](assembly-authoring-evidence.json), `/separate_p256_acceptance/receipt` | Unknown during integration |
| P256 point addition, after object build | `s2n-arm` | Timed out; no nonce-proved target | 1,815.268 | `eecbdcee11fb` — [assembly](assembly-authoring-evidence.json), `/separate_p256_acceptance/retry_receipt` | Unknown during integration |
| P256 preparation, before ELF bootstrap fix | `heavy` | Rejected before original source execution | 0.529 | `7c5304c0ed04` — [ELF bootstrap](elf-bootstrap-evidence.json), `/attempts/0` | `0fa3ef785bf3` |
| P256 preparation, after bootstrap fix | `heavy` | Interrupted after imported errors; no remaining leaf accepted | — | `d5648c06b264` — [ELF bootstrap](elf-bootstrap-evidence.json), `/attempts/5` | `564a0d2426a8` |
| Complete ARM subtraction, 18 instructions | `s2n-arm-mlkem` | Accepted; both literal targets | 13.493 | `daef7e1b8e6a` — [ELF bootstrap](elf-bootstrap-evidence.json), `/attempts/1` | `564a0d2426a8` |
| Same complete subtraction source | `heavy` | Rejected in imported ARM infrastructure; neither target proved | 152.684 | `e7fa0789814c` — [ELF bootstrap](elf-bootstrap-evidence.json), `/attempts/4` | `564a0d2426a8` |
| Same subtraction source, initially absent ELF loaders | `light` | Accepted after ordinary imports; both literal targets | 310.544 | `72282efead08` — [ELF bootstrap](elf-bootstrap-evidence.json), `/attempts/13` | `564a0d2426a8` |
| ARM base preparation, then fresh subtraction leaf | `light` | Base source completed; leaf proved both literal targets | 300.505 + 28.719 | `d6192a149a1a`, `f2081a2713cd` — [ELF bootstrap](elf-bootstrap-evidence.json), `/attempts/11` and `/attempts/12` | `564a0d2426a8` |

For the ELF bootstrap campaign, the commits were verified from clean checkouts
independently of receipt client metadata, which reports an unknown revision.
Earlier integration results do not certify later runtime code. The installation
checks are [listed separately](#historical-clean-installation-evidence).

## Review follow-up: input identity and assembly handoffs

The [review follow-up evidence](review-followup-evidence.json) records the
September 22 reproductions, refinements and final checks. Existing warm profiles
worked throughout; failures were in input tracking and authoring handoffs.
No published shelf was rebuilt. Validation ran locally with
`./dev/check-all --offline`, including compiled OCaml diagnostic checks and
pinned Ruff 0.15.7; no GitHub CI was used.

Two successful replays consumed different helper bytes through `Toploop.use_file`
while recording the same dependency hash. A watcher also missed a helper-only
edit. The corrected path refuses uncaptured loaders before HOL, with the source
location and guidance to use captured literal imports. Real watcher checks then
preserved refusal, acceptance, helper-only failure and repair. Further testing
found the same omission through standard HOL loader helpers; those before/after
receipts are retained separately. The guard now also covers `use_file`,
`file_loader`, `load_on_path`, and module exports that can hide known loaders.
This bounded contract does not cover arbitrary OCaml I/O or generated-code effects.

The final runtime, `e2df28477895`, checked the substantial ML-KEM NTT layers 1–3
functional source on `s2n-arm-mlkem`:

| Case | HOL time (s) | Literal conclusion/empty-hypothesis probes | Receipt SHA-256 prefix |
| --- | ---: | ---: | --- |
| Project dependency preparation | 160.915 | 2 | `941c816bd57b` |
| Complete functional leaf | 18.350 | 4 | `67efb302a7bf` |
| Unchanged leaf through the supplied authoring command | 18.520 | 4 | `6a196fe150b2` |

All observed zero new axioms relative to their restored basis. Both leaf attempts
used the same preparation receipt, source hash and dependency identity. Two
earlier combined-controller cycles rejected a false import despite four successful
target probes, then accepted the unchanged repaired leaf using the same prepared
basis. Their actual controller identities remain separate; the final runtime did
not repeat that NTT negative control. Final loader and watcher controls passed.

Inspection now labels computed-goal bindings as
`thm bound (conclusion and hypotheses not checked)` and counts probe strengths
separately. Assembly `reopen` verifies recorded source/object bytes, places scratch
beside the original source and retains the basis, run root and budget. A real ARM
subtraction cycle recorded expected failure in 4.683 seconds, diagnostic scratch
completion with no named theorem in 4.808 seconds, and the completed literal
theorem in 8.595 seconds. These intermediate development runs are not attributed
to the final clean runtime.

Final read-only preflight captured 106 source edges and seven ELF inputs for
P256, and 20 edges and one ELF input for NTT. NTT's four unresolved disk imports
were satisfied by separately verified published warm inventory. Historical P256
failure handoff preserved its exact prefix and object bytes without replaying the
long proof. Warm execution and these static checks do not establish an independent
ISA, ABI, alignment-premise or caller-contract audit.

Integration also exposed two recoverability defects. Display-only branding no
longer invalidates historical receipt identities; substantive metadata still
does. A matching HOL timing line no longer hides the recorded failing binding.
Re-accounting the saved ARM failure identifies `BIGNUM_SUB_P256_CORRECT` at source
line 45, with acceptance unchanged and no HOL replay. External proof sources,
objects and raw logs remain outside the public repository.

## Soundness controls

**A real warm-cache identity bug incorrectly accepted a source containing an
edited false import in 0.345 seconds.** HOL skipped an already-loaded base, so
the changed helper never executed. This was incorrect complete-source acceptance,
not a kernel proof of the false statement. The before and after receipts have
identical leaf and dependency hashes; the corrected path refuses before HOL.

| Negative control | Expected result | Recorded observation | Evidence JSON entry |
| --- | --- | --- | --- |
| Rebind a file-loader module alias after using it | Preserve the earlier loading reference and refuse transport | `refused_dynamic` after HOL syntax preflight; no source execution | [file-loader guards](file-loader-guard-evidence.json), `/alias_rebinding_control` |
| Hidden file-loading API, qualified loader, changed directory, or hidden loader in an import | Refuse before source execution | All four real ARM-project controls returned `refused_dynamic`; no HOL evaluation or profile restore requested | [file-loader guards](file-loader-guard-evidence.json), `/controls` |
| False helper beneath an unchanged warm base | Reject changed source | Before: incorrect acceptance in 0.345 s. After: `refused_profile_satisfaction_dependency_changed`, no HOL execution | [assembly](assembly-authoring-evidence.json), `/iterations/3/hidden_false_import_before` and `/iterations/3/hidden_false_import_after` |
| Unused false import in the substantial NTT source | Reject complete source even if named targets prove | Rejected; all four target probes still proved | [assembly](assembly-authoring-evidence.json), `/iterations/4/clean_checkout/controls/1/receipt` |
| False theorem added to a transitive prepared-basis helper | Invalidate reuse and reject new preparation | Preparation rejected; both basis targets missing | [assembly](assembly-authoring-evidence.json), `/iterations/4/clean_checkout/controls/2/receipt` |
| One executable ELF byte edited with proof source unchanged | Reject object/proof mismatch | Complete subtraction rejected in 0.459 s; neither target proved. Restoring exact bytes passed | [ELF bootstrap](elf-bootstrap-evidence.json), `/attempts/2` and `/attempts/3` |
| Two missing ELF inputs in P256 closure | Refuse before HOL starts | `project_input_missing`, transport `not_started`, no target accepted | [assembly](assembly-authoring-evidence.json), `/iterations/3/p256_missing_objects_before_hol` |
| Wrong node index, reversed normal, wrong weight | Reject the incorrect geometric contracts | All three rejected by HOL arithmetic contradiction failures | [hard-authoring](hard-authoring-evidence.json), `/negative_controls` |

The history below retains the initial defects, timeouts, interrupted attempts
and subsequent refinements. The ML-KEM and subtraction checks are separate
from the completed P256 point-addition campaign and its retained failures.

The file-loader guard checks used an existing `light` profile on x86-64 WSL
Linux, without rebuilding it. Both complete ARM subtraction replays recorded
zero new axioms; the second includes the alias-rebinding correction. Five
project variants were refused before evaluation. Two earlier alias spellings
were rejected by HOL's parser before reaching the loader guard; their receipts
are retained in the same evidence record. These checks cover known file loaders
and directory changes, not arbitrary OCaml subprocess or generated-code effects.
Runtime revisions were independently checked before launch; checkout-relative
receipt metadata itself still reports an unknown revision.

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

## Assembly authoring integration, September 22

This campaign uses existing Linux `s2n-arm-mlkem`, `s2n-arm` and `light` shelves.
No shelf was rebuilt. The [sanitized assembly record](assembly-authoring-evidence.json)
contains input, profile and receipt hashes; external source, object files,
configuration and raw receipts remain outside the repository. Early runs took place
during implementation, so their client metadata does not identify a committed
runtime revision. The final repeat also has an independently verified clean
checkout identity.

The first three test-feedback-refinement iterations exercise the same ARM
ML-KEM NTT layers 1–3 functional source:

1. **Source scope and runtime identity.** The existing lexical scanner excludes
   local theorem helpers from global claim accounting. Dependency capture uses
   the selected runtime root and exact warm library bytes. Complete replay
   accepted all four named targets in **336.679 seconds**, with zero new axioms.
   `MLKEM_NTT_CLEAN_LAYER123_FUNCTIONAL_ENTRY` covers all **256 cells**, subject
   to its alignment, range, memory-separation and machine-state premises. The
   receipt binds the exact ELF object and transitive source closure. A separate
   absolute-library control waited **1,595.173 seconds** for the ARM seat, then
   passed in **1.935 seconds**; its 120-second evaluation budget survived queueing.
2. **Timeout feedback.** A deliberately short run of that source exposed an
   unknown running binding despite an available imported compiler call site.
   Diagnostics now map only imports whose captured and packaged bytes match,
   using the existing theorem scanner. A new real timeout names
   `MLKEM_ZETA_56_RESIDUAL_BOUND` at source line 136. It remains rejected, with
   zero proved target bindings. Its 20-second requested budget produced a
   35.290-second recorded evaluation including the 15-second controller response
   allowance and cleanup. Running-binding attribution is diagnostic evidence;
   it is neither a theorem nor proof that the active tactic failed.
3. **Explicit project basis and controls.** The complete loop basis passed in
   **272.413 seconds**, including its two target bindings and zero new axioms.
   The same functional leaf then passed from a fresh child in **18.369 seconds**,
   with the same four targets, exact source/closure identities, and zero new
   axioms. Repeating it reused the admitted basis and passed in **18.010 seconds**.
   An unused imported false theorem rejected the complete source even when its
   four targets were proved. A false theorem inserted into a transitive basis
   dependency invalidated reuse and rejected the new preparation. Changing one
   byte of the admitted ELF object's executable text also rejected preparation.
   Restoring each exact input recovered successful reuse. Ctrl-C stopped an
   active functional replay with exit 130 and verified quiescence; the next
   replay passed in **18.277 seconds**. Watching the functional leaf with an
   added scratch import produced acceptance (**18.632 seconds**), rejection
   after a dependency-only false theorem (**18.281 seconds**), then acceptance
   after repair (**18.377 seconds**). The leaf hash and admitted basis process
   stayed unchanged; all three attempts reused the basis without preparation.
   Stopping the idle watcher returned exit 130. The initial preparation cost
   is recorded separately; this does not make the first end-to-end attempt an
   18-second check. An interrupted preparation receipt is retained. Six smaller
   lifecycle controls also passed: initial preparation,
   an unused imported failure, its repair, malformed leaf refusal, failed basis
   preparation, and basis repair. They validate rejection and reuse behavior;
   the substantial source remains the acceptance case.

A fourth iteration follows the separate P256 Montgomery point-addition attempt,
which **failed after 1,733.717 seconds** within its explicit 1,800-second budget.
A transitive import reached the ambient profile checkout and encountered a
missing ELF object; no P256 target was accepted. Bounded capture now follows
**106 source edges and seven ELF inputs** in the selected repository and exact
HOL library. It rejects the two missing objects **before HOL starts**. After
building those objects, the full retry passed the imported Montgomery helpers
and reached the entrypoint's `P256_MONTJADD_EQUIV` at source line 1188, then
**timed out after 1,815.268 seconds** including the controller allowance and
cleanup. Source completion was not accepted: no target was nonce-probed as
proved, 11 printed values remain unverified, and six bindings are missing,
including `P256_MONTJADD_CORRECT`. The warm seat was reusable after cleanup.
Both unsuccessful full receipts remain part of acceptance evidence; timeout
alone says nothing about whether the theorem is true or false.

That correction also exposed a warm-cache identity defect. An unchanged cloned
ARM base imported a helper edited to contain a false theorem, yet HOL skipped
the already loaded base and the small control incorrectly appeared accepted in
**0.345 seconds**. The refined preflight compares every captured descendant of
a skipped warm source against the admitted shelf's exact source inventory.
With identical leaf and dependency hashes, the same control now rejects with
`refused_profile_satisfaction_dependency_changed` before HOL starts. The guard
also checks imported `loadt` sources, uses hash-bound graph coordinates and
refuses unattested ELF inputs hidden beneath a skipped warm source. It does not
force cold reloads or treat a matching basename as source identity.

The ML-KEM result does not replace the failed P256 hard case. Zero new axioms
reports a delta from the admitted basis, and the functional result does not
establish an entire ML-KEM implementation or replace an independent publication
audit.

A fifth iteration repeated the substantial source after the project-root and
warm-source identity corrections, integration of the existing lexical
analysis cache, and on-demand imported diagnostics. The newly prepared basis passed in **174.532 seconds** and the
functional leaf passed in **22.080 seconds**, with all four targets and zero
new axioms. Full `./hearth check` passed before that run. The cache still
rehashes source bytes on each capture; it reuses lexical analysis rather than
assuming unchanged paths imply unchanged content.

Imported diagnostics now map only compiler call sites present in the recorded
transcript, avoiding an eager theorem scan of every import. After a narrow
invalid-UTF-8 cache fallback refinement, clean runtime commit
`00861a3df70fb677e8e64e8861ab0247b0a19d8b` passed the full harness checks and
prepared a new basis in **174.404 seconds**. The same functional source then
passed in **20.369 seconds**, again with four targets and zero new axioms.
That clean runtime also passed the repeated controls: reuse completed in
**20.909 seconds**; an unused false import, a changed transitive basis helper
and a changed ELF executable byte were rejected; restoring inputs recovered
the original basis. Active Ctrl-C returned 130 and the next replay passed in
**18.075 seconds**. A final 20-second budget run timed out after **35.231
seconds** including the controller allowance and cleanup, correctly identifying
the imported `MLKEM_NTT_CLEAN_LAYER2_BLOCK` at source line 543. The delivered
launcher, invoked outside the checkout, queued for **35.347 seconds**, then
reused the admitted basis and passed the same four functional targets in
**17.193 seconds**, with zero new axioms. Both attempts left the seat reusable.
Basis identity includes the relevant backend code, so a changed backend must
prepare a new basis; earlier measurements do not certify a later runtime
revision without another recorded run.

## Source-defined ELF loaders, September 22

An actual `heavy` replay exposed a source-loading defect: the harness aliased
`define_assert_from_elf` before the author's first import could define it. The
P256 preparation therefore failed before its original source ran. Ordinary
warm replay used the same premature alias. Exact relative ELF inputs now use
the existing validated package working directory when the captured source
closure has no directory-changing references or native declarations. Imports
retain their original order; other inputs retain the mapped-loader transport.
Basis preparation saves and restores only wrappers actually installed, and its
admission checks the recorded transport identity.

The [sanitized ELF bootstrap record](elf-bootstrap-evidence.json) contains the
source, object, profile, transcript and receipt hashes for these completed runs:

| Check | Result |
| --- | --- |
| Original P256 preparation on `heavy` | Rejected in **0.529 s**: missing ELF loader before original source execution; 12 missing targets; remaining leaf not run |
| Complete 18-instruction `bignum_sub_p256.ml` on `s2n-arm-mlkem` after the fix | Accepted in **13.493 s**; both literal correctness targets matched their conclusions with empty hypotheses; zero observed new axioms |
| One executable ELF byte changed, proof source unchanged | Rejected in **0.459 s** by `dest_cons4`'s instruction/literal mismatch; complete source rejected and neither target proved |
| Exact original ELF restored | Accepted in **15.004 s** with the same source and object hashes as the positive run; both targets proved and zero observed new axioms |
| Same complete subtraction source on `heavy` after the fix | Rejected in **152.684 s** after reaching original imports: `REAL_ARITH` failed in `common/misc.ml`, followed by further ARM support errors; neither target proved |
| P256 preparation on `heavy` after the fix | Interrupted with exit 130 after nine imported-file errors; no target or remaining leaf accepted; cleanup verified a quiescent child and reusable seat |

Existing `light` shelves directly exercised initially absent ARM ELF loaders.
Ordinary imports defined them and the complete subtraction proof passed in
**310.544 s**, with both literal targets checked and zero new axioms. Separately,
the complete ARM base passed preparation in **300.505 s** with zero new axioms;
it has no discovered entrypoint theorem targets and makes no ELF calls. Its
source-defined loaders survived preparation transport, and the fresh subtraction
leaf passed both literal targets in **28.719 s**, again with zero new axioms.
The base receipt establishes complete-source execution and imported-error
rejection, not independent probes of every imported theorem.

The same runtime also repeated the substantial 256-cell ARM NTT layers 1–3
functional source using the retained mapped-loader route. Dependency preparation
passed in **230.263 s** with its two targets; the full functional leaf passed in
**26.720 s** with all four targets. A repeat reused the same preparation receipt
and passed those four targets in **33.679 s**. All three recorded zero new axioms.
The exact source and object identities match the earlier functional campaign.
These runs overlapped other proof work and are not a controlled speed comparison.

The corrected runs use independently verified clean runtime commit
`564a0d2426a85dbb1dd3edfb34a07b54f7aaf3ae`. Their receipt client metadata itself
reports an unknown revision; the record preserves this distinction. Full
`./hearth check` passed, including 32 focused transport, basis-identity and
replay checks. This validates source loading and exact-object rejection on a
real assembly routine; it does not replace the recorded P256 point-addition
hard case. The later heavy failures expose an additional incompatibility while
loading ARM infrastructure into that mathematical environment; removing the
bootstrap error does not make `heavy` an accepted replacement for an ARM shelf.
The interrupted receipt has no evaluation duration or foundation-delta marker;
neither is inferred from progress output or the other attempts.

Two ordinary HOL controls isolated the first heavy failure. Loading
`Library/integer.ml` sets integer overload priority; the subsequent
`needs "Library/floor.ml"` is skipped because heavy already contains it, so its
real-priority side effect does not run. The diagnostic reported integer operands
and the unchanged quoted arithmetic statement failed in **11.278 s**. Adding
only `prioritize_real()` after the same imports produced real operands and
acceptance of that statement in **11.326 s**, with its literal conclusion and
empty hypotheses checked. This reduced control diagnoses load-order state;
it is not assembly acceptance. An independent collision remains: heavy's topology
theorem named `open_in` shadows the file-opening function expected by the ARM
ELF reader, which was observed to fail with theorem type. A priority reset alone
does not make the complete heavy environment compatible with these ARM sources.

## Long proof activity diagnostics, September 22

A live P256 run exhausted the original activity protocol's 8,192-call limit,
leaving later work unattributed. Protocol v2 retains up to 64 active calls and
discards completed-call history. Deeper nesting emits explicit overflow and
resume records; legacy v1 receipts remain readable. This bounds activity
accounting, while the raw transcript continues to grow with emitted events.

The [sanitized activity control record](long-proof-activity-evidence.json)
contains two completed real HOL attempts on the existing `light` shelf:

| Diagnostic control | Observed result |
| --- | --- |
| 8,300 completed calls followed by a deliberately delayed target | The 20-second budget, plus the 15-second controller response allowance and cleanup, returned 124 in **35.129 s**. Call 8,301 remained attributable to `HEARTH_AFTER_ACTIVITY_LIMIT` at original source line 2; complete source and its target were not accepted. |
| Same completed-call prefix, target repaired | Accepted in **3.610 s**; 8,301 calls recorded, none active; the one literal `T` target matched its conclusion with empty hypotheses, and zero new axioms were observed. |

Both receipts report a quiescent child and reusable seat. The timeout's axiom
measurement is missing, not zero. These are diagnostic controls, not assembly
acceptance or a completed P256 certificate. Activity attribution itself supplies
neither failure nor theorem evidence. Full `./hearth check` passed on the
implementation, including legacy-accounting regressions and a plain OCaml
emitter check for long histories, overflow, recovery and exception propagation.
The tested merge changed only documentation from that checked implementation;
the record distinguishes independently verified Git identity from the receipts'
unknown client revision.

The combined runtime also accepted the unchanged 256-cell ARM NTT layers 1–3
functional source. Preparation took **194.827 s**, with both targets checked;
the complete leaf took **18.323 s**, with all four literal conclusions and empty
hypotheses checked. Both observed zero new axioms. The receipts report
**3,582.321 s** and **1,273.738 s** of physical-shelf admission waiting respectively;
that waiting did not consume either phase's 1,500-second evaluation budget.
Reported admission time can omit earlier project-basis lock waiting and is not
total wall time. These results are bound to independently verified runtime
`7b9933845b2e334ec73af9fa9a1dfa21a807ad7f`; its changes from the checked runtime
are documentation only.

## Completed P256 authoring continuation, September 22

The earlier P256 failures remained in the record while the same hard case was
continued with an explicit project basis and a longer budget. The
[P256 record](p256-authoring-evidence.json) binds the unchanged upstream
`arm/proofs/p256_montjadd.ml` at s2n-bignum `471fca76`. Its first 60,208 bytes
form the project basis; the exact remaining suffix gains one literal `needs`
for that basis. Removing that added declaration reconstructs all 70,755 original
bytes. No original proof, premise, frame or instruction bytes were changed.

| Existing profile and checked runtime | Preparation | Complete remaining leaf |
| --- | --- | --- |
| `s2n-arm`, merged integration `0fa3ef7` | **4,404.013 s**, 12 targets | **1,095.040 s**, five targets |
| Same retained ARM basis, unchanged leaf after the negative control | Reused the same preparation receipt | **1,285.456 s**, five targets |
| `s2n-arm-mlkem`, ELF correction `564a0d2` | **4,601.477 s**, 12 targets | **1,251.807 s**, five targets |

Every accepted phase completed its source and observed zero new axioms. The leaf
includes optimized correctness, subroutine correctness and safety under the
upstream theorem premises. These concurrent-work timings are not a controlled
profile comparison. Preparation is a separate one-time cost: the first ARM
attempt took about 92 minutes across both evaluations, and each subsequent
complete remaining leaf still took 18–21 minutes. Small contract checks against
the retained dependency can be much shorter.

Fifteen discovered targets have literal statements and received exact-conclusion
and empty-hypothesis probes. The equivalence and exact-step statements are
computed terms, so their ordinary binding probes establish theorem type only.
Additional ordinary HOL sources explicitly checked empty hypotheses and
alpha-equivalence to both original computed goals. Those sources passed in
**375.818 s** on ARM and **314.671 s** on ARM-MLKEM, with all three discovered
targets and zero new axioms. This closes that statement-checking gap without
adding another evaluator or theorem-lookup mechanism.

The controls retain the same preparation. An incorrect unoptimized sum was
rejected by `ACCEPT_TAC` in **0.767 s**; restoring its exact original quotation
passed in **0.805 s**. Changing only the optimized postcondition's second group
operand from `P2` to `P1` in the full remaining leaf produced
`solve_goal: Too deep` after **1,134.199 s**. The source was rejected, with no
accepted target probe, one printed/unprobed value and four missing targets.
This was a proof failure within budget, not a timeout or an independent proof
of the changed proposition's negation. The subsequent unchanged full-leaf check
was planned negative-control/repair validation, not an automatic retry after
an unexplained infrastructure failure.

All seven complete ELF objects are hash-bound. Four available literal instruction
assertions matched their full text sections. The optimized `define_from_elf`
objects have no literal assertion; their proof evidence is not replaced by an
invented byte-list comparison. These results establish the recorded warm proof
under the upstream semantics and premises. Independent publication replay and
a complete alignment, ABI and frame review remain separate.

## Failure feedback from the P256 control

The full negative control exposed a concrete diagnostic defect. HOL printed one
blank line between the nonce-bound diagnostic frame and its matching uncaught
exception. The old adjacency rule therefore left the failing binding unknown,
despite a compiler call site in the exact captured source. The corrected rule
allows at most eight blank lines and requires a supported exception rendering
to match. Substantive intervening output, different later exceptions, malformed
or truncated frames and completed sources remain barriers. Legacy display
preserves an already identified adjacent diagnostic without weakening new
attribution.

The [failure-context record](failure-context-evidence.json) checks the original
receipt, source and raw-transcript hashes. Re-accounting those immutable bytes
identifies `P256_MONTJADD_CORRECT` at original source line 38. Only diagnostic
fields changed; source acceptance and every other accounting field stayed
unchanged. The original receipt was preserved and the failed assembly proof was
not rerun. Attribution remains diagnostic: output alone cannot distinguish a
silent catch followed immediately by the identical exception.

Four small real HOL controls exercised identified failure, successful repair,
caught failure and an unrelated later exception in **0.235–0.240 s**. All had
the expected result, zero new axioms and reusable seats. These were diagnostic
controls on the implementation worktree before the final legacy-display
refinement; exact runtime hashes were not captured, so they are not relabeled
as final-commit HOL acceptance. Full `./hearth check` and the immutable P256
re-accounting passed on the final implementation.

The [final delivery record](warm-delivery-evidence.json) also checks the unchanged
256-cell ARM NTT functional source on clean runtime
`a8c85d17c333b2798fbc9a7cf0ac43f2a35ba9e4`, which combines the failure-context fix,
long activity diagnostics and clearer warm-basis onboarding. Preparation passed
in **145.062 s** with both targets, and the full leaf passed in **17.649 s** with
all four literal conclusions and empty hypotheses checked. The ready launcher,
invoked outside the checkout, reused that exact preparation receipt and passed
again in **18.028 s**. All three observed zero new axioms and reusable seats.
The prior onboarding-only checkout independently passed preparation in
**146.754 s** and the same full leaf in **18.086 s**. The record retains earlier
runtime results separately; none is relabeled as testing a later implementation.

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
