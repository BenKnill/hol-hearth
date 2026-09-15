# Validation

Checked on 2026-09-15 against the exported HOL Hearth source.

| Check | Ubuntu 26.04 ARM64 | Debian 13 ARM64 |
| --- | --- | --- |
| System Python, with no third-party packages | 3.14.4: pass | 3.13.5: pass |
| Public commands, receipts, isolated imports | pass | pass |
| Source byte identity and dependency packaging | pass | pass |
| Loader/parser contracts | 82 source, 22 artifact, 11 convergence cases: pass | same: pass |
| Lifecycle and interrupted cleanup | 11 lifecycle and 14 hostile-interrupt cases: pass | same: pass |
| Native evaluator, direct OCaml compiler | 5.4.0: pass | 5.3.0: pass |
| Native parser/error-location checks | pass | pass |
| Real child termination reporting | SIGTERM / 15 / exit 143: pass | SIGTERM / 15 / exit 143: pass |
| CRIU kernel capability check | Existing configured runtime | Distribution CRIU 4.1.1: pass |
| Real HOL recorded demos | pass, existing light profile | not run |
| Real failure → repair → Ctrl-C edit session | pass | not run |
| Fresh-machine HOL profile provisioning | not tested | pending |

Both are real local Linux VMs. No distribution identity was spoofed. The
Debian checks used a clean source export, system Python, distribution OCaml,
and no copied Ubuntu executable or warm snapshot.

## Real proof demos

The recorded Ubuntu runs accepted all five intended theorem bindings:

- `cat-map.ml`: `HEARTH_CAT_INVARIANT`, `HEARTH_CAT_INVERSE`.
- `balance.ml`: `HEARTH_TRANSFER_CONSERVES`, `HEARTH_TRANSFER_NONNEGATIVE`.
- `repaired.ml`: `HEARTH_SQUARE_REPAIRED`.

`failure.ml` was rejected and its proposed binding stayed missing. The live
edit test went from failure to acceptance after saving the repair, then stopped
with SIGINT / exit 130. A separate killed-child test reported SIGKILL / signal
9 and closed that edit session; a later session and recorded check succeeded.
The final runtime inspection reported no active proofs, no queue, and clean
lifecycle state. Shared warm brokers remained available.

[Demo evidence](../demos/evidence.json) records exact source hashes and selected
receipt fields. Raw receipts stay local because they contain machine paths.
The [terminal recording](../demos/live-loop.cast) preserves captured timing and
output, with only the source path normalized. Its subsecond timings describe
one warm local run, not installation time or a general performance guarantee.

## Boundaries

The tests establish portable orchestration and evaluator compilation on the
listed versions. They do not establish a complete Debian HOL/CRIU installation,
snapshot portability across hosts, or compatibility with every Linux kernel.
ARM64 is the tested architecture; x86-64 remains unverified for this release.

The portable tool tests use synthetic protocol fixtures. They validate
orchestration and receipt handling, not mathematics. Real demo proofs are
checked separately against an existing local HOL Light profile.

The native sources compile on OCaml 5.3 and 5.4. The live evaluator must still
be built with the compiler matching the HOL runtime that will load it.
