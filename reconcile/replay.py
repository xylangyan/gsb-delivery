"""
Replay one round of operations against the real OrderedSet, once per
transcript.

Each batch of goods is declared as a compressed Segment. A transcript
records, per batch, whether the batch is handed over as a reusable list
(LIST) or as a one-shot stream (STREAM). The two transcripts of a round
differ only in these form flags; item-numbers and their order of
appearance are the same.

Every op is executed inside its own try/except: the old program catches a
single error and continues, and whatever an op changed before raising
stays changed. Query ops record the original returned index/value or the
original exception. Op arguments may reference an earlier op's receipt
(Receipt) — the old ledger used update() receipts directly as the index of
a following pop or position read — and may reference the round's segments
repeatedly.

Provenance is tracked by delivering Tagged values into the OrderedSet.
Tagged compares and hashes exactly like the wrapped value, so library
semantics are untouched; the origin rides along on the page.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ordered_set import OrderedSet

from .page import INITIAL_OP, Origin, Page, PagePart
from .segments import Materialized, Segment, coerce_segment

LIST = "list"
STREAM = "stream"


class Tagged:
    """A value carrying its Origin. Equality and hash ignore the origin, so
    an OrderedSet of Tagged behaves exactly like an OrderedSet of the raw
    values."""

    __slots__ = ("value", "origin")

    def __init__(self, value: Any, origin: Origin):
        self.value = value
        self.origin = origin

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Tagged):
            return self.value == other.value
        return self.value == other

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(self.value)

    def __repr__(self) -> str:
        return f"Tagged({self.value!r}, {self.origin!r})"


def unwrap(value):
    if isinstance(value, Tagged):
        return value.value
    return value


# --- operation program -------------------------------------------------


@dataclass(frozen=True)
class Receipt:
    """An op argument that resolves to an earlier op's receipt."""

    op: int


@dataclass(frozen=True)
class Update:
    batch: str


@dataclass(frozen=True)
class SymmetricDifferenceUpdate:
    batch: str


@dataclass(frozen=True)
class IntersectionUpdate:
    batch: str


@dataclass(frozen=True)
class DifferenceUpdate:
    batches: Tuple[str, ...]


@dataclass(frozen=True)
class Add:
    value: Any


@dataclass(frozen=True)
class Pop:
    index: Any = -1  # int or Receipt


@dataclass(frozen=True)
class Discard:
    value: Any


@dataclass(frozen=True)
class Remove:
    value: Any


@dataclass(frozen=True)
class IndexQuery:
    key: Any  # a single key, or a list of keys


@dataclass(frozen=True)
class GetItem:
    index: Any  # int, Receipt, list of ints/Receipts, or slice (steps allowed)


@dataclass(frozen=True)
class Clear:
    pass


@dataclass
class OpRecord:
    """What one op did in one transcript: either a receipt or the original
    exception it raised."""

    op: object
    receipt: Any = None
    error: Optional[Exception] = None


@dataclass
class Transcript:
    records: List[OpRecord]
    page: Page
    items: List[Any] = field(default_factory=list)  # raw values on the final page

    @property
    def receipts(self) -> List[Any]:
        return [record.receipt for record in self.records]


class _Error:
    """Sentinel receipt for an op that raised."""

    def __init__(self, error: Exception):
        self.error = error


class _TaggingSequence:
    """LIST form: re-iterable; every pass yields the whole batch, tagged
    with the consuming op's id."""

    def __init__(self, segment: Segment, op_id: int):
        self.segment = segment
        self.op_id = op_id

    def __len__(self) -> int:
        return len(self.segment)

    def __iter__(self):
        op_id = self.op_id
        for pos, value in enumerate(self.segment):
            yield Tagged(value, Origin(op_id, pos))


def _tagging_stream(shared: Iterator[Tuple[int, Any]], op_id: int):
    """STREAM form: wraps the batch's shared one-shot iterator; consumption
    continues where the previous op left off."""
    for pos, value in shared:
        yield Tagged(value, Origin(op_id, pos))


