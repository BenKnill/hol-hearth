#!/usr/bin/env python3
"""export-replay writes a plain HOL Light script from a receipt; no HOL or CRIU."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))
from hol_workbench.cli.export_replay import ExportError, build_script
from hol_workbench.cli.inspect import REPLAY_SCHEMA


class ExportReplay(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project with space"
        (self.project / "proofs").mkdir(parents=True)
        self.source = self.project / "proofs" / "leaf.ml"
        self.source.write_text('needs "arm/proofs/base.ml";;\nneeds "deps.ml";;\nneeds "../shared/util.ml";;\n')
        self.receipt_path = self.root / "runs" / "attempt" / "transcript.log.json"
        self.receipt_path.parent.mkdir(parents=True)
        self.receipt = {
            "schema": REPLAY_SCHEMA, "evidence": "recorded_warm_replay",
            "source": str(self.source), "source_sha256": "ab" * 32, "source_dependency_closure_sha256": "cd" * 32,
            "logical_profile": "s2n-arm", "profile_basis_id": "hol.s2n.arm.base.v2",
            "profile_cwd": "/shelf/s2n-bignum", "profile_base": "/cache/hol.s2n.arm.base.v2-765ddeba16e6.ml",
            "profile_satisfaction": {"host_holdir": "/shelf/hol-light"},
            "profile_satisfied_dependencies": [{"declared_path": "arm/proofs/base.ml", "declaring_file": "<entrypoint>"}],
            "bindings": [{"name": "LEAF_OK", "status": "proved"}, {"name": "LEAF_BAD", "status": "missing"}],
            "source_dependency_closure": {"records": [
                {"declared_path": "arm/proofs/base.ml", "declaring_file": "<entrypoint>", "resolution": "unresolved"},
                {"declared_path": "deps.ml", "declaring_file": "<entrypoint>", "resolution": "source_local",
                 "resolved_path": str(self.project / "proofs" / "deps.ml")},
                {"declared_path": "../shared/util.ml", "declaring_file": "<entrypoint>", "resolution": "source_local",
                 "resolved_path": str(self.project / "shared" / "util.ml")},
            ]},
        }
        self.receipt_path.write_text(json.dumps(self.receipt))

    def test_script_reproduces_cwd_load_path_profile_and_source(self):
        script = build_script(self.receipt_path, self.receipt)
        self.assertTrue(script.startswith("#!/usr/bin/env bash\n"))
        self.assertIn("HOLDIR=/shelf/hol-light\n", script)
        self.assertIn("cd /shelf/s2n-bignum\n", script)
        self.assertIn(f"export HOLLIGHT_LOAD_PATH='{self.project / 'proofs'}'\n", script)
        self.assertIn('loadt "/cache/hol.s2n.arm.base.v2-765ddeba16e6.ml";;', script)
        self.assertIn(f'loadt "{self.source}";;', script)
        self.assertIn("\nLEAF_OK;;\n", script)
        self.assertNotIn("LEAF_BAD;;", script)
        self.assertIn("NOTE: ../shared/util.ml resolved to", script)
        self.assertNotIn("arm/proofs/base.ml resolved", script)
        self.assertIn("axioms before=%d after=%d", script)
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)

    def test_missing_holdir_refuses_without_runtime_config(self):
        del self.receipt["profile_satisfaction"]
        import hol_workbench.cli.export_replay as module
        from hol_workbench.runtime_config import RuntimeConfigError
        from unittest.mock import patch
        with patch.object(module, "load_runtime_config", side_effect=RuntimeConfigError("absent")):
            with self.assertRaises(ExportError):
                build_script(self.receipt_path, self.receipt)
        self.assertIn("HOLDIR=/elsewhere/hol\n", build_script(self.receipt_path, self.receipt, holdir_override=Path("/elsewhere/hol")))

    def test_command_writes_executable_once(self):
        out = self.root / "replay.sh"
        command = [str(ROOT / "hearth"), "export-replay", str(self.receipt_path.parent.parent), "--out", str(out)]
        result = subprocess.run(command, cwd="/tmp", capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("EXPORTED:", result.stdout)
        self.assertTrue(out.stat().st_mode & 0o111)
        self.assertEqual(out.read_text(), build_script(self.receipt_path.resolve(), self.receipt))
        again = subprocess.run(command, cwd="/tmp", capture_output=True, text=True, timeout=20)
        self.assertEqual(again.returncode, 2)
        self.assertIn("already exists", again.stderr)
        printed = subprocess.run(command[:3], cwd="/tmp", capture_output=True, text=True, timeout=20)
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertEqual(printed.stdout, out.read_text())

    def test_non_recorded_receipt_is_refused(self):
        self.receipt["evidence"] = "warm_development_only"
        with self.assertRaises(ExportError):
            build_script(self.receipt_path, self.receipt)


if __name__ == "__main__":
    unittest.main()
