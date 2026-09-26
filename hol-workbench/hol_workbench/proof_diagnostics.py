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
MAX_TEXT_BYTES = 2048  # labels, names and file paths
MAX_TERM_BYTES = 16384  # rendered goal conclusions and assumptions
MAX_EXCEPTION_BYTES = 65536  # complete Printexc text; INT_ARITH quotes its whole goal
MAX_STEPS = 12  # recorded failing tactic steps, outermost first
MAX_STEP_LOCATIONS = 8
MAX_EXCEPTION_GAP_LINES = 8
TOPLEVEL_TRUNCATION = re.compile(r'^(Failure|Invalid_argument) "(.*)"\.\.\. \(\* string length (\d+); truncated \*\)$', re.S)


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
let hol_hearth_steps_@NONCE@ = ref ([] : (goal * exn * Printexc.backtrace_slot list) list);;
let prove =
  let original_prove = prove in
  let original_print = Printf.printf in
  let original_concl = concl in
  let original_term_printer = pp_print_term in
  let steps = hol_hearth_steps_@NONCE@ in
  let events = ref 0 in
  let calls = ref 0 in
  let depth = ref 0 in
  let activity_enabled = ref true in
  let bound = @MAX_TERM_BYTES@ in
  let bounded_to limit s = if String.length s <= limit then s else
    String.sub s 0 limit ^ "... [truncated]" in
  let bounded = bounded_to 2048 in
  let bounded_exn = bounded_to @MAX_EXCEPTION_BYTES@ in
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
  let emit kind tm error goals locations recorded_steps =
    if !events < 8 then
      (incr events;
       let event = !events in
       let chosen = take 8 goals in
       original_print "\n__HOL_PROOF_DIAGNOSTIC__:@NONCE@:%d:BEGIN:%s:%d:%d:%s:%s\n"
         event kind (List.length goals) (List.length chosen)
         (hex (render tm)) (hex (bounded_exn (Printexc.to_string error)));
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
       (match recorded_steps with [] -> () | _ ->
         original_print "__HOL_PROOF_DIAGNOSTIC__:@NONCE@:%d:STEPS:%d:%d\n"
           event (List.length recorded_steps) (List.length (take @MAX_STEPS@ recorded_steps));
         List.iteri (fun index ((assumptions,conclusion),step_error,step_locations) ->
           original_print "__HOL_PROOF_DIAGNOSTIC__:@NONCE@:%d:STEP:%d:%d:%s:%s\n"
             event index (List.length assumptions) (hex (render conclusion))
             (hex (bounded_exn (Printexc.to_string step_error)));
           List.iter (fun (label,th) ->
             original_print "__HOL_PROOF_DIAGNOSTIC__:@NONCE@:%d:STEPASSUMPTION:%d:%s:%s\n"
               event index (hex (bounded label)) (hex (render (original_concl th))))
             (take 16 assumptions);
           List.iter (fun slot -> match Printexc.Slot.location slot with
             None -> () | Some loc ->
               let name = match Printexc.Slot.name slot with None -> "" | Some n -> n in
               original_print "__HOL_PROOF_DIAGNOSTIC__:@NONCE@:%d:STEPLOCATION:%d:%s:%s:%d\n"
                 event index (hex (bounded loc.Printexc.filename)) (hex (bounded name))
                 loc.Printexc.line_number)
             (take @MAX_STEP_LOCATIONS@ step_locations))
           (take @MAX_STEPS@ recorded_steps));
       original_print "__HOL_PROOF_DIAGNOSTIC__:@NONCE@:%d:END\n%!" event)
    else if !events = 8 then
      (incr events; original_print "__HOL_PROOF_DIAGNOSTIC__:@NONCE@:9:TRUNCATED\n%!") in
  fun (tm,tac) ->
    let locations = try match Printexc.backtrace_slots (Printexc.get_callstack 32) with
      None -> [] | Some slots -> Array.to_list slots with _ -> [] in
    let call = enter locations in
    let returned_goals = ref None in
    steps := [];
    let observe goal =
      let ((_,goals,_) as result) = tac goal in
      returned_goals := Some goals;
      steps := [];
      result in
    try let theorem = original_prove (tm,observe) in leave call; theorem with error ->
      leave call;
      let recorded_steps = !steps in
      steps := [];
      (try
         (match !returned_goals with
            Some (_::_ as goals) -> emit "residual_goals" tm error goals locations recorded_steps
          | _ -> emit "tactic_input" tm error [([],tm)] locations recorded_steps)
       with _ -> ());
      raise error;;
