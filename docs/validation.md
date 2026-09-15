# Validation

Fresh setup and agent-facing command journeys passed on **Debian 13 ARM64 and
Ubuntu 26.04 ARM64** on 2026-09-15. The final acceptance run used code revision
`fb575fc7503446d5ef87636ce0f5b77a77d573db`, a clean clone in each VM, and empty HOL source and runtime directories.
Distribution packages supplied the toolchain. Neither run reused an OPAM switch
or copied a warm image. These were real Linux runs, not distribution-ID simulation.

The [machine-readable record](onboarding-evidence.json) binds setup receipts,
profile identities, demo source/receipt hashes and live-loop logs. Raw receipts
remain local because they include machine paths; the public record is a summary.

| Check | Ubuntu 26.04 ARM64 | Debian 13 ARM64 |
| --- | --- | --- |
| Python / OCaml | 3.14.4 / 5.4.0 | 3.13.5 / 5.3.0 |
| Camlp5 / distribution CRIU | 8.04.00 / 4.2 | 8.03.01 / 4.1.1 |
| Pinned HOL source fetch and module build | pass | pass |
| First light profile load, dump, restore and smoke proofs | pass | pass |
| Public cat-map proof before setup declares ready | pass | pass |
| Repeated setup | existing profile reused; one shelf | existing profile reused; one shelf |
| Recorded cat-map, balance and repaired-square proofs | all five bindings proved | all five bindings proved |
| Intentional false square | rejected; binding missing | rejected; binding missing |
| Live false proof → syntax error → repair → Ctrl-C | pass | pass |
| Relative proof filename containing spaces | pass | pass |
| Missing config/source, unknown profile, bad syntax, occupied output | clear nonzero result; no traceback | same |
| Full portable tool and provenance checks | pass | pass |
| Direct evaluator build and real child SIGTERM reporting | pass | pass |
| Final doctor | healthy, no active proofs or queue | healthy, no active proofs or queue |

The profile construction step took approximately 155–157 seconds in these two
runs, including about 149–151 seconds of HOL startup and loading. Source fetch
and compilation were separate short steps. These are local measurements, not
a promise for other hardware or profiles.

## Bugs found and fixed by the fresh runs

- Missing installation route: added `./hearth setup`, a pinned HOL revision,
  capability checks, progress, logs and a source-to-restored-profile path.
- False sudo refusal: test the actual authorized CRIU invocation instead of
  requiring a sudo validation operation that can reject passwordless policies.
- Root-owned dump files: transfer the generated artifacts to the unprivileged
  controller before hashing them; reject symlinks and restrict file permissions.
- Python 3.13 permission exceptions during restored-pidfile cleanup: use the
  existing privileged probe for root-owned output directories.
- OPAM-only syntax preflight: use the configured switch when present and system
  OCaml paths otherwise; do not require a nonexistent local stublibs directory.
- Misleading doctor output: default to the installed `light` profile, show the
  configuration detail, and give new users a setup command. Optional profile
  checks remain available through `--all-profiles`.
- Unhelpful live parser exception: report a syntax error instead of `Stdlib.Exit`.
- Duplicate demo output: keep receipt inspection opt-in with `--inspect`, and
  offer `./hearth demo repaired` after the expected-failure demonstration.

Setup now includes a public proof check so an internal restore smoke test alone
cannot make a broken public authoring path appear ready. Existing configurations
and profiles are preserved. A failed build stays unpublished for diagnosis.

Seven focused setup/permission regressions accompany the portable harness:
source byte identity, dependency packaging, seven CRIU contracts, 82 source / 22
artifact / 11 convergence loader cases, and 11 lifecycle / 14 hostile-interrupt
cases. Synthetic tests check orchestration; the real proof runs above check HOL.

## Licensing and boundaries

The provenance inventory covers 15 shipped ML files, both profile manifests,
and the HOL source build lock. Changed, new or missing ML, changed manifests,
and standalone recipe drift require renewed review. Upstream sources and images
remain outside the repository under their applicable licenses.

Only the listed ARM64 versions and local VM/kernel environments were tested.
This does not establish x86-64 support or CRIU operation under every container,
kernel or privilege policy. Setup checks capabilities before loading HOL.
Optional heavy, probability and assembly profiles were not freshly provisioned
in this acceptance run. `light` is sufficient for the published demos.

The native evaluator must match its HOL runtime's OCaml compiler. Warm source
acceptance and named theorem probes remain authoring evidence; independent final
publication replay and review of assumptions are separate work.

The earlier [demo evidence](../demos/evidence.json) and
[terminal recording](../demos/live-loop.cast) remain historical measurements of
the initial source release. This page and the onboarding record describe the
new source-built environments.
