#!/usr/bin/env python3
"""Content-keyed authoring cache controls; original fixtures, no HOL or CRIU."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))

from hol_workbench import source_analysis_cache as cache
from hol_workbench import source_dependency_closure as scanner
from hol_workbench.cli.prove_loop import project_revision
from hol_workbench.source_execution_plan import capture_source_dependency_closure


class SourceAnalysisCache(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / ".hol-workbench-source-root").touch()
        self.leaf = self.project / "leaf.ml"
        self.helper = self.project / "helper.ml"
        self.leaf.write_text('needs "helper.ml";;\nlet TARGET = prove (`T`, REWRITE_TAC[]);;\n')
        self.helper.write_text("let helper = 1;;\n")
        self.cache_home = self.root / "cache"
        self.enterContext(patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache_home)}))
        self.enterContext(patch("hol_workbench.source_execution_plan.machine_holdir_authority", return_value=None))
        self.scans = self.enterContext(patch.object(scanner, "scan_ocaml_loaders", wraps=scanner.scan_ocaml_loaders))

    def capture(self):
        closure, _ = capture_source_dependency_closure(
            self.leaf, profile_cwd=None, legacy_holdir_roots=(), logical_source_root_declarations=(),
        )
        self.assertTrue(scanner.source_dependency_closure_identity_matches(closure))
        return closure

    def test_cache_hit_matches_uncached_closure_without_relexing(self):
        first = self.capture()
        self.assertEqual(self.scans.call_count, 2)
        self.scans.reset_mock()
        repeated = self.capture()
        self.scans.assert_not_called()
        self.assertEqual(repeated["analysis_cache"]["hits"], 2)
        self.assertEqual(repeated["analysis_cache"]["misses"], 0)
        uncached = scanner.build_source_dependency_closure(self.leaf)
        for actual in (first, repeated):
            self.assertEqual({key: value for key, value in actual.items() if key != "analysis_cache"}, uncached)
        self.assertTrue(cache.source_analysis_cache_root().is_relative_to(self.cache_home / "hol-hearth/source-analysis"))

    def test_same_size_same_mtime_source_edit_rehashes_and_relexes(self):
        before = self.capture()
        old = self.helper.stat()
        self.helper.write_text("let helper = 2;;\n")
        os.utime(self.helper, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.scans.reset_mock()
        after = self.capture()
        self.assertEqual(self.scans.call_count, 1)
        self.assertNotEqual(before["strict_sha256"], after["strict_sha256"])
        self.assertEqual(after["analysis_cache"]["hits"], 1)
        self.assertEqual(after["analysis_cache"]["misses"], 1)

    def test_cached_literals_still_resolve_missing_and_new_source_files(self):
        before = self.capture()
        original = self.helper.read_bytes()
        self.helper.unlink()
        self.scans.reset_mock()
        missing = self.capture()
        self.scans.assert_not_called()
        self.assertFalse(missing["semantic_identity_complete"])
        self.assertNotEqual(before["strict_sha256"], missing["strict_sha256"])
        self.helper.write_bytes(original)
        recovered = self.capture()
        self.scans.assert_not_called()
        self.assertEqual(before["strict_sha256"], recovered["strict_sha256"])

    def test_cached_artifact_literals_still_hash_objects_and_refuse_missing_objects(self):
        obj = self.project / "code.o"
        obj.write_bytes(b"original-object")
        self.helper.write_text('let code = define_from_elf "code" "code.o";;\n')
        before = self.capture()
        self.assertTrue(before["semantic_identity_complete"])
        obj.write_bytes(b"modified-object")
        self.scans.reset_mock()
        edited = self.capture()
        self.assertNotEqual(before["strict_sha256"], edited["strict_sha256"])
        obj.unlink()
        missing = self.capture()
        self.scans.assert_not_called()
        self.assertFalse(missing["semantic_identity_complete"])
        self.assertEqual(missing["unresolved_artifact_count"], 1)

    def test_dynamic_and_malformed_edits_remain_refused_on_cache_hits(self):
        self.capture()
        for source in ('needs chosen_path;;\n', 'needs "helper.ml";;\n(* unfinished'):
            with self.subTest(source=source):
                self.leaf.write_text(source)
                first = self.capture()
                self.assertFalse(first["semantic_identity_complete"])
                self.scans.reset_mock()
                repeated = self.capture()
                self.scans.assert_not_called()
                self.assertEqual(first["strict_sha256"], repeated["strict_sha256"])

    def test_invalid_cache_payload_falls_back_to_existing_scanner(self):
        before = self.capture()
        paths = list(cache.source_analysis_cache_root().rglob("*.json"))
        self.assertEqual(len(paths), 2)
        paths[0].write_text('{"schema":"wrong-schema","analysis":{}}')
        paths[1].write_text("invalid JSON")
        self.scans.reset_mock()
        after = self.capture()
        self.assertEqual(self.scans.call_count, 2)
        self.assertEqual(before["strict_sha256"], after["strict_sha256"])

    def test_unwritable_cache_location_preserves_uncached_capture(self):
        self.cache_home.write_text("a file cannot contain cache entries")
        first = self.capture()
        self.scans.reset_mock()
        repeated = self.capture()
        self.assertEqual(self.scans.call_count, 2)
        self.assertEqual(first["strict_sha256"], repeated["strict_sha256"])
        self.assertEqual(repeated["analysis_cache"]["writes"], 0)

    def test_watcher_reuses_lexing_and_detects_transitive_edits(self):
        profile = SimpleNamespace(cwd=None, legacy_holdir_roots=(), logical_source_roots=())
        before = project_revision(self.leaf, profile)
        self.assertFalse(before.startswith("unavailable:"))
        self.scans.reset_mock()
        for _ in range(3):
            self.assertEqual(project_revision(self.leaf, profile), before)
        self.scans.assert_not_called()
        self.helper.write_text("let helper = 3;;\n")
        self.assertNotEqual(project_revision(self.leaf, profile), before)
        self.assertEqual(self.scans.call_count, 1)

    def test_parser_source_change_uses_new_namespace_in_fresh_process(self):
        install = self.root / "install"
        runtime = install / "hol_workbench"
        shutil.copytree(ROOT / "hol-workbench/hol_workbench", runtime,
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        holdir = self.root / "holdir"
        holdir.mkdir()
        command = [sys.executable, "-I", "-B", "-c", "\n".join([
            "import json,sys",
            "from pathlib import Path",
            "sys.path.insert(0,sys.argv[1])",
            "from hol_workbench.source_analysis_cache import source_analysis_cache_root",
            "from hol_workbench.source_execution_plan import capture_source_dependency_closure",
            "closure,_=capture_source_dependency_closure(Path(sys.argv[2]),profile_cwd=None,legacy_holdir_roots=(),"
            "logical_source_root_declarations=(),holdir_root_override=Path(sys.argv[3]))",
            "print(json.dumps({'root':str(source_analysis_cache_root()),'cache':closure['analysis_cache'],"
            "'identity':closure['strict_sha256']}))",
        ]), str(install), str(self.leaf), str(holdir)]

        def invoke():
            result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
            return json.loads(result.stdout)

        first = invoke()
        warm = invoke()
        self.assertEqual(first["root"], warm["root"])
        self.assertEqual(warm["cache"]["hits"], 2)
        with (runtime / "proofs/loader_scan.py").open("a") as output:
            output.write("\n# Original cache invalidation fixture.\n")
        updated = invoke()
        self.assertNotEqual(first["root"], updated["root"])
        self.assertEqual(updated["cache"]["misses"], 2)
        self.assertEqual(first["identity"], updated["identity"])


if __name__ == "__main__":
    unittest.main()
