#!/usr/bin/env python3
"""Captured clone identity versus synthetic loaded inventory; no HOL or CRIU."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))

from hol_workbench import profile_satisfied_dependencies as satisfaction
from hol_workbench.criu_snapshot_admission import (
    SnapshotAdmissionMode, SnapshotAdmissionRequest, StaticSnapshotAdmissionDecision,
)
from hol_workbench.source_dependency_closure import build_source_dependency_closure, source_dependency_closure_identity_matches
from hol_workbench.source_dependency_package import dependency_transport_status, materialize_dependency_package
from hol_workbench.source_execution_plan import decide_profile_satisfaction


class CloneSatisfaction(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="hearth-clone-satisfaction-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clone = self.root / "clone"
        self.shelf_project = self.root / "shelf-project"
        self.holdir = self.root / "hol"
        self.profile = self.root / "profile"
        for directory in (self.clone, self.shelf_project, self.holdir, self.profile):
            directory.mkdir()
        (self.clone / ".hol-workbench-source-root").write_text("\n")
        (self.holdir / ".hol-workbench-source-root").write_text("\n")
        self.files = {
            "arm/proofs/base.ml": 'needs "common/helper.ml";;\nloadt "common/loaded.ml";;\nneeds "Library/words.ml";;\n',
            "common/helper.ml": "let STABLE_HELPER = 1;;\n",
            "common/loaded.ml": "let LOADED_HELPER = 2;;\n",
        }
        self.entries = []
        for relative, text in self.files.items():
            self.write(self.clone / relative, text)
            path = self.shelf_project / relative
            self.write(path, text)
            self.entries.append(self.inventory(path, relative, holdir=False))
        for relative, text in {
            "Library/words.ml": 'needs "Library/child.ml";;\nlet WORDS_VERSION = 1;;\n',
            "Library/child.ml": "let HOL_CHILD = 3;;\n",
        }.items():
            path = self.holdir / relative
            self.write(path, text)
            self.entries.append(self.inventory(path, relative, holdir=True))
        self.source = self.clone / "arm/proofs/leaf.ml"
        self.write(self.source, 'needs "arm/proofs/base.ml";;\nneeds "custom/fresh.ml";;\n')
        self.write(self.clone / "custom/fresh.ml", "let FRESH_PROJECT_WORK = 4;;\n")
        admission = StaticSnapshotAdmissionDecision(
            request=SnapshotAdmissionRequest.create(required_capabilities={"seat_reusable", "proof_search_budget_binding_resets"}),
            manifest_path=str(self.profile / "snapshot-manifest.json"), manifest_sha256="a" * 64,
            profile_identity={"profile_basis_id": "test-basis", "execution_topology": "mechanical_basis_broker_v3",
                              "fork_snapshot_abi": "proof-run-fork-snapshot.v3",
                              "broker_protocol": "hol-workbench.fork-basis-broker.v1", "broker_runtime_sha256": "e" * 64},
            runtime_compatibility={"compatible": True, "status": "compatible", "binding_budget_owner": "current_controller"},
            mode=SnapshotAdmissionMode.FULL_BINDING_SCOPED, reason="synthetic inventory contract only",
        )
        self.manifest = {
            "static_admission_decision": admission.record(), "profile": "test", "profile_basis_id": "test-basis",
            "profile_sha256": "b" * 64, "snapshot_environment_sha256": "c" * 64,
            "profile_cwd": str(self.shelf_project),
            "snapshot_provenance": {"holdir": str(self.holdir),
                                    "loaded_closure": {"strict_sha256": "d" * 64, "entries": self.entries}},
        }
        self.admission = patch.object(satisfaction, "validate_snapshot_manifest", return_value=self.manifest)
        self.admission.start()
        self.addCleanup(self.admission.stop)

    def write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def inventory(self, path: Path, relative: str, *, holdir: bool) -> dict:
        data = path.read_bytes()
        return {"path_kind": "holdir_relative" if holdir else "absolute",
                "path": relative if holdir else str(path), "resolved_path": str(path),
                "roles": ["holdir" if holdir else "profile_cwd"], "basename": path.name,
                "sha256": hashlib.sha256(data).hexdigest(), "loader_md5": hashlib.md5(data, usedforsecurity=False).hexdigest(),
                "size_bytes": len(data)}

    def capture(self) -> dict:
        return build_source_dependency_closure(self.source, holdir_root=self.holdir)

    def decide(self, closure: dict | None = None) -> dict:
        decision, status, _ = decide_profile_satisfaction(
            self.capture() if closure is None else closure, profile_root=self.profile,
            logical_profile="test", profile_cwd=self.shelf_project, holdir_root=self.holdir)
        self.assertTrue(status.startswith("packaged"), status)
        self.assertIsNotNone(decision)
        return decision

    def test_exact_clone_retains_ordinary_package_and_attests_full_skipped_subtree(self) -> None:
        closure = self.capture()
        decision = self.decide(closure)
        self.assertEqual(decision["edges"], [])
        captured = decision["captured_warm_sources"]
        self.assertEqual(len(captured), 5)
        self.assertIn("loadt", {row["loader"] for row in captured})
        self.assertNotIn(str(self.clone / "custom/fresh.ml"), {row["host_path"] for row in captured})
        self.assertTrue(satisfaction.profile_satisfaction_identity_matches(decision, closure))
        self.assertEqual(dependency_transport_status(closure, profile_satisfaction=decision)[0], "packaged")
        _entrypoint, _package = materialize_dependency_package(
            source=self.source, closure=closure, destination=self.root / "package", profile_satisfaction=decision)
        self.assertEqual((self.root / "package/arm/proofs/base.ml").read_bytes(),
                         (self.clone / "arm/proofs/base.ml").read_bytes())
        # No replacement evaluator or forced reload is introduced for clones.
        self.assertEqual(satisfaction.profile_satisfied_needs_prelude(decision), [])

    def test_unchanged_warm_parent_does_not_hide_changed_needs_or_loadt_child(self) -> None:
        for relative in ("common/helper.ml", "common/loaded.ml"):
            with self.subTest(relative=relative):
                path = self.clone / relative
                original = path.read_bytes()
                path.write_bytes(b"let UNUSED_FALSE = prove (`F`,ALL_TAC);;\n" + original)
                with self.assertRaisesRegex(satisfaction.ProfileSatisfactionError, "differs from the admitted"):
                    self.decide()
                path.write_bytes(original)
        self.decide()

    def test_uninventoried_descendant_is_not_accepted_under_identical_parent(self) -> None:
        self.entries[:] = [row for row in self.entries if row["basename"] != "loaded.ml"]
        with self.assertRaisesRegex(satisfaction.ProfileSatisfactionError, "descendant.*no exact shelf"):
            self.decide()

    def test_unrelated_fresh_project_dependency_is_allowed_to_change(self) -> None:
        before = self.decide()["captured_warm_sources"]
        self.write(self.clone / "custom/fresh.ml", "let NEW_PROJECT_THEOREM = prove (`T`,REWRITE_TAC[]);;\n")
        self.assertEqual(self.decide()["captured_warm_sources"], before)

    def test_captured_hol_checkout_sources_use_exact_hol_relative_inventory(self) -> None:
        self.source = self.holdir / "Library/leaf.ml"
        self.write(self.source, 'needs "words.ml";;\n')
        closure = self.capture()
        self.assertEqual(closure["records"][0]["resolution"], "source_local")
        decision = self.decide(closure)
        self.assertEqual(len(decision["captured_warm_sources"]), 2)
        self.assertEqual(decision["captured_warm_sources"][0]["shelf_path"], str(self.holdir / "Library/words.ml"))

    def test_same_basename_and_bytes_in_unmapped_namespace_cannot_satisfy_warm_needs(self) -> None:
        self.write(self.clone / "custom/helper.ml", self.files["common/helper.ml"])
        self.write(self.source, 'needs "custom/helper.ml";;\n')
        with self.assertRaisesRegex(satisfaction.ProfileSatisfactionError, "without an exact source coordinate"):
            self.decide()

    def test_elf_inside_skipped_parent_needs_independent_object_attestation(self) -> None:
        base = self.files["arm/proofs/base.ml"] + 'let mc = define_from_elf "mc" "objects/inside.o";;\n'
        self.write(self.clone / "arm/proofs/base.ml", base)
        shelf_base = self.shelf_project / "arm/proofs/base.ml"
        self.write(shelf_base, base)
        self.entries[0] = self.inventory(shelf_base, "arm/proofs/base.ml", holdir=False)
        self.write(self.clone / "objects/inside.o", "exact synthetic object")
        with self.assertRaisesRegex(satisfaction.ProfileSatisfactionError, "without shelf object attestation"):
            self.decide()

    def test_reporting_paths_cannot_hide_changed_loadt_descendant_or_elf(self) -> None:
        self.write(self.clone / "common/loaded.ml", "let UNUSED_FALSE = prove (`F`,ALL_TAC);;\n")
        closure = self.capture()
        child = next(row for row in closure["records"] if row["declared_path"] == "common/loaded.ml")
        child["declaring_path"] = str(self.clone / "irrelevant.ml")
        self.assertTrue(source_dependency_closure_identity_matches(closure))
        with self.assertRaisesRegex(satisfaction.ProfileSatisfactionError, "differs from the admitted"):
            self.decide(closure)
        self.write(self.clone / "common/loaded.ml", self.files["common/loaded.ml"])
        base = self.files["arm/proofs/base.ml"] + 'let mc = define_from_elf "mc" "objects/inside.o";;\n'
        self.write(self.clone / "arm/proofs/base.ml", base)
        shelf_base = self.shelf_project / "arm/proofs/base.ml"
        self.write(shelf_base, base)
        self.entries[0] = self.inventory(shelf_base, "arm/proofs/base.ml", holdir=False)
        self.write(self.clone / "objects/inside.o", "exact synthetic object")
        closure = self.capture()
        closure["artifacts"][0]["declaring_path"] = str(self.clone / "irrelevant.ml")
        # Object readiness also checks its diagnostic path against projection.
        self.assertFalse(source_dependency_closure_identity_matches(closure))
        with self.assertRaisesRegex(satisfaction.ProfileSatisfactionError, "without shelf object attestation"):
            self.decide(closure)

    def test_exact_absolute_hol_descendant_uses_existing_shelf_edge_contract(self) -> None:
        words = self.holdir / "Library/words.ml"
        self.write(words, f'needs "{self.holdir / "Library/child.ml"}";;\n')
        self.entries[:] = [row for row in self.entries if row["basename"] != "words.ml"]
        self.entries.append(self.inventory(words, "Library/words.ml", holdir=True))
        closure = self.capture()
        decision = self.decide(closure)
        self.assertEqual(len(decision["edges"]), 1)
        self.assertEqual(decision["edges"][0]["host_path"], str(self.holdir / "Library/child.ml"))
        self.assertTrue(satisfaction.profile_satisfaction_identity_matches(decision, closure))
        self.write(self.holdir / "Library/child.ml", "let CHANGED = 1;;\n")
        with self.assertRaisesRegex(satisfaction.ProfileSatisfactionError, "no longer matches"):
            self.decide()

    def test_capture_and_receipt_edits_are_refused(self) -> None:
        closure = self.capture()
        decision = self.decide(closure)
        modified = copy.deepcopy(decision)
        modified["captured_warm_sources"][0]["sha256"] = "f" * 64
        modified["strict_sha256"] = satisfaction._canonical_digest({key:value for key,value in modified.items() if key != "strict_sha256"})
        self.assertFalse(satisfaction.profile_satisfaction_identity_matches(modified, closure))
        self.write(self.clone / "common/helper.ml", "changed after capture")
        with self.assertRaisesRegex(satisfaction.ProfileSatisfactionError, "changed before warm"):
            self.decide(closure)
        with self.assertRaisesRegex(satisfaction.ProfileSatisfactionError, "changed before evaluation"):
            satisfaction.revalidate_live_edge_files(decision)


if __name__ == "__main__":
    unittest.main()
