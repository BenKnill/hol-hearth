#!/usr/bin/env python3
"""Cold-free project-basis identity and fail-closed admission checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))

from hol_workbench import project_basis as basis
from hol_workbench.hashing import sha256_file
from hol_workbench.source_dependency_closure import build_source_dependency_closure


class BasisContracts(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="hearth-project-basis-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "basis.ml"
        self.source.write_text('needs "helper.ml";;\nlet code = define_assert_from_elf "code" "object.o" [0];;\n')
        self.helper = self.root / "helper.ml"
        self.helper.write_text("let SOME_STABLE_CONSTANT = 1;;\n")
        self.object = self.root / "object.o"
        self.object.write_bytes(b"exact object input")
        self.profile = self.root / "profile"
        self.profile.mkdir()
        (self.profile / "snapshot-manifest.json").write_text('{"profile_basis_id":"test"}\n')
        self.raw = self.root / "transcript.raw"
        self.raw.write_text("completed proof source\n")
        self.receipt = self.root / "receipt.json"
        self.closure = build_source_dependency_closure(self.source, project_root=self.root)
        self.plan = self.make_plan()
        self.package = self.plan.record_path.parent / "generations" / "test-generation" / "source-package"
        self.package.mkdir(parents=True)
        (self.package / "basis.ml").write_bytes(self.source.read_bytes() + b"(* instrumented *)\n")
        (self.package / "helper.ml").write_bytes(self.helper.read_bytes())
        (self.package / "object.o").write_bytes(self.object.read_bytes())

    def make_plan(self) -> basis.BasisPlan:
        return basis.plan_basis(self.source, profile_root=self.profile, logical_profile="s2n-arm",
                                run_root=self.root / "runs", closure=self.closure)

    def good_receipt(self) -> dict:
        result = {
            "schema": "hol-workbench.warm-vanilla-artifact.v1", "evidence": "recorded_warm_replay",
            "source": str(self.source), "source_sha256": sha256_file(self.source),
            "source_dependency_closure_sha256": self.closure["strict_sha256"],
            "logical_profile": "s2n-arm", "source_completed": True, "claims_complete": True,
            "exit_status": 0, "transport_status": "completed", "included_file_error_observed": False,
            "foundation_delta": {"status": "observed", "new_axiom_count": 0},
            "raw_transcript": str(self.raw), "raw_transcript_sha256": sha256_file(self.raw),
            "completion_marker_valid": True, "semantic_exit_status": 0, "worker_exit_status": 0,
            "preparation_package_root": str(self.package),
            "literal_elf_transport": self.plan.identity["elf_transport"],
            "executed_source_sha256": sha256_file(self.package / "basis.ml"),
            "dependency_package_files": [
                {"package_path": "basis.ml", "role": "entrypoint", "sha256": sha256_file(self.source)},
                {"package_path": "helper.ml", "role": "dependency", "sha256": sha256_file(self.helper),
                 "size_bytes": self.helper.stat().st_size},
                {"package_path": "object.o", "role": "literal_elf_artifact", "sha256": sha256_file(self.object),
                 "size_bytes": self.object.stat().st_size},
            ],
        }
        result["transcript_accounting"] = {key: result[key] for key in (
            "source_completed", "claims_complete", "included_file_error_observed", "completion_marker_valid",
            "semantic_exit_status", "foundation_delta",
        )}
        return result

    def validate(self, receipt: dict) -> dict:
        self.receipt.write_text(json.dumps(receipt))
        return basis.validate_preparation_receipt(self.plan, self.receipt)

    def test_receipt_accepts_only_complete_zero_axiom_preparation(self) -> None:
        self.assertEqual(self.validate(self.good_receipt())["exit_status"], 0)
        for field, value in (("source_completed", False), ("claims_complete", False),
                             ("included_file_error_observed", True), ("exit_status", 1),
                             ("transport_status", "timeout"), ("logical_profile", "light"),
                             ("source_dependency_closure_sha256", "b" * 64),
                             ("literal_elf_transport", {"mode": "mapped_loaders",
                                                        "cwd_from_package_root": None,
                                                        "wrapped_loaders": ["define_assert_from_elf"]}),
                             ("source_completed", 1), ("exit_status", False)):
            with self.subTest(field=field):
                row = self.good_receipt()
                row[field] = value
                with self.assertRaises(ValueError):
                    self.validate(row)
        for foundation in ({"status": "missing", "new_axiom_count": 0},
                           {"status": "observed", "new_axiom_count": 1}, {}):
            row = self.good_receipt()
            row["foundation_delta"] = foundation
            with self.assertRaises(ValueError):
                self.validate(row)

    def test_receipt_transcript_must_keep_exact_identity(self) -> None:
        row = self.good_receipt()
        self.raw.write_text("changed transcript\n")
        with self.assertRaisesRegex(ValueError, "transcript identity"):
            self.validate(row)

    def test_retained_package_changes_and_symlinks_are_refused(self) -> None:
        row = self.good_receipt()
        helper = self.package / "helper.ml"
        helper.write_text("different retained source\n")
        with self.assertRaisesRegex(ValueError, "retained preparation bytes"):
            self.validate(row)
        helper.unlink()
        helper.symlink_to(self.helper)
        with self.assertRaises(OSError):
            self.validate(row)
        helper.unlink()
        helper.write_bytes(self.helper.read_bytes())
        entrypoint = self.package / "basis.ml"
        entrypoint.write_bytes(self.source.read_bytes())
        with self.assertRaisesRegex(ValueError, "retained preparation bytes"):
            self.validate(row)

    def test_retained_object_is_checked_after_preparation(self) -> None:
        row = self.good_receipt()
        packaged_object = self.package / "object.o"
        original = packaged_object.read_bytes()
        packaged_object.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        with self.assertRaisesRegex(ValueError, "retained preparation bytes"):
            self.validate(row)
        packaged_object.write_bytes(original)
        self.assertEqual(self.validate(row)["exit_status"], 0)

    def test_source_dependency_and_object_edits_invalidate_existing_plan(self) -> None:
        for path in (self.source, self.helper, self.object):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                with self.assertRaisesRegex(ValueError, "input changed"):
                    basis.lookup_basis(self.plan)
                path.write_bytes(original)
        self.assertIsNone(basis.lookup_basis(self.plan))

    def test_recaptured_source_dependency_and_object_change_content_address(self) -> None:
        for path in (self.source, self.helper, self.object):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                self.closure = build_source_dependency_closure(self.source, project_root=self.root)
                self.assertNotEqual(self.make_plan().key, self.plan.key)
                path.write_bytes(original)
                self.closure = build_source_dependency_closure(self.source, project_root=self.root)
                self.assertEqual(self.make_plan().key, self.plan.key)

    def test_manifest_and_backend_change_invalidate_existing_plan(self) -> None:
        manifest = self.profile / "snapshot-manifest.json"
        original = manifest.read_bytes()
        manifest.write_text("changed shelf")
        with self.assertRaisesRegex(ValueError, "shelf manifest"):
            basis.lookup_basis(self.plan)
        manifest.write_bytes(original)
        with patch.object(basis, "backend_sha256", return_value="b" * 64):
            with self.assertRaisesRegex(ValueError, "backend changed"):
                basis.lookup_basis(self.plan)

    def test_bootstrap_uses_distinct_private_owned_generations(self) -> None:
        first = basis.bootstrap_postlude(self.plan)
        generation = self.plan.generation
        self.assertIsNotNone(generation)
        spec = json.loads((generation / "spec.json").read_text())
        self.assertEqual(spec["identity"], self.plan.identity)
        self.assertEqual(spec["nonce"], self.plan.nonce)
        self.assertEqual((generation.stat().st_mode & 0o777), 0o700)
        self.assertIn(b"Toploop.parse_toplevel_phrase", first)
        self.assertIn(b"hearth_basis_original_cwd", first)
        self.assertIn(hashlib.md5(self.source.read_bytes(), usedforsecurity=False).hexdigest().encode(), first)
        second = basis.bootstrap_postlude(self.plan)
        self.assertNotEqual(generation, self.plan.generation)
        self.assertNotEqual(first, second)

    def test_recycled_pid_cannot_be_adopted(self) -> None:
        basis.bootstrap_postlude(self.plan)
        (self.plan.generation / "child.ready").write_text(f"123\n456\n{self.plan.nonce}\n")
        with patch.object(basis, "_process_identity", return_value={"pid": 123, "start_ticks": 789}):
            with self.assertRaisesRegex(ValueError, "no longer alive"):
                basis._pending_identity(self.plan)

    def test_source_capture_mismatch_and_dynamic_dependencies_are_refused(self) -> None:
        self.closure["entrypoint"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "differs"):
            self.make_plan()
        self.closure["entrypoint"]["sha256"] = sha256_file(self.source)
        self.source.write_text('let file = "helper.ml";; needs file;;\n')
        self.closure = build_source_dependency_closure(self.source, project_root=self.root)
        with self.assertRaisesRegex(ValueError, "literal dependencies"):
            self.make_plan()

    def test_leaf_captured_bytes_must_match_basis_even_if_disk_is_restored(self) -> None:
        leaf = self.root / "leaf.ml"
        leaf.write_text('needs "basis.ml";;\n')
        closure = build_source_dependency_closure(leaf, project_root=self.root)
        row = {"schema": basis.SCHEMA, "identity": self.plan.identity, "broker": {}, "basis": {}}
        handle = basis.BasisHandle(self.root / "session", row, self.plan.record_path)
        with patch.object(basis, "assert_live_basis"):
            expected = basis.validate_basis_use(handle, closure)
        self.assertEqual(expected["profile_basis_id"], "hearth.project." + self.plan.key)
        for path in (self.helper, self.object):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                changed_closure = build_source_dependency_closure(leaf, project_root=self.root)
                path.write_bytes(original)
                with self.assertRaisesRegex(ValueError, "packaged leaf dependency"):
                    basis.validate_basis_use(handle, changed_closure)
        leaf.write_text('loadt "basis.ml";;\n')
        with self.assertRaisesRegex(ValueError, "literal needs"):
            basis.validate_basis_use(handle, build_source_dependency_closure(leaf, project_root=self.root))

    def test_live_endpoint_must_echo_exact_owned_generation_and_empty_children(self) -> None:
        row = {"schema": basis.SCHEMA, "identity": self.plan.identity,
               "broker": {"pid": 123}, "basis": {"pid": 456}}
        handle = basis.BasisHandle(self.root / "session", row, self.plan.record_path)
        with patch.object(basis, "_live", return_value=True), patch.object(basis, "admit_single_threaded_basis"):
            for response in ({"status": "ready"}, {"status": "ready", "active_children": None},
                             {"status": "error", "active_children": []}):
                with patch.object(basis, "broker_control_request", return_value=response):
                    with self.assertRaises(RuntimeError):
                        basis.assert_live_basis(handle)
            with patch.object(basis, "broker_control_request", return_value={"status": "ready", "active_children": []}) as request:
                basis.assert_live_basis(handle)
                self.assertEqual(request.call_args.kwargs["expected"], basis.expected_basis(handle))

    def test_invalid_cached_receipt_retires_old_generation_before_replacement(self) -> None:
        self.plan.record_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"schema": basis.SCHEMA, "status": "ready", "identity": self.plan.identity,
               "session": str(self.plan.record_path.parent / "session"), "broker": {}, "basis": {},
               "preparation_receipt": str(self.receipt), "preparation_receipt_sha256": "0" * 64}
        self.plan.record_path.write_text(json.dumps(row))
        with patch.object(basis, "_live", return_value=True), patch.object(basis, "assert_live_basis"), \
                patch.object(basis, "retire_basis") as retire:
            self.assertIsNone(basis.lookup_basis(self.plan))
        self.assertEqual(retire.call_args.args[0].record_path, self.plan.record_path)
        self.assertEqual(retire.call_args.kwargs["reason"], "cached_generation_failed_revalidation")

    def test_busy_owned_generation_is_not_retired_or_overwritten(self) -> None:
        self.plan.record_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"schema": basis.SCHEMA, "status": "ready", "identity": self.plan.identity,
               "session": str(self.plan.record_path.parent / "session"), "broker": {}, "basis": {}}
        self.plan.record_path.write_text(json.dumps(row))
        with patch.object(basis, "_live", return_value=True), \
                patch.object(basis, "assert_live_basis", side_effect=basis.ProjectBasisBusy("in use")), \
                patch.object(basis, "retire_basis") as retire:
            with self.assertRaises(basis.ProjectBasisBusy):
                basis.lookup_basis(self.plan)
            with self.assertRaises(basis.ProjectBasisBusy):
                basis.bootstrap_postlude(self.plan)
        retire.assert_not_called()
        self.assertIsNone(self.plan.generation)
        self.assertEqual(json.loads(self.plan.record_path.read_text()), row)


if __name__ == "__main__":
    unittest.main()
