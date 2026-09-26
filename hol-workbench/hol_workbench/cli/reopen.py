#!/usr/bin/env python3
"""Prepare a diagnostic HOL scratch from a failed recorded source; never execute it."""
from __future__ import annotations

import argparse
import json
import math
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
from hol_workbench.profile_satisfied_dependencies import (
    ProfileSatisfactionError, profile_runtime_fallback_record_indexes, profile_satisfied_record_indexes,
    revalidate_live_edge_files,
)
from hol_workbench.proofs.loader_scan import ProveBinding, scan_prove_bindings
from hol_workbench.proofs.theorem_scan import extract_hol_theorems_bytes
from hol_workbench.secure_tree_read import read_regular_file_beneath
from hol_workbench.source_dependency_package import (
    DependencyPackageError, dependency_transport_status, materialize_dependency_package,
)


class ReopenError(ValueError):
    """The recorded source cannot be reopened without guessing."""


CAPTURED_SOURCE_KINDS = frozenset({"source_local", "holdir_source", "source_overlay"})
# Resolutions build_profile_satisfaction may bind to a validated warm shelf.
PROFILE_BOUND_KINDS = frozenset({"unresolved", "external_resolved", "mounted_source"})


def _profile_bound_record_indexes(receipt: dict[str, Any], closure: dict[str, Any]) -> set[int]:
    """Return record indexes the receipt's warm-shelf decision satisfied or hash-bound.

    A profile-satisfied ``needs`` (for example ``arm/proofs/base.ml`` under the
    ``s2n-arm`` profile) never enters the literal package: the closure records
    it as ``unresolved`` and ``not_followed`` while the same receipt records the
    exact shelf file that satisfied it. Only the decision whose identity matches
    this closure counts; it is fail-closed on any record-bound field mismatch.
    """
    document = receipt.get("profile_satisfaction")
    if document is None:
        return set()
    if not isinstance(document, dict):
        raise ReopenError("receipt has an invalid profile-satisfaction record")
    indexes = profile_satisfied_record_indexes(document, closure) | profile_runtime_fallback_record_indexes(
        document, closure,
    )
    if document.get("edges") and not indexes:
        raise ReopenError("recorded profile-satisfaction decision does not match the dependency closure")
    if indexes and document.get("logical_profile") != receipt.get("logical_profile"):
        raise ReopenError("recorded profile-satisfaction decision names a different profile than the receipt")
    return indexes


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
    profile_satisfaction = receipt.get("profile_satisfaction") if isinstance(receipt.get("profile_satisfaction"), dict) else None
    status, reason = dependency_transport_status(closure, profile_satisfaction=profile_satisfaction)
    if not status.startswith("packaged"):
        raise ReopenError(f"requires a complete literal source package: {reason}")
    profile_indexes = _profile_bound_record_indexes(receipt, closure)
    records = closure.get("records") or []
    if not closure.get("semantic_identity_complete"):
        # The only admissible gaps are literal needs the recorded warm shelf satisfied.
        gaps = {index for index, record in enumerate(records) if record.get("resolution") not in CAPTURED_SOURCE_KINDS}
        if closure.get("unresolved_artifact_count") or not profile_indexes or gaps != profile_indexes:
            raise ReopenError(f"requires a complete literal source package: {reason}")
    if receipt.get("source_dependency_closure_sha256") != closure.get("strict_sha256"):
        raise ReopenError("recorded dependency closure identity does not match")
    entry = closure.get("entrypoint") or {}
    if entry.get("path") != str(source):
        raise ReopenError("recorded entrypoint path does not match")
    for index, record in enumerate(records):
        if index in profile_indexes:
            if (
                record.get("resolution") not in PROFILE_BOUND_KINDS
                or record.get("loader") != "needs"
                or record.get("traversal") != "not_followed"
            ):
                raise ReopenError("recorded profile-satisfied needs does not match its closure record")
            continue
        if (
            record.get("resolution") not in CAPTURED_SOURCE_KINDS
            or record.get("loader") not in {"needs", "loadt", "loads"}
            or Path(str(record.get("declared_path") or "")).is_absolute()
            or record.get("traversal") not in {"followed", "already_seen"}
        ):
            raise ReopenError(
                "reopen supports acyclic captured relative needs/loadt/loads; "
                "mapped imports, #use and bare load are unsupported"
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


def _requires_original_project(closure: dict[str, Any], profile_indexes: set[int]) -> bool:
    """Keep ELF, project-root and captured library paths at their original coordinates.

    Profile-satisfied needs resolve through the profile cwd or HOL load path, not
    through the scratch location, so they never force the original coordinates.
    """
    return bool(closure.get("artifacts")) or any(
        row.get("resolution") != "source_local"
        or row.get("resolution_base") == "source_package_root"
        for index, row in enumerate(closure.get("records") or [])
        if index not in profile_indexes
    )


def _profile_satisfied_summary(receipt: dict[str, Any], closure: dict[str, Any], profile_indexes: set[int]) -> dict[str, Any]:
    """Verify the recorded shelf files still hold the recorded bytes; copy nothing."""
    document = receipt.get("profile_satisfaction")
    if not profile_indexes or not isinstance(document, dict):
        return {}
    try:
        revalidate_live_edge_files(document)
    except ProfileSatisfactionError as exc:
        raise ReopenError(f"{exc}; record a fresh attempt") from exc
    edges = [
        {key: edge.get(key) for key in (
            "declared_path", "resolution", "mapping_root", "host_path", "sha256", "size_bytes", "source_line",
        )}
        for edge in document.get("edges") or []
        if edge.get("record_index") in profile_indexes
    ]
    return {
        "profile_satisfied_dependencies": edges,
        "profile_basis_id": document.get("profile_basis_id"),
        "profile_sha256": document.get("profile_sha256"),
        "profile_satisfaction_sha256": document.get("strict_sha256"),
        "profile_satisfied_dependencies_role": "verified_not_copied",
    }


def _recorded_basis_handoff(
    receipt: dict[str, Any], closure: dict[str, Any], binding: ProveBinding,
) -> tuple[str | None, str | None]:
    """Retain an explicit basis only when its exact needs import precedes the goal."""
    basis = receipt.get("project_basis") or {}
    if not isinstance(basis, dict):
        return None, None
    identity = basis.get("identity") or {}
    if not isinstance(identity, dict):
        return None, None
    source = identity.get("source")
    preparation = basis.get("preparation_receipt")
    if not isinstance(source, str) or not Path(source).is_absolute():
        return None, None
    if not isinstance(preparation, str) or not Path(preparation).is_absolute():
        return None, None
    for row in closure.get("records") or []:
        if (
            row.get("resolved_path") == source
            and row.get("sha256") == identity.get("source_sha256")
            and row.get("loader") == "needs"
            and row.get("declaring_file") == "<entrypoint>"
            and isinstance(row.get("source_line"), int)
            and row["source_line"] < binding.source_line
        ):
            return source, str(Path(preparation).parent.parent)
    return None, None


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
    profile_indexes = _profile_bound_record_indexes(receipt, closure)
    profile_summary = _profile_satisfied_summary(receipt, closure, profile_indexes)

    # Never create parents or overwrite files, including dangling symlinks.
    out = Path(os.path.abspath(out.expanduser()))
    parent = out.parent.resolve(strict=True)
    out = parent / out.name
    if out.suffix != ".ml":
        raise ReopenError("--out must name a new .ml file in an existing directory")
    bundle = out.with_name(out.name + ".reopen")
    if os.path.lexists(out) or os.path.lexists(bundle):
        raise ReopenError("output or its .reopen companion already exists; choose a new --out")
    original_project = _requires_original_project(closure, profile_indexes)
    if original_project and parent != source.parent:
        raise ReopenError(
            "assembly and project-root/library imports require --out beside the original source "
            f"to preserve path coordinates: {source.parent / out.name}"
        )
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
            profile_satisfaction=receipt.get("profile_satisfaction") if profile_indexes else None,
        )
        # The materializer securely rereads and hash-checks every captured input
        # before publishing even this private staging package.
        entry_relative = entrypoint.relative_to(package)
        prefix_relative = Path(bundle.name) / "inputs" / entry_relative
        quote = binding.statement_span.bytes_from(data).decode("utf-8")
        tactic = binding.tactic_span.bytes_from(data).decode("utf-8")
        profile_note = "".join(
            f"\nProfile-satisfied import, verified and not copied: {edge['declared_path']} (profile {receipt.get('logical_profile')})"
            for edge in profile_summary.get("profile_satisfied_dependencies") or []
        )
        scratch_header = _comment(
            origin + "Fresh diagnostic goal, not a recovered residual. The selected tactic stays inactive.\n"
            + ("Original project coordinates: ordinary prove recaptures current dependency and ELF bytes."
               if original_project else "Imports the copied prefix and dependencies below.")
            + profile_note
        ).encode("utf-8")
        scratch_prefix = (
            prefix if original_project
            else f"needs {ocaml_string_literal(prefix_relative.as_posix())};;\n".encode("utf-8")
        )
        scratch = scratch_header + scratch_prefix + (
            "\n"
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
            "source_layout": "original_project" if original_project else "copied_package",
            "copied_files_role": "verified_reference" if original_project else "scratch_inputs",
            "scratch_prefix_byte_span": (
                [len(scratch_header), len(scratch_header) + len(prefix)] if original_project else None
            ),
            **profile_summary,
        }
        basis_source, basis_run_root = _recorded_basis_handoff(receipt, closure, binding)
        if original_project and basis_source:
            metadata["basis"] = basis_source
            metadata["run_root"] = basis_run_root
        timeout = receipt.get("requested_timeout_seconds")
        if type(timeout) in {int, float} and math.isfinite(timeout) and timeout > 0:
            metadata["timeout_seconds"] = timeout
        (staging / "origin.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        scratch_identity = scratch_path.stat()
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
                # A signal can arrive after link() publishes the scratch but
                # before Python observes its return. Keep its complete companion
                # in that case; never delete or follow another author's output.
                try:
                    published = os.path.samestat(scratch_identity, out.lstat())
                except FileNotFoundError:
                    published = False
                if not published:
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
    if result["source_layout"] == "original_project":
        print("INPUTS: original project; ordinary prove recaptures source and ELF bytes; copied files are verified references")
    satisfied = result.get("profile_satisfied_dependencies") or []
    if satisfied:
        names = ", ".join(str(edge.get("declared_path")) for edge in satisfied)
        print(f"PROFILE INPUTS: {len(satisfied)} literal needs satisfied by profile {result.get('profile')} "
              f"({result.get('profile_basis_id')}); shelf bytes verified against the receipt, not copied: {names}")
    command = [result["scratch"]]
    if result.get("profile"):
        command.extend(["--profile", result["profile"]])
    if result.get("basis"):
        command.extend(["--basis", result["basis"]])
        print("BASIS: recorded preparation requested; ordinary prove checks compatibility before reuse")
    if result.get("timeout_seconds"):
        command.extend(["--timeout", str(result["timeout_seconds"])])
    run_root = result.get("run_root") or result["scratch"] + ".runs"
    command.extend(["--run-root", run_root])
    print("NEXT: " + public_command("prove", *command))
    print("GOALS: " + public_command("inspect", run_root, "--tail", "40"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