let hol_hearth_wrap_tacticals_@NONCE@ =
  (* Record the goal each THEN/THENL continuation received when it raised. The
     wrapped tacticals return the original results and re-raise the original
     exceptions; a continuation that later succeeds drops what its callees
     recorded, so only the propagating failure path reaches the prove frame. *)
  let steps = hol_hearth_steps_@NONCE@ in
  let slots () =
    try match Printexc.backtrace_slots (Printexc.get_callstack 24) with
      None -> [] | Some found ->
        List.filter (fun slot -> Printexc.Slot.location slot <> None) (Array.to_list found)
    with _ -> [] in
  let rec drop n l = if n <= 0 then l else match l with [] -> [] | _::t -> drop (n-1) t in
  let guard locations (tac:tactic) : tactic = fun g ->
    try tac g with error -> steps := (g,error,locations) :: !steps; raise error in
  let settle (tac:tactic) : tactic = fun g ->
    let mark = List.length !steps in
    let result = tac g in
    steps := drop (List.length !steps - mark) !steps;
    result in
  fun (original_then : tactic -> tactic -> tactic) (original_thenl : tactic -> tactic list -> tactic) ->
    let then_ tac1 tac2 =
      let locations = slots () in
      settle (original_then tac1 (guard locations tac2))
    and thenl_ tac1 tacl =
      let locations = slots () in
      settle (original_thenl tac1 (List.map (guard locations) tacl)) in
    then_,thenl_;;
