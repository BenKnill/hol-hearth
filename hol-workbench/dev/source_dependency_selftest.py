#!/usr/bin/env python3
"""Cold-free regression for inferred project closure and exact artifact transport."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from hol_workbench.profile_satisfied_dependencies import (
    ProfileSatisfactionError,
    _validated_edge,
)
from hol_workbench.source_dependency_closure import (
    build_source_dependency_closure,
    source_dependency_closure_identity_matches,
)
from hol_workbench.source_dependency_package import (
    dependency_transport_status,
    literal_elf_artifact_package_prelude,
    materialize_dependency_package,
)


def absolute_holdir_capture_contract(root: Path) -> None:
    """Capture warm absolute needs without admitting arbitrary external loads."""
    holdir = root / "hol"
    library = holdir / "Library" / "words.ml"
    library.parent.mkdir(parents=True)
    library_bytes = b'needs "Library/other.ml";;\nlet WORD_BASIS = prove(`T`,REWRITE_TAC[]);;\n'
    library.write_bytes(library_bytes)
    project = root / "project"
    project.mkdir()
    source = project / "leaf.ml"

    def capture(literal: str, *, loader: str = "needs") -> dict:
        source.write_text(f'{loader} "{literal}";;\n', encoding="utf-8")
        return build_source_dependency_closure(source, holdir_root=holdir)

    closure = capture(str(library))
    assert source_dependency_closure_identity_matches(closure)
    assert len(closure["records"]) == 1, "warm library contents must not be traversed"
    record = closure["records"][0]
    inventory = {
        "path_kind": "holdir_relative",
        "path": "Library/words.ml",
        "basename": "words.ml",
        "loader_md5": hashlib.md5(library_bytes, usedforsecurity=False).hexdigest(),
        "sha256": hashlib.sha256(library_bytes).hexdigest(),
        "size_bytes": len(library_bytes),
    }

    def validate(captured: dict, *, entries: list[dict] | None = None) -> dict:
        # This checks only the real file/loaded-inventory comparison. It makes
        # no claim that a synthetic inventory is an admitted runtime shelf.
        return _validated_edge(
            captured,
            record_index=0,
            relative=Path("Library/words.ml"),
            host_holdir=holdir,
            shelf_holdir=holdir,
            entries=[inventory] if entries is None else entries,
        )

    edge = validate(record)
    assert edge["resolution"] == "profile_satisfied"
    assert record["sha256"] == inventory["sha256"]
    assert record["size_bytes"] == len(library_bytes)
    assert record["trusted_root_path"] == str(holdir)
    assert record["resolution"] == "external_resolved"
    assert record["traversal"] == "not_followed"
    assert "package_path" not in record
    assert dependency_transport_status(closure)[0] == "refused_external_runtime_dependency"

    def require_refusal(captured: dict, status: str, *, entries: list[dict] | None = None) -> None:
        try:
            validate(captured, entries=entries)
        except ProfileSatisfactionError as exc:
            assert exc.status == status, exc.status
        else:
            raise AssertionError(f"expected {status}")

    require_refusal(record, "refused_profile_satisfaction_uninventoried", entries=[])
    library.write_bytes(library_bytes + b"(* edited after capture *)\n")
    require_refusal(record, "refused_profile_satisfaction_dependency_changed")
    assert capture(str(library))["strict_sha256"] != closure["strict_sha256"]
    library.write_bytes(library_bytes)

    outside = root / "hol-other" / "Library" / "words.ml"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(library_bytes)
    (holdir / "Library" / "alias.ml").symlink_to(library)
    (holdir / "Library" / "outside.ml").symlink_to(outside)
    (holdir / "alias-dir").symlink_to(library.parent, target_is_directory=True)
    unsafe_literals = [
        str(outside),
        str(holdir / "Library" / "alias.ml"),
        str(holdir / "Library" / "outside.ml"),
        str(holdir / "alias-dir" / "words.ml"),
        f"{holdir}/Library/../Library/words.ml",
        f"{holdir}/Library/./words.ml",
    ]
    for literal in unsafe_literals:
        unsafe = capture(literal)
        unsafe_record = unsafe["records"][0]
        assert "sha256" not in unsafe_record, literal
        assert "size_bytes" not in unsafe_record, literal
        assert dependency_transport_status(unsafe)[0] == "refused_external_runtime_dependency", literal
    traversal = capture(f"{holdir}/Library/../Library/words.ml")["records"][0]
    require_refusal(traversal, "refused_profile_satisfaction_mapping_alias")
    forced_reload = capture(str(library), loader="loadt")
    assert "sha256" not in forced_reload["records"][0]
    assert dependency_transport_status(forced_reload)[0] == "refused_external_runtime_dependency"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="holwb-source-dependency-") as temporary:
        absolute_holdir_capture_contract(Path(temporary))
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

    print("source-dependency self-test: absolute warm identity, inferred closure and ELF transport passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
