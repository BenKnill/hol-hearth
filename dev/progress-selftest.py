#!/usr/bin/env python3
"""Progress timing, CLI boundaries and cleanup checks; no HOL or CRIU."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))
from hol_workbench.cli import prove_loop, prove_replay
from hol_workbench.cli.replay_progress import ReplayProgress


class ProgressRegression(unittest.TestCase):
    def test_queue_does_not_spend_request_budget(self):
        now = [100.0]
        output = io.StringIO()
        progress = ReplayProgress(timeout=180, interval=15, stream=output, clock=lambda: now[0])
        progress.set_phase("admission")
        now[0] += 400
        progress.emit()
        self.assertIn("phase=admission elapsed=400.0s", output.getvalue())
        self.assertIn("evaluation request not started", output.getvalue())
        self.assertNotIn("requested_remaining", output.getvalue())
        progress.set_phase("evaluation-request")
        now[0] += 20
        progress.emit()
        self.assertIn("elapsed=420.0s phase_elapsed=20.0s", output.getvalue())
        self.assertIn("requested_budget=180s requested_remaining=160.0s", output.getvalue())
        self.assertIn("response_limit=195s response_remaining=175.0s", output.getvalue())
        now[0] += 180
        progress.emit()
        self.assertIn("requested_remaining=0.0s", output.getvalue())
        self.assertIn("response_remaining=0.0s", output.getvalue())
        self.assertEqual(output.getvalue().count("WAIT:"), 1)

    def test_short_budget_keeps_existing_controller_allowance(self):
        output = io.StringIO()
        progress = ReplayProgress(timeout=2, interval=1, stream=output, clock=lambda: 0)
        progress.set_phase("evaluation-request")
        progress.emit()
        self.assertIn("requested_budget=2s requested_remaining=2.0s", output.getvalue())
        self.assertIn("response_limit=30s response_remaining=30.0s", output.getvalue())

    def test_thread_stops_on_exception_and_disabled_is_silent(self):
        written = threading.Event()

        class Stream(io.StringIO):
            def flush(self):
                written.set()

        stream = Stream()
        progress = ReplayProgress(timeout=180, interval=0.01, stream=stream)
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with progress:
                self.assertTrue(written.wait(2), "heartbeat did not arrive")
                raise RuntimeError("fixture")
        self.assertFalse(progress.thread.is_alive())
        before = stream.getvalue()
        progress.emit()
        self.assertEqual(before, stream.getvalue())
        with ReplayProgress(timeout=180, interval=0, stream=stream) as disabled:
            disabled.set_phase("evaluation-request")
            disabled.set_phase("cancelling")
        self.assertIsNone(disabled.thread)
        self.assertEqual(before, stream.getvalue())

    def test_blocked_output_cannot_hold_cancellation_or_context_exit(self):
        for blocking_operation in ("write", "flush"):
            with self.subTest(blocking_operation=blocking_operation):
                entered = threading.Event()
                release = threading.Event()
                phase_done = threading.Event()
                exit_done = threading.Event()

                class BlockedSink:
                    def write(self, text):
                        if blocking_operation == "write":
                            entered.set()
                            release.wait()

                    def flush(self):
                        if blocking_operation == "flush":
                            entered.set()
                            release.wait()

                progress = ReplayProgress(timeout=180, interval=0.01, stream=BlockedSink())
                progress.__enter__()
                controller = None
                try:
                    self.assertTrue(entered.wait(1), "reporter did not enter blocked output")

                    def cancel_and_exit():
                        progress.set_phase("cancelling")
                        phase_done.set()
                        progress.__exit__(None, None, None)
                        exit_done.set()

                    controller = threading.Thread(target=cancel_and_exit, daemon=True)
                    controller.start()
                    self.assertTrue(phase_done.wait(1), "cancellation callback waited for output")
                    self.assertTrue(exit_done.wait(1), "context exit waited for blocked reporter")
                    self.assertFalse(release.is_set(), "sink must stay blocked through both checks")
                    self.assertTrue(progress.thread.is_alive(), "test must exercise a stalled writer")
                    self.assertTrue(progress.thread.daemon)
                finally:
                    release.set()
                    if controller is not None:
                        controller.join(timeout=2)
                    progress.__exit__(None, None, None)
                    progress.thread.join(timeout=2)
                self.assertFalse(progress.thread.is_alive(), "released reporter did not terminate")
                self.assertTrue(exit_done.is_set())

    def test_cancellation_wakes_only_the_background_reporter(self):
        written = threading.Event()
        writer_threads = []

        class Stream(io.StringIO):
            def write(self, text):
                writer_threads.append(threading.current_thread())
                return super().write(text)

            def flush(self):
                written.set()

        output = Stream()
        with ReplayProgress(timeout=180, interval=3600, stream=output) as progress:
            progress.set_phase("cancelling")
            self.assertTrue(written.wait(1), "cancellation did not wake reporter")
            self.assertIn("phase=cancelling", output.getvalue())
            self.assertEqual(writer_threads, [progress.thread])

    def test_cli_rejects_unbounded_output_and_nonfinite_budgets(self):
        for parse in (prove_replay._parse, prove_loop._parse):
            for interval in ("-1", "0.1", "nan", "inf"):
                with self.subTest(parser=parse.__module__, interval=interval):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        parse(["source.ml", "--progress-interval", interval])
            for timeout in ("0", "nan", "inf"):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    parse(["source.ml", "--timeout", timeout])
            self.assertEqual(parse(["source.ml"]).progress_interval, 15)
            self.assertEqual(parse(["source.ml", "--progress-interval", "0"]).progress_interval, 0)

    def test_timeout_handoff_is_attempt_specific_and_budget_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.ml"
            source.write_text("let source = 1;;\n")
            attempt = root / "runs" / "attempt"
            attempt.mkdir(parents=True)
            transcript = attempt / "transcript.log"

            def replay(profile, actual_source, **kwargs):
                self.assertEqual(actual_source, source)
                self.assertEqual(kwargs["timeout"], 180)
                kwargs["on_phase"]("admission")
                kwargs["on_phase"]("evaluation-request")
                Path(str(kwargs["transcript"]) + ".json").write_text(json.dumps({
                    "transport_status": "timeout", "semantic_source_status": "not_completed",
                }))
                return 124

            out, err = io.StringIO(), io.StringIO()
            with patch.object(prove_replay, "_profile", return_value="light"), \
                 patch.object(prove_replay, "resolve_published_warm_profile", return_value=object()), \
                 patch.object(prove_replay, "default_transcript_path", return_value=transcript), \
                 patch.object(prove_replay, "run_published_warm_replay", side_effect=replay), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = prove_replay.main(
                    [str(source), "--timeout", "180", "--progress-interval", "0"],
                    script_dir=ROOT / "hol-workbench/bin", cwd=root,
                )
            self.assertEqual(status, 124)
            self.assertIn("INCOMPLETE: timeout", out.getvalue())
            self.assertIn(f"inspect {attempt}", out.getvalue())
            self.assertNotIn("SOURCE CHECK: passed", out.getvalue())
            self.assertNotIn("PROGRESS:", out.getvalue())
            self.assertEqual(err.getvalue(), "")

    def test_loop_forwards_budget_and_progress_to_same_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.ml"
            source.write_text("let source = 1;;\n")
            launched = []

            def launch(argv, **kwargs):
                launched.append(argv)
                return SimpleNamespace(poll=lambda: 0)

            def tick(seconds):
                if launched:
                    raise KeyboardInterrupt

            with patch.object(prove_loop, "_profile", return_value="light"), \
                 patch.object(prove_loop, "resolve_published_warm_profile", return_value=object()), \
                 patch.object(prove_loop, "project_revision", return_value="revision"), \
                 patch.object(prove_loop, "_replay_receipt", return_value=None), \
                 patch.object(prove_loop.subprocess, "Popen", side_effect=launch), \
                 patch.object(prove_loop.time, "sleep", side_effect=tick), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = prove_loop.main(
                    [str(source), "--loop", "--timeout", "180", "--progress-interval", "7",
                     "--run-root", str(Path(temporary) / "runs")],
                    script_dir=ROOT / "hol-workbench/bin", cwd=temporary,
                )
            self.assertEqual(result, 130)
            self.assertEqual(len(launched), 1)
            command = launched[0]
            self.assertEqual(command[0], str(ROOT / "hol-workbench/bin/prove"))
            self.assertEqual(command[command.index("--timeout") + 1], "180.0")
            self.assertEqual(command[command.index("--progress-interval") + 1], "7.0")


if __name__ == "__main__":
    unittest.main()
