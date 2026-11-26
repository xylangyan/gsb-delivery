"""Replay the receipts produced by :meth:`pyformlang.fst.FST.translate`.

``FST.translate`` enumerates the translations of a word with a depth-first
walk.  The walk has a few properties that matter when the translations are
used as numbered receipts:

* edges are pushed in registration order and the stack is popped last in,
  first out (so the input edges of a state are considered before its epsilon
  edges are pushed, and the epsilon edges are popped first),
* a configuration is only expanded once for a given pair ``(remaining input,
  generated output)`` in a given state, and the first walk that reaches it
  wins,
* ``max_length`` is checked before an epsilon edge is followed, while the
  edge may emit several symbols and input edges are never limited.

This module keeps that exact enumeration.  Receipts are numbered from one in
the order in which the original walk yields them.  Enumeration is lazy:
asking for one receipt, a range, or a relocation count does not build a list
of every previous translation, and it never sorts by output.

The main entry points are :func:`replay`, :class:`TranslationReplay` and
:class:`VersionedReplay`.
"""
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple


_EPSILON = "epsilon"
_ANY_STATE = object()
_MASK = (1 << 64) - 1
_PRIME = 1099511628211
_OFFSET = 1469598103934665603


def _symbol_hash(symbol):
    try:
        return hash(symbol)
    except TypeError:
        # Unhashable symbols still contribute by content; equality itself is
        # decided by the receipt, not by the digest.
        return hash((type(symbol).__name__, repr(symbol)))


class _Output:
    """Persistent output word.

    A node stores one chunk of symbols, the total length, and a hash computed
    per symbol (so the hash does not depend on how the word was split into
    chunks).  Appending is O(len(chunk)) and shared prefixes are kept.  The
    traversal only ever keeps digests, so nodes do not accumulate.
    """

    __slots__ = ("previous", "chunk", "length", "digest")

    def __init__(self, previous, chunk, length, digest):
        self.previous = previous
        self.chunk = chunk
        self.length = length
        self.digest = digest

    def append(self, chunk):
        """Return a new word with ``chunk`` appended."""
        if not chunk:
            return self
        digest = self.digest
        for symbol in chunk:
            digest = ((digest ^ _symbol_hash(symbol)) * _PRIME) & _MASK
        return _Output(self, chunk, self.length + len(chunk), digest)

    def _iter_symbols(self):
        chunks = []
        node = self
        while node is not _EMPTY:
            chunks.append(node.chunk)
            node = node.previous
        for chunk in reversed(chunks):
            yield from chunk

    def to_tuple(self):
        """Materialize the word."""
        if self is _EMPTY:
            return ()
        return tuple(self._iter_symbols())

    def key(self):
        """A compact, comparable key for the word."""
        return self.digest, self.length

    def __len__(self):
        return self.length

    def __hash__(self):
        return self.digest

    def __eq__(self, other):
        if self is other:
            return True
        if not isinstance(other, _Output):
            return NotImplemented
        if self.length != other.length or self.digest != other.digest:
            return False
        return all(left == right for left, right in
                   zip(self._iter_symbols(), other._iter_symbols()))

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result


_EMPTY = _Output(None, (), 0, _OFFSET)


def _extend_digest(digest, symbols):
    """Hash ``symbols`` into ``digest`` one symbol at a time."""
    for symbol in symbols:
        digest = ((digest ^ _symbol_hash(symbol)) * _PRIME) & _MASK
    return digest


def _append_digest(state, chunk):
    """Append ``chunk`` to a ``(digest, length)`` output state."""
    return (_extend_digest(state[0], chunk), state[1] + len(chunk))


def _as_output(word) -> _Output:
    """Build an output node from a tuple/list/other iterable."""
    if isinstance(word, _Output):
        return word
    symbols = tuple(word)
    if not symbols:
        return _EMPTY
    return _Output(_EMPTY, symbols, len(symbols),
                   _extend_digest(_OFFSET, symbols))


