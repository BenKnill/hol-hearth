"""Run snapshot and diff helpers for proof-run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hol_workbench.evidence_policy import RAW_LOG_POLICY
from hol_workbench.jsonio import read_json

RUN_DIFF_SCHEMA = "proof-cockpit.run-diff.v1"
DIFF_FIELDS = [
    "status",
    "target_status",
    "phase",
    "claim_name",
    "source_sha256",
    "statement_sha256",
    "claim_verification_status",
    "proof_exit_status",
    "child_exit_status",
    "proof_final_phase",
    "failure_kind",
    "failure_class",
    "doctor_status",
    "doctor_blockers",
    "command_argv",
    "command_environment",
    "raw_log_lines",
    "event_counts",
    "completed_bindings_tail",
    "claim_graph_schema",
    "claim_graph_edge_count",
    "static_edge_count",
    "observed_edge_count",
    "certified_edge_count",
    "certified_edge_status",
    "checkpoint_kind",
    "resume_scope",
    "resume_mechanism",
    "resume_supported",
    "replays_on_edit",
    "checkpoint_evidence_role",
    "completed_theorem_frontier",
    "observed_exact_source_attempt",
]


def load_run_snapshot(run_dir: Path) -> dict:
    run_dir = run_dir.resolve()
    run = read_json(run_dir / "run.json")
    if not run:
        raise SystemExit(f"run.json not found under {run_dir}")
    target_ref = (run.get("targets") or [{}])[0]
    target = read_json(Path(target_ref.get("target_json", "")))
    artifacts = target.get("artifacts") or {}
    claim = read_json(Path(target["claim"])) if target.get("claim") else {}
    summary = read_json(Path(artifacts["summary_json"])) if artifacts.get("summary_json") else {}
    failure = read_json(Path(artifacts["failure_json"])) if artifacts.get("failure_json") else {}
    checkpoint_data = read_json(Path(artifacts["checkpoint"])) if artifacts.get("checkpoint") else {}
    command = read_json(Path(artifacts["command_argv"])) if artifacts.get("command_argv") else {}
    doctor = read_json(Path(artifacts["doctor_json"])) if artifacts.get("doctor_json") else {}
    graph = read_json(Path(artifacts["claim_graph"])) if artifacts.get("claim_graph") else {}
    edge_layers = (graph.get("edge_extraction") or {}).get("layers") or {}
    return {
        "schema": "proof-cockpit.run-snapshot.v1",
        "run_id": run.get("run_id"),
        "run_dir": str(run_dir),
        "status": run.get("status"),
        "target_id": target.get("target_id") or target_ref.get("id"),
        "target_status": target.get("status") or target_ref.get("status"),
        "phase": target.get("phase"),
        "claim_name": target.get("claim_name"),
        "source_sha256": (target.get("source") or {}).get("sha256"),
        "statement_sha256": claim.get("statement_sha256"),
        "claim_verification_status": (target.get("claim_verification") or {}).get("status")
        or (claim.get("verification") or {}).get("status"),
        "proof_exit_status": target.get("proof_exit_status"),
        "child_exit_status": summary.get("child_exit_status"),
        "proof_final_phase": target.get("proof_final_phase") or summary.get("final_phase"),
        "failure_kind": failure.get("failure_kind"),
        "failure_class": (failure.get("failure_kind_detail") or {}).get("class"),
        "doctor_status": doctor.get("status"),
        "doctor_blockers": doctor.get("blockers") or [],
        "command_argv": command.get("argv"),
        "command_environment": command.get("environment"),
        "raw_log_lines": summary.get("raw_log_lines"),
        "event_counts": summary.get("event_counts") or {},
        "completed_bindings_tail": summary.get("completed_bindings_tail") or [],
        "claim_graph_schema": graph.get("schema"),
        "claim_graph_edge_count": len(graph.get("edges") or []),
        "static_edge_count": len(graph.get("static_edges") or []),
        "observed_edge_count": len(graph.get("observed_edges") or []),
        "certified_edge_count": len(graph.get("certified_edges") or []),
        "certified_edge_status": (edge_layers.get("certified_edges") or {}).get("status"),
        "events_jsonl": artifacts.get("events_jsonl"),
        "checkpoint": artifacts.get("checkpoint"),
        "checkpoint_kind": checkpoint_data.get("checkpoint_kind"),
        "resume_scope": checkpoint_data.get("resume_scope"),
        "resume_mechanism": checkpoint_data.get("resume_mechanism"),
        "resume_supported": checkpoint_data.get("resume_supported"),
        "replays_on_edit": checkpoint_data.get("replays_on_edit"),
        "checkpoint_evidence_role": checkpoint_data.get("evidence_role"),
        "completed_theorem_frontier": checkpoint_data.get("completed_theorem_frontier") or [],
        "observed_exact_source_attempt": checkpoint_data.get("observed_exact_source_attempt"),
    }


def diff_snapshots(left: dict, right: dict) -> list[dict]:
    changes = []
    for field in DIFF_FIELDS:
        if left.get(field) != right.get(field):
            changes.append({"field": field, "left": left.get(field), "right": right.get(field)})
    return changes


def diff_report(left: dict, right: dict) -> dict:
    return {
        "schema": RUN_DIFF_SCHEMA,
        "left": left,
        "right": right,
        "changes": diff_snapshots(left, right),
        "raw_log_policy": RAW_LOG_POLICY,
    }


def print_diff_report(report: dict) -> None:
    left = report["left"]
    right = report["right"]
    print("proof-run diff")
    print(f"left: {left['run_id']} ({left['status']})")
    print(f"right: {right['run_id']} ({right['status']})")
    print("")
    if report["changes"]:
        print("changed:")
        for change in report["changes"]:
            print(f"- {change['field']}: {change['left']!r} -> {change['right']!r}")
    else:
        print("changed: none")
    print("")
    print(f"left events: {left.get('events_jsonl')}")
    print(f"right events: {right.get('events_jsonl')}")
    print(f"raw log policy: {RAW_LOG_POLICY}")


def diff_command(args: argparse.Namespace) -> int:
    left = load_run_snapshot(Path(args.left_run_dir))
    right = load_run_snapshot(Path(args.right_run_dir))
    report = diff_report(left, right)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    print_diff_report(report)
    return 0
