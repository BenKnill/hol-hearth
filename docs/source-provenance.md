# ML and profile source review

Reviewed 2026-09-15. No incompatible license or unresolved copied proof source
was identified in the files shipped by this source release. This review covers
the files below; it does not license third-party libraries or generated memory
images under MIT.

## Shipped files

| Files | Provenance and decision |
| --- | --- |
| `demos/*.ml` (4) | Original release examples: a quadratic identity, balance arithmetic, and an intentional failure/repair pair. MIT. |
| `hol-workbench/native-eval/*.ml` (3) | First-party phrase evaluator, process/session controller and test probe. Inspected all source and original addition history; no imported third-party implementation identified. MIT. |
| `profiles/*.ml` (7) | Exact generated copies of the public recipes. Short load instructions, tactic aliases and smoke checks. MIT; see the ML-KEM attribution below. |
| `hol-workbench/profile-support/compact_after_preload.ml` | First-party three-line helper using OCaml GC APIs and a Workbench marker. MIT. |
| Both `warmup-profiles*.json` manifests | Reviewed embedded recipes, operation examples and restore sentinels, including developer profiles. No external proof corpus is embedded. MIT; see ML-KEM attribution below. |
| `dev/setup-lock.json` | First-party build metadata naming the same publicly reviewed HOL revision below. MIT. Upstream sources are fetched separately under their own license. |

The [inventory](source-provenance.json) records the reviewed file bytes by
SHA-256. `./hearth check` refuses new, removed or changed ML-family files or
changed manifests until the inventory is reviewed and updated. It also checks
that standalone recipes match the runtime manifest. This is a change guard,
not an automated legal determination.

Two developer entries requiring excluded project sources were removed. The
compaction helper now lives with the shipped runtime rather than in an
excluded research directory. No papers, external proof corpus, compiled HOL
runtime or CRIU image was added.

## Libraries named by recipes

These are external source dependencies, not vendored Hearth files. The HOL
revision below identifies the upstream source checked during this licensing
review and pinned for new setups. It is not a claim that older, independently
configured local profiles used that revision.

| Source | License evidence |
| --- | --- |
| HOL Light arithmetic, ring theory, Multivariate and Probability | [HOL Light LICENSE at reviewed revision](https://github.com/jrh13/hol-light/blob/2a1cea8f1cb7f3885a60d947ba06eabac6ec1d32/LICENSE): BSD-style terms, with explicit per-file/subdirectory exceptions. |
| HOL Light `Formal_ineqs` developer recipe | [Directory notice](https://github.com/jrh13/hol-light/blob/2a1cea8f1cb7f3885a60d947ba06eabac6ec1d32/Formal_ineqs/README.md) expressly states BSD-2-Clause terms and names its copyright holders. |
| s2n-bignum ARM/x86/ML-KEM and developer AES recipes | [License at the manifest's pinned revision](https://github.com/awslabs/s2n-bignum/blob/fce78c7c17baee6a60511efe821930d4d049a6c0/LICENSE); entrypoint file notices offer Apache-2.0 OR ISC OR MIT-0. |

### ML-KEM statement attribution

The public ML-KEM recipe and its restore smoke check refer to the statement of
`FORWARD_NTT` from
[`common/mlkem_mldsa.ml`](https://github.com/awslabs/s2n-bignum/blob/fce78c7c17baee6a60511efe821930d4d049a6c0/common/mlkem_mldsa.ml).
Copyright Amazon.com, Inc. or its affiliates. The source offers
`Apache-2.0 OR ISC OR MIT-0`; the MIT-0 option applies to this small referenced
statement. The upstream proof implementation is not copied into Hearth.

## Distribution boundary

A recipe's `needs` instruction loads someone else's library under that
library's existing license. Hearth's MIT license covers Hearth code. Preserve
upstream copyright and license files in fetched checkouts, and check individual
file exceptions before copying any upstream implementation into this repo.

Generated profiles are local build artifacts containing loaded code and runtime
state. Before distributing any such image, review its exact loaded-source and
runtime contents and carry all required notices. The source review above is
not clearance to distribute arbitrary snapshots as MIT-only artifacts.
