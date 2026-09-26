#!/usr/bin/env python3
"""Stale warm-pool leases are reclaimed at checkout and reported by doctor; no HOL or CRIU."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hol-workbench"))
from hol_workbench.pools.leases import WarmPoolCheckoutError, checkout_warm_pool_seat, reclaim_stale_leases, stale_pool_leases

PRELOAD = {"path": "/cache/base.ml", "sha256": "a" * 64, "status": "loaded"}


def pool(*sessions):
    return {"status": "ready", "engine": "fork_basis", "preloads": [PRELOAD], "sessions": list(sessions)}


def seat(name, status, owner_pid=None):
    lease = None
    if status == "busy":
        lease = {"lease_id": name, "owner_pid": owner_pid, "leased_utc": "2026-09-26T14:21:52Z",
                 "owner_command": "orbstack-criu vanilla"}
    return {"session_dir": f"/pool/sessions/{name}", "status": status, "lease": lease,
            "loaded_preloads": [PRELOAD], "physical_control_session": "/pool/sessions/seat-1"}


class StaleLeases(unittest.TestCase):
    def test_dead_owner_lease_is_reclaimed_and_seat_reused(self):
        p = pool(seat("seat-1", "busy", owner_pid=45020))
        alive = {45020: False}
        reclaimed = reclaim_stale_leases(p, released_utc="now", owner_alive=lambda pid: alive.get(pid, True))
        self.assertEqual([row["owner_pid"] for row in reclaimed], [45020])
        self.assertEqual(p["sessions"][0]["status"], "idle")
        self.assertIsNone(p["sessions"][0]["lease"])
        self.assertIn("owner pid 45020 is not running", p["sessions"][0]["release_reason"])
        self.assertEqual(p["reclaimed_leases"][0]["reclaimed_utc"], "now")

    def test_live_or_unknown_owner_keeps_the_seat(self):
        p = pool(seat("seat-1", "busy", owner_pid=100), seat("seat-2", "busy"))
        self.assertEqual(stale_pool_leases(p, owner_alive=lambda pid: True), [])
        self.assertEqual(reclaim_stale_leases(p, released_utc="now", owner_alive=lambda pid: True), [])
        self.assertEqual([s["status"] for s in p["sessions"]], ["busy", "busy"])

    def test_checkout_reclaims_then_leases_the_seat(self):
        p = pool(seat("seat-1", "busy", owner_pid=45020))
        item = checkout_warm_pool_seat(
            p, session_alive=lambda item: True, lease_factory=lambda: {"lease_id": "new", "owner_pid": 1},
            owner_alive=lambda pid: pid != 45020, released_utc="now",
        )
        self.assertEqual(item["status"], "busy")
        self.assertEqual(item["lease"]["lease_id"], "new")
        self.assertEqual(p["reclaimed_leases"][0]["owner_pid"], 45020)

    def test_checkout_still_refuses_when_the_owner_is_alive(self):
        p = pool(seat("seat-1", "busy", owner_pid=45020))
        with self.assertRaises(WarmPoolCheckoutError):
            checkout_warm_pool_seat(
                p, session_alive=lambda item: True, lease_factory=lambda: {"lease_id": "new"},
                owner_alive=lambda pid: True, released_utc="now",
            )
        self.assertEqual(p["sessions"][0]["lease"]["lease_id"], "seat-1")


if __name__ == "__main__":
    unittest.main()
