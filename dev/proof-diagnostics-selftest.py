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


# OCaml reports Stack_overflow without an "Exception:" prefix. Recognition
# must enable exact callsite diagnostics without treating printed text as proof.
from hol_workbench.proofs.theorem_scan import extract_hol_theorems_bytes
from hol_workbench.vanilla_claims import first_error

overflow_message = "Stack overflow during evaluation (looping recursion?)."
overflow_source = b"let STUCK = prove (`T`, fun _ -> raise Stack_overflow);;\n"
overflow_claims = extract_hol_theorems_bytes(Path("/project/stack.ml"), overflow_source)
_, overflow_contract = instrumented_source_bytes(
    overflow_source, overflow_claims, nonce=nonce, include_foundation_delta=True,
    diagnostic_source_path="/package/stack.ml")
callsite_line = overflow_contract["proof_diagnostics"]["source_line_offset"] + 1
overflow_records = [
    f"1:BEGIN:tactic_input:1:1:{hx('T')}:{hx('Stack overflow')}",
    f"1:GOAL:0:0:{hx('T')}",
    f"1:LOCATION:{hx('/package/stack.ml')}:{hx('STUCK')}:{callsite_line}",
    "1:END",
]
overflow_frame = ("\n".join(prefix + line for line in overflow_records) + "\n").encode()

def analyze_overflow(transcript):
    return analyze_vanilla_transcript(
        claims=overflow_claims, transcript=transcript, contract=overflow_contract,
        transport="completed", response={"exit_status": 0})

assert first_error("noise\n# " + overflow_message + "\n") == (2, overflow_message)
assert first_error("note: " + overflow_message + "\n") == (None, None)
uncaught = analyze_overflow(overflow_frame + (overflow_message + "\n").encode())
assert uncaught["source_status"] == "failed" and uncaught["effective_exit_status"] == 1
assert uncaught["first_failure"] == overflow_message and uncaught["first_failure_transcript_line"] == 5
assert uncaught["failing_binding"]["name"] == "STUCK"
assert uncaught["failing_binding"]["source_line"] == 1
assert uncaught["bindings"][0]["status"] == "missing"
assert uncaught["claim_accounting"][0]["first_error_line"] is None  # no per-claim guess
view = StringIO()
with redirect_stdout(view):
    print_proof_diagnostics(uncaught, verbose=False)
assert "original tactic input" in view.getvalue()
assert "earlier/caught proof context" not in view.getvalue()

# A caught diagnostic cannot be attributed to a later, unrelated overflow.
later = analyze_overflow(overflow_frame + ("continued after caught failure\n" + overflow_message + "\n").encode())
assert later["first_failure_transcript_line"] == 6
assert later["failing_binding"]["status"] == "unknown"
view = StringIO()
with redirect_stdout(view):
    print_proof_diagnostics(later, verbose=False)
assert "earlier/caught proof context" in view.getvalue()

# Completion and verified probes still override merely printed error text.
markers = "\n".join([overflow_contract["claims"][0]["ok_marker"], overflow_contract["completion_marker"]]) + "\n"
caught = analyze_overflow(overflow_frame + (overflow_message + "\n" + markers).encode())
assert caught["source_status"] == "succeeded" and caught["bindings"][0]["status"] == "proved"
assert caught["first_failure"] is None and caught["failing_binding"] is None
view = StringIO()
with redirect_stdout(view):
    print_proof_diagnostics(caught, verbose=False)
assert "caught proof failures; the source continued to completion" in view.getvalue()
print("proof-diagnostics overflow: exact callsite, unrelated failure and completed-source boundaries passed")
