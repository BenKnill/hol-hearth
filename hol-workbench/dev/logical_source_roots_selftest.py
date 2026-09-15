#!/usr/bin/env python3
"""Executable Phase-1 contracts for logical source replay and fail-closed loop scope."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

from hol_workbench.cli.prove_loop import _source_import_roots
from hol_workbench.cli.reconcile_source_layout import _materialize
from hol_workbench.logical_source_roots import (
    LOGICAL_SOURCE_ROOT_SCHEMA,
    SOURCE_GENERATION_SCHEMA,
    LogicalSourceRootError,
    classify_logical_literal,
    logical_source_root_declarations,
    logical_source_root_identity,
    managed_source_paths,
    resolve_logical_project_roots,
    resolve_logical_source_roots,
    validate_requested_logical_source_roots,
)
from hol_workbench.source_dependency_closure import build_source_dependency_closure
from hol_workbench.source_dependency_package import DependencyPackageError, materialize_dependency_package

REVISION = "1" * 40
TREE = "2" * 40


def declaration(
    alias: str,
    source_role: str,
    execution_role: str = "none",
    *,
    source_subdir: str = ".",
    project_subdir: str = ".",
) -> dict[str, str]:
    row = {"alias": alias, "source_role": source_role, "execution_role": execution_role}
    if source_role == "managed_mirror":
        row.update(remote="https://example.invalid/source.git", revision=REVISION, tree=TREE)
    else:
        row["source_subdir"] = source_subdir
        row["project_subdir"] = project_subdir
    return row


DECLARATIONS = (
    declaration("s2n_bignum", "managed_mirror", "profile_cwd"),
    declaration(
        "mldsa_native",
        "entrypoint_repository",
        source_subdir="proofs/hol_light",
        project_subdir="artifact-project",
    ),
)


def require_error(status: str, action: object) -> None:
    try:
        action()  # type: ignore[operator]
    except (DependencyPackageError, LogicalSourceRootError) as exc:
        assert exc.status == status, (exc.status, str(exc))
    else:
        raise AssertionError(f"expected {status}")


def generation(cache: Path, source: Path) -> None:
    root = cache / "hol-workbench" / "source-generations" / "s2n_bignum" / REVISION
    tree = root / "tree"
    (tree / "arm" / "nested").mkdir(parents=True)
    (tree / "arm" / "base.ml").write_bytes(source.read_bytes())
    (tree / "arm" / "nested" / "leaf.ml").write_text("let LEAF = 1;;\n", encoding="utf-8")
    (tree / "arm" / "object.o").write_bytes(b"object")
    files = {}
    for path in sorted(tree.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            files[path.relative_to(tree).as_posix()] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
    payload = {
        "schema": SOURCE_GENERATION_SCHEMA,
        "alias": "s2n_bignum",
        "remote": "https://example.invalid/source.git",
        "revision": REVISION,
        "tree": TREE,
        "files": files,
    }
    payload["strict_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload["local_tree_root"] = str(tree)
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def reconciler_recovery_contract(root: Path) -> None:
    remote = root / "managed-remote"
    remote.mkdir()
    subprocess.run(("git", "init", "-q", str(remote)), check=True)
    tracked = remote / "arm" / "base.ml"
    tracked.parent.mkdir()
    tracked.write_text("let MANAGED = 1;;\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(remote), "add", "arm/base.ml"), check=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(remote),
            "-c",
            "user.name=Workbench selftest",
            "-c",
            "user.email=workbench-selftest@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ),
        check=True,
    )
    revision = subprocess.run(
        ("git", "-C", str(remote), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree_identity = subprocess.run(
        ("git", "-C", str(remote), "rev-parse", "HEAD^{tree}"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    row = {
        "alias": "repairable",
        "source_role": "managed_mirror",
        "execution_role": "none",
        "remote": str(remote),
        "revision": revision,
        "tree": tree_identity,
    }
    cache = root / "reconciler-cache"
    old = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = str(cache)
    try:
        assert _materialize(row)["status"] == "ready"
        _bare, tree, _manifest = managed_source_paths(row)
        corrupted = tree / "arm" / "base.ml"
        corrupted.chmod(0o644)
        corrupted.write_text("let MANAGED = 0;;\n", encoding="utf-8")
        assert _materialize(row)["status"] == "ready"
        assert corrupted.read_text(encoding="utf-8") == "let MANAGED = 1;;\n"
        quarantines = tuple(tree.parent.parent.glob(f".{revision}.invalid-*/generation"))
        assert len(quarantines) == 1
        assert (quarantines[0] / "tree" / "arm" / "base.ml").read_text(encoding="utf-8") == (
            "let MANAGED = 0;;\n"
        )
        assert _materialize(row)["status"] == "ready"
    finally:
        if old is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = old


def main() -> int:
    assert LOGICAL_SOURCE_ROOT_SCHEMA == "hol-workbench.logical-source-roots.v4"
    assert logical_source_root_identity(DECLARATIONS)["schema"] == LOGICAL_SOURCE_ROOT_SCHEMA
    aliases = {"s2n_bignum", "mldsa_native"}
    matrix = {
        "s2n_bignum/arm/base.ml": "logical",
        "./s2n_bignum/arm/base.ml": "declaring_relative",
        "../s2n_bignum/arm/base.ml": "declaring_relative",
        "s2n_bignum/../outside.ml": "invalid_logical",
        "s2n_bignum/./arm/base.ml": "invalid_logical",
        "s2n_bignum//arm/base.ml": "invalid_logical",
        "s2n_bignum/arm/./base.ml": "invalid_logical",
        "s2n_bignum_extra/arm/base.ml": "ordinary_relative",
        "nested/s2n_bignum/arm/base.ml": "ordinary_relative",
    }
    for literal, expected in matrix.items():
        assert classify_logical_literal(literal, aliases)[0] == expected, literal
    require_error(
        "refused_logical_source_root_declaration",
        lambda: logical_source_root_declarations(
            [
                {
                    "alias": "mldsa_native",
                    "source_role": "entrypoint_repository",
                    "source_subdir": "../proofs/hol_light",
                    "project_subdir": ".",
                    "execution_role": "none",
                }
            ],
            profile="unsafe-subdir",
        ),
    )
    require_error(
        "refused_logical_source_root_declaration",
        lambda: logical_source_root_declarations(
            [
                {
                    "alias": "mldsa_native",
                    "source_role": "entrypoint_repository",
                    "source_subdir": "proofs/hol_light",
                    "project_subdir": "../artifacts",
                    "execution_role": "none",
                }
            ],
            profile="unsafe-project-subdir",
        ),
    )
    require_error(
        "refused_logical_source_root_declaration",
        lambda: logical_source_root_declarations(
            [
                {
                    "alias": "mldsa_native",
                    "source_role": "entrypoint_repository",
                    "source_subdir": "proofs/hol_light",
                    "execution_role": "none",
                }
            ],
            profile="missing-project-subdir",
        ),
    )

    with tempfile.TemporaryDirectory(prefix="holwb-logical-roots-") as temporary:
        root = Path(temporary)
        reconciler_recovery_contract(root)
        cache_a = root / "machine-a-cache"
        cache_b = root / "machine-b" / "different-cache"
        repository = root / "mldsa-repository"
        (repository / ".git").mkdir(parents=True)
        source_tree = repository / "proofs" / "hol_light"
        artifact_project = repository / "artifact-project"
        (artifact_project / "objects").mkdir(parents=True)
        helper_object = artifact_project / "objects" / "helper.o"
        helper_object.write_bytes(b"helper object")
        (source_tree / "proofs").mkdir(parents=True)
        source = source_tree / "proofs" / "helper.ml"
        source.write_text(
            'needs "s2n_bignum/arm/base.ml";;\n'
            'let HELPER_OBJ = define_from_elf "HELPER_OBJ" "objects/helper.o";;\n'
            'let HELPER = prove(`T`,REWRITE_TAC[]);;\n',
            encoding="utf-8",
        )
        mapped = root / "mapped-base.ml"
        mapped.write_text(
            'loadt "nested/leaf.ml";;\nlet OBJ = define_from_elf "OBJ" "arm/object.o";;\n',
            encoding="utf-8",
        )
        generation(cache_a, mapped)
        generation(cache_b, mapped)

        execution = root / "execution"
        (execution / "arm").mkdir(parents=True)
        (execution / "arm" / "base.ml").write_bytes(mapped.read_bytes())
        profile_root = root / "profile"
        (profile_root / "provenance").mkdir(parents=True)
        base_data = mapped.read_bytes()
        (profile_root / "provenance" / "snapshot-provenance.json").write_text(
            json.dumps(
                {
                    "loaded_closure": {
                        "entries": [
                            {
                                "path": str(execution / "arm" / "base.ml"),
                                "resolved_path": str(execution / "arm" / "base.ml"),
                                "sha256": hashlib.sha256(base_data).hexdigest(),
                                "size_bytes": len(base_data),
                                "roles": ["profile_cwd"],
                            }
                        ]
                    }
                }
            ),
            encoding="utf-8",
        )

        captures = []
        for cache, suffix in ((cache_a, "a"), (cache_b, "b")):
            old = os.environ.get("XDG_CACHE_HOME")
            os.environ["XDG_CACHE_HOME"] = str(cache)
            try:
                roots = resolve_logical_source_roots(source, DECLARATIONS, profile_cwd=root / "execution")
                project_roots = resolve_logical_project_roots(source, DECLARATIONS, roots)
                assert roots["mldsa_native"] == source_tree
                assert project_roots["mldsa_native"] == artifact_project
                validate_requested_logical_source_roots(source, DECLARATIONS, roots)
                if suffix == "a":
                    loop_roots = _source_import_roots(
                        source, execution, DECLARATIONS, profile_root
                    )
                    assert loop_roots["s2n_bignum"] == execution
                    assert loop_roots["mldsa_native"] == source_tree
                    unsupported_loop = source_tree / "proofs" / "recursive.ml"
                    unsupported_loop.write_text(
                        'needs "mldsa_native/proofs/helper.ml";;\n', encoding="utf-8"
                    )
                    require_error(
                        "refused_loop_recursive_logical_source_dependency",
                        lambda unsupported_loop=unsupported_loop: _source_import_roots(
                            unsupported_loop, execution, DECLARATIONS, profile_root
                        ),
                    )
                closure = build_source_dependency_closure(
                    source,
                    holdir_root=root / "empty-holdir",
                    logical_source_root_declarations=DECLARATIONS,
                    logical_source_roots=roots,
                    logical_project_roots=project_roots,
                )
                captures.append(closure)
                helper_artifact = next(
                    row for row in closure["artifacts"] if row["declared_path"] == "objects/helper.o"
                )
                assert helper_artifact["project_root"] == str(artifact_project)
                assert helper_artifact["resolved_path"] == str(helper_object)
                _entry, manifest = materialize_dependency_package(
                    source=source,
                    closure=closure,
                    destination=root / f"package-{suffix}",
                )
                assert manifest["exact_source_composition"]["evidence"] == "mixed_exact_file_bound_sources"
            finally:
                if old is None:
                    os.environ.pop("XDG_CACHE_HOME", None)
                else:
                    os.environ["XDG_CACHE_HOME"] = old
        assert captures[0]["strict_sha256"] == captures[1]["strict_sha256"]

        managed_tree = cache_a / "hol-workbench" / "source-generations" / "s2n_bignum" / REVISION / "tree"
        outside = root / "outside"
        outside.mkdir()
        base = managed_tree / "arm" / "base.ml"
        base_bytes = base.read_bytes()
        outside_base = outside / "base.ml"
        outside_base.write_bytes(base_bytes)
        base.chmod(0o644)
        base.unlink()
        base.symlink_to(outside_base)
        require_error(
            "refused_dependency_changed",
            lambda: materialize_dependency_package(
                source=source, closure=captures[0], destination=root / "file-symlink-race"
            ),
        )
        base.unlink()
        base.write_bytes(base_bytes)

        nested = managed_tree / "arm" / "nested"
        nested.chmod(0o755)
        saved_nested = managed_tree / "arm" / "nested.saved"
        nested.rename(saved_nested)
        outside_nested = outside / "nested"
        outside_nested.mkdir()
        (outside_nested / "leaf.ml").write_bytes((saved_nested / "leaf.ml").read_bytes())
        nested.symlink_to(outside_nested, target_is_directory=True)
        require_error(
            "refused_dependency_changed",
            lambda: materialize_dependency_package(
                source=source, closure=captures[0], destination=root / "parent-symlink-race"
            ),
        )
        nested.unlink()
        saved_nested.rename(nested)

        artifact = managed_tree / "arm" / "object.o"
        artifact_bytes = artifact.read_bytes()
        outside_artifact = outside / "object.o"
        outside_artifact.write_bytes(artifact_bytes)
        artifact.chmod(0o644)
        artifact.unlink()
        artifact.symlink_to(outside_artifact)
        require_error(
            "refused_dependency_changed",
            lambda: materialize_dependency_package(
                source=source, closure=captures[0], destination=root / "artifact-symlink-race"
            ),
        )
        artifact.unlink()
        artifact.write_bytes(artifact_bytes)

        old = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(cache_a)
        try:
            roots = resolve_logical_source_roots(source, DECLARATIONS, profile_cwd=None)
            for bad in (
                "s2n_bignum/../outside.ml",
                "s2n_bignum/./arm/base.ml",
                "s2n_bignum//arm/base.ml",
                "s2n_bignum/arm/./base.ml",
            ):
                candidate = source_tree / "proofs" / "bad.ml"
                candidate.write_text(f'needs "{bad}";;\n', encoding="utf-8")
                require_error(
                    "refused_invalid_logical_source_path",
                    lambda candidate=candidate: validate_requested_logical_source_roots(candidate, DECLARATIONS, roots),
                )

            missing_role = (
                declaration("s2n_bignum", "entrypoint_repository"),
                declaration("missing_alias", "entrypoint_repository"),
            )
            mapped_base = roots["s2n_bignum"] / "arm" / "base.ml"
            mapped_base.chmod(0o644)
            mapped_base.write_text('needs "missing_alias/proofs/no.ml";;\n', encoding="utf-8")
            direct_roots = {"s2n_bignum": roots["s2n_bignum"]}
            closure = build_source_dependency_closure(
                source,
                holdir_root=root / "empty-holdir",
                logical_source_root_declarations=missing_role,
                logical_source_roots=direct_roots,
            )
            assert any(row["resolution"] == "unavailable_logical_source_root" for row in closure["records"])
        finally:
            if old is None:
                os.environ.pop("XDG_CACHE_HOME", None)
            else:
                os.environ["XDG_CACHE_HOME"] = old

        same = root / "same"
        same.mkdir()
        overlap_declarations = (
            declaration("aaa", "entrypoint_repository"),
            declaration("zzz", "entrypoint_repository"),
        )
        overlap_source = same / "x.ml"
        (same / ".git").mkdir()
        overlap_source.write_text("let X = 1;;\n", encoding="utf-8")
        require_error(
            "refused_overlapping_logical_source_roots",
            lambda: resolve_logical_source_roots(overlap_source, overlap_declarations, profile_cwd=None),
        )

        linked_project = root / "linked-project"
        linked_project.mkdir()
        (same / "project-link").symlink_to(linked_project, target_is_directory=True)
        symlink_declaration = (
            declaration("same", "entrypoint_repository", project_subdir="project-link"),
        )
        same_roots = resolve_logical_source_roots(overlap_source, symlink_declaration, profile_cwd=None)
        require_error(
            "refused_logical_source_root",
            lambda: resolve_logical_project_roots(overlap_source, symlink_declaration, same_roots),
        )

    print("logical-source Phase-1 self-test: lexical, portable, unavailable, overlap contracts passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
