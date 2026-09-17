"""Nonce-bound claim accounting for warm raw-HOL routes."""

from __future__ import annotations

from typing import Any

from hol_workbench.foundation_delta import account_foundation_delta, foundation_probe_contract
from hol_workbench.vanilla_claims import (
    EVIDENCE_BOUNDARY,
    account_claims,
    build_claim_probe,
    binding_status_counts,
    first_error,
)

RECORDED_REPLAY_EVIDENCE_BOUNDARY = (
    f"{EVIDENCE_BOUNDARY}; advisory runtime logical-foundation registry cardinality deltas; "
    "warm development evidence only, not final audit or promotion authority"
)


def instrumented_source_bytes(
    source_bytes: bytes,
    claims: list[dict[str, Any]],
    *,
    nonce: str | None = None,
    prefix_bytes: bytes = b"",
    include_foundation_delta: bool = False,
) -> tuple[bytes, dict[str, Any]]:
    """Optionally add recorded-replay telemetry to the shared claim probe."""

    payload, contract = build_claim_probe(source_bytes, claims, nonce=nonce, prefix_bytes=prefix_bytes)
    if include_foundation_delta:
        contract["foundation_delta"] = foundation_probe_contract(str(contract["nonce"]))
    return payload, contract


def completion_observed(transcript: bytes, contract: dict[str, Any]) -> bool:
    marker = str(contract["completion_marker"]).encode("ascii")
    return sum(line == marker for line in transcript.splitlines()) == 1


def included_file_error_observed(transcript: bytes) -> bool:
    return any(b"Error in included file" in line for line in transcript.splitlines())


def displayed_transcript(transcript: bytes, contract: dict[str, Any]) -> str:
    hidden = {str(contract["completion_marker"]).encode("ascii")}
    for row in contract.get("claims") or []:
        hidden.add(str(row["ok_marker"]).encode("ascii"))
        hidden.add(str(row["mismatch_marker"]).encode("ascii"))
    foundation = contract.get("foundation_delta")
    foundation_prefix = (
        str(foundation.get("marker_prefix") or "").encode("ascii") if isinstance(foundation, dict) else b""
    )
    rendered = b"\n".join(
        line
        for line in transcript.splitlines()
        if line not in hidden and not (foundation_prefix and line.startswith(foundation_prefix))
    )
    if rendered and transcript.endswith((b"\n", b"\r")):
        rendered += b"\n"
    return rendered.decode("utf-8", errors="replace")


def analyze_vanilla_transcript(
    *,
    claims: list[dict[str, Any]],
    transcript: bytes,
    contract: dict[str, Any],
    transport: str,
    response: dict[str, Any] | None,
) -> dict[str, Any]:
    claim_docs, accounting = account_claims(claims, transcript, contract)
    foundation_enabled = isinstance(contract.get("foundation_delta"), dict)
    diagnostic = transcript.decode("utf-8", errors="replace")
    failure_like_lineno, failure_like_line = first_error(diagnostic)
    included_file_error = included_file_error_observed(transcript)
    source_completed = (
        transport == "completed" and bool(accounting["completion_marker_valid"]) and not included_file_error
    )
    claims_complete = all(row["status"] == "proved" for row in claim_docs)
    observed_probe_bindings = [doc["theorem"] for doc in claim_docs if doc["status"] == "proved"]
    semantic_success = source_completed and claims_complete
    process_exit_status = response.get("exit_status") if response else None
    clean_process_exit = type(process_exit_status) is int and process_exit_status == 0
    if semantic_success:
        source_status = "succeeded"
    elif source_completed:
        source_status = "claims_incomplete"
    else:
        source_status = "failed" if transport == "completed" else "not_completed"
    semantic_exit_status = 0 if semantic_success else 1
    if transport == "interrupted":
        effective_exit_status = 130
    elif isinstance(process_exit_status, int) and process_exit_status != 0:
        effective_exit_status = process_exit_status
    else:
        effective_exit_status = semantic_exit_status
    bindings = []
    for doc in claim_docs:
        row: dict[str, Any] = {
            "name": doc["theorem"],
            "status": doc["status"],
            "evidence": doc["evidence"],
            "verification_kind": doc["verification_kind"],
            "observed_transcript_line": doc["observed_transcript_line"],
        }
        diagnostic_lines = list(doc.get("natural_output_diagnostic_lines") or [])
        if diagnostic_lines:
            row["unverified_binding_like_text_observed"] = True
            row["unverified_binding_like_transcript_lines"] = diagnostic_lines
            row["printed_output"] = doc.get("natural_output_diagnostic") or []
        bindings.append(row)
    result = {
        "source_status": source_status,
        "source_completed": source_completed,
        "claims_complete": claims_complete,
        "semantic_exit_status": semantic_exit_status,
        "effective_exit_status": effective_exit_status,
        "transport_status": transport,
        "process_exit_status": process_exit_status,
        "completion_marker_observed": accounting["completion_marker_observed"],
        "completion_marker_valid": accounting["completion_marker_valid"] and not included_file_error,
        "completion_marker_count": accounting["completion_marker_count"],
        "completion_invariant": "exact nonce-bound full-line marker occurs exactly once in raw transcript bytes",
        "included_file_error_observed": included_file_error,
        "first_failure": None if source_completed else failure_like_line,
        "first_failure_transcript_line": None if source_completed else failure_like_lineno,
        "failure_like_text_observed": failure_like_line is not None,
        "failure_like_text_first_line": failure_like_line,
        "failure_like_text_first_transcript_line": failure_like_lineno,
        "failure_like_text_ignored_after_completed_source": source_completed and failure_like_line is not None,
        "observed_bindings": observed_probe_bindings,
        "bindings": bindings,
        "binding_counts": binding_status_counts(bindings),
        "failing_binding": None if source_completed else {
            "status": "unknown",
            "name": None,
            "reason": "no reliable source binding location was recorded; preceding printed text is not attribution",
        },
        "claim_accounting": claim_docs,
        "probe_contract": contract,
        "natural_output_is_evidence": False,
        "evidence": "nonce_bound_claim_probe",
        "evidence_boundary": EVIDENCE_BOUNDARY,
    }
    if foundation_enabled:
        foundation_delta = account_foundation_delta(transcript, contract)
        advisory_reasons = list(foundation_delta["advisory_reasons"])
        if not semantic_success:
            advisory_reasons.append("recorded replay did not complete successfully")
        if not clean_process_exit:
            detail = process_exit_status if isinstance(process_exit_status, int) else "unavailable"
            advisory_reasons.append(f"worker process exit status is {detail}, not zero")
        if foundation_delta["status"] == "observed" and foundation_delta.get("new_axiom_count", 0) > 0:
            recommendation = "block"
        elif foundation_delta["status"] == "observed" and semantic_success and clean_process_exit:
            recommendation = "no_foundation_objection"
        else:
            recommendation = "manual_review"
        result.update(
            {
                "foundation_delta": foundation_delta,
                "promotion_advisory": {
                    "authority": "advisory_only",
                    "recommendation": recommendation,
                    "reasons": list(dict.fromkeys(advisory_reasons)),
                    "policy": (
                        "block on an observed positive axiom delta, report no foundation objection on an observed "
                        "zero delta after a successful zero-exit replay, and otherwise require manual review"
                    ),
                    "does_not_change_replay_result": True,
                },
                "evidence_boundary": RECORDED_REPLAY_EVIDENCE_BOUNDARY,
            }
        )
    return result
