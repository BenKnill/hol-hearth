#!/usr/bin/env python3
"""The curated receipt summary: verdicts, binding states, next commands; no HOL or CRIU."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))
from hol_workbench.cli.inspect import REPLAY_SCHEMA
from hol_workbench.receipt_summary import SUMMARY_SCHEMA, next_command, render_card, summarize


def receipt(**overrides):
    base = {
        "schema": REPLAY_SCHEMA, "evidence": "recorded_warm_replay", "source": "/work/leaf.ml",
        "source_sha256": "ab" * 32, "logical_profile": "light", "transport_status": "completed",
        "semantic_source_status": "succeeded", "source_completed": True, "completion_marker_valid": True,
        "claims_complete": True, "semantic_exit_status": 0, "exit_status": 0, "worker_exit_status": 0,
        "process_exit_status": 0, "eval_elapsed_seconds": 0.31, "requested_timeout_seconds": 120.0,
        "bindings": [{"name": "ONE", "status": "proved", "verification_kind": "kernel_conclusion_and_empty_hypotheses"},
                     {"name": "TWO", "status": "proved"}],
        "transcript_accounting": {"claim_accounting": [
            {"theorem": "ONE", "source_span": [1, 2]}, {"theorem": "TWO", "source_span": [4, 5]},
            {"theorem": "THREE", "source_span": [7, 8]}]},
        "foundation_delta": {"status": "observed", "deltas": {"axioms": 0, "definitions": 1, "types": 0, "constants": 1}},
        "source_dependency_closure": {"entrypoint": {"path": "/work/leaf.ml"},
                                      "records": [{"resolved_path": "/work/deps.ml"}]},
        "source_dependency_closure_sha256": "cd" * 32,
    }
    base.update(overrides)
    return base


def command(*args):
    return " ".join(["hearth", *map(str, args)])


class Verdicts(unittest.TestCase):
    def test_passed(self):
        summary = summarize(receipt(), receipt_path=Path("/runs/attempt/transcript.log.json"))
        self.assertEqual(summary.verdict, "passed")
        self.assertEqual(summary.line, "PASSED leaf.ml: 2/2 bindings proved, 0 new axioms, eval 0.3s (light)")
        self.assertEqual(summary.inputs, 2)
        self.assertEqual(next_command(summary, run_root=Path("/runs"), public_command=command),
                         "hearth inspect /runs/attempt")
        payload = summary.to_json()
        self.assertEqual(payload["schema"], SUMMARY_SCHEMA)
        self.assertEqual(payload["binding_counts"]["proved"], 2)
        self.assertLessEqual(len(payload), 26)

    def test_new_axiom_is_loud_but_still_passed(self):
        summary = summarize(receipt(foundation_delta={"status": "observed", "deltas": {"axioms": 1}}))
        self.assertEqual(summary.verdict, "passed")
        self.assertIn("1 NEW AXIOM", summary.line)

    def test_failed_with_attribution_marks_later_bindings_not_reached(self):
        summary = summarize(receipt(
            semantic_source_status="failed", source_completed=False, completion_marker_valid=False,
            claims_complete=False, semantic_exit_status=1, exit_status=1,
            first_failure='Exception: Failure "REAL_ARITH `x`: linear_ineqs: no contradiction".',
            first_failure_transcript_line=40,
            failing_binding={"status": "identified", "name": "TWO", "source": "/work/leaf.ml", "source_line": 4},
            bindings=[{"name": "ONE", "status": "proved"}, {"name": "TWO", "status": "missing"},
                      {"name": "THREE", "status": "missing"}],
            proof_diagnostics={"status": "recorded", "events": [{
                "following_exception_transcript_line": 40, "steps": [
                    {"source_line": 5, "conclusion": "x + y = y + x", "assumption_count": 0, "exception": "Failure(\"x\")"}],
            }]},
        ), receipt_path=Path("/runs/attempt/transcript.log.json"))
        self.assertEqual(summary.verdict, "failed")
        self.assertTrue(summary.line.startswith("FAILED leaf.ml at TWO (line 4): Exception: Failure"), summary.line)
        self.assertEqual([(b.name, b.status) for b in summary.bindings],
                         [("ONE", "proved"), ("TWO", "failed"), ("THREE", "not_reached")])
        self.assertEqual(summary.failing_step["conclusion"], "x + y = y + x")
        self.assertEqual(next_command(summary, run_root=Path("/runs"), public_command=command),
                         "hearth reopen /runs/attempt --binding TWO")
        card = render_card(summary, receipt())
        self.assertIn("  THREE: not reached (after the failure) source_line=7", card)
        self.assertIn("failing_binding: TWO source=/work/leaf.ml:4", card)

    def test_failed_without_attribution_keeps_missing(self):
        summary = summarize(receipt(
            semantic_source_status="failed", source_completed=False, completion_marker_valid=False,
            claims_complete=False, semantic_exit_status=1, exit_status=1, first_failure="Exception: boom",
            bindings=[{"name": "ONE", "status": "missing"}, {"name": "TWO", "status": "missing"}],
        ))
        self.assertEqual(summary.line, "FAILED leaf.ml: Exception: boom")
        self.assertEqual({b.status for b in summary.bindings}, {"missing"})

    def test_incomplete_and_refused(self):
        timed_out = summarize(receipt(transport_status="timeout", semantic_source_status="not_completed",
                                      source_completed=False, eval_elapsed_seconds=120.0),
                              receipt_path=Path("/runs/attempt/transcript.log.json"))
        self.assertEqual(timed_out.verdict, "incomplete")
        self.assertEqual(timed_out.line, "INCOMPLETE leaf.ml: timeout after 120s; not a disproof (light)")
        self.assertEqual(next_command(timed_out, run_root=Path("/runs"), public_command=command),
                         "hearth prove /work/leaf.ml --profile light --timeout 240 --run-root /runs")
        refused = summarize(receipt(
            evidence="pre_eval_dependency_transport_refusal", semantic_source_status="not_started",
            source_completed=False, transport_status="not_started",
            source_preflight_status="source_changed_during_capture",
            source_pin={"pinned_sha256": "a" * 64, "read_sha256": "b" * 64},
            first_failure="source_changed_during_capture: prove: source_pin=refused", bindings=[],
        ))
        self.assertEqual(refused.verdict, "refused")
        self.assertEqual(refused.line, "REFUSED leaf.ml: source changed during capture (pinned sha=aaaaaaaaaaaa, "
                                       "read sha=bbbbbbbbbbbb); no HOL ran")
        self.assertEqual(next_command(refused, run_root=None, public_command=command),
                         "rerun the same prove command once the file is stable")

    def test_worker_exit_after_completion_is_failed(self):
        summary = summarize(receipt(exit_status=42, worker_exit_status=42, process_exit_status=42))
        self.assertEqual(summary.verdict, "failed")
        self.assertIn("worker exited with status 42", summary.line)


class Commands(unittest.TestCase):
    def test_inspect_card_and_json_and_verbose(self):
        with tempfile.TemporaryDirectory() as temporary:
            attempt = Path(temporary) / "runs" / "attempt"
            attempt.mkdir(parents=True)
            path = attempt / "transcript.log.json"
            path.write_text(json.dumps(receipt()))
            hearth = str(ROOT / "hearth")
            card = subprocess.run([hearth, "inspect", str(attempt.parent)], capture_output=True, text=True, timeout=20)
            self.assertEqual(card.returncode, 0, card.stderr)
            lines = card.stdout.splitlines()
            self.assertEqual(lines[0], "PASSED leaf.ml: 2/2 bindings proved, 0 new axioms, eval 0.3s (light)")
            self.assertEqual(lines[1], "source: /work/leaf.ml sha=abababababab")
            self.assertLess(len(lines), 12, card.stdout)
            self.assertTrue(lines[-1].startswith("NEXT: "))
            summary = subprocess.run([hearth, "inspect", str(attempt.parent), "--json"], capture_output=True,
                                     text=True, timeout=20)
            payload = json.loads(summary.stdout)
            self.assertEqual(payload["verdict"], "passed")
            self.assertEqual(payload["receipt"], str(path))
            full = subprocess.run([hearth, "inspect", str(attempt.parent), "--json", "--verbose"], capture_output=True,
                                  text=True, timeout=20)
            self.assertEqual(json.loads(full.stdout)["schema"], REPLAY_SCHEMA)
            legacy = subprocess.run([hearth, "inspect", str(attempt.parent), "--verbose"], capture_output=True,
                                    text=True, timeout=20)
            self.assertIn("status: succeeded", legacy.stdout)


if __name__ == "__main__":
    unittest.main()
