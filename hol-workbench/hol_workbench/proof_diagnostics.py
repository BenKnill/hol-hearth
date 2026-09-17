"""Bounded, nonce-bound authoring diagnostics; never theorem evidence."""
from __future__ import annotations

import re
from typing import Any

PREFIX = "__HOL_PROOF_DIAGNOSTIC__"
NONCE = re.compile(r"^[0-9a-f]{32}$")
MAX_EVENTS = 8
MAX_GOALS = 8
MAX_ASSUMPTIONS = 16
MAX_TEXT_BYTES = 2048


def diagnostic_prelude(nonce: str) -> bytes:
    """Observe the original prove's tactic result without replacing its checks."""
    if not NONCE.fullmatch(nonce):
        raise ValueError("invalid proof diagnostic nonce")
    # All helpers are local to the new prove closure. The original source remains
    # byte-for-byte present; only this disposable child's later prove calls see it.
    source = r'''
Clflags.debug := true;;
let prove =
  let original_prove = prove in
  let original_print = Printf.printf in
  let original_concl = concl in
  let original_term_printer = pp_print_term in
  let events = ref 0 in
  let bound = 2048 in
  let bounded s = if String.length s <= bound then s else
    String.sub s 0 bound ^ "... [truncated]" in
  let hex s =
    let b = Buffer.create (2 * String.length s) in
    String.iter (fun c -> Buffer.add_string b (Printf.sprintf "%02x" (Char.code c))) s;
    Buffer.contents b in
  let render tm =
    let b = Buffer.create 256 in
    let output s pos count =
      let remaining = bound - Buffer.length b in
      if remaining > 0 then Buffer.add_substring b s pos (min remaining count) in
    let fmt = Format.make_formatter output (fun () -> ()) in
    Format.pp_set_max_boxes fmt 32;
    original_term_printer fmt tm; Format.pp_print_flush fmt ();
    let s = Buffer.contents b in
    if String.length s = bound then s ^ "... [truncated]" else s in
  let rec take n xs = match xs with
    [] -> [] | _ when n = 0 -> [] | x::rest -> x::take (n-1) rest in
  let emit kind tm error goals locations =
    if !events < 8 then
      (incr events;
       let event = !events in
       let chosen = take 8 goals in
       original_print "\n__HOL_PROOF_DIAGNOSTIC__:@NONCE@:%d:BEGIN:%s:%d:%d:%s:%s\n"
         event kind (List.length goals) (List.length chosen)
         (hex (render tm)) (hex (bounded (Printexc.to_string error)));
       List.iteri (fun index (assumptions, conclusion) ->
         original_print "__HOL_PROOF_DIAGNOSTIC__:@NONCE@:%d:GOAL:%d:%d:%s\n"
           event index (List.length assumptions) (hex (render conclusion));
         List.iter (fun (label,th) ->
           original_print "__HOL_PROOF_DIAGNOSTIC__:@NONCE@:%d:ASSUMPTION:%d:%s:%s\n"
             event index (hex (bounded label)) (hex (render (original_concl th))))
           (take 16 assumptions)) chosen;
       List.iter (fun slot -> match Printexc.Slot.location slot with
         None -> () | Some loc ->
           let name = match Printexc.Slot.name slot with None -> "" | Some n -> n in
           original_print "__HOL_PROOF_DIAGNOSTIC__:@NONCE@:%d:LOCATION:%s:%s:%d\n"
             event (hex (bounded loc.Printexc.filename)) (hex (bounded name)) loc.Printexc.line_number)
         locations;
       original_print "__HOL_PROOF_DIAGNOSTIC__:@NONCE@:%d:END\n%!" event)
    else if !events = 8 then
      (incr events; original_print "__HOL_PROOF_DIAGNOSTIC__:@NONCE@:9:TRUNCATED\n%!") in
  fun (tm,tac) ->
    let locations = try match Printexc.backtrace_slots (Printexc.get_callstack 32) with
      None -> [] | Some slots -> Array.to_list slots with _ -> [] in
    let returned_goals = ref None in
    let observe goal =
      let ((_,goals,_) as result) = tac goal in
      returned_goals := Some goals;
      result in
    try original_prove (tm,observe) with error ->
      (try
         (match !returned_goals with
            Some (_::_ as goals) -> emit "residual_goals" tm error goals locations
          | _ -> emit "tactic_input" tm error [([],tm)] locations)
       with _ -> ());
      raise error;;
'''
    return source.replace("@NONCE@", nonce).encode("utf-8")


