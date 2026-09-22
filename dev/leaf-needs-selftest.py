#!/usr/bin/env python3
"""Leaf-versus-recipe needs report regressions; original fixtures, no HOL or CRIU."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))
from hol_workbench.cli.leaf_needs import LeafNeedsError, build_report
from hol_workbench.hashing import sha256_bytes
from hol_workbench.profile_satisfied_dependencies import ProfileSatisfactionError
from hol_workbench.source_dependency_closure import SourceDependencyInferenceError

LEAF = (
    '(* original fixture leaf *)\n'
    'needs "arm/proofs/base.ml";;\n'
    'needs "project/helper.ml";;\n'
    'loadt "arm/proofs/base.ml";;\n'
    'let code = define_assert_from_elf "leaf_mc" "project/leaf.o"\n[0xd65f03c0];;\n'
    'let name = "project/other.ml";;\n'
    'needs name;;\n'
    'let LEAF_OK = prove(`T`, REWRITE_TAC[]);;\n'
)


def paths(rows):
    return [(row["loader"], row["path"]) for row in rows]


class LeafNeeds(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.leaf = self.root / "leaf.ml"
        self.leaf.write_text(LEAF)

    def tearDown(self):
        self.temporary.cleanup()

    def receipt(self, directory, **changes):
        directory.mkdir(parents=True, exist_ok=True)
        record = {
            "schema": "hol-workbench.warm-vanilla-artifact.v1",
            "evidence": "recorded_warm_replay",
            "logical_profile": "s2n-arm",
            "source_sha256": sha256_bytes(self.leaf.read_bytes()),
            "profile_sha256": sha256_bytes((ROOT / "profiles/s2n-arm.ml").read_bytes()),
            "semantic_source_status": "succeeded",
        }
        record.update(changes)
        path = directory / "transcript.log.json"
        path.write_text(json.dumps(record))
        return path

    def test_classifies_leaf_loads_against_recipe_text(self):
        report = build_report(self.leaf, "s2n-arm")
        self.assertEqual(report["evidence_class"], "static_recipe_text_comparison")
        self.assertIn("not a proof", report["boundary"])
        self.assertEqual(paths(report["recipe_literal_loads"]), [("needs", "arm/proofs/base.ml")])
        self.assertEqual(paths(report["needs_named_by_recipe"]), [("needs", "arm/proofs/base.ml")])
        self.assertEqual(paths(report["needs_not_in_recipe"]), [("needs", "project/helper.ml")])
        self.assertEqual(paths(report["non_needs_source_loads"]), [("loadt", "arm/proofs/base.ml")])
        self.assertEqual(paths(report["artifact_loads"]), [("define_assert_from_elf", "project/leaf.o")])
        self.assertEqual([row["loader"] for row in report["dynamic_loads"]], ["needs"])
        self.assertIsNone(report["receipt"])
        other = build_report(self.leaf, "s2n-x86")
        self.assertEqual(paths(other["needs_named_by_recipe"]), [])
        self.assertIn(("needs", "arm/proofs/base.ml"), paths(other["needs_not_in_recipe"]))

    def test_refuses_unknown_profile_drift_and_unscannable_leaf(self):
        with self.assertRaisesRegex(LeafNeedsError, "unknown public profile"):
            build_report(self.leaf, "s2n-arm-private")
        copy = self.root / "repo"
        (copy / "hol-workbench").mkdir(parents=True)
        shutil.copy(ROOT / "hol-workbench/warmup-profiles.json", copy / "hol-workbench")
        shutil.copytree(ROOT / "profiles", copy / "profiles")
        with (copy / "profiles/s2n-arm.ml").open("a") as recipe:
            recipe.write('needs "project/helper.ml";;\n')
        with self.assertRaisesRegex(LeafNeedsError, "differs from warmup-profiles.json"):
            build_report(self.leaf, "s2n-arm", repo_root=copy)
        broken = self.root / "broken.ml"
        broken.write_text('needs "arm/proofs/base.ml";;\n(* unterminated\n')
        with self.assertRaises(LeafNeedsError):
            build_report(broken, "s2n-arm")

    def test_receipt_identity_attaches_as_warm_exploration_only(self):
        run = self.root / "runs" / "attempt"
        self.receipt(run)
        attached = build_report(self.leaf, "s2n-arm", receipt=str(self.root / "runs"))["receipt"]
        self.assertEqual(attached["evidence_class"], "warm_exploration")
        self.assertEqual(attached["recorded_evidence"], "recorded_warm_replay")
        self.assertEqual(attached["source_sha256"], sha256_bytes(self.leaf.read_bytes()))
        for field, value, message in (
            ("logical_profile", "s2n-x86", "does not match selected profile"),
            ("source_sha256", "0" * 64, "source SHA-256"),
            ("profile_sha256", "1" * 64, "profile SHA-256"),
        ):
            with self.subTest(field=field):
                path = self.receipt(self.root / field, **{field: value})
                with self.assertRaisesRegex(LeafNeedsError, message):
                    build_report(self.leaf, "s2n-arm", receipt=str(path))
        with self.assertRaisesRegex(LeafNeedsError, "no hol-workbench.warm-vanilla-artifact.v1 receipt"):
            build_report(self.leaf, "s2n-arm", receipt=str(self.root / "empty"))
        self.leaf.write_text(LEAF + "(* edited *)\n")
        with self.assertRaisesRegex(LeafNeedsError, "source SHA-256"):
            build_report(self.leaf, "s2n-arm", receipt=str(run))

    def test_public_launcher(self):
        environment = {**os.environ, "HOL_WORKBENCH_PYTHON": sys.executable}
        def run(*args):
            return subprocess.run([str(ROOT / "hearth"), "leaf-needs", *args], cwd=self.root,
                                  env=environment, capture_output=True, text=True, timeout=30)
        result = run("leaf.ml", "--profile", "s2n-arm")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("EVIDENCE: static_recipe_text_comparison", result.stdout)
        self.assertIn("pinned recipe root, not resolved here:", result.stdout)
        self.assertNotIn("paths resolve", result.stdout)
        self.assertIn("needs not in recipe: 1", result.stdout)
        self.assertIn("  line 3: needs project/helper.ml", result.stdout)
        self.assertIn("receipt: none", result.stdout)
        self.receipt(self.root / "runs" / "attempt")
        result = run("leaf.ml", "--profile", "s2n-arm", "--receipt", "runs", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["receipt"]["evidence_class"], "warm_exploration")
        result = run("leaf.ml", "--profile", "s2n-x86", "--receipt", "runs")
        self.assertEqual(result.returncode, 2)
        self.assertIn("refused", result.stderr)
        self.assertEqual(result.stdout, "")


class DeepLeafNeeds(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / ".hol-workbench-source-root").touch()
        self.leaf = self.root / "leaf.ml"
        self.leaf.write_text('needs "helper.ml";;\nlet LEAF = prove(`T`, REWRITE_TAC[]);;\n')
        (self.root / "helper.ml").write_text('needs "nested.ml";;\n')
        (self.root / "nested.ml").write_text('let code = define_from_elf "code" "code.o";;\n')
        (self.root / "code.o").write_bytes(b"original object input")

    def tearDown(self):
        self.temporary.cleanup()

    def report(self):
        with mock.patch("hol_workbench.source_execution_plan.machine_holdir_authority", return_value=None), \
             mock.patch("hol_workbench.source_execution_plan.source_analysis_cache_root",
                        side_effect=AssertionError("read-only preflight must not request a cache")), \
             mock.patch("hol_workbench.cli.leaf_needs.resolve_published_warm_profile",
                        side_effect=RuntimeError("no local shelf in this fixture")):
            return build_report(self.leaf, "light", deep=True)

    def test_transitive_identity_changes_and_read_only_capture(self):
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        report = self.report()
        preflight = report["preflight"]
        closure = preflight["closure"]
        self.assertEqual(report["evidence_class"], "static_recipe_text_comparison")
        self.assertEqual(preflight["evidence_class"], "static_dependency_closure")
        self.assertEqual(preflight["status"], "complete")
        self.assertEqual(preflight["transport_status"], "packaged")
        self.assertEqual(preflight["warm_inventory"]["status"], "unavailable")
        self.assertIn("no local shelf", preflight["warm_inventory"]["reason"])
        self.assertEqual(closure["literal_edge_count"], 2)
        self.assertEqual(closure["literal_artifact_count"], 1)
        self.assertEqual(closure["artifacts"][0]["sha256"], sha256_bytes(b"original object input"))
        self.assertNotIn("analysis_cache", closure)
        after = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        original = closure["strict_sha256"]
        (self.root / "nested.ml").write_text((self.root / "nested.ml").read_text() + "(* edit *)\n")
        source_edited = self.report()["preflight"]["closure"]["strict_sha256"]
        self.assertNotEqual(original, source_edited)
        (self.root / "code.o").write_bytes(b"changed object input")
        self.assertNotEqual(source_edited, self.report()["preflight"]["closure"]["strict_sha256"])

    def test_valid_disk_capture_does_not_hide_failed_warm_inventory_validation(self):
        with mock.patch("hol_workbench.cli.leaf_needs.resolve_published_warm_profile",
                        return_value=SimpleNamespace(root=self.root, cwd=self.root)), \
             mock.patch("hol_workbench.cli.leaf_needs.decide_profile_satisfaction",
                        side_effect=ProfileSatisfactionError("refused_profile_satisfaction_dependency_changed",
                                                             "source differs from the shelf inventory")):
            report = build_report(self.leaf, "light", deep=True)["preflight"]
        self.assertEqual(report["disk_closure_status"], "complete")
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["warm_inventory"]["status"], "refused")
        self.assertIn("differs from the shelf", report["transport_reason"])

    def test_transitive_missing_and_dynamic_inputs_are_visible(self):
        (self.root / "code.o").unlink()
        report = self.report()["preflight"]
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["transport_status"], "project_input_missing")
        self.assertEqual(report["closure"]["unresolved_artifact_count"], 1)
        self.assertIn("code.o", report["transport_reason"])
        (self.root / "nested.ml").write_text('let path = "hidden.ml";; needs path;;\n')
        report = self.report()["preflight"]
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["transport_status"], "refused_dynamic")
        self.assertEqual(report["closure"]["dynamic_loaders"][0]["declaring_file"], "nested.ml")
        (self.root / "nested.ml").unlink()
        report = self.report()["preflight"]
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["closure"]["records"][-1]["declared_path"], "nested.ml")
        self.assertEqual(report["closure"]["records"][-1]["resolution"], "unresolved")

    def test_nested_scanner_refusal_and_bounds_are_not_complete(self):
        (self.root / "nested.ml").write_text('(* unterminated\n')
        report = self.report()["preflight"]
        self.assertEqual(report["status"], "incomplete")
        self.assertTrue(report["closure"]["dynamic_loaders"])
        for number in range(35):
            filename = "nested.ml" if number == 0 else f"chain{number}.ml"
            (self.root / filename).write_text(f'needs "chain{number + 1}.ml";;\n')
        with self.assertRaisesRegex(SourceDependencyInferenceError, "depth bound exceeded"):
            self.report()

    def test_cli_prints_full_closure_and_returns_nonzero_for_blocker(self):
        environment = {**os.environ, "HOL_WORKBENCH_PYTHON": sys.executable}
        command = [str(ROOT / "hearth"), "leaf-needs", str(self.leaf), "--profile", "light", "--deep"]
        complete = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(complete.returncode, 0, complete.stderr)
        self.assertIn("DEEP PREFLIGHT: complete", complete.stdout)
        self.assertIn("SOURCE EDGES: 2; ELF INPUTS: 1", complete.stdout)
        self.assertIn("ELF nested.ml:1:", complete.stdout)
        self.assertIn(sha256_bytes(b"original object input"), complete.stdout)
        (self.root / "code.o").unlink()
        blocked = subprocess.run([*command, "--json"], env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertEqual(json.loads(blocked.stdout)["preflight"]["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
