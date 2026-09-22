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
def hx(s):
    return s.encode().hex()

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


# Interruption records an entered call, never a failed/proved theorem. A return
# (including a caught exception) clears the active call before later source work.
from hol_workbench.proof_diagnostics import (
    ACTIVITY_PREFIX, MAX_ACTIVITY_CALLS, account_proof_activity, identify_running_binding,
)
from hol_workbench.cli.orbstack_criu_vanilla_semantics import displayed_transcript

activity_prefix = f"{ACTIVITY_PREFIX}:{nonce}:"

def activity_line(call, op, *, file="/package/stack.ml", name="STUCK", line=callsite_line):
    locations = f":{hx(file)}:{hx(name)}:{line}" if op == "ENTER" else ""
    return f"{activity_prefix}{call}:{op}{locations}\n".encode()

def analyze_activity(transcript, transport="timeout"):
    return analyze_vanilla_transcript(
        claims=overflow_claims, transcript=transcript, contract=overflow_contract,
        transport=transport, response={"exit_status": 124} if transport == "timeout" else None)

entered = activity_line(1, "ENTER")
left = activity_line(1, "LEAVE")
for transport in ("timeout", "interrupted", "cancelled"):
    running = analyze_activity(entered + b"ongoing tactic output\n", transport)
    assert running["source_status"] == "not_completed" and not running["source_completed"]
    assert running["running_binding"]["status"] == "running_at_interruption"
    assert running["running_binding"]["name"] == "STUCK" and running["running_binding"]["source_line"] == 1
    assert running["running_binding"]["authority"] == "diagnostic_only"
    assert running["failing_binding"]["status"] == "unknown"
    assert running["bindings"][0]["status"] == "missing"
assert analyze_activity(entered, "completed")["running_binding"] is None
assert analyze_activity(entered + left)["running_binding"]["status"] == "unknown"
assert displayed_transcript(entered + b"visible\n" + left, overflow_contract) == "visible\n"
assert analyze_activity(entered + left + overflow_frame + b"Exception: caught\n")["running_binding"]["status"] == "unknown"

# Current imported or ambiguous calls never borrow a preceding completed name.
imported = activity_line(2, "ENTER", file="/package/imported.ml", name="HELPER")
assert analyze_activity(entered + left + imported)["running_binding"]["status"] == "unknown"
assert analyze_activity(activity_line(1, "ENTER", line=callsite_line + 5))["running_binding"]["status"] == "unknown"
active_data = account_proof_activity(entered, overflow_contract)
assert identify_running_binding(active_data, overflow_contract,
    overflow_claims + [{**overflow_claims[0], "source": "/other/stack.ml"}])["status"] == "unknown"
# Nested calls return in stack order. An unlocated inner call is unknown until
# it returns, at which point the entered outer call is again the active one.
nested = entered + imported
assert analyze_activity(nested)["running_binding"]["status"] == "unknown"
assert analyze_activity(nested + activity_line(2, "LEAVE"))["running_binding"]["name"] == "STUCK"

for malformed in (
    entered + entered, left, entered + activity_line(2, "LEAVE"),
    activity_line(2, "ENTER"), entered + activity_line(1, "BAD"),
):
    assert account_proof_activity(malformed, overflow_contract)["status"] == "malformed"
    assert analyze_activity(malformed)["running_binding"]["status"] == "unknown"
for partial in (entered.rstrip(b"\n"), entered + activity_line(2, "ENTER")[:-12],
                entered + activity_prefix[:-3].encode()):
    assert account_proof_activity(partial, overflow_contract)["status"] == "incomplete"
    assert analyze_activity(partial)["running_binding"]["status"] == "unknown"
assert account_proof_activity(entered.replace(nonce.encode(), b"b" * 32), overflow_contract)["status"] == "none"
assert account_proof_activity(entered, contract)["status"] == "unavailable"

# The bounded stream explicitly invalidates activity after capture is capped.
many_calls = b"".join(activity_line(call, "ENTER") + activity_line(call, "LEAVE")
                      for call in range(1, MAX_ACTIVITY_CALLS + 1))
truncated_activity = many_calls + activity_line(MAX_ACTIVITY_CALLS + 1, "TRUNCATED")
assert account_proof_activity(truncated_activity, overflow_contract)["status"] == "truncated"
assert analyze_activity(truncated_activity)["running_binding"]["status"] == "unknown"
assert b"original_prove (tm,observe)" in diagnostic_prelude(nonce)
print("proof activity: interruption, return, nested/imported scope and bounded framing passed")


