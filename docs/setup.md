# Linux setup

## Tool checks

Install Python 3.11+ and Bash through your distribution. For current Debian and
Ubuntu releases with a suitable Python package:

```sh
sudo apt-get update
sudo apt-get install python3 git
./hearth check
```

The launcher uses `python3` from PATH. An explicit absolute
`HOL_WORKBENCH_PYTHON` may select another interpreter. There are no third-party
Python runtime packages.

## Create the first profile

Use a clean Git clone. The builder records its exact commit and refuses
uncommitted changes before loading HOL.

```sh
sudo apt-get update
sudo apt-get install --no-install-recommends python3 git make gcc ocaml-nox \
  ocaml-findlib camlp5 libzarith-ocaml-dev libcamlp-streams-ocaml-dev criu sudo
./hearth setup --check
./hearth setup
./hearth doctor
./hearth demo cat-map
```

The source build requires OCaml 4.14+ and strict-mode Camlp5. Setup checks these
capabilities, the OCaml packages, CRIU authority, and the kernel before fetching
or loading HOL. If sudo needs a password, run `sudo -v` in the same shell.
Passwordless sudo is supported without requiring a separate `sudo -v`.
Run Hearth as your ordinary user; only CRIU and distribution package commands
use sudo. On a host using expiring sudo credentials, later restores may also
need `sudo -v`.

Setup fetches the exact HOL commit in [setup-lock.json](../dev/setup-lock.json),
compiles its module and the live evaluator, then loads
[`light.ml`](../profiles/light.ml), checkpoints it, restores it and checks its
smoke theorems. It builds no optional heavy or assembly profiles. Progress is
printed every 20 seconds, with a log path for each step.

Sources, snapshots and setup receipts go under
`~/.local/share/hol-hearth` (or `$XDG_DATA_HOME/hol-hearth`). They remain local
build artifacts with their upstream licenses. Keep this directory in place:
its absolute paths are part of the local runtime. The setup uses HOL's compiled
module mode (`HOLLIGHT_USE_MODULE=1`), including the upstream source inliner;
see [the upstream build description](https://github.com/jrh13/hol-light/blob/2a1cea8f1cb7f3885a60d947ba06eabac6ec1d32/README#L241).

`setup` is safe to repeat for an already compatible profile: it reuses that
profile and builds the evaluator for the current clone. A failed or incompatible
existing shelf is preserved for diagnosis; setup does not silently cold-rebuild
it. Read the printed log and `build-results.json` before retrying.

For a separate test environment, keep both the config and data separate:

```sh
export HOL_WORKBENCH_RUNTIME_CONFIG=/ABS/hearth-test/runtime.toml
./hearth setup --data-dir /ABS/hearth-test/data
./hearth demo balance
```

An existing config with different paths is preserved. For an installed
file-capability CRIU binary, `--criu-mode capability --criu-bin /ABS/criu` selects
that authority explicitly. Setup does not alter sudo rules or grant file
capabilities. Use `./hearth setup --help` for all options.

## Existing HOL and CRIU environment

The proof route needs a working HOL Light checkout and compatible published
CRIU profile on the same host. Configure their locations:

```sh
./hearth configure \
  --hol-light-dir /ABS/hol-light \
  --criu-shelf-root /ABS/warm-shelves \
  --criu-bin /ABS/criu \
  --criu-mode sudo
./hearth doctor --profile light
```

Configuration is stored in `~/.config/hol-hearth/runtime.toml`.
`HOL_WORKBENCH_RUNTIME_CONFIG` can name a separate configuration file.
Hearth does not alter another Workbench installation's configuration.

A CRIU snapshot is tied to its runtime, libraries, paths, architecture and
kernel environment. Installing a CRIU package does not create a compatible
HOL snapshot. Do not copy arbitrary snapshot images between machines.

## Live evaluator

Use the same OCaml compiler as the HOL environment:

```sh
./hearth build-native --ocamlc /ABS/hol-compiler/bin/ocamlc
```

This compiles two small modules and runs parser/error-location checks.
It does not load HOL, fetch packages, change compiler switches or use Dune.

The [profile recipes](../profiles) are available as standalone `.ml` files,
with exact bytes checked against the runtime manifest. A profile is generated
from those recipes and the selected upstream HOL/runtime inputs; see
[profile generation](profiles.md) and [validation](validation.md) for the
generation contract and tested platforms.
