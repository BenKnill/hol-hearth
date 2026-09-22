"""Bounded mechanical acceptance probe for one restored fork-basis broker."""

from __future__ import annotations

import argparse
import json
import secrets
from pathlib import Path

from hol_workbench.proof_run_fork_broker_client import prepare_broker_spawn, run_broker_attempt


def run_broker_capture_request(
    session: Path,
    request: dict,
    *,
    timeout_seconds: float | None,
    expected: dict | None = None,
) -> dict:
    """Run one policy-free build capture through the mechanical broker."""

    source = Path(str(request["source"])).expanduser().resolve()
    transcript = Path(str(request["transcript_path"])).expanduser().resolve()
    attempt_id = str(request.get("attempt_id") or f"broker-capture-{secrets.token_hex(6)}")
    broker_output = transcript.with_suffix(".broker.raw")
    raw_source = bool(request.get("raw_source", False))
    source_phrase_builder = None
    if not raw_source:
        from hol_workbench.source_load_transport import source_load_phrase

        source_phrase_builder = source_load_phrase
    spawn = prepare_broker_spawn(
        source=source,
        broker_output=broker_output,
        attempt_id=attempt_id,
        raw_source=raw_source,
        source_phrase_builder=source_phrase_builder,
        foundation_marker_prefix=(
            str(request["foundation_marker_prefix"]) if request.get("foundation_marker_prefix") else None
        ),
    )
    result = run_broker_attempt(
        session,
        spawn=spawn,
        transcript=transcript,
        response_timeout_seconds=float(timeout_seconds or 120.0),
        expected=expected,
    )
    output = transcript.read_bytes()
    broker_bytes = broker_output.read_bytes()
    final = result["final"]
    child = final.get("child") if isinstance(final.get("child"), dict) else {}
    quiescent = final.get("child_quiescent") is True and final.get("seat_reusable") is True
    sentinel = spawn["done_token"].encode() in output
    raw_output_byte_exact = output == broker_bytes
    cancellation_reason = result.get("controller_cancellation_reason")
    if not quiescent or not raw_output_byte_exact:
        status = "lifecycle_error"
        exit_status = 126
    elif cancellation_reason == "controller_response_timeout":
        status = "timeout"
        exit_status = 124
    else:
        status = "ok" if sentinel else "prompt_lost"
        exit_status = 0 if sentinel else 125
    return {
        "status": status,
        "exit_status": exit_status,
        "timeout_kind": "controller_response_timeout" if status == "timeout" else None,
        "controller_cancellation_reason": cancellation_reason,
        "controller_response_timeout_seconds": float(timeout_seconds or 120.0),
        "requested_timeout_seconds": request.get("timeout_seconds"),
        "termination_kind": final.get("termination"),
        "broker_child_quiescent": final.get("child_quiescent"),
        "broker_seat_reusable": final.get("seat_reusable"),
        "hol_pid": child.get("pid"),
        "hol_pgid": child.get("pgid"),
        "sentinel_observed": sentinel,
        "child_cleanup": final.get("child_cleanup"),
        "child_quiescent": quiescent,
        "cleanup_verified": quiescent,
        "seat_reusable": quiescent,
        "eval_elapsed_seconds": result.get("elapsed_seconds"),
        "transcript_path": str(transcript),
        "broker_output_path": str(broker_output),
        "raw_output_byte_exact": raw_output_byte_exact,
        "engine": "fork_basis",
        "broker_result": result,
    }


def run_probe(*, session: Path, source: Path, transcript: Path) -> dict:
    attempt_id = f"broker-probe-{secrets.token_hex(6)}"
    response = run_broker_capture_request(
        session,
        {
            "attempt_id": attempt_id,
            "source": str(source),
            "transcript_path": str(transcript),
        },
        timeout_seconds=120.0,
    )
    passed = response["status"] == "ok"
    return {
        "schema": "hol-workbench.fork-basis-broker-probe.v1",
        "status": "passed" if passed else "failed",
        "attempt_id": attempt_id,
        "source": str(source),
        "transcript": str(transcript),
        "broker_output": response["broker_output_path"],
        "raw_output_byte_exact": response["raw_output_byte_exact"],
        "done_token_observed": response["sentinel_observed"],
        "result": response["broker_result"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    receipt = run_probe(
        session=args.session.expanduser().resolve(),
        source=args.source.expanduser().resolve(),
        transcript=args.transcript.expanduser().resolve(),
    )
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    transcript_text = args.transcript.read_text(encoding="utf-8", errors="replace")
    print(transcript_text, end="" if transcript_text.endswith("\n") else "\n")
    print(f"status: {'proved' if receipt['status'] == 'passed' else 'failed'}")
    print(f"broker probe receipt: {args.receipt}")
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
