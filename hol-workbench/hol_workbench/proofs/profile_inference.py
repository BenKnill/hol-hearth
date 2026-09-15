"""Dependency-light public profile inference for ordinary proof sources."""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from hol_workbench.proofs.loader_scan import LoaderScanResult
from hol_workbench.proofs.source import (
    PROFILE_HINT_PATTERNS,
    TRANSITIVE_NEEDS_MAX_DEPTH,
    TRANSITIVE_NEEDS_MAX_FILES,
    mask_ocaml_comments_and_strings,
    path_is_within,
    resolve_local_need_path,
    resolve_local_source_load_path,
    scan_hol_loaders,
)

SOURCE_LOAD_SCAN_MAX_DEPTH = 16
SOURCE_LOAD_SCAN_MAX_FILES = 64
SOURCE_LOAD_CLOSURE_LIMIT_PROFILE = "source-load-closure-limit"
SOURCE_LOADER_SCAN_REFUSED_PROFILE = "source-loader-scan-refused"

PublicProfileInferenceStatus = Literal["selected", "refused"]


@dataclass(frozen=True)
class PublicProfileInference:
    """Registry-bounded result of translating one static source hint."""

    status: PublicProfileInferenceStatus
    inferred_profile: str
    profile: str | None
    reason: str


_PUBLIC_PROFILE_FALLBACKS = {
    "core": "light",
}


def resolve_public_profile_inference(
    inferred_profile: str,
    public_profiles: Collection[str],
) -> PublicProfileInference:
    """Select only a registry-published profile, or refuse without guessing."""

    published = frozenset(public_profiles)
    if inferred_profile == SOURCE_LOADER_SCAN_REFUSED_PROFILE:
        return PublicProfileInference(
            status="refused",
            inferred_profile=inferred_profile,
            profile=None,
            reason=(
                "the strict source-loader scan found dynamic loader syntax or malformed lexical input; "
                "no profile was guessed and HOL evaluation was not started"
            ),
        )
    if inferred_profile in published:
        return PublicProfileInference(
            status="selected",
            inferred_profile=inferred_profile,
            profile=inferred_profile,
            reason="the inferred profile is published for Linux warm authoring",
        )
    mapped = _PUBLIC_PROFILE_FALLBACKS.get(inferred_profile)
    if mapped is not None and mapped in published:
        return PublicProfileInference(
            status="selected",
            inferred_profile=inferred_profile,
            profile=mapped,
            reason="generic source maps to the default public light basis",
        )
    return PublicProfileInference(
        status="refused",
        inferred_profile=inferred_profile,
        profile=None,
        reason=(
            f"static source scan selected {inferred_profile!r}, but no compatible public profile "
            "is declared by the Linux registry"
        ),
    )


def infer_public_profile_for_source(
    source: str | Path | None,
    public_profiles: Collection[str],
) -> PublicProfileInference:
    """Scan one source, then enforce the public-registry boundary."""

    return resolve_public_profile_inference(
        suggest_warmup_profile_for_source(source),
        public_profiles,
    )


def _has_dynamic_source_loader(scan: LoaderScanResult) -> bool:
    return any(
        item.family == "source" and item.outcome == "dynamic"
        for item in scan.occurrences
    )


def _bounded_source_load_closure(source: Path) -> tuple[list[Path], bool, bool]:
    """Follow literal source-local loads without turning inference into discovery."""

    pending = [(source.expanduser().resolve(), 0)]
    paths: list[Path] = []
    seen: set[Path] = set()
    limited = False
    refused = False
    while pending:
        path, depth = pending.pop()
        if path in seen:
            continue
        if len(seen) >= SOURCE_LOAD_SCAN_MAX_FILES:
            limited = True
            break
        seen.add(path)
        paths.append(path)
        try:
            scan = scan_hol_loaders(path)
        except OSError:
            continue
        if scan.status == "refused" or _has_dynamic_source_loader(scan):
            refused = True
            break
        children: list[Path] = []
        for item in scan.literal_occurrences:
            if item.family != "source" or item.path is None:
                continue
            if item.loader == "needs":
                continue
            resolved = resolve_local_source_load_path(path, item.path)
            if resolved is None:
                continue
            child = resolved.expanduser().resolve()
            if child in seen:
                continue
            if depth >= SOURCE_LOAD_SCAN_MAX_DEPTH:
                limited = True
                continue
            children.append(child)
        pending.extend((child, depth + 1) for child in reversed(children))
    return paths, limited, refused


