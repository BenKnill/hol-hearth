#!/usr/bin/env python3
"""Prepare a diagnostic HOL scratch from a failed recorded source; never execute it."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any

from hol_workbench.cli.inspect import _replay_receipt
from hol_workbench.cli.public_commands import public_command
from hol_workbench.fork_child_ocaml import ocaml_string_literal
from hol_workbench.hashing import sha256_bytes
from hol_workbench.proofs.loader_scan import ProveBinding, scan_prove_bindings
from hol_workbench.proofs.theorem_scan import extract_hol_theorems_bytes
from hol_workbench.secure_tree_read import read_regular_file_beneath
from hol_workbench.source_dependency_package import (
    DependencyPackageError, dependency_transport_status, materialize_dependency_package,
)


class ReopenError(ValueError):
    """The recorded source cannot be reopened without guessing."""


def _recorded_binding(
    receipt: dict[str, Any], name: str,
) -> tuple[Path, bytes, dict[str, Any], ProveBinding]:
    if receipt.get("evidence") != "recorded_warm_replay":
        raise ReopenError("requires a recorded warm replay receipt")
    if receipt.get("semantic_source_status") not in {"failed", "not_completed"} or receipt.get("source_completed"):
        raise ReopenError("requires a failed or incomplete source receipt")
    bindings = [row for row in receipt.get("bindings", []) if isinstance(row, dict) and row.get("name") == name]
    if len(bindings) != 1 or bindings[0].get("status") not in {"missing", "failed"}:
        raise ReopenError("binding must be one recorded, unproved entrypoint claim; imported bindings are unsupported")
    claims = [
        row for row in (receipt.get("transcript_accounting") or {}).get("claim_accounting", [])
        if isinstance(row, dict) and row.get("theorem") == name
    ]
    if len(claims) != 1:
        raise ReopenError("binding has no unique recorded source statement")
    claim = claims[0]
    source_value = receipt.get("source")
    if not isinstance(source_value, str) or not Path(source_value).is_absolute():
        raise ReopenError("receipt has no absolute entrypoint source")
    source = Path(source_value)
    if claim.get("source") != str(source):
        raise ReopenError("imported bindings cannot be reopened")
    closure = receipt.get("source_dependency_closure")
    if not isinstance(closure, dict):
        raise ReopenError("receipt has no recorded dependency closure")
    status, reason = dependency_transport_status(closure)
    if status != "packaged" or not closure.get("semantic_identity_complete"):
        raise ReopenError(f"requires a complete literal source package: {reason}")
    if receipt.get("source_dependency_closure_sha256") != closure.get("strict_sha256"):
        raise ReopenError("recorded dependency closure identity does not match")
    entry = closure.get("entrypoint") or {}
    if entry.get("path") != str(source):
        raise ReopenError("recorded entrypoint path does not match")
    if closure.get("artifacts") or closure.get("dynamic_artifacts"):
        raise ReopenError("artifact-bearing sources cannot yet be relocated by reopen")
    for record in closure.get("records") or []:
        if (
            record.get("resolution") != "source_local"
            or record.get("loader") not in {"needs", "loadt", "loads"}
            or Path(str(record.get("declared_path") or "")).is_absolute()
            or record.get("traversal") not in {"followed", "already_seen"}
        ):
            raise ReopenError(
                "reopen supports acyclic relative needs/loadt/loads within the recorded local source package; "
                "mapped/library imports, #use and bare load are unsupported"
            )
    data = read_regular_file_beneath(Path(closure["root"]), source).data
    digest = sha256_bytes(data)
    if any(value != digest for value in (
        receipt.get("source_sha256"), claim.get("source_sha256"), entry.get("sha256"),
    )):
        raise ReopenError("entrypoint source changed since the receipt; record a fresh attempt")
    scanned = scan_prove_bindings(data)
    if scanned.status != "ok":
        raise ReopenError(f"source cannot be delimited: {scanned.refusal.reason}")
    selected = [row for row in scanned.bindings if row.name == name]
    if len(selected) != 1:
        raise ReopenError("binding cannot be delimited unambiguously as a standalone literal prove phrase")
    binding = selected[0]
    inventories = [row for row in extract_hol_theorems_bytes(source, data) if row.get("name") == name]
    if len(inventories) != 1:
        raise ReopenError("binding is ambiguous in the recorded entrypoint")
    inventory = inventories[0]
    if (
        inventory.get("proof_constructor") != "prove"
        or inventory.get("source_line") != binding.source_line
        or inventory.get("statement_quote_sha256") != claim.get("statement_quote_sha256")
        or inventory.get("statement") != claim.get("statement")
        or inventory.get("statement_quote", "").encode("utf-8") != binding.statement_span.bytes_from(data)
        or claim.get("source_span") != [inventory.get("source_line"), inventory.get("statement_end_line")]
    ):
        raise ReopenError("recorded statement or source span does not match the selected source phrase")
    return source, data, closure, binding


def _comment(text: str) -> str:
    # OCaml quoted strings inside comments shield quotes, nested-comment tokens
    # and arbitrary tactic text. Select a delimiter absent from the payload.
    delimiter = "hearth_reopen"
    while f"|{delimiter}}}" in text:
        delimiter += "_"
    return f"(* {{{delimiter}|\n{text}\n|{delimiter}}} *)\n"


def reopen(run: Path, *, binding_name: str, out: Path) -> dict[str, Any]:
    receipt_path = _replay_receipt(run.expanduser())
    if receipt_path is None:
        raise ReopenError("no recorded replay receipt found; pass a receipt, attempt directory or run root")
    receipt_path = receipt_path.resolve()
    receipt_bytes = receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    if not isinstance(receipt, dict):
        raise ReopenError("receipt is not a JSON object")
    source, data, closure, binding = _recorded_binding(receipt, binding_name)

    # Never create parents or overwrite files, including dangling symlinks.
    out = Path(os.path.abspath(out.expanduser()))
    parent = out.parent.resolve(strict=True)
    out = parent / out.name
    if out.suffix != ".ml":
        raise ReopenError("--out must name a new .ml file in an existing directory")
    bundle = out.with_name(out.name + ".reopen")
    if os.path.lexists(out) or os.path.lexists(bundle):
        raise ReopenError("output or its .reopen companion already exists; choose a new --out")
    origin = (
        "DIAGNOSTIC ONLY: source execution does not establish the selected theorem.\n"
        f"Selected binding: {binding_name}\n"
        f"Origin receipt: {receipt_path}\n"
        f"Receipt SHA-256: {sha256_bytes(receipt_bytes)}\n"
        f"Original source: {source}\n"
        f"Source SHA-256: {sha256_bytes(data)}\n"
    )
    prefix = data[:binding.binding_span.start]
    prefix_header = _comment(
        origin + f"Exact original prefix follows ({len(prefix)} bytes); no selected binding or later source."
    ).encode("utf-8")
    owned_bundle = False
    with tempfile.TemporaryDirectory(prefix=f".{out.name}.staging-", dir=parent) as temporary:
        staging = Path(temporary)
        package = staging / "inputs"
        entrypoint, manifest = materialize_dependency_package(
            source=source, closure=closure, destination=package,
            entrypoint_output_bytes=prefix_header + prefix,
        )
        # The materializer securely rereads and hash-checks every captured input
        # before publishing even this private staging package.
        entry_relative = entrypoint.relative_to(package)
        prefix_relative = Path(bundle.name) / "inputs" / entry_relative
        quote = binding.statement_span.bytes_from(data).decode("utf-8")
        tactic = binding.tactic_span.bytes_from(data).decode("utf-8")
        scratch = (
            _comment(origin + "Fresh diagnostic goal, not a recovered residual. No tactic is executed below.")
            + f"needs {ocaml_string_literal(prefix_relative.as_posix())};;\n\n"
            + f"g {quote};;\n\n"
            + _comment("Recorded tactic, inactive. Copy selected steps into e (...) and replay the scratch:\n"
                       + "e (" + tactic + ");;")
        ).encode("utf-8")
        scratch_path = staging / "scratch.ml"
        scratch_path.write_bytes(scratch)
        metadata = {
            "schema": "hol-hearth.reopened-proof.v1",
            "diagnostic_only": True,
            "origin_receipt": str(receipt_path),
            "origin_receipt_sha256": sha256_bytes(receipt_bytes),
            "source": str(source),
            "source_sha256": sha256_bytes(data),
            "binding": binding_name,
            "binding_byte_span": [binding.binding_span.start, binding.binding_span.end],
            "statement_byte_span": [binding.statement_span.start, binding.statement_span.end],
            "prefix_byte_count": len(prefix),
            "prefix_sha256": sha256_bytes(prefix),
            "prefix_header_byte_count": len(prefix_header),
            "dependency_closure_sha256": closure["strict_sha256"],
            "copied_files": manifest["files"],
            "scratch": str(out),
            "prefix": str(bundle / "inputs" / entry_relative),
            "scratch_sha256": sha256_bytes(scratch),
            "profile": receipt.get("logical_profile") or receipt.get("physical_profile"),
        }
        (staging / "origin.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        try:
            # mkdir reserves the companion without replacing an existing tree.
            # Link the complete scratch last with atomic no-replace semantics.
            bundle.mkdir()
            owned_bundle = True
            package.rename(bundle / "inputs")
            (staging / "origin.json").rename(bundle / "origin.json")
            os.link(scratch_path, out)
        except BaseException:
            if owned_bundle:
                shutil.rmtree(bundle)
            raise
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hearth reopen",
        description="Write an exact-prefix diagnostic g/e scratch from a failed recorded binding; never run HOL.",
        allow_abbrev=False,
    )
    parser.add_argument("run", type=Path, help="receipt, attempt directory, or root (newest receipt)")
    parser.add_argument("--binding", required=True, help="exact unproved entrypoint binding")
    parser.add_argument("--out", required=True, type=Path, help="new .ml file; existing parent directory")
    args = parser.parse_args(argv)
    try:
        result = reopen(args.run, binding_name=args.binding, out=args.out)
    except (OSError, ValueError, KeyError, TypeError, DependencyPackageError) as exc:
        print(f"reopen: refused: {exc}", file=sys.stderr)
        return 2
    print(f"REOPENED: {result['binding']} (diagnostic only; not a recovered residual)")
    print(f"SCRATCH: {result['scratch']}")
    print(f"PREFIX: {result['prefix']}")
    print(f"ORIGIN: {result['scratch']}.reopen/origin.json")
    print("NOTE: no HOL was run; source success will not establish the selected theorem")
    command = [result["scratch"]]
    if result.get("profile"):
        command.extend(["--profile", result["profile"]])
    command.extend(["--run-root", result["scratch"] + ".runs"])
    print("NEXT: " + public_command("prove", *command))
    print("GOALS: " + public_command("inspect", result["scratch"] + ".runs", "--tail", "40"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
