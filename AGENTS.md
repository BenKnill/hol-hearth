# HOL Hearth development

Hearth must help authors advance substantial proof projects. Use the latest
ancestor acceptance cases in docs/validation.md; theorem counts and tiny-proof
latency alone do not establish product usefulness.

Run inspection, code, HOL, CRIU and tests on Linux. Preserve the configured
runtime and existing profiles. Work in a separate checkout.

## Authoring contract

Use one recorded execution path for both ordinary prove and project watching:

```sh
./hearth prove /ABS/project/leaf.ml --profile light --run-root /ABS/runs
./hearth inspect /ABS/runs --binding TARGET_THEOREM
./hearth prove /ABS/project/leaf.ml --profile light --loop --run-root /ABS/runs
```

Support transitive project dependencies, exact source identity, meaningful
negative controls, long explicit budgets, and admission queueing. Display
effective proof capacity, not a count of sockets or runtime processes.
An imported failure rejects the source even when some named claims succeed.
Edits invalidate the displayed current result. Keep every attempt's receipt.

Every replay starts in a fresh child. Work on a small leaf; complete-source
replay is not incremental proof-prefix reuse. Keep the warm seat after Ctrl-C.
Do not recover by killing shared brokers, rebuilding profiles or cold-loading HOL.

Use the existing HOL parser and dependency scanner. Do not introduce a second
evaluator, ad hoc source-language parser, theorem-lookup backend or automatic
proof-repair engine. HOL search and g/e exploration run as ordinary source;
source execution and completed theorem bindings remain distinct.

## Changes and validation

Run ./hearth check after edits and exercise changed behavior with real project
inputs on an existing profile. Preserve failed attempts and inspect the target
binding and complete-source result. Do not replace a failed hard acceptance case
with an easier demo. Keep installer smoke tests labeled as installation checks.

Keep the runtime small: Python standard library, Bash, HOL and CRIU.
For a new Linux environment, docs/setup.md describes source-built light setup.
That is provisioning, not a recovery mechanism for a proof failure.

Only publish source and assets with reviewed MIT-compatible provenance.
Keep external proof projects, upstream sources, local logs, machine paths,
memory images and private history outside the public repository.
