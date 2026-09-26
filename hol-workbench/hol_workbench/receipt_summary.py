"""One curated summary of a recorded receipt: the verdict, the bindings, the failure.

The receipt file keeps every observation. This module answers the questions a
reader actually asks, in a fixed small shape shared by ``prove``, ``inspect``
and the ``verdict`` block written into the receipt itself.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hol_workbench.hashing import short_sha256

SUMMARY_SCHEMA = "hol-hearth.receipt-summary.v1"
MAX_FAILURE_CHARS = 1200
MAX_LINE_FAILURE_CHARS = 220
MAX_GOAL_CHARS = 300
STATUS_ORDER = ("proved", "failed", "printed_unprobed", "missing", "unknown")


@dataclass(frozen=True)
class BindingSummary:
    name: str
    status: str  # proved | failed | unverified | not_reached | missing | unknown
    recorded_status: str  # the receipt's own probe status, for counts
    line: int | None
    detail: str | None = None


@dataclass(frozen=True)
class ReceiptSummary:
    verdict: str  # passed | failed | incomplete | refused
    line: str
    source: str | None
    source_name: str
    source_sha256: str | None
    profile: str | None
    bindings_proved: int
    bindings_total: int
    bindings: list[BindingSummary]
    binding_counts: dict[str, int]
    new_axioms: int | None
    eval_seconds: float | None
    wait_seconds: float | None
    failing_binding: dict[str, Any] | None
    first_failure: str | None
    first_failure_transcript_line: int | None
    failing_step: dict[str, Any] | None
    reason: str | None
    basis: str | None
    inputs: int | None
    closure_sha256: str | None
    receipt: str | None
    transcript: str | None
    next: str | None = None
    schema: str = field(default=SUMMARY_SCHEMA)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema"] = self.schema
        return payload

    def json(self) -> str:
        return json.dumps(self.to_json(), indent=2, sort_keys=True)


def _squash(text: object, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _int(value: object) -> int | None:
    return value if type(value) is int else None


def _float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _source_lines(receipt: dict[str, Any]) -> dict[str, int]:
    accounting = (receipt.get("transcript_accounting") or {}).get("claim_accounting") or []
    lines: dict[str, int] = {}
    for row in accounting:
        if not isinstance(row, dict):
            continue
        span = row.get("source_span")
        if isinstance(span, list) and span and type(span[0]) is int and isinstance(row.get("theorem"), str):
            lines[row["theorem"]] = span[0]
    return lines


def _passed(receipt: dict[str, Any]) -> bool:
    semantic = bool(
        receipt.get("semantic_source_status") == "succeeded"
        and receipt.get("source_completed")
        and receipt.get("completion_marker_valid")
        and receipt.get("claims_complete")
        and receipt.get("semantic_exit_status") == 0
    )
    exit_fields = ("exit_status", "worker_exit_status", "process_exit_status")
    recorded = {name: receipt[name] for name in exit_fields if name in receipt}
    if not recorded:
        return semantic  # legacy receipt without process exit records
    return semantic and len(recorded) == len(exit_fields) and all(recorded[n] == 0 for n in exit_fields)


def _failing(receipt: dict[str, Any]) -> dict[str, Any] | None:
    attribution = receipt.get("failing_binding") or (receipt.get("transcript_accounting") or {}).get("failing_binding")
    if isinstance(attribution, dict) and attribution.get("status") == "identified" and attribution.get("name"):
        return {
            "name": attribution["name"],
            "source": attribution.get("source"),
            "source_line": _int(attribution.get("source_line")),
        }
    return None


def _failing_step(receipt: dict[str, Any]) -> dict[str, Any] | None:
    diagnostics = receipt.get("proof_diagnostics") or {}
    events = diagnostics.get("events") or [] if isinstance(diagnostics, dict) else []
    if diagnostics.get("status") != "recorded" or diagnostics.get("capture_truncated") or not events:
        return None
    event = events[-1]
    failure_line = receipt.get("first_failure_transcript_line")
    if type(failure_line) is not int or event.get("following_exception_transcript_line") != failure_line:
        return None
    steps = event.get("steps") or []
    if not steps:
        return None
    step = steps[0]
    return {
        "source_line": _int(step.get("source_line")),
        "conclusion": _squash(step.get("conclusion"), MAX_GOAL_CHARS),
        "assumption_count": _int(step.get("assumption_count")),
        "exception": _squash(step.get("exception"), MAX_LINE_FAILURE_CHARS),
    }


def _bindings(receipt: dict[str, Any], failing: dict[str, Any] | None) -> list[BindingSummary]:
    lines = _source_lines(receipt)
    failing_name = failing["name"] if failing else None
    failing_line = failing.get("source_line") if failing else None
    rows: list[BindingSummary] = []
    for row in receipt.get("bindings") or []:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            continue
        name = row["name"]
        recorded = str(row.get("status") or "unknown")
        line = lines.get(name)
        detail = None
        if recorded == "proved":
            status = "proved"
            kind = row.get("verification_kind")
            if kind == "binding_and_thm_type_only_nonliteral_statement":
                detail = "thm bound (conclusion and hypotheses not checked)"
            elif kind == "kernel_conclusion_and_empty_hypotheses":
                detail = "source conclusion matched; hypotheses empty"
        elif recorded == "printed_unprobed":
            status = "printed_unprobed"
            detail = "printed a thm; no kernel probe ran"
        elif recorded == "failed":
            status = "failed"
        elif recorded == "missing":
            if failing_name and name == failing_name:
                status = "failed"
            elif failing_line is not None and line is not None and line > failing_line:
                status = "not_reached"
            else:
                status = "missing"
                if row.get("unverified_binding_like_text_observed") is True:
                    detail = "unverified binding-like text observed"
        else:
            status = recorded
        rows.append(BindingSummary(name=name, status=status, recorded_status=recorded, line=line, detail=detail))
    return rows


def _verdict(receipt: dict[str, Any], failing: dict[str, Any] | None) -> tuple[str, str | None]:
    """Return (verdict, reason)."""
    transport = str(receipt.get("transport_status") or receipt.get("transport") or "")
    preflight = receipt.get("source_preflight_status")
    if receipt.get("evidence") == "pre_eval_dependency_transport_refusal" or (
        receipt.get("semantic_source_status") == "not_started" and preflight
    ):
        if preflight in {"source_changed_during_capture", "source_pin_refused"}:
            pin = receipt.get("source_pin") if isinstance(receipt.get("source_pin"), dict) else {}
            pinned = short_sha256(str(pin.get("pinned_sha256") or "")) or "?"
            read = short_sha256(str(pin.get("read_sha256") or "")) or "?"
            return "refused", (
                f"source changed during capture (pinned sha={pinned}, read sha={read}); no HOL ran"
            )
        reason = receipt.get("dependency_transport_reason") or receipt.get("first_failure") or "refused before HOL"
        return "refused", _squash(reason, MAX_FAILURE_CHARS)
    if transport in {"timeout", "interrupted", "cancelled"}:
        budget = _float(receipt.get("requested_timeout_seconds"))
        spent = _float(receipt.get("eval_elapsed_seconds"))
        if transport == "timeout":
            return "incomplete", f"timeout after {budget:g}s" if budget else "timeout"
        return "incomplete", f"{transport}" + (f" after {spent:.1f}s" if spent is not None else "")
    if _passed(receipt):
        return "passed", None
    if receipt.get("source_completed") and receipt.get("completion_marker_valid"):
        exits = [receipt.get(n) for n in ("exit_status", "worker_exit_status", "process_exit_status")]
        nonzero = [e for e in exits if type(e) is int and e != 0]
        if nonzero:
            return "failed", f"the source completed but the worker exited with status {nonzero[0]}"
        if not receipt.get("claims_complete"):
            return "failed", "the source completed but not every named binding was probed"
        return "failed", "the source completed but the recorded checks did not all pass"
    if receipt.get("first_failure"):
        return "failed", _squash(receipt["first_failure"], MAX_FAILURE_CHARS)
    if receipt.get("semantic_source_status") == "not_started":
        return "failed", "HOL evaluation did not start"
    return "failed", "the source did not complete"


def _axioms(receipt: dict[str, Any]) -> int | None:
    foundation = receipt.get("foundation_delta")
    if not isinstance(foundation, dict):
        return None
    deltas = foundation.get("deltas")
    if isinstance(deltas, dict):
        return _int(deltas.get("axioms"))
    return _int(foundation.get("new_axiom_count"))


def _axioms_text(count: int | None) -> str:
    if count is None:
        return "axiom delta not recorded"
    if count == 0:
        return "0 new axioms"
    return f"{count} NEW AXIOM{'S' if count != 1 else ''}"


def _verdict_line(
    verdict: str, *, name: str, profile: str | None, proved: int, total: int, axioms: int | None,
    eval_seconds: float | None, failing: dict[str, Any] | None, first_failure: str | None, reason: str | None,
) -> str:
    where = f" ({profile})" if profile else ""
    timing = f", eval {eval_seconds:.1f}s" if eval_seconds is not None else ""
    if verdict == "passed":
        proved_text = f"{proved}/{total} bindings proved" if total else "source evaluated, no named bindings"
        return f"PASSED {name}: {proved_text}, {_axioms_text(axioms)}{timing}{where}"
    if verdict == "incomplete":
        return f"INCOMPLETE {name}: {reason}; not a disproof{where}"
    if verdict == "refused":
        return f"REFUSED {name}: {_squash(reason, MAX_LINE_FAILURE_CHARS)}"
    if failing:
        line = f" (line {failing['source_line']})" if failing.get("source_line") else ""
        message = _squash(first_failure, MAX_LINE_FAILURE_CHARS) if first_failure else (reason or "failed")
        return f"FAILED {name} at {failing['name']}{line}: {message}"
    message = _squash(first_failure, MAX_LINE_FAILURE_CHARS) if first_failure else (reason or "failed")
    return f"FAILED {name}: {message}"


def summarize(receipt: dict[str, Any], *, receipt_path: Path | None = None) -> ReceiptSummary:
    source = receipt.get("source") if isinstance(receipt.get("source"), str) else None
    name = Path(source).name if source else "source"
    failing = _failing(receipt)
    verdict, reason = _verdict(receipt, failing)
    bindings = _bindings(receipt, failing if verdict == "failed" else None)
    counts = dict.fromkeys(STATUS_ORDER, 0)
    for row in bindings:
        counts[row.recorded_status] = counts.get(row.recorded_status, 0) + 1
    proved = counts.get("proved", 0)
    axioms = _axioms(receipt)
    eval_seconds = _float(receipt.get("eval_elapsed_seconds"))
    first_failure = receipt.get("first_failure") if isinstance(receipt.get("first_failure"), str) else None
    if verdict == "passed":
        first_failure = None
    closure = receipt.get("source_dependency_closure") if isinstance(receipt.get("source_dependency_closure"), dict) else {}
    inputs = None
    if closure:
        paths = {(closure.get("entrypoint") or {}).get("path")}
        paths.update(row.get("resolved_path") for row in closure.get("records") or [] if isinstance(row, dict))
        paths.discard(None)
        inputs = len(paths)
    basis = receipt.get("project_basis") if isinstance(receipt.get("project_basis"), dict) else None
    basis_source = ((basis or {}).get("identity") or {}).get("source") if basis else None
    profile = receipt.get("logical_profile") or receipt.get("physical_profile")
    return ReceiptSummary(
        verdict=verdict,
        line=_verdict_line(
            verdict, name=name, profile=profile if isinstance(profile, str) else None, proved=proved,
            total=len(bindings), axioms=axioms, eval_seconds=eval_seconds, failing=failing,
            first_failure=first_failure, reason=reason,
        ),
        source=source,
        source_name=name,
        source_sha256=receipt.get("source_sha256") if isinstance(receipt.get("source_sha256"), str) else None,
        profile=profile if isinstance(profile, str) else None,
        bindings_proved=proved,
        bindings_total=len(bindings),
        bindings=bindings,
        binding_counts=counts,
        new_axioms=axioms,
        eval_seconds=eval_seconds,
        wait_seconds=_float(receipt.get("admission_wait_seconds")),
        failing_binding=failing if verdict == "failed" else None,
        first_failure=_squash(first_failure, MAX_FAILURE_CHARS) if first_failure else None,
        first_failure_transcript_line=_int(receipt.get("first_failure_transcript_line")),
        failing_step=_failing_step(receipt) if verdict == "failed" else None,
        reason=reason,
        basis=basis_source if isinstance(basis_source, str) else None,
        inputs=inputs,
        closure_sha256=receipt.get("source_dependency_closure_sha256")
        if isinstance(receipt.get("source_dependency_closure_sha256"), str) else None,
        receipt=str(receipt_path) if receipt_path is not None else None,
        transcript=receipt.get("transcript") if isinstance(receipt.get("transcript"), str) else None,
    )


def next_command(summary: ReceiptSummary, *, run_root: Path | None, public_command: Any) -> str | None:
    """The one command a reader most likely wants next; None when there is nothing to do."""
    attempt = Path(summary.receipt).parent if summary.receipt else run_root
    if summary.verdict == "passed":
        return public_command("inspect", attempt) if attempt else None
    if summary.verdict == "failed":
        if summary.failing_binding and attempt:
            return public_command("reopen", attempt, "--binding", summary.failing_binding["name"])
        return public_command("inspect", attempt, "--verbose") if attempt else None
    if summary.verdict == "incomplete" and summary.source:
        budget = None
        if summary.reason and "timeout after " in summary.reason:
            try:
                budget = float(summary.reason.split("timeout after ", 1)[1].rstrip("s"))
            except ValueError:
                budget = None
        args = [summary.source]
        if summary.profile:
            args += ["--profile", summary.profile]
        if budget:
            args += ["--timeout", f"{budget * 2:g}"]
        if run_root:
            args += ["--run-root", run_root]
        return public_command("prove", *args)
    if summary.verdict == "refused" and summary.reason and summary.reason.startswith("source changed"):
        return "rerun the same prove command once the file is stable"
    return public_command("inspect", attempt, "--verbose") if attempt else None


def render_card(
    summary: ReceiptSummary, receipt: dict[str, Any], *, selected: set[str] | None = None, all_bindings: bool = False,
) -> list[str]:
    """The default inspect card: verdict first, then only what changes what the reader does."""
    selected = selected or set()
    lines = [summary.line]
    if summary.source:
        short = short_sha256(summary.source_sha256) if summary.source_sha256 else None
        lines.append(f"source: {summary.source}" + (f" sha={short}" if short else ""))
    rows = [row for row in summary.bindings if row.name in selected] if selected else list(summary.bindings)
    if not selected:
        rows.sort(key=lambda row: (row.status == "proved", row.line or 0))
    visible = rows if (all_bindings or selected) else rows[:12]
    accounting = (receipt.get("transcript_accounting") or {}).get("claim_accounting") or []
    claims = {row.get("theorem"): row for row in accounting if isinstance(row, dict)}
    for row in visible:
        label = "not reached (after the failure)" if row.status == "not_reached" else row.status
        if row.detail and row.status == "proved":
            # Probe strength matters when a reader asks about that binding; a
            # weaker-than-usual probe is always shown.
            if row.detail.startswith("thm bound"):
                label = row.detail
            elif row.name in selected:
                label += f" ({row.detail})"
        elif row.detail:
            label += f" ({row.detail})"
        location = f" source_line={row.line}" if row.line is not None else ""
        lines.append(f"  {row.name}: {label}{location}")
        raw = next((b for b in receipt.get("bindings") or [] if isinstance(b, dict) and b.get("name") == row.name), {})
        if row.status == "printed_unprobed":
            for item in (raw.get("printed_output") or [])[-1:]:
                if isinstance(item, dict) and item.get("printed_conclusion"):
                    lines.append("    printed_conclusion (unverified): " + _squash(item["printed_conclusion"], 1600))
        if row.name in selected:
            if raw.get("verification_kind"):
                lines.append(f"    verification_kind: {raw['verification_kind']}")
            statement = (claims.get(row.name) or {}).get("statement")
            if statement:
                lines.append("    source_statement: " + _squash(statement, 1600))
    if len(rows) > len(visible):
        hidden = rows[len(visible):]
        counts: dict[str, int] = {}
        for row in hidden:
            counts[row.status] = counts.get(row.status, 0) + 1
        lines.append(f"  ... {len(hidden)} more ({', '.join(f'{c} {s}' for s, c in counts.items())}); "
                     "use --verbose or --binding NAME")
    for name in sorted(selected - {row.name for row in rows}):
        lines.append(f"  {name}: not recorded (not a claim of theorem absence)")
    if summary.bindings_total:
        lines.append("binding_counts: " + " ".join(f"{k}={summary.binding_counts.get(k, 0)}" for k in STATUS_ORDER))
        probe_counts = dict.fromkeys(("conclusion_checked", "thm_type_only", "unknown"), 0)
        kinds = {
            "kernel_conclusion_and_empty_hypotheses": "conclusion_checked",
            "binding_and_thm_type_only_nonliteral_statement": "thm_type_only",
        }
        for raw in receipt.get("bindings") or []:
            if isinstance(raw, dict) and raw.get("status") == "proved":
                probe_counts[kinds.get(str(raw.get("verification_kind")), "unknown")] += 1
        lines.append("successful_probe_counts: " + " ".join(f"{k}={v}" for k, v in probe_counts.items()))
    if summary.verdict == "failed":
        if summary.failing_binding:
            fb = summary.failing_binding
            lines.append(f"failing_binding: {fb['name']} source={fb.get('source')}:{fb.get('source_line')}")
        else:
            attribution = receipt.get("failing_binding")
            reason = attribution.get("reason") if isinstance(attribution, dict) else None
            lines.append("failing_binding: unknown" + (f" ({reason})" if reason else " (no reliable location recorded)"))
        if summary.first_failure:
            coordinate = (f"transcript_line={summary.first_failure_transcript_line} "
                          if summary.first_failure_transcript_line is not None else "")
            lines.append(f"first_failure: {coordinate}{summary.first_failure}")
        elif summary.reason:
            lines.append(f"reason: {summary.reason}")
        if not summary.failing_binding:
            unverified = [
                (line, row["name"]) for row in receipt.get("bindings") or [] if isinstance(row, dict)
                for line in (row.get("unverified_binding_like_transcript_lines") or []) if type(line) is int
            ]
            if unverified:
                line, name = max(unverified)
                lines.append(f"last_binding_like_text: {name} (unverified, transcript_line={line})")
            if summary.bindings and all(row.recorded_status in {"missing", "unknown", "printed_unprobed"}
                                        for row in summary.bindings):
                lines.append("binding_note: named theorem probes are missing; unverified printed theorem text "
                             "does not establish which binding failed")
    if summary.verdict == "incomplete":
        running = receipt.get("running_binding") or {}
        if isinstance(running, dict) and running.get("status") == "running_at_interruption" and running.get("name"):
            lines.append(f"running_at_interruption: {running['name']} "
                         f"source={running.get('source')}:{running.get('source_line')}")
    if summary.verdict == "refused" and summary.reason and summary.reason.startswith("source changed"):
        lines.append("source_changed_during_capture: the file was rewritten between the pinned digest and the "
                     "evaluation read; nothing stale was evaluated. Not a proof failure.")
    if summary.inputs:
        digest = short_sha256(summary.closure_sha256) or "unknown"
        lines.append(f"inputs: {summary.inputs} files; closure_sha={digest}")
    basis = receipt.get("project_basis")
    if isinstance(basis, dict):
        identity = basis.get("identity") or {}
        lines.append(f"inherited_project_basis: {identity.get('source')} sha={short_sha256(identity.get('source_sha256'))}")
        lines.append(f"basis_preparation_receipt: {basis.get('preparation_receipt')}")
        lines.append("basis_scope: preparation was checked separately; this attempt checks the full leaf in a fresh child")
    if summary.receipt:
        lines.append(f"receipt: {summary.receipt}")
    if summary.next:
        lines.append(f"NEXT: {summary.next}")
    return lines
