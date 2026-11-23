"""
Certified, mergeable quantile summaries.

A :class:`CertifiedDigest` is a deterministic, integer-domain quantile
summary with a *proven rank error* measured against every original
observation.

Design
------
Samples are integer units, so a collection window is summarized as an exact
histogram of ``(value, count)`` pairs. Histograms merge associatively by
adding the counts of equal values, which makes every intermediate node a
pure function of the effective set of observations:

* no dependence on the shape of the aggregation tree,
* no dependence on insertion order or message retries,
* duplicate deliveries are removed up front via stable record ids.

Only a root node runs :meth:`~_GKStore.canonicalize`, a deterministic
compression that packs the ordered histogram into blocks of at most
``B = ceil(epsilon * n)`` observations; each block keeps its maximum value as
representative. For any queried value ``x`` the true rank is bracketed by two
consecutive cumulative block weights, so the rank interval width -- and the
rank error against the original observations -- is at most ``B`` ranks. A
value repeated more than ``B`` times is split into several blocks sharing the
same representative, which preserves the same interval guarantee.

Provenance and the root memory budget
-------------------------------------
A node may be :meth:`~CertifiedDigest.prune`-ed so it retains only the
compressed store plus one lightweight manifest per effective window
(identity, version, weight, hashes, heaviest repeated value). Neither raw
observations nor per-window full summaries are ever needed at a root. The
root footprint is ``O(1/epsilon)`` blocks plus ``O(s)`` manifests for *s*
sources; :meth:`CertifiedDigest.memory_budget` reports it from the configured
precision and source count.

Certification
-------------
A certificate is only issued when every effective source is certified and
every retry/replacement relation is provable. Merging a legacy
``TDigest.to_dict`` window, replacing at a non-retractable boundary, or an
ambiguous retry overwrite instead records the offending window id in
``gap_sources`` and forbids certification at the requested precision.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_left
from collections import Counter

from ..tdigest import TDigest

SCHEMA = "certified-hist-block-v1"
MAX_ATTEMPTS = 1 << 31


class CertificationError(Exception):
    """Raised when a requested error bound cannot be proven.

    ``gap_sources`` lists the stable window ids whose contribution breaks the
    proof, each with a machine-readable reason. No certificate is issued at
    the requested precision while gaps exist.
    """

    def __init__(self, message, gap_sources=None):
        super(CertificationError, self).__init__(message)
        self.gap_sources = list(gap_sources or [])


def _fraction(value):
    if isinstance(value, (int, float)):
        return float(value)
    num, _, den = str(value).partition("/")
    return float(num) / float(den) if den else float(num)


def _epsilon_capacity(epsilon):
    epsilon = _fraction(epsilon)
    if not 0 < epsilon < 1:
        raise ValueError("epsilon must satisfy 0 < epsilon < 1")
    return epsilon, int(math.ceil(1.0 / epsilon))


# ---------------------------------------------------------------------------
# Exact histogram store with deterministic root block compression
# ---------------------------------------------------------------------------

class _GKStore(object):
    """Exact ``(value, count)`` histogram with a block-compressed view.

    Tuples are always ``(value, weight, 0)``; the third element is retained
    only to keep a stable, versioned serialization shape. Before
    canonicalization the histogram is exact; afterwards tuples are blocks
    whose representative is the maximum value covered by the block.
    """

    __slots__ = ("tuples", "n", "capacity", "epsilon", "min_value",
                 "max_value", "block_cap", "compressed")

    def __init__(self, capacity, tuples=None, n=0,
                 min_value=None, max_value=None, epsilon=None,
                 block_cap=None, compressed=False):
        self.capacity = int(capacity)
        self.tuples = list(tuples or [])
        self.n = int(n)
        self.min_value = min_value
        self.max_value = max_value
        self.epsilon = epsilon
        self.block_cap = block_cap
        self.compressed = bool(compressed)

    # -- exact histogram insertion ----------------------------------------

    def add(self, value, weight=1):
        value = int(value)
        weight = int(weight)
        if weight <= 0:
            return
        if self.n == 0:
            self.tuples = [(value, weight, 0)]
            self.n = weight
            self.min_value = self.max_value = value
            return
        if value < self.min_value:
            self.min_value = value
            idx = 0
        elif value > self.max_value:
            self.max_value = value
            idx = len(self.tuples)
        else:
            idx = bisect_left(self.tuples, (value, -1, -1))
        if idx < len(self.tuples) and self.tuples[idx][0] == value:
            v, g, d = self.tuples[idx]
            self.tuples[idx] = (v, g + weight, d)
        else:
            self.tuples.insert(idx, (value, weight, 0))
        self.n += weight

    def add_many(self, values):
        counts = Counter(int(v) for v in values)
        for value in sorted(counts):
            self.add(value, counts[value])

    # -- associative exact merge ------------------------------------------

    @classmethod
    def merge(cls, stores):
        """Exact, associative union of histogram stores."""
        stores = [s for s in stores if s.n > 0]
        if not stores:
            return cls(0)
        capacity = max(s.capacity for s in stores)
        epsilons = [s.epsilon for s in stores if s.epsilon is not None]
        epsilon = min(epsilons) if epsilons else None
        combined = []
        for store in stores:
            combined.extend(store.tuples)
        combined.sort(key=lambda t: t[0])
        tuples = []
        for value, weight, d in combined:
            if tuples and tuples[-1][0] == value:
                v, g, _ = tuples[-1]
                tuples[-1] = (v, g + weight, 0)
            else:
                tuples.append((value, weight, 0))
        n = sum(s.n for s in stores)
        return cls(capacity, tuples, n,
                   min(s.min_value for s in stores),
                   max(s.max_value for s in stores),
                   epsilon=epsilon)

    # -- deterministic root compression -----------------------------------

    def canonicalize(self, block_cap=None):
        """Pack the exact histogram into deterministic rank-bounded blocks.

        Pure function of the sorted histogram, the total weight ``n`` and the
        configured precision, so two nodes holding the same observations
        produce byte-identical compressed stores regardless of how the
        observations were grouped. Blocks are packed left-to-right while the
        block weight stays at or below ``block_cap``; a single value heavier
        than the cap is split into same-representative blocks.
        """
        if not self.tuples:
            out = _GKStore(self.capacity, [], 0, None, None,
                           epsilon=self.epsilon)
            out.block_cap = 0
            return out
        if block_cap is None:
            epsilon = self.epsilon
            if epsilon is None:
                epsilon = 1.0 / max(1, self.capacity)
            # floor keeps the relative width block_cap/n <= epsilon exactly
            # (a minimum of one rank is unavoidable for tiny streams).
            block_cap = max(1, int(math.floor(epsilon * self.n)))
        block_cap = int(block_cap)
        blocks = []
        run_value, run_weight = None, 0
        for value, weight, _d in self.tuples:
            if run_value is None:
                run_value, run_weight = value, weight
            elif value == run_value:
                run_weight += weight
            elif run_weight + weight <= block_cap:
                # Representative is the block maximum (values are sorted).
                run_value, run_weight = value, run_weight + weight
            else:
                self._emit(blocks, run_value, run_weight, block_cap)
                run_value, run_weight = value, weight
        if run_value is not None:
            self._emit(blocks, run_value, run_weight, block_cap)
        out = _GKStore(self.capacity, blocks, self.n,
                       self.min_value, self.max_value,
                       epsilon=self.epsilon, block_cap=block_cap,
                       compressed=True)
        return out

    @staticmethod
    def _emit(blocks, value, weight, block_cap):
        while weight > block_cap:
            blocks.append((value, block_cap, 0))
            weight -= block_cap
        if weight > 0:
            blocks.append((value, weight, 0))

    # -- rank queries ------------------------------------------------------

    def rank_bounds(self, value):
        """Proven 1-based rank interval of ``value``.

        On a compressed store, if block *j* is the first block whose
        representative exceeds ``value``, every earlier block is fully
        ``<= value`` and block *j* is the only one that can straddle it, so
        the true rank lies between consecutive cumulative weights -- an
        interval of at most ``block_cap`` ranks. On an uncompressed exact
        store the interval collapses to the true rank.
        """
        if not self.tuples:
            return 0, 0
        if value < self.min_value:
            return 0, 0
        if value >= self.max_value:
            return self.n, self.n
        cumulative = 0
        for rep, weight, _d in self.tuples:
            if rep <= value:
                cumulative += weight
                continue
            return cumulative, min(cumulative + weight, self.n)
        return self.n, self.n

    def quantile(self, q):
        if not self.tuples:
            raise ValueError("cannot take a quantile of an empty summary")
        target = q * self.n
        cumulative = 0
        for rep, weight, _d in self.tuples:
            cumulative += weight
            if cumulative >= target:
                # The target rank falls in this block; the representative's
                # rank is cumulative, off by less than one block weight.
                return rep
        return self.max_value

    def heaviest_value(self):
        if not self.tuples:
            return None, 0
        value, mass = max(((v, g) for v, g, _d in self.tuples),
                          key=lambda pair: pair[1])
        return value, mass

    # -- serialization -----------------------------------------------------

    def to_list(self):
        return [[v, g, d] for v, g, d in self.tuples]

    @classmethod
    def from_list(cls, payload, n, min_value, max_value, capacity=None,
                  epsilon=None, block_cap=None, compressed=False):
        cap = capacity if capacity is not None else 1
        return cls(cap, [tuple(t) for t in payload], n, min_value,
                   max_value, epsilon=epsilon, block_cap=block_cap,
                   compressed=compressed)

    def hash(self):
        body = json.dumps(
            {"capacity": self.capacity, "n": self.n,
             "min": self.min_value, "max": self.max_value,
             "block_cap": self.block_cap,
             "tuples": self.to_list()},
            sort_keys=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# Window records and provenance
# ---------------------------------------------------------------------------

class WindowRecord(object):
    """One certified collection window held at a retractable boundary.

    ``record_ids`` are the stable identities of individual observations.
    Duplicate deliveries of the same record id are counted once.
    """

    kind = "certified"

    __slots__ = ("window_id", "version", "epsilon", "capacity", "store",
                 "record_ids", "supersedes")

    def __init__(self, window_id, version, epsilon, capacity, store,
                 record_ids, supersedes=()):
        self.window_id = str(window_id)
        self.version = int(version)
        self.epsilon = _fraction(epsilon)
        self.capacity = int(capacity)
        self.store = store
        self.record_ids = frozenset(record_ids)
        self.supersedes = frozenset(supersedes)

    def manifest(self):
        tie_value, tie_mass = self.store.heaviest_value()
        return {
            "kind": "certified",
            "window_id": self.window_id,
            "version": self.version,
            "record_count": len(self.record_ids),
            "weight": self.store.n,
            "supersedes": sorted(self.supersedes),
            "store_hash": self.store.hash(),
            "tie_value": tie_value,
            "tie_mass": tie_mass,
        }


class SourceManifest(object):
    """A pruned source: provenance only, no per-window store or raw values."""

    kind = "manifest"

    __slots__ = ("window_id", "version", "weight", "record_count",
                 "supersedes", "store_hash", "legacy", "tie_value",
                 "tie_mass")

    def __init__(self, window_id, version, weight, record_count,
                 supersedes=(), store_hash=None, legacy=False,
                 tie_value=None, tie_mass=0):
        self.window_id = str(window_id)
        self.version = int(version)
        self.weight = int(weight)
        self.record_count = (None if record_count is None
                             else int(record_count))
        self.supersedes = frozenset(supersedes)
        self.store_hash = store_hash
        self.legacy = bool(legacy)
        self.tie_value = tie_value
        self.tie_mass = int(tie_mass or 0)

    def manifest(self):
        out = {
            "kind": "legacy" if self.legacy else "manifest",
            "window_id": self.window_id,
            "version": self.version,
            "record_count": self.record_count,
            "weight": self.weight,
            "supersedes": sorted(self.supersedes),
            "store_hash": self.store_hash,
            "tie_value": self.tie_value,
            "tie_mass": self.tie_mass,
        }
        if self.legacy:
            out["legacy"] = True
        return out


class LegacyWindow(object):
    """A pre-certification window only available as ``TDigest.to_dict``.

    No raw data and no error certificate exist, so any aggregate containing
    it can be queried approximately but never certified.
    """

    kind = "legacy"

    __slots__ = ("window_id", "version", "digest", "weight", "supersedes")

    def __init__(self, window_id, version, digest):
        self.window_id = str(window_id)
        self.version = int(version)
        if not isinstance(digest, TDigest):
            digest = TDigest.from_dict(digest)
        self.digest = digest
        self.weight = int(digest.n)
        self.supersedes = frozenset()

    def manifest(self):
        return {
            "kind": "legacy",
            "window_id": self.window_id,
            "version": self.version,
            "record_count": None,
            "weight": self.weight,
            "supersedes": [],
            "store_hash": None,
            "tie_value": None,
            "tie_mass": 0,
            "legacy": True,
        }


def _source_key(source):
    return (source.window_id, source.kind, source.version)


# ---------------------------------------------------------------------------
# Certified digest
# ---------------------------------------------------------------------------

class CertifiedDigest(object):
    """Mergeable, certifiable quantile summary of a group of windows."""

    def __init__(self, epsilon=0.01, sources=None, store=None,
                 certified=True, gap_sources=None, replacements=None,
                 sub_certificates=None):
        self.epsilon, self.capacity = _epsilon_capacity(epsilon)
        self.sources = list(sources or [])
        self._store = store
        self.certified = bool(certified)
        self.gap_sources = list(gap_sources or [])
        self.replacements = list(replacements or [])
        self.sub_certificates = list(sub_certificates or [])

    # -- construction ------------------------------------------------------

    @classmethod
    def from_window(cls, window_id, version, values, epsilon=0.01,
                    record_ids=None, supersedes=()):
        epsilon_f, capacity = _epsilon_capacity(epsilon)
        values = list(values)
        if record_ids is None:
            record_ids = list(range(len(values)))
        record_ids = [str(r) for r in record_ids]
        if len(record_ids) != len(values):
            raise ValueError("record_ids must align one-to-one with values")
        seen = set()
        unique = []
        kept_ids = []
        for rid, value in zip(record_ids, values):
            if rid in seen:
                continue
            seen.add(rid)
            unique.append(int(value))
            kept_ids.append(rid)
        store = _GKStore(capacity, epsilon=epsilon_f)
        store.add_many(unique)
        record = WindowRecord(window_id, version, epsilon_f, capacity, store,
                              kept_ids, supersedes)
        return cls(epsilon_f, [record], store, certified=True)

    @classmethod
    def from_legacy_dict(cls, window_id, version, digest_dict):
        record = LegacyWindow(window_id, version, digest_dict)
        return cls(digest_dict.get("delta", 0.01), [record], None,
                   certified=False,
                   gap_sources=[{"window_id": str(window_id),
                                 "reason": "legacy_tdigest_no_certificate",
                                 "detail": "only TDigest.to_dict centroids "
                                           "and weights are available"}])

    # -- properties --------------------------------------------------------

    @property
    def n(self):
        return self._merged_store().n

    @property
    def window_ids(self):
        return sorted({s.window_id for s in self.sources})

    @property
    def is_pruned(self):
        return all(not isinstance(s, WindowRecord) for s in self.sources)

    def _merged_store(self):
        if self._store is not None:
            return self._store
        stores = [s.store for s in self.sources
                  if isinstance(s, WindowRecord) and s.store.n > 0]
        self._store = (_GKStore.merge(stores) if stores
                       else _GKStore(self.capacity, epsilon=self.epsilon))
        return self._store

    def _final_store(self):
        store = self._merged_store()
        if store.compressed:
            # A finalized root must not be compressed a second time, or its
            # blocks would be misread as exact histogram entries.
            return store
        return store.canonicalize()

    def _legacy_sources(self):
        return [s for s in self.sources if isinstance(s, LegacyWindow)]

    def _certified_records(self):
        return [s for s in self.sources if isinstance(s, WindowRecord)]

    # -- merging -----------------------------------------------------------

    def merge(self, *others):
        """Merge summaries from any aggregation level.

        Canonical by construction: the numeric store is the exact histogram
        union of the effective attempts, and provenance is the effective
        (highest-version) attempt of every window id.
        """
        all_digests = [self] + list(others)
        for digest in all_digests:
            if digest._store is not None and digest._store.compressed:
                raise CertificationError(
                    "cannot merge a finalized (root-compressed) summary; "
                    "perform all merges before calling finalize()",
                    [{"window_id": "(finalized-node)",
                      "reason": "compressed_summary_not_mergeable"}])
        epsilon = min(d.epsilon for d in all_digests)
        capacity = max(d.capacity for d in all_digests)

        attempts = {}
        gaps = []
        sub_certificates = []
        # Replacements already applied at a retractable boundary are proven
        # operations; the superseded version need not still be present here.
        verified = set()
        for digest in all_digests:
            for rep in digest.replacements:
                verified.add((rep["window_id"], rep["old_version"],
                              rep["new_version"]))
            for source in digest.sources:
                key = (source.window_id, source.version)
                if key in attempts and attempts[key] is not source:
                    if not _same_attempt(attempts[key], source):
                        gaps.append({
                            "window_id": source.window_id,
                            "reason": "ambiguous_retry_overwrite",
                            "detail": "two non-identical payloads for attempt "
                                      "%d" % source.version,
                        })
                        continue
                attempts.setdefault(key, source)
            gaps.extend(digest.gap_sources)
            sub_certificates.extend(digest.sub_certificates)

        chosen, retry_gaps = _resolve_attempts(attempts, verified)
        gaps.extend(retry_gaps)

        merged_store, numeric_gaps = self._numeric_merge(
            all_digests, attempts, capacity)
        gaps.extend(numeric_gaps)

        for source in chosen:
            if isinstance(source, LegacyWindow):
                if not any(g["window_id"] == source.window_id and
                           g["reason"] == "legacy_tdigest_no_certificate"
                           for g in gaps):
                    gaps.append({
                        "window_id": source.window_id,
                        "reason": "legacy_tdigest_no_certificate",
                        "detail": "legacy window merged into the aggregate; "
                                  "its rank error is not provable",
                    })

        replacements = []
        for source in chosen:
            for old in sorted(source.supersedes):
                replacements.append({
                    "window_id": source.window_id,
                    "old_version": old,
                    "new_version": source.version,
                })

        result = CertifiedDigest(
            epsilon, sorted(chosen, key=_source_key), merged_store,
            certified=False, gap_sources=_dedupe_gaps(gaps),
            replacements=_dedupe_replacements(replacements),
            sub_certificates=_dedupe_strings(sub_certificates))
        result.gap_sources = _collapse_legacy_gaps(result.gap_sources)
        result.certified = not result.gap_sources
        return result

    def __add__(self, other):
        return self.merge(other)

    def _numeric_merge(self, all_digests, attempts, capacity):
        """Merge histogram stores without double counting retried windows.

        Effective leaf records (one per ``(window_id, version)``) are merged
        exactly once. A window that only exists inside a pruned summary can
        only be counted through that summary's store, which is safe solely
        when the summary is disjoint from every other input; an overlapping
        pruned window cannot be de-duplicated and becomes a named gap.
        """
        gaps = []
        # Legacy windows have no GK store; they never enter the numeric
        # histogram merge (they only affect approximation and certification).
        cert_attempts = {k: s for k, s in attempts.items()
                         if not isinstance(s, LegacyWindow)}
        effective_keys = frozenset(cert_attempts)
        leaf_store_by_key = {}
        for key, source in cert_attempts.items():
            if isinstance(source, WindowRecord):
                leaf_store_by_key[key] = source.store
        leaf_keys = frozenset(leaf_store_by_key)
        pruned_only_keys = effective_keys - leaf_keys

        coverages = [frozenset((s.window_id, s.version)
                               for s in d.sources
                               if not isinstance(s, LegacyWindow))
                     for d in all_digests]
        overlap_keys = set()
        for i in range(len(coverages)):
            for j in range(i + 1, len(coverages)):
                overlap_keys |= coverages[i] & coverages[j]

        for window_id, version in sorted(pruned_only_keys):
            if (window_id, version) in overlap_keys:
                gaps.append({
                    "window_id": window_id,
                    "reason": "overlapping_pruned_summary",
                    "detail": ("attempt %d arrives in overlapping pruned "
                               "summaries and cannot be de-duplicated"
                               % version),
                })
        if gaps:
            stores = [d._merged_store() for d in all_digests
                      if d._merged_store().n > 0]
            return (_GKStore.merge(stores) if stores
                    else _GKStore(capacity, epsilon=self.epsilon)), gaps

        stores = list(leaf_store_by_key.values())
        # Pruned-only windows are counted via disjoint pruned summaries;
        # reject any such summary that also covers a leaf key (double count).
        for digest, coverage in zip(all_digests, coverages):
            if coverage and coverage <= pruned_only_keys and \
                    coverage.isdisjoint(overlap_keys):
                if digest._merged_store().n > 0:
                    stores.append(digest._merged_store())
            elif not coverage.isdisjoint(leaf_keys) and \
                    not coverage.isdisjoint(pruned_only_keys):
                for window_id, version in sorted(coverage & leaf_keys):
                    gaps.append({
                        "window_id": window_id,
                        "reason": "mixed_pruned_summary",
                        "detail": ("attempt %d shares a pruned summary with "
                                   "non-recoverable windows" % version),
                    })
        if gaps:
            stores = [d._merged_store() for d in all_digests
                      if d._merged_store().n > 0]
            return (_GKStore.merge(stores) if stores
                    else _GKStore(capacity, epsilon=self.epsilon)), gaps
        return (_GKStore.merge(stores) if stores
                else _GKStore(capacity, epsilon=self.epsilon)), gaps

    # -- pruning -----------------------------------------------------------

    def prune(self):
        """Drop per-window full summaries and raw record ids at this node."""
        pruned = [SourceManifest(**_manifest_kwargs(s))
                  for s in sorted(self.sources, key=_source_key)]
        out = CertifiedDigest(
            self.epsilon, pruned, self._merged_store(),
            certified=self.certified, gap_sources=list(self.gap_sources),
            replacements=list(self.replacements),
            sub_certificates=self.sub_certificates)
        if self.certified:
            cert = self.certificate()
            out.sub_certificates = _dedupe_strings(
                self.sub_certificates + [cert["certificate_hash"]])
        return out

    def finalize(self):
        """Persist the deterministic root compression on this node."""
        out = self._clone()
        out._store = self._final_store()
        return out

    def _clone(self):
        return CertifiedDigest(
            self.epsilon, list(self.sources), self._store,
            certified=self.certified, gap_sources=list(self.gap_sources),
            replacements=list(self.replacements),
            sub_certificates=list(self.sub_certificates))

    # -- replacement / retraction ------------------------------------------

    def replace_window(self, window_id, new_record):
        """Replace a window only at a retractable (full-record) boundary."""
        window_id = str(window_id)
        present = [s for s in self.sources if s.window_id == window_id]
        if not present:
            raise CertificationError(
                "window %s is not present at this node" % window_id,
                [{"window_id": window_id,
                  "reason": "window_not_at_boundary",
                  "detail": "it was already folded into an upstream summary"}])
        if not any(isinstance(s, WindowRecord) for s in present):
            raise CertificationError(
                "window %s is held as a pruned manifest only; replacement is "
                "not retractable at this layer" % window_id,
                [{"window_id": window_id,
                  "reason": "non_retractable_boundary",
                  "detail": "full per-window summary is not available here"}])
        old = max(present, key=lambda s: s.version)
        if new_record.version <= old.version:
            raise CertificationError(
                "replacement for %s must have a greater version" % window_id,
                [{"window_id": window_id,
                  "reason": "version_not_advancing"}])
        if not isinstance(new_record, WindowRecord):
            raise CertificationError(
                "replacement for %s is not a certified record" % window_id,
                [{"window_id": window_id,
                  "reason": "legacy_tdigest_no_certificate"}])
        remaining = [s for s in self.sources if s.window_id != window_id]
        remaining.append(new_record)
        rebuilt = CertifiedDigest(
            self.epsilon, remaining,
            replacements=self.replacements + [{
                "window_id": window_id,
                "old_version": old.version,
                "new_version": new_record.version,
            }])
        return rebuilt.merge()

    # -- queries -----------------------------------------------------------

    def percentile(self, p):
        if not self._certified_records() and self._legacy_sources():
            return self._legacy_percentile(p)
        return float(self._final_store().quantile(p / 100.0))

    def quantile(self, q):
        return float(self._final_store().quantile(q))

    def _legacy_percentile(self, p):
        digests = self._legacy_sources()
        total = sum(d.weight for d in digests) or 1
        return sum(d.digest.percentile(p) * d.weight
                   for d in digests) / total

    def rank_interval(self, value):
        self.require_certificate()
        return self._final_store().rank_bounds(int(value))

    # -- certification -----------------------------------------------------

    def require_certificate(self):
        if not self.certified:
            raise CertificationError(
                "rank error of %g cannot be proven: %d source(s) have gaps"
                % (self.epsilon, len(self.gap_sources)),
                self.gap_sources)

    def certificate(self):
        """Error certificate valid for further upward merging.

        The bound is expressed in ranks against every original observation;
        only lightweight window manifests and hashes are embedded, so the
        certificate survives pruning and re-validation at an upstream node.
        """
        self.require_certificate()
        store = self._final_store()
        windows = []
        for source in sorted(self.sources, key=_source_key):
            manifest = source.manifest()
            manifest["kind"] = ("legacy" if manifest.get("legacy")
                                else "certified")
            windows.append(manifest)
        payload = {
            "schema": SCHEMA,
            "epsilon": self.epsilon,
            "capacity": self.capacity,
            "rank_error_bound": store.block_cap,
            "relative_rank_error": store.block_cap / float(store.n),
            "n": store.n,
            "window_count": len(windows),
            "windows": windows,
            "digest_hash": self.digest_hash(),
            "replacements": sorted(self.replacements,
                                   key=lambda r: (r["window_id"],
                                                  r["old_version"])),
        }
        payload["certificate_hash"] = _hash_json(payload)
        payload["sub_certificates"] = sorted(self.sub_certificates)
        return payload

    def verify_certificate(self, certificate):
        if not self.certified:
            return False
        return certificate.get("certificate_hash") == \
            self.certificate().get("certificate_hash")

    # -- serialization -----------------------------------------------------

    def to_dict(self):
        # Persist the exact (uncompressed) union so a restored node can still
        # be merged upward; canonical output is produced separately on demand.
        store = self._merged_store()
        sources = []
        for source in sorted(self.sources, key=_source_key):
            if isinstance(source, WindowRecord):
                sources.append({
                    "kind": "certified",
                    "window_id": source.window_id,
                    "version": source.version,
                    "epsilon": source.epsilon,
                    "record_ids": sorted(source.record_ids),
                    "supersedes": sorted(source.supersedes),
                    "store": _store_payload(source.store),
                })
            elif isinstance(source, LegacyWindow):
                sources.append({
                    "kind": "legacy",
                    "window_id": source.window_id,
                    "version": source.version,
                    "tdigest": source.digest.to_dict(),
                })
            else:
                sources.append({"kind": "manifest", **source.manifest()})
        return {
            "schema": SCHEMA,
            "epsilon": self.epsilon,
            "capacity": self.capacity,
            "certified": self.certified,
            "gap_sources": self.gap_sources,
            "replacements": self.replacements,
            "sub_certificates": sorted(self.sub_certificates),
            "store": _store_payload(store),
            "sources": sources,
        }

    @classmethod
    def from_dict(cls, payload):
        if "centroids" in payload and "schema" not in payload:
            return cls.from_legacy_dict(
                payload.get("window_id", "legacy"),
                payload.get("version", 0), payload)
        sources = []
        for item in payload["sources"]:
            kind = item["kind"]
            if kind == "legacy":
                sources.append(LegacyWindow(item["window_id"],
                                            item["version"],
                                            item["tdigest"]))
            elif kind == "manifest":
                sources.append(SourceManifest(
                    item["window_id"], item["version"], item["weight"],
                    item["record_count"], item.get("supersedes", ()),
                    item.get("store_hash"), item.get("legacy", False),
                    item.get("tie_value"), item.get("tie_mass", 0)))
            else:
                sp = item["store"]
                sources.append(WindowRecord(
                    item["window_id"], item["version"], item["epsilon"],
                    sp["capacity"], _store_from_payload(sp),
                    item["record_ids"], item.get("supersedes", ())))
        return cls(payload["epsilon"], sources,
                   _store_from_payload(payload["store"]),
                   certified=payload["certified"],
                   gap_sources=payload.get("gap_sources", []),
                   replacements=payload.get("replacements", []),
                   sub_certificates=payload.get("sub_certificates", []))

    def canonical_dict(self):
        """Tree-independent view: compressed store plus window manifests."""
        windows = []
        for source in sorted(self.sources, key=_source_key):
            manifest = source.manifest()
            manifest["kind"] = ("legacy" if manifest.get("legacy")
                                else "certified")
            windows.append(manifest)
        return {
            "schema": SCHEMA,
            "epsilon": self.epsilon,
            "capacity": self.capacity,
            "certified": self.certified,
            "gap_sources": sorted(
                self.gap_sources,
                key=lambda g: (g["window_id"], g["reason"],
                               g.get("detail", ""))),
            "replacements": sorted(
                self.replacements,
                key=lambda r: (r["window_id"], r["old_version"],
                               r["new_version"])),
            "store": _store_payload(self._final_store()),
            "windows": windows,
        }

    def to_canonical_bytes(self):
        return json.dumps(self.canonical_dict(), sort_keys=True,
                          separators=(",", ":"), ensure_ascii=True).encode(
            "ascii")

    def digest_hash(self):
        return hashlib.sha256(self.to_canonical_bytes()).hexdigest()

    def memory_budget(self, source_count):
        """Root footprint for the configured precision and source count.

        The compressed store holds at most ``ceil(n / B) + 1`` blocks with
        ``B = ceil(epsilon * n)``, i.e. ``O(1/epsilon)``, plus one manifest
        per source. Raw values and per-window full summaries are excluded.
        """
        store = self._final_store()
        blocks = len(store.tuples)
        sources = max(1, int(source_count))
        return {
            "blocks": blocks,
            "block_capacity": store.block_cap,
            "manifests": sources,
            "total_units": blocks + sources,
            "precision_budget": int(math.ceil(1.0 / self.epsilon)),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store_payload(store):
    return {
        "capacity": store.capacity,
        "n": store.n,
        "min": store.min_value,
        "max": store.max_value,
        "block_cap": store.block_cap,
        "compressed": store.compressed,
        "tuples": store.to_list(),
    }


def _store_from_payload(payload):
    return _GKStore.from_list(
        payload["tuples"], payload["n"], payload["min"], payload["max"],
        payload.get("capacity"), block_cap=payload.get("block_cap"),
        compressed=payload.get("compressed", False))


def _manifest_kwargs(source):
    m = source.manifest()
    return {
        "window_id": m["window_id"],
        "version": m["version"],
        "weight": m["weight"],
        "record_count": m["record_count"],
        "supersedes": m.get("supersedes", ()),
        "store_hash": m.get("store_hash"),
        "legacy": m.get("legacy", False),
        "tie_value": m.get("tie_value"),
        "tie_mass": m.get("tie_mass", 0),
    }


def _attempt_identity(source):
    if isinstance(source, WindowRecord):
        return (source.window_id, source.version, source.store.n,
                source.store.hash(), len(source.record_ids))
    if isinstance(source, SourceManifest):
        return (source.window_id, source.version, source.weight,
                source.store_hash, source.record_count)
    return (source.window_id, source.version, source.weight, None, None)


def _same_attempt(a, b):
    if isinstance(a, LegacyWindow) or isinstance(b, LegacyWindow):
        return (type(a) is type(b)
                and a.digest.to_dict() == b.digest.to_dict())
    return _attempt_identity(a) == _attempt_identity(b)


def _resolve_attempts(attempts, verified_replacements=()):
    by_window = {}
    for (window_id, version), source in attempts.items():
        by_window.setdefault(window_id, {})[version] = source
    verified = set(verified_replacements)
    chosen = []
    gaps = []
    for window_id, versions in by_window.items():
        if len(versions) > MAX_ATTEMPTS:
            gaps.append({"window_id": window_id,
                         "reason": "retry_chain_too_long",
                         "detail": "more than %d attempts" % MAX_ATTEMPTS})
        latest_version = max(versions)
        latest = versions[latest_version]
        known = set(versions)
        missing = []
        for old in latest.supersedes:
            if old in known:
                continue
            if (window_id, old, latest_version) in verified:
                # The replacement was applied at a retractable boundary.
                continue
            missing.append(old)
        if missing:
            gaps.append({"window_id": window_id,
                         "reason": "unverifiable_supersession",
                         "detail": "superseded attempts not observed: %s"
                                   % sorted(missing)})
        chosen.append(latest)
    return chosen, gaps


def _collapse_legacy_gaps(gaps):
    """One canonical legacy gap per window, regardless of merge depth."""
    seen = set()
    out = []
    for gap in gaps:
        if gap["reason"] != "legacy_tdigest_no_certificate":
            out.append(gap)
            continue
        if gap["window_id"] in seen:
            continue
        seen.add(gap["window_id"])
        out.append({
            "window_id": gap["window_id"],
            "reason": "legacy_tdigest_no_certificate",
            "detail": "legacy TDigest.to_dict window has no raw data and no "
                      "error certificate",
        })
    return out


def _dedupe_gaps(gaps):
    seen = set()
    out = []
    for gap in gaps:
        key = (gap["window_id"], gap["reason"], gap.get("detail"))
        if key not in seen:
            seen.add(key)
            out.append(gap)
    return out


def _dedupe_replacements(replacements):
    seen = set()
    out = []
    for rep in replacements:
        key = (rep["window_id"], rep["old_version"], rep["new_version"])
        if key not in seen:
            seen.add(key)
            out.append(rep)
    return out


def _dedupe_strings(values):
    return sorted(set(values))


def _hash_json(payload):
    body = {k: v for k, v in payload.items() if k != "certificate_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()
