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

## New proof environments

This initial release exposes the authoring tools and original demos.
Automated first-profile provisioning has not been validated as a fresh-machine
installation. Do not treat the source-only checks as proof-runtime setup.
See the validation record for the exact tested scope.
