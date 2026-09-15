"""Trusted-wrapper runtime accounting for HOL logical-foundation growth."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hol_workbench.fork_child_ocaml import ocaml_string_literal

FOUNDATION_DELTA_SCHEMA = "hol-workbench.runtime-foundation-delta.v1"
FOUNDATION_DELTA_PREFIX = "__HOL_FOUNDATION_DELTA__"
FOUNDATION_CATEGORIES = ("axioms", "definitions", "types", "constants")
FOUNDATION_MARKER_PREFIX_RE = re.compile(r"^__HOL_FOUNDATION_DELTA__:[0-9a-f]{32}:$")


def foundation_probe_contract(nonce: str) -> dict[str, Any]:
    """Describe one nonce-bound foundation probe emitted by the fork wrapper."""

    marker_prefix = f"{FOUNDATION_DELTA_PREFIX}:{nonce}:"
    if FOUNDATION_MARKER_PREFIX_RE.fullmatch(marker_prefix) is None:
        raise ValueError("foundation marker prefix is malformed")
    return {
        "schema": FOUNDATION_DELTA_SCHEMA,
        "marker_prefix": marker_prefix,
        "categories": list(FOUNDATION_CATEGORIES),
        "measurement": "runtime prepend-extension-validated cardinality delta for HOL logical-foundation registries",
        "baseline_boundary": (
            "trusted fork-child wrapper captures original registry closures after profile restore and immediately "
            "before loading the exact packaged replay payload"
        ),
        "advisory_policy": (
            "block on an observed positive axiom delta, report no foundation objection on an observed zero delta, "
            "and otherwise require manual review; this advisory never changes the replay result"
        ),
        "inventory_boundary": (
            "cardinality delta relative to the restored warm basis only; does not inventory pre-existing basis "
            "axioms, identify declarations, or detect arbitrary hostile OCaml runtime mutation"
        ),
    }


def foundation_wrapped_load(
    *,
    load_wrapper: Path,
    done_token: str,
    marker_prefix: str | None,
) -> str:
    """Load source under captured registry closures and always emit its delta."""

    load_literal = ocaml_string_literal(str(load_wrapper))
    done_literal = ocaml_string_literal(done_token)
    if marker_prefix is None:
        return f"""     (try
        loadt {load_literal};
        print_endline {done_literal}
      with e ->
        Printf.printf "Exception: %s\\n" (Printexc.to_string e));
"""
    if FOUNDATION_MARKER_PREFIX_RE.fullmatch(marker_prefix) is None:
        raise ValueError("foundation marker prefix is malformed")
    marker_format = ocaml_string_literal(marker_prefix + "%s:%d:%d:%d:%d\n%!")
    return f"""     let hol_workbench_original_axioms = axioms
     and hol_workbench_original_definitions = definitions
     and hol_workbench_original_types = types
     and hol_workbench_original_constants = constants in
     let hol_workbench_before =
       try Some
         (hol_workbench_original_axioms (),
          hol_workbench_original_definitions (),
          hol_workbench_original_types (),
          hol_workbench_original_constants ())
       with _ -> None in
     let hol_workbench_source_completed =
       try loadt {load_literal}; true with e ->
         (Printf.printf "Exception: %s\\n" (Printexc.to_string e); false) in
     let rec hol_workbench_drop n xs =
       if n = 0 then Some xs else
       match xs with [] -> None | _::rest -> hol_workbench_drop (n - 1) rest in
     let hol_workbench_delta before after =
       let n = List.length after - List.length before in
       if n < 0 then None else
       match hol_workbench_drop n after with
         Some tail when tail = before -> Some n
       | _ -> None in
     let hol_workbench_value = function Some n -> n | None -> -1 in
     let hol_workbench_emit status axioms definitions types constants =
       Printf.printf {marker_format} status axioms definitions types constants in
     (try
        match hol_workbench_before with
          None -> hol_workbench_emit "invalid" (-1) (-1) (-1) (-1)
        | Some
            (hol_workbench_before_axioms,
             hol_workbench_before_definitions,
             hol_workbench_before_types,
             hol_workbench_before_constants) ->
            let hol_workbench_after_axioms = hol_workbench_original_axioms ()
            and hol_workbench_after_definitions = hol_workbench_original_definitions ()
            and hol_workbench_after_types = hol_workbench_original_types ()
            and hol_workbench_after_constants = hol_workbench_original_constants () in
            let hol_workbench_axiom_delta =
              hol_workbench_delta hol_workbench_before_axioms hol_workbench_after_axioms
            and hol_workbench_definition_delta =
              hol_workbench_delta hol_workbench_before_definitions hol_workbench_after_definitions
            and hol_workbench_type_delta =
              hol_workbench_delta hol_workbench_before_types hol_workbench_after_types
            and hol_workbench_constant_delta =
              hol_workbench_delta hol_workbench_before_constants hol_workbench_after_constants in
            let hol_workbench_valid =
              hol_workbench_axiom_delta <> None && hol_workbench_definition_delta <> None &&
              hol_workbench_type_delta <> None && hol_workbench_constant_delta <> None in
            hol_workbench_emit
              (if hol_workbench_valid then "ok" else "invalid")
              (hol_workbench_value hol_workbench_axiom_delta)
              (hol_workbench_value hol_workbench_definition_delta)
              (hol_workbench_value hol_workbench_type_delta)
              (hol_workbench_value hol_workbench_constant_delta)
      with _ ->
        (try hol_workbench_emit "invalid" (-1) (-1) (-1) (-1) with _ -> ()));
     if hol_workbench_source_completed then print_endline {done_literal};
