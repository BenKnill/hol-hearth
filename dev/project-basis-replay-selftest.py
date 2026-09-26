#!/usr/bin/env python3
"""Project basis CLI orchestration checks using real source capture, without HOL."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))

from hol_workbench.cli import project_basis_replay as route, prove_loop, prove_replay
from hol_workbench.cli.published_profile import PublishedWarmProfile
from hol_workbench.cli.published_profile_replay import run_published_warm_replay
from hol_workbench.hashing import sha256_file


class BasisReplay(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.basis = self.root / "basis.ml"
        self.helper = self.root / "common/helper.ml"
        self.helper.parent.mkdir()
        self.leaf = self.root / "leaf.ml"
        self.helper.write_text("let helper = 1;;\n")
        self.basis.write_text('needs "common/helper.ml";;\nlet prepared = helper;;\n')
        self.leaf.write_text('needs "basis.ml";;\nlet TARGET = prove (`T`, REWRITE_TAC[]);;\n')
        self.profile_root = self.root / "profile"
        self.profile_root.mkdir()
        (self.profile_root / "snapshot-manifest.json").write_text('{"profile_basis_id":"test"}\n')
        self.profile = PublishedWarmProfile("light", self.profile_root, self.root, 1, (), ())
        self.run_root = self.root / "runs"
        self.transcript = self.run_root / "leaf-attempt" / "transcript.log"
        self.calls = []
        self.cache = {}
        self.preparation_status = 0
        self.preparation_error = None
        self.output, self.errors = io.StringIO(), io.StringIO()
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.errors))
        self.stack.enter_context(patch("hol_workbench.source_execution_plan.machine_holdir_authority", return_value=None))
        # This orchestration fixture has no admitted shelf; inventory checks
        # have real captured-source coverage in clone-satisfaction-selftest.
        self.stack.enter_context(patch("hol_workbench.source_execution_plan.build_profile_satisfaction",
                                       return_value={"edges": [], "captured_warm_sources": []}))
        self.stack.enter_context(patch.object(route, "bootstrap_prelude", return_value=b"saved transport\n"))
        self.bootstrap = self.stack.enter_context(patch.object(route, "bootstrap_postlude", side_effect=self.bootstrap_preparation))
        self.abort = self.stack.enter_context(patch.object(route, "abort_basis"))
        self.lookup = self.stack.enter_context(patch.object(route, "lookup_basis", side_effect=lambda plan: self.cache.get(plan.key)))
        self.adopt = self.stack.enter_context(patch.object(route, "adopt_basis", side_effect=self.adopt_preparation))
        self.stack.enter_context(patch("hol_workbench.cli.orbstack_criu_vanilla.run", side_effect=self.replay))

    def adopt_preparation(self, plan, receipt):
        self.assertTrue(receipt.is_file())
        handle = SimpleNamespace(session=self.root / "basis-session",
                                 record={"preparation_receipt": str(receipt), "identity": plan.identity})
        self.cache[plan.key] = handle
        return handle

    def bootstrap_preparation(self, plan):
        plan.generation = plan.cache_root / plan.key / "generations" / "test-generation"
        return b"basis transport\n"

    def replay(self, *, source, **kwargs):
        self.calls.append((source, kwargs))
        if source == self.basis and self.preparation_error:
            raise self.preparation_error
        transcript = kwargs["transcript_output"]
        receipt = Path(f"{transcript}.json")
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"source": str(source)}))
        return self.preparation_status if source == self.basis else 0

    def run_replay(self, basis=None):
        return run_published_warm_replay(
            self.profile, self.leaf, timeout=765, transcript=self.transcript,
            evidence_role="recorded_warm_replay", expected_source_sha256=sha256_file(self.leaf),
            basis_source=basis or self.basis, run_root=self.run_root, basis_cache_root=self.run_root,
        )

    def test_prepares_once_reuses_then_reprepares_after_transitive_edit(self):
        original = self.leaf.read_bytes()
        self.assertEqual(self.run_replay(), 0)
        self.assertEqual(self.run_replay(), 0)
        self.helper.write_text("let helper = 2;;\n")
        self.assertEqual(self.run_replay(), 0)
        self.assertEqual([source for source, _ in self.calls],
                         [self.basis, self.leaf, self.leaf, self.basis, self.leaf])
        self.assertEqual(self.adopt.call_count, 2)
        self.abort.assert_not_called()
        self.assertEqual(self.leaf.read_bytes(), original)
        for source, options in self.calls:
            self.assertEqual(options["timeout"], 765)
            self.assertEqual(options["expected_source_sha256"], sha256_file(source))
            self.assertEqual(options["evidence_role"], "recorded_warm_replay")
            if source == self.basis:
                self.assertEqual(options["preparation_prefix"], b"saved transport\n")
                self.assertEqual(options["preparation_postlude"], b"basis transport\n")
                generation = options["preparation_package_root"].parent
                self.assertEqual(generation.parent.name, "generations")
                self.assertTrue(generation.is_relative_to(self.run_root / ".project-bases"))
                self.assertNotIn("project_basis_handle", options)
            else:
                self.assertIn("project_basis_handle", options)
                self.assertNotIn("preparation_postlude", options)
                self.assertEqual(options["transcript_output"], self.transcript)
        self.assertIn("PROJECT BASIS: reusing", self.output.getvalue())

    def test_failed_preparation_keeps_receipt_and_does_not_run_leaf(self):
        self.preparation_status = 124
        self.assertEqual(self.run_replay(), 124)
        self.assertEqual([source for source, _ in self.calls], [self.basis])
        receipt = Path(f"{self.calls[0][1]['transcript_output']}.json")
        self.assertTrue(receipt.is_file())
        self.assertIn(str(receipt), self.output.getvalue())
        self.assertIn("leaf was not evaluated", self.errors.getvalue())
        self.abort.assert_called_once()
        self.adopt.assert_not_called()

    def test_rejected_adoption_and_interrupt_abort_only_pending_basis(self):
        self.adopt.side_effect = ValueError("zero axiom admission rejected")
        self.assertEqual(self.run_replay(), 2)
        self.assertEqual(len(self.calls), 1)
        self.abort.assert_called_once()
        self.abort.reset_mock()
        self.preparation_error = KeyboardInterrupt()
        self.assertEqual(self.run_replay(), 130)
        self.abort.assert_called_once()

    def test_basis_must_be_an_exact_needs_dependency(self):
        self.assertEqual(self.run_replay(self.leaf), 2)
        self.leaf.write_text('loadt "basis.ml";;\n')
        self.assertEqual(self.run_replay(), 2)
        self.leaf.write_text('needs "common/helper.ml";;\n')
        self.assertEqual(self.run_replay(), 2)
        self.assertEqual(self.calls, [])
        self.bootstrap.assert_not_called()

    def test_malformed_leaf_and_missing_basis_dependency_keep_refusal_receipts(self):
        for content in ('needs "basis.ml";;\n(* unfinished', 'needs "basis.ml";;\n'):
            with self.subTest(content=content):
                self.leaf.write_text(content)
                if content.endswith(';;\n'):
                    self.helper.unlink()
                receipt = Path(f"{self.transcript}.json")
                if receipt.exists():
                    receipt.unlink()
                self.assertEqual(self.run_replay(), 2)
                recorded = json.loads(receipt.read_text())
                self.assertEqual(recorded["source"], str(self.leaf))
                self.assertEqual(recorded["source_sha256"], sha256_file(self.leaf))
                self.assertEqual(recorded["source_preflight_status"], "project_basis_refused")
                self.assertEqual(recorded["project_basis"]["requested_basis"], str(self.basis))
                self.assertFalse(recorded["source_completed"])
                self.assertEqual(recorded["transport_status"], "not_started")
        self.assertEqual(self.calls, [])

    def test_cache_root_is_separate_from_watch_attempt_receipts(self):
        self.assertEqual(self.run_replay(), 0)
        watch_root = self.run_root / "watch-session"
        watch_transcript = watch_root / "leaf-attempt" / "transcript.log"
        self.assertEqual(run_published_warm_replay(
            self.profile, self.leaf, timeout=765, transcript=watch_transcript,
            evidence_role="recorded_warm_replay", expected_source_sha256=sha256_file(self.leaf),
            basis_source=self.basis, run_root=watch_root, basis_cache_root=self.run_root), 0)
        self.assertEqual([source for source, _ in self.calls], [self.basis, self.leaf, self.leaf])
        self.assertEqual(self.calls[-1][1]["transcript_output"], watch_transcript)
        self.assertEqual(self.adopt.call_count, 1)

    def test_ordinary_replay_remains_one_call(self):
        result = run_published_warm_replay(self.profile, self.leaf, timeout=42,
                                           transcript=self.transcript, evidence_role="recorded_warm_replay")
        self.assertEqual(result, 0)
        self.assertEqual([source for source, _ in self.calls], [self.leaf])
        self.assertNotIn("project_basis_handle", self.calls[0][1])
        self.lookup.assert_not_called()

    def test_cli_accepts_basis_and_watch_forwards_resolved_basis(self):
        for parse in (prove_replay._parse, prove_loop._parse):
            self.assertEqual(parse([str(self.leaf), "--basis", str(self.basis)]).basis, str(self.basis))
        launched = []

        def launch(argv, **kwargs):
            launched.append(argv)
            return SimpleNamespace(poll=lambda: 0)

        def tick(_seconds):
            if launched:
                raise KeyboardInterrupt

        with patch.object(prove_loop, "_profile", return_value="light"), \
             patch.object(prove_loop, "resolve_published_warm_profile", return_value=self.profile), \
             patch.object(prove_loop, "project_revision", return_value="revision"), \
             patch.object(prove_loop, "_replay_receipt", return_value=None), \
             patch.object(prove_loop.subprocess, "Popen", side_effect=launch), \
             patch.object(prove_loop.time, "sleep", side_effect=tick):
            status = prove_loop.main(
                [str(self.leaf), "--loop", "--basis", str(self.basis), "--run-root", str(self.run_root)],
                script_dir=ROOT / "hol-workbench/bin", cwd=self.root)
        self.assertEqual(status, 130)
        self.assertEqual(launched[0][launched[0].index("--basis") + 1], str(self.basis))
        # The watch shares the per-user basis cache with ordinary prove; no private cache root.
        self.assertNotIn("--basis-cache-root", launched[0])
        self.assertNotEqual(launched[0][launched[0].index("--run-root") + 1], str(self.run_root))

    def test_failed_preparation_receipt_does_not_make_watch_retry_unchanged_leaf(self):
        preparation = self.root / "failed-preparation.json"
        preparation.write_text(json.dumps({"source": str(self.basis),
                                           "source_dependency_closure_sha256": "basis-revision"}))
        launched = []
        ticks = 0

        def launch(argv, **kwargs):
            launched.append(argv)
            return SimpleNamespace(poll=lambda: 124)

        def tick(_seconds):
            nonlocal ticks
            if launched:
                ticks += 1
                if ticks == 5:
                    raise KeyboardInterrupt

        with patch.object(prove_loop, "_profile", return_value="light"), \
             patch.object(prove_loop, "resolve_published_warm_profile", return_value=self.profile), \
             patch.object(prove_loop, "project_revision", return_value="leaf-revision"), \
             patch.object(prove_loop, "_replay_receipt", side_effect=[None, preparation]), \
             patch.object(prove_loop.subprocess, "Popen", side_effect=launch), \
             patch.object(prove_loop.time, "sleep", side_effect=tick):
            status = prove_loop.main(
                [str(self.leaf), "--loop", "--basis", str(self.basis), "--run-root", str(self.run_root)],
                script_dir=ROOT / "hol-workbench/bin", cwd=self.root)
        self.assertEqual(status, 130)
        self.assertEqual(len(launched), 1)
        self.assertIn("CURRENT: not accepted", self.output.getvalue())
        self.assertNotIn("STALE:", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
