#!/usr/bin/env python3
"""Project-watch and receipt-inspection regressions; no HOL or CRIU."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))
from hol_workbench.cli.prove_loop import project_revision
from hol_workbench.public_surface_contract import _receipt


class AuthoringRegression(unittest.TestCase):
    def inspect(self, path, *args):
        return subprocess.run([str(ROOT / "hearth"), "inspect", str(path), *args],
                              cwd="/tmp", capture_output=True, text=True, timeout=10)

    def test_transitive_bytes_and_missing_dependency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, helper, leaf = (root / n for n in ("main.ml", "helper.ml", "leaf.ml"))
            source.write_text('needs "helper.ml";;\n')
            helper.write_text('needs "leaf.ml";;\n')
            leaf.write_text("let n = 1;;\n")
            profile = SimpleNamespace(cwd=root, legacy_holdir_roots=(), logical_source_roots=())
            with patch("hol_workbench.source_execution_plan.machine_holdir_authority", return_value=None):
                original = project_revision(source, profile)
                stamp = leaf.stat()
                leaf.write_text("let n = 2;;\n")
                os.utime(leaf, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                edited = project_revision(source, profile)
                self.assertNotEqual(original, edited, "same-size, same-mtime edits must invalidate")
                leaf.unlink()
                self.assertNotEqual(edited, project_revision(source, profile))
                leaf.write_text("let n = 1;;\n")
                self.assertEqual(original, project_revision(source, profile))

    def test_target_after_default_limit_json_and_whole_source_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "watch-session" / "attempt"
            run.mkdir(parents=True)
            receipt = run / "transcript.log.json"
            bindings = [{"name": f"TARGET_{i}", "status": "proved"} for i in range(16)]
            _receipt(receipt, succeeded=True, recorded_exit_status=0, bindings=bindings)
            result = self.inspect(root, "--binding", "TARGET_15")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("TARGET_15: proved", result.stdout)
            self.assertNotIn("TARGET_0: proved", result.stdout)
            result = self.inspect(root, "--verbose")
            self.assertIn("TARGET_15: proved", result.stdout)
            self.assertEqual(json.loads(self.inspect(root, "--json").stdout),
                             json.loads(receipt.read_text()))
            self.assertEqual(self.inspect(root, "--binding", "ABSENT").returncode, 1)
            _receipt(receipt, succeeded=False, recorded_exit_status=1, bindings=bindings)
            self.assertEqual(self.inspect(root, "--binding", "TARGET_15").returncode, 1)

    def test_multiline_failure_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "transcript.log.json"
            _receipt(receipt, succeeded=False, first_failure='Exception:')
            raw = receipt.with_suffix(".raw")
            raw.write_text('startup\nException:\nFailure "unbound project helper".\n'
                           'val OTHER = hidden\n' + "noise\n" * 200)
            result = self.inspect(receipt)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Failure "unbound project helper"', result.stdout)
            self.assertNotIn("val OTHER = hidden", result.stdout)
            self.assertNotIn("noise", result.stdout)


if __name__ == "__main__":
    unittest.main()
