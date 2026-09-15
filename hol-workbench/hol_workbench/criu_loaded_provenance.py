"""Exact loaded-HOL and OCaml runtime provenance for CRIU shelves.

This module is intentionally independent of the OrbStack build/restore CLI.
It defines the fail-closed capture, resolution, and validation contract used
by the authoritative snapshot manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hol_workbench.hashing import sha256_file

LOADER_EXPORT_SCHEMA = "hol-workbench.loaded-files-export.v1"
LOADED_CLOSURE_SCHEMA = "hol-workbench.loaded-closure.v1"
RUNTIME_CLOSURE_SCHEMA = "hol-workbench.ocaml-runtime-closure.v1"
_EXPORT_PREFIX = "__HOL_WORKBENCH_LOADED_FILE_V1__"
_EXPORT_BEGIN = f"{_EXPORT_PREFIX}:BEGIN"
_EXPORT_END_PREFIX = f"{_EXPORT_PREFIX}:END:"
_EXPORT_RECORD = re.compile(rf"^{re.escape(_EXPORT_PREFIX)}:([0-9a-f]*):([0-9a-f]{{32}})$")


class LoadedProvenanceError(RuntimeError):
    """The loaded closure cannot be represented or validated exactly."""


@dataclass(frozen=True, order=True)
class LoaderRecord:
    basename: str
    loader_md5: str

    def __post_init__(self) -> None:
        if not self.basename or Path(self.basename).name != self.basename:
            raise ValueError(f"loader basename is not a basename: {self.basename!r}")
        if not re.fullmatch(r"[0-9a-f]{32}", self.loader_md5):
            raise ValueError(f"loader digest is not canonical MD5 hex: {self.loader_md5!r}")


@dataclass(frozen=True)
class SemanticInput:
    path: Path
    role: str

    def __post_init__(self) -> None:
        if not self.role or not re.fullmatch(r"[a-z][a-z0-9_-]*", self.role):
            raise ValueError(f"invalid semantic input role: {self.role!r}")


def loader_export_source() -> str:
    """Return a no-load OCaml payload that prints the inherited loader set.

    The warm fork worker loads this source with ``loadt``. HOL records that
    source only after its evaluation returns, so the payload observes the
    parent's inherited ``loaded_files`` without including itself.
    """

    return "\n".join(
        [
            "let hol_workbench_hex s =",
            "  let b = Buffer.create (2 * String.length s) in",
            '  String.iter (fun c -> Printf.bprintf b "%02x" (Char.code c)) s;',
            "  Buffer.contents b;;",
            f'print_endline "{_EXPORT_BEGIN}";;',
            "List.iter",
            "  (fun (name,digest) ->",
            f'    Printf.printf "{_EXPORT_PREFIX}:%s:%s\\n%!"',
            "      (hol_workbench_hex name) (Digest.to_hex digest))",
            "  (List.rev !Hol_loader.loaded_files);;",
            f'Printf.printf "{_EXPORT_END_PREFIX}%d\\n%!"',
            "  (List.length !Hol_loader.loaded_files);;",
            "",
        ]
    )


def parse_loader_export(text: str) -> list[LoaderRecord]:
    """Parse exactly one complete, versioned loader export from a transcript."""

    lines = text.splitlines()
    begin_indexes = [index for index, line in enumerate(lines) if line.strip() == _EXPORT_BEGIN]
    end_rows = [
        (index, line.strip()) for index, line in enumerate(lines) if line.strip().startswith(_EXPORT_END_PREFIX)
    ]
    if len(begin_indexes) != 1 or len(end_rows) != 1:
        raise LoadedProvenanceError("loader export must contain exactly one begin marker and one end marker")
    begin = begin_indexes[0]
    end, end_line = end_rows[0]
    if end <= begin:
        raise LoadedProvenanceError("loader export end marker precedes its begin marker")
    raw_count = end_line.removeprefix(_EXPORT_END_PREFIX)
    if not raw_count.isdigit():
        raise LoadedProvenanceError(f"loader export has invalid record count: {raw_count!r}")

    records: list[LoaderRecord] = []
    for line in lines[begin + 1 : end]:
        stripped = line.strip()
        if not stripped.startswith(f"{_EXPORT_PREFIX}:"):
            continue
        match = _EXPORT_RECORD.fullmatch(stripped)
        if match is None:
            raise LoadedProvenanceError(f"malformed loader export record: {stripped!r}")
        try:
            basename = os.fsdecode(bytes.fromhex(match.group(1)))
        except ValueError as exc:
            raise LoadedProvenanceError("loader export basename is not canonical filesystem-name hex") from exc
        try:
            records.append(LoaderRecord(basename=basename, loader_md5=match.group(2)))
        except ValueError as exc:
            raise LoadedProvenanceError(str(exc)) from exc
    expected = int(raw_count)
    if len(records) != expected:
        raise LoadedProvenanceError(f"loader export count mismatch: marker={expected} parsed={len(records)}")
    return records


def _md5_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                digest.update(chunk)
    except OSError as exc:
        raise LoadedProvenanceError(f"cannot hash loaded input {path}: {exc}") from exc
    return digest.hexdigest()


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _candidate_map(
    holdir: Path,
    *,
    wanted_basenames: set[str],
    semantic_inputs: list[SemanticInput],
) -> dict[str, dict[Path, set[str]]]:
    candidates: dict[str, dict[Path, set[str]]] = defaultdict(lambda: defaultdict(set))
    for root, directory_names, file_names in os.walk(holdir, followlinks=False):
        directory_names.sort()
        for name in sorted(file_names):
            if name in wanted_basenames:
                candidates[name][_lexical_absolute(Path(root) / name)].add("holdir")
    for item in semantic_inputs:
        path = _lexical_absolute(item.path)
        if path.is_dir():
            for root, directory_names, file_names in os.walk(path, followlinks=False):
                directory_names.sort()
                for name in sorted(file_names):
                    if name in wanted_basenames:
                        candidates[name][_lexical_absolute(Path(root) / name)].add(item.role)
        elif path.name in wanted_basenames:
            candidates[path.name][path].add(item.role)
    return candidates


def _stored_path_identity(path: Path, *, holdir: Path) -> tuple[str, str]:
    try:
        relative = path.relative_to(holdir)
    except ValueError:
        return "absolute", str(path)
    return "holdir_relative", relative.as_posix()


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolve_loaded_closure(
    records: list[LoaderRecord],
    *,
    holdir: Path,
    semantic_inputs: list[SemanticInput],
) -> dict[str, Any]:
    """Resolve every unique loader record to exactly one lexical path.

    Matching is basename plus the loader's MD5. Equal-content copies and
    symlink aliases remain distinct lexical candidates and therefore fail as
    ambiguous; the loader record does not contain enough information to choose
    soundly between them.
    """

    if not records:
        raise LoadedProvenanceError("loader export is empty")
    holdir = _lexical_absolute(holdir)
    if not holdir.is_dir():
        raise LoadedProvenanceError(f"HOL directory is missing or not a directory: {holdir}")
    occurrences = Counter(records)
    candidates = _candidate_map(
        holdir,
        wanted_basenames={record.basename for record in occurrences},
        semantic_inputs=semantic_inputs,
    )
    entries: list[dict[str, Any]] = []
    for record in sorted(occurrences):
        matching: list[tuple[Path, set[str]]] = []
        for path, roles in sorted(candidates.get(record.basename, {}).items()):
            if not path.is_file():
                continue
            if _md5_file(path) == record.loader_md5:
                matching.append((path, roles))
        if not matching:
            raise LoadedProvenanceError(
                f"unresolved loader record: basename={record.basename!r} loader_md5={record.loader_md5}"
            )
        if len(matching) != 1:
            rendered = ", ".join(str(path) for path, _roles in matching)
            raise LoadedProvenanceError(
                "ambiguous loader record: "
                f"basename={record.basename!r} loader_md5={record.loader_md5} "
                f"matches=[{rendered}]"
            )
        path, roles = matching[0]
        sha256 = sha256_file(path)
        if sha256 is None:
            raise LoadedProvenanceError(f"cannot hash loaded input {path} with SHA-256")
        path_kind, stored_path = _stored_path_identity(path, holdir=holdir)
        try:
            resolved_path = str(path.resolve(strict=True))
            size = path.stat().st_size
        except OSError as exc:
            raise LoadedProvenanceError(f"cannot stat loaded input {path}: {exc}") from exc
        entries.append(
            {
                "basename": record.basename,
                "loader_md5": record.loader_md5,
                "occurrences": occurrences[record],
                "path_kind": path_kind,
                "path": stored_path,
                "resolved_path": resolved_path,
                "roles": sorted(roles),
                "sha256": sha256,
                "size_bytes": size,
            }
        )
    strict_entries = [
        {
            key: entry[key]
            for key in (
                "basename",
                "loader_md5",
                "occurrences",
                "path_kind",
                "path",
                "resolved_path",
                "roles",
                "sha256",
            )
        }
        for entry in entries
    ]
    return {
        "schema": LOADED_CLOSURE_SCHEMA,
        "export_schema": LOADER_EXPORT_SCHEMA,
        "record_count": len(records),
        "unique_record_count": len(entries),
        "entries": entries,
        "strict_sha256": _canonical_digest(strict_entries),
    }


def runtime_file_identity(path: Path, *, role: str) -> dict[str, Any]:
    """Hash one exact OCaml runtime input, preserving logical and real paths."""

    logical_path = _lexical_absolute(path)
    if not logical_path.is_file():
        raise LoadedProvenanceError(f"runtime input is missing or not a file: {logical_path}")
    sha256 = sha256_file(logical_path)
    if sha256 is None:
        raise LoadedProvenanceError(f"cannot hash runtime input {logical_path}")
    try:
        return {
            "role": role,
            "path": str(logical_path),
            "resolved_path": str(logical_path.resolve(strict=True)),
            "sha256": sha256,
            "size_bytes": logical_path.stat().st_size,
        }
    except OSError as exc:
        raise LoadedProvenanceError(f"cannot stat runtime input {logical_path}: {exc}") from exc


def build_runtime_closure(*, ocaml_hol: Path, ocamlrun: Path) -> dict[str, Any]:
    entries = [
        runtime_file_identity(ocaml_hol, role="ocaml_hol"),
        runtime_file_identity(ocamlrun, role="ocamlrun"),
    ]
    strict_entries = [{key: entry[key] for key in ("role", "path", "resolved_path", "sha256")} for entry in entries]
    return {
        "schema": RUNTIME_CLOSURE_SCHEMA,
        "entries": entries,
        "strict_sha256": _canonical_digest(strict_entries),
    }


def loaded_closure_entry_path(entry: dict[str, Any], *, holdir: Path) -> Path:
    """Resolve one stored closure path without permitting HOLDIR escape."""

    path = str(entry.get("path") or "")
    if entry.get("path_kind") == "holdir_relative":
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            raise LoadedProvenanceError(f"invalid HOLDIR-relative closure path: {path!r}")
        return holdir / path
    if entry.get("path_kind") == "absolute" and Path(path).is_absolute():
        return Path(path)
    raise LoadedProvenanceError(f"invalid closure path identity: {entry!r}")


def validate_loaded_closure(closure: dict[str, Any], *, holdir: Path) -> list[str]:
    """Validate only stored closure paths; unrelated tree drift is ignored."""

    failures: list[str] = []
    if closure.get("schema") != LOADED_CLOSURE_SCHEMA:
        return ["loaded closure schema is incompatible"]
    entries = closure.get("entries")
    if not isinstance(entries, list) or not entries:
        return ["loaded closure entries are missing"]
    strict_entries: list[dict[str, Any]] = []
    loader_keys: set[tuple[str, str]] = set()
    occurrence_total = 0
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            failures.append(f"loaded closure entry {index} is invalid")
            continue
        entry = raw_entry
        loader_key = (str(entry.get("basename") or ""), str(entry.get("loader_md5") or ""))
        if loader_key in loader_keys:
            failures.append(f"loaded closure entry {index} duplicates loader identity {loader_key!r}")
        loader_keys.add(loader_key)
        occurrences = entry.get("occurrences")
        if not isinstance(occurrences, int) or isinstance(occurrences, bool) or occurrences < 1:
            failures.append(f"loaded closure entry {index} has invalid occurrence count")
        else:
            occurrence_total += occurrences
        try:
            path = loaded_closure_entry_path(entry, holdir=_lexical_absolute(holdir))
        except LoadedProvenanceError as exc:
            failures.append(str(exc))
            continue
        label = f"{entry.get('basename') or path.name} ({path})"
        if path.name != entry.get("basename"):
            failures.append(f"loaded closure basename/path mismatch: {label}")
        if not path.is_file():
            failures.append(f"loaded closure input is missing: {label}")
        else:
            try:
                current_md5 = _md5_file(path)
            except LoadedProvenanceError as exc:
                failures.append(str(exc))
                current_md5 = None
            current_sha256 = sha256_file(path)
            if current_md5 is not None and current_md5 != entry.get("loader_md5"):
                failures.append(f"loaded closure MD5 mismatch: {label}")
            if current_sha256 != entry.get("sha256"):
                failures.append(f"loaded closure SHA-256 mismatch: {label}")
            try:
                if str(path.resolve(strict=True)) != entry.get("resolved_path"):
                    failures.append(f"loaded closure resolved path mismatch: {label}")
            except OSError as exc:
                failures.append(f"cannot resolve loaded closure input {label}: {exc}")
        try:
            strict_entries.append(
                {
                    key: entry[key]
                    for key in (
                        "basename",
                        "loader_md5",
                        "occurrences",
                        "path_kind",
                        "path",
                        "resolved_path",
                        "roles",
                        "sha256",
                    )
                }
            )
        except KeyError as exc:
            failures.append(f"loaded closure entry {index} is missing {exc.args[0]}")
    if closure.get("record_count") != occurrence_total:
        failures.append("loaded closure record count is inconsistent")
    if closure.get("unique_record_count") != len(entries):
        failures.append("loaded closure unique-record count is inconsistent")
    if closure.get("strict_sha256") != _canonical_digest(strict_entries):
        failures.append("loaded closure strict digest is inconsistent")
    return failures


def validate_runtime_closure(closure: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if closure.get("schema") != RUNTIME_CLOSURE_SCHEMA:
        return ["OCaml runtime closure schema is incompatible"]
    entries = closure.get("entries")
    if not isinstance(entries, list) or not entries:
        return ["OCaml runtime closure entries are missing"]
    strict_entries: list[dict[str, Any]] = []
    roles: Counter[str] = Counter()
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            failures.append(f"OCaml runtime closure entry {index} is invalid")
            continue
        entry = raw_entry
        role = str(entry.get("role") or "")
        roles[role] += 1
        path = Path(str(entry.get("path") or ""))
        label = f"{role or 'unknown'} ({path})"
        if not path.is_absolute() or not path.is_file():
            failures.append(f"OCaml runtime input is missing: {label}")
        else:
            if sha256_file(path) != entry.get("sha256"):
                failures.append(f"OCaml runtime SHA-256 mismatch: {label}")
            try:
                if str(path.resolve(strict=True)) != entry.get("resolved_path"):
                    failures.append(f"OCaml runtime resolved path mismatch: {label}")
            except OSError as exc:
                failures.append(f"cannot resolve OCaml runtime input {label}: {exc}")
        try:
            strict_entries.append({key: entry[key] for key in ("role", "path", "resolved_path", "sha256")})
        except KeyError as exc:
            failures.append(f"OCaml runtime closure entry {index} is missing {exc.args[0]}")
    missing_roles = {"ocaml_hol", "ocamlrun"} - set(roles)
    if missing_roles:
        failures.append(f"OCaml runtime closure is missing roles: {', '.join(sorted(missing_roles))}")
    duplicate_roles = sorted(role for role, count in roles.items() if count != 1 and role)
    if duplicate_roles:
        failures.append(f"OCaml runtime closure has duplicate roles: {', '.join(duplicate_roles)}")
    unexpected_roles = set(roles) - {"ocaml_hol", "ocamlrun"}
    if unexpected_roles:
        failures.append(f"OCaml runtime closure has unexpected roles: {', '.join(sorted(unexpected_roles))}")
    if closure.get("strict_sha256") != _canonical_digest(strict_entries):
        failures.append("OCaml runtime closure strict digest is inconsistent")
    return failures
