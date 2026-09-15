"""Start-bound source preparation for the current fork controller."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hol_workbench.fork_child_ocaml import ocaml_string_literal


def prepare_fork_spawn(
    request: dict[str, Any],
    *,
    source_phrase_builder: Callable[..., str] | None,
    fork_phrase_builder: Callable[..., str],
) -> dict[str, Any]:
    """Build one fork-child phrase without importing code after attempt start."""

    attempt_id = str(request.get("attempt_id") or secrets.token_hex(8))
    source_path = Path(request["source"]).resolve()
    transcript_path = Path(request["transcript_path"]).resolve()
    load_wrapper = transcript_path.with_name(f"{transcript_path.stem}.load.ml")
    if request.get("raw_source"):
        load_phrase = f"loadt {ocaml_string_literal(str(source_path))};;"
    elif source_phrase_builder is not None:
        load_phrase = source_phrase_builder(source_path, use_loadt=True)
    else:
        raise RuntimeError("non-raw fork source requires an eagerly supplied source phrase builder")
    load_wrapper.write_text(load_phrase + "\n", encoding="utf-8")
    done_token = f"__PROOF_RUN_FORK_CHILD_DONE__:{attempt_id}:{secrets.token_hex(8)}"
    spawn_token = f"__PROOF_RUN_FORK_SPAWNED__:{attempt_id}:"
    refusal_token = f"__PROOF_RUN_FORK_REFUSED__:{attempt_id}:"
    ack_path = transcript_path.with_name(f"{transcript_path.stem}.registration-ack.json")
    ownership_path = transcript_path.with_name(f"{transcript_path.stem}.ownership-ready")
    ack_path.unlink(missing_ok=True)
    ownership_path.unlink(missing_ok=True)
    phrase = fork_phrase_builder(
        attempt_id=attempt_id,
        source=str(source_path),
        transcript=str(transcript_path),
        load_wrapper=load_wrapper,
        done_token=done_token,
        spawn_token=spawn_token,
        ack_path=ack_path,
        ownership_path=ownership_path,
        refusal_token=refusal_token,
        foundation_marker_prefix=request.get("foundation_marker_prefix"),
    )
    return {
        "attempt_id": attempt_id,
        "transcript_path": transcript_path,
        "done_token": done_token,
        "spawn_token": spawn_token,
        "refusal_token": refusal_token,
        "ack_path": ack_path,
        "ownership_path": ownership_path,
        "phrase": phrase,
    }