def _bounded_need_paths(source: Path) -> tuple[list[str], bool]:
    """Preserve the existing needs-only bounds while sharing lexical decisions."""

    source = source.expanduser().resolve()
    allowed_roots = {source.parent}
    visited_files = {source}
    stack: list[tuple[Path, int]] = []
    need_paths: list[str] = []

    try:
        root_scan = scan_hol_loaders(source)
    except OSError:
        return need_paths, False
    if root_scan.status == "refused" or _has_dynamic_source_loader(root_scan):
        return need_paths, True

    for item in root_scan.literal_occurrences:
        if item.family != "source" or item.loader != "needs" or item.path is None:
            continue
        need_paths.append(item.path)
        resolved = resolve_local_need_path(source, item.path)
        if resolved is not None and resolved not in visited_files:
            allowed_roots.add(resolved.parent)
            stack.append((resolved, 1))

    scanned_files = 0
    while stack and scanned_files < TRANSITIVE_NEEDS_MAX_FILES:
        current, depth = stack.pop()
        if current in visited_files or depth > TRANSITIVE_NEEDS_MAX_DEPTH:
            continue
        visited_files.add(current)
        scanned_files += 1
        try:
            scan = scan_hol_loaders(current)
        except OSError:
            continue
        if scan.status == "refused" or _has_dynamic_source_loader(scan):
            return need_paths, True
        for item in scan.literal_occurrences:
            if item.family != "source" or item.loader != "needs" or item.path is None:
                continue
            need_paths.append(item.path)
            resolved = resolve_local_need_path(current, item.path)
            if (
                resolved is not None
                and any(path_is_within(resolved, root) for root in allowed_roots)
                and resolved not in visited_files
                and depth < TRANSITIVE_NEEDS_MAX_DEPTH
            ):
                stack.append((resolved, depth + 1))
    return need_paths, False


def suggest_warmup_profile_for_source(source: str | Path | None) -> str:
    if not source:
        return "core"
    paths, limited, refused = _bounded_source_load_closure(Path(source))
    if refused:
        return SOURCE_LOADER_SCAN_REFUSED_PROFILE
    if limited:
        return SOURCE_LOAD_CLOSURE_LIMIT_PROFILE

    texts: list[str] = []
    readable_paths: list[Path] = []
    need_paths: list[str] = []
    for path in paths:
        try:
            text = path.read_bytes().decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        texts.append(text)
        readable_paths.append(path)
        path_needs, path_refused = _bounded_need_paths(path)
        if path_refused:
            return SOURCE_LOADER_SCAN_REFUSED_PROFILE
        need_paths.extend(path_needs)
    if not texts:
        return "core"

    text = "\n".join(texts)
    lower_path = "\n".join(str(path).lower() for path in readable_paths)
    lower_text = text.lower()
    masked_for_hints = mask_ocaml_comments_and_strings(text)

    def has_need(fragment: str) -> bool:
        return any(fragment in need_path for need_path in need_paths)

    if has_need("arm/proofs/utils/aes_xts_") or re.search(r"\bAES_XTS_[A-Z0-9_]+\b", masked_for_hints):
        return "s2n-arm-aes-xts"
    if has_need("arm/proofs/utils/aes_encrypt_spec.ml"):
        return "s2n-arm-aes"
    # Architecture-bearing evidence wins over the shared ML-KEM/ML-DSA
    # definitions: real x86 post-quantum proofs need both resources, while the
    # ARM ML-KEM shelf cannot supply the x86 instruction basis.
    if has_need("x86/proofs/") or re.search(r"\bX86_(?:STEPS|SIM)_TAC\b", masked_for_hints):
        return "s2n-x86"
    has_mlkem_semantic_signature = bool(
        re.search(r"\bbitreverse7\b", masked_for_hints) and re.search(r"\b3329\b", masked_for_hints)
    )
    if has_need("common/mlkem_mldsa.ml") or has_mlkem_semantic_signature:
        return "s2n-arm-mlkem"
    if has_need("arm/proofs/") or re.search(r"\bARM_(?:STEPS|SIM)_TAC\b", masked_for_hints):
        return "s2n-arm"
    if has_need("Formal_ineqs/") or "M_verifier_main" in masked_for_hints:
        return "formal-ineqs"
    if has_need("Probability/"):
        return "probability"
    if has_need("Multivariate/flyspeck.ml") or "Flyspeck" in masked_for_hints or "flyspeck" in masked_for_hints:
        return "flyspeck-geom"
    if has_need("Multivariate/realanalysis.ml"):
        return "heavy"
    for profile, _name, pattern, _message in PROFILE_HINT_PATTERNS:
        if pattern.search(masked_for_hints):
            return profile
    if (
        "tether" in lower_path
        or "tether" in lower_text
        or "chain_segment_laccel_model" in text
        or "node_eom2" in text
        or "CHAIN_FORCE_MASS" in text
    ):
        return "heavy"
    if has_need("Multivariate/"):
        return "heavy"
    if (
        "RING_RULE" in masked_for_hints
        or "REAL_RING" in masked_for_hints
        or "REAL_FIELD" in masked_for_hints
        or has_need("Library/ringtheory.ml")
        or has_need("calc_rat.ml")
    ):
        return "light"
    return "core"
