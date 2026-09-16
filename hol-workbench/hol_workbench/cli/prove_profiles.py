#!/usr/bin/env python3
"""Warm-profile helpers for the legacy ``bin/prove`` wrapper."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from hol_workbench.cli.public_commands import public_command
from hol_workbench.hashing import sha256_text
from hol_workbench.logical_source_roots import (
    LogicalSourceRootError,
    logical_source_root_declarations,
)
from hol_workbench.machine_client import (
    WARM_HOL_ESCAPE_MODE,
    client_identity,
    machine_dispatch_active,
    preferred_tool_command,
    repository_resource_root,
)
from hol_workbench.profile_project_context import resolve_profile_project_context
from hol_workbench.profile_registry import (
    load_developer_profile_manifest,
    load_public_profile_manifest,
    public_profile_document,
    public_profile_names,
    public_profile_records,
)
from hol_workbench.project_warm_profile import ProjectWarmProfileError, load_project_warm_profile
from hol_workbench.restored_execution_topology import RestoredExecutionTopology
from hol_workbench.runtime_cache import workbench_cache_root
from hol_workbench.ubuntu_runtime_layout import resolve_criu_shelf_root

VALID_CWD_POLICIES = {"strict", "source_dir", "profile_cwd", "global_hol_lib"}


def _legacy_holdir_roots(profile: dict[str, Any], *, name: str) -> tuple[str, ...]:
    """Validate profile-owned historical HOL roots without resolving host paths."""
    raw_roots = profile.get("legacy_holdir_roots", [])
    if not isinstance(raw_roots, list) or not all(isinstance(value, str) for value in raw_roots):
        print(f"prove: warmup profile {name!r} legacy_holdir_roots must be a list of absolute paths", file=sys.stderr)
        raise SystemExit(2)
    roots: list[str] = []
    for value in raw_roots:
        root = Path(value).expanduser()
        if not root.is_absolute() or not root.parts or any(part in {"", ".", ".."} for part in root.parts):
            print(f"prove: warmup profile {name!r} has unsafe legacy HOLDIR root {value!r}", file=sys.stderr)
            raise SystemExit(2)
        lexical = os.path.abspath(root)
        if lexical not in roots:
            roots.append(lexical)
    return tuple(roots)


def workbench_dir_from_script_dir(script_dir: str | Path) -> Path:
    return Path(script_dir).expanduser().resolve().parent


def project_manifest_root(workbench_dir: Path) -> Path:
    """Resolve developer project-profile paths outside a frozen warm client."""

    identity = client_identity()
    if identity.get("mode") == WARM_HOL_ESCAPE_MODE:
        checkout = Path(str(identity.get("checkout") or "")).expanduser()
        candidate = checkout / "hol-workbench"
        if candidate.is_dir():
            return candidate.resolve()
    return workbench_dir


def load_manifest(script_dir: str | Path) -> dict[str, Any]:
    workbench_dir = workbench_dir_from_script_dir(script_dir)
    manifest = workbench_dir / "warmup-profiles.json"
    try:
        return load_public_profile_manifest(workbench_dir)
    except (OSError, ValueError) as exc:
        print(f"prove: cannot load public warmup profile manifest {manifest}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def load_developer_manifest(script_dir: str | Path) -> dict[str, Any]:
    workbench_dir = workbench_dir_from_script_dir(script_dir)
    manifest = workbench_dir / "dev" / "warmup-profiles-developer.json"
    try:
        return load_developer_profile_manifest(workbench_dir)
    except (OSError, ValueError) as exc:
        print(f"prove: cannot load developer warmup profile manifest {manifest}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _profiles(data: dict[str, Any]) -> dict[str, Any]:
    profiles = data.get("profiles") or {}
    return profiles if isinstance(profiles, dict) else {}


def public_authoring_profile_names(script_dir: str | Path) -> tuple[str, ...]:
    """Return the manifest-declared profiles exposed to proof authors."""
    return public_profile_names(load_manifest(script_dir))


def _public_profiles(data: dict[str, Any]) -> dict[str, Any]:
    return public_profile_records(data)


def _public_profile(data: dict[str, Any], name: str) -> dict[str, Any]:
    profiles = _public_profiles(data)
    profile = profiles.get(name)
    if not isinstance(profile, dict):
        available = ", ".join(profiles) or "(none)"
        print(
            f"prove: profile {name!r} is not published for Linux warm authoring; available: {available}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return profile


def _profile(data: dict[str, Any], name: str) -> dict[str, Any]:
    profiles = _profiles(data)
    profile = profiles.get(name)
    if not isinstance(profile, dict):
        available = ", ".join(sorted(profiles)) or "(none)"
        print(f"prove: unknown warmup profile {name!r}; available: {available}", file=sys.stderr)
        raise SystemExit(2)
    return profile


def _useful_operation_strings(profile: dict[str, Any], profile_name: str, *, strict: bool) -> list[str]:
    useful_operations = profile.get("useful_operations") or []
    if not isinstance(useful_operations, list):
        if strict:
            print(
                f"prove: warmup profile {profile_name!r} has invalid useful_operations; expected a list",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return []
    if strict and len(useful_operations) > 10:
        print(
            f"prove: warmup profile {profile_name!r} has {len(useful_operations)} useful_operations; limit is 10",
            file=sys.stderr,
        )
        raise SystemExit(2)
    operation_items: list[str] = []
    for item in useful_operations[:10]:
        if isinstance(item, str):
            op_name = item.strip()
            op_hint = ""
        elif isinstance(item, dict):
            op_name = str(item.get("name") or "").strip()
            op_hint = str(item.get("hint") or "").strip()
        else:
            if strict:
                print(f"prove: warmup profile {profile_name!r} has invalid useful operation entry", file=sys.stderr)
                raise SystemExit(2)
            continue
        if not op_name:
            if strict:
                print(f"prove: warmup profile {profile_name!r} has useful operation without a name", file=sys.stderr)
                raise SystemExit(2)
            continue
        operation_items.append(f"{op_name}: {op_hint}" if op_hint else op_name)
    return operation_items


def profile_operation_smoke_lines(
    script_dir: str | Path,
    name: str,
    *,
    include_developer_profiles: bool = False,
) -> list[str]:
    """Return executable checks attached to a profile's advertised operations."""
    loader = load_developer_manifest if include_developer_profiles else load_manifest
    profile = _profile(loader(script_dir), name)
    operations = profile.get("useful_operations") or []
    if not isinstance(operations, list):
        raise ValueError(f"profile {name!r} has invalid useful_operations; expected a list")

    lines: list[str] = []
    for item in operations:
        if not isinstance(item, dict) or "smoke" not in item:
            continue
        smoke = item["smoke"]
        if not isinstance(smoke, str) or not smoke.strip().endswith(";;"):
            operation = str(item.get("name") or "(unnamed)")
            raise ValueError(f"profile {name!r} operation {operation!r} has invalid smoke expression")
        lines.append(smoke.strip())
    return lines


