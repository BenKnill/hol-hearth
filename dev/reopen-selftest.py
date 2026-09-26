#!/usr/bin/env python3
"""Recorded-prefix reopening regressions; original fixtures, no HOL or CRIU."""
from __future__ import annotations

import hashlib
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
from hol_workbench import profile_satisfied_dependencies as satisfaction
from hol_workbench.cli.reopen import reopen
from hol_workbench.criu_snapshot_admission import (
    SnapshotAdmissionMode, SnapshotAdmissionRequest, StaticSnapshotAdmissionDecision,
)
from hol_workbench.hashing import sha256_bytes
from hol_workbench.proofs.loader_scan import scan_ocaml_loaders, scan_prove_bindings
from hol_workbench.proofs.theorem_scan import extract_hol_theorems_bytes
from hol_workbench.source_dependency_closure import build_source_dependency_closure
from hol_workbench.source_execution_plan import decide_profile_satisfaction
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


class _ReopenFixture(unittest.TestCase):
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


class Reopening(_ReopenFixture):
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

    def test_historical_and_current_display_labels_preserve_receipt_identity(self):
        for label in ("Workbench", "Hearth"):
            with self.subTest(label=label):
                self.capture()
                self.payload["source_dependency_closure"]["project_inputs"]["declaration"]["meaning"] = (
                    f"optional display-only project metadata; {label} never executes it"
                )
                self.save()
                self.out = self.outdir / f"{label}_debug.ml"
                result = self.command()
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_nondisplay_declaration_changes_still_refuse(self):
        for key, value in (("status", "present"), ("path", str(self.root / "different.json"))):
            with self.subTest(key=key):
                self.capture()
                self.payload["source_dependency_closure"]["project_inputs"]["declaration"][key] = value
                self.save()
                result = self.command()
                self.assertEqual(result.returncode, 2)
                self.assertIn("closure identity is invalid", result.stderr)
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


