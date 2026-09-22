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
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))
from hol_workbench.cli.leaf_needs import LeafNeedsError, build_report
from hol_workbench.hashing import sha256_bytes

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


if __name__ == "__main__":
    unittest.main()