def developer_profile_operation_smoke_lines(script_dir: str | Path, name: str) -> list[str]:
    return profile_operation_smoke_lines(script_dir, name, include_developer_profiles=True)


def developer_profile_post_restore_sentinels(script_dir: str | Path, name: str) -> list[dict[str, str]]:
    """Return the developer-only named capability probes for shelf publication."""

    profile = _profile(load_developer_manifest(script_dir), name)
    raw = profile.get("post_restore_sentinels") or []
    if not isinstance(raw, list):
        raise ValueError(f"profile {name!r} has invalid post_restore_sentinels; expected a list")
    sentinels: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"profile {name!r} has invalid post-restore sentinel entry")
        fields = {key: str(item.get(key) or "").strip() for key in ("capability", "binding", "smoke", "marker")}
        if not all(fields.values()) or not fields["smoke"].endswith(";;"):
            raise ValueError(f"profile {name!r} has incomplete post-restore sentinel entry")
        if not fields["marker"].startswith("__HOL_WORKBENCH_POST_RESTORE_SENTINEL__:"):
            raise ValueError(f"profile {name!r} has an invalid post-restore sentinel marker")
        sentinels.append(fields)
    for field in ("capability", "binding", "marker"):
        values = [item[field] for item in sentinels]
        if len(values) != len(set(values)):
            raise ValueError(f"profile {name!r} has duplicate post-restore sentinel {field}")
    return sentinels


