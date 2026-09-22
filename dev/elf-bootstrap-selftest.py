#!/usr/bin/env python3
"""Exact ELF transport and source-defined loader regressions, without HOL."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))

from hol_workbench import project_basis as basis
from hol_workbench.source_dependency_closure import build_source_dependency_closure
from hol_workbench.source_dependency_package import (
    DependencyPackageError,
    dependency_package_entrypoint,
    elf_package_transport,
    literal_elf_artifact_runtime_cwd,
    materialize_dependency_package,
)
from hol_workbench.source_execution_plan import source_execution_prelude
from hol_workbench.vanilla_claims import vanilla_artifact_runtime_cwd


class ElfBootstrap(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="hearth-elf-bootstrap-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "project"
        self.source = self.project / "arm/proofs/basis.ml"
        self.decoder = self.source.with_name("decoder.ml")
        self.source.parent.mkdir(parents=True)
        (self.project / ".hol-workbench-source-root").write_text("\n")
        self.decoder.write_text(
            "let define_from_elf name path = (name,path);;\n"
            "let define_assert_from_elf name path bytes = (name,path,bytes);;\n"
        )
        self.objects = [self.project / "arm/objects/a.o", self.project / "arm/objects/b.o"]
        self.objects[0].parent.mkdir()
        for index, path in enumerate(self.objects):
            path.write_bytes(b"captured object fixture " + bytes([index]))
        self.original = (
            b'needs "arm/proofs/decoder.ml";;\n'
            b'let a = define_from_elf "a" "arm/objects/a.o";;\n'
            b'let b = define_assert_from_elf "b" "arm/objects/b.o" [0];;\n'
        )
        self.source.write_bytes(self.original)
        self.package = self.root / "package"
        self.profile = self.root / "unrelated-published-shelf"
        self.profile.mkdir()
        (self.profile / "snapshot-manifest.json").write_text('{"profile_basis_id":"test"}\n')
        self.closure = self.capture()

    def capture(self) -> dict:
        return build_source_dependency_closure(self.source, project_root=self.project)

    def plan(self) -> basis.BasisPlan:
        return basis.plan_basis(
            self.source, profile_root=self.profile, logical_profile="heavy",
            run_root=self.root / "runs", closure=self.closure,
        )

    def prelude(self) -> bytes:
        return source_execution_prelude(
            package_root=self.package,
            virtual_entrypoint=dependency_package_entrypoint(self.package, self.closure),
            closure=self.closure, profile_satisfaction=None,
        )

    def test_source_defines_elf_loaders_in_original_order_inside_exact_package(self) -> None:
        strategy = elf_package_transport(self.closure)
        self.assertEqual(strategy["mode"], "package_cwd")
        self.assertEqual(strategy["cwd_from_package_root"], ".")
        self.assertEqual(strategy["wrapped_loaders"], [])
        prefix = self.prelude()
        for name in (b"define_from_elf", b"define_assert_from_elf"):
            self.assertNotIn(name, prefix)
        self.assertIn(f'Sys.chdir "{self.package}";;'.encode(), prefix)
        packaged_source, manifest = materialize_dependency_package(
            source=self.source, closure=self.closure, destination=self.package,
            entrypoint_output_bytes=prefix + self.original,
        )
        self.assertEqual(packaged_source.read_bytes()[len(prefix):], self.original)
        self.assertEqual((self.package / "arm/proofs/decoder.ml").read_bytes(), self.decoder.read_bytes())
        object_rows = [row for row in manifest["files"] if row["role"] == "literal_elf_artifact"]
        self.assertEqual(len(object_rows), 2)
        for row in self.closure["artifacts"]:
            target = self.package / row["runtime_literal_path"]
            self.assertEqual(target, self.package / row["package_path"])
            self.assertEqual(target.read_bytes(), Path(row["resolved_path"]).read_bytes())

    def test_basis_does_not_save_or_restore_loaders_supplied_later_by_source(self) -> None:
        plan = self.plan()
        prefix, suffix = basis.bootstrap_prelude(plan), basis.bootstrap_postlude(plan)
        for name in (b"define_from_elf", b"define_assert_from_elf"):
            self.assertNotIn(name, prefix)
            self.assertNotIn(name, suffix)
        self.assertIn(b"let hearth_basis_original_needs = needs;;", prefix)
        self.assertIn(b"let needs = hearth_basis_original_needs;;", suffix)
        self.assertIn(b"Sys.chdir hearth_basis_original_cwd;;", suffix)

    def test_strict_cwd_helper_preserves_existing_cold_contract(self) -> None:
        options = {"original_cwd": self.project, "package_root": self.package}
        self.assertEqual(literal_elf_artifact_runtime_cwd(self.closure, **options), self.package)
        self.assertEqual(vanilla_artifact_runtime_cwd(self.closure, **options), self.package)
        with self.assertRaises(DependencyPackageError):
            literal_elf_artifact_runtime_cwd(
                self.closure, original_cwd=self.profile, package_root=self.package,
            )
        # Warm selection uses the captured source project, not that unrelated shelf.
        self.assertEqual(elf_package_transport(self.closure)["mode"], "package_cwd")

    def test_strict_cwd_helper_rejects_absolute_escape_and_inconsistent_package_paths(self) -> None:
        mutations = (
            ("runtime_literal_path", str(self.objects[0])),
            ("runtime_literal_path", "../../../outside.o"),
            ("package_path", "../outside.o"),
            ("package_path", "__other__/arm/objects/a.o"),
        )
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                closure = deepcopy(self.closure)
                closure["artifacts"][0][key] = value
                with self.assertRaises(DependencyPackageError):
                    literal_elf_artifact_runtime_cwd(
                        closure, original_cwd=self.project, package_root=self.package,
                    )
                self.assertNotEqual(elf_package_transport(closure)["mode"], "package_cwd")

    def test_absolute_literal_preserves_mapping_and_only_its_used_loader(self) -> None:
        self.source.write_text(
            'needs "arm/proofs/decoder.ml";;\n'
            f'let a = define_from_elf "a" "{self.objects[0]}";;\n'
        )
        self.closure = self.capture()
        strategy = elf_package_transport(self.closure)
        self.assertEqual(strategy["mode"], "mapped_loaders")
        self.assertEqual(strategy["wrapped_loaders"], ["define_from_elf"])
        prefix = self.prelude()
        self.assertIn(b"let proof_run_unmapped_define_from_elf = define_from_elf;;", prefix)
        self.assertNotIn(b"define_assert_from_elf", prefix)
        plan = self.plan()
        saved, restored = basis.bootstrap_prelude(plan), basis.bootstrap_postlude(plan)
        self.assertIn(b"let hearth_basis_original_define_from_elf = define_from_elf;;", saved)
        self.assertIn(b"let define_from_elf = hearth_basis_original_define_from_elf;;", restored)
        self.assertNotIn(b"define_assert_from_elf", saved + restored)

    def test_source_or_imported_cwd_mutation_keeps_loader_mapping(self) -> None:
        controls = (
            'Sys.chdir "elsewhere";;\n',
            "let move = Unix.chdir;;\n",
            "let move = Unix.fchdir;;\n",
            'external native_move : string -> unit = "custom_native_move";;\n',
        )
        for path in (self.source, self.decoder):
            original = path.read_bytes()
            for control in controls:
                with self.subTest(source=path.name, control=control):
                    path.write_bytes(control.encode() + original)
                    closure = self.capture()
                    self.assertEqual(elf_package_transport(closure)["mode"], "mapped_loaders")
                    path.write_bytes(original)

    def test_comments_and_strings_do_not_create_cwd_mutation(self) -> None:
        self.decoder.write_text(
            self.decoder.read_text() + '\n(* Sys.chdir Unix.fchdir external *)\n'
            'let note = "Sys.chdir Unix.fchdir external";;\n'
        )
        self.assertEqual(elf_package_transport(self.capture())["mode"], "package_cwd")

    def test_capture_is_revalidated_before_eliding_elf_wrappers(self) -> None:
        for path in (self.source, self.decoder):
            with self.subTest(source=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b'\nSys.chdir "elsewhere";;\n')
                self.assertNotEqual(elf_package_transport(self.closure)["mode"], "package_cwd")
                path.write_bytes(original)
                self.assertEqual(elf_package_transport(self.closure)["mode"], "package_cwd")

    def test_no_elf_input_requires_no_loader_transport(self) -> None:
        self.source.write_text('needs "arm/proofs/decoder.ml";;\nlet pure_value = 1;;\n')
        strategy = elf_package_transport(self.capture())
        self.assertEqual(strategy["mode"], "none")
        self.assertEqual(strategy["wrapped_loaders"], [])


if __name__ == "__main__":
    unittest.main()