def _text(value: str) -> str:
    if len(value) > (MAX_TEXT_BYTES + 32) * 2:
        raise ValueError("oversized diagnostic text")
    return bytes.fromhex(value).decode("utf-8", errors="replace")


def account_proof_diagnostics(transcript: bytes, contract: dict[str, Any]) -> dict[str, Any]:
    """Accept only complete, bounded frames; diagnostic text cannot prove claims."""
    descriptor = contract.get("proof_diagnostics")
    empty: dict[str, Any] = {
        "schema": "hol-hearth.proof-diagnostics.v1",
        "status": "unavailable" if not isinstance(descriptor, dict) else "none",
        "events": [],
        "authority": "diagnostic_only",
        "boundary": "Returned tactic goals or original tactic input; never a kernel theorem probe. "
                    "Exceptions inside a tactic do not expose intermediate residual goals.",
    }
    if not isinstance(descriptor, dict):
        return empty
    nonce = str(contract.get("nonce") or "")
    if not NONCE.fullmatch(nonce) or descriptor.get("nonce") != nonce:
        return {**empty, "status": "invalid_contract"}
    prefix = f"{PREFIX}:{nonce}:".encode()
    lines = [(number, line) for number, line in enumerate(transcript.splitlines(), 1) if line.startswith(prefix)]
    if not lines:
        return empty
    events: list[dict[str, Any]] = []
    truncated = False
    active: dict[str, Any] | None = None
    try:
        # Bound framing as well as goal text, including adversarial source output.
        if len(lines) > MAX_EVENTS * (34 + MAX_GOALS * (1 + MAX_ASSUMPTIONS)) + 1:
            raise ValueError("too many diagnostic records")
        for lineno, raw in lines:
            parts = raw[len(prefix):].decode("ascii").split(":")
            ordinal = int(parts[0])
            op = parts[1]
            if truncated:
                raise ValueError("record after truncation")
            if op == "TRUNCATED":
                if len(parts) != 2 or active is not None or ordinal != MAX_EVENTS + 1 or len(events) != MAX_EVENTS:
                    raise ValueError("invalid truncation marker")
                truncated = True
            elif op == "BEGIN":
                if active is not None or ordinal != len(events) + 1 or ordinal > MAX_EVENTS or len(parts) != 7:
                    raise ValueError("ambiguous event")
                if parts[2] not in {"residual_goals", "tactic_input"}:
                    raise ValueError("unknown goal kind")
                total, count = int(parts[3]), int(parts[4])
                if not 0 <= count <= MAX_GOALS or total < count:
                    raise ValueError("invalid goal count")
                active = {"event": ordinal, "kind": parts[2], "goal_count": total,
                          "shown_goal_count": count, "statement": _text(parts[5]),
                          "exception": _text(parts[6]), "goals": [], "locations": []}
            elif active is None or active["event"] != ordinal:
                raise ValueError("record outside event")
            elif op == "GOAL":
                if len(parts) != 5 or int(parts[2]) != len(active["goals"]):
                    raise ValueError("invalid goal index")
                if len(active["goals"]) >= active["shown_goal_count"] or int(parts[3]) < 0:
                    raise ValueError("invalid goal count")
                active["goals"].append({"conclusion": _text(parts[4]), "assumption_count": int(parts[3]),
                                         "assumptions": []})
            elif op == "ASSUMPTION":
                if len(parts) != 5 or int(parts[2]) != len(active["goals"]) - 1 or not active["goals"]:
                    raise ValueError("invalid assumption index")
                goal = active["goals"][-1]
                if len(goal["assumptions"]) >= min(MAX_ASSUMPTIONS, goal["assumption_count"]):
                    raise ValueError("too many assumptions")
                goal["assumptions"].append({"label": _text(parts[3]), "conclusion": _text(parts[4])})
            elif op == "LOCATION":
                if len(parts) != 5 or len(active["locations"]) >= 32 or int(parts[4]) < 1:
                    raise ValueError("invalid location")
                active["locations"].append({"file": _text(parts[2]), "name": _text(parts[3]), "line": int(parts[4])})
            elif op == "END":
                if len(parts) != 2 or len(active["goals"]) != active["shown_goal_count"]:
                    raise ValueError("incomplete event")
                if any(len(g["assumptions"]) != min(MAX_ASSUMPTIONS, g["assumption_count"]) for g in active["goals"]):
                    raise ValueError("incomplete assumptions")
                active["end_transcript_line"] = lineno
                events.append(active)
                active = None
            else:
                raise ValueError("unknown record")
        if active is not None:
            return {**empty, "status": "incomplete"}
    except (ValueError, UnicodeError, IndexError):
        return {**empty, "status": "malformed"}
    return {**empty, "status": "recorded", "events": events, "capture_truncated": truncated}


