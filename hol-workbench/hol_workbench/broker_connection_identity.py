"""OS-attested identity for one admitted Unix broker connection."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Any

BROKER_ENDPOINT_IDENTITY_SCHEMA = "hol-workbench.broker-endpoint-identity.v2"
BROKER_CONNECTION_IDENTITY_SCHEMA = "hol-workbench.broker-connection-identity.v2"
_LINUX_UNIX_SOCKET_TABLE = Path("/proc/net/unix")
_LINUX_NETWORK_NAMESPACE = Path("/proc/self/ns/net")
_LINUX_LISTENING_SOCKET_FLAG = 0x00010000


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _linux_listener_kernel_identity(endpoint: Path) -> dict[str, int]:
    # A socket pathname's st_ino can be recycled immediately after unlink.
    # /proc records the live kernel socket object's distinct inode; accepted
    # children may retain the path, so only the SO_ACCEPTCON row identifies
    # the broker listener.
    encoded_endpoint = os.fsencode(str(endpoint))
    if b"\n" in encoded_endpoint:
        raise RuntimeError("broker endpoint cannot be represented exactly in the Linux Unix-socket table")
    try:
        namespace = _LINUX_NETWORK_NAMESPACE.stat()
        raw_table = _LINUX_UNIX_SOCKET_TABLE.read_bytes()
    except OSError as exc:
        raise RuntimeError("broker endpoint has no observable Linux kernel socket identity") from exc

    matching_rows = []
    for raw_line in raw_table.splitlines()[1:]:
        fields = raw_line.split(None, 7)
        if len(fields) == 8 and fields[7] == encoded_endpoint:
            matching_rows.append(fields)
    listeners: list[int] = []
    for row in matching_rows:
        _number, _references, _protocol, raw_flags, raw_type, raw_state, raw_inode, _path = row
        try:
            flags = int(raw_flags, 16)
            socket_type = int(raw_type, 16)
            state = int(raw_state, 16)
            socket_inode = int(raw_inode, 10)
        except ValueError as exc:
            raise RuntimeError("broker endpoint has a malformed Linux kernel socket identity") from exc
        if (
            flags & _LINUX_LISTENING_SOCKET_FLAG != 0
            and socket_type == socket.SOCK_STREAM
            and state == 1
            and socket_inode > 0
        ):
            listeners.append(socket_inode)
    if len(listeners) != 1:
        raise RuntimeError(
            "broker endpoint has no unique live Linux kernel socket identity: "
            f"observed {len(listeners)} matching listeners"
        )
    socket_inode = listeners[0]
    return {
        "network_namespace_device": namespace.st_dev,
        "network_namespace_inode": namespace.st_ino,
        "socket_inode": socket_inode,
    }


def _endpoint_filesystem_identity(endpoint: Path) -> dict[str, int]:
    try:
        metadata = endpoint.lstat()
    except OSError as exc:
        raise RuntimeError(f"broker endpoint is unavailable: {endpoint}: {exc}") from exc
    if not stat.S_ISSOCK(metadata.st_mode):
        raise RuntimeError(f"broker endpoint is not an exact Unix socket: {endpoint}")
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "mode": metadata.st_mode,
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
    }


def observe_broker_endpoint(endpoint: Path) -> dict[str, Any]:
    path = endpoint.expanduser()
    if not path.is_absolute():
        raise RuntimeError("broker endpoint is not absolute")
    filesystem_before = _endpoint_filesystem_identity(path)
    kernel_before = _linux_listener_kernel_identity(path)
    filesystem_after = _endpoint_filesystem_identity(path)
    kernel_after = _linux_listener_kernel_identity(path)
    if filesystem_before != filesystem_after or kernel_before != kernel_after:
        raise RuntimeError("broker endpoint changed while its live identity was observed")
    payload = {
        "schema": BROKER_ENDPOINT_IDENTITY_SCHEMA,
        "path": str(path),
        **filesystem_before,
        "linux_listener": kernel_before,
    }
    return {**payload, "identity_sha256": _canonical_sha256(payload)}


def validate_broker_endpoint_identity(record: object) -> dict[str, Any]:
    if not isinstance(record, Mapping) or record.get("schema") != BROKER_ENDPOINT_IDENTITY_SCHEMA:
        raise RuntimeError("broker endpoint identity schema is incompatible")
    data = dict(record)
    payload = {key: value for key, value in data.items() if key != "identity_sha256"}
    if data.get("identity_sha256") != _canonical_sha256(payload):
        raise RuntimeError("broker endpoint identity digest is invalid")
    observed = observe_broker_endpoint(Path(str(data.get("path") or "")))
    if observed != data:
        raise RuntimeError("broker endpoint was replaced after live admission")
    return data


def _linux_process_start_ticks(pid: int) -> int:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        _, separator, tail = raw.rpartition(")")
        if not separator:
            raise ValueError("missing process stat delimiter")
        ticks = int(tail.split()[19])
    except (OSError, IndexError, ValueError) as exc:
        raise RuntimeError(f"broker peer {pid} has no stable Linux birth identity") from exc
    if ticks <= 0:
        raise RuntimeError(f"broker peer {pid} has no stable Linux birth identity")
    return ticks


def observe_unix_peer(connection: socket.socket) -> dict[str, Any]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise RuntimeError("broker connection has no OS-attested peer identity")
    size = struct.calcsize("3i")
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
        pid, uid, gid = struct.unpack("3i", raw)
    except (OSError, struct.error) as exc:
        raise RuntimeError("broker connection has no OS-attested peer identity") from exc
    if pid <= 0 or uid < 0 or gid < 0:
        raise RuntimeError("broker connection returned an invalid OS-attested peer identity")
    ticks = _linux_process_start_ticks(pid)
    return {
        "pid": pid,
        "uid": uid,
        "gid": gid,
        "start_ticks": ticks,
        "birth_identity": f"linux-proc:{ticks}",
    }


def broker_connection_identity_record(
    *,
    endpoint: Mapping[str, Any],
    connected_endpoint: str,
    peer: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema": BROKER_CONNECTION_IDENTITY_SCHEMA,
        "endpoint": dict(endpoint),
        "connected_endpoint": connected_endpoint,
        "peer": dict(peer),
    }
    return {**payload, "identity_sha256": _canonical_sha256(payload)}


def validate_broker_connection_identity(
    record: object,
    *,
    expected_endpoint: Mapping[str, Any],
    expected_broker: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(record, Mapping) or record.get("schema") != BROKER_CONNECTION_IDENTITY_SCHEMA:
        raise RuntimeError("broker connection identity schema is incompatible")
    data = dict(record)
    payload = {key: value for key, value in data.items() if key != "identity_sha256"}
    if data.get("identity_sha256") != _canonical_sha256(payload):
        raise RuntimeError("broker connection identity digest is invalid")
    if data.get("endpoint") != dict(expected_endpoint):
        raise RuntimeError("broker connection identity differs from the admitted endpoint")
    if data.get("connected_endpoint") != expected_endpoint.get("path"):
        raise RuntimeError("broker connection identity did not observe the admitted endpoint")
    peer = data.get("peer")
    if not isinstance(peer, Mapping):
        raise RuntimeError("broker connection identity has no OS-attested peer")
    if (
        peer.get("pid") != expected_broker.get("pid")
        or peer.get("start_ticks") != expected_broker.get("start_ticks")
        or peer.get("birth_identity") != expected_broker.get("birth_identity")
    ):
        raise RuntimeError("broker connection identity differs from the admitted worker")
    peer_uid = peer.get("uid")
    peer_gid = peer.get("gid")
    if (
        not isinstance(peer_uid, int)
        or isinstance(peer_uid, bool)
        or peer_uid < 0
        or not isinstance(peer_gid, int)
        or isinstance(peer_gid, bool)
        or peer_gid < 0
    ):
        raise RuntimeError("broker connection identity has invalid OS peer credentials")
    return data


def observe_broker_connection(
    connection: socket.socket,
    *,
    expected_endpoint: Mapping[str, Any],
    expected_broker: Mapping[str, Any],
) -> dict[str, Any]:
    endpoint = validate_broker_endpoint_identity(expected_endpoint)
    try:
        connected_endpoint = os.fsdecode(connection.getpeername())
    except (OSError, TypeError) as exc:
        raise RuntimeError("broker connection has no OS-attested endpoint identity") from exc
    if connected_endpoint != endpoint.get("path"):
        raise RuntimeError(
            "actual broker connection endpoint differs from the admitted endpoint: "
            f"observed={connected_endpoint!r} expected={endpoint.get('path')!r}"
        )
    peer = observe_unix_peer(connection)
    if peer.get("pid") != expected_broker.get("pid") or peer.get("birth_identity") != expected_broker.get(
        "birth_identity"
    ):
        raise RuntimeError("broker connection peer differs from the admitted worker identity")
    record = broker_connection_identity_record(
        endpoint=endpoint,
        connected_endpoint=connected_endpoint,
        peer=peer,
    )
    return validate_broker_connection_identity(
        record,
        expected_endpoint=endpoint,
        expected_broker=expected_broker,
    )
