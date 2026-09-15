#!/usr/bin/env python3
"""Run original demo proofs against an existing warm HOL profile."""
from __future__ import annotations
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main() -> int:
    parser = argparse.ArgumentParser(prog="hearth demo", description=__doc__)
    parser.add_argument("name", choices=("cat-map", "balance", "failure", "repaired"), nargs="?", default="cat-map")
    parser.add_argument("--run-root", type=Path)
    parser.add_argument("--inspect", action="store_true", help="Also print the full receipt inspection")
    args = parser.parse_args()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_root = (args.run_root or ROOT / "runs" / f"{args.name}-{stamp}").resolve()
    if run_root.exists() and (not run_root.is_dir() or any(run_root.iterdir())):
        parser.error("--run-root must be absent or empty to avoid confusing old receipts with this run")
    code = subprocess.run([str(ROOT / "hearth"), "prove", str(ROOT / "demos" / f"{args.name}.ml"),
                           "--profile", "light", "--run-root", str(run_root)], check=False).returncode
    receipts = list(run_root.rglob("transcript.log.json"))
    if len(receipts) != 1:
        print("Demo did not produce exactly one receipt; no theorem result claimed.", file=sys.stderr)
        return code or 1
    receipt = json.loads(receipts[0].read_text())
    if args.inspect:
        subprocess.run([str(ROOT / "hearth"), "inspect", str(run_root)], check=False)
    succeeded = receipt.get("semantic_source_status") == "succeeded" and receipt.get("source_completed") is True
    if args.name == "failure":
        # Refusal, transport failure, and missing runtime are not the expected HOL rejection.
        rejected = receipt.get("semantic_source_status") == "failed" and bool(receipt.get("first_failure"))
        if code and rejected:
            print("DEMO: passed (expected HOL rejection). Next: ./hearth demo repaired")
            return 0
        print("Expected a HOL source failure, but did not obtain that evidence.", file=sys.stderr)
        return 1
    if code or not succeeded:
        return code or 1
    print("Demo passed: complete source accepted in the configured warm HOL environment.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
