from __future__ import annotations

import hashlib
import pickle
import threading
from collections import defaultdict
from collections.abc import Hashable, Iterable, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from typing import Generic, Literal

from zict.common import KT, VT, discard, locked
from zict.sieve import MKT, Sieve


class BatchConflictError(Exception):
    """A batch id was reused with a different payload."""


class VersionMismatchError(Exception):
    """The expected versions of a relocation request don't match the
    currently published ones; the request was rejected without side effects.
    """


class DeferredWriteError(Exception):
    """A backend may raise this from ``__setitem__`` to signal that the fate
    of the write is unknown and a failure confirmation will arrive later
    (see :meth:`RelocatingSieve.confirm_failure`). The batch is left in
    ``pending`` state and nothing is published.
    """


@dataclass(frozen=True)
class RelocationItem(Generic[KT, VT, MKT]):
    """A single key to relocate: new value and target layer name."""

    key: KT
    value: VT
    target: MKT


@dataclass(frozen=True)
class RelocationRequest(Generic[KT, VT, MKT]):
    """A joint relocation of a group of keys, applied atomically.

    Parameters
    ----------
    batch_id:
        Idempotency key. Reusing it with an identical payload is a no-op
        replay; reusing it with a different payload raises
        :class:`BatchConflictError`.
    partition:
        Token identifying the partition (e.g. the job) the keys belong to.
        Relocations and group reads of the same partition are serialized;
        different partitions never block each other.
    items:
        The keys to relocate, with their new values and target layers.
    expected:
        Optimistic-concurrency guard: {key: expected current version}.
        If any key's published version differs, the request is rejected
        with :class:`VersionMismatchError` before anything is written.
    """

    batch_id: str
    partition: Hashable
    items: tuple[RelocationItem[KT, VT, MKT], ...]
    expected: Mapping[KT, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RelocationReceipt(Generic[KT, MKT]):
    """Outcome of :meth:`RelocatingSieve.relocate`.

    status is one of:

    - ``"committed"``: the batch was actually written and published now;
    - ``"already_published"``: identical replay of a committed batch;
      nothing was written again;
    - ``"pending"``: a backend reported a deferred failure; nothing was
      published and the group still reads the old members. Awaiting
      :meth:`RelocatingSieve.confirm_failure`.
    """

    batch_id: str
    status: Literal["committed", "already_published", "pending"]
    locations: Mapping[KT, MKT]
    versions: Mapping[KT, int]


class _BatchRecord:
    __slots__ = ("batch_id", "fingerprint", "status", "receipt", "staged")

    def __init__(self, batch_id: str, fingerprint: str):
        self.batch_id = batch_id
        self.fingerprint = fingerprint
        self.status: Literal["preparing", "committed", "pending", "failed"] = "preparing"
        self.receipt: RelocationReceipt | None = None
        # (target layer name, key) pairs successfully staged by this batch
        self.staged: list[tuple[object, object]] = []


def _fingerprint(request: RelocationRequest) -> str:
    payload = (
        request.partition,
        [(item.key, item.target, item.value) for item in request.items],
        sorted(request.expected.items(), key=lambda kv: repr(kv[0])),
    )
    return hashlib.sha256(pickle.dumps(payload)).hexdigest()


class RelocatingSieve(Sieve[KT, VT, MKT]):
    """A Sieve with a joint, all-or-nothing relocation entry point.

    Keys of the same job (e.g. a body and its index) may live in different
    layers. :meth:`relocate` moves a whole group of keys to new layers and
    publishes the new locations only once every member is ready, so
    consumers using :meth:`read_group` never observe a half-relocated
    group.

    The underlying mappings don't share a transaction, so relocation is
    two-phase: values are first written to per-layer staging areas
    (invisible to readers), then published under a per-partition lock.
    Staged data of failed batches may be left behind until it is confirmed
    and cleaned up by :meth:`confirm_failure`.

    Several layer names may point to the same underlying mapping; physical
    locations are deduplicated by identity.

    Parameters
    ----------
    mappings: dict of {layer name: MutableMapping}
    selector: callable (key, value) -> layer name
        Used by the plain MutableMapping interface only; relocations name
        their target layer explicitly.
    staging: optional dict of {layer name: MutableMapping}
        Staging area per layer name. Defaults to plain dicts.

    Notes
    -----
    Backend I/O is never performed while holding the registry lock or
    another partition's lock, so a blocked backend does not pause reads or
    relocations of other partitions.
    """

    versions: dict[KT, int]
    staging: dict[MKT, MutableMapping[tuple[str, KT], VT]]

    def __init__(
        self,
        mappings: Mapping[MKT, MutableMapping[KT, VT]],
        selector,
        staging: Mapping[MKT, MutableMapping[tuple[str, KT], VT]] | None = None,
    ):
        super().__init__(mappings, selector)
        self.versions = {}
        self.staging = {
            mkey: (staging[mkey] if staging is not None and mkey in staging else {})
            for mkey in mappings
        }
        self._batches: dict[str, _BatchRecord] = {}
        self._meta_lock = threading.RLock()
        self._group_locks: defaultdict[Hashable, threading.RLock] = defaultdict(
            threading.RLock
        )

    def __getstate__(self):
        state = super().__getstate__()
        del state["_meta_lock"]
        del state["_group_locks"]
        return state

    def __setstate__(self, state):
        super().__setstate__(state)
        self._meta_lock = threading.RLock()
        self._group_locks = defaultdict(threading.RLock)

    def _group_lock(self, partition: Hashable) -> threading.RLock:
        with self._meta_lock:
            return self._group_locks[partition]

    # --- version tracking for the plain MutableMapping interface ---

    @locked
    def __setitem__(self, key: KT, value: VT) -> None:
        # Sieve.__setitem__ discard()es the key first, which routes through
        # our __delitem__ override and drops the version; capture it upfront.
        old = self.versions.get(key, 0)
        super().__setitem__(key, value)
        self.versions[key] = old + 1

    @locked
    def _do_update(self, items: Iterable[tuple[KT, VT]]) -> None:
        items = list(items)
        olds = {key: self.versions.get(key, 0) for key, _ in items}
        super()._do_update(items)
        for key, old in olds.items():
            self.versions[key] = old + 1

    @locked
    def __delitem__(self, key: KT) -> None:
        super().__delitem__(key)
        self.versions.pop(key, None)

    # --- joint relocation ---

    def _check_versions(self, request: RelocationRequest[KT, VT, MKT]) -> None:
        mismatched = {
            key: (expected, self.versions.get(key, 0))
            for key, expected in request.expected.items()
            if self.versions.get(key, 0) != expected
        }
        if mismatched:
            raise VersionMismatchError(
                f"batch {request.batch_id!r} rejected: stale expected versions "
                f"(expected vs. published: {mismatched})"
            )

    def relocate(
        self, request: RelocationRequest[KT, VT, MKT]
    ) -> RelocationReceipt[KT, MKT]:
        """Jointly relocate a group of keys, all or nothing.

        See :class:`RelocationRequest` and :class:`RelocationReceipt`.
        """
        fingerprint = _fingerprint(request)
        with self._meta_lock:
            record = self._batches.get(request.batch_id)
            if record is not None:
                if record.fingerprint != fingerprint:
                    raise BatchConflictError(
                        f"batch id {request.batch_id!r} was already used with a "
                        "different payload"
                    )
                if record.status == "committed":
                    assert record.receipt is not None
                    return replace(record.receipt, status="already_published")
                # pending / failed / in-flight identical request: fall through
                # and re-validate against the currently published versions.
            else:
                record = _BatchRecord(request.batch_id, fingerprint)
                self._batches[request.batch_id] = record

        group_lock = self._group_lock(request.partition)
        with group_lock:
            self._check_versions(request)

        # Prepare: write to the staging areas, invisible to readers.
        # No lock is held here, so a blocked backend doesn't stall other
        # partitions.
        try:
            for item in request.items:
                # Record before writing: a backend may fail *after* having
                # stored the value, and the leftover must be cleanable.
                record.staged.append((item.target, item.key))
                self.staging[item.target][(request.batch_id, item.key)] = item.value
        except DeferredWriteError:
            # The backend will confirm the failure later; leave the staged
            # data in place until confirm_failure() cleans it up.
            with self._meta_lock:
                record.status = "pending"
            return RelocationReceipt(request.batch_id, "pending", {}, {})
        except Exception:
            with self._meta_lock:
                record.status = "failed"
            self._discard_staged(record)
            raise

        # Publish: re-validate (another batch may have committed meanwhile),
        # then move staged values to their targets and flip the directory,
        # all under the partition lock so group reads never see a half
        # relocated group.
        with group_lock:
            try:
                self._check_versions(request)
            except VersionMismatchError:
                with self._meta_lock:
                    record.status = "failed"
                self._discard_staged(record)
                raise
            for item in request.items:
                value = self.staging[item.target].pop((request.batch_id, item.key))
                target = self.mappings[item.target]
                old = self.key_to_mapping.get(item.key)
                if old is not None and old is not target:
                    discard(old, item.key)
                target[item.key] = value
                self.key_to_mapping[item.key] = target
                self.versions[item.key] = self.versions.get(item.key, 0) + 1
            self.gen += 1
            receipt = RelocationReceipt(
                request.batch_id,
                "committed",
                {item.key: item.target for item in request.items},
                {item.key: self.versions[item.key] for item in request.items},
            )
        with self._meta_lock:
            record.status = "committed"
            record.receipt = receipt
            record.staged.clear()
        return receipt

    def confirm_failure(self, batch_id: str) -> str:
        """Deliver a delayed failure confirmation for a pending batch.

        Marks the batch as failed and cleans up its staged data on a
        best-effort basis (a still-blocked backend may keep its residue).
        Returns the batch status. No-op if the batch isn't pending.
        """
        with self._meta_lock:
            record = self._batches[batch_id]
            if record.status != "pending":
                return record.status
            record.status = "failed"
        self._discard_staged(record)
        return "failed"

    def _discard_staged(self, record: _BatchRecord) -> None:
        for target, key in record.staged:
            try:
                discard(self.staging[target], (record.batch_id, key))
            except Exception:
                # A still-blocked backend may keep its residue
                pass
        record.staged.clear()

    # --- group read ---

    def read_group(self, partition: Hashable, keys: Iterable[KT]) -> dict[KT, VT]:
        """Atomically read a group of keys of one partition.

        Returns the currently published members; a group being relocated
        keeps reading its old members until the whole batch is ready.
        """
        with self._group_lock(partition):
            return {key: self[key] for key in keys}