@dataclass(frozen=True)
class TransitionSource:
    """The edge that led to a receipt.

    ``edge_index`` is the zero-based position of the edge in the list that
    was registered for ``(source_state, input_symbol)``.  ``edge_number`` is
    the same position counted from one.  ``kind`` is ``"input"`` or
    ``"epsilon"``.
    """

    source_state: Any
    input_symbol: Any
    target_state: Any
    output_symbols: Tuple[Any, ...]
    edge_index: int
    kind: str

    @property
    def edge_number(self):
        """One-based position of the edge in its registration list."""
        return self.edge_index + 1

    @property
    def output(self):
        """The symbols emitted by the edge, as a list."""
        return list(self.output_symbols)

    @property
    def transition(self):
        """The edge as ``(source, input, target, output)``."""
        return (self.source_state, self.input_symbol,
                self.target_state, list(self.output_symbols))

    @property
    def is_epsilon(self):
        """Whether this edge consumes no input."""
        return self.kind == "epsilon"


class _Path:
    """A shared, immutable derivation prefix.

    ``source`` is a plain ``(source_state, input_symbol, target_state,
    output_symbols, edge_index, kind)`` tuple.  The public
    :class:`TransitionSource` objects are only built when a receipt is
    materialized, which keeps the traversal cheap.
    """

    __slots__ = ("previous", "source", "depth")

    def __init__(self, previous, source):
        self.previous = previous
        self.source = source
        self.depth = 1 if previous is None else previous.depth + 1

    def last_source(self):
        """Return the transition that formed the word, as public object."""
        return TransitionSource(*self.source)

    def sources(self):
        """Return the transitions of the derivation, oldest first."""
        result = []
        node = self
        while node is not None:
            result.append(TransitionSource(*node.source))
            node = node.previous
        result.reverse()
        return result

    def to_tuple(self):
        """Return the transitions of the derivation, oldest first."""
        return tuple(self.sources())


@dataclass(frozen=True, eq=False)
class Receipt:
    """One translation yielded by the original walk.

    ``ordinal`` is one-based (the "receipt number"), ``index`` is the
    corresponding zero-based Python index.  ``symbols`` is the translated
    word as a tuple, while ``output`` and ``code`` return the same word as a
    list, the shape used by ``FST.translate``.  ``final_state`` is the state
    at which the receipt was accepted.

    ``provenance`` is the last transition of the first derivation that
    produced the receipt (the transition that formed it), and ``path`` is
    that whole derivation, oldest transition first.  ``path`` is built on
    demand; looking up a receipt does not materialize every step unless the
    derivation is requested.
    """

    ordinal: int
    index: int
    symbols: Tuple[Any, ...]
    final_state: Any
    provenance: Optional[TransitionSource]
    _derivation: Any = None

    @property
    def output(self):
        """The short code, as a list (the shape used by ``translate``)."""
        return list(self.symbols)

    @property
    def code(self):
        """Alias for ``output``."""
        return list(self.symbols)

    @property
    def output_tuple(self):
        """The short code as a tuple."""
        return self.symbols

    def __eq__(self, other):
        if isinstance(other, Receipt):
            return (self.ordinal, self.index, self.symbols,
                    self.final_state, self.provenance) == \
                (other.ordinal, other.index, other.symbols,
                 other.final_state, other.provenance)
        if isinstance(other, (list, tuple)):
            return list(self.symbols) == list(other)
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.ordinal, self.symbols, self.final_state))

    @property
    def end_state(self):
        """Alias for ``final_state``."""
        return self.final_state

    @property
    def source(self):
        """Alias for ``provenance``."""
        return self.provenance

    @property
    def path(self):
        """The whole derivation, oldest transition first."""
        if self._derivation is None:
            return ()
        return self._derivation.to_tuple()

    @property
    def sources(self):
        """Alias for ``path``."""
        return self.path

    @property
    def first_source(self):
        """The first transition of the derivation, or ``None``."""
        if self._derivation is None:
            return None
        node = self._derivation
        while node.previous is not None:
            node = node.previous
        return TransitionSource(*node.source)

    def __iter__(self):
        return iter(self.symbols)

    def __len__(self):
        return len(self.symbols)

    def __getitem__(self, item):
        return self.symbols[item]

    def __repr__(self):
        return ("Receipt(ordinal={!r}, index={!r}, symbols={!r}, "
                "final_state={!r})".format(
                    self.ordinal, self.index, self.symbols,
                    self.final_state))


