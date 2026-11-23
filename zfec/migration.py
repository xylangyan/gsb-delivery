"""
Plan a safe retirement of disks that hold zfec share files.

Manifest
--------
The input is JSON with four required arrays and one optional array::

    {
      "versions": [
        {"name": "obj-1", "original_length": 12345, "k": 3, "m": 8}
      ],
      "disks": [
        {"name": "old-a", "available_bytes": 0},
        {"name": "spare", "available_bytes": 500000,
         "writable": true, "retired": false, "scratch": true}
      ],
      "shares": [
        {"version": "obj-1", "number": 0, "disk": "old-a", "size": 4123}
      ],
      "failures": [
        {"name": "old-a dies", "lost_disks": ["old-a"]}
      ],
      "scratch_disks": ["spare"]
    }

``available_bytes`` is the space currently free for *new* share files.  It does
not include the bytes of shares already listed.  A share ``size`` is the actual
on-disk file size, including the zfec metadata header.

``retired`` (default true) means the disk must end with no share copies.
``writable`` (default true) controls whether new copies may be written there.
``scratch`` disks are usable during the move but must be empty at the end.

Every failure scenario lists disks lost simultaneously.  After every step, and
under every scenario, each version must retain at least ``k`` distinct share
numbers.  Copies with the same number on different disks count once.

By default every share number present in the input is preserved: a source copy
may only be deleted after another verified copy of the same version and number
exists.  With ``--allow-drop`` (or ``"preserve_all": false``), surplus copies
may instead be discarded, but never below the ``k`` distinct-number safety
floor.

The plan minimizes bytes actually written.  The search is exact for the states
it explores (A* with an admissible lower bound), subject to ``max_states``.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import sys
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple


class ManifestError(ValueError):
    """The manifest is malformed or internally inconsistent."""


class PlanningError(RuntimeError):
    """No safe plan exists for the supplied manifest."""

    def __init__(self, message: str, diagnostics: Optional["Diagnostics"] = None):
        super().__init__(message)
        self.diagnostics = diagnostics or Diagnostics()


# ---------------------------------------------------------------------------
# Public data model


@dataclass(frozen=True)
class Version:
    name: str
    original_length: int
    k: int
    m: int


@dataclass(frozen=True)
class Share:
    version: str
    number: int
    disk: str
    size: Optional[int]

    @property
    def identity(self) -> Tuple[str, int]:
        return (self.version, self.number)


@dataclass(frozen=True)
class Disk:
    name: str
    available_bytes: int
    writable: bool = True
    retired: bool = True
    scratch: bool = False

    @property
    def must_empty(self) -> bool:
        return self.retired or self.scratch

    @property
    def keep(self) -> bool:
        """True when final share copies may remain here."""
        return not self.must_empty


@dataclass(frozen=True)
class Failure:
    name: str
    lost_disks: FrozenSet[str]


@dataclass
class Manifest:
    versions: Dict[str, Version]
    disks: Dict[str, Disk]
    shares: List[Share]
    failures: List[Failure]
    preserve_all: bool = True
    scratch_disks: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Action:
    kind: str  # "copy" or "delete"
    version: str
    number: int
    source: str
    destination: Optional[str]
    size: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "kind": self.kind,
            "version": self.version,
            "number": self.number,
            "source": self.source,
            "destination": self.destination,
            "bytes_written": self.size if self.kind == "copy" else 0,
        }


@dataclass
class Step:
    action: Action
    disk: str
    free_before: int
    free_after: int
    managed_bytes_after: int
    remaining: Dict[str, int]
    remaining_by_failure: Dict[str, Dict[str, int]]
    copies: Dict[Tuple[str, int], FrozenSet[str]]
    verified_copies: FrozenSet[str]

    def to_dict(self, index: int) -> Dict[str, object]:
        return {
            "index": index,
            "action": self.action.to_dict(),
            "disk": self.disk,
            "free_before": self.free_before,
            "free_after": self.free_after,
            "managed_share_bytes_after": self.managed_bytes_after,
            "remaining_distinct_shares": dict(sorted(self.remaining.items())),
            "remaining_distinct_shares_by_failure": {
                name: dict(sorted(counts.items()))
                for name, counts in sorted(self.remaining_by_failure.items())
            },
            "copies": {
                "%s/%d" % identity: sorted(disks)
                for identity, disks in sorted(self.copies.items())
            },
            "verified_copies": sorted(self.verified_copies),
        }


@dataclass
class Plan:
    steps: List[Step]
    total_bytes_written: int
    initial_free: Dict[str, int]
    final_free: Dict[str, int]
    preserve_all: bool = True

    def to_dict(self) -> Dict[str, object]:
        return {
            "feasible": True,
            "policy": "preserve_all" if self.preserve_all else "keep_k",
            "total_bytes_written": self.total_bytes_written,
            "initial_free": dict(sorted(self.initial_free.items())),
            "final_free": dict(sorted(self.final_free.items())),
            "steps": [step.to_dict(i) for i, step in enumerate(self.steps, 1)],
        }


@dataclass
class Diagnostics:
    """Concrete reasons a manifest cannot be migrated."""

    reasons: List[str] = field(default_factory=list)
    blocked_versions: List[str] = field(default_factory=list)
    capacity_conflicts: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        return {
            "feasible": False,
            "reasons": list(self.reasons),
            "blocked_versions": sorted(set(self.blocked_versions)),
            "capacity_conflicts": list(self.capacity_conflicts),
        }


# ---------------------------------------------------------------------------
# Parsing


def _as_int(value, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError("%s must be an integer, got %r" % (where, value))
    return value


def _as_bool(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise ManifestError("%s must be a boolean, got %r" % (where, value))
    return value


def _first(mapping: Mapping[str, object], names: Sequence[str]):
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _name(raw: Mapping[str, object], where: str, key: str = "name") -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ManifestError("%s.%s must be a non-empty string" % (where, key))
    return value


def _disks_list(raw: Mapping[str, object], where: str) -> List[str]:
    value = _first(raw, ("lost_disks", "disks", "loses", "lost"))
    if not isinstance(value, list) or not all(isinstance(d, str) for d in value):
        raise ManifestError("%s.lost_disks must be a list of disk names" % where)
    return value


def _normalize_shares_raw(raw_shares) -> List[Mapping[str, object]]:
    """Accept a list of share objects, or version-keyed maps/lists."""
    if isinstance(raw_shares, list):
        return [s for s in raw_shares if isinstance(s, Mapping)]
    if not isinstance(raw_shares, Mapping):
        raise ManifestError("manifest.shares must be a list or an object")
    normalized: List[Mapping[str, object]] = []
    for version_name, entries in raw_shares.items():
        if isinstance(entries, Mapping):
            entries = [
                dict(value, number=key) if isinstance(value, Mapping) else value
                for key, value in entries.items()
            ]
        if not isinstance(entries, list):
            raise ManifestError(
                "manifest.shares[%r] must be a list of share objects" % version_name
            )
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ManifestError(
                    "manifest.shares[%r] contains a non-object entry" % version_name
                )
            copied = dict(entry)
            copied.setdefault("version", version_name)
            normalized.append(copied)
    return normalized


def _normalize_versions_raw(raw_versions) -> List[Mapping[str, object]]:
    if isinstance(raw_versions, list):
        return [v for v in raw_versions if isinstance(v, Mapping)]
    if isinstance(raw_versions, Mapping):
        out: List[Mapping[str, object]] = []
        for name, entry in raw_versions.items():
            if not isinstance(entry, Mapping):
                raise ManifestError("manifest.versions[%r] must be an object" % name)
            copied = dict(entry)
            copied.setdefault("name", name)
            out.append(copied)
        return out
    raise ManifestError("manifest.versions must be a list or an object")


def _normalize_disks_raw(raw_disks) -> List[Mapping[str, object]]:
    if isinstance(raw_disks, list):
        return [d for d in raw_disks if isinstance(d, Mapping)]
    if isinstance(raw_disks, Mapping):
        out: List[Mapping[str, object]] = []
        for name, entry in raw_disks.items():
            if not isinstance(entry, Mapping):
                raise ManifestError("manifest.disks[%r] must be an object" % name)
            copied = dict(entry)
            copied.setdefault("name", name)
            out.append(copied)
        return out
    raise ManifestError("manifest.disks must be a list or an object")


def _normalize_failures_raw(raw_failures) -> List[Mapping[str, object]]:
    if isinstance(raw_failures, list):
        return [f for f in raw_failures if isinstance(f, Mapping)]
    if isinstance(raw_failures, Mapping):
        out: List[Mapping[str, object]] = []
        for name, entry in raw_failures.items():
            if isinstance(entry, list):
                out.append({"name": name, "lost_disks": entry})
            elif isinstance(entry, Mapping):
                copied = dict(entry)
                copied.setdefault("name", name)
                out.append(copied)
            else:
                raise ManifestError(
                    "manifest.failures[%r] must be a list or object" % name
                )
        return out
    raise ManifestError("manifest.failures must be a list or an object")


def manifest_from_dict(data: Mapping[str, object]) -> Manifest:
    if not isinstance(data, Mapping):
        raise ManifestError("manifest must be a JSON object")

    raw_versions = _normalize_versions_raw(data.get("versions"))
    raw_disks = _normalize_disks_raw(data.get("disks"))
    raw_shares = _normalize_shares_raw(data.get("shares"))
    raw_failures = _normalize_failures_raw(data.get("failures"))

    if not raw_versions:
        raise ManifestError("manifest.versions must not be empty")
    if not raw_disks:
        raise ManifestError("manifest.disks must not be empty")

    versions: Dict[str, Version] = {}
    for i, raw in enumerate(raw_versions):
        where = "versions[%d]" % i
        name = raw.get("name", raw.get("version", raw.get("id")))
        if not isinstance(name, str) or not name:
            raise ManifestError("%s.name must be a non-empty string" % where)
        if name in versions:
            raise ManifestError("duplicate version %r" % name)
        length = _as_int(
            _first(raw, ("original_length", "original_bytes", "length", "object_size")),
            where + ".original_length",
        )
        k = _as_int(raw.get("k"), where + ".k")
        m = _as_int(raw.get("m"), where + ".m")
        if length < 0:
            raise ManifestError("%s.original_length must be >= 0" % where)
        if not 1 <= k <= m <= 256:
            raise ManifestError("%s must satisfy 1 <= k <= m <= 256" % where)
        versions[name] = Version(name=name, original_length=length, k=k, m=m)

    preserve_all = True
    if "preserve_all" in data:
        preserve_all = _as_bool(data["preserve_all"], "manifest.preserve_all")
    if "allow_drop_shares" in data:
        preserve_all = not _as_bool(
            data["allow_drop_shares"], "manifest.allow_drop_shares"
        )
    if "policy" in data:
        policy = data["policy"]
        if policy not in ("preserve_all", "keep_k"):
            raise ManifestError(
                "manifest.policy must be 'preserve_all' or 'keep_k'"
            )
        preserve_all = policy == "preserve_all"

    raw_scratch = data.get("scratch_disks", [])
    if not isinstance(raw_scratch, list) or not all(
        isinstance(d, str) for d in raw_scratch
    ):
        raise ManifestError("scratch_disks must be a list of disk names")
    if len(set(raw_scratch)) != len(raw_scratch):
        raise ManifestError("scratch_disks contains duplicate names")
    named_scratch: Set[str] = set(raw_scratch)

    disks: Dict[str, Disk] = {}
    for i, raw in enumerate(raw_disks):
        where = "disks[%d]" % i
        name = raw.get("name", raw.get("disk", raw.get("id")))
        if not isinstance(name, str) or not name:
            raise ManifestError("%s.name must be a non-empty string" % where)
        if name in disks:
            raise ManifestError("duplicate disk %r" % name)

        available_value = _first(
            raw, ("available_bytes", "available", "free_bytes", "free")
        )
        capacity_value = _first(raw, ("capacity", "total_bytes", "size_bytes"))
        used_value = _first(raw, ("used_bytes", "used"))
        if available_value is not None:
            available = _as_int(available_value, where + ".available_bytes")
        elif capacity_value is not None:
            capacity = _as_int(capacity_value, where + ".capacity")
            if used_value is not None:
                used = _as_int(used_value, where + ".used_bytes")
                if used > capacity:
                    raise ManifestError("%s.used_bytes exceeds capacity" % where)
                available = capacity - used
            else:
                available = capacity
        else:
            raise ManifestError("%s needs available_bytes or capacity" % where)
        if available < 0:
            raise ManifestError("%s available bytes must be >= 0" % where)

        writable_raw = _first(
            raw, ("writable", "write_allowed", "allowed", "can_write")
        )
        writable = True if writable_raw is None else _as_bool(
            writable_raw, where + ".writable"
        )
        role = raw.get("role")
        retired_raw = _first(
            raw, ("retired", "retire", "must_vacate", "decommission")
        )
        if retired_raw is not None:
            retired = _as_bool(retired_raw, where + ".retired")
        elif role in ("keep", "retain", "destination", "safe"):
            retired = False
        else:
            retired = True
        if role in ("scratch", "spare", "staging", "transit"):
            named_scratch.add(name)
        scratch_raw = raw.get("scratch", False)
        scratch = _as_bool(scratch_raw, where + ".scratch") or name in named_scratch
        if not writable and role in ("scratch", "spare"):
            raise ManifestError("scratch disk %r must be writable" % name)
        disks[name] = Disk(
            name=name,
            available_bytes=available,
            writable=writable,
            retired=retired,
            scratch=scratch,
        )

    unknown = sorted(named_scratch - set(disks))
    if unknown:
        raise ManifestError(
            "scratch_disks names unknown disks: %s" % ", ".join(unknown)
        )

    shares: List[Share] = []
    seen: Set[Tuple[str, int, str]] = set()
    for i, raw in enumerate(raw_shares):
        where = "shares[%d]" % i
        version_name = raw.get(
            "version", raw.get("version_name", raw.get("object"))
        )
        if isinstance(version_name, Mapping):
            version_name = version_name.get("name")
        if not isinstance(version_name, str) or version_name not in versions:
            raise ManifestError(
                "%s.version %r is not declared in versions"
                % (where, version_name)
            )
        number_raw = raw.get(
            "number",
            raw.get(
                "index",
                raw.get("shnum", raw.get("share_number", raw.get("share"))),
            ),
        )
        number = _as_int(number_raw, where + ".number")
        version = versions[version_name]
        if not 0 <= number < version.m:
            raise ManifestError(
                "%s.number %d is outside 0..%d for version %r"
                % (where, number, version.m - 1, version_name)
            )
        disk_name = raw.get("disk", raw.get("disk_name", raw.get("on_disk")))
        if not isinstance(disk_name, str) or disk_name not in disks:
            raise ManifestError(
                "%s.disk %r is not declared in disks" % (where, disk_name)
            )
        raw_size = _first(
            raw,
            (
                "size",
                "bytes",
                "actual_size",
                "on_disk_bytes",
                "file_bytes",
                "length",
            ),
        )
        size = None
        if raw_size is not None:
            size = _as_int(raw_size, where + ".size")
            if size < 0:
                raise ManifestError("%s.size must be >= 0" % where)
        key = (version_name, number, disk_name)
        if key in seen:
            raise ManifestError(
                "duplicate copy of version %r share %d on disk %r"
                % (version_name, number, disk_name)
            )
        seen.add(key)
        shares.append(
            Share(version=version_name, number=number, disk=disk_name, size=size)
        )

    failures: List[Failure] = []
    failure_names: Set[str] = set()
    for i, raw in enumerate(raw_failures):
        where = "failures[%d]" % i
        name = _name(raw, where)
        if name in failure_names:
            raise ManifestError("duplicate failure scenario %r" % name)
        failure_names.add(name)
        lost = _disks_list(raw, where)
        unknown = sorted(set(lost) - set(disks))
        if unknown:
            raise ManifestError(
                "%s.lost_disks names unknown disks: %s"
                % (where, ", ".join(unknown))
            )
        failures.append(Failure(name=name, lost_disks=frozenset(lost)))

    return Manifest(
        versions=versions,
        disks=disks,
        shares=shares,
        failures=failures,
        preserve_all=preserve_all,
        scratch_disks=tuple(sorted(named_scratch)),
    )


def load_manifest(path: str) -> Manifest:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise ManifestError("cannot read manifest %s: %s" % (path, e))
    except json.JSONDecodeError as e:
        raise ManifestError("manifest %s is not valid JSON: %s" % (path, e))
    return manifest_from_dict(data)


# ---------------------------------------------------------------------------
# Normalized problem


def _normalize_share_sizes(manifest: Manifest) -> Dict[Tuple[str, int], int]:
    """Resolve exactly one on-disk size per (version, number).

    A share file for a given version and number is the same file on every disk.
    Declared sizes must therefore agree.  A copy may omit its size when another
    copy of the same identity declares it.
    """
    sizes: Dict[Tuple[str, int], int] = {}
    for share in manifest.shares:
        if share.size is None:
            continue
        key = share.identity
        previous = sizes.get(key)
        if previous is not None and previous != share.size:
            raise ManifestError(
                "copies of version %r share %d disagree on size (%d vs %d)"
                % (share.version, share.number, previous, share.size)
            )
        sizes[key] = share.size

    missing = sorted(
        {share.identity for share in manifest.shares if share.size is None}
        - set(sizes)
    )
    if missing:
        rendered = ", ".join("%s/%d" % identity for identity in missing)
        raise ManifestError(
            "no size declared for share identity/copies: %s; at least one copy "
            "of each identity must give its actual on-disk bytes (header included)"
            % rendered
        )
    return sizes


@dataclass(frozen=True)
class _State:
    """copies: identity -> disks; free: disk -> available bytes."""

    copies: Tuple[Tuple[Tuple[str, int], Tuple[str, ...]], ...]
    free: Tuple[Tuple[str, int], ...]

    def copy_map(self) -> Dict[Tuple[str, int], FrozenSet[str]]:
        return {identity: frozenset(disks) for identity, disks in self.copies}

    def free_map(self) -> Dict[str, int]:
        return dict(self.free)


def _state_from(
    copies: Mapping[Tuple[str, int], Set[str]], free: Mapping[str, int]
) -> _State:
    return _State(
        copies=tuple(
            (identity, tuple(sorted(disks)))
            for identity, disks in sorted(copies.items())
            if disks
        ),
        free=tuple(sorted(free.items())),
    )


def _initial_state(
    manifest: Manifest, sizes: Mapping[Tuple[str, int], int]
) -> _State:
    copies: Dict[Tuple[str, int], Set[str]] = {}
    for share in manifest.shares:
        copies.setdefault(share.identity, set()).add(share.disk)
    free = {name: disk.available_bytes for name, disk in manifest.disks.items()}
    return _state_from(copies, free)


def _managed_bytes(
    state: _State, sizes: Mapping[Tuple[str, int], int]
) -> Dict[str, int]:
    used = {disk: 0 for disk in state.free_map()}
    for identity, disks in state.copies:
        for disk in disks:
            used[disk] += sizes[identity]
    return used


def _numbers_by_version(
    state: _State, lost: FrozenSet[str] = frozenset()
) -> Dict[str, Set[int]]:
    result: Dict[str, Set[int]] = {}
    for (version, number), disks in state.copies:
        if not (set(disks) & lost):
            result.setdefault(version, set()).add(number)
    return result


def _violations(manifest: Manifest, state: _State) -> List[str]:
    problems: List[str] = []

    def check(label: str, lost: FrozenSet[str]) -> None:
        numbers = _numbers_by_version(state, lost)
        for version_name, version in manifest.versions.items():
            count = len(numbers.get(version_name, set()))
            if count < version.k:
                problems.append(
                    "%s leaves version %r with %d distinct number(s), needs k=%d"
                    % (label, version_name, count, version.k)
                )

    check("the current state", frozenset())
    for failure in manifest.failures:
        check("failure %r" % failure.name, failure.lost_disks)
    return problems


def _safe(manifest: Manifest, state: _State) -> bool:
    return not _violations(manifest, state)


def _initial_identities(manifest: Manifest) -> Set[Tuple[str, int]]:
    return {(share.version, share.number) for share in manifest.shares}


def _is_goal(manifest: Manifest, state: _State) -> bool:
    for _identity, disks in state.copies:
        for disk in disks:
            if manifest.disks[disk].must_empty:
                return False
    if manifest.preserve_all:
        if not _initial_identities(manifest).issubset(state.copy_map()):
            return False
    return _safe(manifest, state)


def _identity_needs_keep_copy(
    manifest: Manifest, state: _State
) -> Set[Tuple[str, int]]:
    """Identities with no copy on a disk that may retain shares at the end."""
    needs: Set[Tuple[str, int]] = set()
    for identity, disks in state.copies:
        if not any(manifest.disks[disk].keep for disk in disks):
            needs.add(identity)
    return needs


# ---------------------------------------------------------------------------
# Actions


@dataclass(frozen=True)
class _Transition:
    action: Action
    after: _State
    cost: int


def _copy_candidates(
    manifest: Manifest, state: _State, sizes: Mapping[Tuple[str, int], int]
) -> Iterable[_Transition]:
    """Copies that can matter for a minimum-write plan.

    A copy is useful only when its source is a disk that must be vacated.
    Copying between two keep disks, or off a scratch disk that was staged for
    a later keep move, only adds writes.  Restricting sources this way removes
    an enormous number of symmetric states while preserving optimality.
    """
    free = state.free_map()
    copies = state.copy_map()
    for identity in sorted(copies):
        version_name, number = identity
        size = sizes[identity]
        disks_holding = copies[identity]
        # One keep copy is enough for every policy.  Any plan with two keep
        # copies of the same identity can drop one (a free delete) without
        # weakening safety, so restricting to one never raises the optimum.
        if any(manifest.disks[disk].keep for disk in disks_holding):
            continue
        # Likewise, two scratch copies of the same identity are useless.
        has_scratch_copy = any(manifest.disks[disk].scratch for disk in disks_holding)
        for source in sorted(disks_holding):
            if not manifest.disks[source].must_empty:
                continue
            seen_destination_kinds: Set[Tuple[object, ...]] = set()
            for destination in sorted(manifest.disks):
                disk = manifest.disks[destination]
                if destination == source or destination in disks_holding:
                    continue
                if not disk.writable:
                    continue
                if disk.scratch and has_scratch_copy:
                    continue
                if free[destination] < size:
                    continue
                # Disks with the same role, same free-space bucket for this
                # file, and membership in the same failure scenarios are
                # interchangeable.  Trying one representative preserves
                # optimality and avoids a factorial destination choice.
                signature = (
                    disk.keep,
                    disk.scratch,
                    disk.must_empty,
                    free[destination],
                    tuple(
                        sorted(
                            failure.name
                            for failure in manifest.failures
                            if destination in failure.lost_disks
                        )
                    ),
                )
                if signature in seen_destination_kinds:
                    continue
                seen_destination_kinds.add(signature)
                new_copies = {key: set(disks) for key, disks in copies.items()}
                new_copies[identity].add(destination)
                new_free = dict(free)
                new_free[destination] -= size
                after = _state_from(new_copies, new_free)
                if _safe(manifest, after):
                    yield _Transition(
                        action=Action(
                            kind="copy",
                            version=version_name,
                            number=number,
                            source=source,
                            destination=destination,
                            size=size,
                        ),
                        after=after,
                        cost=size,
                    )


def _delete_candidates(
    manifest: Manifest, state: _State, sizes: Mapping[Tuple[str, int], int]
) -> Iterable[_Transition]:
    free = state.free_map()
    copies = state.copy_map()
    for identity in sorted(copies):
        version_name, number = identity
        current = copies[identity]
        must_keep_copy = manifest.preserve_all or number in _required_number_set(
            manifest, state, version_name
        )
        if must_keep_copy and len(current) < 2:
            # Every removed source copy must leave a verified same-version,
            # same-number copy behind.  In preserve_all mode that is required
            # of every identity; otherwise it is required for the k numbers
            # that make the version safe.
            continue
        for disk in sorted(current):
            # Removing a copy from a keep disk can never help the final state
            # and may needlessly destroy a verified copy; only retiring or
            # scratch disks need deletes.  (Allowing keep-disk deletes would
            # only add equivalent states.)
            if not manifest.disks[disk].must_empty:
                continue
            new_copies = {key: set(disks) for key, disks in copies.items()}
            new_copies[identity].discard(disk)
            if not new_copies[identity]:
                del new_copies[identity]
            new_free = dict(free)
            new_free[disk] += sizes[identity]
            after = _state_from(new_copies, new_free)
            if _safe(manifest, after):
                yield _Transition(
                    action=Action(
                        kind="delete",
                        version=version_name,
                        number=number,
                        source=disk,
                        destination=None,
                        size=sizes[identity],
                    ),
                    after=after,
                    cost=0,
                )


def _required_number_set(
    manifest: Manifest, state: _State, version_name: str
) -> Set[int]:
    """Numbers of a version that must survive on a keep disk, if identifiable.

    This is used only to decide whether a delete may remove the *last* copy of
    a number.  Deleting a copy when another copy exists is always allowed in
    keep_k mode; deleting the last copy is allowed unless the number is needed
    to reach k distinct numbers on keep disks.
    """
    copy_map = state.copy_map()
    on_keep = {
        number
        for (version, number), disks in state.copies
        if version == version_name and any(manifest.disks[d].keep for d in disks)
    }
    version = manifest.versions[version_name]
    if len(on_keep) >= version.k:
        return on_keep
    # Required set is not unique; be conservative and forbid dropping any
    # identity that currently lacks a keep copy, because it may be needed.
    return {
        number
        for (version, number), disks in state.copies
        if version == version_name
        and not any(manifest.disks[d].keep for d in disks)
    }


def _space_relief_candidates(
    manifest: Manifest, state: _State, sizes: Mapping[Tuple[str, int], int]
) -> Iterable[_Transition]:
    """Safe keep-disk deletes that unblock a copy which cannot otherwise fit.

    Only relevant when surplus copies may be discarded (keep-k policy).  A keep
    disk can be full of numbers that are not needed for the k-number floor,
    while a number stranded on an old disk needs a slot.  Dropping such a
    number is required for any plan to exist; without this branch the search
    would report a false infeasibility.
    """
    if manifest.preserve_all:
        return

    free = state.free_map()
    copies = state.copy_map()

    blocked: List[Tuple[str, int]] = []
    for identity, disks in state.copies:
        if any(manifest.disks[d].keep for d in disks):
            continue
        size = sizes[identity]
        fits = any(
            disk.writable
            and (disk.keep or disk.scratch)
            and destination not in disks
            and free[destination] >= size
            for destination, disk in manifest.disks.items()
        )
        if not fits:
            blocked.append(identity)
    if not blocked:
        return

    for identity in sorted(copies):
        for disk in sorted(copies[identity]):
            if not manifest.disks[disk].keep:
                continue
            new_copies = {key: set(disks) for key, disks in copies.items()}
            new_copies[identity].discard(disk)
            if not new_copies[identity]:
                del new_copies[identity]
            new_free = dict(free)
            new_free[disk] += sizes[identity]
            after = _state_from(new_copies, new_free)
            if not _safe(manifest, after):
                continue
            if any(
                new_free.get(destination, 0) >= sizes[need]
                for need in blocked
                for destination, target in manifest.disks.items()
                if target.writable
                and (target.keep or target.scratch)
                and destination not in after.copy_map().get(need, frozenset())
            ):
                yield _Transition(
                    action=Action(
                        kind="delete",
                        version=identity[0],
                        number=identity[1],
                        source=disk,
                        destination=None,
                        size=sizes[identity],
                    ),
                    after=after,
                    cost=0,
                )


def _close_deletes(
    manifest: Manifest, state: _State, sizes: Mapping[Tuple[str, int], int]
) -> Tuple[_State, Tuple[Action, ...]]:
    """Apply free deletes whose identity already has a final keep copy.

    Such a source copy is redundant for every future step: the keep copy
    survives, free space only increases, and later copies cannot be harmed.
    Deletes for identities that have *no* keep copy are deliberately left to
    the search: they may be staging steps that must survive until the number
    reaches a keep disk.  Auto-deleting those would destroy valid orderings
    (for example, a share parked on a freed old disk while its destination is
    made ready).
    """
    actions: List[Action] = []
    current = state
    while True:
        chosen = None
        copy_map = current.copy_map()
        for transition in _delete_candidates(manifest, current, sizes):
            action = transition.action
            identity = (action.version, action.number)
            has_keep_copy = any(
                manifest.disks[disk].keep for disk in copy_map.get(identity, ())
            )
            if has_keep_copy:
                chosen = transition
                break
        if chosen is None:
            return current, tuple(actions)
        actions.append(chosen.action)
        current = chosen.after


def _heuristic(
    manifest: Manifest, state: _State, sizes: Mapping[Tuple[str, int], int]
) -> int:
    """Admissible lower bound on bytes still to write.

    Each identity that currently has no copy on a keep disk must be written at
    least once more (a copy onto an old/scratch disk is also a write).  When
    only k numbers are required, choose the k cheapest per version; this is
    still a lower bound on the actual work.
    """
    by_version: Dict[str, List[int]] = {}
    for identity, disks in state.copies:
        if any(manifest.disks[disk].keep for disk in disks):
            continue
        version_name, _number = identity
        by_version.setdefault(version_name, []).append(sizes[identity])

    total = 0
    for version_name, version in manifest.versions.items():
        costs = sorted(by_version.get(version_name, []))
        if not costs:
            continue
        if manifest.preserve_all:
            total += sum(costs)
        else:
            # Need enough distinct numbers on keep disks.  Count how many
            # numbers already live on keep disks and cover the rest as cheaply
            # as possible; identities that must move are exactly those with no
            # keep copy.
            on_keep = {
                number
                for (version, number), disks in state.copies
                if version == version_name
                and any(manifest.disks[d].keep for d in disks)
            }
            need = max(0, version.k - len(on_keep))
            total += sum(costs[:need])
    return total


# ---------------------------------------------------------------------------
# Search


def _state_after_actions(
    manifest: Manifest,
    sizes: Mapping[Tuple[str, int], int],
    initial: _State,
    initial_deletes: Sequence[Action],
    actions: Sequence[Action],
) -> _State:
    state = initial
    for action in tuple(initial_deletes) + tuple(actions):
        state = _apply(manifest, state, action)
    return state


def _fast_plan(
    manifest: Manifest, sizes: Mapping[Tuple[str, int], int], initial: _State
) -> Optional[List[Action]]:
    """Construct the trivial optimum when all required copies fit at once.

    The lower bound is one write for every identity that has no keep-disk copy.
    If all of those copies can be placed on keep disks simultaneously, then
    copying them all first and deleting every old/scratch copy afterwards is
    safe (adding copies never weakens safety) and achieves the lower bound, so
    it is optimal.  This turns the common "plenty of destination space" case
    into a linear-time plan, including large m.
    """
    copies = initial.copy_map()
    free = initial.free_map()
    stranded = [
        identity
        for identity, disks in copies.items()
        if not any(manifest.disks[disk].keep for disk in disks)
    ]
    if manifest.preserve_all:
        required = stranded
    else:
        # Only enough stranded numbers per version to reach k on keep disks,
        # choosing the cheapest.  Extra numbers may be dropped for free.
        required = []
        for version_name, version in manifest.versions.items():
            on_keep = {
                number
                for (name, number), disks in copies.items()
                if name == version_name
                and any(manifest.disks[disk].keep for disk in disks)
            }
            need = max(0, version.k - len(on_keep))
            candidates = sorted(
                (
                    identity
                    for identity in stranded
                    if identity[0] == version_name
                ),
                key=lambda identity: (sizes[identity], identity),
            )
            if len(candidates) < need:
                return None
            required.extend(candidates[:need])
    keep_disks = sorted(
        (
            disk
            for disk in manifest.disks.values()
            if disk.writable and disk.keep
        ),
        key=lambda disk: (-free[disk.name], disk.name),
    )
    if sum(sizes[i] for i in required) > sum(free[d.name] for d in keep_disks):
        return None

    assignment: Dict[Tuple[str, int], str] = {}
    for identity in sorted(required, key=lambda i: (-sizes[i], i)):
        size = sizes[identity]
        for disk in keep_disks:
            if free[disk.name] >= size:
                assignment[identity] = disk.name
                free[disk.name] -= size
                break
        else:
            return None

    # Emit copy-then-delete pairs, keeping the ordinary operational invariant
    # that a source is removed only after a verified copy exists elsewhere.
    actions: List[Action] = []
    remaining = {identity: set(disks) for identity, disks in copies.items()}
    for identity in sorted(assignment):
        destination = assignment[identity]
        source = next(
            (
                disk
                for disk in sorted(remaining[identity])
                if manifest.disks[disk].must_empty
            ),
            None,
        )
        if source is None:
            return None
        actions.append(
            Action(
                kind="copy",
                version=identity[0],
                number=identity[1],
                source=source,
                destination=destination,
                size=sizes[identity],
            )
        )
        remaining[identity].add(destination)
        actions.append(
            Action(
                kind="delete",
                version=identity[0],
                number=identity[1],
                source=source,
                destination=None,
                size=sizes[identity],
            )
        )
        remaining[identity].discard(source)

    for identity in sorted(copies):
        for disk in sorted(copies[identity]):
            if not manifest.disks[disk].must_empty:
                continue
            if disk not in remaining[identity]:
                continue
            if not (remaining[identity] - {disk}):
                return None
            actions.append(
                Action(
                    kind="delete",
                    version=identity[0],
                    number=identity[1],
                    source=disk,
                    destination=None,
                    size=sizes[identity],
                )
            )
            remaining[identity].discard(disk)

    # Validate the constructed sequence before trusting it.  If any step is
    # unsafe or the final state is not a goal, fall back to the exact search.
    state = initial
    for action in actions:
        state = _apply(manifest, state, action)
        if not _safe(manifest, state):
            return None
    if not _is_goal(manifest, state):
        return None
    return actions


def plan(manifest: Manifest, max_states: int = 500_000) -> Plan:
    """Return a minimum-write safe plan, or raise :class:`PlanningError`.

    The search branches only on copies.  After each copy it closes every
    currently-safe delete.  This normalization is optimality-preserving: a
    delete is free, releases space, and cannot make a later copy unsafe, while
    adding copies cannot make a state unsafe.  Removing the delete branching
    collapses the factorial permutations that would otherwise dominate.
    """
    if max_states < 1:
        raise ValueError("max_states must be positive")
    sizes = _normalize_share_sizes(manifest)
    initial = _initial_state(manifest, sizes)

    pre = _initial_diagnostics(manifest, sizes, initial)
    if pre.reasons:
        raise PlanningError(
            "manifest cannot be migrated safely: " + "; ".join(pre.reasons), pre
        )

    original_initial = initial
    closed, initial_deletes = _close_deletes(manifest, initial, sizes)

    if _is_goal(manifest, closed):
        if initial_deletes:
            steps = _replay_actions(
                manifest, sizes, original_initial, [tuple(initial_deletes)]
            )
            return Plan(
                steps=steps,
                total_bytes_written=0,
                initial_free=original_initial.free_map(),
                final_free=closed.free_map(),
                preserve_all=manifest.preserve_all,
            )
        return Plan(
            steps=[],
            total_bytes_written=0,
            initial_free=original_initial.free_map(),
            final_free=original_initial.free_map(),
            preserve_all=manifest.preserve_all,
        )

    initial = closed
    lower_bound = _heuristic(manifest, initial, sizes)

    fast_actions = _fast_plan(manifest, sizes, initial)
    if fast_actions is not None:
        chunks: List[Tuple[Action, ...]] = []
        if initial_deletes:
            chunks.append(tuple(initial_deletes))
        if fast_actions:
            chunks.append(tuple(fast_actions))
        steps = _replay_actions(manifest, sizes, original_initial, chunks)
        return Plan(
            steps=steps,
            total_bytes_written=sum(
                action.size for action in fast_actions if action.kind == "copy"
            ),
            initial_free=original_initial.free_map(),
            final_free=_state_after_actions(
                manifest, sizes, original_initial, initial_deletes, fast_actions
            ).free_map(),
            preserve_all=manifest.preserve_all,
        )
    counter = itertools.count()
    heap: List[Tuple[int, int, int, int, _State]] = []
    heapq.heappush(heap, (lower_bound, 0, 0, next(counter), initial))
    best_cost: Dict[_State, int] = {initial: 0}
    if initial_deletes:
        parents: Dict[
            _State, Tuple[Optional[_State], Optional[Tuple[Action, ...]]]
        ] = {initial: (original_initial, tuple(initial_deletes))}
        parents.setdefault(original_initial, (None, None))
    else:
        parents = {initial: (None, None)}
    seen = 0
    truncated = False
    goal: Optional[_State] = None
    best_upper: Optional[int] = None

    while heap:
        f, cost, depth, _tie, state = heapq.heappop(heap)
        if cost != best_cost.get(state):
            continue
        if best_upper is not None and f >= best_upper:
            break
        seen += 1
        if seen > max_states:
            truncated = True
            break
        if _is_goal(manifest, state):
            best_upper = cost
            goal = state
            continue
        copy_map = state.copy_map()
        successors = list(_copy_candidates(manifest, state, sizes))
        for transition in _delete_candidates(manifest, state, sizes):
            identity = (transition.action.version, transition.action.number)
            if any(manifest.disks[d].keep for d in copy_map.get(identity, ())):
                # Already handled by the delete closure.
                continue
            successors.append(transition)
        successors.extend(_space_relief_candidates(manifest, state, sizes))
        for transition in successors:
            child, deletes = _close_deletes(manifest, transition.after, sizes)
            new_cost = cost + transition.cost
            previous = best_cost.get(child)
            if previous is not None and previous <= new_cost:
                continue
            best_cost[child] = new_cost
            actions = (transition.action,) + deletes
            parents[child] = (state, actions)
            if _is_goal(manifest, child):
                if best_upper is None or new_cost < best_upper:
                    best_upper = new_cost
                    goal = child
                    if best_upper == lower_bound:
                        return _finish_plan(
                            manifest,
                            sizes,
                            original_initial,
                            parents,
                            goal,
                            new_cost,
                        )
            heapq.heappush(
                heap,
                (
                    new_cost + _heuristic(manifest, child, sizes),
                    new_cost,
                    -(depth + 1),
                    next(counter),
                    child,
                ),
            )

    if truncated or goal is None:
        if truncated:
            raise PlanningError(
                "exact search exceeded %d states before proving optimality"
                % max_states,
                Diagnostics(
                    reasons=[
                        "the exact-search limit of %d states was reached; this "
                        "instance is too large to prove a minimum-write plan"
                        % max_states
                    ]
                ),
            )
        raise _diagnose_no_plan(manifest, sizes, initial, max_states)

    return _finish_plan(manifest, sizes, original_initial, parents, goal, best_upper)


def _finish_plan(
    manifest: Manifest,
    sizes: Mapping[Tuple[str, int], int],
    initial: _State,
    parents: Mapping[_State, Tuple[Optional[_State], Optional[Tuple[Action, ...]]]],
    goal: _State,
    total_cost: Optional[int],
) -> Plan:
    steps = _reconstruct(manifest, sizes, initial, parents, goal)
    return Plan(
        steps=steps,
        total_bytes_written=total_cost or 0,
        initial_free=initial.free_map(),
        final_free=goal.free_map(),
        preserve_all=manifest.preserve_all,
    )


def _apply(manifest: Manifest, state: _State, action: Action) -> _State:
    copies = state.copy_map()
    free = state.free_map()
    identity = (action.version, action.number)
    if action.kind == "copy":
        assert action.destination is not None
        copies[identity] = copies[identity] | {action.destination}
        free[action.destination] -= action.size
    elif action.kind == "delete":
        remaining = copies[identity] - {action.source}
        if remaining:
            copies[identity] = remaining
        else:
            del copies[identity]
        free[action.source] += action.size
    else:
        raise AssertionError("unknown action %r" % (action.kind,))
    return _state_from(copies, free)


def _reconstruct(
    manifest: Manifest,
    sizes: Mapping[Tuple[str, int], int],
    initial: _State,
    parents: Mapping[_State, Tuple[Optional[_State], Optional[Tuple[Action, ...]]]],
    goal: _State,
) -> List[Step]:
    chain: List[Tuple[Action, ...]] = []
    state = goal
    while True:
        previous, actions = parents[state]
        if previous is None:
            break
        assert actions is not None
        chain.append(actions)
        state = previous
    chain.reverse()

    return _replay_actions(manifest, sizes, initial, chain)


def _replay_actions(
    manifest: Manifest,
    sizes: Mapping[Tuple[str, int], int],
    initial: _State,
    chain: Sequence[Tuple[Action, ...]],
) -> List[Step]:
    current = initial
    steps: List[Step] = []
    for actions in chain:
        for action in actions:
            free_before = current.free_map()
            after = _apply(manifest, current, action)
            free_after = after.free_map()
            disk = action.destination if action.kind == "copy" else action.source
            assert disk is not None
            copies_after = after.copy_map()
            remaining = {
                version: len(numbers)
                for version, numbers in sorted(_numbers_by_version(after).items())
            }
            for version in manifest.versions:
                remaining.setdefault(version, 0)
            by_failure: Dict[str, Dict[str, int]] = {}
            for failure in manifest.failures:
                counts = {
                    version: len(numbers)
                    for version, numbers in sorted(
                        _numbers_by_version(after, failure.lost_disks).items()
                    )
                }
                for version in manifest.versions:
                    counts.setdefault(version, 0)
                by_failure[failure.name] = counts
            if action.kind == "copy":
                verified = copies_after[(action.version, action.number)]
            else:
                verified = copies_after.get((action.version, action.number), frozenset())
            steps.append(
                Step(
                    action=action,
                    disk=disk,
                    free_before=free_before[disk],
                    free_after=free_after[disk],
                    managed_bytes_after=_managed_bytes(after, sizes)[disk],
                    remaining=remaining,
                    remaining_by_failure=by_failure,
                    copies=copies_after,
                    verified_copies=verified,
                )
            )
            current = after
    return steps


def verify_plan(manifest: Manifest, result: Plan) -> None:
    """Independently replay a plan and assert every invariant.

    Raises ``AssertionError`` with a useful message on the first violation.
    This is intended for tests and for auditing plans produced elsewhere.
    """
    sizes = _normalize_share_sizes(manifest)
    state = _initial_state(manifest, sizes)
    assert _safe(manifest, state), "initial state is already unsafe"
    written = 0
    for index, step in enumerate(result.steps, 1):
        action = step.action
        identity = (action.version, action.number)
        copies = state.copy_map()
        free = state.free_map()
        assert identity in copies, "step %d acts on a missing share" % index
        if action.kind == "copy":
            destination = action.destination
            assert destination is not None
            assert action.source in copies[identity], (
                "step %d copies from a disk without the share" % index
            )
            assert destination not in copies[identity], (
                "step %d destination already has the share" % index
            )
            assert manifest.disks[destination].writable, (
                "step %d writes to a non-writable disk" % index
            )
            assert free[destination] >= sizes[identity], (
                "step %d needs %d bytes on %s, only %d free"
                % (index, sizes[identity], destination, free[destination])
            )
            copies[identity] = set(copies[identity]) | {destination}
            free[destination] -= sizes[identity]
            written += sizes[identity]
        elif action.kind == "delete":
            source = action.source
            assert source in copies[identity], (
                "step %d deletes from a disk without the share" % index
            )
            if manifest.preserve_all:
                assert len(copies[identity]) >= 2, (
                    "step %d would delete the last copy of %r" % (index, identity)
                )
            copies[identity] = set(copies[identity]) - {source}
            if not copies[identity]:
                del copies[identity]
            free[source] += sizes[identity]
        else:
            raise AssertionError("step %d has unknown action %r" % (index, action.kind))
        state = _state_from(copies, free)
        problems = _violations(manifest, state)
        assert not problems, "step %d unsafe: %s" % (index, "; ".join(problems))
    assert _is_goal(manifest, state), "plan does not empty retired/scratch disks"
    assert written == result.total_bytes_written, (
        "declared write volume %d does not match replay %d"
        % (result.total_bytes_written, written)
    )


# ---------------------------------------------------------------------------
# Diagnostics


def _initial_diagnostics(
    manifest: Manifest,
    sizes: Mapping[Tuple[str, int], int],
    initial: _State,
) -> Diagnostics:
    diag = Diagnostics()

    for version_name, version in manifest.versions.items():
        numbers = {
            share.number
            for share in manifest.shares
            if share.version == version_name
        }
        if len(numbers) < version.k:
            diag.blocked_versions.append(version_name)
            diag.reasons.append(
                "version %r has only %d distinct share number(s) in the input, "
                "fewer than k=%d" % (version_name, len(numbers), version.k)
            )

    for failure in manifest.failures:
        numbers = _numbers_by_version(initial, failure.lost_disks)
        for version_name, version in manifest.versions.items():
            count = len(numbers.get(version_name, set()))
            if count < version.k:
                if version_name not in diag.blocked_versions:
                    diag.blocked_versions.append(version_name)
                diag.reasons.append(
                    "failure %r already leaves version %r with %d distinct "
                    "number(s), fewer than k=%d"
                    % (failure.name, version_name, count, version.k)
                )

    # Necessary capacity bound.  The final write set is exactly one copy of
    # each identity that has no keep-disk copy in a preserve-all plan, or k
    # numbers per version in a keep-k plan.  Qualify a size as "must write"
    # only when *every* existing copy of that identity sits on a disk that
    # must be emptied; that copy cannot remain.
    if manifest.preserve_all:
        required = 0
        details = []
        for identity in sorted(initial.copy_map()):
            disks = initial.copy_map()[identity]
            if any(manifest.disks[disk].keep for disk in disks):
                continue
            required += sizes[identity]
            details.append("%s/%d=%d" % (identity[0], identity[1], sizes[identity]))
        target = "each share number with no copy on a keep disk"
    else:
        required = 0
        details = []
        for version_name, version in manifest.versions.items():
            on_keep = {
                share.number
                for share in manifest.shares
                if share.version == version_name
                and manifest.disks[share.disk].keep
            }
            missing = version.k - len(on_keep)
            if missing > 0:
                candidates = sorted(
                    sizes[(version_name, number)]
                    for number in range(version.m)
                    if (version_name, number) in sizes
                )
                required += sum(candidates[:missing])
                details.append(
                    "%s needs %d more number(s)" % (version_name, missing)
                )
        target = "at least k share numbers per version"

    if required:
        # Final copies may only remain on keep disks (a scratch disk must end
        # empty, so its copy must be moved or deleted again).  A scratch disk
        # or an old disk can still hold a transient copy while a delete frees
        # space.  Therefore this is informational, not a rejection: only the
        # exact search can weigh the temporary copy count and ordering.
        keep_free = sum(
            disk.available_bytes
            for disk in manifest.disks.values()
            if disk.writable and disk.keep
        )
        scratch_free = sum(
            disk.available_bytes
            for disk in manifest.disks.values()
            if disk.writable and disk.scratch
        )
        if required > keep_free:
            text = (
                "final placement of %s needs at least %d byte(s) (%s); keep "
                "disks have %d byte(s) free and scratch can hold at most a "
                "transient %d byte(s)"
                % (target, required, "; ".join(details), keep_free, scratch_free)
            )
            diag.capacity_conflicts.append(text)

    # No destination at all (ignoring transient capacity) for an identity
    # that must be preserved.
    if manifest.preserve_all:
        for identity in sorted(_identity_needs_keep_copy(manifest, initial)):
            version_name, number = identity
            source_disks = initial.copy_map()[identity]
            candidates = [
                destination
                for destination, disk in manifest.disks.items()
                if disk.writable and destination not in source_disks
            ]
            if not candidates:
                diag.blocked_versions.append(version_name)
                diag.reasons.append(
                    "share %s/%d has no writable destination at all; its "
                    "copies are on %s"
                    % (
                        version_name,
                        number,
                        ", ".join(sorted(source_disks)),
                    )
                )
    return diag


def _diagnose_no_plan(
    manifest: Manifest,
    sizes: Mapping[Tuple[str, int], int],
    initial: _State,
    max_states: int,
) -> PlanningError:
    diag = _initial_diagnostics(manifest, sizes, initial)
    if diag.reasons:
        return PlanningError("no safe migration plan exists", diag)

    if not (
        list(_copy_candidates(manifest, initial, sizes))
        or list(_space_relief_candidates(manifest, initial, sizes))
        or list(_delete_candidates(manifest, initial, sizes))
    ):
        diag.reasons.append(
            "no first action is safe: every copy either lacks transient "
            "destination space or is forbidden, and every delete would break a "
            "failure scenario"
        )
        # Add concrete per-identity detail.
        for identity in sorted(_identity_needs_keep_copy(manifest, initial)):
            version_name, number = identity
            sources = initial.copy_map()[identity]
            details = []
            for destination, disk in sorted(manifest.disks.items()):
                if destination in sources or not disk.writable:
                    continue
                if not (disk.keep or disk.scratch):
                    continue
                details.append(
                    "%s free=%d%s"
                    % (
                        destination,
                        disk.available_bytes,
                        "" if disk.available_bytes >= sizes[identity] else " < needed",
                    )
                )
            diag.reasons.append(
                "share %s/%d (%d bytes) is only on %s; candidate destinations: %s"
                % (
                    version_name,
                    number,
                    sizes[identity],
                    ", ".join(sorted(sources)),
                    ", ".join(details) if details else "none",
                )
            )
        return PlanningError("no safe migration plan exists", diag)

    # A first action exists, so the obstruction is deeper: report the
    # resources and the preserved-share obligations rather than hand-waving.
    needs = _identity_needs_keep_copy(manifest, initial)
    if needs:
        detail = ", ".join("%s/%d" % identity for identity in sorted(needs))
        diag.reasons.append(
            "the search exhausted all reachable safe states while still needing "
            "keep-disk copies of: %s" % detail
        )
    else:
        diag.reasons.append(
            "the search exhausted all reachable safe states; the declared "
            "failure scenarios prevent the remaining deletes from being safe"
        )
    for failure in manifest.failures:
        counts = _numbers_by_version(initial, failure.lost_disks)
        parts = []
        for version_name in sorted(manifest.versions):
            parts.append(
                "%s=%d/%d"
                % (
                    version_name,
                    len(counts.get(version_name, set())),
                    manifest.versions[version_name].k,
                )
            )
        diag.reasons.append(
            "initial survival under %r: %s" % (failure.name, ", ".join(parts))
        )
    diag.reasons.append(
        "the instance is infeasible, not merely unordered; adding transient "
        "space, a scratch disk, or a writable keep destination is required"
    )
    return PlanningError("no safe migration plan exists", diag)


# ---------------------------------------------------------------------------
# Rendering


def _render_text(manifest: Manifest, result: Plan) -> str:
    sizes = _normalize_share_sizes(manifest)
    initial_managed = _managed_bytes(_initial_state(manifest, sizes), sizes)
    lines: List[str] = []
    policy = (
        "preserve every share number"
        if result.preserve_all
        else "keep at least k numbers per version"
    )
    lines.append(
        "Migration plan (%s): %d step(s), %d byte(s) written"
        % (policy, len(result.steps), result.total_bytes_written)
    )
    lines.append("")
    lines.append("Disks (available bytes are free space):")
    for name, disk in sorted(manifest.disks.items()):
        tags = ["old" if disk.retired else "keep"]
        if disk.scratch:
            tags.append("scratch")
        if not disk.writable:
            tags.append("read-only")
        lines.append(
            "  %-20s %-16s %d share bytes, %d free"
            % (name, "[" + ", ".join(tags) + "]", initial_managed.get(name, 0), disk.available_bytes)
        )

    if not result.steps:
        lines.append("")
        lines.append("Nothing to do: all shares already reside on keep disks.")
        return "\n".join(lines)

    lines.append("")
    for index, step in enumerate(result.steps, 1):
        action = step.action
        if action.kind == "copy":
            lines.append(
                "%d. copy    %s/%d  %s -> %s  (%d bytes)"
                % (
                    index,
                    action.version,
                    action.number,
                    action.source,
                    action.destination,
                    action.size,
                )
            )
            lines.append(
                "   %s free: %d -> %d; %d managed share bytes after"
                % (
                    action.destination,
                    step.free_before,
                    step.free_after,
                    step.managed_bytes_after,
                )
            )
            lines.append(
                "   verify %s now holds version %r number %d before deleting any source copy"
                % (
                    action.destination,
                    action.version,
                    action.number,
                )
            )
        else:
            lines.append(
                "%d. delete  %s/%d  from %s"
                % (index, action.version, action.number, action.source)
            )
            lines.append(
                "   %s free: %d -> %d; %d managed share bytes after"
                % (
                    action.source,
                    step.free_before,
                    step.free_after,
                    step.managed_bytes_after,
                )
            )
            lines.append(
                "   verified copies remain on: %s"
                % (", ".join(sorted(step.verified_copies)) or "(none)")
            )
        lines.append(
            "   distinct numbers: %s"
            % ", ".join(
                "%s=%d" % (version, count)
                for version, count in sorted(step.remaining.items())
            )
        )
        for failure_name, counts in sorted(step.remaining_by_failure.items()):
            lines.append(
                "   after failure %r: %s"
                % (
                    failure_name,
                    ", ".join(
                        "%s=%d" % (version, count)
                        for version, count in sorted(counts.items())
                    ),
                )
            )
    lines.append("")
    lines.append("Total bytes written: %d" % result.total_bytes_written)
    return "\n".join(lines)


def _render_diagnostics(diag: Diagnostics) -> str:
    lines = ["No safe migration plan exists."]
    if diag.reasons:
        lines.append("")
        lines.append("Blocking constraints:")
        for reason in diag.reasons:
            lines.append("  - " + reason)
    if diag.blocked_versions:
        lines.append("")
        lines.append(
            "Versions short of safe copies: " + ", ".join(sorted(set(diag.blocked_versions)))
        )
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zfec-migrate",
        description=(
            "Plan a safe, minimum-write retirement of zfec share disks. The "
            "input is a JSON manifest; every step preserves at least k distinct "
            "share numbers per object version under every declared failure."
        ),
    )
    parser.add_argument(
        "manifest", help="JSON manifest path, or '-' to read standard input"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit a machine-readable plan"
    )
    parser.add_argument(
        "--max-states",
        type=int,
        default=500_000,
        help="exact-search state limit (default: 500000)",
    )
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument(
        "--allow-drop",
        action="store_true",
        help="permit discarding surplus copies down to k numbers per version",
    )
    policy.add_argument(
        "--preserve-all",
        action="store_true",
        help="retain every share number present in the input instead of only k",
    )
    args = parser.parse_args(argv)

    try:
        if args.manifest == "-":
            try:
                data = json.load(sys.stdin)
            except json.JSONDecodeError as e:
                raise ManifestError("stdin is not valid JSON: %s" % e)
            manifest = manifest_from_dict(data)
        else:
            manifest = load_manifest(args.manifest)
        if args.allow_drop:
            manifest.preserve_all = False
        elif args.preserve_all:
            manifest.preserve_all = True
        result = plan(manifest, max_states=args.max_states)
        verify_plan(manifest, result)
    except ManifestError as e:
        if args.json:
            print(json.dumps({"feasible": False, "error": str(e)}, indent=2))
        else:
            print("Manifest error: %s" % e, file=sys.stderr)
        return 2
    except PlanningError as e:
        if args.json:
            payload = e.diagnostics.to_dict()
            payload["error"] = str(e)
            print(json.dumps(payload, indent=2))
        else:
            print(_render_diagnostics(e.diagnostics), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(_render_text(manifest, result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