def replay(
    program: Sequence,
    batches: Dict[str, Any],
    forms: Dict[str, str],
    initial=(),
) -> Transcript:
    """
    Run `program` against a fresh OrderedSet preloaded with `initial`.

    `batches` maps batch names to Segments (or plain iterables, coerced to
    Materialized). `forms` maps batch names to LIST or STREAM for this
    transcript. Returns the op records and the compressed final page.
    """
    segments = {name: coerce_segment(values) for name, values in batches.items()}
    oset: OrderedSet = OrderedSet()
    deliveries: Dict[int, Segment] = {INITIAL_OP: Materialized(tuple(unwrap(v) for v in initial))}
    for pos, value in enumerate(deliveries[INITIAL_OP].values):
        oset.add(Tagged(value, Origin(INITIAL_OP, pos)))

    streams: Dict[str, Iterator[Tuple[int, Any]]] = {}

    def batch_arg(name: str, op_id: int):
        segment = segments[name]
        deliveries[op_id] = segment
        if forms.get(name, LIST) == STREAM:
            shared = streams.setdefault(name, enumerate(segment.stream()))
            return _tagging_stream(shared, op_id)
        return _TaggingSequence(segment, op_id)

    def resolve(arg):
        if isinstance(arg, Receipt):
            receipt = receipts[arg.op]
            if isinstance(receipt, _Error):
                raise RuntimeError(f"receipt of op {arg.op} is an error: {receipt.error!r}")
            return receipt
        return arg

    records: List[OpRecord] = []
    receipts: List[Any] = []

    for op_id, op in enumerate(program):
        try:
            if isinstance(op, Update):
                receipt = oset.update(batch_arg(op.batch, op_id))
            elif isinstance(op, SymmetricDifferenceUpdate):
                receipt = oset.symmetric_difference_update(batch_arg(op.batch, op_id))
            elif isinstance(op, IntersectionUpdate):
                receipt = oset.intersection_update(batch_arg(op.batch, op_id))
            elif isinstance(op, DifferenceUpdate):
                receipt = oset.difference_update(
                    *(batch_arg(name, op_id) for name in op.batches)
                )
            elif isinstance(op, Add):
                value = resolve(op.value)
                deliveries[op_id] = Materialized((value,))
                receipt = oset.add(Tagged(value, Origin(op_id, 0)))
            elif isinstance(op, Pop):
                receipt = unwrap(oset.pop(resolve(op.index)))
            elif isinstance(op, Discard):
                receipt = oset.discard(resolve(op.value))
            elif isinstance(op, Remove):
                receipt = oset.remove(resolve(op.value))
            elif isinstance(op, IndexQuery):
                receipt = oset.index(op.key)
            elif isinstance(op, GetItem):
                index = op.index
                if isinstance(index, list):
                    index = [resolve(i) for i in index]
                else:
                    index = resolve(index)
                result = oset[index]
                if isinstance(result, OrderedSet):
                    receipt = OrderedSet(unwrap(v) for v in result)
                elif isinstance(result, list):
                    receipt = [unwrap(v) for v in result]
                else:
                    receipt = unwrap(result)
            elif isinstance(op, Clear):
                receipt = oset.clear()
            else:
                raise TypeError(f"unknown op {op!r}")
            records.append(OpRecord(op, receipt=receipt))
            receipts.append(receipt)
        except Exception as exc:
            records.append(OpRecord(op, error=exc))
            receipts.append(_Error(exc))

    page = compress_page(list(oset.items), deliveries)
    items = [unwrap(v) for v in oset.items]
    return Transcript(records=records, page=page, items=items)


def compress_page(tagged_items: List[Tagged], deliveries: Dict[int, Segment]) -> Page:
    """
    Compress a replayed page into PageParts.

    Consecutive items delivered by the same op at consecutive delivery
    positions collapse into one part that references a slice of the
    delivering segment — no new record per expanded item-number.
    """
    parts: List[PagePart] = []
    if not tagged_items:
        return Page(parts)
    run_start = 0
    for i in range(1, len(tagged_items) + 1):
        prev = tagged_items[i - 1].origin
        if i < len(tagged_items):
            cur = tagged_items[i].origin
            if cur.op == prev.op and cur.pos == prev.pos + 1:
                continue
        op, base = prev.op, tagged_items[run_start].origin.pos
        length = i - run_start
        segment = deliveries.get(op)
        if segment is not None:
            part_segment = segment.sub(base, base + length)
        else:
            part_segment = Materialized(
                tuple(unwrap(v) for v in tagged_items[run_start:i])
            )
        parts.append(PagePart(part_segment, op, base))
        run_start = i
    return Page(parts)
