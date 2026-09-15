# Profiles are generated from source

The checked-in [ML recipes](../profiles) contain the exact bytes used by the
public profile builder. Their source of truth is
[`warmup-profiles.json`](../hol-workbench/warmup-profiles.json), and
`./hearth check` verifies that both representations agree. For example,
[`light.ml`](../profiles/light.ml) loads arithmetic and ring theory and proves
small smoke-test theorems. It contains no copy of those upstream libraries.

The build path is:

1. Start the selected HOL Light runtime from its sources.
2. Load the profile's ML recipe and the declared runtime support files.
3. Capture source, loaded-file and runtime identities.
4. Checkpoint the warm process with CRIU, restore it, and run smoke checks.
5. Publish the successful local profile to the configured shelf.

The recipe bytes and their SHA-256 are deterministic. Rebuilding the same
logical basis also requires pinned upstream source revisions and a compatible
toolchain. The resulting CRIU images contain process state and depend on the
host environment; byte-identical images are not promised.

First-time provisioning still needs end-to-end validation on a clean machine.
The runtime currently accepts a configured HOL source tree; this release has
not yet supplied and validated a universal HOL/toolchain lock for fresh builds.
The checked-in recipes address source availability, not that remaining test.

## Licensing

Hearth's recipes and small compaction helper are first-party code, released
under MIT. The ML-KEM smoke recipe references an upstream theorem statement;
its provenance and the upstream MIT-0 option are recorded in
[the source review](source-provenance.md).

Loading a library does not replace its license with Hearth's license.
Keep fetched HOL and optional s2n sources separate, with their original notices.
A local CRIU image contains the loaded runtime and libraries. This repository
does not distribute such images or claim they are wholly MIT licensed.
