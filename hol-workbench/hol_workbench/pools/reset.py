"""Warm-pool reset root discovery and summary policy."""

from __future__ import annotations

import shlex
from contextlib import suppress
from pathlib import Path
from typing import Any

from hol_workbench.processes import descendant_pids


def parse_command_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def resolved_absolute_path(text: str | Path | None) -> Path | None:
    if not text:
        return None
    path = Path(str(text)).expanduser()
    if not path.is_absolute():
        return None
    return path.resolve()


def sterile_pool_root_from_path_text(text: str | Path | None) -> Path | None:
    path = resolved_absolute_path(text)
    if path is None:
        return None
    parts = path.parts
    if "sterile-scratch-pools" not in parts:
        return None
    index = parts.index("sterile-scratch-pools")
    return Path(*parts[: index + 1]).resolve()


def add_reset_root(
    roots: dict[str, dict[str, Any]],
    root: Path | None,
    *,
    source: str,
    pid: int | None = None,
) -> None:
    if root is None:
        return
    resolved = root.expanduser().resolve()
    key = str(resolved)
    item = roots.setdefault(key, {"pool_root": key, "sources": set(), "process_pids": set()})
    item["sources"].add(source)
    if pid is not None:
        item["process_pids"].add(pid)


def discover_pool_reset_roots(args: Any, process_table: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    roots: dict[str, dict[str, Any]] = {}
    pool_root = Path(args.pool_root).expanduser().resolve()
    run_root = Path(args.run_root).expanduser().resolve()
    add_reset_root(roots, pool_root, source="configured_pool_root")
    add_reset_root(roots, run_root / "sterile-scratch-pools", source="configured_run_root_sterile")
    for extra in getattr(args, "extra_pool_root", None) or []:
        extra_root = Path(str(extra)).expanduser()
        if not extra_root.is_absolute():
            extra_root = Path.cwd() / extra_root
        add_reset_root(roots, extra_root.resolve(), source="extra_pool_root")

    if not getattr(args, "no_process_discovery", False):
        for pid, info in process_table.items():
            command = str(info.get("command") or "")
            tokens = parse_command_tokens(command)
            for index, token in enumerate(tokens):
                value: str | None = None
                source = "process_command"
                if token == "--pool-root" and index + 1 < len(tokens):
                    value = tokens[index + 1]
                    source = "process_pool_root"
                elif token.startswith("--pool-root="):
                    value = token.split("=", 1)[1]
                    source = "process_pool_root"
                elif token == "--run-root" and index + 1 < len(tokens):
                    run_value = resolved_absolute_path(tokens[index + 1])
                    add_reset_root(
                        roots,
                        run_value / "sterile-scratch-pools" if run_value else None,
                        source="process_run_root_sterile",
                        pid=pid,
                    )
                elif token.startswith("--run-root="):
                    run_value = resolved_absolute_path(token.split("=", 1)[1])
                    add_reset_root(
                        roots,
                        run_value / "sterile-scratch-pools" if run_value else None,
                        source="process_run_root_sterile",
                        pid=pid,
                    )
                elif token == "--pool-dir" and index + 1 < len(tokens):
                    pool_dir = resolved_absolute_path(tokens[index + 1])
                    add_reset_root(
                        roots, pool_dir.parent if pool_dir else None, source="process_pool_dir_parent", pid=pid
                    )
                elif token.startswith("--pool-dir="):
                    pool_dir = resolved_absolute_path(token.split("=", 1)[1])
                    add_reset_root(
                        roots, pool_dir.parent if pool_dir else None, source="process_pool_dir_parent", pid=pid
                    )
                if value is not None:
                    add_reset_root(roots, resolved_absolute_path(value), source=source, pid=pid)
                sterile_root = sterile_pool_root_from_path_text(token)
                if sterile_root is not None:
                    add_reset_root(roots, sterile_root, source="process_sterile_path", pid=pid)

    results = []
    for item in roots.values():
        results.append(
            {
                "pool_root": item["pool_root"],
                "sources": sorted(item["sources"]),
                "process_pids": sorted(item["process_pids"]),
            }
        )
    return sorted(results, key=lambda item: item["pool_root"])


def reset_process_pids_for_root(
    root: dict[str, Any],
    process_table: dict[int, dict[str, Any]],
) -> list[int]:
    return sorted(reset_process_pid_selection(root, process_table))


def reset_process_pid_selection(
    root: dict[str, Any],
    process_table: dict[int, dict[str, Any]],
) -> dict[int, list[str]]:
    """Return selected process ids and the path-safe reason for each selection."""

    pool_root = resolved_absolute_path(root.get("pool_root"))
    reasons: dict[int, set[str]] = {}

    def add_reason(pid: int, reason: str) -> None:
        reasons.setdefault(pid, set()).add(reason)

    for pid in root.get("process_pids") or []:
        with suppress(TypeError, ValueError):
            add_reason(int(pid), "explicitly discovered process seed")

    if pool_root is not None:
        for pid, info in process_table.items():
            tokens = parse_command_tokens(str(info.get("command") or ""))
            for index, token in enumerate(tokens):
                option: str | None = None
                value: str | None = None
                if token == "--pool-root" and index + 1 < len(tokens):
                    option, value = "--pool-root", tokens[index + 1]
                elif token.startswith("--pool-root="):
                    option, value = "--pool-root", token.split("=", 1)[1]
                elif token == "--pool-dir" and index + 1 < len(tokens):
                    option, value = "--pool-dir", tokens[index + 1]
                elif token.startswith("--pool-dir="):
                    option, value = "--pool-dir", token.split("=", 1)[1]
                elif token == "--run-root" and index + 1 < len(tokens):
                    option, value = "--run-root", tokens[index + 1]
                elif token.startswith("--run-root="):
                    option, value = "--run-root", token.split("=", 1)[1]

                path = resolved_absolute_path(value)
                if option == "--pool-root" and path == pool_root:
                    add_reason(pid, f"exact {option} path {path}")
                elif option == "--pool-dir" and path is not None:
                    try:
                        path.relative_to(pool_root)
                    except ValueError:
                        pass
                    else:
                        add_reason(pid, f"{option} path beneath pool root: {path}")
                elif option == "--run-root" and path is not None:
                    sterile_root = (path / "sterile-scratch-pools").resolve()
                    if sterile_root == pool_root:
                        add_reason(pid, f"exact {option} sterile pool root {path}")

    seed_pids = set(reasons)
    descendants = descendant_pids(seed_pids, process_table)
    for pid in descendants:
        add_reason(pid, "descendant of a selected process")
    return {pid: sorted(pid_reasons) for pid, pid_reasons in reasons.items()}


def pool_reset_summary(root_reports: list[dict[str, Any]]) -> dict[str, int]:
    selected = protected = stopped = deleted = failed = 0
    terminated = 0
    process_cleanup_failures = 0
    surviving_processes = 0
    roots_with_pools = 0
    for item in root_reports:
        gc = item.get("gc") or {}
        summary = gc.get("summary") or {}
        candidates = gc.get("candidates") or []
        protected_items = gc.get("protected") or []
        if candidates or protected_items:
            roots_with_pools += 1
        selected += int(summary.get("selected_candidates") or 0)
        protected += len(protected_items)
        stopped += int(summary.get("stopped") or 0)
        deleted += int(summary.get("deleted") or 0)
        failed += int(summary.get("failed") or 0)
        process_cleanup = item.get("process_cleanup") or {}
        terminated += int(process_cleanup.get("terminated_processes") or 0)
        if process_cleanup.get("verified_quiescent") is False:
            process_cleanup_failures += 1
            failed += 1
        surviving_processes += len(process_cleanup.get("survivor_pids") or [])
    return {
        "roots_scanned": len(root_reports),
        "roots_with_pools": roots_with_pools,
        "selected_candidates": selected,
        "protected_candidates": protected,
        "stopped": stopped,
        "deleted": deleted,
        "terminated_processes": terminated,
        "process_cleanup_failures": process_cleanup_failures,
        "surviving_processes": surviving_processes,
        "failed": failed,
    }
