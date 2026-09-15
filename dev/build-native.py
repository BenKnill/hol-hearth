#!/usr/bin/env python3
"""Compile the live-loop evaluator directly with OCaml; no Dune or OPAM required."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ocamlc", help="Compiler matching the HOL profile")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Linux is required")
    compiler = args.ocamlc or os.environ.get("HOL_HEARTH_OCAMLC") or shutil.which("ocamlc")
    if not compiler:
        parser.error("Pass --ocamlc /absolute/path/to/the/HOL/compiler")
    compiler = str(Path(compiler).resolve())
    if not Path(compiler).is_file() or not os.access(compiler, os.X_OK):
        parser.error(f"Compiler is not executable: {compiler}")
    version = subprocess.check_output([compiler, "-version"], text=True).strip()
    parent = ROOT / "_build/default/hol-workbench"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / "native-eval"
    with tempfile.TemporaryDirectory(prefix="native-eval-", dir=parent) as temporary:
        stage = Path(temporary)
        objects = stage / ".workbench_hot_session.objs/byte"
        objects.mkdir(parents=True)
        hashes = {}
        for name in ("eval.ml", "hot_session.ml", "probe.ml"):
            source = ROOT / "hol-workbench/native-eval" / name
            shutil.copyfile(source, stage / name)
            hashes[name] = hashlib.sha256(source.read_bytes()).hexdigest()
        for name in ("eval", "hot_session", "probe"):
            subprocess.run([compiler, "-I", "+compiler-libs", "-I", "+unix", "-I", str(objects),
                            "-c", "-o", str(objects / f"{name}.cmo"), str(stage / f"{name}.ml")],
                           check=True)
        subprocess.run([compiler, "-a", "-o", str(stage / "workbench_hot_session.cma"),
                        str(objects / "eval.cmo"), str(objects / "hot_session.cmo")], check=True)
        subprocess.run([compiler, "-I", "+compiler-libs", "-I", "+unix", "-I", str(objects), "-linkall",
                        "-o", str(stage / "probe"),
                        "unix.cma", "ocamlcommon.cma", "ocamlbytecomp.cma", "ocamltoplevel.cma",
                        str(stage / "workbench_hot_session.cma"), str(objects / "probe.cmo")], check=True)
        subprocess.run([sys.executable, "-I", "-B", str(ROOT / "hol-workbench/dev/native_eval_selftest.py"),
                        str(stage / "probe")], check=True)
        sys.path.insert(0, str(ROOT / "hol-workbench"))
        from hol_workbench.native_signals import ocaml_signal_configuration

        signal_probe = stage / "signal_probe.ml"
        signal_probe.write_text(ocaml_signal_configuration(version) + "\n" + '''
let () =
  let child = Unix.fork () in
  if child = 0 then (Unix.kill (Unix.getpid ()) Sys.sigterm; exit 99)
  else
    let _, status = Unix.waitpid [] child in
    let result = Hot_session.child_status status in
    Printf.printf "%s:%d\\n" (Hot_session.child_status_payload result)
      (Hot_session.child_status_code result)
''')
        subprocess.run([compiler, "-I", "+compiler-libs", "-I", "+unix", "-I", str(objects), "-linkall",
                        "-o", str(stage / "signal-probe"), "unix.cma", "ocamlcommon.cma",
                        "ocamlbytecomp.cma", "ocamltoplevel.cma",
                        str(stage / "workbench_hot_session.cma"), str(signal_probe)], check=True)
        observed = subprocess.check_output([str(stage / "signal-probe")], text=True, timeout=10).strip()
        if observed != "signaled:15:SIGTERM:143":
            raise SystemExit(f"Unexpected terminated-child status: {observed}")
        print("Native child termination: SIGTERM / 15 / exit 143 verified")
        (stage / "build.json").write_text(json.dumps({"ocaml": version, "sources": hashes}, indent=2) + "\n")
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(stage, target)
    print(f"Evaluator ready: OCaml {version}; no HOL session was loaded")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
