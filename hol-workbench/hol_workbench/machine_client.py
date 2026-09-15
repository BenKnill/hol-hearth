"""Checkout-independent dispatch for one authoritative Workbench client."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hol_workbench.jsonio import (
    PathContainmentError,
    TreePublicationError,
    atomic_write_json,
    confined_child,
    publish_tree,
    read_json_strict,
    stage_tree,
)

AUTHORITY_SCHEMA = "hol-workbench.machine-client-authority.v1"
IDENTITY_SCHEMA = "hol-workbench.machine-client-identity.v1"
AUTHORITY_ENV = "HOL_WORKBENCH_CLIENT_AUTHORITY_JSON"
AUTHORITY_CONFIG_ENV = "HOL_WORKBENCH_CLIENT_AUTHORITY_CONFIG"
CHECKOUT_LAUNCHER_BIN_ENV = "HOL_WORKBENCH_CHECKOUT_LAUNCHER_BIN"
DEFAULT_CONFIG = Path.home() / ".config" / "hol-light-workbench" / "client-authority.json"
DEFAULT_PREFIX = Path.home() / ".local" / "share" / "hol-light-workbench"
DISPATCHER_NAME = "machine-client"
MACHINE_CLIENT_INSTALL_SCHEMA = "hol-workbench.machine-client-install.v1"
MACHINE_CLIENT_RELATIVE = Path("libexec") / "machine-client"
MACHINE_CLIENT_RESOURCE_PATHS = (
    "AGENTS.md",
    "README.md",
    "docs",
    "research/agent-trials",
    "research/diagnostics/algebra-aware-authoring-2026-07-20.md",
    "research/jane-street-probes/compact_after_preload.ml",
)
CONTROLLER_RUNTIME_BUNDLE_MANIFEST = "controller-runtime-bundle.json"
CONTROLLER_PROFILE_RESOURCE = "research/jane-street-probes/compact_after_preload.ml"
WARM_HOL_TOOL = "warm-hol"
WARM_HOL_ESCAPE_MODE = "warm_hol_escape"
WARM_HOL_INSTALL_SCHEMA = "hol-workbench.warm-hol-install.v1"
WARM_HOL_CLIENT_RELATIVE = Path("libexec") / "warm-hol-client"
DEVELOPMENT_CHECKOUT_MODE = "development_checkout"
DEVELOPMENT_EVIDENCE = "development_only_non_authoritative"
DEVELOPMENT_EVIDENCE_BOUNDARY = (
    "development-only execution of an exact committed dev client; not theorem, replay, "
    "cold-audit, final-proof, publication, or promotion evidence"
)
PORTABLE_BIN = "hol-workbench/bin"
PORTABLE_TOOL_REFERENCE = re.compile(r"(?<![A-Za-z0-9_./-])hol-workbench/bin/([A-Za-z0-9][A-Za-z0-9_-]*)")


class ClientAuthorityError(RuntimeError):
    """The configured machine client is absent, dirty, stale, or malformed."""


@dataclass(frozen=True)
class ClientAuthority:
    checkout: Path
    branch: str
    remote_ref: str
    remote_url: str
    config_path: Path


@dataclass(frozen=True)
class DispatchPlan:
    executable: Path
    argv: tuple[str, ...]
    environment: dict[str, str]
    identity: dict[str, Any]


def _git(checkout: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise ClientAuthorityError(f"client checkout is not usable by git: {detail}")
    return completed.stdout.strip()


def read_authority(path: Path = DEFAULT_CONFIG) -> ClientAuthority:
    config_path = path.expanduser().resolve()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ClientAuthorityError(
            f"machine Workbench authority is not installed: {config_path}; "
            "run the canonical checkout's hol-workbench/dev/install-machine-client"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ClientAuthorityError(f"machine Workbench authority is unreadable: {config_path}: {exc}") from exc
    if payload.get("schema") != AUTHORITY_SCHEMA:
        raise ClientAuthorityError(f"machine Workbench authority has an unsupported schema: {config_path}")
    checkout_value = str(payload.get("checkout") or "")
    if not checkout_value:
        raise ClientAuthorityError(f"machine Workbench authority has no checkout: {config_path}")
    return ClientAuthority(
        checkout=Path(checkout_value).expanduser().resolve(),
        branch=str(payload.get("branch") or "main"),
        remote_ref=str(payload.get("remote_ref") or "origin/main"),
        remote_url=str(payload.get("remote_url") or ""),
        config_path=config_path,
    )


def validate_authority(authority: ClientAuthority) -> dict[str, Any]:
    checkout = authority.checkout
    if not checkout.is_dir():
        raise ClientAuthorityError(f"authoritative Workbench checkout is missing: {checkout}")
    top_level = Path(_git(checkout, "rev-parse", "--show-toplevel")).resolve()
    if top_level != checkout:
        raise ClientAuthorityError(f"configured Workbench checkout resolves to {top_level}, expected {checkout}")
    branch = _git(checkout, "branch", "--show-current")
    if branch != authority.branch:
        raise ClientAuthorityError(
            f"client checkout is not authoritative: branch is {branch or '(detached)'}, expected {authority.branch}"
        )
    dirty = _git(checkout, "status", "--porcelain", "--untracked-files=all")
    if dirty:
        first = dirty.splitlines()[0]
        raise ClientAuthorityError(f"client checkout is not authoritative: {checkout} is dirty ({first})")
    revision = _git(checkout, "rev-parse", "HEAD")
    remote_revision = _git(checkout, "rev-parse", authority.remote_ref)
    if revision != remote_revision:
        raise ClientAuthorityError(
            f"client checkout is stale: HEAD {revision[:12]} does not match {authority.remote_ref} {remote_revision[:12]}"
        )
    remote_url = _git(checkout, "remote", "get-url", "origin")
    if authority.remote_url and remote_url != authority.remote_url:
        raise ClientAuthorityError(
            f"client checkout remote is {remote_url}, expected authoritative remote {authority.remote_url}"
        )
    return {
        "schema": IDENTITY_SCHEMA,
        "mode": "machine_dispatch",
        "authoritative": True,
        "evidence": "authoritative_machine_client",
        "checkout": str(checkout),
        "revision": revision,
        "branch": branch,
        "remote_ref": authority.remote_ref,
        "remote_revision": remote_revision,
        "remote_url": remote_url,
        "clean": True,
        "config": str(authority.config_path),
    }


def locked_checkout_python(checkout: Path) -> Path:
    """Return the authoritative checkout interpreter, or explain how to create it.

    Installed clients are deliberately small frozen launchers.  They must never
    turn a missing checkout venv into an ambient ``python3`` lookup: that makes
    a fresh host appear to work until a dependency happens to be absent.
    """

    candidate = checkout / ".venv" / "bin" / "python"
    guidance = f"run {checkout}/hol-workbench/dev/bootstrap-ubuntu"
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ClientAuthorityError(
            f"authoritative checkout Python is missing or not executable: {candidate}; {guidance}"
        )
    try:
        completed = subprocess.run(
            [str(candidate), "-I", "-B", "-c", "import sys; print(sys.version_info[:2])"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError as exc:
        raise ClientAuthorityError(
            f"authoritative checkout Python cannot start: {candidate}: {exc}; {guidance}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ClientAuthorityError(
            f"authoritative checkout Python did not start promptly: {candidate}; {guidance}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise ClientAuthorityError(f"authoritative checkout Python is unusable: {candidate}: {detail}; {guidance}")
    try:
        major, minor = (int(part) for part in completed.stdout.strip().strip("()").split(",")[:2])
    except ValueError as exc:
        raise ClientAuthorityError(
            f"authoritative checkout Python did not report a version: {candidate}; {guidance}"
        ) from exc
    if (major, minor) < (3, 11):
        raise ClientAuthorityError(
            f"authoritative checkout Python must be 3.11 or newer: {candidate} reports {major}.{minor}; {guidance}"
        )
    return candidate.resolve()


def dispatch_plan(
    tool: str,
    args: list[str],
    *,
    config_path: Path = DEFAULT_CONFIG,
    launcher: str = "",
    environment: dict[str, str] | None = None,
    frozen_client_root: Path | None = None,
) -> DispatchPlan:
    if not tool or Path(tool).name != tool or tool in {".", "..", DISPATCHER_NAME}:
        raise ClientAuthorityError(f"unsupported Workbench tool name: {tool!r}")
    authority = read_authority(config_path)
    identity = validate_authority(authority)
    checkout_python = locked_checkout_python(authority.checkout)
    identity["launcher"] = launcher
    if frozen_client_root is None:
        executable_root = authority.checkout / "hol-workbench"
    else:
        executable_root = frozen_client_root.expanduser().resolve()
        manifest = _validate_machine_client_candidate(executable_root)
        installed_revision = str(manifest.get("revision") or "")
        if installed_revision != identity["revision"]:
            raise ClientAuthorityError(
                "frozen machine client revision "
                f"{installed_revision[:12] or '(missing)'} does not match validated checkout "
                f"{identity['revision'][:12]}"
            )
        identity["installed_client"] = str(executable_root)
        identity["execution_revision"] = installed_revision
        identity["execution_frozen"] = True
    executable = executable_root / "bin" / tool
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ClientAuthorityError(f"authoritative Workbench tool is missing or not executable: {executable}")
    env = dict(os.environ if environment is None else environment)
    # The frozen dispatcher intentionally contains executable launchers but no
    # copied virtual environment.  Bind it to the validated checkout's locked
    # interpreter so installed front doors remain runnable after installation
    # and do not accidentally search an ambient Python or a frozen path.
    env["HOL_WORKBENCH_PYTHON"] = str(checkout_python)
    env[AUTHORITY_ENV] = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return DispatchPlan(
        executable=executable,
        argv=(str(executable), *args),
        environment=env,
        identity=identity,
    )


def client_identity(workspace_root: Path | None = None) -> dict[str, Any]:
    encoded = os.environ.get(AUTHORITY_ENV)
    if encoded:
        try:
            identity = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ClientAuthorityError(f"{AUTHORITY_ENV} is malformed: {exc}") from exc
        mode = identity.get("mode")
        if identity.get("schema") != IDENTITY_SCHEMA or mode not in {
            "machine_dispatch",
            WARM_HOL_ESCAPE_MODE,
            DEVELOPMENT_CHECKOUT_MODE,
        }:
            raise ClientAuthorityError(f"{AUTHORITY_ENV} does not contain a recognized client identity")
        if mode == WARM_HOL_ESCAPE_MODE and identity.get("evidence") != "warm_development_only":
            raise ClientAuthorityError(f"{AUTHORITY_ENV} contains an invalid warm-HOL escape identity")
        if mode == DEVELOPMENT_CHECKOUT_MODE and (
            identity.get("authoritative") is not False
            or identity.get("evidence") != DEVELOPMENT_EVIDENCE
            or identity.get("clean") is not True
            or identity.get("execution_frozen") is not True
        ):
            raise ClientAuthorityError(f"{AUTHORITY_ENV} contains an invalid development-checkout identity")
        return identity

    # A checkout-relative invocation is deliberately non-authoritative.  Do
    # not make the public proof loop depend on Git merely to decorate a
    # receipt; installed and explicit development clients carry a validated
    # identity in AUTHORITY_ENV instead.
    root = (workspace_root or Path(__file__).resolve().parents[2]).resolve()
    return {
        "schema": IDENTITY_SCHEMA,
        "mode": "checkout_relative",
        "authoritative": False,
        "checkout": str(root),
        "revision": "unknown",
        "branch": "unknown",
        "clean": False,
    }


def forwarded_authority_assignment() -> str | None:
    encoded = os.environ.get(AUTHORITY_ENV)
    return f"{AUTHORITY_ENV}={encoded}" if encoded else None


def machine_dispatch_active(*, environment: dict[str, str] | None = None) -> bool:
    """Return whether this process came through the installed machine dispatcher."""
    active_env = os.environ if environment is None else environment
    encoded = active_env.get(AUTHORITY_ENV)
    if not encoded:
        return False
    try:
        identity = json.loads(encoded)
    except json.JSONDecodeError:
        return False
    return identity.get("schema") == IDENTITY_SCHEMA and identity.get("mode") == "machine_dispatch"


def development_client_identity() -> dict[str, Any] | None:
    """Return the validated non-authoritative dev identity, when active."""

    identity = client_identity()
    return identity if identity.get("mode") == DEVELOPMENT_CHECKOUT_MODE else None


def preferred_tool_command(
    tool: str,
    *,
    platform: str | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    """Render a runnable command without bypassing installed machine authority.

    A public Linux checkout launcher records its validated checkout-local bin
    directory so live handoffs remain executable from any working directory.
    Pure library rendering remains portable. A process launched by the installed
    machine dispatcher carries its immutable launcher identity and therefore
    stays in that installed bin directory.

    ``platform`` remains injectable for compatibility with callers and tests;
    dispatch identity, not the host operating system, decides authority.
    """
    if not tool or Path(tool).name != tool:
        raise ValueError(f"unsafe Workbench tool name: {tool!r}")
    selected_platform = sys.platform if platform is None else platform
    fallback = f"{PORTABLE_BIN}/{tool}"
    active_env = os.environ if environment is None else environment
    encoded = active_env.get(AUTHORITY_ENV)
    if encoded:
        try:
            identity = json.loads(encoded)
        except json.JSONDecodeError:
            identity = {}
        mode = identity.get("mode")
        if identity.get("schema") == IDENTITY_SCHEMA and mode in {"machine_dispatch", DEVELOPMENT_CHECKOUT_MODE}:
            launcher_value = str(identity.get("launcher") or "")
            if launcher_value:
                launcher = Path(launcher_value).expanduser()
                return str(launcher if launcher.name == tool else launcher.parent / tool)
    checkout_bin_value = active_env.get(CHECKOUT_LAUNCHER_BIN_ENV)
    if selected_platform.startswith("linux") and checkout_bin_value:
        expected_bin = (Path(__file__).resolve().parents[1] / "bin").resolve()
        checkout_bin = Path(checkout_bin_value).expanduser().resolve()
        candidate = checkout_bin / tool
        if checkout_bin == expected_bin and candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return fallback


def render_preferred_tool_references(
    text: str,
    *,
    platform: str | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    """Render executable handoffs through the active machine front door.

    Stored artifacts and portable checkout documentation may retain
    ``hol-workbench/bin/TOOL``. Live text emitted by an installed machine
    command must not tell the next agent to bypass that installed authority.
    """

    return PORTABLE_TOOL_REFERENCE.sub(
        lambda match: preferred_tool_command(
            match.group(1),
            platform=platform,
            environment=environment,
        ),
        text,
    )


def install(
    *,
    checkout: Path,
    prefix: Path = DEFAULT_PREFIX,
    config_path: Path = DEFAULT_CONFIG,
    branch: str = "main",
    remote_ref: str = "origin/main",
) -> None:
    checkout = checkout.expanduser().resolve()
    remote_url = _git(checkout, "remote", "get-url", "origin")
    config_path = config_path.expanduser().resolve()
    authority = ClientAuthority(
        checkout=checkout,
        branch=branch,
        remote_ref=remote_ref,
        remote_url=remote_url,
        config_path=config_path,
    )
    identity = validate_authority(authority)
    # Validate before writing any install artifact or authority config.  A
    # front door is only publishable when its pinned interpreter is usable.
    locked_checkout_python(checkout)
    canonical_bin = checkout / "hol-workbench" / "bin"
    warm_hol = canonical_bin / WARM_HOL_TOOL
    if not warm_hol.is_file() or not os.access(warm_hol, os.X_OK):
        raise ClientAuthorityError(f"authoritative warm-HOL escape launcher is missing or not executable: {warm_hol}")
    tools = sorted(
        path.name
        for path in canonical_bin.iterdir()
        if path.name not in {DISPATCHER_NAME, WARM_HOL_TOOL} and path.is_file() and os.access(path, os.X_OK)
    )
    if not tools:
        raise ClientAuthorityError(
            f"authoritative Workbench bin directory contains no executable tools: {canonical_bin}"
        )
    prefix_root = prefix.expanduser()
    prefix_root.mkdir(parents=True, exist_ok=True)
    try:
        bin_dir = confined_child(prefix_root, "bin")
    except PathContainmentError as exc:
        raise ClientAuthorityError(str(exc)) from exc
    if bin_dir.exists() or bin_dir.is_symlink():
        if not bin_dir.is_dir():
            raise ClientAuthorityError(f"machine Workbench bin path is not a directory: {bin_dir}")
        for existing in bin_dir.iterdir():
            if not existing.is_symlink():
                raise ClientAuthorityError(f"machine Workbench install will not overwrite non-symlink path: {existing}")
    installed_dispatcher = _install_machine_client(
        checkout=checkout,
        prefix=prefix,
        identity=identity,
    )
    installed_warm_hol = _install_warm_hol_client(
        checkout=checkout,
        prefix=prefix,
        identity=identity,
    )
    links = dict.fromkeys(tools, installed_dispatcher)
    links[WARM_HOL_TOOL] = installed_warm_hol
    try:
        with stage_tree(prefix_root, "bin") as staged:
            for name, target in links.items():
                (staged.path / name).symlink_to(target)
            publish_tree(
                staged,
                validate=lambda candidate: _validate_front_door_candidate(candidate, links),
                replace=bin_dir.exists(),
            )
    except (OSError, PathContainmentError, TreePublicationError) as exc:
        raise ClientAuthorityError(f"cannot publish machine Workbench front doors: {exc}") from exc
    atomic_write_json(
        config_path,
        {
            "schema": AUTHORITY_SCHEMA,
            "checkout": str(checkout),
            "branch": branch,
            "remote_ref": remote_ref,
            "remote_url": remote_url,
        },
    )


def _validate_front_door_candidate(candidate: Path, links: dict[str, Path]) -> None:
    if {path.name for path in candidate.iterdir()} != set(links):
        raise ClientAuthorityError("staged machine Workbench front doors are incomplete")
    for name, target in links.items():
        link = candidate / name
        if not link.is_symlink() or link.resolve() != target.resolve():
            raise ClientAuthorityError(f"staged machine Workbench front door is invalid: {link}")


def installed_warm_hol_path(prefix: Path = DEFAULT_PREFIX) -> Path:
    return prefix.expanduser().resolve() / WARM_HOL_CLIENT_RELATIVE / "bin" / WARM_HOL_TOOL


def installed_machine_client_path(prefix: Path = DEFAULT_PREFIX) -> Path:
    return prefix.expanduser().resolve() / MACHINE_CLIENT_RELATIVE / "bin" / DISPATCHER_NAME


def _controller_profile_resource_sha256(root: Path) -> str | None:
    """Validate a legacy frozen-controller publication resource when present."""

    marker = root / CONTROLLER_RUNTIME_BUNDLE_MANIFEST
    if not marker.exists() and not marker.is_symlink():
        return None
    resource = root / CONTROLLER_PROFILE_RESOURCE
    if marker.is_symlink() or not marker.is_file():
        raise ClientAuthorityError("frozen controller bundle marker is not a regular file")
    if resource.is_symlink() or not resource.is_file():
        raise ClientAuthorityError("frozen controller bundle lacks its profile publication resource")
    try:
        from hol_workbench.controller_runtime_bundle import verified_active_controller_runtime_attempt

        attempt = verified_active_controller_runtime_attempt()
    except (ImportError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ClientAuthorityError(f"frozen controller runtime attempt is invalid: {exc}") from exc
    if Path(str(attempt.get("runtime_bundle") or "")).resolve() != root:
        raise ClientAuthorityError("active controller runtime differs from the resource bundle")
    bundle = attempt.get("bundle")
    if not isinstance(bundle, dict):
        raise ClientAuthorityError("active controller runtime has no verified bundle")
    resource_rows = bundle.get("semantic_resources")
    resource_record = (
        next(
            (row for row in resource_rows if isinstance(row, dict) and row.get("path") == CONTROLLER_PROFILE_RESOURCE),
            None,
        )
        if isinstance(resource_rows, list)
        else None
    )
    resource_sha256 = hashlib.sha256(resource.read_bytes()).hexdigest()
    if (
        bundle.get("schema") != "hol-workbench.controller-runtime-bundle.v4"
        or not isinstance(resource_record, dict)
        or resource_record.get("sha256") != resource_sha256
        or resource_record.get("size_bytes") != resource.stat().st_size
        or resource_record.get("executable") is not False
    ):
        raise ClientAuthorityError("frozen controller profile publication resource is not bundle-bound")
    return resource_sha256


def _machine_dispatch_resource_root(*, bundled_resource_sha256: str) -> Path:
    identity = client_identity()
    if (
        identity.get("authoritative") is not True
        or identity.get("evidence") != "authoritative_machine_client"
        or identity.get("execution_frozen") is not True
    ):
        raise ClientAuthorityError("machine dispatch resource identity is not authoritative and frozen")
    installed_value = identity.get("installed_client")
    if not isinstance(installed_value, str) or not installed_value:
        raise ClientAuthorityError("machine dispatch resource identity has no installed client")
    installed_path = Path(installed_value).expanduser()
    if not installed_path.is_absolute():
        raise ClientAuthorityError("machine dispatch resource root is not absolute")
    installed = installed_path.resolve()
    if installed_path != installed:
        raise ClientAuthorityError("machine dispatch resource root is not canonical")
    install_marker = installed / "machine-client-install.json"
    if install_marker.is_symlink() or not install_marker.is_file():
        raise ClientAuthorityError("frozen machine client install marker is not a regular file")
    manifest = _validate_machine_client_candidate(installed)
    manifest_root_value = manifest.get("installed_client")
    if not isinstance(manifest_root_value, str) or not Path(manifest_root_value).is_absolute():
        raise ClientAuthorityError("frozen machine client install manifest has no absolute resource root")
    if Path(manifest_root_value).expanduser().resolve() != installed:
        raise ClientAuthorityError("frozen machine client resource root differs from its install manifest")
    execution_revision = str(identity.get("execution_revision") or "")
    if not execution_revision or manifest.get("revision") != execution_revision:
        raise ClientAuthorityError("frozen machine client resource revision differs from dispatch identity")
    installed_resource = installed / CONTROLLER_PROFILE_RESOURCE
    if installed_resource.is_symlink() or not installed_resource.is_file():
        raise ClientAuthorityError("frozen machine client lacks its profile publication resource")
    if hashlib.sha256(installed_resource.read_bytes()).hexdigest() != bundled_resource_sha256:
        raise ClientAuthorityError("frozen machine and controller profile publication resources differ")
    return installed


def repository_resource_root(workbench_root: Path | None = None) -> Path:
    """Locate repository-level resources in a checkout or frozen client.

    The reviewed controller runs from a relocatable immutable materialization.
    Its bundled semantic resource authenticates the bytes; an authoritative
    installed dispatch uses the validated machine-client identity only to
    locate the shelf-recorded copy of those same bytes.
    """
    root = (workbench_root or Path(__file__).resolve().parents[1]).resolve()
    if (root / "machine-client-install.json").is_file():
        return root
    bundled_resource_sha256 = _controller_profile_resource_sha256(root)
    if bundled_resource_sha256 is not None:
        if machine_dispatch_active():
            return _machine_dispatch_resource_root(bundled_resource_sha256=bundled_resource_sha256)
        return root
    return root.parent


def _extract_git_archive(checkout: Path, revision: str, destination: Path, *paths: str) -> None:
    treeish = revision if paths else f"{revision}:hol-workbench"
    command = ["git", "-C", str(checkout), "archive", "--format=tar", treeish]
    if paths:
        command.extend(["--", *paths])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip() or f"exit {completed.returncode}"
        raise ClientAuthorityError(f"cannot freeze Workbench client at {revision[:12]}: {detail}")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
        members = archive.getmembers()
        root = destination.resolve()
        for member in members:
            member_path = (destination / member.name).resolve()
            if not member_path.is_relative_to(root) or member.issym() or member.islnk() or member.isdev():
                raise ClientAuthorityError(f"unsafe path in frozen Workbench client archive: {member.name}")
        archive.extractall(destination, members=members)


def _extract_frozen_client(
    checkout: Path,
    revision: str,
    destination: Path,
    *,
    include_machine_resources: bool = False,
) -> None:
    _extract_git_archive(checkout, revision, destination)
    if include_machine_resources:
        _extract_git_archive(checkout, revision, destination, *MACHINE_CLIENT_RESOURCE_PATHS)


def materialize_frozen_client(checkout: Path, revision: str, destination: Path) -> dict[str, dict[str, Any]]:
    """Extract and inventory one exact committed Workbench client."""

    _extract_frozen_client(checkout.expanduser().resolve(), revision, destination.expanduser().resolve())
    return _frozen_file_inventory(destination.expanduser().resolve())


def _frozen_file_inventory(root: Path) -> dict[str, dict[str, Any]]:
    return {
        path.relative_to(root).as_posix(): {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": path.stat().st_mode & 0o777,
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _install_machine_client(*, checkout: Path, prefix: Path, identity: dict[str, Any]) -> Path:
    prefix = prefix.expanduser().resolve()
    target = prefix / MACHINE_CLIENT_RELATIVE
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        with stage_tree(parent, target.name) as staged:
            revision = str(identity["revision"])
            _extract_frozen_client(checkout, revision, staged.path, include_machine_resources=True)
            atomic_write_json(
                staged.path / "machine-client-install.json",
                {
                    "schema": MACHINE_CLIENT_INSTALL_SCHEMA,
                    "installed_client": str(target),
                    "source_checkout": str(checkout),
                    "revision": revision,
                    "branch": identity["branch"],
                    "remote_ref": identity["remote_ref"],
                    "remote_url": identity["remote_url"],
                    "files": _frozen_file_inventory(staged.path),
                },
            )
            publish_tree(
                staged,
                validate=_validate_machine_client_publication_candidate,
                replace=target.exists(),
            )
        return target / "bin" / DISPATCHER_NAME
    except (OSError, PathContainmentError, TreePublicationError) as exc:
        raise ClientAuthorityError(f"cannot publish frozen machine client: {exc}") from exc


def _install_warm_hol_client(*, checkout: Path, prefix: Path, identity: dict[str, Any]) -> Path:
    prefix = prefix.expanduser().resolve()
    target = prefix / WARM_HOL_CLIENT_RELATIVE
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        with stage_tree(parent, target.name) as staged:
            revision = str(identity["revision"])
            _extract_frozen_client(checkout, revision, staged.path)
            atomic_write_json(
                staged.path / "warm-hol-install.json",
                {
                    "schema": WARM_HOL_INSTALL_SCHEMA,
                    "development_only": True,
                    "installed_client": str(target),
                    "source_checkout": str(checkout),
                    "revision": revision,
                    "branch": identity["branch"],
                    "remote_ref": identity["remote_ref"],
                    "remote_url": identity["remote_url"],
                    "files": _frozen_file_inventory(staged.path),
                },
            )
            publish_tree(staged, validate=_validate_warm_hol_candidate, replace=target.exists())
        return target / "bin" / WARM_HOL_TOOL
    except (OSError, PathContainmentError, TreePublicationError) as exc:
        raise ClientAuthorityError(f"cannot publish frozen warm-HOL client: {exc}") from exc


def _validate_warm_hol_candidate(candidate: Path) -> None:
    staged_launcher = candidate / "bin" / WARM_HOL_TOOL
    if not staged_launcher.is_file() or not os.access(staged_launcher, os.X_OK):
        raise ClientAuthorityError(f"frozen warm-HOL launcher is missing or not executable: {staged_launcher}")
    _validate_frozen_client_candidate(
        candidate,
        manifest_name="warm-hol-install.json",
        schema=WARM_HOL_INSTALL_SCHEMA,
    )


def _validate_machine_client_candidate(candidate: Path) -> dict[str, Any]:
    staged_launcher = candidate / "bin" / DISPATCHER_NAME
    if not staged_launcher.is_file() or not os.access(staged_launcher, os.X_OK):
        raise ClientAuthorityError(f"frozen machine-client launcher is missing or not executable: {staged_launcher}")
    return _validate_frozen_client_candidate(
        candidate,
        manifest_name="machine-client-install.json",
        schema=MACHINE_CLIENT_INSTALL_SCHEMA,
    )


def _validate_machine_client_publication_candidate(candidate: Path) -> None:
    """Adapt manifest-returning validation to the publication callback contract."""

    _validate_machine_client_candidate(candidate)


def _validate_frozen_client_candidate(
    candidate: Path,
    *,
    manifest_name: str,
    schema: str,
) -> dict[str, Any]:
    manifest = candidate / manifest_name
    if not manifest.is_file():
        raise ClientAuthorityError(f"frozen client install manifest is missing: {manifest}")
    try:
        document = read_json_strict(manifest)
    except (OSError, TypeError, ValueError) as exc:
        raise ClientAuthorityError(f"frozen client install manifest is invalid: {manifest}: {exc}") from exc
    if document.get("schema") != schema or not isinstance(document.get("files"), dict):
        raise ClientAuthorityError(f"frozen client install manifest has an unsupported shape: {manifest}")
    expected_files = document["files"]
    observed_files = {
        path.relative_to(candidate).as_posix() for path in candidate.rglob("*") if path.is_file() and path != manifest
    }
    if observed_files != set(expected_files):
        raise ClientAuthorityError(f"frozen client install manifest does not cover the candidate tree: {manifest}")
    for relative, record in expected_files.items():
        path = candidate / relative
        if (
            path.is_symlink()
            or not path.is_file()
            or not isinstance(record, dict)
            or hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256")
            or path.stat().st_mode & 0o777 != record.get("mode")
        ):
            raise ClientAuthorityError(f"frozen client candidate file failed validation: {relative}")
    return document


def _tool_and_args(argv: list[str], invoked_as: str) -> tuple[str, list[str]]:
    basename = Path(invoked_as).name
    if basename != DISPATCHER_NAME:
        return basename, argv
    if not argv:
        raise ClientAuthorityError("usage: machine-client TOOL ...")
    return argv[0], argv[1:]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    invoked_as = os.environ.get("HOL_WORKBENCH_MACHINE_INVOKED_AS", sys.argv[0])
    try:
        tool, tool_args = _tool_and_args(args, invoked_as)
        config_path = Path(os.environ.get(AUTHORITY_CONFIG_ENV, str(DEFAULT_CONFIG)))
        plan = dispatch_plan(
            tool,
            tool_args,
            config_path=config_path,
            launcher=str(Path(invoked_as).expanduser().absolute()),
            frozen_client_root=Path(__file__).resolve().parents[1],
        )
    except ClientAuthorityError as exc:
        print(f"machine Workbench refused: {exc}", file=sys.stderr)
        return 78
    os.execve(plan.executable, list(plan.argv), plan.environment)
    return 70


def install_main(argv: list[str] | None = None) -> int:
    if sys.platform != "linux":
        print(
            "machine Workbench install refused: Linux-only runtime; no macOS client was installed",
            file=sys.stderr,
        )
        return 2
    parser = argparse.ArgumentParser(description="Install one machine-authoritative Workbench front door.")
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, default=DEFAULT_PREFIX)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parsed = parser.parse_args(argv)
    try:
        install(checkout=parsed.checkout, prefix=parsed.prefix, config_path=parsed.config)
    except ClientAuthorityError as exc:
        print(f"machine Workbench install refused: {exc}", file=sys.stderr)
        return 78
    print(f"machine Workbench authority: {parsed.config.expanduser().resolve()}")
    print(f"machine Workbench front doors: {parsed.prefix.expanduser().resolve() / 'bin'}")
    return 0


if __name__ == "__main__":
    module_args = sys.argv[1:]
    if module_args and module_args[0] == "--install":
        raise SystemExit(install_main(module_args[1:]))
    raise SystemExit(main(module_args))
