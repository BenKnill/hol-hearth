# HOL Hearth development

Run code, tests, OCaml and HOL on Linux. Preserve the user's configured HOL
checkout and warm profiles. Work in a separate source checkout.

For a fresh Linux environment, follow docs/setup.md: install the distribution
packages, then run `./hearth setup --check` and `./hearth setup`. Setup creates
the first `light` profile from pinned HOL sources. It preserves an existing
runtime configuration and reuses an already compatible profile. `./hearth
doctor` checks `light` by default; optional uninstalled profiles are not errors.

Use a recorded check first:

```sh
./hearth prove /ABS/source.ml --profile light --run-root /ABS/runs
./hearth inspect /ABS/runs
```

Use `--loop` only for a live multi-edit session. Never substitute cold loading,
rebuild profiles, or kill shared brokers to recover from an authoring failure.

Run `./hearth check` after changes. This uses stdlib-only tests and does not load
HOL. Build evaluator changes with `./hearth build-native` using the same OCaml
compiler as the configured HOL profile. Run changed-source demos on an existing
compatible profile. Preserve exact source hashes, complete-source status, and
nonce-bound binding evidence in receipts; natural transcript text is diagnostic.

Keep runtime requirements minimal. Do not add a package manager, environment
manager, host OS dispatcher, or distro name gate to ordinary proof commands.
Linux facilities and tool capabilities determine support.

Only add code and assets with documented provenance and MIT-compatible terms.
Do not commit external checkouts, papers, warm profiles, local logs, credentials,
machine configuration, or old private Git history.
