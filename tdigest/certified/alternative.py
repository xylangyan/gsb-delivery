"""
Alternative same-precision summary emitted by collection agents.

The collector may send a second summary of the same window at the same
precision using a *bulk* construction (the exact integer histogram is built
in one sorted pass) instead of the streaming construction used by
:class:`~tdigest.certified.certified_digest.CertifiedDigest`.

Both paths produce the identical exact ``(value, count)`` histogram, so they
merge interchangeably and canonicalize to the same byte representation. An
alternative summary is an older-version delivery channel: like every other
old version it may only replace another summary at a retractable boundary
(see
:meth:`CertifiedDigest.replace_window
<tdigest.certified.certified_digest.CertifiedDigest.replace_window>`).
"""

from __future__ import annotations

from collections import Counter

from .certified_digest import (
    CertificationError,
    CertifiedDigest,
    WindowRecord,
    _GKStore,
    _epsilon_capacity,
    _fraction,
)

ALT_SCHEMA = "alt-bulk-hist-v1"


class AlternativeSummary(object):
    """Bulk-construction exact-histogram summary at a declared precision."""

    def __init__(self, window_id, version, epsilon, store, record_ids,
                 supersedes=()):
        self.window_id = str(window_id)
        self.version = int(version)
        self.epsilon = _fraction(epsilon)
        self.capacity = store.capacity
        self.store = store
        self.record_ids = frozenset(record_ids)
        self.supersedes = frozenset(supersedes)

    @classmethod
    def from_window(cls, window_id, version, values, epsilon=0.01,
                    record_ids=None, supersedes=()):
        epsilon_f, capacity = _epsilon_capacity(epsilon)
        values = list(values)
        if record_ids is None:
            record_ids = list(range(len(values)))
        record_ids = [str(r) for r in record_ids]
        seen = set()
        pairs = []
        kept_ids = []
        for rid, value in zip(record_ids, values):
            if rid in seen:
                continue
            seen.add(rid)
            pairs.append((rid, int(value)))
            kept_ids.append(rid)
        counts = Counter(value for _, value in pairs)
        ordered = sorted(counts)
        # Exact histogram tuples -- byte-identical to the streaming path.
        tuples = [(value, counts[value], 0) for value in ordered]
        store = _GKStore(capacity, tuples, len(pairs), ordered[0],
                         ordered[-1], epsilon=epsilon_f)
        return cls(window_id, version, epsilon_f, store, kept_ids,
                   supersedes)

    def to_certified(self):
        """Convert to a :class:`CertifiedDigest` window record."""
        record = WindowRecord(
            self.window_id, self.version, self.epsilon, self.capacity,
            self.store, self.record_ids, self.supersedes)
        return CertifiedDigest(self.epsilon, [record], self.store,
                               certified=True)

    def to_dict(self):
        return {
            "schema": ALT_SCHEMA,
            "window_id": self.window_id,
            "version": self.version,
            "epsilon": self.epsilon,
            "record_ids": sorted(self.record_ids),
            "supersedes": sorted(self.supersedes),
            "store": {
                "capacity": self.capacity,
                "n": self.store.n,
                "min": self.store.min_value,
                "max": self.store.max_value,
                "block_cap": self.store.block_cap,
                "compressed": self.store.compressed,
                "tuples": self.store.to_list(),
            },
        }

    @classmethod
    def from_dict(cls, payload):
        if payload.get("schema") != ALT_SCHEMA:
            raise CertificationError(
                "not an alternative summary",
                [{"window_id": payload.get("window_id", "?"),
                  "reason": "unexpected_schema",
                  "detail": str(payload.get("schema"))}])
        sp = payload["store"]
        store = _GKStore.from_list(
            sp["tuples"], sp["n"], sp["min"], sp["max"], sp["capacity"],
            epsilon=payload["epsilon"], block_cap=sp.get("block_cap"),
            compressed=sp.get("compressed", False))
        return cls(payload["window_id"], payload["version"],
                   payload["epsilon"], store, payload["record_ids"],
                   payload.get("supersedes", ()))
