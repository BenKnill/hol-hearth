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
from hol_workbench.cli.orbstack_criu_vanilla_semantics import analyze_vanilla_transcript
from hol_workbench.proofs.theorem_scan import extract_hol_theorems_bytes
from hol_workbench.vanilla_claims import (
    build_claim_probe, first_error, account_claims, target_pack_status, diagnostic_claim_output,
)


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


    def test_printed_output_is_not_a_probe_or_source_acceptance(self):
        source = b"let BEFORE = prove (`T`, REWRITE_TAC[]);;\nlet FAILING = prove (`F`, ALL_TAC);;\n"
        claims = extract_hol_theorems_bytes(Path("/work/proof.ml"), source)
        _, contract = build_claim_probe(source, claims, nonce="1" * 32)
        transcript = b'val BEFORE : thm =\n  |- T\nException:\nFailure\n "TAC_PROOF: Unsolved goals".\n'
        result = analyze_vanilla_transcript(claims=claims, transcript=transcript, contract=contract,
                                          transport="completed", response={"exit_status": 0})
        self.assertEqual(result["source_status"], "failed")
        self.assertFalse(result["claims_complete"])
        self.assertEqual(result["observed_bindings"], [])
        self.assertEqual(result["binding_counts"],
                         {"proved": 0, "failed": 0, "printed_unprobed": 1, "missing": 1, "unknown": 0})
        before, failing = result["bindings"]
        self.assertEqual(before["status"], "printed_unprobed")
        self.assertEqual(before["printed_output"][0]["printed_conclusion"], "|- T")
        self.assertFalse(before["printed_output"][0]["verified"])
        self.assertEqual(failing["status"], "missing")
        self.assertEqual(result["failing_binding"]["status"], "unknown")
        self.assertEqual(result["first_failure"], 'Exception: Failure "TAC_PROOF: Unsolved goals".')
        self.assertEqual(result["first_failure_transcript_line"], 3)
        self.assertEqual(target_pack_status(result["claim_accounting"], child_status=0), "partial")
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "transcript.log.json"
            _receipt(receipt, succeeded=False, bindings=result["bindings"],
                     first_failure=result["first_failure"],
                     first_failure_transcript_line=result["first_failure_transcript_line"])
            receipt.with_suffix(".raw").write_bytes(transcript)
            doc = json.loads(receipt.read_text())
            doc["binding_counts"] = result["binding_counts"]
            doc["failing_binding"] = result["failing_binding"]
            receipt.write_text(json.dumps(doc))
            rendered = self.inspect(receipt, "--binding", "BEFORE")
            self.assertEqual(rendered.returncode, 1)
            self.assertIn("BEFORE: printed_unprobed", rendered.stdout)
            self.assertIn("printed_conclusion (unverified): |- T", rendered.stdout)
            self.assertIn("failing_binding: unknown", rendered.stdout)
            self.assertIn('first_failure: transcript_line=3 Exception: Failure "TAC_PROOF: Unsolved goals".',
                          rendered.stdout)
            self.assertEqual(json.loads(self.inspect(receipt, "--json").stdout)["binding_counts"]["proved"], 0)

    def test_natural_or_forged_text_cannot_override_kernel_probe(self):
        source = b"let BEFORE = prove (`T`, REWRITE_TAC[]);;\n"
        claims = extract_hol_theorems_bytes(Path("/work/proof.ml"), source)
        _, contract = build_claim_probe(source, claims, nonce="2" * 32)
        natural = "val BEFORE : thm = |- F\nException: misleading printed text\n"
        probe = contract["claims"][0]["ok_marker"]
        done = contract["completion_marker"]
        complete = (natural + probe + "\n" + done + "\n").encode()
        result = analyze_vanilla_transcript(claims=claims, transcript=complete, contract=contract,
                                          transport="completed", response={"exit_status": 0})
        self.assertEqual(result["source_status"], "succeeded")
        self.assertEqual(result["bindings"][0]["status"], "proved")
        self.assertIsNone(result["first_failure"])
        duplicate = (natural + probe + "\n" + probe + "\n" + done + "\n").encode()
        docs, _ = account_claims(claims, duplicate, contract)
        self.assertEqual(docs[0]["status"], "failed")
        self.assertEqual(docs[0]["evidence"], "nonce_probe_marker_ambiguous")

    def test_printed_conclusion_is_bounded_plain_text_with_raw_copy(self):
        line = "val BEFORE : thm = |- \\x1b[35mT\\x1b[0m".replace("\\x1b", "\x1b")
        result = diagnostic_claim_output([line] * 10, list(range(1, 11)))
        self.assertEqual(len(result), 3)
        self.assertEqual(result[-1]["printed_conclusion"], "|- T")
        self.assertIn("\x1b[35m", result[-1]["text"])
        result = diagnostic_claim_output(["val HUGE : thm = |- " + "x" * 10000], [1])
        self.assertEqual(len(result[0]["text"]), 4096)
        self.assertTrue(result[0]["truncated"])

    def test_bounded_failure_block_stops_at_next_binding_or_marker(self):
        self.assertEqual(first_error('noise\nException:\n  Failure "a\\nlong error".\n'
                                     'val NEXT : thm = |- T\nmore\n'),
                         (2, 'Exception: Failure "a\\nlong error".'))
        self.assertEqual(first_error('Exception: first\n__HOL_CLAIM_PROBE_DONE__:nonce\n'),
                         (1, "Exception: first"))
        self.assertLessEqual(len(first_error("Exception:\n  " + "x" * 10000)[1]), 2400)

    def test_inspect_counts_all_and_shows_nonproved_before_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "transcript.log.json"
            bindings = [{"name": f"GOOD_{i}", "status": "proved"} for i in range(73)]
            bindings.extend([{"name": "LATE_FAILED", "status": "failed"},
                             {"name": "LATE_PRINTED", "status": "printed_unprobed"},
                             {"name": "LATE_MISSING", "status": "missing"}])
            _receipt(receipt, succeeded=False, bindings=bindings)
            result = self.inspect(receipt)
            self.assertIn("binding_counts: proved=73 failed=1 printed_unprobed=1 missing=1 unknown=0",
                          result.stdout)
            self.assertIn("LATE_FAILED: failed", result.stdout)
            self.assertIn("LATE_PRINTED: printed_unprobed", result.stdout)
            self.assertIn("LATE_MISSING: missing", result.stdout)
            self.assertLess(result.stdout.index("LATE_MISSING:"), result.stdout.index("GOOD_0:"))
            self.assertIn("64 more (64 proved)", result.stdout)
            _receipt(receipt, succeeded=True, bindings=bindings[:73])
            passed = self.inspect(receipt)
            self.assertIn("binding_counts: proved=73 failed=0 printed_unprobed=0 missing=0 unknown=0",
                          passed.stdout)
            self.assertIn("61 more (61 proved)", passed.stdout)


if __name__ == "__main__":
    unittest.main()