'''
    return (source.replace("@NONCE@", nonce)
            .replace("@MAX_ACTIVITY_DEPTH@", str(MAX_ACTIVITY_DEPTH))
            .replace("@MAX_ACTIVITY_SEQUENCE@", str(MAX_ACTIVITY_SEQUENCE))
            .replace("@MAX_TERM_BYTES@", str(MAX_TERM_BYTES))
            .replace("@MAX_EXCEPTION_BYTES@", str(MAX_EXCEPTION_BYTES))
            .replace("@MAX_STEPS@", str(MAX_STEPS))
            .replace("@MAX_STEP_LOCATIONS@", str(MAX_STEP_LOCATIONS)).encode("utf-8"))


def tactical_step_prelude(nonce: str) -> bytes:
    """Bind HOL's THEN and THENL to the recording wrappers; HOL toplevel syntax only.

    Kept apart from ``diagnostic_prelude`` because plain OCaml cannot spell
    HOL's uppercase tactical identifiers; the emitter selftest compiles the
    plain part and exercises the same wrapper factory with fixture tacticals.
    """
    if not NONCE.fullmatch(nonce):
        raise ValueError("invalid proof diagnostic nonce")
    return (
        "let (THEN),(THENL) =\n"
        f"  hol_hearth_wrap_tacticals_{nonce} (fun tac1 tac2 -> tac1 THEN tac2) (fun tac1 tacl -> tac1 THENL tacl);;\n"
    ).encode("utf-8")


def _text(value: str, limit: int = MAX_TEXT_BYTES) -> str:
    if len(value) > (limit + 32) * 2:
        raise ValueError("oversized diagnostic text")
    return bytes.fromhex(value).decode("utf-8", errors="replace")


def _term(value: str) -> str:
    return _text(value, MAX_TERM_BYTES)


def _exception(value: str) -> str:
    return _text(value, MAX_EXCEPTION_BYTES)


def _exception_string_body(error: str) -> tuple[str, str, bool] | None:
    """Split a Printexc ``Failure("...")`` rendering into constructor, escaped body and completeness."""
    complete = re.fullmatch(r'(Failure|Invalid_argument)\(("(?:[^"\\\r\n]|\\[^\r\n])*")\)', error)
    if complete:
        return complete[1], complete[2][1:-1], True
    partial = re.fullmatch(r'(Failure|Invalid_argument)\("((?:[^"\\\r\n]|\\[^\r\n])*)\.\.\. \[truncated\]', error, re.S)
    if partial:
        return partial[1], partial[2], False
    return None


def _toplevel_exception_block(lines: list[bytes], start: int) -> tuple[int, str] | None:
    """Join the toplevel's possibly multi-line ``Exception:`` rendering, as first_error does."""
    text = lines[start].strip().decode("utf-8", errors="replace").removeprefix("# ").strip()
    if not text.startswith("Exception:"):
        return None
    parts = [text]
    for index in range(start + 1, min(len(lines), start + 14)):
        raw = lines[index].decode("utf-8", errors="replace")
        stripped = raw.strip()
        if not stripped or stripped.startswith(("val ", "# ", "__HOL_", "HOL_WORKBENCH_", "Error in included file")):
            break
        if not raw[:1].isspace() and not re.match(r"^(Failure|Invalid_argument|[A-Z][A-Za-z0-9_'.]*)\b", stripped):
            break
        parts.append(stripped)
        if stripped.endswith("."):
            break
    return start + 1, " ".join(parts)


def _following_exception_line(lines: list[bytes], event: dict[str, Any]) -> int | None:
    """Link a frame only to a matching, nearby OCaml exception rendering.

    HOL can insert empty lines or its time wrapper's failure report before the
    exception re-raised by prove. Skip at most one matching timing report within
    the same total line bound, never arbitrary output or another diagnostic frame.
    Unknown, wrapped or truncated exception renderings remain unattributed.
    """
    error = event["exception"]
    string_body = None
    if error == "Stack overflow":
        expected = "Stack overflow during evaluation (looping recursion?)."
    else:
        # Printexc.to_string and the toplevel differ for these common exceptions.
        # Match their already-escaped string verbatim; do not decode OCaml text.
        string_body = _exception_string_body(error)
        if string_body and string_body[2]:
            rendered = f'{string_body[0]} "{string_body[1]}"'
        elif string_body:
            rendered = None
        elif re.fullmatch(r"[A-Z][A-Za-z0-9_']*(?:\.[A-Z][A-Za-z0-9_']*)*", error):
            rendered = error
        else:
            return None
        expected = f"Exception: {rendered}." if rendered is not None else None
    start = event["end_transcript_line"]
    timing_report_seen = False
    for index in range(start, min(len(lines), start + MAX_EXCEPTION_GAP_LINES + 1)):
        line = lines[index].strip()
        if not line:
            continue
        text = line.decode("utf-8", errors="replace")
        if expected is not None and text.removeprefix("# ").strip() == expected:
            return index + 1
        block = _toplevel_exception_block(lines, index)
        if block is not None:
            lineno, joined = block
            if expected is not None and joined == expected:
                return lineno
            if string_body is not None:
                # The toplevel truncates long strings itself ("..." plus a length note)
                # and may break the rendering across lines. Accept only a rendering
                # whose shown escaped prefix is exactly a prefix of the recorded text.
                shown = TOPLEVEL_TRUNCATION.fullmatch(joined.removeprefix("Exception:").strip().removesuffix("."))
                if shown and shown[1] == string_body[0]:
                    prefix = shown[2]
                    body = string_body[1]
                    limit = min(len(prefix), len(body))
                    if limit and prefix[:limit] == body[:limit] and (string_body[2] or len(prefix) >= limit):
                        if not string_body[2] or len(prefix) <= len(body):
                            return lineno
            return None
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
        if len(lines) > MAX_EVENTS * (
            35 + MAX_GOALS * (1 + MAX_ASSUMPTIONS) + MAX_STEPS * (1 + MAX_ASSUMPTIONS + MAX_STEP_LOCATIONS)
        ) + 1:
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
                          "shown_goal_count": count, "statement": _term(parts[5]),
                          "exception": _exception(parts[6]), "goals": [], "locations": [],
                          "step_count": 0, "shown_step_count": 0, "steps": []}
            elif active is None or active["event"] != ordinal:
                raise ValueError("record outside event")
            elif op == "GOAL":
                if len(parts) != 5 or int(parts[2]) != len(active["goals"]):
                    raise ValueError("invalid goal index")
                if len(active["goals"]) >= active["shown_goal_count"] or int(parts[3]) < 0:
                    raise ValueError("invalid goal count")
                if active["step_count"]:
                    raise ValueError("goal after steps")
                active["goals"].append({"conclusion": _term(parts[4]), "assumption_count": int(parts[3]),
                                         "assumptions": []})
            elif op == "ASSUMPTION":
                if len(parts) != 5 or int(parts[2]) != len(active["goals"]) - 1 or not active["goals"]:
                    raise ValueError("invalid assumption index")
                goal = active["goals"][-1]
                if len(goal["assumptions"]) >= min(MAX_ASSUMPTIONS, goal["assumption_count"]):
                    raise ValueError("too many assumptions")
                if active["step_count"]:
                    raise ValueError("assumption after steps")
                goal["assumptions"].append({"label": _text(parts[3]), "conclusion": _term(parts[4])})
            elif op == "LOCATION":
                if len(parts) != 5 or len(active["locations"]) >= 32 or int(parts[4]) < 1 or active["step_count"]:
                    raise ValueError("invalid location")
                active["locations"].append({"file": _text(parts[2]), "name": _text(parts[3]), "line": int(parts[4])})
            elif op == "STEPS":
                total, shown = int(parts[2]), int(parts[3])
                if len(parts) != 4 or active["step_count"] or total < 1 or not 1 <= shown <= min(MAX_STEPS, total):
                    raise ValueError("invalid step count")
                active["step_count"], active["shown_step_count"] = total, shown
            elif op == "STEP":
                if len(parts) != 6 or int(parts[2]) != len(active["steps"]) or int(parts[3]) < 0:
                    raise ValueError("invalid step index")
                if len(active["steps"]) >= active["shown_step_count"]:
                    raise ValueError("invalid step count")
                active["steps"].append({"conclusion": _term(parts[4]), "assumption_count": int(parts[3]),
                                        "assumptions": [], "exception": _exception(parts[5]), "locations": []})
            elif op == "STEPASSUMPTION":
                if len(parts) != 5 or not active["steps"] or int(parts[2]) != len(active["steps"]) - 1:
                    raise ValueError("invalid step assumption index")
                step = active["steps"][-1]
                if step["locations"] or len(step["assumptions"]) >= min(MAX_ASSUMPTIONS, step["assumption_count"]):
                    raise ValueError("too many step assumptions")
                step["assumptions"].append({"label": _text(parts[3]), "conclusion": _term(parts[4])})
            elif op == "STEPLOCATION":
                if len(parts) != 6 or not active["steps"] or int(parts[2]) != len(active["steps"]) - 1 or int(parts[5]) < 1:
                    raise ValueError("invalid step location")
                step = active["steps"][-1]
                if len(step["locations"]) >= MAX_STEP_LOCATIONS:
                    raise ValueError("too many step locations")
                step["locations"].append({"file": _text(parts[3]), "name": _text(parts[4]), "line": int(parts[5])})
            elif op == "END":
                if len(parts) != 2 or len(active["goals"]) != active["shown_goal_count"]:
                    raise ValueError("incomplete event")
                if any(len(g["assumptions"]) != min(MAX_ASSUMPTIONS, g["assumption_count"]) for g in active["goals"]):
                    raise ValueError("incomplete assumptions")
                if len(active["steps"]) != active["shown_step_count"] or any(
                    len(s["assumptions"]) != min(MAX_ASSUMPTIONS, s["assumption_count"]) for s in active["steps"]
                ):
                    raise ValueError("incomplete steps")
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


def attach_step_source_lines(diagnostics: dict[str, Any], contract: dict[str, Any]) -> None:
    """Map each recorded step's first call site in the packaged entrypoint back to source lines.

    The diagnostic prelude is prepended to the packaged entrypoint, so its own
    wrapper frames sit at lines up to the recorded offset and are skipped. A
    frame in another packaged file keeps its coordinates; nothing is inferred.
    """
    descriptor = contract.get("proof_diagnostics")
    if not isinstance(descriptor, dict) or diagnostics.get("status") != "recorded":
        return
    packaged = descriptor.get("packaged_entrypoint")
    offset = descriptor.get("source_line_offset")
    if not isinstance(packaged, str) or type(offset) is not int:
        return
    try:
        packaged_path = Path(packaged).resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return
    for event in diagnostics.get("events") or []:
        for step in event.get("steps") or []:
            for location in step.get("locations") or []:
                file = location.get("file")
                line = location.get("line")
                if not isinstance(file, str) or type(line) is not int:
                    continue
                try:
                    same_file = Path(file).resolve(strict=False) == packaged_path
                except (OSError, RuntimeError, ValueError):
                    continue
                if same_file and line <= offset:
                    continue  # the prelude's own wrapper frames
                if same_file:
                    step["source_line"] = line - offset
                    step["source_location_kind"] = "entrypoint"
                else:
                    step["source_location_kind"] = "packaged_dependency"
                    step["packaged_file"] = file
                    step["packaged_line"] = line
                break


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
        steps = event.get("steps") or []
        label = "residual goals" if kind == "residual_goals" else (
            "original tactic input" + ("" if steps else " (intermediate goals unavailable)"))
        print(f"  {label}: {event['shown_goal_count']}/{event['goal_count']}")
        goals = event["goals"] if verbose else event["goals"][:3]
        for goal in goals:
            _print_goal(goal, verbose=verbose, indent="    ")
        if len(event["goals"]) > len(goals):
            print(f"    ... {len(event['goals']) - len(goals)} more recorded goals; use --verbose")
        _print_bounded_text("  exception", event.get("exception"), verbose=verbose)
        if steps:
            print(f"  failing tactic steps: {event.get('shown_step_count', len(steps))}/{event.get('step_count', len(steps))}"
                  " recorded, outermost first; each goal is what that THEN/THENL continuation received before it raised")
            shown_steps = steps if verbose else steps[:1]
            for index, step in enumerate(shown_steps, 1):
                where = _step_location(step, receipt.get("source"))
                print(f"    step {index}{' at ' + where if where else ''}:")
                _print_bounded_text("      raised", step.get("exception"), verbose=verbose, limit=240)
                _print_goal(step, verbose=verbose, indent="      ")
            if len(steps) > len(shown_steps):
                print(f"    ... {len(steps) - len(shown_steps)} more nested steps; use --verbose")


def _print_goal(goal: dict[str, Any], *, verbose: bool, indent: str) -> None:
    assumptions = goal["assumptions"] if verbose else goal["assumptions"][:4]
    for assumption in assumptions:
        print(f"{indent}assumption {assumption['label']}: {assumption['conclusion']}")
    if goal["assumption_count"] > len(assumptions):
        print(f"{indent}... {goal['assumption_count'] - len(assumptions)} more assumptions")
    print(f"{indent}|- {goal['conclusion']}")


def _print_bounded_text(label: str, text: Any, *, verbose: bool, limit: int = 1200) -> None:
    if not isinstance(text, str) or not text:
        return
    if verbose or len(text) <= limit:
        print(f"{label}: {text}")
        return
    print(f"{label}: {text[:limit]}... ({len(text) - limit} more characters; use --verbose)")


def _step_location(step: dict[str, Any], source: Any = None) -> str:
    if type(step.get("source_line")) is int:
        name = Path(str(source)).name if isinstance(source, str) and source else "source"
        return f"{name}:{step['source_line']}"
    if step.get("source_location_kind") == "packaged_dependency":
        file = str(step.get("packaged_file") or "")
        marker = "/source-package/"
        shown = file.split(marker, 1)[1] if marker in file else Path(file).name
        return f"{shown}:{step.get('packaged_line')} (packaged dependency coordinates)"
    return ""


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
