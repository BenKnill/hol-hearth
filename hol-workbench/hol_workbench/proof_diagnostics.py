"""Bounded, nonce-bound authoring diagnostics; never theorem evidence."""
from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

PREFIX = "__HOL_PROOF_DIAGNOSTIC__"
ACTIVITY_PREFIX = "__HOL_PROOF_ACTIVITY__"
LEGACY_ACTIVITY_PROTOCOL = "hol-hearth.proof-activity.v1"
ACTIVITY_PROTOCOL = "hol-hearth.proof-activity.v2"
NONCE = re.compile(r"^[0-9a-f]{32}$")
MAX_EVENTS = 8
MAX_ACTIVITY_CALLS = 8192  # Legacy v1 lifetime limit, not a v2 capture quota.
MAX_ACTIVITY_DEPTH = 64
MAX_ACTIVITY_SEQUENCE = (1 << 62) - 1  # Positive OCaml int limit on the 64-bit runtime.
MAX_GOALS = 8
MAX_ASSUMPTIONS = 16
MAX_TEXT_BYTES = 2048
MAX_EXCEPTION_GAP_LINES = 8


def attach_dependency_diagnostic_sources(
    contract: dict[str, Any], closure: dict[str, Any], package_root: Path,
    *, transcript: bytes | None = None,
) -> None:
    """Map exact imports requested by bounded diagnostics, never verified claims."""
    from hol_workbench.hashing import sha256_bytes
    from hol_workbench.proofs.theorem_scan import extract_hol_theorems_bytes
    from hol_workbench.secure_tree_read import read_regular_file_beneath
    from hol_workbench.source_dependency_closure import source_dependency_closure_identity_matches

    descriptor = contract.get("proof_diagnostics")
    if not isinstance(descriptor, dict):
        return
    descriptor["dependency_sources"] = []
    descriptor["dependency_sources_status"] = "invalid_closure"
    try:
        if not source_dependency_closure_identity_matches(closure):
            return
        root = package_root.resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return
    requested: set[Path] | None = None
    if transcript is not None:
        requested = set()
        activity = account_proof_activity(transcript, contract)
        diagnostics = account_proof_diagnostics(transcript, contract)
        contexts = []
        if activity.get("status") == "recorded":
            contexts.extend(activity["active_calls"])
        if diagnostics.get("status") == "recorded":
            contexts.extend(diagnostics["events"])
        for context in contexts:
            for location in context["locations"]:
                try:
                    path = Path(location["file"])
                    if not path.is_absolute():
                        continue
                    path = path.resolve(strict=False)
                    if path.is_relative_to(root):
                        requested.add(path)
                except (OSError, RuntimeError, TypeError, ValueError):
                    continue
        if not requested:
            descriptor["dependency_sources_status"] = "not_needed"
            return
    mappings: dict[str, dict[str, Any]] = {}
    claims_by_source: dict[tuple[Path, str], list[dict[str, Any]]] = {}
    rejected: set[str] = set()
    for row in closure["records"]:
        if row.get("resolution") not in {"source_local", "source_overlay", "holdir_source", "mounted_source"}:
            continue
        if row.get("traversal") not in {"followed", "already_seen", "cycle"}:
            continue
        portable = str(row.get("package_path") or "")
        try:
            relative = Path(portable)
            if (not relative.parts or relative.is_absolute() or relative.as_posix() != portable
                    or ".." in relative.parts or row.get("symlinked") is not False):
                raise ValueError("unsafe diagnostic source path")
            packaged = root / relative
            if requested is not None and packaged not in requested:
                continue
            source = Path(str(row.get("resolved_path") or ""))
            trusted_root = Path(str(row.get("trusted_root_path") or ""))
            if not source.is_absolute() or not trusted_root.is_absolute():
                raise ValueError("missing diagnostic source authority")
            original = read_regular_file_beneath(trusted_root, source).data
            captured = read_regular_file_beneath(root, packaged).data
            if (sha256_bytes(original) != row.get("sha256") or captured != original
                    or type(row.get("size_bytes")) is not int or len(captured) != row["size_bytes"]):
                raise ValueError("diagnostic source bytes changed")
            claim_key = (source, row["sha256"])
            if claim_key not in claims_by_source:
                claims_by_source[claim_key] = [
                    {key: claim[key] for key in ("name", "source", "source_line", "statement_line")}
                    for claim in extract_hol_theorems_bytes(source, captured)
                    if claim.get("proof_constructor") == "prove"
                ]
            claims = claims_by_source[claim_key]
            mapping = {
                "source": str(source), "packaged_source": str(packaged),
                "source_sha256": row["sha256"], "source_line_offset": 0,
                "claims": claims, "authority": "diagnostic_only",
            }
            if portable in mappings and mappings[portable] != mapping:
                raise ValueError("ambiguous diagnostic source mapping")
            mappings[portable] = mapping
        except (OSError, RuntimeError, TypeError, ValueError, UnicodeError):
            rejected.add(portable)
    descriptor["dependency_sources"] = [value for key, value in mappings.items() if key not in rejected]
    descriptor["dependency_sources_status"] = "partial" if rejected else "recorded"


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
  let calls = ref 0 in
  let depth = ref 0 in
  let activity_enabled = ref true in
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
  let enter locations =
    if !activity_enabled && !calls = @MAX_ACTIVITY_SEQUENCE@ then
      (activity_enabled := false;
       (try original_print "\n__HOL_PROOF_ACTIVITY__:@NONCE@:%d:EXHAUSTED\n%!" !calls
        with _ -> ()));
    if !activity_enabled then (incr calls; incr depth);
    let call = !calls in
    (try
       if !activity_enabled && !depth <= @MAX_ACTIVITY_DEPTH@ then
         (original_print "\n__HOL_PROOF_ACTIVITY__:@NONCE@:%d:ENTER" call;
          List.iter (fun slot -> match Printexc.Slot.location slot with
            None -> () | Some loc ->
              let name = match Printexc.Slot.name slot with None -> "" | Some n -> n in
              original_print ":%s:%s:%d" (hex (bounded loc.Printexc.filename))
                (hex (bounded name)) loc.Printexc.line_number) locations;
          original_print "\n%!")
       else if !activity_enabled && !depth = @MAX_ACTIVITY_DEPTH@ + 1 then
         original_print "\n__HOL_PROOF_ACTIVITY__:@NONCE@:%d:OVERFLOW\n%!" call
     with _ -> ());
    call in
  let leave call =
    if !activity_enabled then
      ((try
          if !depth <= @MAX_ACTIVITY_DEPTH@ then
            original_print "\n__HOL_PROOF_ACTIVITY__:@NONCE@:%d:LEAVE\n%!" call
          else if !depth = @MAX_ACTIVITY_DEPTH@ + 1 then
            original_print "\n__HOL_PROOF_ACTIVITY__:@NONCE@:%d:RESUME:%d\n%!" call !calls
        with _ -> ());
       decr depth) in
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
    let call = enter locations in
    let returned_goals = ref None in
    let observe goal =
      let ((_,goals,_) as result) = tac goal in
      returned_goals := Some goals;
      result in
    try let theorem = original_prove (tm,observe) in leave call; theorem with error ->
      leave call;
      (try
         (match !returned_goals with
            Some (_::_ as goals) -> emit "residual_goals" tm error goals locations
          | _ -> emit "tactic_input" tm error [([],tm)] locations)
       with _ -> ());
      raise error;;
