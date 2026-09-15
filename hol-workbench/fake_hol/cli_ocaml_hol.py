from __future__ import annotations

import os
import sys
from pathlib import Path

from .runtime import FakeHolConfig, FakeHolRuntime


def run_persistent(config: FakeHolConfig) -> int:
    runtime = FakeHolRuntime(config)
    if config.startup:
        runtime.emit_startup()
    for line in runtime.output:
        sys.stdout.write(line + "\n")
    sys.stdout.write(f"__FAKE_HOL_PROCESS__:pid={os.getpid()}\n")
    sys.stdout.flush()
    runtime.output.clear()

    def emit_line(line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    pending: list[str] = []
    for line in sys.stdin:
        pending.append(line)
        if not line.rstrip().endswith(";;"):
            continue
        text = "".join(pending)
        pending = []
        before = len(runtime.output)
        streaming = "__PROOF_RUN_FORK_SPAWNED__" in text
        runtime.emit_callback = emit_line if streaming else None
        runtime.run_fragment(text, cwd=Path.cwd())
        if not streaming:
            for item in runtime.output[before:]:
                sys.stdout.write(item + "\n")
            sys.stdout.flush()
        runtime.emit_callback = None
    return config.exit_status


def main(argv: list[str] | None = None) -> int:
    _ = argv if argv is not None else sys.argv[1:]
    config = FakeHolConfig.from_env()
    if os.environ.get("FAKE_HOL_PERSISTENT") == "1":
        return run_persistent(config)
    stream = config.roleplay_profile in {"noisy", "unstable"} or config.sleep_on_token
    emitted = False

    def emit_line(line: str) -> None:
        nonlocal emitted
        if not stream:
            return
        emitted = True
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    runtime = FakeHolRuntime(config, emit_callback=emit_line if stream else None)
    text = sys.stdin.read()
    result = runtime.run_text(text, cwd=Path.cwd())
    if not emitted:
        sys.stdout.write(result.stdout)
    return result.exit_status


if __name__ == "__main__":
    raise SystemExit(main())