"""


def account_foundation_delta(transcript: bytes, contract: dict[str, Any]) -> dict[str, Any]:
    """Parse one exact nonce-bound marker for a fail-closed advisory."""

    foundation_contract = contract.get("foundation_delta")
    if not isinstance(foundation_contract, dict):
        return {
            "schema": FOUNDATION_DELTA_SCHEMA,
            "status": "unavailable",
            "marker_count": 0,
            "deltas": None,
            "advisory_reasons": ["runtime foundation delta contract is unavailable"],
        }
    marker_prefix = str(foundation_contract.get("marker_prefix") or "")
    if FOUNDATION_MARKER_PREFIX_RE.fullmatch(marker_prefix) is None:
        return {
            "schema": FOUNDATION_DELTA_SCHEMA,
            "status": "invalid_contract",
            "marker_count": 0,
            "deltas": None,
            "advisory_reasons": ["runtime foundation delta contract is malformed"],
        }
    marker_prefix_bytes = marker_prefix.encode("ascii")
    pattern = re.compile(
        rb"^"
        + re.escape(marker_prefix_bytes)
        + rb"(ok|invalid):(-?[0-9]{1,18}):(-?[0-9]{1,18}):(-?[0-9]{1,18}):(-?[0-9]{1,18})$"
    )
    candidates: list[tuple[int, bytes]] = []
    for lineno, line in enumerate(transcript.splitlines(), 1):
        if line.startswith(marker_prefix_bytes):
            candidates.append((lineno, line))
    if len(candidates) != 1:
        status = "missing" if not candidates else "ambiguous"
        deltas = None
        reasons = [f"runtime foundation delta marker is {status}"]
        observed_line = None
    else:
        observed_line, candidate = candidates[0]
        match = pattern.fullmatch(candidate)
        if match is None:
            status = "malformed"
            deltas = None
            reasons = ["runtime foundation delta marker is malformed"]
        else:
            values = tuple(int(match.group(index)) for index in range(2, 6))
            wrapper_status = match.group(1).decode("ascii")
            if wrapper_status == "ok" and all(value >= 0 for value in values):
                status = "observed"
                deltas = dict(zip(FOUNDATION_CATEGORIES, values, strict=True))
                reasons = []
                if deltas["axioms"] > 0:
                    reasons.append(f"recorded replay payload introduced {deltas['axioms']} runtime axiom(s)")
            else:
                status = "invalid"
                deltas = None
                reasons = ["runtime foundation registries were not exact prepend-extensions"]
    return {
        "schema": FOUNDATION_DELTA_SCHEMA,
        "status": status,
        "marker_count": len(candidates),
        "observed_transcript_line": observed_line,
        "deltas": deltas,
        "new_axiom_count": deltas.get("axioms") if deltas is not None else None,
        "advisory_reasons": reasons,
        "measurement": foundation_contract.get("measurement"),
        "baseline_boundary": foundation_contract.get("baseline_boundary"),
        "advisory_policy": foundation_contract.get("advisory_policy"),
        "inventory_boundary": foundation_contract.get("inventory_boundary"),
        "natural_output_is_evidence": False,
    }
