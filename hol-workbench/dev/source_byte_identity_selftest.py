#!/usr/bin/env python3
"""Contract checks for the compact public source byte-identity fields."""

from __future__ import annotations

import contextlib
import inspect
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

WORKBENCH = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKBENCH))

from hol_workbench.cli import inspect as inspect_cli  # noqa: E402
from hol_workbench.cli import prove_replay  # noqa: E402
from hol_workbench.cli.published_profile_replay import run_published_warm_replay  # noqa: E402
from hol_workbench.hashing import normalized_sha256, sha256_bytes, short_sha256  # noqa: E402

ACCEPTED = b"let TRIVIAL = prove\n (`!n. n + 0 = n`,\n  ARITH_TAC);;\n"
EDITED = b"let TRIVIAL = prove\n (`!n. 0 + n = n`,\n  ARITH_TAC);;\n"


def _receipt(directory: Path, source: Path, *, source_sha256: object) -> Path:
    run = directory / "runs" / "attempt"
    run.mkdir(parents=True, exist_ok=True)
    transcript = run / "transcript.log"
    transcript.write_text("ok\n", encoding="utf-8")
    (run / "transcript.log.json").write_text(
        json.dumps(
            {
                "schema": inspect_cli.REPLAY_SCHEMA,
                "evidence": "recorded_warm_replay",
                "source": str(source),
                "source_sha256": source_sha256,
                "logical_profile": "light",
                "transport": "ok",
                "semantic_source_status": "succeeded",
                "source_completed": True,
                "completion_marker_valid": True,
                "claims_complete": True,
                "semantic_exit_status": 0,
                "exit_status": 0,
                "worker_exit_status": 0,
                "process_exit_status": 0,
                "bindings": [],
                "transcript": str(transcript),
            }
        ),
        encoding="utf-8",
    )
    return run


def _card(run: Path) -> tuple[str, int]:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        status = inspect_cli.main([str(run)])
    return stdout.getvalue(), status


def _source_line(card: str) -> str:
    return next(line for line in card.splitlines() if line.startswith("source: "))


def _advisory_lines(card: str) -> list[str]:
    return [line for line in card.splitlines() if line.startswith("current source changed: ")]


def _replay_line(output: str) -> str:
    return next(line for line in output.splitlines() if line.startswith("REPLAY: "))


def _pin_contract(accepted_sha: str, edited_sha: str) -> None:
    """The admitted-bytes pin refuses before claim parsing, restore, or HOL."""

    from hol_workbench.cli.orbstack_criu_vanilla import _pinned_source_refusal, run

    # Only an omitted pin is unpinned.
    assert _pinned_source_refusal(None, accepted_sha) is None
    assert _pinned_source_refusal(accepted_sha, accepted_sha) is None
    assert _pinned_source_refusal(accepted_sha.upper(), accepted_sha) is None
    assert _pinned_source_refusal(f"  {accepted_sha}  ", accepted_sha) is None

    refusal = _pinned_source_refusal(accepted_sha, edited_sha)
    assert refusal is not None
    assert len(refusal.splitlines()) == 1
    assert "source_pin=refused" in refusal
    assert f"expected sha={accepted_sha[:12]}" in refusal
    assert f"actual sha={edited_sha[:12]}" in refusal
    assert "admitted" not in refusal
    assert "HOL evaluation and profile restore not started" in refusal

    # A malformed pin refuses; it must never silently disable enforcement.
    for malformed in ("", "not-a-digest", accepted_sha[:12], f"{accepted_sha}0", "z" * 64):
        malformed_refusal = _pinned_source_refusal(malformed, accepted_sha)
        assert malformed_refusal is not None, malformed
        assert len(malformed_refusal.splitlines()) == 1
        assert "source_pin=refused" in malformed_refusal
        assert "expected sha=malformed" in malformed_refusal
        assert f"actual sha={accepted_sha[:12]}" in malformed_refusal

    pin = inspect.signature(run).parameters["expected_source_sha256"]
    assert pin.kind is inspect.Parameter.KEYWORD_ONLY
    assert pin.default is None, "developer callers must keep prior behavior"
    assert inspect.signature(run_published_warm_replay).parameters["expected_source_sha256"].default is None


def _banner_pin_receipt_contract(directory: Path, accepted_sha: str) -> None:
    """A successful REPLAY sha, the internal pin, and the receipt digest are one value."""

    if sys.platform != "linux":
        return
    source = directory / "banner.ml"
    source.write_bytes(ACCEPTED)
    run_root = directory / "banner-runs"
    recorded: dict[str, object] = {}

    def _fake_replay(profile: object, replayed: Path, **kwargs: object) -> int:
        recorded.update(kwargs)
        recorded["source"] = replayed
        transcript = kwargs["transcript"]
        assert isinstance(transcript, Path)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text("ok\n", encoding="utf-8")
        Path(f"{transcript}.json").write_text(
            json.dumps(
                {
                    "schema": inspect_cli.REPLAY_SCHEMA,
                    "source": str(replayed),
                    "source_sha256": kwargs["expected_source_sha256"],
                }
            ),
            encoding="utf-8",
        )
        return 0

    stdout = io.StringIO()
    with (
        patch.object(prove_replay, "run_published_warm_replay", _fake_replay),
        patch.object(prove_replay, "resolve_published_warm_profile", lambda scripts, name: object()),
        patch.object(prove_replay, "_profile", lambda source, explicit, scripts: "light"),
        contextlib.redirect_stdout(stdout),
    ):
        status = prove_replay.main(
            [str(source), "--profile", "light", "--run-root", str(run_root)],
            script_dir=directory,
            cwd=directory,
        )
    assert status == 0
    banner = _replay_line(stdout.getvalue())
    assert banner == f"REPLAY: profile=light source={source} sha={accepted_sha[:12]}"
    assert recorded["expected_source_sha256"] == accepted_sha

    receipt_run = Path(str(recorded["transcript"])).parent
    card, _ = _card(receipt_run)
    assert _source_line(card).endswith(f"sha={accepted_sha[:12]}")
    assert f"sha={accepted_sha[:12]}" in banner

    # An unhashable source refuses before the banner and never reaches the replay route.
    recorded.clear()
    unhashable = io.StringIO()
    stderr = io.StringIO()
    with (
        patch.object(prove_replay, "sha256_file", lambda path: None),
        patch.object(prove_replay, "run_published_warm_replay", _fake_replay),
        patch.object(prove_replay, "resolve_published_warm_profile", lambda scripts, name: object()),
        patch.object(prove_replay, "_profile", lambda source, explicit, scripts: "light"),
        contextlib.redirect_stdout(unhashable),
        contextlib.redirect_stderr(stderr),
    ):
        blocked = prove_replay.main(
            [str(source), "--profile", "light", "--run-root", str(directory / "blocked-runs")],
            script_dir=directory,
            cwd=directory,
        )
    assert blocked == 2
    assert "REPLAY" not in unhashable.getvalue()
    assert recorded == {}, "replay route must not run without a banner digest"
    assert stderr.getvalue().splitlines() == [f"prove: source digest unavailable: {source}"]


