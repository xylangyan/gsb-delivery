"""Incremental, versioned weighted MinHash index for corrected term counts.

The regular :class:`datasketch.WeightedMinHashGenerator` computes a signature
from a complete frequency vector.  This module keeps the *same* signature
arithmetic while accepting sparse positive/negative corrections for a fixed
vocabulary.  Only the documents and terms mentioned by a correction batch are
touched.

Design notes
------------
* Every document keeps, for every active term and every sample, the two values
  used by :meth:`WeightedMinHashGenerator.minhash` (``t`` and ``ln_a``).  A
  per-sample lazy heap stores ``(ln_a, term, version)``.  A correction therefore
  costs ``O(sample_size)`` score recomputation for the changed term, plus heap
  pushes.  Terms that did not change are never rescanned.
* ``minhash`` uses float32 arithmetic and ``numpy.argmin``.  The per-term
  computation here deliberately uses the exact same operation order, dtype and
  ``(score, term)`` tie-break, so an incrementally maintained signature is
  bit-identical to calling ``minhash`` on the aggregated frequency vector.
* A successful batch publishes one immutable :class:`IndexSnapshot`.  Readers
  that already hold an older snapshot continue to see exactly that version.
* Batches are identified by ``batch_id``.  Re-submitting the same identifier
  with the same payload returns the original result and never applies deltas
  twice.  A failed batch leaves the visible state, the event log and the batch
  registry untouched.
"""

from __future__ import annotations

