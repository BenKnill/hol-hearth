#!/usr/bin/env python3
"""Cold-free regression for inferred project closure and exact artifact transport."""

from __future__ import annotations

import hashlib
import copy
import tempfile
from pathlib import Path
from unittest.mock import patch

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
from hol_workbench.source_execution_plan import capture_source_dependency_closure, source_execution_prelude


def repository_source_contract(root: Path) -> None:
    """A selected clone owns root-relative imports before an ambient profile cwd."""
    clone, ambient, holdir = (root / name for name in ("clone", "other-checkout", "holdir"))
    for checkout in (clone, ambient):
        (checkout / ".git").mkdir(parents=True)
        (checkout / "arm/proofs").mkdir(parents=True)
        (checkout / "arm/objects").mkdir(parents=True)
    (holdir / "Library").mkdir(parents=True)
    library = holdir / "Library" / "support.ml"
    library.write_text("let library_support = 1;;\n")
    source = clone / "arm/proofs/leaf.ml"
    helper = clone / "arm/proofs/helper.ml"
    nested = clone / "arm/proofs/nested.ml"
    artifact = clone / "arm/objects/code.o"
    source.write_text('needs "arm/proofs/helper.ml";;\nneeds "Library/support.ml";;\n')
    helper.write_text('needs "arm/proofs/nested.ml";;\n')
    nested.write_text('let code = define_assert_from_elf "code" "arm/objects/code.o" [];;\n')
    artifact.write_bytes(b"selected clone object")
    for name in ("helper.ml", "nested.ml"):
        (ambient / "arm/proofs" / name).write_text('failwith "ambient checkout must not load";;\n')
    (ambient / "arm/objects/code.o").write_bytes(b"ambient object must not substitute")

    def capture() -> dict:
        with patch("hol_workbench.source_execution_plan.machine_holdir_authority", return_value=holdir):
            closure, _ = capture_source_dependency_closure(
                source, profile_cwd=ambient, legacy_holdir_roots=(), logical_source_root_declarations=())
        return closure

    closure = capture()
    assert closure["root"] == closure["project_root"] == str(clone)
    assert closure["boundary"]["kind"] == "nearest_repository_root"
    assert closure["semantic_identity_complete"]
    assert dependency_transport_status(closure)[0] == "packaged"
    assert {row["resolved_path"] for row in closure["records"]} == {str(helper), str(nested), str(library)}
    assert closure["artifacts"][0]["resolved_path"] == str(artifact)
    assert closure["records"][0]["resolution_base"] == "source_package_root"
    assert closure["records"][1]["resolution_base"] == "source_package_root"
    assert closure["records"][2]["resolution"] == "holdir_source"
    package_root = root / "packaged"
    entry, package = materialize_dependency_package(source=source, closure=closure, destination=package_root)
    assert entry == package_root / "arm/proofs/leaf.ml"
    assert (package_root / "arm/objects/code.o").read_bytes() == artifact.read_bytes()
    prelude = source_execution_prelude(package_root=package_root, virtual_entrypoint=entry,
                                       closure=closure, profile_satisfaction=None).decode()
    assert f'("{package_root / "arm/proofs"}","arm/proofs/helper.ml")' in prelude
    assert str(package_root / "arm/proofs/helper.ml") in prelude
    assert str(ambient) not in prelude
    modified_provenance = copy.deepcopy(closure)
    modified_provenance["records"][0]["declaring_path"] = str(clone / "somewhere/else.ml")
    assert source_dependency_closure_identity_matches(modified_provenance)
    assert source_execution_prelude(package_root=package_root, virtual_entrypoint=entry,
                                    closure=modified_provenance, profile_satisfaction=None).decode() == prelude

    # Missing transitive ELF bytes fail the actual package admission before HOL.
    artifact.unlink()
    missing_object = capture()
    assert missing_object["artifacts"][0]["resolution"] == "unresolved"
    assert dependency_transport_status(missing_object)[0] != "packaged"
    try:
        materialize_dependency_package(source=source, closure=missing_object, destination=root / "missing-object")
    except RuntimeError:
        pass
    else:
        raise AssertionError("missing transitive object must refuse package admission")
    artifact.write_bytes(b"selected clone object")

    # The local arm namespace owns a missing source, despite an ambient copy.
    helper.unlink()
    missing_source = capture()
    assert missing_source["records"][0]["resolution"] == "unresolved_source_root"
    assert dependency_transport_status(missing_source)[0] == "refused_missing_source_dependency"
    assert all(not str(row.get("resolved_path", "")).startswith(str(ambient)) for row in missing_source["records"])
    helper.write_text('needs "arm/proofs/nested.ml";;\n')

    # Declaring-file siblings retain priority over root-relative names. The
    # exact root mapping is keyed by declaring directory, never a global alias.
    sibling = source.parent / "arm/proofs/helper.ml"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("let sibling = 2;;\n")
    sibling_closure = capture()
    assert sibling_closure["records"][0]["resolved_path"] == str(sibling)
    assert "resolution_base" not in sibling_closure["records"][0]
    sibling.unlink()
    sibling.mkdir()
    directory_shadow = capture()
    assert directory_shadow["records"][0]["resolved_path"] == str(helper)
    assert directory_shadow["records"][0]["resolution_base"] == "source_package_root"
    sibling.rmdir()

    original_source = source.read_bytes()
    sub_source = source.parent / "sub/foo.ml"
    common = clone / "common/bar.ml"
    sub_source.parent.mkdir()
    common.parent.mkdir()
    sub_source.write_text('needs "common/bar.ml";;\n')
    common.write_text("let common = 3;;\n")
    source.write_text('needs "./sub/foo.ml";;\n')
    dotted_closure = capture()
    assert dotted_closure["records"][1]["resolution_base"] == "source_package_root"
    dotted_prelude = source_execution_prelude(
        package_root=package_root, virtual_entrypoint=entry, closure=dotted_closure,
        profile_satisfaction=None).decode()
    assert f'("{package_root / "arm/proofs/sub"}","common/bar.ml")' in dotted_prelude
    assert "proof_run_normalized_source_dir (Filename.dirname local_path)" in dotted_prelude
    source.write_bytes(original_source)

    # Explicit package boundaries still win; symlinked repository markers do not.
    marker = source.parent / ".hol-workbench-source-root"
    marker.write_text("explicit narrow package\n")
    assert capture()["root"] == str(source.parent)
    assert capture()["boundary"]["kind"] == "source_root_marker"
    marker.unlink()
    (clone / ".git").rmdir()
    (clone / ".git").symlink_to(ambient / ".git", target_is_directory=True)
    assert capture()["boundary"]["kind"] == "entrypoint_parent"


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
        repository_source_contract(Path(temporary) / "repository-contract")
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

    print("source-dependency self-test: selected repository roots, absolute warm identity, inferred closure and ELF transport passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