class ProfileSatisfiedReopening(_ReopenFixture):
    """hol-hearth#20: external s2n-arm projects import arm/proofs/base.ml through the profile.

    The project has no arm/ tree, so the closure records the needs as unresolved
    and not followed while the receipt records the shelf file that satisfied it.
    """

    def setUp(self):
        super().setUp()
        self.shelf_project = self.root / "shelf-project"
        self.holdir = self.root / "hol"
        self.profile_root = self.root / "profile"
        for directory in (self.shelf_project, self.holdir, self.profile_root):
            directory.mkdir()
        (self.holdir / ".hol-workbench-source-root").write_text("\n")
        self.shelf_base = self.shelf_project / "arm" / "proofs" / "base.ml"
        self.shelf_base.parent.mkdir(parents=True)
        self.shelf_base.write_text("let ARM_BASE_MARKER = 1;;\n")
        self.prefix = 'needs "arm/proofs/base.ml";;\n' + theorem("PREFIX")
        self.source.write_text(self.prefix + theorem(tactic='failwith "control"') + theorem("AFTER"))
        admission = StaticSnapshotAdmissionDecision(
            request=SnapshotAdmissionRequest.create(
                required_capabilities={"seat_reusable", "proof_search_budget_binding_resets"}),
            manifest_path=str(self.profile_root / "snapshot-manifest.json"), manifest_sha256="a" * 64,
            profile_identity={"profile_basis_id": "hol.test.arm", "execution_topology": "mechanical_basis_broker_v3",
                              "fork_snapshot_abi": "proof-run-fork-snapshot.v3",
                              "broker_protocol": "hol-workbench.fork-basis-broker.v1", "broker_runtime_sha256": "e" * 64},
            runtime_compatibility={"compatible": True, "status": "compatible", "binding_budget_owner": "current_controller"},
            mode=SnapshotAdmissionMode.FULL_BINDING_SCOPED, reason="synthetic inventory contract only",
        )
        self.manifest = {
            "static_admission_decision": admission.record(), "profile": "s2n-arm", "profile_basis_id": "hol.test.arm",
            "profile_sha256": "b" * 64, "snapshot_environment_sha256": "c" * 64,
            "profile_cwd": str(self.shelf_project),
            "snapshot_provenance": {"holdir": str(self.holdir),
                                    "loaded_closure": {"strict_sha256": "d" * 64, "entries": [self.inventory()]}},
        }
        self.capture_with_profile()

    def inventory(self):
        data = self.shelf_base.read_bytes()
        return {"path_kind": "absolute", "path": str(self.shelf_base), "resolved_path": str(self.shelf_base),
                "roles": ["profile_cwd"], "basename": "base.ml", "sha256": sha256_bytes(data),
                "loader_md5": hashlib.md5(data, usedforsecurity=False).hexdigest(), "size_bytes": len(data)}

    def capture_with_profile(self):
        self.capture(holdir_root=self.holdir)
        closure = self.payload["source_dependency_closure"]
        record = closure["records"][0]
        self.assertEqual((record["declared_path"], record["resolution"], record["traversal"]),
                         ("arm/proofs/base.ml", "unresolved", "not_followed"))
        self.assertFalse(closure["semantic_identity_complete"])
        with patch.object(satisfaction, "validate_snapshot_manifest", return_value=self.manifest):
            decision, status, _reason = decide_profile_satisfaction(
                closure, profile_root=self.profile_root, logical_profile="s2n-arm",
                profile_cwd=self.shelf_project, holdir_root=self.holdir)
        self.assertEqual(status, "packaged_with_profile_satisfaction")
        self.assertEqual(decision["edges"][0]["resolution"], "profile_satisfied")
        self.payload["logical_profile"] = "s2n-arm"
        self.payload["profile_satisfaction"] = decision
        self.payload["profile_satisfied_dependencies"] = decision["edges"]
        self.save()

    def test_profile_satisfied_needs_is_accepted_away_from_the_project(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle = self.out.with_name(self.out.name + ".reopen")
        origin = json.loads((bundle / "origin.json").read_text())
        self.assertEqual(origin["source_layout"], "copied_package")
        self.assertEqual(origin["profile"], "s2n-arm")
        self.assertEqual(origin["profile_basis_id"], "hol.test.arm")
        self.assertEqual(origin["profile_satisfied_dependencies_role"], "verified_not_copied")
        [edge] = origin["profile_satisfied_dependencies"]
        self.assertEqual(edge["declared_path"], "arm/proofs/base.ml")
        self.assertEqual(edge["host_path"], str(self.shelf_base))
        self.assertEqual(edge["sha256"], sha256_bytes(self.shelf_base.read_bytes()))
        prefix = Path(origin["prefix"]).read_bytes()
        self.assertEqual(prefix[origin["prefix_header_byte_count"]:], self.prefix.encode())
        self.assertEqual([row["role"] for row in origin["copied_files"]], ["entrypoint"])
        self.assertFalse((bundle / "inputs" / "arm").exists(), "profile-satisfied files are never copied")
        scratch = self.out.read_bytes()
        self.assertIn(b"Profile-satisfied import, verified and not copied: arm/proofs/base.ml", scratch)
        self.assertIn("PROFILE INPUTS: 1 literal needs satisfied by profile s2n-arm", result.stdout)
        self.assertIn("--profile s2n-arm", result.stdout)

    def test_changed_shelf_file_is_refused_before_writes(self):
        self.shelf_base.write_text("let ARM_BASE_MARKER = 2;;\n")
        result = self.command()
        self.assertEqual(result.returncode, 2)
        self.assertIn("profile-satisfied dependency changed", result.stderr)
        self.assert_no_outputs()

    def test_unaccounted_unresolved_needs_is_still_refused(self):
        del self.payload["profile_satisfaction"]
        self.save()
        result = self.command()
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing from the exact cold HOLDIR: arm/proofs/base.ml", result.stderr)
        self.assert_no_outputs()

    def test_tampered_decision_or_other_profile_is_refused(self):
        decision = self.payload["profile_satisfaction"]
        for mutate in (
            lambda: decision["edges"][0].__setitem__("sha256", "0" * 64),
            lambda: decision.__setitem__("strict_sha256", "1" * 64),
            lambda: self.payload.__setitem__("logical_profile", "s2n-arm-mlkem"),
        ):
            with self.subTest(mutate=mutate):
                self.capture_with_profile()
                decision = self.payload["profile_satisfaction"]
                mutate()
                self.save()
                result = self.command()
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertNotIn("PROFILE INPUTS", result.stdout)
                self.assert_no_outputs()

    def test_assembly_with_profile_needs_keeps_original_coordinates_only_for_the_object(self):
        (self.project / ".hol-workbench-source-root").write_text("")
        self.object = self.project / "support" / "code.o"
        self.object.write_bytes(b"recorded object bytes")
        self.prefix += 'let mc = define_from_elf "mc" "support/code.o";;\n'
        self.source.write_text(self.prefix + theorem(tactic='failwith "control"'))
        self.capture_with_profile()
        result = self.command()
        self.assertEqual(result.returncode, 2)
        self.assertIn("--out beside the original source", result.stderr)
        self.out = self.source.with_name("TARGET_debug.ml")
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        origin = json.loads(Path(str(self.out) + ".reopen/origin.json").read_text())
        self.assertEqual(origin["source_layout"], "original_project")
        self.assertEqual(origin["profile_satisfied_dependencies"][0]["declared_path"], "arm/proofs/base.ml")
        self.assertIn("PROFILE INPUTS", result.stdout)


if __name__=="__main__":
    unittest.main()