# Imported attribution requires byte-identical captured and packaged sources.
# The imported claims stay outside entrypoint theorem verification.
import copy
import tempfile
from unittest.mock import patch
from hol_workbench.proof_diagnostics import attach_dependency_diagnostic_sources
from hol_workbench.source_dependency_closure import build_source_dependency_closure
from hol_workbench.source_dependency_package import materialize_dependency_package

with tempfile.TemporaryDirectory(prefix="hearth-imported-diagnostics-") as temporary:
    root = Path(temporary)
    project = root / "project"
    dependency = project / "nested" / "imported.ml"
    dependency.parent.mkdir(parents=True)
    dependency_bytes = (
        b"(* exact imported source *)\nlet IMPORTED = prove\n (`T`,REWRITE_TAC[]);;\n"
        b"let helper () = let LOCAL = prove (`T`,REWRITE_TAC[]) in LOCAL;;\n"
    )
    dependency.write_bytes(dependency_bytes)
    (project / "unrelated.ml").write_bytes(b"let UNRELATED = prove (`T`,REWRITE_TAC[]);;\n")
    source = project / "entry.ml"
    source.write_bytes(
        b'needs "nested/imported.ml";;\nneeds "nested/imported.ml";;\n'
        b'needs "unrelated.ml";;\nlet ENTRY = prove (`T`,REWRITE_TAC[]);;\n')
    entry_claims = extract_hol_theorems_bytes(source, source.read_bytes())
    closure = build_source_dependency_closure(source)
    package = root / "package"
    entrypoint, _ = materialize_dependency_package(source=source, closure=closure, destination=package)
    packaged_dependency = package / "nested" / "imported.ml"
    _, imported_contract = instrumented_source_bytes(
        source.read_bytes(), entry_claims, nonce=nonce, include_foundation_delta=True,
        diagnostic_source_path=str(entrypoint))
    imported_frame = activity_line(
        1, "ENTER", file=f"{package}/nested/././imported.ml", name="IMPORTED", line=2)

    def imported_result(frame=imported_frame, selected_contract=None):
        return analyze_vanilla_transcript(
            claims=entry_claims, transcript=frame,
            contract=imported_contract if selected_contract is None else selected_contract,
            transport="timeout", response={"exit_status": 124})

    assert imported_result()["running_binding"]["status"] == "unknown"
    attach_dependency_diagnostic_sources(imported_contract, closure, package)
    mapped = imported_result()
    assert mapped["running_binding"]["name"] == "IMPORTED"
    assert mapped["running_binding"]["source"] == str(dependency)
    assert mapped["running_binding"]["source_line"] == 2
    assert mapped["running_binding"]["authority"] == "diagnostic_only"
    assert mapped["source_completed"] is False and mapped["effective_exit_status"] == 124
    assert [(row["name"], row["status"]) for row in mapped["bindings"]] == [("ENTRY", "missing")]
    descriptor = imported_contract["proof_diagnostics"]
    assert descriptor["dependency_sources_status"] == "recorded"
    assert descriptor["dependency_sources"][0]["source_line_offset"] == 0
    assert [row["name"] for row in descriptor["dependency_sources"][0]["claims"]] == ["IMPORTED"]

    # Completed calls never trigger imported theorem scanning. Malformed,
    # foreign and incomplete frames cannot request an imported attribution.
    for frame in (
        b"", imported_frame + activity_line(1, "LEAVE"),
        imported_frame + imported_frame, imported_frame.rstrip(b"\n"),
        imported_frame.replace(nonce.encode(), b"b" * 32),
        activity_line(1, "ENTER", file="nested/imported.ml", name="IMPORTED", line=2),
        activity_line(1, "ENTER", file=str(root / "other.ml"), name="IMPORTED", line=2),
    ):
        on_demand = copy.deepcopy(imported_contract)
        with patch("hol_workbench.proofs.theorem_scan.extract_hol_theorems_bytes") as scanner:
            attach_dependency_diagnostic_sources(on_demand, closure, package, transcript=frame)
            scanner.assert_not_called()
        assert on_demand["proof_diagnostics"]["dependency_sources"] == []
        assert imported_result(frame, on_demand)["running_binding"]["status"] == "unknown"

    # Normalize compiler paths and scan only the requested imported file, once
    # even when the source contains duplicate needs edges or active locations.
    on_demand = copy.deepcopy(imported_contract)
    nested_imported = imported_frame + activity_line(
        2, "ENTER", file=str(packaged_dependency), name="IMPORTED", line=2)
    with patch("hol_workbench.proofs.theorem_scan.extract_hol_theorems_bytes",
               wraps=extract_hol_theorems_bytes) as scanner:
        attach_dependency_diagnostic_sources(on_demand, closure, package, transcript=nested_imported)
        assert scanner.call_count == 1
        assert scanner.call_args.args == (dependency, dependency_bytes)
    assert len(on_demand["proof_diagnostics"]["dependency_sources"]) == 1
    assert imported_result(nested_imported, on_demand)["running_binding"]["name"] == "IMPORTED"
    assert [(row["name"], row["status"]) for row in imported_result(nested_imported, on_demand)["bindings"]] == [("ENTRY", "missing")]

    # Complete bounded failure diagnostics request only their exact source.
    failure_frame = overflow_frame.replace(hx("/package/stack.ml").encode(), hx(str(packaged_dependency)).encode())
    failure_frame = failure_frame.replace(hx("STUCK").encode(), hx("IMPORTED").encode())
    on_demand = copy.deepcopy(imported_contract)
    with patch("hol_workbench.proofs.theorem_scan.extract_hol_theorems_bytes",
               wraps=extract_hol_theorems_bytes) as scanner:
        attach_dependency_diagnostic_sources(on_demand, closure, package, transcript=failure_frame)
        assert scanner.call_count == 1
    assert len(on_demand["proof_diagnostics"]["dependency_sources"]) == 1
    with patch("hol_workbench.proofs.theorem_scan.extract_hol_theorems_bytes") as scanner:
        attach_dependency_diagnostic_sources(on_demand, closure, package, transcript=failure_frame + failure_frame)
        scanner.assert_not_called()
    assert on_demand["proof_diagnostics"]["dependency_sources"] == []
    for frame in (
        activity_line(1, "ENTER", file=str(packaged_dependency), name="LOCAL", line=4),
        activity_line(1, "ENTER", file=str(packaged_dependency), name="IMPORTED", line=4),
        activity_line(1, "ENTER", file=str(root / "other" / "imported.ml"), name="IMPORTED", line=2),
        imported_frame.rstrip(b"\n"),
    ):
        assert imported_result(frame)["running_binding"]["status"] == "unknown"

    ambiguous = copy.deepcopy(imported_contract)
    extra = copy.deepcopy(descriptor["dependency_sources"][0])
    extra["claims"][0]["source"] = str(root / "other" / "imported.ml")
    ambiguous["proof_diagnostics"]["dependency_sources"].append(extra)
    assert imported_result(selected_contract=ambiguous)["running_binding"]["status"] == "unknown"
    malformed = copy.deepcopy(imported_contract)
    malformed["proof_diagnostics"]["dependency_sources"][0]["source_line_offset"] = False
    assert imported_result(selected_contract=malformed)["running_binding"]["status"] == "unknown"

    dependency.write_bytes(dependency_bytes + b"(* source edit *)\n")
    attach_dependency_diagnostic_sources(imported_contract, closure, package)
    assert imported_result()["running_binding"]["status"] == "unknown"
    assert descriptor["dependency_sources_status"] == "partial"
    dependency.write_bytes(dependency_bytes)
    packaged_dependency.chmod(0o644)
    packaged_dependency.write_bytes(dependency_bytes + b"(* package edit *)\n")
    attach_dependency_diagnostic_sources(imported_contract, closure, package)
    assert imported_result()["running_binding"]["status"] == "unknown"
    packaged_dependency.unlink()
    packaged_dependency.symlink_to(dependency)
    attach_dependency_diagnostic_sources(imported_contract, closure, package)
    assert imported_result()["running_binding"]["status"] == "unknown"
    packaged_dependency.unlink()
    packaged_dependency.write_bytes(dependency_bytes)
    invalid_closure = copy.deepcopy(closure)
    invalid_closure["strict_sha256"] = "0" * 64
    attach_dependency_diagnostic_sources(imported_contract, invalid_closure, package)
    assert imported_result()["running_binding"]["status"] == "unknown"
    assert descriptor["dependency_sources_status"] == "invalid_closure"
    attach_dependency_diagnostic_sources(imported_contract, closure, package)
    assert imported_result()["running_binding"]["name"] == "IMPORTED"
print("proof activity: exact imported source, normalized frames, edits and ambiguity boundaries passed")