'''
    return (source.replace("@NONCE@", nonce)
            .replace("@MAX_ACTIVITY_DEPTH@", str(MAX_ACTIVITY_DEPTH))
            .replace("@MAX_ACTIVITY_SEQUENCE@", str(MAX_ACTIVITY_SEQUENCE)).encode("utf-8"))


def _text(value: str) -> str:
    if len(value) > (MAX_TEXT_BYTES + 32) * 2:
        raise ValueError("oversized diagnostic text")
    return bytes.fromhex(value).decode("utf-8", errors="replace")


def _following_exception_line(lines: list[bytes], event: dict[str, Any]) -> int | None:
    """Link a frame only to a matching, nearby OCaml exception rendering.

    HOL can insert empty lines or its time wrapper's failure report before the
    exception re-raised by prove. Skip at most one matching timing report within
    the same total line bound, never arbitrary output or another diagnostic frame.
    Unknown, wrapped or truncated exception renderings remain unattributed.
    """
    error = event["exception"]
    if error == "Stack overflow":
        expected = "Stack overflow during evaluation (looping recursion?)."
    else:
        # Printexc.to_string and the toplevel differ for these common exceptions.
        # Match their already-escaped string verbatim; do not decode OCaml text.
        string_error = re.fullmatch(r'(Failure|Invalid_argument)\(("(?:[^"\\\r\n]|\\[^\r\n])*")\)', error)
        if string_error:
            rendered = f"{string_error[1]} {string_error[2]}"
        elif re.fullmatch(r"[A-Z][A-Za-z0-9_']*(?:\.[A-Z][A-Za-z0-9_']*)*", error):
            rendered = error
        else:
            return None
        expected = f"Exception: {rendered}."
    start = event["end_transcript_line"]
    timing_report_seen = False
    for index in range(start, min(len(lines), start + MAX_EXCEPTION_GAP_LINES + 1)):
        line = lines[index].strip()
        if not line:
            continue
        text = line.decode("utf-8", errors="replace")
        if text.removeprefix("# ").strip() == expected:
            return index + 1
        # HOL lib.ml's time function prints the exact Printexc.to_string error
        # after a nonnegative string_of_float CPU duration, then re-raises it.
        timing = re.fullmatch(
            r"Failed after \(user\) CPU time of "
            r"(?:[0-9]+\.[0-9]*(?:e[+-]?[0-9]+)?|[0-9]+e[+-]?[0-9]+): (.+)", text)
        if not timing_report_seen and timing is not None and timing[1] == error:
            timing_report_seen = True
            continue
        return None
    return None


def _event_at_failure(event: dict[str, Any], failure_line: Any) -> bool:
    return (type(failure_line) is int
            and type(event.get("following_exception_transcript_line")) is int
            and event["following_exception_transcript_line"] == failure_line)


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
    transcript_lines = transcript.splitlines()
    lines = [(number, line) for number, line in enumerate(transcript_lines, 1) if line.startswith(prefix)]
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
    for event in events:
        event["following_exception_transcript_line"] = _following_exception_line(transcript_lines, event)
    return {**empty, "status": "recorded", "events": events, "capture_truncated": truncated}


def _line_ranges(transcript: bytes) -> Iterator[tuple[int, int, int]]:
    """Yield line offsets without copying or retaining the transcript's lines."""
    start = 0
    number = 0
    for number, match in enumerate(re.finditer(rb"\r\n?|\n", transcript), 1):
        end = match.end()
        yield number, start, end
        start = end
    if start < len(transcript):
        yield number + 1, start, len(transcript)


def account_proof_activity(transcript: bytes, contract: dict[str, Any]) -> dict[str, Any]:
    """Retain only active calls, with explicit loss/recovery at the nesting bound.

    The v2 raw event log grows with execution, but completed calls consume no
    retained activity history. Legacy v1 receipts keep their terminal call cap.
    Neither protocol establishes failure, completion, or theorem evidence.
    """
    descriptor = contract.get("proof_diagnostics")
    protocol = descriptor.get("activity_protocol") if isinstance(descriptor, dict) else None
    available = isinstance(protocol, str) and protocol in {LEGACY_ACTIVITY_PROTOCOL, ACTIVITY_PROTOCOL}
    legacy = protocol == LEGACY_ACTIVITY_PROTOCOL
    empty: dict[str, Any] = {
        "schema": protocol if available else ACTIVITY_PROTOCOL,
        "status": "none" if available else "unavailable",
        "active_calls": [], "authority": "diagnostic_only",
        "boundary": "An entered prove call without a recorded return; neither failure nor theorem evidence.",
    }
    if not available:
        return empty
    nonce = str(contract.get("nonce") or "")
    if not NONCE.fullmatch(nonce) or descriptor.get("nonce") != nonce:
        return {**empty, "status": "invalid_contract"}
    prefix = f"{ACTIVITY_PREFIX}:{nonce}:".encode()
    active: list[dict[str, Any]] = []
    entered = records = 0
    overflow: int | None = None
    overflow_spans = suppressed_calls = 0
    terminal: str | None = None
    max_line_bytes = 32 * (MAX_TEXT_BYTES + 32) * 4 + 256
    try:
        for lineno, start, end in _line_ranges(transcript):
            if not transcript.startswith(prefix, start, end):
                # A cut-off ENTER must not leave an earlier call looking current.
                if (end - start < len(prefix) and transcript.startswith(ACTIVITY_PREFIX.encode(), start, end)
                        and prefix.startswith(transcript[start:end])):
                    return {**empty, "status": "incomplete"}
                continue
            if end - start > max_line_bytes:
                raise ValueError("proof activity record limit exceeded")
            raw = transcript[start:end]
            if not raw.endswith(b"\n"):
                return {**empty, "status": "incomplete"}
            records += 1
            if legacy and records > MAX_ACTIVITY_CALLS * 2 + 1:
                raise ValueError("proof activity record limit exceeded")
            if terminal is not None:
                raise ValueError("proof activity record after terminal truncation")
            parts = raw[len(prefix):].rstrip(b"\r\n").decode("ascii").split(":")
            call = int(parts[0])
            op = parts[1]
            if not 1 <= call <= MAX_ACTIVITY_SEQUENCE:
                raise ValueError("invalid proof activity sequence")
            if legacy and op == "TRUNCATED":
                if len(parts) != 2 or call != MAX_ACTIVITY_CALLS + 1 or entered != MAX_ACTIVITY_CALLS:
                    raise ValueError("invalid proof activity truncation")
                terminal = "truncated"
            elif not legacy and op == "EXHAUSTED":
                if len(parts) != 2 or call != MAX_ACTIVITY_SEQUENCE or (overflow is None and entered != call):
                    raise ValueError("invalid proof activity sequence exhaustion")
                entered = call
                terminal = "sequence_exhausted"
            elif not legacy and op == "OVERFLOW":
                if (len(parts) != 2 or overflow is not None or len(active) != MAX_ACTIVITY_DEPTH
                        or call != entered + 1):
                    raise ValueError("invalid proof activity overflow")
                overflow = call
                overflow_spans += 1
                entered = call
            elif not legacy and op == "RESUME":
                if len(parts) != 3 or overflow is None or call != overflow:
                    raise ValueError("unmatched proof activity recovery")
                last = int(parts[2])
                if not call <= last <= MAX_ACTIVITY_SEQUENCE:
                    raise ValueError("invalid proof activity recovery sequence")
                suppressed_calls += last - call + 1
                entered = last
                overflow = None
            elif overflow is not None:
                raise ValueError("unexpected proof activity record inside overflow")
            elif op == "ENTER":
                if (call != entered + 1 or (legacy and call > MAX_ACTIVITY_CALLS)
                        or (not legacy and len(active) >= MAX_ACTIVITY_DEPTH)
                        or (len(parts) - 2) % 3 or len(parts) > 98):
                    raise ValueError("invalid proof activity entry")
                locations = []
                for pos in range(2, len(parts), 3):
                    line = int(parts[pos + 2])
                    if line < 1:
                        raise ValueError("invalid proof activity location")
                    locations.append({"file": _text(parts[pos]), "name": _text(parts[pos + 1]), "line": line})
                active.append({"call": call, "locations": locations, "entered_transcript_line": lineno})
                entered = call
            elif op == "LEAVE":
                if len(parts) != 2 or not active or active[-1]["call"] != call:
                    raise ValueError("unmatched proof activity return")
                active.pop()
            else:
                raise ValueError("unknown proof activity operation")
    except (ValueError, UnicodeError, IndexError):
        return {**empty, "status": "malformed"}
    if terminal is not None:
        return {**empty, "status": terminal, "entered_call_count": entered}
    bounds = {} if legacy else {
        "max_active_calls": MAX_ACTIVITY_DEPTH, "history_retention": "active_calls_only",
        "completed_call_history_retained": 0, "overflow_span_count": overflow_spans,
        "suppressed_call_count": suppressed_calls,
        "suppressed_call_count_complete": overflow is None,
    }
    if overflow is not None:
        return {**empty, **bounds, "status": "depth_overflow", "entered_call_count": entered,
                "entered_call_count_complete": False}
    return {**empty, "status": "recorded" if records else "none", "active_calls": active,
            "entered_call_count": entered, **bounds}


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
        recorded_binding = receipt.get("failing_binding") or {}
        # Old receipts predate exception-link accounting. Preserve their already
        # identified adjacent context for display only; never infer a new link.
        legacy_current = (
            "following_exception_transcript_line" not in event
            and type(failure_line) is int and event.get("end_transcript_line") == failure_line - 1
            and recorded_binding.get("status") == "identified"
            and recorded_binding.get("verification_kind") == "compiler_callsite_diagnostic"
            and recorded_binding.get("authority") == "diagnostic_only"
            and any(loc.get("name") == recorded_binding.get("name") for loc in event.get("locations", []))
        )
        current = (not data.get("capture_truncated") and not receipt.get("source_completed")
                   and (_event_at_failure(event, failure_line) or legacy_current))
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


def _binding_at_locations(
    locations: list[dict[str, Any]], contract: dict[str, Any], claims: list[dict[str, Any]],
    *, include_dependencies: bool = False,
) -> tuple[str, str, int] | None:
    """Map only a unique named call site in an exactly captured source."""
    descriptor = contract.get("proof_diagnostics")
    if not isinstance(descriptor, dict):
        return None
    entry = descriptor.get("packaged_entrypoint")
    offset = descriptor.get("source_line_offset")
    sources = [(entry, offset, claims)]
    if include_dependencies:
        dependencies = descriptor.get("dependency_sources") or []
        if not isinstance(dependencies, list):
            return None
        for mapping in dependencies:
            if (not isinstance(mapping, dict) or mapping.get("authority") != "diagnostic_only"
                    or type(mapping.get("source_line_offset")) is not int
                    or mapping["source_line_offset"] != 0 or not isinstance(mapping.get("claims"), list)):
                return None
            sources.append((mapping.get("packaged_source"), 0, mapping["claims"]))
    candidates: list[tuple[str, str, int]] = []
    for loc in locations:
        if not isinstance(loc, dict) or type(loc.get("line")) is not int:
            continue
        for packaged, line_offset, source_claims in sources:
            try:
                if (not isinstance(packaged, str) or not isinstance(loc.get("file"), str)
                        or type(line_offset) is not int or not Path(packaged).is_absolute()
                        or not Path(loc["file"]).is_absolute()
                        or Path(loc["file"]).resolve(strict=False) != Path(packaged).resolve(strict=False)):
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
            line = loc["line"] - line_offset
            for claim in source_claims:
                if not isinstance(claim, dict):
                    return None
                start = claim.get("source_line")
                quote = claim.get("statement_line") or start
                if (loc.get("name") == claim.get("name") and type(start) is int and type(quote) is int
                        and isinstance(claim.get("source"), str) and start <= line <= quote):
                    candidates.append((claim["name"], claim["source"], start))
    candidates = list(dict.fromkeys(candidates))
    return candidates[0] if len(candidates) == 1 else None


def identify_failed_binding(
    diagnostics: dict[str, Any], contract: dict[str, Any],
    claims: list[dict[str, Any]], failure_line: int | None,
) -> dict[str, Any] | None:
    """Map a compiler-reported direct call site, never a preceding printed val."""
    events = diagnostics.get("events") or []
    if diagnostics.get("status") != "recorded" or diagnostics.get("capture_truncated") or not events:
        return None
    event = events[-1]
    # A caught failure followed by another error is not attribution. The
    # diagnostic must be followed only by bounded blank/matching HOL timing
    # output and its matching exception.
    if not _event_at_failure(event, failure_line):
        return None
    candidate = _binding_at_locations(event.get("locations") or [], contract, claims)
    if candidate is None:
        return None
    name, source, line = candidate
    return {"status": "identified", "name": name, "source": source, "source_line": line,
            "verification_kind": "compiler_callsite_diagnostic",
            "reason": "unique compiler call site in the exact packaged entrypoint, followed only by bounded blank or matching HOL timing output and the matching uncaught exception",
            "authority": "diagnostic_only"}


def identify_running_binding(
    activity: dict[str, Any], contract: dict[str, Any], claims: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attribute an interrupted active call; a return never means a proved binding."""
    unknown: dict[str, Any] = {
        "status": "unknown", "name": None, "authority": "diagnostic_only",
        "reason": "no complete, unambiguous active prove call was recorded at interruption",
    }
    active = activity.get("active_calls") or []
    if activity.get("status") != "recorded" or not active:
        return unknown
    call = active[-1]
    candidate = _binding_at_locations(call.get("locations") or [], contract, claims, include_dependencies=True)
    if candidate is None:
        return {**unknown, "reason": "active prove call is unsupported or has no unique exactly captured call site"}
    name, source, line = candidate
    return {
        "status": "running_at_interruption", "name": name, "source": source, "source_line": line,
        "verification_kind": "compiler_callsite_diagnostic", "authority": "diagnostic_only",
        "entered_transcript_line": call["entered_transcript_line"],
        "reason": "unique exactly captured prove call entered without a recorded return before interruption; not failure or proof evidence",
    }
