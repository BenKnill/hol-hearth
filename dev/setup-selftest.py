#!/usr/bin/env python3
"""Regression checks for fresh setup and sudo portability; never starts HOL."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))
from hol_workbench.cli import orbstack_criu_restore as restore
from hol_workbench.cli import prove_doctor
from hol_workbench.cli.hol_syntax_preflight import syntax_preflight_environment
from hol_workbench import criu_maintenance_preflight as maintenance
from hol_workbench.criu_shelf_result_evidence import publish_dump_artifacts_for_controller
from hol_workbench.runtime_config import write_runtime_config

spec = importlib.util.spec_from_file_location("hearth_setup", ROOT / "dev/setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupRegression(unittest.TestCase):
    def test_passwordless_sudo_without_validate_permission(self):
        command = ["sudo", "-n", "/usr/bin/env", "-u", "CRIU_CONFIG_FILE", "/usr/sbin/criu", "--version"]
        def sudo_policy(argv, **kwargs):
            if argv == command:
                return subprocess.CompletedProcess(argv, 0, "Version: 4.1.1\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "sudo: a password is required")
        with patch.object(maintenance, "criu_command", return_value=command), \
             patch.object(maintenance.subprocess, "run", side_effect=sudo_policy):
            maintenance._require_sudo_timestamp()

    def test_root_owned_pidfile_probe_on_python_313(self):
        class ProtectedPath:
            def is_file(self):
                raise PermissionError("root-owned restore output directory")
            def __str__(self):
                return "/proc/controller/fd/7/restore.pid"
        for returncode, expected in ((0, True), (1, False)):
            with patch.object(restore.subprocess, "run", return_value=subprocess.CompletedProcess([], returncode)):
                self.assertEqual(restore._pidfile_is_file(ProtectedPath()), expected)

    def test_dump_artifacts_restrict_permissions_and_refuse_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            images = root / "images"
            images.mkdir()
            artifact = images / "pages.img"
            artifact.write_bytes(b"synthetic fixture")
            artifact.chmod(0o644)
            publish_dump_artifacts_for_controller(images)
            self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
            outside = root / "outside"
            outside.write_text("preserve me")
            outside.chmod(0o644)
            (images / "escape.img").symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "symlink"):
                publish_dump_artifacts_for_controller(images)
            self.assertEqual(outside.stat().st_mode & 0o777, 0o644)

    def test_existing_config_is_preserved_before_prerequisites(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "runtime.toml"
            env = {"HOL_WORKBENCH_RUNTIME_CONFIG": str(config)}
            write_runtime_config(hol_light_dir=root / "existing-hol", criu_shelf_root=root / "existing-shelves",
                                 criu_bin=Path("/usr/sbin/criu"), environment=env)
            before = config.read_bytes()
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(sys, "argv", ["hearth setup", "--data-dir", str(root / "new")]), \
                 patch.object(setup, "prerequisites", side_effect=AssertionError("must not run")):
                with self.assertRaisesRegex(setup.SetupError, "Existing configuration preserved"):
                    setup.main()
            self.assertEqual(config.read_bytes(), before)
            self.assertFalse((root / "new").exists())

    def test_check_does_not_create_runtime_or_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, config = root / "data", root / "config.toml"
            with patch.dict(os.environ, {"HOL_WORKBENCH_RUNTIME_CONFIG": str(config)}, clear=True), \
                 patch.object(sys, "argv", ["hearth setup", "--data-dir", str(data), "--check"]), \
                 patch.object(setup, "prerequisites", return_value={"ocaml": "test"}), \
                 patch.object(setup, "committed_source_identity", return_value={"revision": "fixture"}), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(setup.main(), 0)
            self.assertFalse(data.exists())
            self.assertFalse(config.exists())

    def test_doctor_defaults_to_light(self):
        self.assertEqual(prove_doctor._parse([]).profile, "light")
        self.assertTrue(prove_doctor._parse(["--all-profiles"]).all_profiles)

    def test_distribution_parser_does_not_require_opam(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = syntax_preflight_environment(Path(temporary), environ={
                "PATH": "/other/toolchain/bin", "OPAM_SWITCH_PREFIX": "/other/toolchain",
                "CAML_LD_LIBRARY_PATH": "/other/toolchain/stublibs",
                "OCAML_TOPLEVEL_PATH": "/other/toolchain/toplevel",
            })
            self.assertNotIn("_opam", env["PATH"])
            self.assertNotIn("/other/toolchain", env["PATH"])
            for name in ("OPAM_SWITCH_PREFIX", "CAML_LD_LIBRARY_PATH", "OCAML_TOPLEVEL_PATH"):
                self.assertNotIn(name, env)


if __name__ == "__main__":
    unittest.main()
