"""Legacy final-evidence graph for immutable admission-to-credit attempts.

Ordinary warm authoring is authorized by the published broker/runtime shelf and
must not construct or require this source bundle.  This module remains for the
separate final-evidence and frozen-publication workflows that still consume its
schemas.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import secrets
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any

from hol_workbench.fork_broker_protocol import BROKER_RUNTIME_FILES

CONTROLLER_RUNTIME_BUNDLE_SCHEMA = "hol-workbench.controller-runtime-bundle.v4"
CONTROLLER_EXECUTION_IDENTITY_SCHEMA = "hol-workbench.controller-execution-identity.v4"
CONTROLLER_RUNTIME_AUTHORIZATION_SCHEMA = "hol-workbench.controller-runtime-authorization.v1"
CONTROLLER_RUNTIME_OBSERVATION_SCHEMA = "hol-workbench.controller-runtime-authoring-observation.v1"
CONTROLLER_RUNTIME_ATTEMPT_SCHEMA = "hol-workbench.controller-runtime-attempt.v1"
CONTROLLER_CLOSURE_POLICY = "hol-workbench.admission-to-credit-graph.v4"
CONTROLLER_EXECUTION_MODE = "isolated-read-only-materialized-python-graph.v4"
CONTROLLER_PROCESS_INVENTORY_POLICY = "hol-workbench.exact-process-callsite-inventory.v1"
CONTROLLER_AUTHORIZATION_FILENAME = "controller-runtime-authorization.json"
CONTROLLER_OBSERVATION_FILENAME = "controller-runtime-authoring-observation.json"
INDEPENDENTLY_REVIEWED_ATTEMPT = "independently_reviewed_attempt"
IMMUTABLE_AUTHORING_ATTEMPT = "immutable_authoring_attempt"
CONTROLLER_RUNTIME_ROOT_FILES = ("authoritative_runtime_worker.py",)
CONTROLLER_SEMANTIC_RESOURCES = (
    "warmup-profiles.json",
    "bin/phase-zero-runtime",
    "bin/workbench-python",
    "research/jane-street-probes/compact_after_preload.ml",
)
CONTROLLER_REPOSITORY_SEMANTIC_RESOURCES = frozenset({"research/jane-street-probes/compact_after_preload.ml"})
CONTROLLER_RUNTIME_OPENED_RESOURCES = tuple(f"hol_workbench/{name}" for name in BROKER_RUNTIME_FILES)
CONTROLLER_DECLARED_DYNAMIC_IMPORTS = frozenset(
    {
        ("authoritative_runtime_worker.py", "self_check", "importlib.import_module"),
    }
)
CONTROLLER_INDIRECT_PROCESS_ENTRYPOINTS = (
    {
        "kind": "indirect",
        "path": "hol_workbench/process_groups.py",
        "scope": "process_birth_identity",
        "operation": "injected subprocess.run",
    },
    {
        "kind": "indirect",
        "path": "hol_workbench/process_groups.py",
        "scope": "OwnedProcessSet.spawn",
        "operation": "injected subprocess.Popen",
    },
    {
        "kind": "indirect",
        "path": "hol_workbench/cli/filter_hol_output.py",
        "scope": "main",
        "operation": "OwnedProcessSet.spawn",
    },
)
_SUBPROCESS_LAUNCH_OPERATIONS = frozenset(
    {
        "Popen",
        "call",
        "check_call",
        "check_output",
        "getoutput",
        "getstatusoutput",
        "run",
    }
)
_SUBPROCESS_NON_LAUNCH_CONSTRUCTORS = frozenset(
    {
        "CalledProcessError",
        "CompletedProcess",
        "SubprocessError",
        "TimeoutExpired",
    }
)
_PROCESS_RUNTIME_MANIFEST: dict[str, Any] | None = None
_ACTIVE_RUNTIME_ATTEMPT: dict[str, Any] | None = None


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _package_root(package_root: Path | None = None) -> Path:
    return (package_root or Path(__file__).resolve().parent).expanduser().resolve()


def _workbench_root(package_root: Path | None = None) -> Path:
    return _package_root(package_root).parent


def default_controller_runtime_authorization_path(package_root: Path | None = None) -> Path:
    return _workbench_root(package_root) / CONTROLLER_AUTHORIZATION_FILENAME


def _regular_source(path: Path, *, root: Path) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(f"controller dependency is unreadable: {path}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"controller dependency is not an exact regular file: {path}")
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeError(f"controller dependency escapes the package root: {path}")
    return path.read_bytes()


def _semantic_resource_source(package_root: Path, relative: str) -> bytes:
    workbench = _workbench_root(package_root)
    packaged = workbench / relative
    if relative not in CONTROLLER_REPOSITORY_SEMANTIC_RESOURCES or packaged.exists() or packaged.is_symlink():
        return _regular_source(packaged, root=workbench)
    repository = workbench.parent
    return _regular_source(repository / relative, root=repository)


def _module_source(root: Path, module: str) -> Path | None:
    if module == "hol_workbench":
        path = root / "__init__.py"
        return path if path.is_file() else None
    if not module.startswith("hol_workbench."):
        return None
    relative = module.removeprefix("hol_workbench.").replace(".", "/")
    module_path = root / f"{relative}.py"
    package_path = root / relative / "__init__.py"
    if module_path.is_file():
        return module_path
    if package_path.is_file():
        return package_path
    return None


def _module_name(relative: str) -> str:
    path = Path(relative)
    if path.name == "__init__.py":
        suffix = ".".join(path.parent.parts)
    else:
        suffix = ".".join(path.with_suffix("").parts)
    return "hol_workbench" + (f".{suffix}" if suffix and suffix != "." else "")


def _relative_import_module(current: str, node: ast.ImportFrom, *, package_initializer: bool) -> str:
    if node.level == 0:
        return str(node.module or "")
    package_parts = current.split(".") if package_initializer else current.split(".")[:-1]
    ascend = node.level - 1
    if ascend >= len(package_parts):
        raise RuntimeError(f"controller relative import escapes its package: {current}")
    base = package_parts[: len(package_parts) - ascend]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _call_target(
    node: ast.Call,
    *,
    module_aliases: Mapping[str, str],
    member_aliases: Mapping[str, str],
) -> str | None:
    if isinstance(node.func, ast.Name):
        if node.func.id in {"__import__", "compile", "eval", "exec"}:
            return node.func.id
        return member_aliases.get(node.func.id)
    if not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name):
        return None
    owner = module_aliases.get(node.func.value.id)
    return f"{owner}.{node.func.attr}" if owner is not None else None


def _process_escape(
    node: ast.Call,
    *,
    module_aliases: Mapping[str, str],
    member_aliases: Mapping[str, str],
) -> tuple[str, str] | None:
    target = _call_target(node, module_aliases=module_aliases, member_aliases=member_aliases)
    if target in {"__import__", "compile", "eval", "exec"}:
        return "dynamic", str(target)
    if target is None or "." not in target:
        return None
    owner, operation = target.split(".", 1)
    if owner == "importlib":
        return "dynamic", target
    if owner == "subprocess":
        if operation in _SUBPROCESS_LAUNCH_OPERATIONS:
            return "process", target
        if operation in _SUBPROCESS_NON_LAUNCH_CONSTRUCTORS:
            return None
        return "unclassified_subprocess", target
    if owner == "os" and (
        operation in {"fork", "popen", "system"} or operation.startswith(("exec", "spawn", "posix_spawn"))
    ):
        return "process", target
    return None


def _enclosing_scope(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names)) or "<module>"


def _controller_source_closure(
    root: Path,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    pending = [*CONTROLLER_RUNTIME_ROOT_FILES, "__init__.py"]
    discovered: set[str] = set()
    external_modules: set[str] = set()
    late_project_imports: set[tuple[str, int, str]] = set()
    process_entrypoints: list[tuple[str, int, int, str, str]] = []
    while pending:
        relative = pending.pop()
        if relative in discovered:
            continue
        source_path = root / relative
        raw = _regular_source(source_path, root=root)
        try:
            tree = ast.parse(raw, filename=str(source_path))
        except (SyntaxError, ValueError) as exc:
            raise RuntimeError(f"controller dependency is not valid Python: {source_path}: {exc}") from exc
        discovered.add(relative)
        current_module = _module_name(relative)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        module_aliases: dict[str, str] = {}
        member_aliases: dict[str, str] = {}
        for imported_node in ast.walk(tree):
            if isinstance(imported_node, ast.Import):
                for alias in imported_node.names:
                    if alias.name in {"importlib", "os", "subprocess"}:
                        module_aliases[alias.asname or alias.name] = alias.name
            elif (
                isinstance(imported_node, ast.ImportFrom)
                and imported_node.level == 0
                and imported_node.module in {"importlib", "os", "subprocess"}
            ):
                for alias in imported_node.names:
                    member_aliases[alias.asname or alias.name] = f"{imported_node.module}.{alias.name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                escape := _process_escape(
                    node,
                    module_aliases=module_aliases,
                    member_aliases=member_aliases,
                )
            ):
                escape_kind, operation = escape
                scope = _enclosing_scope(node, parents)
                declaration = (relative, scope, operation)
                if escape_kind == "dynamic":
                    if declaration in CONTROLLER_DECLARED_DYNAMIC_IMPORTS:
                        continue
                    raise RuntimeError(
                        "controller dependency has an undeclared dynamic import or process entrypoint: "
                        f"{relative}:{node.lineno}: {scope}: {operation}"
                    )
                if escape_kind == "unclassified_subprocess":
                    raise RuntimeError(
                        "controller dependency has an unclassified subprocess callsite: "
                        f"{relative}:{node.lineno}:{node.col_offset + 1}: {scope}: {operation}"
                    )
                process_entrypoints.append((relative, node.lineno, node.col_offset + 1, scope, operation))
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            late_import = not isinstance(parents.get(node), ast.Module)
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            else:
                imported = _relative_import_module(
                    current_module,
                    node,
                    package_initializer=source_path.name == "__init__.py",
                )
                modules.append(imported)
                if imported == "hol_workbench" or imported.startswith("hol_workbench."):
                    for alias in node.names:
                        candidate = f"{imported}.{alias.name}"
                        if alias.name != "*" and _module_source(root, candidate) is not None:
                            modules.append(candidate)
            for module in modules:
                if module in {"", "__future__"}:
                    continue
                if module == "hol_workbench" or module.startswith("hol_workbench."):
                    dependency = _module_source(root, module)
                    if dependency is None:
                        if module == "hol_workbench":
                            continue
                        raise RuntimeError(
                            f"controller dependency import escapes the captured closure: {relative}:{node.lineno}: {module}"
                        )
                    dependency_relative = str(dependency.relative_to(root))
                    pending.append(dependency_relative)
                    if late_import:
                        late_project_imports.add((relative, node.lineno, module))
                    parent = dependency.parent
                    while parent != root:
                        init = parent / "__init__.py"
                        if init.is_file():
                            pending.append(str(init.relative_to(root)))
                        parent = parent.parent
                    continue
                top_level = module.split(".", 1)[0]
                if top_level not in sys.stdlib_module_names:
                    raise RuntimeError(
                        f"controller dependency imports non-stdlib code outside its closure: "
                        f"{relative}:{node.lineno}: {module}"
                    )
                external_modules.add(top_level)
    for root_file in CONTROLLER_RUNTIME_ROOT_FILES:
        _regular_source(root / root_file, root=root)
    captured = {f"hol_workbench/{relative}" for relative in discovered}
    indirect = tuple(entry for entry in CONTROLLER_INDIRECT_PROCESS_ENTRYPOINTS if str(entry["path"]) in captured)
    direct = tuple(
        {
            "kind": "direct",
            "path": f"hol_workbench/{relative}",
            "line": line,
            "column": column,
            "scope": scope,
            "operation": operation,
        }
        for relative, line, column, scope, operation in sorted(process_entrypoints)
    )
    late = tuple(
        {"path": f"hol_workbench/{relative}", "line": line, "module": module}
        for relative, line, module in sorted(late_project_imports)
    )
    return (
        tuple(sorted(discovered)),
        tuple(sorted(external_modules)),
        late,
        tuple(
            sorted(
                (*direct, *indirect),
                key=lambda row: (
                    str(row["path"]),
                    int(row.get("line") or 0),
                    int(row.get("column") or 0),
                    str(row["scope"]),
                    str(row["operation"]),
                ),
            )
        ),
    )


def controller_runtime_manifest(package_root: Path | None = None) -> dict[str, Any]:
    root = _package_root(package_root)
    runtime_files, external_modules, late_project_imports, subprocess_entrypoints = _controller_source_closure(root)
    digest = hashlib.sha256()
    files: list[dict[str, Any]] = []
    for relative in runtime_files:
        raw = _regular_source(root / relative, root=root)
        digest.update(relative.encode("utf-8") + b"\0" + raw + b"\0")
        files.append(
            {
                "path": f"hol_workbench/{relative}",
                "module": _module_name(relative),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
            }
        )
    runtime_paths = {f"hol_workbench/{relative}" for relative in runtime_files}
    opened_resources = tuple(
        relative for relative in CONTROLLER_RUNTIME_OPENED_RESOURCES if relative not in runtime_paths
    )
    resources: list[dict[str, Any]] = []
    for relative in (*CONTROLLER_SEMANTIC_RESOURCES, *opened_resources):
        raw = _semantic_resource_source(root, relative)
        digest.update(relative.encode("utf-8") + b"\0" + raw + b"\0")
        resources.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "executable": relative.startswith("bin/"),
            }
        )
    payload = {
        "schema": CONTROLLER_RUNTIME_BUNDLE_SCHEMA,
        "closure_policy": CONTROLLER_CLOSURE_POLICY,
        "execution_mode": CONTROLLER_EXECUTION_MODE,
        "root_files": list(CONTROLLER_RUNTIME_ROOT_FILES),
        "runtime_files": files,
        "semantic_resources": resources,
        "external_stdlib_modules": list(external_modules),
        "late_project_imports": list(late_project_imports),
        "process_inventory_policy": CONTROLLER_PROCESS_INVENTORY_POLICY,
        "subprocess_entrypoints": list(subprocess_entrypoints),
        "runtime_sha256": digest.hexdigest(),
    }
    return {**payload, "executable_identity_sha256": _canonical_sha256(payload)}


def controller_runtime_files(package_root: Path | None = None) -> tuple[str, ...]:
    manifest = controller_runtime_manifest(package_root)
    return tuple(str(row["path"]).removeprefix("hol_workbench/") for row in manifest["runtime_files"])


def controller_runtime_sha256(package_root: Path | None = None) -> str:
    return str(controller_runtime_manifest(package_root)["runtime_sha256"])


def controller_executable_identity_sha256(package_root: Path | None = None) -> str:
    return str(controller_runtime_manifest(package_root)["executable_identity_sha256"])


def bind_controller_process_runtime(package_root: Path | None = None) -> dict[str, Any]:
    """Bind the graph visible to this process without consulting import caches."""

    global _PROCESS_RUNTIME_MANIFEST
    root = _package_root(package_root)
    manifest = controller_runtime_manifest(root)
    if _PROCESS_RUNTIME_MANIFEST is not None and manifest != _PROCESS_RUNTIME_MANIFEST:
        raise RuntimeError("controller process runtime identity changed after eager binding")
    _PROCESS_RUNTIME_MANIFEST = manifest
    return dict(manifest)


def materialize_controller_runtime_bundle(
    destination: Path,
    *,
    package_root: Path | None = None,
) -> dict[str, Any]:
    source = _package_root(package_root)
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise RuntimeError(f"controller runtime bundle already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = controller_runtime_manifest(source)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        package = temporary / "hol_workbench"
        for row in manifest["runtime_files"]:
            relative = str(row["path"]).removeprefix("hol_workbench/")
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, target)
        for row in manifest["semantic_resources"]:
            relative = str(row["path"])
            target = temporary / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_semantic_resource_source(source, relative))
        payload = dict(manifest)
        payload["bundle_sha256"] = _canonical_sha256(manifest)
        (temporary / "controller-runtime-bundle.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        for path in temporary.rglob("*"):
            if path.is_file():
                path.chmod(0o555 if path.relative_to(temporary).as_posix().startswith("bin/") else 0o444)
        for path in sorted((item for item in temporary.rglob("*") if item.is_dir()), reverse=True):
            path.chmod(0o555)
        temporary.chmod(0o555)
        os.replace(temporary, destination)
    except BaseException:
        for path in sorted(temporary.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            with suppress(OSError):
                path.rmdir() if path.is_dir() else path.unlink()
        with suppress(OSError):
            temporary.rmdir()
        raise
    return verify_controller_runtime_bundle(destination)


def verify_controller_runtime_bundle(
    bundle: Path,
    *,
    expected_bundle_sha256: str | None = None,
) -> dict[str, Any]:
    bundle = bundle.expanduser().resolve()
    manifest_path = bundle / "controller-runtime-bundle.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"controller runtime bundle manifest is unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != CONTROLLER_RUNTIME_BUNDLE_SCHEMA:
        raise RuntimeError("controller runtime bundle schema is incompatible")
    recorded_bundle_sha256 = str(data.get("bundle_sha256") or "")
    payload = {key: value for key, value in data.items() if key != "bundle_sha256"}
    if recorded_bundle_sha256 != _canonical_sha256(payload):
        raise RuntimeError("controller runtime bundle identity is invalid")
    if expected_bundle_sha256 is not None and recorded_bundle_sha256 != expected_bundle_sha256:
        raise RuntimeError("controller runtime bundle differs from the start-bound identity")
    observed = controller_runtime_manifest(bundle / "hol_workbench")
    if observed != payload:
        raise RuntimeError("controller runtime bundle bytes or dependency closure changed after attempt start")
    for row in data.get("runtime_files") or []:
        path = bundle / str(row.get("path") or "")
        if path.stat().st_mode & 0o222:
            raise RuntimeError("controller runtime bundle is not read-only")
    for row in data.get("semantic_resources") or []:
        path = bundle / str(row.get("path") or "")
        expected_mode = 0o555 if row.get("executable") is True else 0o444
        if stat.S_IMODE(path.stat().st_mode) != expected_mode:
            raise RuntimeError("controller runtime semantic resource mode is not immutable")
    return data


def _make_explicit_test_bundle_cleanup_capable(bundle: Path) -> None:
    """Let a test sandbox delete its fixture without weakening real attempts."""

    for directory in (bundle, *(path for path in bundle.rglob("*") if path.is_dir())):
        directory.chmod(0o755)


def _read_controller_runtime_authorization(
    path: Path,
) -> tuple[dict[str, Any], bytes, str]:
    path = path.expanduser().resolve()
    try:
        raw = path.read_bytes()
        decoded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"controller runtime authorization is unreadable: {exc}") from exc
    if not isinstance(decoded, dict) or decoded.get("schema") != CONTROLLER_RUNTIME_AUTHORIZATION_SCHEMA:
        raise RuntimeError("controller runtime authorization schema is incompatible")
    identity = decoded.get("executable_identity_sha256")
    authorization_id = decoded.get("authorization_id")
    if (
        decoded.get("status") != "reviewed"
        or decoded.get("issued_separately") is not True
        or not isinstance(identity, str)
        or len(identity) != 64
        or not isinstance(authorization_id, str)
        or not authorization_id
    ):
        raise RuntimeError("controller runtime authorization is not an independently issued reviewed identity")
    return decoded, raw, hashlib.sha256(raw).hexdigest()


def _controller_authoring_observation(executable_identity_sha256: str) -> tuple[dict[str, Any], bytes, str]:
    """Record exact mutable-controller provenance without claiming independent review."""

    record: dict[str, Any] = {
        "schema": CONTROLLER_RUNTIME_OBSERVATION_SCHEMA,
        "status": "observed_for_warm_authoring",
        "issued_separately": False,
        "executable_identity_sha256": executable_identity_sha256,
        "evidence_boundary": "warm authoring only; not independent authorization or final theorem evidence",
    }
    raw = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return record, raw, hashlib.sha256(raw).hexdigest()


def _read_controller_authoring_observation(path: Path) -> tuple[dict[str, Any], bytes, str]:
    try:
        raw = path.expanduser().resolve().read_bytes()
        decoded = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"controller authoring observation is unreadable: {exc}") from exc
    identity = decoded.get("executable_identity_sha256") if isinstance(decoded, dict) else None
    if (
        not isinstance(decoded, dict)
        or decoded.get("schema") != CONTROLLER_RUNTIME_OBSERVATION_SCHEMA
        or decoded.get("status") != "observed_for_warm_authoring"
        or decoded.get("issued_separately") is not False
        or not isinstance(identity, str)
        or len(identity) != 64
    ):
        raise RuntimeError("controller authoring observation is invalid")
    return decoded, raw, hashlib.sha256(raw).hexdigest()


def reviewed_controller_runtime_authorization(
    authorization_path: Path | None = None,
    *,
    package_root: Path | None = None,
) -> dict[str, Any]:
    active = _ACTIVE_RUNTIME_ATTEMPT
    if active is not None and authorization_path is None:
        return {
            **active["authorization_record"],
            "authorization_mode": active.get("authorization_mode") or INDEPENDENTLY_REVIEWED_ATTEMPT,
            "authorization_sha256": active["authorization_sha256"],
            "authorization_path": active["authorization"],
        }
    path = authorization_path or default_controller_runtime_authorization_path(package_root)
    record, _raw, digest = _read_controller_runtime_authorization(path)
    return {**record, "authorization_sha256": digest, "authorization_path": str(path.resolve())}


def prepare_authorized_controller_runtime_attempt(
    destination: Path,
    *,
    authorization_path: Path | None = None,
    package_root: Path | None = None,
    allow_authoring_observed: bool = False,
) -> dict[str, Any]:
    """Freeze one graph, preserving independent review or an explicit authoring boundary."""

    source = _package_root(package_root)
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise RuntimeError(f"controller runtime attempt already exists: {destination}")
    authorization_source = authorization_path or default_controller_runtime_authorization_path(source)
    authorization, authorization_raw, authorization_sha256 = _read_controller_runtime_authorization(
        authorization_source
    )
    source_manifest = controller_runtime_manifest(source)
    authorization_mode = INDEPENDENTLY_REVIEWED_ATTEMPT
    authorization_filename = CONTROLLER_AUTHORIZATION_FILENAME
    if authorization["executable_identity_sha256"] != source_manifest["executable_identity_sha256"]:
        if not allow_authoring_observed:
            raise RuntimeError("current admission-to-credit graph is not the independently reviewed runtime identity")
        authorization, authorization_raw, authorization_sha256 = _controller_authoring_observation(
            str(source_manifest["executable_identity_sha256"])
        )
        authorization_mode = IMMUTABLE_AUTHORING_ATTEMPT
        authorization_filename = CONTROLLER_OBSERVATION_FILENAME
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        runtime = temporary / "runtime"
        bundle = materialize_controller_runtime_bundle(runtime, package_root=source)
        authorization_copy = temporary / authorization_filename
        authorization_copy.write_bytes(authorization_raw)
        payload = {
            "schema": CONTROLLER_RUNTIME_ATTEMPT_SCHEMA,
            "execution_mode": CONTROLLER_EXECUTION_MODE,
            "runtime_bundle": "runtime",
            "runtime_bundle_sha256": bundle["bundle_sha256"],
            "executable_identity_sha256": bundle["executable_identity_sha256"],
            "authorization": authorization_filename,
            "authorization_mode": authorization_mode,
            "authorization_sha256": authorization_sha256,
            "authorization_id": authorization.get("authorization_id"),
        }
        attempt = {**payload, "attempt_identity_sha256": _canonical_sha256(payload)}
        attempt_path = temporary / "controller-runtime-attempt.json"
        attempt_path.write_text(json.dumps(attempt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        authorization_copy.chmod(0o444)
        attempt_path.chmod(0o444)
        temporary.chmod(0o555)
        os.replace(temporary, destination)
    except BaseException:
        with suppress(OSError):
            temporary.chmod(0o755)
        for path in sorted(temporary.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            with suppress(OSError):
                if path.is_dir():
                    path.chmod(0o755)
                    path.rmdir()
                else:
                    path.unlink()
        with suppress(OSError):
            temporary.rmdir()
        raise
    return verify_controller_runtime_attempt(destination)


def verify_controller_runtime_attempt(
    attempt_root: Path,
    *,
    expected_attempt_identity_sha256: str | None = None,
    require_current_process: bool = False,
) -> dict[str, Any]:
    attempt_root = attempt_root.expanduser().resolve()
    attempt_path = attempt_root / "controller-runtime-attempt.json"
    try:
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"controller runtime attempt is unreadable: {exc}") from exc
    if not isinstance(attempt, dict) or attempt.get("schema") != CONTROLLER_RUNTIME_ATTEMPT_SCHEMA:
        raise RuntimeError("controller runtime attempt schema is incompatible")
    payload = {key: value for key, value in attempt.items() if key != "attempt_identity_sha256"}
    attempt_identity = str(attempt.get("attempt_identity_sha256") or "")
    if attempt_identity != _canonical_sha256(payload):
        raise RuntimeError("controller runtime attempt identity is invalid")
    if expected_attempt_identity_sha256 is not None and attempt_identity != expected_attempt_identity_sha256:
        raise RuntimeError("controller runtime attempt differs from the attempt-start identity")
    runtime = (attempt_root / str(attempt.get("runtime_bundle") or "")).resolve()
    authorization_path = (attempt_root / str(attempt.get("authorization") or "")).resolve()
    if not runtime.is_relative_to(attempt_root) or not authorization_path.is_relative_to(attempt_root):
        raise RuntimeError("controller runtime attempt path escapes its immutable materialization")
    bundle = verify_controller_runtime_bundle(
        runtime,
        expected_bundle_sha256=str(attempt.get("runtime_bundle_sha256") or ""),
    )
    authorization_mode = str(attempt.get("authorization_mode") or INDEPENDENTLY_REVIEWED_ATTEMPT)
    if authorization_mode == INDEPENDENTLY_REVIEWED_ATTEMPT:
        authorization, _raw, authorization_sha256 = _read_controller_runtime_authorization(authorization_path)
    elif authorization_mode == IMMUTABLE_AUTHORING_ATTEMPT:
        authorization, _raw, authorization_sha256 = _read_controller_authoring_observation(authorization_path)
    else:
        raise RuntimeError("controller runtime attempt has an unsupported authorization mode")
    if (
        authorization_sha256 != attempt.get("authorization_sha256")
        or authorization.get("authorization_id") != attempt.get("authorization_id")
        or authorization.get("executable_identity_sha256") != bundle.get("executable_identity_sha256")
        or attempt.get("executable_identity_sha256") != bundle.get("executable_identity_sha256")
    ):
        raise RuntimeError("controller runtime attempt is not bound to its independent authorization")
    if require_current_process:
        current_package = _package_root()
        expected_package = runtime / "hol_workbench"
        if current_package != expected_package:
            raise RuntimeError("controller runtime process did not start from the immutable materialization")
        captured_modules = {
            str(row["module"]): expected_package / str(row["path"]).removeprefix("hol_workbench/")
            for row in bundle["runtime_files"]
        }
        for module_name, module in tuple(sys.modules.items()):
            if module_name != "hol_workbench" and not module_name.startswith("hol_workbench."):
                continue
            module_file = getattr(module, "__file__", None)
            if not module_file:
                continue
            expected = captured_modules.get(module_name)
            if expected is None or Path(str(module_file)).resolve() != expected:
                raise RuntimeError(f"controller runtime loaded project code outside its immutable graph: {module_name}")
    return {
        **attempt,
        "authorization_mode": authorization_mode,
        "attempt_root": str(attempt_root),
        "runtime_bundle": str(runtime),
        "authorization": str(authorization_path),
        "authorization_record": authorization,
        "bundle": bundle,
    }


def activate_controller_runtime_attempt(
    attempt_root: Path,
    *,
    expected_attempt_identity_sha256: str,
) -> dict[str, Any]:
    global _ACTIVE_RUNTIME_ATTEMPT
    attempt = verify_controller_runtime_attempt(
        attempt_root,
        expected_attempt_identity_sha256=expected_attempt_identity_sha256,
        require_current_process=True,
    )
    if _ACTIVE_RUNTIME_ATTEMPT is not None and (
        _ACTIVE_RUNTIME_ATTEMPT.get("attempt_identity_sha256") != attempt.get("attempt_identity_sha256")
        or _ACTIVE_RUNTIME_ATTEMPT.get("authorization_sha256") != attempt.get("authorization_sha256")
    ):
        raise RuntimeError("controller process cannot switch runtime authorization between attempts")
    _ACTIVE_RUNTIME_ATTEMPT = attempt
    os.environ["HOL_WORKBENCH_CONTROLLER_ATTEMPT_ROOT"] = attempt["attempt_root"]
    os.environ["HOL_WORKBENCH_CONTROLLER_ATTEMPT_IDENTITY"] = attempt["attempt_identity_sha256"]
    os.environ["HOL_WORKBENCH_PYTHON"] = sys.executable
    return dict(attempt)


def active_controller_runtime_attempt(*, required: bool = True) -> dict[str, Any] | None:
    global _ACTIVE_RUNTIME_ATTEMPT
    if _ACTIVE_RUNTIME_ATTEMPT is None:
        root = os.environ.get("HOL_WORKBENCH_CONTROLLER_ATTEMPT_ROOT")
        identity = os.environ.get("HOL_WORKBENCH_CONTROLLER_ATTEMPT_IDENTITY")
        if root and identity:
            _ACTIVE_RUNTIME_ATTEMPT = verify_controller_runtime_attempt(
                Path(root),
                expected_attempt_identity_sha256=identity,
                require_current_process=True,
            )
    if _ACTIVE_RUNTIME_ATTEMPT is None:
        if required:
            raise RuntimeError("authoritative controller stage ran outside a reviewed runtime attempt")
        return None
    return dict(_ACTIVE_RUNTIME_ATTEMPT)


def verified_active_controller_runtime_attempt() -> dict[str, Any]:
    """Reverify the current immutable graph and its independent authorization."""

    active = active_controller_runtime_attempt(required=True)
    assert active is not None
    return verify_controller_runtime_attempt(
        Path(str(active["attempt_root"])),
        expected_attempt_identity_sha256=str(active["attempt_identity_sha256"]),
        require_current_process=True,
    )


def start_controller_execution_identity(
    transcript: Path,
    *,
    expected_executable_identity_sha256: str,
    allow_unreviewed_fixture: bool = False,
    authorized_attempt_root: Path | None = None,
) -> dict[str, Any]:
    attempt = (
        verify_controller_runtime_attempt(authorized_attempt_root)
        if authorized_attempt_root is not None
        else active_controller_runtime_attempt(required=False)
    )
    if attempt is not None:
        bundle_path = Path(str(attempt["runtime_bundle"]))
        bundle = dict(attempt["bundle"])
        authorization_mode = str(attempt.get("authorization_mode") or INDEPENDENTLY_REVIEWED_ATTEMPT)
        authorization_sha256 = attempt["authorization_sha256"]
        authorization_id = attempt["authorization_id"]
        controller_attempt_root = attempt["attempt_root"]
        controller_attempt_identity_sha256 = attempt["attempt_identity_sha256"]
    elif allow_unreviewed_fixture:
        if _PROCESS_RUNTIME_MANIFEST is None:
            raise RuntimeError("controller process runtime was not bound")
        current = controller_runtime_manifest()
        if current != _PROCESS_RUNTIME_MANIFEST:
            raise RuntimeError("controller source changed after process import; start a new proof command")
        transcript = transcript.expanduser().resolve()
        bundle_path = transcript.with_name(f"{transcript.stem}.controller-runtime-{secrets.token_hex(8)}")
        bundle = materialize_controller_runtime_bundle(bundle_path)
        _make_explicit_test_bundle_cleanup_capable(bundle_path)
        authorization_mode = "explicit_test_fixture"
        authorization_sha256 = None
        authorization_id = None
        controller_attempt_root = None
        controller_attempt_identity_sha256 = None
    else:
        raise RuntimeError("controller execution has no independently reviewed attempt-start authorization")
    if bundle.get("executable_identity_sha256") != expected_executable_identity_sha256:
        raise RuntimeError("controller executable identity is not the reviewed attempt-start identity")
    payload = {
        "schema": CONTROLLER_EXECUTION_IDENTITY_SCHEMA,
        "execution_mode": CONTROLLER_EXECUTION_MODE,
        "runtime_bundle": str(bundle_path),
        "runtime_bundle_sha256": bundle["bundle_sha256"],
        "executable_identity_sha256": bundle["executable_identity_sha256"],
        "runtime_sha256": bundle["runtime_sha256"],
        "runtime_files": list(bundle["runtime_files"]),
        "semantic_resources": list(bundle["semantic_resources"]),
        "closure_policy": CONTROLLER_CLOSURE_POLICY,
        "external_stdlib_modules": list(bundle["external_stdlib_modules"]),
        "late_project_imports": list(bundle["late_project_imports"]),
        "process_inventory_policy": bundle["process_inventory_policy"],
        "subprocess_entrypoints": list(bundle["subprocess_entrypoints"]),
        "authorization_mode": authorization_mode,
        "authorization_sha256": authorization_sha256,
        "authorization_id": authorization_id,
        "controller_attempt_root": controller_attempt_root,
        "controller_attempt_identity_sha256": controller_attempt_identity_sha256,
    }
    return {**payload, "execution_identity_sha256": _canonical_sha256(payload)}


def validate_controller_execution_identity(
    record: object,
    *,
    expected_executable_identity_sha256: str,
    allow_unreviewed_fixture: bool = False,
) -> dict[str, Any]:
    if not isinstance(record, Mapping) or record.get("schema") != CONTROLLER_EXECUTION_IDENTITY_SCHEMA:
        raise RuntimeError("controller execution identity schema is incompatible")
    data = dict(record)
    payload = {key: value for key, value in data.items() if key != "execution_identity_sha256"}
    if data.get("execution_identity_sha256") != _canonical_sha256(payload):
        raise RuntimeError("controller execution identity digest is invalid")
    if data.get("executable_identity_sha256") != expected_executable_identity_sha256:
        raise RuntimeError("controller execution identity was not authorized at attempt start")
    authorization_mode = data.get("authorization_mode")
    if authorization_mode in {INDEPENDENTLY_REVIEWED_ATTEMPT, IMMUTABLE_AUTHORING_ATTEMPT}:
        attempt = verify_controller_runtime_attempt(
            Path(str(data.get("controller_attempt_root") or "")),
            expected_attempt_identity_sha256=str(data.get("controller_attempt_identity_sha256") or ""),
        )
        if (
            data.get("authorization_sha256") != attempt.get("authorization_sha256")
            or data.get("authorization_id") != attempt.get("authorization_id")
            or data.get("runtime_bundle") != attempt.get("runtime_bundle")
            or authorization_mode != attempt.get("authorization_mode")
        ):
            raise RuntimeError("controller execution identity changed its attempt-start authorization")
    elif authorization_mode != "explicit_test_fixture" or not allow_unreviewed_fixture:
        raise RuntimeError("controller execution identity has no independent review authorization")
    bundle = verify_controller_runtime_bundle(
        Path(str(data.get("runtime_bundle") or "")),
        expected_bundle_sha256=str(data.get("runtime_bundle_sha256") or ""),
    )
    if (
        data.get("executable_identity_sha256") != bundle.get("executable_identity_sha256")
        or data.get("runtime_sha256") != bundle.get("runtime_sha256")
        or data.get("runtime_files") != bundle.get("runtime_files")
        or data.get("semantic_resources") != bundle.get("semantic_resources")
        or data.get("closure_policy") != bundle.get("closure_policy")
        or data.get("external_stdlib_modules") != bundle.get("external_stdlib_modules")
        or data.get("late_project_imports") != bundle.get("late_project_imports")
        or data.get("process_inventory_policy") != bundle.get("process_inventory_policy")
        or data.get("subprocess_entrypoints") != bundle.get("subprocess_entrypoints")
    ):
        raise RuntimeError("controller execution identity differs from its immutable bundle")
    return data


def controller_completion_diagnostics(
    identity: Mapping[str, Any],
    *,
    expected_executable_identity_sha256: str,
) -> dict[str, Any]:
    bundle_problem = None
    try:
        validate_controller_execution_identity(
            identity,
            expected_executable_identity_sha256=expected_executable_identity_sha256,
            allow_unreviewed_fixture=identity.get("authorization_mode") == "explicit_test_fixture",
        )
    except RuntimeError as exc:
        bundle_problem = str(exc)
    bound_runtime_sha256 = str(identity.get("runtime_sha256") or "") if bundle_problem is None else None
    return {
        "controller_runtime_sha256_at_completion": bound_runtime_sha256,
        "controller_runtime_unchanged_during_attempt": bundle_problem is None,
        "controller_checkout_drift_problem": None,
        "controller_runtime_bundle_validation_problem": bundle_problem,
        "controller_completion_identity_source": "immutable_start_bound_bundle",
    }