def warmup_profile_publication_guard(
    script_dir: str | Path,
    name: str,
    *,
    include_developer_profiles: bool = False,
) -> dict[str, Any] | None:
    loader = load_developer_manifest if include_developer_profiles else load_manifest
    profile = _profile(loader(script_dir), name)
    guard = profile.get("publication_guard")
    if guard is None:
        return None
    if not isinstance(guard, dict):
        raise ValueError(f"profile {name!r} publication_guard must be an object")
    resolved_guard = dict(guard)
    raw_preloads = guard.get("required_extra_preloads")
    if raw_preloads is None:
        return resolved_guard
    if not isinstance(raw_preloads, list) or not all(isinstance(item, str) for item in raw_preloads):
        raise ValueError(f"profile {name!r} publication_guard required_extra_preloads must be a list of strings")
    resource_root = repository_resource_root(workbench_dir_from_script_dir(script_dir)).resolve()
    resolved_preloads: list[str] = []
    for item in raw_preloads:
        if not item.strip():
            raise ValueError(f"profile {name!r} publication_guard has an empty required preload")
        path = Path(item).expanduser()
        if not path.is_absolute():
            if any(part in {"", ".", ".."} for part in path.parts):
                raise ValueError(f"profile {name!r} publication_guard has unsafe resource-relative preload {item!r}")
            path = resource_root / path
            resolved = path.resolve()
            if not resolved.is_relative_to(resource_root):
                raise ValueError(
                    f"profile {name!r} publication_guard resource-relative preload escapes the Workbench root"
                )
        else:
            resolved = path.resolve()
        resolved_preloads.append(str(resolved))
    resolved_guard["required_extra_preloads"] = resolved_preloads
    return resolved_guard


def developer_warmup_profile_publication_guard(script_dir: str | Path, name: str) -> dict[str, Any] | None:
    return warmup_profile_publication_guard(script_dir, name, include_developer_profiles=True)


