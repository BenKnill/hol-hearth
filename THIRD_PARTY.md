# Licensing and external tools

HOL Hearth's source, tests, installation fixture and documentation are MIT licensed.
This repository contains no downloaded paper text, third-party proof corpus,
external source checkout, warm memory image, or third-party executable.

The tools you install separately keep their own licenses:

| Tool | Use | Upstream licensing |
| --- | --- | --- |
| [HOL Light](https://github.com/jrh13/hol-light) | Proof kernel and libraries | [BSD-style license, with per-file exceptions](https://github.com/jrh13/hol-light/blob/master/LICENSE) |
| [CRIU](https://github.com/checkpoint-restore/criu) | Separate checkpoint/restore executable | [GPL-2.0; library exceptions](https://github.com/checkpoint-restore/criu/blob/criu-dev/COPYING) |
| [OCaml](https://github.com/ocaml/ocaml) | HOL source build and runtime | [Upstream LICENSE](https://github.com/ocaml/ocaml/blob/trunk/LICENSE) |
| [Python](https://www.python.org/) | Orchestration and receipt reading | [PSF license](https://docs.python.org/3/license.html) |
| [s2n-bignum](https://github.com/awslabs/s2n-bignum) | Optional assembly proof profiles | [Upstream licenses and per-file notices](https://github.com/awslabs/s2n-bignum#license) |

CRIU is invoked as a separate program. HOL Hearth does not link to libcriu or
include CRIU source. The MIT license does not relicense these external tools.
If you distribute a combined runtime or memory image, review its complete
contents and retain all applicable upstream notices and obligations.

This public repository starts with a fresh history. Imported first-party
orchestration code was selected separately from experimental case studies;
the original cryptography papers and proof copies are excluded. The retained
installation fixture is an original first-party example.

The [ML and profile source review](docs/source-provenance.md) records file-level
provenance, recipe dependencies and the ML-KEM statement attribution. Its hash
inventory is checked by `./hearth check` when these sources change.
