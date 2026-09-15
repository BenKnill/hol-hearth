#!/usr/bin/env python3
"""Exercise the Dune-built OCaml evaluator without loading HOL."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _evaluate(probe: Path, source: str) -> str:
    completed = subprocess.run(
        [str(probe)],
        input=source,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=5,
    )
    output = completed.stdout.strip()
    if completed.returncode != 0:
        raise AssertionError(f"native evaluator exited {completed.returncode}: {output}")
    return output


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "linux":
        print("native-eval-selftest: Linux-only; refusing to run outside Linux", file=sys.stderr)
        return 2

    args = list(argv or [])
    if len(args) != 1:
        print("usage: native_eval_selftest.py PROBE", file=sys.stderr)
        return 2
    probe = Path(args[0]).expanduser().resolve()
    if not probe.is_file():
        print(f"native-eval-selftest: Dune probe is missing: {probe}", file=sys.stderr)
        return 2

    valid = _evaluate(probe, "let x = 1;; x + 1;;\n")
    _require(
        valid == "OK phrases=2 hol=accepted scope=complete-source",
        f"unexpected valid-source card: {valid}",
    )

    malformed = _evaluate(probe, "let broken = ;;\n")
    _require(malformed.startswith("FAIL line=1 "), f"malformed source lacks line 1: {malformed}")
    _require("Syntax error" in malformed, f"malformed source lacks a syntax error: {malformed}")

    later_malformed = _evaluate(probe, "let x = 1;;\n\nlet broken = ;;\n")
    _require(later_malformed.startswith("FAIL line=3 "), f"line context drifted: {later_malformed}")
    _require("Syntax error" in later_malformed, f"line-context probe lacks a syntax error: {later_malformed}")

    print("native_eval_selftest=passed valid_phrases=2 malformed_line=1 later_malformed_line=3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