@dataclass(frozen=True)
class RangeStats:
    """Result of comparing a range of old receipt numbers.

    ``moved`` counts receipts that exist in the new version but whose number
    changed.  ``missing`` counts receipts that the new version does not
    yield.  ``unchanged`` counts receipts that kept their number.
    """

    first: int
    last: int
    total: int
    moved: int
    missing: int
    unchanged: int

    @property
    def present(self):
        """Number of old receipts that are also in the new version."""
        return self.total - self.missing

    @property
    def relocated(self):
        """Alias for ``moved``."""
        return self.moved

    @property
    def not_delivered(self):
        """Alias for ``missing``."""
        return self.missing

    @property
    def moved_or_missing(self):
        """Number of receipts that moved or disappeared."""
        return self.moved + self.missing


class TranslationReplay:
    """Lazy, ordinal-addressable replay of ``FST.translate``.

    Parameters
    ----------
    fst : FST
        The transducer.
    input_word : iterable
        The word given to ``translate``.
    max_length : int
        The same limit that would be given to ``translate``.  ``-1`` means
        unlimited; any other value is compared with the number of symbols
        already written when an epsilon edge is considered.
    """

    def __init__(self, fst, input_word: Iterable[Any], max_length: int = -1):
        self._fst = fst
        self._input = tuple(input_word)
        self._max_length = max_length
        self._start_states = tuple(fst.start_states)
        self._final_states = frozenset(fst.final_states)
        self._states = tuple(fst.states)
        self._edges = {
            head: tuple((s_to, tuple(output))
                        for s_to, output in transitions)
            for head, transitions in fst.transitions.items()
        }
        self._key_index = {}
        self._code_index = {}
        self._indexed_generator = None
        self._indexed_exhausted = False
        self._indexed_count = 0

    @property
    def fst(self):
        """The transducer that is replayed."""
        return self._fst

    @property
    def input_word(self):
        """The input word as a tuple."""
        return self._input

    @property
    def max_length(self):
        """The registered length limit."""
        return self._max_length

    # ------------------------------------------------------------------
    # Core enumeration
    # ------------------------------------------------------------------
    def _iter_events(self, with_path=True, with_nodes=True):
        """Yield ``(output, state, path)`` in the original order.

        This is a line-by-line transcription of the original walk: the same
        stack, the same push order, and the same per-state deduplication.
        With ``with_path`` false the third item is always ``None``.  With
        ``with_nodes`` false the output is a compact ``(digest, length)``
        pair instead of a word, so counting and lookup do not build words at
        all.

        Deduplication uses the content digest of the generated word, which
        is extended once per emitted symbol and does not depend on how edges
        chunked the word.  Keeping digests rather than words avoids retaining
        a full word per visited configuration, which matters when an event
        template repeats very many times.
        """
        input_length = len(self._input)
        to_process = []
        seen_by_state = {state: set() for state in self._states}
        if with_nodes:
            empty = _EMPTY
            append = _Output.append
        else:
            empty = (_OFFSET, 0)
            append = _append_digest
        for start_state in self._start_states:
            to_process.append((0, empty, start_state, None))
        edges = self._edges
        input_word = self._input
        max_length = self._max_length
        while to_process:
            position, output, state, path = to_process.pop()
            seen = seen_by_state.get(state)
            if seen is None:
                seen = seen_by_state.setdefault(state, set())
            if with_nodes:
                digest = output.digest
                length = output.length
            else:
                digest, length = output
            key = (position, digest, length)
            if key in seen:
                continue
            seen.add(key)
            if position == input_length and state in self._final_states:
                yield (output, state, path)
            if position != input_length:
                symbol = input_word[position]
                for index, (target, chunk) in enumerate(
                        edges.get((state, symbol), ())):
                    if with_path:
                        next_path = _Path(
                            path, (state, symbol, target, chunk, index,
                                   "input"))
                    else:
                        next_path = None
                    to_process.append((position + 1, append(output, chunk),
                                       target, next_path))
            if max_length == -1 or length < max_length:
                for index, (target, chunk) in enumerate(
                        edges.get((state, _EPSILON), ())):
                    if with_path:
                        next_path = _Path(
                            path, (state, _EPSILON, target, chunk, index,
                                   "epsilon"))
                    else:
                        next_path = None
                    to_process.append((position, append(output, chunk),
                                       target, next_path))

    def _iter_numbered(self, with_path=True, with_nodes=True):
        for ordinal, event in enumerate(
                self._iter_events(with_path=with_path,
                                  with_nodes=with_nodes), 1):
            yield ordinal, event

    @staticmethod
    def _make_receipt(ordinal, event):
        output, state, path = event
        return Receipt(ordinal, ordinal - 1, output.to_tuple(), state,
                       path.last_source() if path is not None else None,
                       path)

    def __iter__(self):
        return self.iter_receipts()

    def translations(self):
        """Yield the short codes only, exactly like ``FST.translate``."""
        for _, event in self._iter_numbered(with_path=False):
            yield list(event[0].to_tuple())

    transliterations = translations

    # ------------------------------------------------------------------
    # Direct access
    # ------------------------------------------------------------------
    def iter_receipts(self, first: int = 1, last: Optional[int] = None):
        """Iterate receipts ``first``..``last`` (one-based, inclusive).

        Receipts before ``first`` are skipped by the traversal; they are
        never yielded and their codes are not materialized.  No list of all
        translations is built, and the order is the original one.
        """
        if first < 1:
            raise ValueError("first receipt number must be at least 1")
        if last is not None and last < first:
            return
        for ordinal, event in self._iter_numbered(with_path=True):
            if ordinal < first:
                continue
            if last is not None and ordinal > last:
                return
            yield self._make_receipt(ordinal, event)

    def receipts(self, first: int = 1, last: Optional[int] = None):
        """Return the receipts ``first``..``last`` as a list."""
        return list(self.iter_receipts(first, last))

    def receipt(self, ordinal: int):
        """Return one receipt by its one-based number."""
        if ordinal < 1:
            raise IndexError("receipt ordinals start at 1")
        for receipt in self.iter_receipts(ordinal, ordinal):
            return receipt
        raise IndexError(
            "receipt {} does not exist (only {} receipts)".format(
                ordinal, self.count()))

    def at(self, index: int):
        """Return one receipt by its zero-based Python index."""
        if index >= 0:
            return self.receipt(index + 1)
        total = self.count()
        if index < -total:
            raise IndexError("receipt index out of range")
        return self.receipt(total + index + 1)

    def get(self, ordinal: int, default=None):
        """Return a receipt, or ``default`` if the number does not exist."""
        try:
            return self.receipt(ordinal)
        except IndexError:
            return default

    def __getitem__(self, item):
        if isinstance(item, slice):
            step = 1 if item.step is None else item.step
            start = 0 if item.start is None else item.start
            stop = item.stop
            if start < 0 or stop is None or stop < 0:
                start, stop, step = item.indices(self.count())
            if step != 1:
                return [self.receipt(index + 1)
                        for index in range(start, stop, step)]
            return self.receipts(start + 1, stop)
        return self.at(item)

    def __len__(self):
        return self.count()

    def count(self):
        """Count all receipts."""
        self._drain_index()
        return self._indexed_count

    # ------------------------------------------------------------------
    # Incremental index over the enumeration
    # ------------------------------------------------------------------
    def _advance_index(self):
        if self._indexed_exhausted:
            return False
        if self._indexed_generator is None:
            self._indexed_generator = self._iter_numbered(
                with_path=False, with_nodes=False)
        try:
            ordinal, event = next(self._indexed_generator)
        except StopIteration:
            self._indexed_exhausted = True
            return False
        output, state, _ = event
        self._indexed_count = ordinal
        self._key_index.setdefault((output, state), ordinal)
        self._code_index.setdefault(output, ordinal)
        return True

    def _drain_index(self):
        while self._advance_index():
            pass

    def _raw_at(self, ordinal):
        if ordinal < 1:
            return None
        for current, event in self._iter_numbered(with_path=False):
            if current == ordinal:
                return event
        return None

    def ordinal_of_node(self, output, final_state):
        """Ordinal for an output word and final state, or ``None``."""
        if not isinstance(output, _Output):
            output = _as_output(output)
        key = output.key()
        while True:
            ordinal = self._key_index.get((key, final_state))
            if ordinal is not None:
                return ordinal
            if not self._advance_index():
                return None

    def ordinal_of(self, output: Iterable[Any],
                   final_state: Any = _ANY_STATE):
        """Reverse lookup by output word and, optionally, final state.

        With only an output word, the first receipt with that word in
        enumeration order is returned.  ``None`` means the receipt was not
        yielded.
        """
        node = _as_output(output)
        key = node.key()
        if final_state is _ANY_STATE:
            while True:
                ordinal = self._code_index.get(key)
                if ordinal is not None:
                    return ordinal
                if not self._advance_index():
                    return None
        while True:
            ordinal = self._key_index.get((key, final_state))
            if ordinal is not None:
                return ordinal
            if not self._advance_index():
                return None

    lookup = ordinal_of
    find = ordinal_of
    reverse_lookup = ordinal_of
    number_of = ordinal_of

    def receipt_at(self, ordinal: int):
        """Alias for :meth:`receipt` (one-based number)."""
        return self.receipt(ordinal)

    def locate(self, ordinal: int):
        """Alias for :meth:`receipt` (one-based number)."""
        return self.receipt(ordinal)

    def receipt_range(self, first: int = 1, last: Optional[int] = None):
        """Alias for :meth:`receipts`."""
        return self.receipts(first, last)

    def history_numbers(self, first: int = 1, last: Optional[int] = None):
        """Alias for :meth:`receipts`, returning numbers and codes.

        Yields ``(ordinal, code, final_state, provenance)`` tuples in the
        original enumeration order.
        """
        for receipt in self.iter_receipts(first, last):
            yield (receipt.ordinal, receipt.output, receipt.final_state,
                   receipt.provenance)

    def code_history(self, first: int = 1, last: Optional[int] = None):
        """Alias for :meth:`history_numbers`."""
        return self.history_numbers(first, last)

    def ordinals_for(self, output: Iterable[Any]):
        """All ordinals whose output word is ``output``, in order."""
        node = _as_output(output)
        result = []
        for ordinal, event in self._iter_numbered(with_path=False):
            if event[0] == node:
                result.append(ordinal)
        return result

    def ordinals_all(self, output: Iterable[Any], final_state: Any):
        """All ordinals with this code and end state, in order."""
        node = _as_output(output)
        result = []
        for ordinal, event in self._iter_numbered(with_path=False):
            if event[0] == node and event[1] == final_state:
                result.append(ordinal)
        return result

    def codes(self):
        """Map each output word to all of its ordinals, in order."""
        result = {}
        for ordinal, event in self._iter_numbered(with_path=False):
            result.setdefault(event[0].to_tuple(), []).append(ordinal)
        return result

    # ------------------------------------------------------------------
    # Neighbouring versions
    # ------------------------------------------------------------------
    def compared_to(self, other_max_length: int):
        """Build a :class:`VersionedReplay` with this as the old version."""
        return VersionedReplay(self, TranslationReplay(
            self._fst, self._input, other_max_length))

    def renumber(self, ordinal: int, other: "TranslationReplay"):
        """Ordinal of receipt ``ordinal`` in ``other``, or ``None``."""
        event = self._raw_at(ordinal)
        if event is None:
            raise IndexError("receipt {} does not exist".format(ordinal))
        return other.ordinal_of_node(event[0], event[1])

    # ------------------------------------------------------------------
    # Differential check
    # ------------------------------------------------------------------
    def iter_raw(self):
        """Yield ``(ordinal, (output_node, end_state, None))`` internally.

        A small hook used by :class:`VersionedReplay` to walk the enumeration
        without materializing codes or derivations.
        """
        return self._iter_numbered(with_path=False)

    def matches_translate(self, first: int = 1, last: Optional[int] = None):
        """Whether this replay agrees with ``FST.translate`` on a range.

        The original is enumerated with its own fixed edge order; nothing is
        sorted.  Intended for short words, where the original is affordable.
        """
        expected = list(self._fst.translate(list(self._input),
                                            self._max_length))
        if last is None:
            last = len(expected)
        if first < 1 or last > len(expected) or last < first:
            return False
        return expected[first - 1:last] == list(
            self.iter_codes(first, last))

    def iter_codes(self, first: int = 1, last: Optional[int] = None):
        """Yield codes (lists) for a receipt range, in enumeration order."""
        for receipt in self.iter_receipts(first, last):
            yield receipt.output


