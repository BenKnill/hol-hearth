"""Mechanical binary protocol and immutable runtime bundle for the fork-basis broker."""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import struct
from pathlib import Path
from typing import Any

BROKER_PROTOCOL = "hol-workbench.fork-basis-broker.v1"
BROKER_FORK_SNAPSHOT_ABI = "proof-run-fork-snapshot.v3"
BROKER_DESCRIBE_SCHEMA = "hol-workbench.fork-basis-broker.describe.v1"
BROKER_CONTROL_SCHEMA = "hol-workbench.fork-basis-broker.control.v1"
BROKER_EVENT_SCHEMA = "hol-workbench.fork-basis-broker.event.v1"
BROKER_FRAME_CONTROL = 1
BROKER_FRAME_RAW_OUTPUT = 2
BROKER_FRAME_HEADER = struct.Struct("!BI")
BROKER_MAX_CONTROL_BYTES = 1024 * 1024
BROKER_MAX_RAW_BYTES = 4 * 1024 * 1024
BROKER_TRANSPORT_SAFETY_TIMEOUT_SECONDS = 5.0

BROKER_RUNTIME_FILES = (
    "__init__.py",
    "fork_broker_protocol.py",
    "proof_run_fork_basis_safety.py",
    "proof_run_fork_broker_children.py",
    "proof_run_fork_broker.py",
)


class BrokerProtocolError(RuntimeError):
    """The peer violated the mechanical broker wire contract."""


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = conn.recv(remaining)
        if not chunk:
            raise EOFError("broker peer disconnected")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_frame(conn: socket.socket, frame_type: int, payload: bytes) -> None:
    if frame_type not in {BROKER_FRAME_CONTROL, BROKER_FRAME_RAW_OUTPUT}:
        raise BrokerProtocolError(f"unknown broker frame type: {frame_type}")
    maximum = BROKER_MAX_CONTROL_BYTES if frame_type == BROKER_FRAME_CONTROL else BROKER_MAX_RAW_BYTES
    if len(payload) > maximum:
        raise BrokerProtocolError(f"broker frame exceeds {maximum} bytes")
    conn.sendall(BROKER_FRAME_HEADER.pack(frame_type, len(payload)) + payload)


def receive_frame(conn: socket.socket) -> tuple[int, bytes]:
    frame_type, size = BROKER_FRAME_HEADER.unpack(_recv_exact(conn, BROKER_FRAME_HEADER.size))
    if frame_type not in {BROKER_FRAME_CONTROL, BROKER_FRAME_RAW_OUTPUT}:
        raise BrokerProtocolError(f"unknown broker frame type: {frame_type}")
    maximum = BROKER_MAX_CONTROL_BYTES if frame_type == BROKER_FRAME_CONTROL else BROKER_MAX_RAW_BYTES
    if size > maximum:
        raise BrokerProtocolError(f"broker frame exceeds {maximum} bytes")
    return frame_type, _recv_exact(conn, size)


def send_control(conn: socket.socket, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    send_frame(conn, BROKER_FRAME_CONTROL, encoded)


def receive_control(conn: socket.socket) -> dict[str, Any]:
    frame_type, raw = receive_frame(conn)
    if frame_type != BROKER_FRAME_CONTROL:
        raise BrokerProtocolError("expected a broker control frame")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerProtocolError(f"malformed broker control frame: {exc}") from exc
    if not isinstance(payload, dict):
        raise BrokerProtocolError("broker control frame must contain an object")
    return payload


def send_raw_output(conn: socket.socket, payload: bytes) -> None:
    send_frame(conn, BROKER_FRAME_RAW_OUTPUT, payload)


def broker_runtime_sha256(package_root: Path | None = None) -> str:
    root = (package_root or Path(__file__).resolve().parent).resolve()
    digest = hashlib.sha256()
    for name in BROKER_RUNTIME_FILES:
        digest.update(name.encode("utf-8") + b"\0")
        digest.update((root / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def materialize_broker_runtime_bundle(destination: Path, package_root: Path | None = None) -> dict[str, Any]:
    """Freeze the exact import source used by a broker publication."""

    source = (package_root or Path(__file__).resolve().parent).resolve()
    destination = destination.expanduser().resolve()
    package = destination / "hol_workbench"
    if destination.exists():
        raise RuntimeError(f"broker runtime bundle already exists: {destination}")
    package.mkdir(parents=True)
    files: list[dict[str, Any]] = []
    for name in BROKER_RUNTIME_FILES:
        source_path = source / name
        target = package / name
        shutil.copyfile(source_path, target)
        files.append(
            {
                "path": f"hol_workbench/{name}",
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "size_bytes": target.stat().st_size,
            }
        )
    runtime_sha256 = broker_runtime_sha256(package)
    manifest = {
        "schema": "hol-workbench.fork-basis-broker-runtime-bundle.v1",
        "broker_protocol": BROKER_PROTOCOL,
        "broker_runtime_sha256": runtime_sha256,
        "files": files,
    }
    (destination / "broker-runtime-bundle.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest
