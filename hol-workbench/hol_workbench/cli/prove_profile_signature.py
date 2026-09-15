"""Bounded exact theorem lookup through a named warm profile."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hol_workbench.filter_output_theorem_search import (
    SEARCH_RESULT_ITEM_RE,
    TRUNCATION_NOTICE,
    TheoremSearchCapture,
)
from hol_workbench.ids import run_id, utc_now
from hol_workbench.jsonio import atomic_write_json, read_json

THEOREM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_']*$")
UNBOUND_RE = re.compile(r"(?:Error:\s*)?Unbound value\s+([A-Za-z_][A-Za-z0-9_']*)")


@dataclass(frozen=True)
class QueryExecution:
    exit_status: int
    statement_block: list[str]
    names: list[str]
    failure: str | None


Executor = Callable[[list[str], Path, str], QueryExecution]


def query_source(theorem: str) -> str:
    if not THEOREM_NAME_RE.fullmatch(theorem):
        raise ValueError("theorem name must be one OCaml identifier")
    return (
        f'let HOL_WORKBENCH_SIGNATURE_RESULT : (string * thm) list = [("{theorem}", {theorem})];;\n'
        "let HOL_WORKBENCH_SIGNATURE_SENTINEL = prove(`T`,MESON_TAC[]);;\n"
    )


def _bounded_capture(lines: Iterable[str], theorem: str) -> tuple[list[str], list[str], str | None]:
    capture = TheoremSearchCapture()
    retained: list[str] = []
    names: list[str] = []
    failure = None
    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        match = UNBOUND_RE.search(line)
        if match and match.group(1) == theorem:
            failure = f"Unbound value {theorem}"
        elif line.strip().startswith(("prove:", "Error", "Exception", "Failure", "Fatal error")):
            failure = " ".join(line.split())[:420]
        handled, captured = capture.consume(line)
        if not handled:
            continue
        if captured:
            retained.append(captured)
        item = SEARCH_RESULT_ITEM_RE.match(line.lstrip())
        if item:
            names.append(item.group(1))
    return retained, names, failure


def run_bounded_query(
    command: list[str],
    cwd: Path,
    theorem: str,
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> QueryExecution:
    process = popen_factory(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if process.stdout is None:
        raise RuntimeError("theorem signature query did not expose a combined output stream")
    statement_block, names, failure = _bounded_capture(process.stdout, theorem)
    return QueryExecution(process.wait(), statement_block, names, failure)


def query_status(theorem: str, execution: QueryExecution) -> str:
    if execution.names == [theorem] and execution.exit_status == 0:
        return "found_truncated" if TRUNCATION_NOTICE in execution.statement_block else "found"
    if execution.failure == f"Unbound value {theorem}":
        return "not_found"
    if execution.names:
        return "ambiguous"
    return "query_failed"


def _render(report: dict[str, Any]) -> None:
    print("HOL theorem signature")
    print("=====================")
    print(f"profile: {report['profile']}")
    print(f"theorem: {report['theorem']}")
    print(f"status: {report['status']}")
    print(f"present in named warm basis: {'yes' if report['present_in_named_warm_basis'] else 'no'}")
    print("lookup: exact OCaml identifier (no substring ambiguity)")
    print("source: unavailable (profile metadata has no theorem provenance)")
    if report["statement_block"]:
        print("")
        print("bounded statement:")
        for line in report["statement_block"]:
            print(f"  {line}")
    elif report.get("failure"):
        print(f"failure: {report['failure']}")
    print(f"receipt: {report['receipt']}")


def run_theorem_signature_query(
    *,
    script_dir: str | Path,
    profile: str,
    theorem: str,
    run_root: str | Path,
    command_cwd: str | Path | None = None,
    executor: Executor = run_bounded_query,
) -> int:
    try:
        source_text = query_source(theorem)
    except ValueError as exc:
        print(f"prove profiles: {exc}")
        return 2

    cwd = Path(command_cwd or Path.cwd()).expanduser().resolve()
    root = Path(run_root).expanduser()
    if not root.is_absolute():
        root = cwd / root
    attempt_dir = root.resolve() / "theorem-signatures" / run_id(f"{profile}-{theorem}")
    attempt_dir.mkdir(parents=True, exist_ok=False)
    source = attempt_dir / "query.ml"
    source.write_text(source_text, encoding="utf-8")
    command = [
        str(Path(script_dir).expanduser().resolve() / "prove"),
        "--source",
        str(source),
        "--profile",
        profile,
        "--run-root",
        str(attempt_dir / "runs"),
        "--raw-output",
        "--warm-guard",
        "warn",
    ]
    execution = executor(command, cwd, theorem)
    status = query_status(theorem, execution)
    receipt = attempt_dir / "theorem-signature.json"
    route_path = attempt_dir / "runs" / "orbstack-criu-route.json"
    route = read_json(route_path) if route_path.exists() else {}
    present = status in {"found", "found_truncated"}
    if status == "ambiguous":
        fallback_failure = f"signature output contained unexpected or duplicate names: {execution.names}"
    elif status == "query_failed":
        fallback_failure = "profile evaluation produced no trustworthy theorem signature block"
    else:
        fallback_failure = None
    report: dict[str, Any] = {
        "schema": "hol-workbench.theorem-signature.v1",
        "created_utc": utc_now(),
        "profile": profile,
        "theorem": theorem,
        "status": status,
        "present_in_named_warm_basis": present,
        "lookup_mode": "exact_ocaml_identifier",
        "statement_block": execution.statement_block,
        "matched_names": execution.names,
        "source_location": None,
        "source_location_reason": "profile metadata has no theorem provenance",
        "failure": execution.failure or fallback_failure,
        "query_source": str(source),
        "run_root": str(attempt_dir / "runs"),
        "route_receipt": str(route_path) if route else None,
        "execution_backend": route.get("execution_backend"),
        "disposable_worker": "disposable" in str(route.get("execution_backend") or ""),
        "command": command,
        "execution_exit_status": execution.exit_status,
        "evidence_grade": "warm_exploration",
        "receipt": str(receipt),
    }
    atomic_write_json(receipt, report)
    _render(report)
    return 0 if present else 1