class VersionedReplay:
    """Two replays of the same events under different length limits."""

    def __init__(self, old: TranslationReplay, new: TranslationReplay):
        self.old = old
        self.new = new

    @classmethod
    def from_limits(cls, fst, input_word, old_max_length, new_max_length,
                    new_fst=None):
        """Build both replays; ``new_fst`` defaults to the same table."""
        old = TranslationReplay(fst, input_word, old_max_length)
        new = TranslationReplay(
            fst if new_fst is None else new_fst,
            input_word, new_max_length)
        return cls(old, new)

    def ordinals(self, output: Iterable[Any], final_state: Any):
        """Return ``(old_ordinal, new_ordinal)``; blanks are ``None``."""
        return (self.old.ordinal_of(output, final_state),
                self.new.ordinal_of(output, final_state))

    def lookup(self, output: Iterable[Any], final_state: Any):
        """Alias for :meth:`ordinals`."""
        return self.ordinals(output, final_state)

    by_code = lookup

    def old_ordinal(self, output, final_state=_ANY_STATE):
        """Receipt number in the old version, or ``None``."""
        return self.old.ordinal_of(output, final_state)

    def new_ordinal(self, output, final_state=_ANY_STATE):
        """Receipt number in the new version, or ``None``."""
        return self.new.ordinal_of(output, final_state)

    def renumber(self, old_ordinal: int):
        """New number of an old receipt, or ``None`` if it disappeared.

        A ``(code, end state)`` pair identifies at most one receipt in each
        version, because the original walk deduplicates those configurations.
        """
        event = self.old._raw_at(old_ordinal)  # pylint: disable=W0212
        if event is None:
            raise IndexError(
                "receipt {} does not exist".format(old_ordinal))
        return self.new.ordinal_of_node(event[0], event[1])

    def renumber_all(self, first: int = 1, last: Optional[int] = None):
        """Map each old ordinal in the range to its new ordinal (or None)."""
        if first < 1:
            raise ValueError("first receipt number must be at least 1")
        result = {}
        for ordinal, event in self.old.iter_raw():  # pylint: disable=W0212
            if ordinal < first:
                continue
            if last is not None and ordinal > last:
                break
            result[ordinal] = self.new.ordinal_of_node(event[0], event[1])
        return result

    def range_stats(self, first: int, last: int) -> RangeStats:
        """Count moved and missing receipts in an old-number range.

        Only the requested range is walked on the old side; earlier receipts
        are skipped by the traversal and their codes are never materialized.
        """
        if first < 1:
            raise ValueError("first receipt number must be at least 1")
        if last < first:
            return RangeStats(first, last, 0, 0, 0, 0)
        total = moved = missing = unchanged = 0
        for ordinal, event in self.old.iter_raw():
            if ordinal < first:
                continue
            if ordinal > last:
                break
            total += 1
            new_ordinal = self.new.ordinal_of_node(event[0], event[1])
            if new_ordinal is None:
                missing += 1
            elif new_ordinal != ordinal:
                moved += 1
            else:
                unchanged += 1
        return RangeStats(first, last, total, moved, missing, unchanged)

    def moved_or_missing(self, first: int, last: int) -> int:
        """Number of old receipts in the range that moved or disappeared."""
        return self.range_stats(first, last).moved_or_missing

    count_moved = moved_or_missing
    count_changed = moved_or_missing

    def number_map(self, first: int = 1, last: Optional[int] = None):
        """Alias for :meth:`renumber_all`."""
        return self.renumber_all(first, last)

    def locate(self, old_ordinal: int):
        """Alias for :meth:`renumber`, returning ``(old, new)``."""
        return old_ordinal, self.renumber(old_ordinal)

    def both_ordinals(self, output: Iterable[Any], final_state: Any):
        """Alias for :meth:`ordinals`."""
        return self.ordinals(output, final_state)

    reverse_lookup = ordinals
    number_in_old = old_ordinal
    number_in_new = new_ordinal

    def matches_translate(self, first: int = 1, last: Optional[int] = None):
        """Whether both versions agree with ``FST.translate``."""
        return (self.old.matches_translate(first, last) and
                self.new.matches_translate(first, last))