def print_proof_diagnostics(receipt: dict[str, Any], *, verbose: bool) -> None:
    data = receipt.get("proof_diagnostics") or (receipt.get("transcript_accounting") or {}).get("proof_diagnostics")
    if not isinstance(data, dict) or data.get("status") != "recorded":
        return
    events = data.get("events") or []
    print(f"proof_diagnostics: {len(events)} event(s), diagnostic only")
    if data.get("capture_truncated"):
        print("  capture limit reached; later proof failures are unavailable")
    if receipt.get("source_completed"):
        print("  caught proof failures; the source continued to completion")
    for event in events if verbose else events[-1:]:
        failure_line = receipt.get("first_failure_transcript_line")
        current = (not data.get("capture_truncated") and not receipt.get("source_completed") and type(failure_line) is int
                   and event.get("end_transcript_line") == failure_line - 1)
        if not current:
            print("  earlier/caught proof context; not attributed to the current source failure")
        kind = event["kind"]
        label = "residual goals" if kind == "residual_goals" else "original tactic input (intermediate goals unavailable)"
        print(f"  {label}: {event['shown_goal_count']}/{event['goal_count']}")
        goals = event["goals"] if verbose else event["goals"][:3]
        for goal in goals:
            assumptions = goal["assumptions"] if verbose else goal["assumptions"][:4]
            for assumption in assumptions:
                print(f"    assumption {assumption['label']}: {assumption['conclusion']}")
            if goal["assumption_count"] > len(assumptions):
                print(f"    ... {goal['assumption_count'] - len(assumptions)} more assumptions")
            print(f"    |- {goal['conclusion']}")
        if len(event["goals"]) > len(goals):
            print(f"    ... {len(event['goals']) - len(goals)} more recorded goals; use --verbose")


def identify_failed_binding(
    diagnostics: dict[str, Any], contract: dict[str, Any],
    claims: list[dict[str, Any]], failure_line: int | None,
) -> dict[str, Any] | None:
    """Map a compiler-reported direct call site, never a preceding printed val."""
    descriptor = contract.get("proof_diagnostics") or {}
    entry = descriptor.get("packaged_entrypoint")
    offset = descriptor.get("source_line_offset")
    events = diagnostics.get("events") or []
    if diagnostics.get("capture_truncated") or not entry or type(offset) is not int or not events:
        return None
    event = events[-1]
    # A caught failure followed by another error is not attribution. The
    # diagnostic must be immediately followed by HOL's uncaught exception.
    if type(failure_line) is not int or event.get("end_transcript_line") != failure_line - 1:
        return None
    candidates = []
    for loc in event.get("locations") or []:
        if loc.get("file") != entry:
            continue
        line = loc["line"] - offset
        for claim in claims:
            start, quote = claim.get("source_line"), claim.get("statement_line")
            if (loc.get("name") == claim.get("name") and type(start) is int and type(quote) is int
                    and start <= line <= quote):
                candidates.append((claim["name"], claim["source"], start))
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) != 1:
        return None
    name, source, line = candidates[0]
    return {"status": "identified", "name": name, "source": source, "source_line": line,
            "verification_kind": "compiler_callsite_diagnostic",
            "reason": "unique compiler call site in the exact packaged entrypoint, immediately before the uncaught failure",
            "authority": "diagnostic_only"}
