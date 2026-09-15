#!/usr/bin/env python3
"""Cold-free regression for inferred project closure and exact artifact transport."""

from __future__ import annotations

import tempfile
from pathlib import Path

from hol_workbench.source_dependency_closure import (
    build_source_dependency_closure,
    source_dependency_closure_identity_matches,
)
from hol_workbench.source_dependency_package import (
    dependency_transport_status,
    literal_elf_artifact_package_prelude,
    materialize_dependency_package,
)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="holwb-source-dependency-") as temporary:
        root = Path(temporary) / "mlkem-lane"
        stable = root / "stable.ml"
        stable.parent.mkdir(parents=True)
        stable.write_text("let STABLE = prove(`T`,REWRITE_TAC[]);;\n", encoding="utf-8")
        artifact = root / "build" / "proof" / "target.o"
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"exact-object-v1")
        source = root / "attempts" / "target.ml"
        source.parent.mkdir()
        source.write_text(
            'needs "../stable.ml";;\n'
            f'let target_mc = define_assert_from_elf "target_mc" "{artifact}" [];;\n'
            "let TARGET = prove(`T`,REWRITE_TAC[]);;\n",
            encoding="utf-8",
        )

        closure = build_source_dependency_closure(source)
        assert closure["root"] == str(root.resolve())
        assert closure["boundary"]["kind"] == "inferred_literal_closure"
        assert closure["records"][0]["resolution"] == "source_local"
        assert closure["artifacts"][0]["resolution"] == "source_local"
        assert closure["semantic_identity_complete"] is True
        assert source_dependency_closure_identity_matches(closure) is True
        assert dependency_transport_status(closure)[0] == "packaged"

        package = Path(temporary) / "package"
        entrypoint, manifest = materialize_dependency_package(
            source=source,
            closure=closure,
            destination=package,
        )
        assert entrypoint == package / "attempts" / "target.ml"
        packaged_artifact = package / "build" / "proof" / "target.o"
        assert packaged_artifact.read_bytes() == artifact.read_bytes()
        prelude = "\n".join(literal_elf_artifact_package_prelude(manifest))
        assert str(artifact) in prelude
        assert str(packaged_artifact) in prelude

        first_identity = closure["strict_sha256"]
        artifact.write_bytes(b"exact-object-v2")
        assert build_source_dependency_closure(source)["strict_sha256"] != first_identity

    print("source-dependency self-test: inferred closure and ELF transport passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