def warmup_profile_assignments(
    script_dir: str | Path,
    name: str,
    *,
    include_developer_profiles: bool = False,
    materialize_base: bool = True,
) -> dict[str, str]:
    loader = load_developer_manifest if include_developer_profiles else load_manifest
    data = loader(script_dir)
    profile = _profile(data, name)
    workbench_dir = workbench_dir_from_script_dir(script_dir)
    manifest_root = project_manifest_root(workbench_dir)
    repo_root = workbench_dir.parent
    if machine_dispatch_active():
        authoritative_checkout = str(client_identity().get("checkout") or "")
        if authoritative_checkout:
            repo_root = Path(authoritative_checkout).expanduser().resolve()

    project_profile = None
    lines = profile.get("base_lines")
    project_manifest_value = profile.get("project_manifest")
    if project_manifest_value is not None:
        if lines is not None or profile.get("cwd") is not None or not isinstance(project_manifest_value, str):
            print(
                f"prove: project warmup profile {name!r} must define project_manifest instead of base_lines/cwd",
                file=sys.stderr,
            )
            raise SystemExit(2)
        manifest_path = Path(project_manifest_value).expanduser()
        if not manifest_path.is_absolute():
            manifest_path = manifest_root / manifest_path
        try:
            project_profile = load_project_warm_profile(manifest_path, expected_profile=name)
        except (OSError, TypeError, ProjectWarmProfileError) as exc:
            print(f"prove: project warmup profile {name!r} is invalid: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        parent = _profile(data, project_profile.parent_profile)
        parent_lines = parent.get("base_lines")
        if (
            parent.get("project_manifest") is not None
            or not isinstance(parent_lines, list)
            or not parent_lines
            or not all(isinstance(line, str) for line in parent_lines)
        ):
            print(
                f"prove: project warmup profile {name!r} requires a static parent profile",
                file=sys.stderr,
            )
            raise SystemExit(2)
        lines = project_profile.base_lines(parent_lines)
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines) or not lines:
        print(f"prove: warmup profile {name!r} must define nonempty base_lines[]", file=sys.stderr)
        raise SystemExit(2)

    summary = str(profile.get("summary") or "")
    expected = str(profile.get("expected_load") or "")
    convention = str(profile.get("scratch_convention") or "")
    output_profile = str(profile.get("output_profile") or "")
    legacy_holdir_roots = _legacy_holdir_roots(profile, name=name)
    try:
        logical_source_roots = logical_source_root_declarations(
            profile.get("logical_source_roots"),
            profile=name,
        )
    except LogicalSourceRootError as exc:
        print(f"prove: warmup profile {name!r} has invalid logical source roots: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    basis_id = str(profile.get("basis_id") or profile.get("alias_of") or name)
    if project_profile is not None:
        if "basis_id" in profile and basis_id != project_profile.basis_id:
            print(f"prove: project warmup profile {name!r} basis_id disagrees with its manifest", file=sys.stderr)
            raise SystemExit(2)
        basis_id = project_profile.basis_id
    alias_of = str(profile.get("alias_of") or "")
    if "execution_topology" in profile:
        print(
            f"prove: warmup profile {name!r} uses the removed execution_topology selector; "
            "all profiles use the sole v3 runtime",
            file=sys.stderr,
        )
        raise SystemExit(2)
    execution_topology = RestoredExecutionTopology.MECHANICAL_BASIS_BROKER_V3
    cwd_policy = str(profile.get("cwd_policy") or "strict")
    if cwd_policy not in VALID_CWD_POLICIES:
        print(f"prove: warmup profile {name!r} has invalid cwd_policy {cwd_policy!r}", file=sys.stderr)
        raise SystemExit(2)

    recommended_pool_size = profile.get("recommended_pool_size")
    if recommended_pool_size is None:
        recommended_pool_size_text = ""
    elif isinstance(recommended_pool_size, int) and recommended_pool_size > 0:
        recommended_pool_size_text = str(recommended_pool_size)
    else:
        print(
            f"prove: warmup profile {name!r} has invalid recommended_pool_size; expected a positive integer",
            file=sys.stderr,
        )
        raise SystemExit(2)

    orbstack_public_capacity = profile.get("orbstack_public_capacity", 1)
    if not isinstance(orbstack_public_capacity, int) or orbstack_public_capacity <= 0:
        print(
            f"prove: warmup profile {name!r} has invalid orbstack_public_capacity; expected a positive integer",
            file=sys.stderr,
        )
        raise SystemExit(2)

    useful_operations_text = " | ".join(_useful_operation_strings(profile, name, strict=True))
    basis_content = "\n".join([*lines, ""])
    digest = sha256_text(basis_content) or ""
    content = "\n".join([*lines, ""])
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", basis_id).strip("-") or "profile"
    cache_root = os.environ.get("HOL_WORKBENCH_WARMUP_PROFILE_ROOT")
    if cache_root:
        root = Path(cache_root).expanduser()
    else:
        root = workbench_cache_root() / "warmup-profiles"
    base = root / f"{safe_name}-{digest[:12]}.ml"
    if materialize_base and os.environ.get("HOL_WORKBENCH_WARMUP_PROFILE_DRY_RUN") != "1":
        root.mkdir(parents=True, exist_ok=True)
        if not base.exists() or base.read_text(encoding="utf-8", errors="replace") != content:
            base.write_text(content, encoding="utf-8")

    profile_cwd = str(profile.get("cwd") or "")
    profile_cwd_root = str(profile.get("cwd_root") or "checkout")
    if profile_cwd_root == "checkout":
        configured_cwd_root = repo_root
    elif profile_cwd_root == "criu_shelf":
        configured_cwd_root = resolve_criu_shelf_root().path
    else:
        print(
            f"prove: warmup profile {name!r} has invalid cwd_root {profile_cwd_root!r}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not profile_cwd and "cwd_root" in profile:
        print(
            f"prove: warmup profile {name!r} defines cwd_root without cwd",
            file=sys.stderr,
        )
        raise SystemExit(2)
    profile_context = resolve_profile_project_context(
        configured_cwd_root,
        profile_cwd,
        root_label=profile_cwd_root,
    )
    project_assignments: dict[str, str] = {}
    if project_profile is not None:
        profile_context = resolve_profile_project_context(
            repo_root,
            str(project_profile.project_root),
            root_label="project_manifest",
        )
        project_assignments = {
            "PROFILE_PROJECT_MANIFEST": str(project_profile.manifest),
            "PROFILE_PROJECT_MANIFEST_SHA256": project_profile.manifest_sha256,
            "PROFILE_PROJECT_IDENTITY_SHA256": project_profile.identity_sha256,
            "PROFILE_PROJECT_BASE_COMMIT": project_profile.base_commit,
            "PROFILE_PROJECT_REPOSITORY_CLEAN": str(project_profile.repository_clean).lower(),
            "PROFILE_PROJECT_ACTIVE_SOURCE": project_profile.active_source,
            "PROFILE_PROJECT_SENTINEL_THEOREM": project_profile.sentinel_theorem,
        }

    return {
        "PROFILE_NAME": name,
        "PROFILE_BASE": str(base.resolve()),
        "PROFILE_SHA256": digest,
        "PROFILE_BASIS_ID": basis_id,
        "PROFILE_ALIAS_OF": alias_of,
        "PROFILE_EXECUTION_TOPOLOGY": execution_topology.value,
        "PROFILE_CWD_POLICY": cwd_policy,
        "PROFILE_SUMMARY": summary,
        "PROFILE_EXPECTED_LOAD": expected,
        "PROFILE_SCRATCH_CONVENTION": convention,
        **profile_context.assignments(),
        **project_assignments,
        "PROFILE_OUTPUT_PROFILE": output_profile,
        "PROFILE_LEGACY_HOLDIR_ROOTS_JSON": json.dumps(legacy_holdir_roots, separators=(",", ":")),
        "PROFILE_LOGICAL_SOURCE_ROOTS_JSON": json.dumps(logical_source_roots, separators=(",", ":")),
        "PROFILE_RECOMMENDED_POOL_SIZE": recommended_pool_size_text,
        "PROFILE_ORBSTACK_PUBLIC_CAPACITY": str(orbstack_public_capacity),
        "PROFILE_USEFUL_OPERATIONS": useful_operations_text,
    }


def developer_warmup_profile_assignments(
    script_dir: str | Path,
    name: str,
    *,
    materialize_base: bool = True,
) -> dict[str, str]:
    return warmup_profile_assignments(
        script_dir,
        name,
        include_developer_profiles=True,
        materialize_base=materialize_base,
    )


def warmup_profile_identity_assignments(script_dir: str | Path, name: str) -> dict[str, str]:
    """Project a public profile identity without creating runtime cache files."""

    return warmup_profile_assignments(script_dir, name, materialize_base=False)


def developer_warmup_profile_identity_assignments(script_dir: str | Path, name: str) -> dict[str, str]:
    """Project profile identity without creating or rewriting its generated base."""

    return developer_warmup_profile_assignments(script_dir, name, materialize_base=False)


def print_shell_assignments(assignments: dict[str, str]) -> None:
    for key, value in assignments.items():
        print(f"{key}={shlex.quote(value)}")


def _useful_operation_rows(profile: dict[str, Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for item in _useful_operation_strings(profile, "", strict=False):
        if ": " in item:
            name, hint = item.split(": ", 1)
        else:
            name, hint = item, ""
        rows.append((name, hint))
    return rows


def _profile_use_commands(profile: str | None = None) -> list[str]:
    profile_name = profile or "NAME"
    return [
        public_command("prove", "/ABS/SOURCE.ml", "--profile", profile_name, "--run-root", "/ABS/runs"),
        public_command("prove", "profiles", profile_name),
    ]


def _print_compact_profile(profile_name: str, profile: dict[str, Any]) -> None:
    operations = ", ".join(name for name, _hint in _useful_operation_rows(profile)) or "-"
    print(f"PROFILE: {profile_name}")
    print(f"SUMMARY: {profile.get('summary') or '-'}")
    print(
        f"SEATS: Linux warm baseline={profile.get('orbstack_public_capacity', 1)} "
        "(logical seats may expand over the same immutable basis)"
    )
    print(f"OPERATIONS: {operations}")
    print(f"NEXT: {_profile_use_commands(profile_name)[0]}")


def _print_compact_profiles(profiles: dict[str, Any]) -> None:
    print("HOL PROFILES")
    print("BASIC: light")
    print("CALCULUS/GEOMETRY: heavy")
    print("PROBABILITY: probability")
    print("ARM: s2n-arm, s2n-arm-light, s2n-arm-mlkem")
    print("X86: s2n-x86")
    print(f"NEXT: {public_command('prove', 'profiles', 'NAME')}")
    print(f"DETAILS: {public_command('prove', 'profiles', '--all')}")


def profiles_command(script_dir: str | Path, args: list[str]) -> int:
    data = load_manifest(script_dir)
    profiles = _public_profiles(data)
    public_authoring_profile_names(script_dir)
    verbose = "--verbose" in args
    show_all = "--all" in args
    args = [arg for arg in args if arg not in {"--verbose", "--all"}]
    if args == ["--json"]:
        as_json = True
        detail_name = None
    elif not args:
        as_json = False
        detail_name = None
    elif len(args) == 1 and not args[0].startswith("-"):
        as_json = False
        detail_name = args[0]
    else:
        print("usage: hearth profiles [--json|--all|PROFILE [--verbose]]")
        print("Explore theorems in a proof source with search or print_thm, then inspect its transcript.")
        return 0 if args in (["--help"], ["-h"]) else 2

    if as_json:
        print(json.dumps(public_profile_document(data), indent=2, sort_keys=True))
        return 0

    if detail_name:
        profile = profiles.get(detail_name)
        if not isinstance(profile, dict):
            available = ", ".join(profiles) or "(none)"
            raise SystemExit(
                f"profile {detail_name!r} is not published for Linux warm authoring; available: {available}"
            )
        if not verbose:
            _print_compact_profile(detail_name, profile)
            return 0
        print(f"HOL warm profile: {detail_name}")
        print("=" * (18 + len(detail_name)))
        print(f"summary: {profile.get('summary') or '-'}")
        print(f"Linux warm baseline logical seats: {profile.get('orbstack_public_capacity', 1)}")
        print("Live logical seats may expand with demand over the same immutable physical basis.")
        if profile.get("cwd"):
            print(f"cwd: {profile.get('cwd')}")
        print(f"scratch: {profile.get('scratch_convention') or '-'}")
        ops = _useful_operation_rows(profile)
        print("")
        print("Useful operations:")
        if ops:
            for index, (name, hint) in enumerate(ops, 1):
                if hint:
                    print(f"{index}. {name} - {hint}")
                else:
                    print(f"{index}. {name}")
        else:
            print("(none listed yet)")
        print("")
        print("Use:")
        for command in _profile_use_commands(detail_name):
            print(f"  {command}")
        return 0

    if not (verbose or show_all):
        _print_compact_profiles(profiles)
        return 0

    print("HOL warm checkpoint profiles")
    print("============================")
    print("")
    print("Use:")
    for command in _profile_use_commands():
        print(f"  {command}")
    print("")
    default_names = ["light", "heavy", "probability"]
    special_names = [profile_name for profile_name in profiles if profile_name not in default_names]
    shown: set[str] = set()

    def print_profile_rows(title: str, names: list[str]) -> None:
        rows: list[tuple[str, dict[str, Any]]] = []
        for profile_name in names:
            profile = profiles.get(profile_name)
            if isinstance(profile, dict):
                rows.append((profile_name, profile))
        if not rows:
            return
        print(title)
        for profile_name, profile in rows:
            shown.add(profile_name)
            print(f"- {profile_name}: {profile.get('summary') or '-'}")
            print(f"  Linux warm baseline logical seats: {profile.get('orbstack_public_capacity', 1)}")
        print("")

    print_profile_rows("Default path:", default_names)
    print_profile_rows("Project/special profiles:", special_names)

    compat_names = [profile_name for profile_name in profiles if profile_name not in shown]
    if compat_names:
        print("Compatibility/expert profiles:")
        print("- " + ", ".join(compat_names))
        print(
            f"  Use `{preferred_tool_command('prove')} profiles NAME` for details. "
            "Prefer light/heavy unless a receipt asks for a narrower profile."
        )
    print("")
    print("Rule of thumb:")
    print("- light: ordinary arithmetic, REAL_RING/REAL_FIELD, calc_rat, and ring scratch")
    print("- heavy: Multivariate realanalysis/calculus, topology/complex/vector, and tether/physics scratch")
    print("- probability: full upstream Probability/* basis for expectation, martingales, CLT, and ergodic work")
    print("- s2n-arm: published functional AArch64/s2n-bignum ARM proof base")
    print("- s2n-arm-light: ARM proof base plus the complete light arithmetic and abstract-ring basis")
    print("- s2n-arm-mlkem: published ARM ML-KEM/ML-DSA NTT and bignum project basis")
    print("- s2n-x86: published x86-64/s2n-bignum instruction, ABI, and bignum proof base")
    print("")
    print("Sharing model:")
    print("- Linux restores one compatible published warm basis")
    print("- the basis starts at its configured baseline and may add logical seats with live demand")
    print("- expanded logical seats share one physical fork basis; they do not duplicate the loaded HOL basis")
    print("- every source evaluates in a disposable child")
    print("- unavailable profile contents are a dev-lane publication task, never an author-side load or repair")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    assignments_parser = subparsers.add_parser("assignments")
    assignments_parser.add_argument("script_dir")
    assignments_parser.add_argument("profile")

    profiles_parser = subparsers.add_parser("profiles")
    profiles_parser.add_argument("script_dir")
    profiles_parser.add_argument("args", nargs=argparse.REMAINDER)

    parsed = parser.parse_args(argv)
    if parsed.command == "assignments":
        print_shell_assignments(developer_warmup_profile_assignments(parsed.script_dir, parsed.profile))
        return 0
    if parsed.command == "profiles":
        return profiles_command(parsed.script_dir, parsed.args)
    return parser.error(f"unknown command: {parsed.command}")


if __name__ == "__main__":
    raise SystemExit(main())
