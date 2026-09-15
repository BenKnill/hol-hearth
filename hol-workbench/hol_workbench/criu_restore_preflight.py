"""Fail-closed restore preflights that must not mutate a stopped shelf."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path

from hol_workbench import criu_lazy_pages_dispatch


def run(
    args: argparse.Namespace,
    pool: Path,
    *,
    pool_is_live: Callable[[Path], bool],
    display_pool: Path | None = None,
) -> int | None:
    reported_pool = display_pool or pool
    acceptance_override = bool(getattr(args, "accept_lazy_pages_lifecycle_risk", False))
    already_live_only = bool(getattr(args, "already_live_only", False))
    published_shelf_only = bool(getattr(args, "published_shelf_only", False))
    if already_live_only and published_shelf_only:
        print("status=failed")
        print("failing_phase=preflight_admission_mode")
        print(f"pool={reported_pool}")
        print("reason=--already-live-only and --published-shelf-only are mutually exclusive")
        print("lifecycle_action=none")
        return 1
    if acceptance_override and not args.lazy_pages:
        print("status=failed")
        print("failing_phase=preflight_lazy_pages")
        print(f"pool={reported_pool}")
        print("reason=--accept-lazy-pages-lifecycle-risk requires --lazy-pages")
        return 1
    if args.lazy_pages and not criu_lazy_pages_dispatch.PUBLIC_LAZY_PAGES_ENABLED and not acceptance_override:
        print("status=failed")
        print("failing_phase=preflight_lazy_pages")
        print(f"pool={reported_pool}")
        print("reason=CRIU lazy-pages is disabled until its detached daemon has identity-bound readiness and cleanup")
        print("tracking_issue=https://github.com/BenKnill/hol-hearth/issues")
        return 1
    if not already_live_only and not published_shelf_only:
        return None
    try:
        data = json.loads((pool / "pool.json").read_text(encoding="utf-8"))
        status = str(data.get("status") or "unknown")
        live = pool_is_live(pool)
    except (OSError, json.JSONDecodeError, RuntimeError, TypeError, ValueError) as exc:
        return _unavailable(reported_pool, f"{type(exc).__name__}: {exc}")
    if already_live_only and not (status == "ready" and live):
        return _unavailable(reported_pool, "pool is not already ready with matching live process identities")
    if already_live_only:
        print(f"pool={reported_pool}")
        print("restore_log=-")
        print("restore_options=already-live")
        print("restore_seconds=0.000")
        print("lifecycle_action=none")
        return 0
    if status == "ready":
        if not live:
            if published_shelf_only:
                # The caller holds the lifecycle and pool locks.  It may now
                # prove the dead generation quiescent, atomically demote the
                # authoritative ready gate, and enter the normal shelf restore.
                return None
            return _unavailable(reported_pool, "published pool claims ready but its process identities are not live")
        print(f"pool={reported_pool}")
        print("restore_log=-")
        print("restore_options=already-live")
        print("restore_seconds=0.000")
        print("lifecycle_action=none")
        return 0
    if status != "stopped":
        return _unavailable(
            reported_pool,
            f"published pool lifecycle status is {status}; only a verified stopped shelf may be activated",
        )
    if live:
        return _unavailable(
            reported_pool, "published pool claims stopped but recorded process identities are still live"
        )
    # The normal restore preflight now verifies the stopped-session quiescence
    # receipt.  The public caller deliberately skips incomplete-transaction
    # recovery, so this path can activate published bytes but cannot repair a
    # damaged or interrupted shelf.
    return None


def _unavailable(pool: Path, reason: str) -> int:
    print("status=unavailable")
    print("failing_phase=already_live_check")
    print(f"pool={pool}")
    print(f"reason={reason}")
    print("lifecycle_action=none")
    return 75


def add_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--already-live-only",
        action="store_true",
        help="Reuse a ready live pool or fail without recovery or restore.",
    )
    parser.add_argument(
        "--published-shelf-only",
        action="store_true",
        help="Reuse or activate one validated published shelf; never recover an incomplete restore.",
    )
    parser.add_argument(
        "--accept-lazy-pages-lifecycle-risk",
        action="store_true",
        help="Developer acceptance only: exercise the owned lazy-pages transaction while its public gate is disabled.",
    )
