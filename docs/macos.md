# Run Hearth on a Mac through OrbStack

Hearth runs inside an OrbStack **Linux machine**. Its HOL processes, Python,
Bash and CRIU all run there; the launcher does not dispatch from native macOS.
Install [OrbStack](https://docs.orbstack.dev/install), then create and enter a
machine from a Mac terminal:

```sh
orb create ubuntu hearth
orb -m hearth
```

These commands follow the [OrbStack machine guide](https://docs.orbstack.dev/machines/).
Use the default native architecture. Hearth has no validation for checkpointing
an emulated x86 process on Apple Silicon.

## Install inside Linux

Start in the Linux home directory, since the incoming shell may retain the Mac
working directory. Run the following **inside the machine**:

```sh
cd ~
sudo apt-get update
sudo apt-get install --no-install-recommends python3 git make gcc ocaml-nox \
  ocaml-findlib camlp5 libzarith-ocaml-dev libcamlp-streams-ocaml-dev criu sudo
mkdir -p src
cd src
git clone https://github.com/BenKnill/hol-hearth.git
cd hol-hearth
python3 --version
./hearth setup --check
./hearth setup
./hearth doctor --profile light
```

Python must be 3.11 or newer. Setup checks the distribution toolchain, CRIU
authority and kernel before fetching/building the pinned HOL sources. The
ordinary user runs Hearth; CRIU maintenance uses `sudo` by default. OrbStack
normally configures passwordless sudo in new machines, as described in its
[root-access documentation](https://docs.orbstack.dev/machines/#root-access).
For an existing profile, follow [existing-environment setup](setup.md#existing-hol-and-criu-environment)
instead of rebuilding it.

If `setup --check` reports a missing kernel capability or denied CRIU operation,
keep its output and resolve that environment limitation before running setup.
This repository does not establish a minimum OrbStack release, a known-good
OrbStack/kernel pair, or a setting that makes every release support CRIU.
[Recorded Linux installation results](validation.md#historical-clean-installation-evidence)
identify the tested distributions and CRIU versions; they are not a universal
OrbStack compatibility matrix. Record `orb version` on the Mac and `uname -r`,
`criu --version` and `/etc/os-release` inside Linux when reporting a problem.

## Keep the runtime and project in Linux

Keep HOL sources, the profile shelf, project checkouts and run roots in the
machine's Linux home filesystem. Use the same stable Linux absolute paths for
setup, proofs and inspection. This avoids adding a host-file-sharing mount to
the snapshot's runtime dependencies; no measured performance claim is implied.
CRIU images remain specific to their recorded runtime, paths and kernel
environment. A VM copy or OrbStack update is not proof of snapshot compatibility.

Mac editors can access Linux files through Finder's OrbStack entry or
`~/OrbStack`; Linux can access Mac files under `/mnt/mac`. See
[OrbStack file sharing](https://docs.orbstack.dev/machines/file-sharing).
Editing the Linux project this way keeps the command workflow in the machine:

```sh
./hearth prove /ABS/project/proofs/leaf.ml --profile light \
  --timeout 120 --run-root /ABS/project/runs
./hearth inspect /ABS/project/runs --binding TARGET_THEOREM
```

## Compatibility names

No OrbStack-specific Hearth environment variable is required for this route.
`hearth setup` writes the ordinary Linux runtime configuration. Existing
installations can select theirs with `HOL_WORKBENCH_RUNTIME_CONFIG`.

- `HOL_WORKBENCH_ORB_HOLDIR` remains an optional absolute Linux HOL-directory
  override. Prefer runtime configuration; setup detects conflicting overrides.
- `HOL_WORKBENCH_PROCESS_NAMESPACE` and `HOL_WORKBENCH_ORB_MACHINE` label
  process-ownership records for external integrations using
  `HOL_WORKBENCH_CONTEXT_OWNERSHIP_DIR`. They neither enter a VM nor select a
  profile. Ordinary in-machine use keeps the default local process namespace.
- Internal `orbstack_criu_*` module names and `hol-workbench.*` receipt schemas
  remain compatible with existing Linux profiles and receipts.
