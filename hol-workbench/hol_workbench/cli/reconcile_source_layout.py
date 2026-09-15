"""Developer-only materialization of pinned logical source trees."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from hol_workbench.logical_source_roots import (
    SOURCE_GENERATION_SCHEMA,
    LogicalSourceRootError,
    load_managed_source_generation,
    logical_source_root_declarations,
    managed_source_paths,
)
from hol_workbench.secure_tree_read import read_regular_file_beneath


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check or materialize pinned logical source trees")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--materialize", metavar="ALIAS")
    return parser


def _rows(checkout: Path) -> dict[str, dict[str, str]]:
    payload = json.loads((checkout / "hol-workbench" / "warmup-profiles.json").read_bytes())
    found: dict[str, dict[str, str]] = {}
    for profile, record in payload["profiles"].items():
        for row in logical_source_root_declarations(record.get("logical_source_roots"), profile=profile):
            if row["source_role"] != "managed_mirror":
                continue
            previous = found.setdefault(row["alias"], row)
            if previous != row:
                raise LogicalSourceRootError(
                    "refused_logical_source_root_declaration",
                    f"managed alias {row['alias']!r} has conflicting pinned identities",
                )
    return found


def _git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", *args),
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )


def _prepare_bare(row: dict[str, str], bare: Path) -> None:
    if bare.exists() or bare.is_symlink():
        if bare.is_symlink() or not bare.is_dir():
            raise RuntimeError(f"managed bare cache is not a regular directory: {bare}")
        if _git("--git-dir", str(bare), "rev-parse", "--is-bare-repository").stdout.strip() != b"true":
            raise RuntimeError(f"managed bare cache is not bare: {bare}")
        remote = _git("--git-dir", str(bare), "remote", "get-url", "origin").stdout.decode().strip()
        if remote != row["remote"]:
            raise RuntimeError(f"managed bare cache origin is {remote!r}, expected {row['remote']!r}")
    else:
        bare.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{bare.name}.staging-", dir=bare.parent))
        shutil.rmtree(temporary)
        try:
            _git("clone", "--mirror", row["remote"], str(temporary))
            os.rename(temporary, bare)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    _git("--git-dir", str(bare), "fetch", "--prune", "origin", row["revision"])
    commit = _git("--git-dir", str(bare), "rev-parse", f"{row['revision']}^{{commit}}").stdout.decode().strip()
    tree = _git("--git-dir", str(bare), "rev-parse", f"{row['revision']}^{{tree}}").stdout.decode().strip()
    if commit != row["revision"] or tree != row["tree"]:
        raise RuntimeError(f"canonical remote does not provide declared commit/tree for {row['alias']}")


def _extract(row: dict[str, str], bare: Path, staging: Path) -> dict[str, dict[str, object]]:
    process = subprocess.Popen(
        ("git", "--git-dir", str(bare), "archive", "--format=tar", row["revision"]),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    files: dict[str, dict[str, object]] = {}
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
            for member in archive:
                portable = PurePosixPath(member.name)
                if portable.is_absolute() or not portable.parts or any(part in {"", ".", ".."} for part in portable.parts):
                    raise RuntimeError(f"canonical archive contains unsafe path {member.name!r}")
                target = staging.joinpath(*portable.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise RuntimeError(f"canonical archive contains unsupported non-file {member.name!r}")
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"cannot extract canonical archive file {member.name!r}")
                data = source.read()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o555 if member.mode & 0o111 else 0o444)
                files[portable.as_posix()] = {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size_bytes": len(data),
                }
    finally:
        process.stdout.close()
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr is not None else ""
    if process.wait() != 0:
        raise RuntimeError(f"git archive failed: {stderr[-1000:]}")
    for directory, directories, _names in os.walk(staging, topdown=False):
        for name in directories:
            (Path(directory) / name).chmod(0o555)
    staging.chmod(0o555)
    return files


def _generation(row: dict[str, str], tree_root: Path, files: dict[str, dict[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": SOURCE_GENERATION_SCHEMA,
        "alias": row["alias"],
        "remote": row["remote"],
        "revision": row["revision"],
        "tree": row["tree"],
        "files": files,
    }
    payload["strict_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload["local_tree_root"] = str(tree_root)
    return payload


def _validate_all(row: dict[str, str]) -> dict[str, object]:
    tree, generation = load_managed_source_generation(row)
    for relative, raw in generation["files"].items():
        if not isinstance(raw, dict):
            raise RuntimeError(f"invalid generation file row: {relative}")
        data = read_regular_file_beneath(tree, tree / relative).data
        if hashlib.sha256(data).hexdigest() != raw.get("sha256") or len(data) != raw.get("size_bytes"):
            raise RuntimeError(f"managed source tree differs from generation: {relative}")
    return {
        "alias": row["alias"],
        "revision": row["revision"],
        "tree": row["tree"],
        "files": len(generation["files"]),
        "generation_sha256": generation["strict_sha256"],
        "status": "ready",
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_generation(path: Path) -> None:
    for directory, directories, names in os.walk(path, topdown=False, followlinks=False):
        root = Path(directory)
        for name in names:
            descriptor = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for name in directories:
            _fsync_directory(root / name)
        _fsync_directory(root)


def _quarantine_invalid_generation(generation: Path) -> Path:
    container = Path(
        tempfile.mkdtemp(
            prefix=f".{generation.name}.invalid-",
            dir=generation.parent,
        )
    )
    quarantined = container / "generation"
    os.rename(generation, quarantined)
    _fsync_directory(generation.parent)
    return quarantined


def _materialize(row: dict[str, str]) -> dict[str, object]:
    bare, tree, _generation_path = managed_source_paths(row)
    generation = tree.parent
    lock = generation.parents[2] / "source-locks" / f"{row['alias']}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if generation.exists() or generation.is_symlink():
            try:
                return _validate_all(row)
            except (OSError, RuntimeError, ValueError):
                _quarantine_invalid_generation(generation)
        _prepare_bare(row, bare)
        generation.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{row['revision']}.staging-", dir=generation.parent))
        try:
            staged_tree = staging / "tree"
            staged_tree.mkdir()
            files = _extract(row, bare, staged_tree)
            staged_manifest = staging / "manifest.json"
            staged_manifest.write_text(
                json.dumps(_generation(row, tree, files), sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            _fsync_generation(staging)
            os.rename(staging, generation)
            _fsync_directory(generation.parent)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return _validate_all(row)
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    checkout = Path(__file__).resolve().parents[3]
    try:
        rows = _rows(checkout)
        if args.check:
            results = [_validate_all(row) for row in rows.values()]
        else:
            row = rows.get(args.materialize)
            if row is None:
                raise RuntimeError(f"unknown managed logical source alias: {args.materialize}")
            results = [_materialize(row)]
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"reconcile-source-layout: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"schema": "hol-workbench.source-layout-reconcile.v1", "results": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