def _pin_refusal_handoff_contract(directory: Path) -> None:
    from hol_workbench.cli.orbstack_criu_vanilla import run

    source = directory / "changed-before-replay.ml"
    source.write_bytes(ACCEPTED)
    replay_calls = []
    restore_calls = []

    def changed_replay(profile: object, replayed: Path, **kwargs: object) -> int:
        replay_calls.append(replayed)
        source.write_bytes(EDITED)
        return run(
            profile_root=directory / "unused-profile",
            source=replayed,
            timeout=1.0,
            idle_timeout=None,
            restore=lambda: restore_calls.append(True) or 0,
            expected_source_sha256=str(kwargs["expected_source_sha256"]),
        )

    stdout, stderr = io.StringIO(), io.StringIO()
    with (
        patch.object(prove_replay, "run_published_warm_replay", changed_replay),
        patch.object(prove_replay, "resolve_published_warm_profile", lambda scripts, name: object()),
        patch.object(prove_replay, "_profile", lambda source, explicit, scripts: "light"),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        status = prove_replay.main(
            [str(source), "--profile", "light", "--run-root", str(directory / "pin-refused-runs")],
            script_dir=directory,
            cwd=directory,
        )
    assert status == 2
    assert replay_calls == [source], "source-pin refusal must not retry silently"
    assert restore_calls == [], "source-pin refusal must precede restore and HOL"
    assert "source_pin=refused" in stderr.getvalue()
    assert f"expected sha={sha256_bytes(ACCEPTED)[:12]}" in stderr.getvalue()
    assert f"actual sha={sha256_bytes(EDITED)[:12]}" in stderr.getvalue()
    assert "RECEIPT: unavailable; replay did not reach recorded evaluation" in stderr.getvalue()
    assert "NEXT: review the refusal details above, resolve the cause, then rerun prove SOURCE" in stderr.getvalue()
    assert "availability" not in stderr.getvalue()


def main() -> int:
    accepted_sha = sha256_bytes(ACCEPTED)
    edited_sha = sha256_bytes(EDITED)

    assert short_sha256(accepted_sha) == accepted_sha[:12]
    assert len(short_sha256(accepted_sha) or "") == 12
    assert short_sha256(accepted_sha.upper()) == accepted_sha[:12]
    assert normalized_sha256(f"  {accepted_sha}  ") == accepted_sha
    for unusable in (None, "", "not-a-digest", accepted_sha[:63], f"{accepted_sha}0", "z" * 64, 7):
        assert short_sha256(unusable) is None  # pyright: ignore[reportArgumentType]
        assert normalized_sha256(unusable) is None  # pyright: ignore[reportArgumentType]

    with tempfile.TemporaryDirectory(prefix="source-byte-identity-") as raw:
        directory = Path(raw)
        source = directory / "proof.ml"
        source.write_bytes(ACCEPTED)

        run = _receipt(directory, source, source_sha256=accepted_sha)
        card, status = _card(run)
        assert _source_line(card) == f"source: {source} sha={accepted_sha[:12]}"
        assert status == 0
        assert "source_sha256:" not in card
        verbose = io.StringIO()
        with contextlib.redirect_stdout(verbose):
            inspect_cli.main([str(run), "--verbose"])
        assert f"source_sha256: {accepted_sha}" in verbose.getvalue()

        # The card reports the recorded run; the live file on disk is never rehashed.
        source.write_bytes(EDITED)
        changed_card, changed_status = _card(run)
        assert _source_line(changed_card) == f"source: {source} sha={accepted_sha[:12]}"
        assert changed_status == status
        assert _advisory_lines(changed_card) == []
        assert "status: succeeded" in changed_card
        assert "source_acceptance: accepted" in changed_card
        source.unlink()
        assert _card(run)[0] == changed_card

        for older in (None, "unknown", accepted_sha[:12], 7):
            legacy = _receipt(directory / f"legacy-{older}", directory / "gone.ml", source_sha256=older)
            legacy_card = _card(legacy)[0]
            assert _source_line(legacy_card) == f"source: {directory / 'gone.ml'}"
            assert _advisory_lines(legacy_card) == []

        _pin_contract(accepted_sha, edited_sha)
        _banner_pin_receipt_contract(directory, accepted_sha)
        _pin_refusal_handoff_contract(directory)

    print("source_byte_identity_selftest=passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
