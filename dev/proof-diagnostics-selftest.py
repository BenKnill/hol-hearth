#!/usr/bin/env python3
"""Framing and evidence-boundary regressions; no HOL runtime required."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))

from hol_workbench.proof_diagnostics import account_proof_diagnostics, diagnostic_prelude
from hol_workbench.cli.orbstack_criu_vanilla_semantics import instrumented_source_bytes, analyze_vanilla_transcript

nonce = "a" * 32
contract = {"nonce": nonce, "proof_diagnostics": {"nonce": nonce}}
prefix = "__HOL_PROOF_DIAGNOSTIC__:" + nonce + ":"
hx = lambda s: s.encode().hex()
lines = [
    f"1:BEGIN:residual_goals:1:1:{hx('x = x + &1')}:{hx('Failure(unsolved)')}",
    f"1:GOAL:0:1:{hx('x = x + &1')}",
    f"1:ASSUMPTION:0:{hx('H')}:{hx('x = &0')}",
    "1:END",
]
raw = ("\n".join(prefix + line for line in lines) + "\n").encode()
got = account_proof_diagnostics(raw, contract)
assert got["status"] == "recorded" and got["authority"] == "diagnostic_only"
goal = got["events"][0]["goals"][0]
assert goal["conclusion"] == "x = x + &1"
assert goal["assumptions"] == [{"label": "H", "conclusion": "x = &0"}]
assert account_proof_diagnostics(raw.replace(b"a" * 32, b"b" * 32), contract)["status"] == "none"
assert account_proof_diagnostics(raw + raw, contract)["status"] == "malformed"
assert account_proof_diagnostics(raw.rsplit(prefix.encode(), 1)[0], contract)["status"] == "incomplete"
assert account_proof_diagnostics(raw.replace(b"GOAL:0:1", b"GOAL:3:1"), contract)["status"] == "malformed"
assert account_proof_diagnostics(raw.replace(b"residual_goals:1:1", b"residual_goals:9:9"), contract)["status"] == "malformed"
assert account_proof_diagnostics(raw.replace(hx("x = &0").encode(), b"00" * 10000), contract)["status"] == "malformed"
source = b"let VALUE = 42;;\r\n"
payload, probe = instrumented_source_bytes(source, [], nonce=nonce, include_foundation_delta=True)
assert source in payload and diagnostic_prelude(nonce) in payload
semantic = analyze_vanilla_transcript(claims=[], transcript=raw, contract=probe, transport="completed", response={"exit_status": 0})
assert semantic["source_completed"] is False and semantic["effective_exit_status"] != 0
assert semantic["proof_diagnostics"]["status"] == "recorded"
caught = raw + probe["completion_marker"].encode() + b"\n"
semantic = analyze_vanilla_transcript(claims=[], transcript=caught, contract=probe, transport="completed", response={"exit_status": 0})
assert semantic["source_completed"] is True and semantic["effective_exit_status"] == 0
assert semantic["proof_diagnostics"]["status"] == "recorded"
print("proof-diagnostics self-test: bounded frames and unchanged acceptance passed")

from hol_workbench.proof_diagnostics import identify_failed_binding

claim = {"name": "TARGET", "source": "/project/proof.ml", "source_line": 10, "statement_line": 11}
location_contract = {"proof_diagnostics": {"packaged_entrypoint": "/package/proof.ml", "source_line_offset": 80}}
event = {"end_transcript_line": 24, "locations": [{"file": "/package/proof.ml", "name": "TARGET", "line": 90}]}
diagnostic = {"events": [event]}
attribution = identify_failed_binding(diagnostic, location_contract, [claim], 25)
assert attribution["name"] == "TARGET" and attribution["source_line"] == 10
assert identify_failed_binding(diagnostic, location_contract, [claim], 27) is None  # caught earlier
assert identify_failed_binding(diagnostic, location_contract, [{**claim, "name": "OTHER"}], 25) is None
assert identify_failed_binding(diagnostic, {"proof_diagnostics": {**location_contract["proof_diagnostics"], "packaged_entrypoint": "/other/proof.ml"}}, [claim], 25) is None
assert identify_failed_binding(diagnostic, location_contract, [{**claim, "source_line": 11}], 25) is None
assert identify_failed_binding(diagnostic, location_contract, [claim, {**claim, "source": "/other/proof.ml"}], 25) is None
print("proof-diagnostics attribution: exact callsite and caught-failure boundaries passed")

from contextlib import redirect_stdout
from io import StringIO
from hol_workbench.proof_diagnostics import print_proof_diagnostics
view = StringIO()
with redirect_stdout(view):
    print_proof_diagnostics({"proof_diagnostics": got, "source_completed": False,
                             "first_failure_transcript_line": 99}, verbose=False)
assert "earlier/caught proof context; not attributed" in view.getvalue()

# Suppression after the event cap must not make an earlier inner failure current.
capped = b"".join(raw.replace((prefix + "1:").encode(), (prefix + str(i) + ":").encode()) for i in range(1, 9))
capped += (prefix + "9:TRUNCATED\n").encode()
cap_data = account_proof_diagnostics(capped, contract)
assert cap_data["status"] == "recorded" and cap_data["capture_truncated"]
assert len(cap_data["events"]) == 8
assert identify_failed_binding({**diagnostic, "capture_truncated": True}, location_contract, [claim], 25) is None
assert account_proof_diagnostics(capped + (prefix + "9:TRUNCATED\n").encode(), contract)["status"] == "malformed"
# Compact display explicitly calls out retained goals outside its visible slice.
many = {"status": "recorded", "events": [{"event": 1, "kind": "residual_goals", "shown_goal_count": 4,
        "goal_count": 4, "end_transcript_line": 10, "goals": [goal] * 4}]}
view = StringIO()
with redirect_stdout(view):
    print_proof_diagnostics({"proof_diagnostics": many, "source_completed": False,
                            "first_failure_transcript_line": 11}, verbose=False)
assert "1 more recorded goals; use --verbose" in view.getvalue()
print("proof-diagnostics display: caught, capped and hidden-goal boundaries passed")