def replay(fst, input_word: Iterable[Any], max_length: int = -1):
    """Create a :class:`TranslationReplay`."""
    return TranslationReplay(fst, input_word, max_length)


def compare_replays(fst, input_word, old_max_length, new_max_length,
                    new_fst=None):
    """Create a :class:`VersionedReplay` for two length limits."""
    return VersionedReplay.from_limits(
        fst, input_word, old_max_length, new_max_length, new_fst)


def assert_matches_translate(fst, input_word, max_length=-1,
                             first=1, last=None):
    """Differential check of a replay against ``FST.translate``.

    The original is enumerated with its own registration order.  This is
    meant for short words only.  Returns the replay when both agree and
    raises ``AssertionError`` otherwise.
    """
    replay_ = TranslationReplay(fst, input_word, max_length)
    expected = list(fst.translate(list(replay_.input_word), max_length))
    if last is None:
        last = len(expected)
    if last > len(expected):
        raise AssertionError(
            "replay has receipt {} but translate yielded {}".format(
                last, len(expected)))
    actual = list(replay_.iter_codes(first, last))
    if actual != expected[first - 1:last]:
        raise AssertionError(
            "replay diverges from translate between {} and {}".format(
                first, last))
    return replay_


# Friendly aliases.
build_replay = replay
compare = compare_replays
differential = assert_matches_translate
TranslationReceipt = Receipt
Replay = TranslationReplay
Ledger = VersionedReplay


__all__ = [
    "Receipt",
    "TranslationReceipt",
    "Replay",
    "TranslationReplay",
    "TransitionSource",
    "RangeStats",
    "VersionedReplay",
    "Ledger",
    "replay",
    "build_replay",
    "compare_replays",
    "compare",
    "assert_matches_translate",
    "differential",
]
