# Proof checks and evidence

Run once and inspect the result:

```sh
./hearth prove /ABS/source.ml --profile light --run-root /ABS/runs
./hearth inspect /ABS/runs
./hearth inspect /ABS/runs --tail 20
```

A successful recorded run means the complete source was evaluated by HOL Light
in the configured warm environment. The receipt binds the source bytes and
records nonce-bound named theorem probes. An exit code alone is not enough.

Use `--loop` for a dedicated multi-edit session, without `--run-root`:

```sh
./hearth prove /ABS/source.ml --profile light --loop
```

Live-loop success is authoring feedback. It does not produce a durable receipt
per edit. Run a recorded check after the edit you intend to retain.

If execution stops before theorem probes, named bindings remain missing.
Natural transcript text is a diagnostic, not verified binding evidence.
Independent publication replay and review of a proof's assumptions remain
separate from warm authoring acceptance.

Profiles describe a preloaded basis. `light` is suitable for the original demos.
`heavy`, `probability` and optional assembly profiles require separately
provisioned local snapshots. `./hearth profiles` lists recipes; it does not
claim they are installed. `./hearth doctor` checks availability.

HOL source is executable OCaml. Run only source you trust on a machine whose
files that process is permitted to access. A disposable fork isolates mutable
proof state; it is not an operating-system security sandbox.
