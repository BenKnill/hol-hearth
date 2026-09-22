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
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))
from hol_workbench.cli.prove_loop import project_revision
from hol_workbench.public_surface_contract import _receipt
from hol_workbench.cli.orbstack_criu_vanilla_semantics import analyze_vanilla_transcript
from hol_workbench.proofs.theorem_scan import extract_hol_theorems_bytes
from hol_workbench.runtime_config import write_runtime_config
from hol_workbench.source_execution_plan import capture_source_dependency_closure
from hol_workbench.vanilla_claims import (
    ClaimProbeContractError, build_claim_probe, first_error, account_claims,
    target_pack_status, diagnostic_claim_output,
)


class AuthoringRegression(unittest.TestCase):
    def test_inspection_exposes_inherited_preparation_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "transcript.log.json"
            _receipt(receipt, succeeded=True, recorded_exit_status=0, bindings=[])
            row = json.loads(receipt.read_text())
            row["project_basis"] = {
                "identity": {"source": "/project/basis.ml", "source_sha256": "a" * 64},
                "preparation_receipt": "/runs/prepared/transcript.log.json",
            }
            receipt.write_text(json.dumps(row))
            result = self.inspect(receipt.parent)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("inherited_project_basis: /project/basis.ml sha=aaaaaaaaaaaa", result.stdout)
            self.assertIn("basis_preparation_receipt: /runs/prepared/transcript.log.json", result.stdout)
            self.assertIn("preparation was checked separately", result.stdout)

    def test_source_preflight_refusals_leave_receipts_without_restore(self):
        from hol_workbench.cli.orbstack_criu_vanilla import run
        for source_bytes in (
            b"let DUP = prove (`T`, REWRITE_TAC[]);;\n" * 2,
            b"let BROKEN = prove (`T`, REWRITE_TAC[]);; (* unfinished",
        ):
            with self.subTest(source=source_bytes), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, transcript = root / "leaf.ml", root / "run" / "transcript.log"
                source.write_bytes(source_bytes)
                restore = Mock(side_effect=AssertionError("preflight must not restore"))
                status = run(profile_root=root / "profile", source=source, timeout=30,
                             idle_timeout=None, restore=restore, transcript_output=transcript,
                             logical_profile="light", display_transcript=False)
                self.assertEqual(status, 2)
                restore.assert_not_called()
                receipt = json.loads(Path(f"{transcript}.json").read_text())
                self.assertEqual(receipt["transport_status"], "not_started")
                self.assertEqual(receipt["source_preflight_status"], "claim_probe_contract_refused")
                self.assertFalse(receipt["source_completed"])
                self.assertEqual(receipt["exit_status"], 2)

    def inspect(self, path, *args):
        return subprocess.run([str(ROOT / "hearth"), "inspect", str(path), *args],
                              cwd="/tmp", capture_output=True, text=True, timeout=10)

    def test_claim_inventory_uses_global_phrase_scope(self):
        source = '''(* Unicode λ keeps byte offsets distinct from characters. *)
let FIRST =
  let lemma = prove (`T`, REWRITE_TAC[]) in lemma;;
let SECOND =
  let lemma = prove (`T`, REWRITE_TAC[]) in lemma;;
module Hidden = struct
  let lemma = prove (`T`, REWRITE_TAC[]);;
end;;
let lemma = prove (`T`, REWRITE_TAC[]) in ignore lemma;;
let text = {|let FAKE = prove (`F`, ALL_TAC);;|};;
let VISIBLE = time prove
 (`!x:bool. x = x`,
  let lemma = prove (`T`, REWRITE_TAC[]) in REWRITE_TAC[]);;
let COMPUTED = prove (derived_goal, REWRITE_TAC[]);;
let DIRECT = ARITH_RULE `2 + 3 = 5`;;
'''.encode()
        claims = extract_hol_theorems_bytes(Path("/work/proof.ml"), source)
        self.assertEqual([claim["name"] for claim in claims], ["VISIBLE", "COMPUTED", "DIRECT"])
        self.assertEqual(claims[0]["source_line"], 11)
        self.assertEqual(claims[0]["statement_quote"], "`!x:bool. x = x`")
        self.assertFalse(claims[1]["statement_extractable"])
        self.assertEqual(claims[1]["prove_argument_preview"], "derived_goal")
        self.assertEqual(claims[2]["proof_constructor"], "ARITH_RULE")
        payload, contract = build_claim_probe(source, claims, nonce="3" * 32)
        self.assertEqual([row["verification_kind"] for row in contract["claims"]], [
            "kernel_conclusion_and_empty_hypotheses",
            "binding_and_thm_type_only_nonliteral_statement",
            "kernel_conclusion_and_empty_hypotheses",
        ])
        self.assertTrue(payload.startswith(source))
        self.assertNotIn(b"= lemma in", payload[len(source):])

    def test_duplicate_global_claims_are_still_rejected(self):
        source = (b"let DUP = prove (`T`, REWRITE_TAC[]);;\n"
                  b"let helper = let DUP = prove (`F`, ALL_TAC) in DUP;;\n"
                  b"let DUP = prove (`T`, REWRITE_TAC[]);;\n")
        claims = extract_hol_theorems_bytes(Path("/work/proof.ml"), source)
        self.assertEqual([claim["source_line"] for claim in claims], [1, 3])
        with self.assertRaisesRegex(ClaimProbeContractError, "duplicate static theorem names: DUP"):
            build_claim_probe(source, claims)

    def test_claim_scope_refuses_malformed_input_and_omits_unsupported_forms(self):
        source = b"let GLOBAL = prove (`T`, REWRITE_TAC[]);;\n"
        for suffix in (b"(* unfinished", b"(", b"module M = struct", b"end;;"):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                extract_hol_theorems_bytes(Path("/work/proof.ml"), source + suffix)
        for body in (
            b"let LOCAL = prove (`T`, REWRITE_TAC[]) in LOCAL;;",
            b"let A = prove (`T`, REWRITE_TAC[]) and B = prove (`T`, REWRITE_TAC[]);;",
            b"module M = struct let HIDDEN = prove (`T`, REWRITE_TAC[]);; end;;",
        ):
            with self.subTest(body=body):
                claims = extract_hol_theorems_bytes(Path("/work/proof.ml"), source + body)
                self.assertEqual([claim["name"] for claim in claims], ["GLOBAL"])

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

    def test_selected_runtime_config_binds_hol_dependencies_and_watch_revision(self):
        for selector in ("HOL_WORKBENCH_RUNTIME_CONFIG", "XDG_CONFIG_HOME"):
            with self.subTest(selector=selector), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                holdir = root / "selected-hol"
                library = holdir / "Library"
                library.mkdir(parents=True)
                helper, dependency = library / "helper.ml", library / "dependency.ml"
                helper.write_text('needs "dependency.ml";;\n')
                dependency.write_text("let n = 1;;\n")
                project = root / "project"
                project.mkdir()
                source = project / "leaf.ml"
                source.write_text('needs "Library/helper.ml";;\n')
                environment = {"HOME": str(root / "home"), selector: str(root / "config")}
                if selector == "HOL_WORKBENCH_RUNTIME_CONFIG":
                    environment[selector] = str(root / "config" / "custom-runtime.toml")
                write_runtime_config(hol_light_dir=holdir, criu_shelf_root=root / "shelves",
                                     criu_bin=Path("/usr/sbin/criu"), environment=environment)
                profile = SimpleNamespace(cwd=holdir, legacy_holdir_roots=(), logical_source_roots=())
                with patch.dict(os.environ, environment, clear=True):
                    closure, captured_holdir = capture_source_dependency_closure(
                        source, profile_cwd=profile.cwd, legacy_holdir_roots=(),
                        logical_source_root_declarations=())
                    self.assertEqual(captured_holdir, holdir)
                    self.assertEqual({record["resolved_path"] for record in closure["records"]},
                                     {str(helper), str(dependency)})
                    self.assertTrue(closure["semantic_identity_complete"])
                    original = project_revision(source, profile)
                    self.assertEqual(original, closure["strict_sha256"])
                    stamp = dependency.stat()
                    dependency.write_text("let n = 2;;\n")
                    os.utime(dependency, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
                    self.assertNotEqual(original, project_revision(source, profile),
                                        "an imported HOL dependency edit must invalidate the watched result")

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

    def test_inspect_exposes_probe_strength_without_changing_receipt_or_acceptance(self):
        source = (b"let LITERAL = prove (`T`, REWRITE_TAC[]);;\n"
                  b"let COMPUTED = prove (derived_goal, REWRITE_TAC[]);;\n")
        claims = extract_hol_theorems_bytes(Path("/work/proof.ml"), source)
        _, contract = build_claim_probe(source, claims, nonce="5" * 32)
        transcript = ("\n".join(row["ok_marker"] for row in contract["claims"]) +
                      "\n" + contract["completion_marker"] + "\n").encode()
        result = analyze_vanilla_transcript(claims=claims, transcript=transcript, contract=contract,
                                          transport="completed", response={"exit_status": 0})
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "transcript.log.json"
            _receipt(receipt, succeeded=True, recorded_exit_status=0, bindings=result["bindings"])
            original = receipt.read_bytes()
            for flags in ((), ("--verbose",), ("--binding", "COMPUTED")):
                rendered = self.inspect(receipt, *flags)
                self.assertEqual(rendered.returncode, 0, rendered.stderr)
                self.assertIn("COMPUTED: thm bound (conclusion and hypotheses not checked)", rendered.stdout)
                self.assertIn("successful_probe_counts: conclusion_checked=1 thm_type_only=1 unknown=0",
                              rendered.stdout)
                if flags:
                    self.assertIn("verification_kind: binding_and_thm_type_only_nonliteral_statement",
                                  rendered.stdout)
            literal = self.inspect(receipt, "--binding", "LITERAL")
            self.assertIn("LITERAL: proved (source conclusion matched; hypotheses empty)", literal.stdout)
            self.assertNotIn("COMPUTED:", literal.stdout)
            self.assertEqual(json.loads(self.inspect(receipt, "--json", "--binding", "COMPUTED").stdout),
                             json.loads(original))
            self.assertEqual(receipt.read_bytes(), original)
            _receipt(receipt, succeeded=False, recorded_exit_status=1, bindings=result["bindings"])
            rejected = self.inspect(receipt, "--binding", "COMPUTED")
            self.assertEqual(rejected.returncode, 1)
            self.assertIn("COMPUTED: thm bound (conclusion and hypotheses not checked)", rejected.stdout)
            self.assertIn("source_acceptance: not_accepted", rejected.stdout)

    def test_inspect_does_not_infer_probe_strength_or_claim_failed_checks_passed(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "transcript.log.json"
            bindings = [
                {"name": "LEGACY", "status": "proved"},
                {"name": "FUTURE", "status": "proved", "verification_kind": "future_probe"},
                {"name": "MISMATCH", "status": "failed",
                 "verification_kind": "kernel_conclusion_and_empty_hypotheses"},
            ]
            _receipt(receipt, succeeded=False, recorded_exit_status=1, bindings=bindings)
            rendered = self.inspect(receipt, "--verbose")
            for name in ("LEGACY", "FUTURE"):
                self.assertIn(f"{name}: proved (probe strength not recorded or unrecognized)", rendered.stdout)
            self.assertIn("verification_kind: future_probe", rendered.stdout)
            self.assertIn("MISMATCH: failed", rendered.stdout)
            self.assertNotIn("source conclusion matched", rendered.stdout)
            self.assertIn("successful_probe_counts: conclusion_checked=0 thm_type_only=0 unknown=2",
                          rendered.stdout)


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
            bindings[-2]["verification_kind"] = "kernel_conclusion_and_empty_hypotheses"
            bindings[-1]["verification_kind"] = "binding_and_thm_type_only_nonliteral_statement"
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
            self.assertIn("successful_probe_counts: conclusion_checked=1 thm_type_only=1 unknown=71",
                          result.stdout)
            self.assertNotIn("GOOD_72:", result.stdout)
            _receipt(receipt, succeeded=True, bindings=bindings[:73])
            passed = self.inspect(receipt)
            self.assertIn("binding_counts: proved=73 failed=0 printed_unprobed=0 missing=0 unknown=0",
                          passed.stdout)
            self.assertIn("61 more (61 proved)", passed.stdout)


if __name__ == "__main__":
    unittest.main()
