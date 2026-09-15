"""Developer CLI for the fail-fast CRIU maintenance probe."""

from __future__ import annotations

import sys

from hol_workbench.criu_maintenance_preflight import (
    CriuMaintenancePreflightError,
    require_criu_maintenance_ready,
)


def main() -> int:
    try:
        readiness = require_criu_maintenance_ready()
    except CriuMaintenancePreflightError as exc:
        print(f"criu-preflight: {exc}", file=sys.stderr)
        return 77
    print(f"criu-preflight: passed {readiness.json()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
