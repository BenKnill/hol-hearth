"""Capacity-bounded public admission for one physical CRIU profile shelf."""

from __future__ import annotations

import fcntl
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from hol_workbench.criu_shelf_demand import clear_shelf_demand, read_shelf_demands, write_shelf_demand
from hol_workbench.criu_shelf_owner import clear_shelf_owner, write_shelf_owner


@dataclass(frozen=True)
class ShelfAdmission:
    """Observed wait state for one acquired shelf slot."""

    contended: bool
    wait_seconds: float
    slot: int = 1
    capacity: int = 1


class ShelfAdmissionInterrupted(Exception):
    """The caller interrupted a contended shelf acquisition."""


class ShelfAdmissionUnavailable(Exception):
    """No shelf slot was immediately available to a fail-fast caller."""


def shelf_admission_paths(profile_root: Path, capacity: int) -> list[Path]:
    """Return the canonical numbered lock path for every logical slot."""

    if capacity <= 0:
        raise ValueError("CRIU shelf admission capacity must be positive")
    return [profile_root / f"admission-{slot}.lock" for slot in range(1, capacity + 1)]


def _shelf_admission_lock_groups(profile_root: Path, capacity: int) -> list[tuple[Path, ...]]:
    """Return every file a caller must hold for each logical slot.

    Slot one bridges the retired single-file namespace for one compatibility
    generation. Acquiring both files means old ``admission.lock`` clients and
    canonical ``admission-1.lock`` clients consume the same logical slot.
    """

    canonical = shelf_admission_paths(profile_root, capacity)
    return [(profile_root / "admission.lock", canonical[0]), *((path,) for path in canonical[1:])]


def _try_lock_group(locks: list[TextIO]) -> bool:
    acquired: list[TextIO] = []
    try:
        for lock in locks:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            acquired.append(lock)
        return True
    finally:
        if len(acquired) != len(locks):
            for lock in reversed(acquired):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _unlock_group(locks: list[TextIO]) -> None:
    for lock in reversed(locks):
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def try_exclusive_criu_shelf_admission(profile_root: Path, *, capacity: int) -> Iterator[bool]:
    """Try to close both admission generations without waiting.

    Idle retirement must exclude old clients on ``admission.lock`` as well as
    every canonical numbered slot. The order matches normal slot-one
    acquisition so two current clients cannot deadlock.
    """

    locks: list[TextIO] = []
    acquired: list[TextIO] = []
    available = True
    try:
        paths = [profile_root / "admission.lock", *shelf_admission_paths(profile_root, capacity)]
        for path in paths:
            lock = path.open("a+", encoding="utf-8")
            locks.append(lock)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                available = False
                break
            acquired.append(lock)
        yield available
    finally:
        _unlock_group(acquired)
        for lock in locks:
            lock.close()


@contextmanager
def criu_shelf_admission(
    profile_root: Path,
    *,
    capacity: int = 1,
    on_contention: Callable[[], None] | None = None,
    owner: dict | None = None,
    wait: bool = True,
    capacity_expander: Callable[[int], int] | None = None,
) -> Iterator[ShelfAdmission]:
    """Hold one public route slot for ``profile_root``.

    Every slot is an advisory lock group. A caller probes all slots before
    reporting contention, then waits until any slot becomes available. Slot
    one holds both ``admission.lock`` and canonical ``admission-1.lock`` so
    capacity changes remain safe while older installed clients still exist.
    """
    if not profile_root.is_dir():
        raise FileNotFoundError(f"CRIU profile shelf not found: {profile_root}")
    if capacity <= 0:
        raise ValueError("CRIU shelf admission capacity must be positive")
    started = time.monotonic()
    demand_attempt = str(owner.get("attempt_id") or "") if owner is not None else ""
    if owner is not None:
        write_shelf_demand(profile_root, owner)
    try:
        if capacity_expander is not None:
            capacity = capacity_expander(max(capacity, len(read_shelf_demands(profile_root))))
            if capacity <= 0:
                raise ValueError("CRIU shelf capacity expander returned a non-positive capacity")
    except BaseException:
        if demand_attempt:
            clear_shelf_demand(profile_root, demand_attempt)
        raise
    path_groups = _shelf_admission_lock_groups(profile_root, capacity)
    lock_groups: list[list[TextIO]] = []
    try:
        for paths in path_groups:
            locks: list[TextIO] = []
            lock_groups.append(locks)
            for path in paths:
                locks.append(path.open("a+", encoding="utf-8"))
    except BaseException:
        for locks in lock_groups:
            for lock in locks:
                lock.close()
        if demand_attempt:
            clear_shelf_demand(profile_root, demand_attempt)
        raise
    acquired_locks: list[TextIO] | None = None
    acquired_slot = 0
    contended = False
    try:
        try:
            while acquired_locks is None:
                for slot, locks in enumerate(lock_groups, start=1):
                    if not _try_lock_group(locks):
                        continue
                    acquired_locks = locks
                    acquired_slot = slot
                    break
                if acquired_locks is not None:
                    break
                if not contended:
                    contended = True
                    if on_contention is not None:
                        on_contention()
                if not wait:
                    raise ShelfAdmissionUnavailable(f"no immediate admission slot under {profile_root}")
                time.sleep(0.05)
        except KeyboardInterrupt:
            raise ShelfAdmissionInterrupted from None
        acquired = time.monotonic()
        admission = ShelfAdmission(
            contended=contended,
            wait_seconds=max(0.0, acquired - started) if contended else 0.0,
            slot=acquired_slot,
            capacity=capacity,
        )
        admitted_owner = None
        try:
            if owner is not None:
                admitted_owner = {**owner, "admission_slot": acquired_slot, "admission_capacity": capacity}
                write_shelf_owner(profile_root, admitted_owner)
            yield admission
        finally:
            if admitted_owner is not None:
                clear_shelf_owner(profile_root, str(admitted_owner.get("attempt_id") or ""))
            if acquired_locks is not None:
                _unlock_group(acquired_locks)
    finally:
        for locks in lock_groups:
            for lock in locks:
                lock.close()
        if demand_attempt:
            clear_shelf_demand(profile_root, demand_attempt)
