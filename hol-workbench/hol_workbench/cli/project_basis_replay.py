"""Prepare an explicit imported project basis through the ordinary replay path."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys

from hol_workbench.cli.orbstack_criu_vanilla import _write_pre_eval_dependency_artifact
from hol_workbench.cli.orbstack_criu_vanilla_artifacts import default_transcript_path
from hol_workbench.cli.published_profile import PublishedWarmProfile
from hol_workbench.hashing import sha256_file
from hol_workbench.project_basis import (
    abort_basis, adopt_basis, basis_cache_dir, bootstrap_postlude, bootstrap_prelude, lookup_basis,
    plan_basis, project_basis_lock,
)
from hol_workbench.source_execution_plan import capture_source_dependency_closure, decide_profile_satisfaction


def run_project_basis_replay(
    profile: PublishedWarmProfile, source: Path, *, basis_source: Path,
    run_root: Path, replay: Callable[..., int], timeout: float | None,
    transcript: Path, cache_root: Path | None = None,
    expected_source_sha256: str | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> int:
    """Admit a checked basis, then replay the untouched leaf in a fresh child.

    ``replay`` is the same ordinary runner for both phases. Its defaults refer
    to the leaf attempt; preparation overrides only its source, receipt and
    trusted bootstrap transport bytes. Neither phase edits project sources.
    """
    phase = on_phase or (lambda _phase: None)
    # ``cache_root`` None selects the shared per-user cache, so a basis prepared
    # under one run root serves every later run root with identical inputs.
    leaf_closure = {}

    def record_refusal(reason: str) -> None:
        # Preparation runs have their own receipts. A refusal before either
        # ordinary run still records this requested leaf, just like prove's
        # ordinary source/dependency preflight. Never replace an existing run.
        if Path(f"{transcript}.json").is_file():
            return
        digest = sha256_file(source) or expected_source_sha256
        if digest is None:
            return
        _write_pre_eval_dependency_artifact(
            transcript_output=transcript, source=source, source_sha256=digest,
            profile_root=profile.root, logical_profile=profile.name, closure=leaf_closure,
            package={"dependency_transport_status": "not_checked",
                     "dependency_transport_reason": reason,
                     "source_preflight_status": "project_basis_refused",
                     "project_basis": {"status": "refused", "requested_basis": str(basis_source),
                                       "reason": reason}},
            evidence_role="recorded_warm_replay", refusal_stage="project basis",
        )

    try:
        basis_source = basis_source.expanduser().resolve(strict=True)
        if not basis_source.is_file() or basis_source == source.resolve():
            raise ValueError("--basis must name a separate file imported by the leaf through literal needs")
        phase("project-basis-admission")
        with project_basis_lock(cache_root):
            phase("project-basis-capture")
            context = {
                "profile_cwd": profile.cwd,
                "legacy_holdir_roots": profile.legacy_holdir_roots,
                "logical_source_root_declarations": profile.logical_source_roots,
            }
            leaf_closure, _ = capture_source_dependency_closure(source, **context)
            basis_closure, holdir = capture_source_dependency_closure(basis_source, **context)
            basis_sha = basis_closure["entrypoint"]["sha256"]
            if not any(
                row.get("loader") == "needs" and row.get("resolved_path") == str(basis_source)
                and row.get("sha256") == basis_sha
                for row in leaf_closure.get("records") or []
            ):
                raise ValueError("--basis must be reachable with the same bytes through a literal needs in the leaf closure")
            satisfaction, transport, reason = decide_profile_satisfaction(
                basis_closure, profile_root=profile.root, logical_profile=profile.name,
                profile_cwd=profile.cwd, holdir_root=holdir,
            )
            if not transport.startswith("packaged"):
                raise ValueError(f"project basis dependencies cannot be packaged: {transport}; {reason}")
            plan = plan_basis(
                basis_source, profile_root=profile.root, logical_profile=profile.name,
                run_root=cache_root, closure=basis_closure, profile_satisfaction=satisfaction,
                profile_identity={
                    "cwd": str(profile.cwd), "capacity": profile.capacity,
                    "legacy_holdir_roots": [str(path) for path in profile.legacy_holdir_roots],
                    "logical_source_roots": list(profile.logical_source_roots),
                },
            )
            handle = lookup_basis(plan)
            if handle is None:
                preparation = default_transcript_path(run_root, basis_source)
                receipt = Path(f"{preparation}.json")
                print(f"PROJECT BASIS: preparing {basis_source}; budget={timeout:g}s for this phase"
                      if timeout is not None else f"PROJECT BASIS: preparing {basis_source}", flush=True)
                print(f"BASIS CACHE: {basis_cache_dir(cache_root)}; later runs with identical inputs reuse it "
                      "from any run root", flush=True)
                print(f"PREPARATION RECEIPT: {receipt}", flush=True)
                adopted = False
                try:
                    prefix = bootstrap_prelude(plan)
                    postlude = bootstrap_postlude(plan)
                    assert plan.generation is not None
                    status = replay(
                        basis_source, transcript_output=preparation,
                        expected_source_sha256=plan.identity["source_sha256"],
                        evidence_role="recorded_warm_replay",
                        preparation_prefix=prefix, preparation_postlude=postlude,
                        preparation_package_root=plan.generation / "source-package",
                    )
                    if status:
                        print(f"PROJECT BASIS: preparation did not pass (exit {status}); leaf was not evaluated. "
                              f"Inspect {receipt}" if receipt.is_file() else
                              f"PROJECT BASIS: preparation stopped before recording (exit {status}); leaf was not evaluated.",
                              file=sys.stderr, flush=True)
                        return status
                    handle = adopt_basis(plan, receipt)
                    adopted = True
                finally:
                    if not adopted:
                        abort_basis(plan)
                print(f"PROJECT BASIS: prepared {basis_source}; identity={plan.key[:12]}", flush=True)
            else:
                print(f"PROJECT BASIS: reusing {basis_source}; identity={plan.key[:12]}", flush=True)
                print(f"PREPARATION RECEIPT: {handle.record['preparation_receipt']}", flush=True)
            print(f"LEAF REPLAY: {source}; fresh child; budget={timeout:g}s for this phase"
                  if timeout is not None else f"LEAF REPLAY: {source}; fresh child", flush=True)
            phase("project-basis-leaf")
            return replay(source, project_basis_handle=handle)
    except KeyboardInterrupt:
        print("PROJECT BASIS: cancelled; retained receipts and the shared warm shelf.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError) as exc:
        try:
            record_refusal(str(exc))
        except (OSError, RuntimeError, ValueError) as recording_error:
            print(f"PROJECT BASIS: refusal receipt could not be written: {recording_error}", file=sys.stderr)
        print(f"PROJECT BASIS: refused; {exc}", file=sys.stderr)
        return 2
