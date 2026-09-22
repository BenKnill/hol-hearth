#!/usr/bin/env python3
"""Recorded-prefix reopening regressions; original fixtures, no HOL or CRIU."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))
from hol_workbench.cli.reopen import reopen
from hol_workbench.hashing import sha256_bytes
from hol_workbench.proofs.loader_scan import scan_ocaml_loaders, scan_prove_bindings
from hol_workbench.proofs.theorem_scan import extract_hol_theorems_bytes
from hol_workbench.source_dependency_closure import build_source_dependency_closure
from hol_workbench.vanilla_claims import claim_doc

Q = chr(96)


def theorem(name="TARGET", tactic="REWRITE_TAC[]", goal="!x:real. x = x"):
    return f"let {name} = prove\n ({Q}{goal}{Q}, {tactic});;\n"


class BindingBoundaries(unittest.TestCase):
    def test_byte_spans_quotes_nested_comments_and_blocks(self):
        prefix = (
            '(* Unicode λ and nested (* comment ;; *) *)\r\n'
            'let s = "let TARGET = prove (;;";;\n'
            "let character = ';';;\n"
            'module M = struct\nlet INNER = prove (' + Q + 'T' + Q + ', ALL_TAC);;\nend;;\n'
        )
        proof = theorem(tactic='(let s = {| THEN ;; (* " |} in ALL_TAC) THEN REWRITE_TAC[]')
        source = (prefix + proof + theorem("LATER")).encode()
        result = scan_prove_bindings(source)
        self.assertEqual(result.status, "ok")
        self.assertEqual([row.name for row in result.bindings], ["TARGET", "LATER"])
        row = result.bindings[0]
        self.assertEqual(row.binding_span.bytes_from(source), proof.rstrip().encode())
        self.assertEqual(source[:row.binding_span.start], prefix.encode())
        self.assertEqual(row.statement_span.bytes_from(source), (Q+"!x:real. x = x"+Q).encode())
        self.assertIn(b"THEN REWRITE_TAC[]", row.tactic_span.bytes_from(source))

    def test_unsupported_ambiguous_and_local_forms(self):
        cases = [
            theorem() + theorem(),
            theorem() + "let TARGET = 42;;",
            theorem() + "let (TARGET, other) = (42, 0);;",
            theorem() + "let other = 0 and TARGET = 42;;",
            "let OUTER = " + theorem().rstrip().removesuffix(";;") + " in TARGET;;",
            "module M = struct\n" + theorem() + "end;;",
            "let TARGET = prove (goal, ALL_TAC);;",
            "let TARGET = REAL_ARITH " + Q + "&1 = &1" + Q + ";;",
            theorem().replace(");;", ") in TARGET;;"),
            theorem().replace(");;", ", another);;"),
            theorem().removesuffix(";;\n"),
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(scan_prove_bindings(source.encode()).bindings, ())

    def test_timed_prove_and_quoted_comment_delimiter(self):
        source=theorem(tactic='failwith "|hearth_reopen} *)"').replace("= prove", "= time prove")
        result=scan_prove_bindings(source.encode())
        self.assertEqual(len(result.bindings),1)
        self.assertEqual(result.bindings[0].name,"TARGET")

    def test_malformed_suffix_invalidates_earlier_boundary(self):
        for tail in (b"(* unfinished", b'let s = "', b"\xff", b"("):
            result = scan_prove_bindings(theorem().encode()+tail)
            self.assertEqual(result.status, "refused")
            self.assertEqual(result.bindings, ())


class Reopening(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root/"project"
        (self.project/"support").mkdir(parents=True)
        self.base = self.project/"base helper.ml"
        self.base.write_text("let helper = 7;;\n")
        self.helper = self.project/"support"/"helper.ml"
        self.helper.write_text('needs "../base helper.ml";;\n')
        self.source = self.project/"leaf.ml"
        self.prefix = 'needs "support/helper.ml";;\n'+theorem("PREFIX")
        self.source.write_text(self.prefix+theorem(tactic='failwith "inactive *) marker"')+theorem("AFTER"))
        self.receipt = self.root/"runs"/"attempt"/"transcript.log.json"
        self.receipt.parent.mkdir(parents=True)
        self.outdir = self.root/"scratch with spaces"
        self.outdir.mkdir()
        self.out = self.outdir/"TARGET_debug.ml"
        self.capture()

    def capture(self, **closure_options):
        data = self.source.read_bytes()
        closure = build_source_dependency_closure(self.source, **closure_options)
        inventory = extract_hol_theorems_bytes(self.source, data)
        claims = [
            claim_doc(c, status="missing", evidence="nonce_probe_marker_missing",
                      observed_line=None, first_error_line="Exception: Failure", first_error_lineno=8,
                      verification_kind="kernel_conclusion_and_empty_hypotheses",
                      marker_count=0, mismatch_marker_count=0, diagnostic_lines=[])
            for c in inventory
        ]
        self.payload = {
            "schema":"hol-workbench.warm-vanilla-artifact.v1",
            "evidence":"recorded_warm_replay",
            "semantic_source_status":"failed","source_completed":False,
            "logical_profile":"light",
            "source":str(self.source),"source_sha256":sha256_bytes(data),
            "source_dependency_closure":closure,
            "source_dependency_closure_sha256":closure["strict_sha256"],
            "bindings":[{"name":c["name"],"status":"missing"} for c in inventory],
            "transcript_accounting":{"claim_accounting":claims},
        }
        self.save()

    def save(self):
        self.receipt.write_text(json.dumps(self.payload))

    def command(self, name="TARGET"):
        return subprocess.run(
            [str(ROOT/"hearth"), "reopen", str(self.receipt.parent.parent),
             "--binding",name,"--out",str(self.out)],
            cwd="/tmp",capture_output=True,text=True,timeout=10)

    def assert_no_outputs(self):
        self.assertEqual(list(self.outdir.iterdir()), [])

    def test_prefix_goal_and_transitive_bytes_are_preserved(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = self.out.with_name(self.out.name+".reopen")
        origin = json.loads((bundle/"origin.json").read_text())
        prefix = Path(origin["prefix"]).read_bytes()
        self.assertEqual(prefix[origin["prefix_header_byte_count"]:], self.prefix.encode())
        self.assertEqual((bundle/"inputs"/"support"/"helper.ml").read_bytes(), self.helper.read_bytes())
        self.assertEqual((bundle/"inputs"/"base helper.ml").read_bytes(), self.base.read_bytes())
        scratch = self.out.read_bytes()
        self.assertIn(("g "+Q+"!x:real. x = x"+Q+";;").encode(),scratch)
        self.assertIn(b'inactive *) marker',scratch)
        self.assertIn(b"DIAGNOSTIC ONLY",scratch)
        self.assertNotIn(b"let TARGET =",scratch)
        scan = scan_ocaml_loaders(scratch)
        self.assertEqual(scan.status,"ok")
        self.assertEqual(len(scan.occurrences),1)
        self.assertEqual(scan.occurrences[0].loader,"needs")
        self.assertIn("no HOL was run",result.stdout)
        self.assertIn("--run-root",result.stdout)
        self.assertIn(str(ROOT/"hearth")+" prove ",result.stdout)

    def test_changed_entrypoint_same_size_is_refused(self):
        self.source.write_bytes(self.source.read_bytes().replace(b"REWRITE_TAC",b"REWRITX_TAC"))
        result = self.command()
        self.assertEqual(result.returncode,2)
        self.assertIn("changed",result.stderr)
        self.assert_no_outputs()

    def assembly(self):
        (self.project / ".hol-workbench-source-root").write_text("")
        self.object = self.project / "support" / "code.o"
        self.object.write_bytes(b"recorded object bytes")
        self.prefix += 'let mc = define_from_elf "mc" "support/code.o";;\n'
        self.source.write_text(self.prefix + theorem(tactic='failwith "control"'))
        self.capture()

    def test_assembly_keeps_exact_prefix_at_original_project_coordinates(self):
        self.assembly()
        self.out = self.source.with_name("TARGET_debug.ml")
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        origin = json.loads(Path(str(self.out) + ".reopen/origin.json").read_text())
        self.assertEqual(origin["source_layout"], "original_project")
        self.assertEqual(origin["copied_files_role"], "verified_reference")
        start, end = origin["scratch_prefix_byte_span"]
        self.assertEqual(self.out.read_bytes()[start:end], self.prefix.encode())
        self.assertEqual(
            Path(str(self.out) + ".reopen/inputs/support/code.o").read_bytes(), self.object.read_bytes()
        )
        self.assertIn("ordinary prove recaptures source and ELF bytes", result.stdout)
        closure = build_source_dependency_closure(self.out)
        self.assertEqual(closure["artifacts"][0]["resolved_path"], str(self.object))
        self.assertEqual(closure["artifacts"][0]["sha256"], sha256_bytes(self.object.read_bytes()))
        self.assertEqual(self.command().returncode, 2)

    def test_assembly_relocation_is_refused_before_writes(self):
        self.assembly()
        result = self.command()
        self.assertEqual(result.returncode, 2)
        self.assertIn("--out beside the original source", result.stderr)
        self.assert_no_outputs()

    def test_changed_assembly_object_is_refused_before_writes(self):
        self.assembly()
        self.out = self.source.with_name("TARGET_debug.ml")
        self.object.write_bytes(b"different object bytes")
        result = self.command()
        self.assertEqual(result.returncode, 2)
        self.assertIn("ELF artifact bytes changed", result.stderr)
        self.assertFalse(self.out.exists())
        self.assertFalse(Path(str(self.out) + ".reopen").exists())

    def test_assembly_preserves_recorded_basis_and_run_root(self):
        self.assembly()
        self.out = self.source.with_name("TARGET_debug.ml")
        self.payload["project_basis"] = {
            "identity": {"source": str(self.helper), "source_sha256": sha256_bytes(self.helper.read_bytes())},
            "preparation_receipt": str(self.receipt.parent.parent / "preparation" / "transcript.log.json"),
        }
        self.payload["requested_timeout_seconds"] = 3600.0
        self.save()
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        origin = json.loads(Path(str(self.out) + ".reopen/origin.json").read_text())
        self.assertEqual(origin["basis"], str(self.helper))
        self.assertEqual(origin["run_root"], str(self.receipt.parent.parent))
        self.assertIn("--basis", result.stdout)
        self.assertIn("ordinary prove checks compatibility", result.stdout)
        self.assertEqual(origin["timeout_seconds"], 3600.0)
        self.assertIn("--timeout 3600.0", result.stdout)

    def test_captured_library_import_is_verified_without_relocation(self):
        holdir = self.root / "hol"
        holdir.mkdir()
        library = holdir / "library.ml"
        library.write_text("let imported = 12;;\n")
        self.prefix = 'needs "library.ml";;\n'
        self.source.write_text(self.prefix + theorem())
        self.capture(holdir_root=holdir)
        self.out = self.source.with_name("TARGET_debug.ml")
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        origin = json.loads(Path(str(self.out) + ".reopen/origin.json").read_text())
        self.assertEqual(origin["source_layout"], "original_project")
        self.assertEqual(origin["copied_files_role"], "verified_reference")
        self.out = self.source.with_name("TARGET_debug_changed.ml")
        library.write_text("let imported = 13;;\n")
        result = self.command()
        self.assertEqual(result.returncode, 2)
        self.assertIn("dependency bytes changed", result.stderr)
        self.assertFalse(self.out.exists())
        self.assertFalse(Path(str(self.out) + ".reopen").exists())

    def test_changed_transitive_dependency_has_no_partial_files(self):
        self.base.write_text("let helper = 8;;\n")
        result=self.command()
        self.assertEqual(result.returncode,2,result.stdout)
        self.assertIn("dependency bytes changed",result.stderr)
        self.assert_no_outputs()

    def test_symlinked_dependency_is_refused(self):
        target=self.project/"replacement.ml"
        self.base.rename(target)
        self.base.symlink_to(target)
        result=self.command()
        self.assertEqual(result.returncode,2)
        self.assert_no_outputs()

    def test_imported_or_unrecorded_binding_is_refused(self):
        result=self.command("IMPORTED")
        self.assertEqual(result.returncode,2)
        self.assertIn("entrypoint",result.stderr)
        self.assert_no_outputs()
        self.payload["transcript_accounting"]["claim_accounting"][1]["source"]=str(self.helper)
        self.save()
        result=self.command()
        self.assertEqual(result.returncode,2)
        self.assertIn("imported",result.stderr)
        self.assert_no_outputs()

    def test_successful_or_already_proved_binding_is_refused(self):
        self.payload["bindings"][1]["status"]="proved"
        self.save()
        self.assertEqual(self.command().returncode,2)
        self.assert_no_outputs()
        self.payload["semantic_source_status"]="succeeded"
        self.payload["source_completed"]=True
        self.save()
        self.assertEqual(self.command().returncode,2)
        self.assert_no_outputs()

    def test_changed_statement_or_closure_is_refused(self):
        self.payload["transcript_accounting"]["claim_accounting"][1]["statement"]="F"
        self.save()
        self.assertEqual(self.command().returncode,2)
        self.assert_no_outputs()
        self.capture()
        self.payload["source_dependency_closure"]["entrypoint"]["sha256"]="0"*64
        self.save()
        self.assertEqual(self.command().returncode,2)
        self.assert_no_outputs()

    def test_ambiguous_binding_is_refused(self):
        self.source.write_text(self.source.read_text()+theorem())
        self.capture()
        self.assertEqual(self.command().returncode,2)
        self.assert_no_outputs()

    def test_no_overwrite_even_dangling_symlink(self):
        self.out.write_bytes(b"keep")
        self.assertEqual(self.command().returncode,2)
        self.assertEqual(self.out.read_bytes(),b"keep")
        self.out.unlink()
        self.out.symlink_to(self.root/"absent")
        self.assertEqual(self.command().returncode,2)
        self.assertTrue(self.out.is_symlink())
        self.assertFalse(self.out.with_name(self.out.name+".reopen").exists())

    def test_existing_companion_is_not_changed(self):
        bundle=self.out.with_name(self.out.name+".reopen")
        bundle.mkdir()
        (bundle/"keep").write_bytes(b"keep")
        self.assertEqual(self.command().returncode,2)
        self.assertEqual((bundle/"keep").read_bytes(),b"keep")
        self.assertFalse(self.out.exists())

    def test_publish_race_rolls_back_only_owned_files(self):
        def competing_link(*args,**kwargs):
            self.out.write_bytes(b"other author")
            raise FileExistsError("competing author created output")
        with patch("hol_workbench.cli.reopen.os.link",side_effect=competing_link):
            with self.assertRaises(FileExistsError):
                reopen(self.receipt,binding_name="TARGET",out=self.out)
        self.assertEqual(self.out.read_bytes(),b"other author")
        self.assertEqual(list(self.outdir.iterdir()),[self.out])

    def test_interrupt_after_link_preserves_complete_published_scratch(self):
        real_link = os.link

        def linked_then_interrupted(source, destination):
            real_link(source, destination)
            raise KeyboardInterrupt("signal after successful publication")

        with patch("hol_workbench.cli.reopen.os.link", side_effect=linked_then_interrupted):
            with self.assertRaises(KeyboardInterrupt):
                reopen(self.receipt, binding_name="TARGET", out=self.out)
        bundle = self.out.with_name(self.out.name + ".reopen")
        self.assertEqual(set(self.outdir.iterdir()), {self.out, bundle})
        origin = json.loads((bundle / "origin.json").read_text())
        self.assertEqual(sha256_bytes(self.out.read_bytes()), origin["scratch_sha256"])
        prefix = Path(origin["prefix"]).read_bytes()
        self.assertEqual(prefix[origin["prefix_header_byte_count"]:], self.prefix.encode())
        for row in origin["copied_files"]:
            if row["role"] != "entrypoint":
                self.assertEqual(
                    sha256_bytes((bundle / "inputs" / row["package_path"]).read_bytes()),
                    row["sha256"],
                )
        self.assertEqual(self.command().returncode, 2, "a retained artifact cannot be overwritten")
        self.assertEqual(sha256_bytes(self.out.read_bytes()), origin["scratch_sha256"])

    def test_missing_parent_and_unsupported_loader_leave_no_files(self):
        with self.assertRaises(OSError):
            reopen(self.receipt,binding_name="TARGET",out=self.outdir/"absent"/"debug.ml")
        self.assert_no_outputs()
        self.source.write_text(self.source.read_text().replace('needs "support/helper.ml"','#use "support/helper.ml"'))
        self.capture()
        result=self.command()
        self.assertEqual(result.returncode,2)
        self.assertIn("#use",result.stderr)
        self.assert_no_outputs()


if __name__=="__main__":
    unittest.main()