import hashlib
import heapq
import threading
from dataclasses import dataclass, replace
from typing import (
    Any,
    Dict,
    Hashable,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import numpy as np

from datasketch.weighted_minhash import WeightedMinHash, WeightedMinHashGenerator

__all__ = [
    "BatchIdConflictError",
    "BatchResult",
    "Correction",
    "CorrectionConflictError",
    "DeltaWeightedMinHashIndex",
    "IncrementalWeightedMinHashIndex",
    "IndexSnapshot",
    "NegativeFrequencyError",
    "Retraction",
    "UnknownVersionError",
    "UnknownWordError",
    "VersionedWeightedMinHashIndex",
    "WeightedMinHashCorrectionIndex",
    "WeightedMinHashIndexError",
]


class WeightedMinHashIndexError(ValueError):
    """Base class for errors raised by the incremental index."""


class UnknownWordError(WeightedMinHashIndexError):
    """A term is not part of the fixed vocabulary."""


class NegativeFrequencyError(WeightedMinHashIndexError):
    """Applying a batch would make an aggregated term frequency negative.

    The whole batch is rejected before any document becomes visible in a new
    version.  :attr:`negative` maps ``(doc_id, term)`` to the offending
    resulting count.
    """

    def __init__(self, negative: Mapping[Tuple[Hashable, Any], int]) -> None:
        self.negative = dict(negative)
        details = ", ".join(
            f"{doc!r}/{term!r}={count}"
            for (doc, term), count in sorted(
                self.negative.items(), key=lambda item: (repr(item[0][0]), repr(item[0][1]))
            )
        )
        super().__init__(f"correction batch would produce negative frequencies: {details}")


class CorrectionConflictError(WeightedMinHashIndexError):
    """The same correction identifier was reused with a different payload."""


class BatchIdConflictError(WeightedMinHashIndexError):
    """The same batch identifier was reused with a different payload."""


class UnknownVersionError(WeightedMinHashIndexError, KeyError):
    """The requested published index version is not retained."""


@dataclass(frozen=True)
class Correction:
    """One positive or negative contribution to a document term's frequency.

    Args:
        correction_id: Stable identifier of this correction.  A retraction
            refers to it.  Re-using an identifier with a different payload is
            an error.
        doc_id: Document identifier.
        word: Vocabulary term (or its integer index when no named vocabulary is
            configured).
        delta: Signed integer contribution.  Positive adds, negative subtracts.
        sequence: Optional business effective-order key.  Together with
            ``shard`` and ``correction_id`` it defines a deterministic replay
            order.  Deltas are additive, so the visible result is independent
            of arrival order; the sequence is retained for audit/replay.
        shard: Optional source shard identifier, used as an ordering tie-break.

    """

    correction_id: Hashable
    doc_id: Hashable
    word: Any
    delta: int
    sequence: Any = None
    shard: Any = None

    def __post_init__(self) -> None:
        if isinstance(self.delta, bool) or not isinstance(self.delta, (int, np.integer)):
            raise TypeError("delta must be an integer")


@dataclass(frozen=True)
class Retraction:
    """Retract a correction by identifier.

    A retraction is terminal: once recorded, the referenced correction never
    contributes to a new version, even if the retraction arrives before the
    correction itself and even if the correction arrives in a later batch.

    Args:
        correction_id: Identifier of the correction being retracted.
        sequence: Optional business effective-order key.
        shard: Optional source shard identifier.

    """

    correction_id: Hashable
    sequence: Any = None
    shard: Any = None

    @property
    def target_id(self) -> Hashable:
        """Alias for the retracted correction identifier."""
        return self.correction_id


@dataclass(frozen=True)
class BatchResult:
    """Outcome of an idempotent correction batch.

    Args:
        batch_id: Caller supplied batch identifier.
        version: Version that first contained this batch's effect.  For a
            replayed request this is the original version, not the current one.
        applied: ``True`` when the original request changed at least one
            aggregated frequency.
        changed_docs: Documents whose complete signature changed.
        removed_docs: Documents that left the index in this version.
        affected_docs: Documents whose aggregated frequencies changed.
        replayed: ``True`` when the result was served from the batch registry.

    """

    batch_id: Hashable
    version: int
    applied: bool
    changed_docs: Tuple[Hashable, ...] = ()
    removed_docs: Tuple[Hashable, ...] = ()
    affected_docs: Tuple[Hashable, ...] = ()
    replayed: bool = False


@dataclass(frozen=True)
class _StoredCorrection:
    correction_id: Hashable
    doc_id: Hashable
    term: int
    delta: int
    sequence: Any
    shard: Any

    @property
    def order_key(self) -> Tuple[Any, ...]:
        # Numeric and textual sequence keys must never be compared directly.
        if self.sequence is None:
            return (1, 0, 0.0, repr(self.shard), repr(self.correction_id))
        if isinstance(self.sequence, (int, float)) and not isinstance(self.sequence, bool):
            return (0, 0, float(self.sequence), repr(self.shard), repr(self.correction_id))
        return (0, 1, str(self.sequence), repr(self.shard), repr(self.correction_id))

    @property
    def content(self) -> Tuple[Any, ...]:
        return (self.doc_id, self.term, self.delta, self.sequence, self.shard)


class _DocState:
    """Mutable per-document incremental state.

    ``terms[term]`` is ``(ln_a, t)`` where both arrays have length
    ``sample_size`` and exactly mirror the intermediates of ``minhash``.
    """

    __slots__ = ("freq", "heaps", "signature", "terms", "versions")

    def __init__(self) -> None:
        self.freq: Dict[int, int] = {}
        self.terms: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
        self.versions: Dict[int, int] = {}
        self.heaps: List[List[Tuple[float, int, int]]] = []
        self.signature: Optional[WeightedMinHash] = None


def _signature_key(signature: WeightedMinHash) -> Tuple[int, Tuple[int, ...], bytes]:
    values = np.asarray(signature.hashvalues)
    return int(signature.seed), tuple(values.shape), values.tobytes()


class IndexSnapshot(Mapping):
    """An immutable, queryable published version of the index.

    Snapshots are persistent overlays: publishing a batch only records changed
    and removed documents, while older snapshots keep their own view.  They can
    safely be handed to readers that continue to query an older version.

    Instances behave like read-only mappings from ``doc_id`` to
    :class:`WeightedMinHash`.  Documents without a positive aggregated
    frequency are absent.
    """

    def __init__(
        self,
        version: int,
        base: Optional[IndexSnapshot] = None,
        changes: Optional[Mapping[Hashable, WeightedMinHash]] = None,
        removed: Iterable[Hashable] = (),
    ) -> None:
        self.version = version
        self._base = base
        self._changes = dict(changes or {})
        self._removed = frozenset(removed)
        self._lookup: Optional[Dict[Tuple[int, Tuple[int, ...], bytes], Tuple[Hashable, ...]]] = None
        self._len: Optional[int] = None
        self._memo: Dict[Hashable, Optional[WeightedMinHash]] = {}
        self._find_lock = threading.Lock()

    # -- mapping protocol -------------------------------------------------
    def _resolve(self, doc_id: Hashable) -> Optional[WeightedMinHash]:
        memoized = self._memo
        if doc_id in memoized:
            return memoized[doc_id]
        node: Optional[IndexSnapshot] = self
        value: Optional[WeightedMinHash] = None
        while node is not None:
            if doc_id in node._changes:
                value = node._changes[doc_id]
                break
            if doc_id in node._removed:
                value = None
                break
            if node is not self and doc_id in node._memo:
                value = node._memo[doc_id]
                break
            node = node._base
        if len(memoized) < 4096:
            memoized[doc_id] = value
        return value

    def __getitem__(self, doc_id: Hashable) -> WeightedMinHash:
        signature = self._resolve(doc_id)
        if signature is None:
            raise KeyError(doc_id)
        return signature

    def __contains__(self, doc_id: object) -> bool:
        return self._resolve(doc_id) is not None  # type: ignore[arg-type]

    def __iter__(self) -> Iterator[Hashable]:
        if self._base is not None:
            for doc_id in self._base:
                if doc_id not in self._changes and doc_id not in self._removed:
                    yield doc_id
        yield from self._changes

    def __len__(self) -> int:
        if self._len is None:
            self._len = sum(1 for _ in self)
        return self._len

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        state.pop("_find_lock", None)
        return state

    def __setstate__(self, state: Mapping[str, Any]) -> None:
        self.__dict__.update(state)
        self._find_lock = threading.Lock()

    def get(self, doc_id: Hashable, default: Any = None) -> Any:
        signature = self._resolve(doc_id)
        return default if signature is None else signature

    def to_dict(self) -> Dict[Hashable, WeightedMinHash]:
        """Return a plain dictionary copy of this version."""
        return dict(self.items())

    # -- recall helpers ---------------------------------------------------
    def signature_key(self, doc_id: Hashable) -> Optional[Tuple[int, Tuple[int, ...], bytes]]:
        """Return a hashable key of a document's complete signature."""
        signature = self._resolve(doc_id)
        return None if signature is None else _signature_key(signature)

    def find(self, signature: WeightedMinHash) -> Tuple[Hashable, ...]:
        """Return documents in this version with exactly the same signature."""
        if self._lookup is None:
            with self._find_lock:
                if self._lookup is None:
                    lookup: Dict[Tuple[int, Tuple[int, ...], bytes], List[Hashable]] = {}
                    for doc_id, value in self.items():
                        lookup.setdefault(_signature_key(value), []).append(doc_id)
                    self._lookup = {key: tuple(sorted(values, key=repr)) for key, values in lookup.items()}
        return self._lookup.get(_signature_key(signature), ())

    candidates = find


class IncrementalWeightedMinHashIndex:
    """Versioned weighted MinHash index maintained by sparse corrections.

    Args:
        vocab: Fixed vocabulary.  It may be an integer dimension (terms are
            then integer indices), a sequence of terms, or a mapping from term
            to index.  Vocabulary is fixed for the lifetime of the index.
        sample_size: Number of weighted MinHash samples.
        seed: Random seed for the weighted MinHash permutations.
        generator: Optional pre-built :class:`WeightedMinHashGenerator`.  If
            supplied, ``vocab`` may be omitted and the generator's ``dim``,
            ``sample_size`` and ``seed`` are used.
        max_history: Optional cap on retained snapshots.  Snapshots already
            held by readers remain valid even if no longer addressable by
            version number.

    The empty index has version ``0``.  :meth:`initialize` (or
    :meth:`add_documents`) loads the initial documents and publishes version 1.

    """

    def __init__(
        self,
        vocab: Union[int, Sequence[Any], Mapping[Any, int], None] = None,
        sample_size: int = 128,
        seed: int = 1,
        *,
        generator: Optional[WeightedMinHashGenerator] = None,
        max_history: Optional[int] = None,
        documents: Optional[Mapping[Hashable, Any]] = None,
    ) -> None:
        initial_documents = documents
        if generator is not None:
            if vocab is not None and not isinstance(vocab, int):
                raise ValueError("pass either a named vocab or a generator, not both")
            self._generator = generator
            self.dim = generator.dim
            self.sample_size = generator.sample_size
            self.seed = generator.seed
            self._term_to_index: Dict[Any, int] = {i: i for i in range(self.dim)}
            self._index_to_term: List[Any] = list(range(self.dim))
            self._named_vocab = False
        else:
            if vocab is None:
                raise ValueError("vocab or generator is required")
            if isinstance(vocab, int):
                if vocab <= 0:
                    raise ValueError("dim must be positive")
                self.dim = vocab
                self._term_to_index = {i: i for i in range(self.dim)}
                self._index_to_term = list(range(self.dim))
                self._named_vocab = False
            else:
                terms: List[Any]
                mapping: Dict[Any, int]
                if isinstance(vocab, Mapping):
                    mapping = dict(vocab)
                    if sorted(mapping.values()) != list(range(len(mapping))):
                        raise ValueError("vocab mapping indices must be unique and contiguous from 0")
                    terms = [None] * len(mapping)
                    for term, index in mapping.items():
                        terms[index] = term
                else:
                    terms = list(vocab)
                    mapping = {term: index for index, term in enumerate(terms)}
                    if len(mapping) != len(terms):
                        raise ValueError("vocab terms must be unique")
                self.dim = len(terms)
                self._term_to_index = mapping
                self._index_to_term = terms
                self._named_vocab = True
            self._generator = WeightedMinHashGenerator(self.dim, sample_size=sample_size, seed=seed)
            self.sample_size = sample_size
            self.seed = seed

        if self.sample_size < 1:
            raise ValueError("sample_size must be positive")
        self._lock = threading.RLock()
        self._docs: Dict[Hashable, _DocState] = {}
        self._base_freqs: Dict[Hashable, Dict[int, int]] = {}
        self._corrections: Dict[Hashable, _StoredCorrection] = {}
        self._corrections_by_doc: Dict[Hashable, Dict[Hashable, _StoredCorrection]] = {}
        self._retracted: Set[Hashable] = set()
        self._batch_registry: Dict[Hashable, Tuple[str, BatchResult]] = {}
        self._history: Dict[int, IndexSnapshot] = {0: IndexSnapshot(0)}
        self._snapshot = self._history[0]
        self._version = 0
        self._max_history = max_history
        self._initialized = False
        if initial_documents is not None:
            self.initialize(initial_documents)

    # -- public properties ------------------------------------------------
    @property
    def current_version(self) -> int:
        """Latest published version number."""
        return self._version

    @property
    def vocabulary(self) -> Tuple[Any, ...]:
        """Terms in index order."""
        return tuple(self._index_to_term)

    @property
    def versions(self) -> Tuple[int, ...]:
        """Published versions still addressable by :meth:`snapshot`."""
        return tuple(sorted(self._history))

    def term_index(self, word: Any) -> int:
        """Resolve a vocabulary term or integer index to its dimension."""
        if word in self._term_to_index:
            return self._term_to_index[word]
        if isinstance(word, (int, np.integer)) and not isinstance(word, bool):
            index = int(word)
            if 0 <= index < self.dim:
                return index
        raise UnknownWordError(f"term {word!r} is not in the fixed vocabulary")

    # -- initialization ---------------------------------------------------
    def initialize(self, documents: Mapping[Hashable, Any]) -> int:
        """Load initial documents and publish version 1.

        ``documents`` maps document identifiers to either a mapping from term
        to positive integer frequency, or a dense sequence/array of length
        ``dim``.  Zero entries are omitted.  This method may be called once on
        an empty index.
        """
        with self._lock:
            if self._initialized or self._version != 0 or self._docs:
                raise RuntimeError("index has already been initialized")
            built = self._build_documents(documents)
            new_states = {doc_id: self._reapply_corrections(doc_id, base) for doc_id, base in built.items()}
            self._docs.update(new_states)
            self._base_freqs.update(built)
            self._initialized = True
            changes = {doc_id: state.signature for doc_id, state in self._docs.items() if state.signature is not None}
            version = self._publish(changes, ())
            self._evict_history()
            return version

    def add_documents(self, documents: Mapping[Hashable, Any]) -> int:
        """Upsert documents as a new published version.

        Existing corrections remain effective.  Use this for initial/bulk
        loading; online changes should go through :meth:`apply_batch`.
        """
        with self._lock:
            built = self._build_documents(documents)
            new_states = {doc_id: self._reapply_corrections(doc_id, base) for doc_id, base in built.items()}
            self._docs.update(new_states)
            self._base_freqs.update(built)
            changes = {}
            removed = []
            for doc_id in built:
                signature = self._docs[doc_id].signature
                if signature is None:
                    if self._snapshot.get(doc_id) is not None:
                        removed.append(doc_id)
                else:
                    changes[doc_id] = signature
            self._initialized = True
            version = self._publish(changes, removed)
            self._evict_history()
            return version

    def add_document(self, doc_id: Hashable, frequencies: Any) -> int:
        """Upsert one document as a new published version."""
        return self.add_documents({doc_id: frequencies})

    # -- online corrections ----------------------------------------------
    def apply_batch(
        self,
        batch_id: Hashable,
        corrections: Iterable[Union[Correction, Mapping[str, Any]]] = (),
        retractions: Iterable[Union[Retraction, Hashable, Mapping[str, Any]]] = (),
        *,
        events: Optional[Iterable[Union[Correction, Retraction, Mapping[str, Any]]]] = None,
    ) -> BatchResult:
        """Atomically apply one idempotent correction batch.

        Args:
            batch_id: Caller supplied idempotency key.
            corrections: :class:`Correction` objects (or mappings with the same
                fields).
            retractions: :class:`Retraction` objects, correction identifiers,
                or mappings with a ``correction_id``/``target_id`` field.
            events: Optional mixed iterable of corrections and retractions.

        Returns:
            BatchResult: The original result, or the same result with
            ``replayed=True`` when this batch identifier and payload were
            already committed.

        Raises:
            NegativeFrequencyError: The batch is rejected in its entirety when
                any aggregated word frequency would become negative.
            BatchIdConflictError: ``batch_id`` was used with another payload.
            CorrectionConflictError: A correction identifier was reused with
                another payload.

        """
        with self._lock:
            normalized = self._normalize_events(corrections, retractions, events)
            digest = self._batch_digest(normalized)
            previous = self._batch_registry.get(batch_id)
            if previous is not None:
                previous_digest, previous_result = previous
                if previous_digest != digest:
                    raise BatchIdConflictError(f"batch {batch_id!r} was already committed with a different payload")
                return replace(previous_result, replayed=True)

            new_corrections, target_ids, corrections_to_retract = self._plan_events(normalized)
            net = self._net_changes(new_corrections, corrections_to_retract)
            self._validate_nonnegative(net)

            return self._commit_batch(
                batch_id=batch_id,
                digest=digest,
                new_corrections=new_corrections,
                target_ids=target_ids,
                net=net,
            )

    # Accept common alternative names.
    apply = apply_batch
    apply_corrections = apply_batch
    commit_batch = apply_batch
    ingest = apply_batch

    def __getstate__(self) -> Dict[str, Any]:
        state = self.__dict__.copy()
        state.pop("_lock", None)
        return state

    def __setstate__(self, state: Mapping[str, Any]) -> None:
        self.__dict__.update(state)
        self._lock = threading.RLock()

    def retract(
        self,
        batch_id: Hashable,
        correction_id: Hashable,
        *,
        sequence: Any = None,
        shard: Any = None,
    ) -> BatchResult:
        """Convenience wrapper for a batch containing one retraction."""
        return self.apply_batch(
            batch_id,
            retractions=(Retraction(correction_id, sequence=sequence, shard=shard),),
        )

    # -- querying ---------------------------------------------------------
    def snapshot(self, version: Optional[int] = None) -> IndexSnapshot:
        """Return an immutable snapshot of a published version.

        With ``version=None`` the current version is returned.  Older versions
        remain queryable while retained.
        """
        with self._lock:
            if version is None:
                return self._snapshot
            try:
                return self._history[version]
            except KeyError:
                raise UnknownVersionError(f"index version {version!r} is not retained") from None

    def at_version(self, version: int) -> IndexSnapshot:
        """Alias for :meth:`snapshot` with an explicit version."""
        return self.snapshot(version)

    def signature(
        self,
        doc_id: Hashable,
        version: Optional[int] = None,
    ) -> Optional[WeightedMinHash]:
        """Return a document's complete signature in a version.

        Returns ``None`` when the document has no positive aggregated
        frequency and therefore is absent from that version.
        """
        return self.snapshot(version).get(doc_id)

    get_signature = signature
    query = signature

    def signature_at(self, version: int, doc_id: Hashable) -> Optional[WeightedMinHash]:
        """Explicit (version, document) argument order."""
        return self.signature(doc_id, version=version)

    def get(self, doc_id: Hashable, version: Optional[int] = None) -> Optional[WeightedMinHash]:
        return self.signature(doc_id, version=version)

    def find(
        self,
        signature: WeightedMinHash,
        version: Optional[int] = None,
    ) -> Tuple[Hashable, ...]:
        """Recall documents by complete signature in a version."""
        return self.snapshot(version).find(signature)

    candidates = find

    def document_ids(self, version: Optional[int] = None) -> Tuple[Hashable, ...]:
        """Sorted document identifiers present in a version."""
        return tuple(sorted(self.snapshot(version).keys(), key=repr))

    def frequencies(self, doc_id: Hashable) -> Dict[Any, int]:
        """Current aggregated positive frequencies, keyed by vocabulary term."""
        with self._lock:
            state = self._docs.get(doc_id)
            if state is None:
                raise KeyError(doc_id)
            return {self._index_to_term[term]: count for term, count in sorted(state.freq.items())}

    def effective_corrections(self) -> Tuple[Correction, ...]:
        """Corrections that currently contribute, in business replay order."""
        with self._lock:
            stored = [c for c in self._corrections.values() if c.correction_id not in self._retracted]
            stored.sort(key=lambda c: c.order_key)
            return tuple(self._as_correction(c) for c in stored)

    def replay(self) -> Dict[Hashable, Dict[Any, int]]:
        """Recompute aggregated frequencies by ordered replay of the event log.

        The result is useful for auditing and must equal the incrementally
        maintained state.  Only effective (non-retracted) corrections are
        replayed.
        """
        with self._lock:
            result: Dict[Hashable, Dict[int, int]] = {doc_id: dict(freqs) for doc_id, freqs in self._base_freqs.items()}
            for correction in sorted(
                (c for c in self._corrections.values() if c.correction_id not in self._retracted),
                key=lambda c: c.order_key,
            ):
                doc = result.setdefault(correction.doc_id, {})
                doc[correction.term] = doc.get(correction.term, 0) + correction.delta
                if doc[correction.term] == 0:
                    del doc[correction.term]
            return {
                doc_id: {self._index_to_term[term]: count for term, count in sorted(freqs.items()) if count > 0}
                for doc_id, freqs in result.items()
                if any(count > 0 for count in freqs.values())
            }

    # -- internals: document construction --------------------------------
    def _coerce_frequencies(self, frequencies: Any) -> Dict[int, int]:
        if isinstance(frequencies, Mapping):
            items = frequencies.items()
        else:
            values = frequencies if isinstance(frequencies, np.ndarray) else list(frequencies)
            if len(values) != self.dim:
                raise ValueError(f"frequency vector must have length {self.dim}")
            items = enumerate(values)
        result: Dict[int, int] = {}
        for word, count in items:
            term = self.term_index(word)
            if isinstance(count, bool) or not isinstance(count, (int, np.integer)):
                if isinstance(count, (float, np.floating)) and float(count).is_integer():
                    count = int(count)
                else:
                    raise TypeError("frequencies must be integers")
            count = int(count)
            if count < 0:
                raise NegativeFrequencyError({(("<document>", word)): count})
            if count > 0:
                result[term] = result.get(term, 0) + count
        return result

    def _build_documents(
        self,
        documents: Mapping[Hashable, Any],
    ) -> Dict[Hashable, Dict[int, int]]:
        built: Dict[Hashable, Dict[int, int]] = {}
        for doc_id, frequencies in documents.items():
            built[doc_id] = self._coerce_frequencies(frequencies)
        return built

    def _build_state(self, freq: Mapping[int, int]) -> _DocState:
        state = _DocState()
        state.heaps = [[] for _ in range(self.sample_size)]
        for term, count in freq.items():
            if count > 0:
                ln_a, t = self._term_scores(term, count)
                state.freq[term] = count
                version = state.versions.get(term, 0) + 1
                state.versions[term] = version
                state.terms[term] = (ln_a, t)
                for sample in range(self.sample_size):
                    state.heaps[sample].append((float(ln_a[sample]), term, version))
        for heap in state.heaps:
            heapq.heapify(heap)
        state.signature = self._signature_from_state(state)
        return state

    def _reapply_corrections(
        self,
        doc_id: Hashable,
        base: Mapping[int, int],
    ) -> _DocState:
        freq = dict(base)
        for correction in sorted(
            self._corrections_by_doc.get(doc_id, {}).values(),
            key=lambda c: c.order_key,
        ):
            if correction.correction_id in self._retracted:
                continue
            freq[correction.term] = freq.get(correction.term, 0) + correction.delta
        freq = {term: count for term, count in freq.items() if count > 0}
        return self._build_state(freq)

    # -- internals: exact score arithmetic --------------------------------
    def _term_scores(self, term: int, count: int) -> Tuple[np.ndarray, np.ndarray]:
        """Replicate ``WeightedMinHashGenerator.minhash`` for one term.

        The operation order, float32 dtype and broadcasting are intentionally
        identical to ``minhash`` so the resulting scores compare bit-for-bit.
        """
        rs = self._generator.rs[:, term]
        betas = self._generator.betas[:, term]
        ln_cs = self._generator.ln_cs[:, term]
        value = np.float32(np.log(np.array([count], dtype=np.float32))[0])
        t = np.floor(value / rs + betas)
        ln_y = (t - betas) * rs
        ln_a = ln_cs - ln_y - rs
        return ln_a, t

    def _signature_from_state(self, state: _DocState) -> Optional[WeightedMinHash]:
        if not state.freq:
            return None
        hashvalues = np.zeros((self.sample_size, 2), dtype=int)
        for sample, heap in enumerate(state.heaps):
            while heap:
                _, term, version = heap[0]
                if state.versions.get(term) == version and term in state.terms:
                    break
                heapq.heappop(heap)
            if not heap:
                return None
            term = heap[0][1]
            t = state.terms[term][1]
            hashvalues[sample, 0] = term
            hashvalues[sample, 1] = t[sample]
            # Bound heap growth: once stale entries dominate, rebuild from the
            # live term scores.  Live terms are few (sparse document state).
            if len(heap) > 4 * max(len(state.terms), 1) + 16:
                state.heaps[sample] = [
                    (float(ln_a[sample]), live_term, state.versions[live_term])
                    for live_term, (ln_a, _) in state.terms.items()
                ]
                heapq.heapify(state.heaps[sample])
        return WeightedMinHash(self.seed, hashvalues)

    # -- internals: batch planning ---------------------------------------
    def _normalize_events(
        self,
        corrections: Iterable[Union[Correction, Mapping[str, Any]]],
        retractions: Iterable[Union[Retraction, Hashable, Mapping[str, Any]]],
        events: Optional[Iterable[Union[Correction, Retraction, Mapping[str, Any]]]],
    ) -> Tuple[Dict[Hashable, _StoredCorrection], Dict[Hashable, Tuple[Any, Any]]]:
        corrections_by_id: Dict[Hashable, _StoredCorrection] = {}
        retraction_targets: Dict[Hashable, Tuple[Any, Any]] = {}

        def add_correction(raw: Union[Correction, Mapping[str, Any]]) -> None:
            if isinstance(raw, Mapping):
                raw = Correction(**raw)
            if not isinstance(raw, Correction):
                raise TypeError("corrections must be Correction objects or mappings")
            stored = _StoredCorrection(
                correction_id=raw.correction_id,
                doc_id=raw.doc_id,
                term=self.term_index(raw.word),
                delta=int(raw.delta),
                sequence=raw.sequence,
                shard=raw.shard,
            )
            existing = corrections_by_id.get(raw.correction_id)
            if existing is not None and existing.content != stored.content:
                raise CorrectionConflictError(f"correction {raw.correction_id!r} appears twice with different payloads")
            corrections_by_id[raw.correction_id] = stored

        def add_retraction(raw: Union[Retraction, Hashable, Mapping[str, Any]]) -> None:
            if isinstance(raw, Mapping):
                if "correction_id" in raw:
                    raw = Retraction(**raw)
                elif "target_id" in raw:
                    raw = Retraction(
                        correction_id=raw["target_id"],
                        sequence=raw.get("sequence"),
                        shard=raw.get("shard"),
                    )
                else:
                    raise TypeError("retraction mapping needs 'correction_id' or 'target_id'")
            elif not isinstance(raw, Retraction):
                raw = Retraction(correction_id=raw)
            retraction_targets.setdefault(raw.correction_id, (raw.sequence, raw.shard))

        for item in corrections:
            add_correction(item)
        for item in retractions:
            add_retraction(item)
        if events is not None:
            for item in events:
                if isinstance(item, Retraction):
                    add_retraction(item)
                elif isinstance(item, Mapping):
                    if "doc_id" in item and "word" in item and "delta" in item:
                        add_correction(item)
                    else:
                        add_retraction(item)
                elif isinstance(item, Correction):
                    add_correction(item)
                else:
                    raise TypeError("events must contain Correction or Retraction values")
        return corrections_by_id, retraction_targets

    def _batch_digest(
        self,
        normalized: Tuple[Dict[Hashable, _StoredCorrection], Dict[Hashable, Tuple[Any, Any]]],
    ) -> str:
        corrections, retractions = normalized
        payload = (
            tuple(
                sorted(
                    (
                        repr(c.correction_id),
                        repr(c.doc_id),
                        c.term,
                        c.delta,
                        repr(c.sequence),
                        repr(c.shard),
                    )
                    for c in corrections.values()
                )
            ),
            tuple(sorted((repr(cid), repr(seq), repr(shard)) for cid, (seq, shard) in retractions.items())),
        )
        return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()

    def _plan_events(
        self,
        normalized: Tuple[Dict[Hashable, _StoredCorrection], Dict[Hashable, Tuple[Any, Any]]],
    ) -> Tuple[Dict[Hashable, _StoredCorrection], Set[Hashable], List[_StoredCorrection]]:
        corrections, retractions = normalized
        new_corrections: Dict[Hashable, _StoredCorrection] = {}
        for correction_id, correction in corrections.items():
            existing = self._corrections.get(correction_id)
            if existing is not None:
                if existing.content != correction.content:
                    raise CorrectionConflictError(
                        f"correction {correction_id!r} was already committed with a different payload"
                    )
                continue
            new_corrections[correction_id] = correction

        target_ids: Set[Hashable] = set()
        corrections_to_retract: List[_StoredCorrection] = []
        for target in retractions:
            target_ids.add(target)
            if target in self._retracted:
                continue
            existing = self._corrections.get(target)
            if existing is not None:
                corrections_to_retract.append(existing)
        # A retraction is terminal.  Corrections arriving after (or together
        # with) their retraction never become effective, even if the
        # retraction arrived first.
        for correction_id in list(new_corrections):
            if correction_id in self._retracted or correction_id in target_ids:
                del new_corrections[correction_id]
        return new_corrections, target_ids, corrections_to_retract

    def _net_changes(
        self,
        new_corrections: Mapping[Hashable, _StoredCorrection],
        corrections_to_retract: Sequence[_StoredCorrection],
    ) -> Dict[Tuple[Hashable, int], int]:
        net: Dict[Tuple[Hashable, int], int] = {}
        for correction in new_corrections.values():
            key = (correction.doc_id, correction.term)
            net[key] = net.get(key, 0) + correction.delta
        for correction in corrections_to_retract:
            key = (correction.doc_id, correction.term)
            net[key] = net.get(key, 0) - correction.delta
        return {key: value for key, value in net.items() if value != 0}

    def _validate_nonnegative(self, net: Mapping[Tuple[Hashable, int], int]) -> None:
        negative: Dict[Tuple[Hashable, Any], int] = {}
        for (doc_id, term), delta in net.items():
            state = self._docs.get(doc_id)
            current = 0 if state is None else state.freq.get(term, 0)
            final = current + delta
            if final < 0:
                negative[(doc_id, self._index_to_term[term])] = final
        if negative:
            raise NegativeFrequencyError(negative)

    # -- internals: atomic commit ----------------------------------------
    def _commit_batch(
        self,
        *,
        batch_id: Hashable,
        digest: str,
        new_corrections: Mapping[Hashable, _StoredCorrection],
        target_ids: Set[Hashable],
        net: Mapping[Tuple[Hashable, int], int],
    ) -> BatchResult:
        touched: Dict[Hashable, List[Tuple[int, int]]] = {}
        for (doc_id, term), delta in net.items():
            touched.setdefault(doc_id, []).append((term, delta))

        undo_docs: List[
            Tuple[
                Hashable,
                _DocState,
                Optional[WeightedMinHash],
                bool,
                List[Tuple[int, int, Optional[Tuple[np.ndarray, np.ndarray]], int]],
            ]
        ] = []
        changes: Dict[Hashable, WeightedMinHash] = {}
        removed: List[Hashable] = []
        changed: List[Hashable] = []
        affected: List[Hashable] = []
        published_version: Optional[int] = None
        previous_snapshot = self._snapshot

        try:
            for doc_id in sorted(touched, key=repr):
                state = self._docs.get(doc_id)
                created = state is None
                if state is None:
                    state = self._build_state({})
                    self._docs[doc_id] = state
                old_signature = state.signature
                undo_terms: List[Tuple[int, int, Optional[Tuple[np.ndarray, np.ndarray]], int]] = []
                # Register the undo record before mutating so even a failure
                # halfway through one document is rolled back.
                undo_docs.append((doc_id, state, old_signature, created, undo_terms))
                for term, delta in sorted(touched[doc_id]):
                    old_freq = state.freq.get(term, 0)
                    undo_terms.append((term, old_freq, state.terms.get(term), state.versions.get(term, 0)))
                    self._set_term(state, term, old_freq + delta)
                new_signature = self._signature_from_state(state)
                state.signature = new_signature

                affected.append(doc_id)
                if new_signature is not None:
                    changes[doc_id] = new_signature
                elif old_signature is not None:
                    removed.append(doc_id)
                if new_signature != old_signature:
                    changed.append(doc_id)

            applied = bool(net)
            if applied:
                published_version = self._publish(changes, removed)

            result = BatchResult(
                batch_id=batch_id,
                version=published_version if applied else self._version,
                applied=applied,
                changed_docs=tuple(sorted(changed, key=repr)),
                removed_docs=tuple(sorted(removed, key=repr)),
                affected_docs=tuple(sorted(affected, key=repr)),
            )

            # The event log, retraction set and idempotency record are written
            # last.  A failure here rolls all three back together with the
            # document states and the published version.
            previous_retracted = set(self._retracted)
            previous_registry = self._batch_registry.get(batch_id)
            added_corrections: List[Hashable] = []
            added_bases: List[Hashable] = []
            try:
                for correction_id, correction in new_corrections.items():
                    self._corrections[correction_id] = correction
                    self._corrections_by_doc.setdefault(correction.doc_id, {})[correction_id] = correction
                    added_corrections.append(correction_id)
                    if correction.doc_id not in self._base_freqs:
                        self._base_freqs[correction.doc_id] = {}
                        added_bases.append(correction.doc_id)
                self._retracted.update(target_ids)
                self._batch_registry[batch_id] = (digest, result)
            except BaseException:
                for correction_id in added_corrections:
                    correction = self._corrections.pop(correction_id, None)
                    if correction is not None:
                        by_doc = self._corrections_by_doc.get(correction.doc_id)
                        if by_doc is not None:
                            by_doc.pop(correction_id, None)
                            if not by_doc:
                                self._corrections_by_doc.pop(correction.doc_id, None)
                for doc_id in added_bases:
                    self._base_freqs.pop(doc_id, None)
                self._retracted = previous_retracted
                if previous_registry is None:
                    self._batch_registry.pop(batch_id, None)
                else:
                    self._batch_registry[batch_id] = previous_registry
                raise
        except BaseException:
            for version in [v for v in self._history if v > previous_snapshot.version]:
                del self._history[version]
            self._snapshot = previous_snapshot
            self._version = previous_snapshot.version
            for doc_id, state, old_signature, created, undo_terms in reversed(undo_docs):
                for term, old_freq, old_term, old_version in reversed(undo_terms):
                    self._restore_term(state, term, old_freq, old_term, old_version)
                state.signature = old_signature
                if created:
                    self._docs.pop(doc_id, None)
            raise
        self._evict_history()
        return result

    def _set_term(self, state: _DocState, term: int, count: int) -> None:
        old_version = state.versions.get(term, 0)
        version = old_version + 1
        state.versions[term] = version
        if count <= 0:
            state.freq.pop(term, None)
            state.terms.pop(term, None)
            return
        ln_a, t = self._term_scores(term, count)
        state.freq[term] = count
        state.terms[term] = (ln_a, t)
        for sample, heap in enumerate(state.heaps):
            heapq.heappush(heap, (float(ln_a[sample]), term, version))

    def _restore_term(
        self,
        state: _DocState,
        term: int,
        old_freq: int,
        old_term: Optional[Tuple[np.ndarray, np.ndarray]],
        old_version: int,
    ) -> None:
        # Use a fresh version so entries pushed by the failed attempt are
        # ignored, while preserving the exact original scores.
        version = state.versions.get(term, old_version) + 1
        state.versions[term] = version
        if old_freq <= 0 or old_term is None:
            state.freq.pop(term, None)
            state.terms.pop(term, None)
            return
        state.freq[term] = old_freq
        state.terms[term] = old_term
        ln_a = old_term[0]
        for sample, heap in enumerate(state.heaps):
            heapq.heappush(heap, (float(ln_a[sample]), term, version))

    def _publish(
        self,
        changes: Mapping[Hashable, WeightedMinHash],
        removed: Iterable[Hashable],
    ) -> int:
        version = self._version + 1
        snapshot = IndexSnapshot(version, self._snapshot, changes, removed)
        self._history[version] = snapshot
        self._snapshot = snapshot
        self._version = version
        return version

    def _evict_history(self) -> None:
        if self._max_history is None or self._max_history <= 0:
            return
        oldest = self._version - self._max_history + 1
        for old in [v for v in self._history if v < oldest and v != 0]:
            del self._history[old]

    # -- internals: dataclass round-trip ---------------------------------
    def _as_correction(self, stored: _StoredCorrection) -> Correction:
        return Correction(
            correction_id=stored.correction_id,
            doc_id=stored.doc_id,
            word=self._index_to_term[stored.term] if self._named_vocab else stored.term,
            delta=stored.delta,
            sequence=stored.sequence,
            shard=stored.shard,
        )


# Common spellings for the same concept.
WeightedMinHashIndex = IncrementalWeightedMinHashIndex
VersionedWeightedMinHashIndex = IncrementalWeightedMinHashIndex
DeltaWeightedMinHashIndex = IncrementalWeightedMinHashIndex
WeightedMinHashCorrectionIndex = IncrementalWeightedMinHashIndex
