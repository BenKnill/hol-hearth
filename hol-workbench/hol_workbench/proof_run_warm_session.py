"""Compatibility cards plus neutral warm-session lifecycle exports."""

from __future__ import annotations

from pathlib import Path

from hol_workbench.ids import utc_now
from hol_workbench.jsonio import write_text_lines
from hol_workbench.pools.session_lifecycle import (
    pool_session_alive as warm_pool_session_alive,
)
from hol_workbench.pools.session_lifecycle import (
    read_session as read_warm_session,
)
from hol_workbench.pools.session_lifecycle import (
    send_session_request as warm_send_request,
)
from hol_workbench.pools.session_lifecycle import (
    session_socket_path as warm_socket_path,
)
from hol_workbench.pools.store import write_warm_pool_json

RAW_LOG_POLICY = "do not read wholesale; inspect compact summaries first"


def _short_sha(value: object) -> str:
    return str(value)[:12] if value else "unknown"


def write_warm_session_card(path: Path, session: dict) -> None:
    """Write the session card still consumed by logical-capacity maintenance."""

    session_dir = Path(session.get("session_dir", "."))
    fork_safety = session.get("fork_safety") or {}
    lines = [
        "WARM HOL SESSION CARD",
        "=====================",
        f"status: {session.get('status')}",
        f"session_id: {session.get('session_id')}",
        f"session_dir: {session.get('session_dir')}",
        f"holdir: {session.get('holdir')}",
        f"cwd: {session.get('cwd')}",
        f"worker_pid: {session.get('worker_pid')}",
        f"hol_pid: {session.get('hol_pid')}",
        f"fork_active_thread_check: {fork_safety.get('status') or '-'}",
        f"fork_active_os_thread_count: {fork_safety.get('os_thread_count') if fork_safety else '-'}",
        "",
        "evidence grade:",
        "- warm sessions are authoring infrastructure, not final theorem evidence.",
        "",
        "reading order:",
        f"1. session card: {path}",
        f"2. session JSON: {session_dir / 'session.json'}",
        f"3. raw log, bounded grep/tail only: {session_dir / 'session.raw.log'}",
        "",
        "raw log policy:",
        f"- {RAW_LOG_POLICY}",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_lines(path, lines)


def write_warm_pool(pool_dir: Path, pool: dict) -> None:
    """Compatibility writer for maintenance callers that still need a card."""

    write_warm_pool_json(pool_dir, pool, updated_utc=utc_now())
    write_warm_pool_card(pool_dir / "pool-card.txt", pool)


def write_warm_pool_card(path: Path, pool: dict) -> None:
    pool_dir = Path(pool.get("pool_dir", "."))
    engine = pool.get("engine") or "process"
    lines = [
        "WARM HOL POOL CARD",
        "==================",
        f"status: {pool.get('status')}",
        f"pool_id: {pool.get('pool_id')}",
        f"pool_dir: {pool.get('pool_dir')}",
        f"size: {len(pool.get('sessions') or [])}",
        f"engine: {engine}",
        f"holdir: {pool.get('holdir')}",
        f"cwd: {pool.get('cwd')}",
        "",
        "sessions:",
    ]
    for item in pool.get("sessions") or []:
        loaded = item.get("loaded_preloads") or []
        basis = "none"
        if loaded:
            basis = ", ".join(
                f"{Path(entry.get('path', '')).name}:{_short_sha(entry.get('sha256'))}:{entry.get('status')}"
                for entry in loaded
            )
        missing = item.get("basis_missing_preloads") or []
        missing_text = ""
        if missing:
            missing_text = " missing=" + ",".join(
                f"{Path(entry.get('path', '')).name}:{_short_sha(entry.get('sha256'))}" for entry in missing
            )
        lines.append(
            f"- worker {item.get('worker_index')}: {item.get('status')} "
            f"hol_pid={item.get('hol_pid')} basis_ready={item.get('basis_ready', True)} "
            f"basis={basis}{missing_text} session={item.get('session_dir')}"
        )
    preloads = pool.get("preloads") or []
    if preloads:
        lines += ["", "preloads:"]
        for preload in preloads:
            lines.append(f"- {preload.get('path')} sha256={preload.get('sha256')} status={preload.get('status')}")
    fork_safety = pool.get("fork_safety") or {}
    if fork_safety:
        lines += [
            "",
            "fork safety:",
            f"- active thread check: {fork_safety.get('status')}",
            f"- observed OS thread count: {fork_safety.get('os_thread_count')}",
            f"- preload contract: {fork_safety.get('preload_contract')}",
            f"- evidence boundary: {fork_safety.get('evidence_boundary')}",
        ]
    lines += [
        "",
        "evidence:",
        "- pool state is authoring infrastructure, not final theorem evidence.",
        "- each active seat has one identity-bound exclusive lease.",
        "- a fork-basis evaluation runs in a disposable child.",
        "",
        "reading order:",
        f"1. pool card: {path}",
        f"2. pool JSON: {pool_dir / 'pool.json'}",
        "3. session JSON from pool.json sessions[].session_dir",
        "",
        "raw log policy:",
        f"- {RAW_LOG_POLICY}",
    ]
    write_text_lines(path, lines)


__all__ = [
    "read_warm_session",
    "warm_pool_session_alive",
    "warm_send_request",
    "warm_socket_path",
    "write_warm_pool",
    "write_warm_pool_card",
    "write_warm_session_card",
]
