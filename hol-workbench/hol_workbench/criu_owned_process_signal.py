"""Privileged pidfd signal helper with a Linux start-ticks identity check."""

from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path


def linux_start_ticks(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        _, separator, tail = raw.rpartition(")")
        return int(tail.split()[19]) if separator else None
    except (IndexError, OSError, ValueError):
        return None


def signal_exact_process(pid: int, expected_start_ticks: int, signum: int) -> dict[str, object]:
    """Open a pidfd, verify expected Linux start ticks, then signal through the pidfd."""
    try:
        pidfd = os.pidfd_open(pid)
    except ProcessLookupError:
        return {"status": "already_quiescent", "pid": pid, "signal": signum}
    try:
        actual_start_ticks = linux_start_ticks(pid)
        if actual_start_ticks != expected_start_ticks:
            return {
                "status": "identity_mismatch" if actual_start_ticks is not None else "identity_unavailable",
                "pid": pid,
                "expected_start_ticks": expected_start_ticks,
                "actual_start_ticks": actual_start_ticks,
                "signal": signum,
            }
        try:
            signal.pidfd_send_signal(pidfd, signum)
        except ProcessLookupError:
            return {"status": "already_quiescent", "pid": pid, "signal": signum}
        return {
            "status": "signalled",
            "pid": pid,
            "start_ticks": actual_start_ticks,
            "signal": signum,
        }
    finally:
        os.close(pidfd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Signal one exact Linux process through a verified pidfd.")
    parser.add_argument("--pid", required=True, type=int)
    parser.add_argument("--start-ticks", required=True, type=int)
    parser.add_argument("--signal", required=True, type=int, choices=(signal.SIGTERM, signal.SIGKILL))
    args = parser.parse_args(argv)
    result = signal_exact_process(args.pid, args.start_ticks, args.signal)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"signalled", "already_quiescent"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
