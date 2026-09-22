#!/usr/bin/env python3
"""Exercise the emitted diagnostic wrapper in plain OCaml, without HOL."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))

from hol_workbench.proof_diagnostics import (  # noqa: E402
    ACTIVITY_PROTOCOL, MAX_ACTIVITY_CALLS, MAX_ACTIVITY_DEPTH,
    account_proof_activity, diagnostic_prelude,
)


def main() -> int:
    compiler = os.environ.get("HOL_WORKBENCH_TEST_OCAMLC") or shutil.which("ocamlc")
    if compiler is None:
        print("proof activity emitter: skipped (optional OCaml compiler unavailable)")
        return 0
    version = subprocess.run([compiler, "-version"], check=True, capture_output=True,
                             text=True, timeout=10).stdout.strip()
    if tuple(int(part) for part in version.split(".")[:2]) < (4, 11):
        print("proof activity emitter: skipped (optional OCaml 4.11+ compiler unavailable)")
        return 0
    nonce = "d" * 32
    contract = {"nonce": nonce, "proof_diagnostics": {
        "nonce": nonce, "activity_protocol": ACTIVITY_PROTOCOL}}
    # These string-valued fixtures exercise only observation/exception behavior;
    # they are not a HOL implementation or theorem evidence.
    fixture = b'''
module Clflags = struct let debug = ref false end;;
type goal = (string * string) list * string;;
type tactic = goal -> unit * goal list * unit;;
let concl value = value;;
let pp_print_term = Format.pp_print_string;;
let prove ((statement,tactic):string * tactic) =
  let (_,goals,_) = tactic ([],statement) in
  if goals = [] then statement else failwith "unsolved fixture";;
'''
    driver = f'''
let solve _ = ((),[],());;
let completed = {MAX_ACTIVITY_CALLS + 17};;
for index = 1 to completed do
  assert (prove ("fixture",solve) = "fixture")
done;;
let rec nested depth goal =
  if depth = 0 then solve goal else
  (ignore (prove ("nested",nested (depth - 1))); solve goal);;
ignore (prove ("outer",nested {MAX_ACTIVITY_DEPTH + 3}));;
assert (try ignore (prove ("caught",fun _ -> failwith "expected fixture")); false
        with Failure message -> message = "expected fixture");;
assert (prove ("repaired",solve) = "repaired");;
if Array.length Sys.argv > 1 then
  ignore (prove ("still active",fun _ -> exit 0));;
'''.encode()
    with tempfile.TemporaryDirectory(prefix="hearth-activity-emitter-") as temporary:
        directory = Path(temporary)
        source, binary = directory / "activity.ml", directory / "activity"
        source.write_bytes(fixture + diagnostic_prelude(nonce) + driver)
        compiled = subprocess.run([compiler, "-g", "-o", str(binary), str(source)],
                                  cwd=directory, capture_output=True, timeout=30)
        if compiled.returncode:
            sys.stderr.buffer.write(compiled.stderr)
            raise AssertionError("plain OCaml activity wrapper did not compile")
        for stopped in (False, True):
            executed = subprocess.run([str(binary), *(["stop"] if stopped else [])],
                                      cwd=directory, capture_output=True, timeout=30)
            assert executed.returncode == 0, executed.stderr
            activity = account_proof_activity(executed.stdout, contract)
            assert activity["status"] == "recorded", activity
            assert activity["entered_call_count"] > MAX_ACTIVITY_CALLS
            assert activity["overflow_span_count"] == 1
            assert activity["suppressed_call_count"] == 4
            assert activity["completed_call_history_retained"] == 0
            assert len(activity["active_calls"]) == int(stopped)
            if stopped:
                assert activity["active_calls"][0]["locations"]
    print("proof activity emitter: real OCaml long-call, overflow/recovery and exception checks passed (no HOL)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
