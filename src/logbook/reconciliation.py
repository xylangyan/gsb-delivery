"""
logbook.reconciliation
~~~~~~~~~~~~~~~~~~~~~~

Offline reconciliation for buffered handler output.

Buffered handlers such as :class:`~logbook.GroupHandler` and
:class:`~logbook.FingersCrossedHandler` can hand the same original record to
an inner handler more than once.  A group is flushed after a failure and the
next attempt starts from its first record again; an escalation flushes the
records that led up to it and then forwards the triggering record.  When the
same records are replayed under different delivery quotas, an auditor needs to
know which original numbers were really delivered, how often, and where the
two runs first disagree.

This module models that problem without inventing a new continuation rule.
The archive says whether an outer group is used again after a failure.  A
:class:`QuotaProfile` describes, per tag, the balance limit, the starting
balance and the amount a record category adds or removes.  A
:class:`DeliverySimulator` walks the candidates in archive order and applies
the quota exactly as a receiver would:

* the new balance is calculated first;
* if it is below zero the record is not received and a
  :class:`QuotaExhausted` failure is recorded;
* otherwise the record is received and the balance is capped at the limit;
* the received prefix, and the balance it left behind, stay valid.

Starting a new output does not replenish a tag.  If the original output
factory reuses the tag it selected first, that tag's remaining balance simply
continues.  Records keep their category and number, so a record that was
already received is settled again when a group or window hands it over.

Repeated history is represented with runs instead of one row per operation.
A small segment can be repeated up to ``MAX_REPEAT`` (one billion) times and
interval counts are calculated arithmetically, so the ledger stays smaller
than the archive it describes.  :class:`HistoryWalk` walks the same compressed
history lazily when an operation-by-operation pass is needed.

The main entry point is :func:`reconcile`::

    report = reconcile(archive, [quota_a, quota_b])
    report.counts("quota-a")
    report.round_counts(group="checkout", attempt=2)
    report.rounds(profile="quota-a", attempt=2)
    report.first_divergence()
    report.failure_detail("quota-a").reemitted
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Hashable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable

from logbook.base import NOTSET
from logbook.handlers import Handler

__all__ = (
    "MAX_REPEAT",
    "BatchRecorder",
    "Candidate",
    "DeliveryArchive",
    "DeliveryFailure",
    "DeliverySimulator",
    "Divergence",
    "ExpectedEvent",
    "FailureDetail",
    "GroupRound",
    "HistorySegment",
    "HistoryWalk",
    "OperationHistory",
    "ProfileResult",
    "Quota",
    "QuotaExhausted",
    "QuotaProfile",
    "QuotaReceiver",
    "Receipt",
    "Received",
    "ReconciliationError",
    "ReconciliationReport",
    "RecordBatch",
    "Rejected",
    "RoundSummary",
    "SequenceIssue",
    "TagQuota",
    "UnknownCategory",
    "UnknownTag",
    "archive_from_dict",
    "deliveries",
    "merged_counts",
    "profile_from_dict",
    "reconcile",
    "simulate",
)

#: Largest repetition count accepted by :class:`HistorySegment`.
MAX_REPEAT = 1_000_000_000

TagSelector = Callable[[str, int, "Candidate", Sequence[str]], str]


class ReconciliationError(Exception):
    """Base class for reconciliation errors."""


class UnknownTag(ReconciliationError, KeyError):
    """Raised when a profile has no quota for a selected tag."""

    def __init__(self, tag: str, available: Iterable[str]) -> None:
        self.tag = tag
        self.available = tuple(available)
        super().__init__(
            f"tag {tag!r} is not configured; available tags: "
            f"{', '.join(map(str, self.available)) or '<none>'}"
        )


class UnknownCategory(ReconciliationError, KeyError):
    """Raised when a tag has no delta for a record category."""

    def __init__(self, tag: str, category: str) -> None:
        self.tag = tag
        self.category = category
        super().__init__(f"tag {tag!r} has no delta for category {category!r}")


class QuotaExhausted(ReconciliationError):
    """Raised when a record would take a tag's balance below zero."""

    def __init__(self, tag: str, category: str, balance: int, delta: int) -> None:
        self.tag = tag
        self.category = category
        self.balance = balance
        self.delta = delta
        self.next_balance = balance + delta
        super().__init__(
            f"tag {tag!r} cannot receive {category!r}: "
            f"{balance} {delta:+d} = {self.next_balance}"
        )


@dataclass(frozen=True)
class Quota:
    """A single tag's balance limit, starting balance and category deltas.

    ``limit`` is the highest balance the tag may hold.  ``initial`` is the
    balance before the first record.  ``deltas`` maps a record category to the
    amount it adds to, or subtracts from, the balance.
    """

    limit: int
    initial: int = 0
    deltas: Mapping[str, int] = field(default_factory=dict)
    tag: str | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if self.limit < 0:
            raise ValueError("limit must not be negative")
        if self.initial > self.limit:
            raise ValueError("initial must not exceed limit")
        object.__setattr__(self, "deltas", dict(self.deltas))

    def delta_for(self, category: str) -> int:
        """Return the balance delta for *category*."""
        try:
            return self.deltas[category]
        except KeyError:
            raise UnknownCategory(self.tag_name, category) from None

    @property
    def tag_name(self) -> str:
        """The tag this quota belongs to, when known."""
        return self.tag if self.tag is not None else "<unknown>"

    def with_tag_name(self, tag: str) -> Quota:
        """Return this quota bound to *tag* for error messages."""
        return Quota(self.limit, self.initial, self.deltas, tag)

    def apply(self, balance: int, category: str) -> int:
        """Return the balance after receiving *category*.

        The record fails without touching the balance when the resulting
        balance would be negative.  A successful record is capped at
        :attr:`limit`.
        """
        delta = self.delta_for(category)
        next_balance = balance + delta
        if next_balance < 0:
            raise QuotaExhausted(self.tag_name, category, balance, delta)
        return min(next_balance, self.limit)


#: Alias kept for callers that prefer the domain term used in the archive.
TagQuota = Quota


@dataclass(frozen=True)
class QuotaProfile:
    """A named set of quotas, one per receiving tag."""

    name: str
    quotas: Mapping[str, Quota]

    def __post_init__(self) -> None:
        quotas = {tag: quota.with_tag_name(tag) for tag, quota in self.quotas.items()}
        if not quotas:
            raise ValueError("a quota profile needs at least one tag")
        object.__setattr__(self, "quotas", quotas)

    @property
    def tags(self) -> tuple[str, ...]:
        """Tag names in declaration order."""
        return tuple(self.quotas)

    def initial_balances(self) -> dict[str, int]:
        """Return the starting balance for every tag."""
        return {tag: quota.initial for tag, quota in self.quotas.items()}


@dataclass(frozen=True)
class Candidate:
    """An original record offered to the receiver.

    ``number`` identifies the original record.  ``category`` selects the
    balance delta.  ``tag`` optionally routes this particular record to a
    receiving tag; when it is ``None`` the output's selected tag is used.
    """

    number: Hashable
    category: str
    tag: str | None = None
    source: str = ""
    attempt: int = 1

    def __post_init__(self) -> None:
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")
        try:
            hash(self.number)
        except TypeError:
            raise TypeError("number must be hashable") from None


@dataclass(frozen=True)
class GroupRound:
    """One group output attempt in the archive.

    ``output`` names the receiving output (the handler that was bound).  It
    defaults to ``group``.  ``new_output`` marks the start of a new output;
    the output's tag may then be selected again, but no tag balance is
    replenished.  ``continue_after_failure`` overrides the archive-wide rule
    for this group.
    """

    group: str
    candidates: tuple[Candidate, ...]
    attempt: int = 1
    output: str | None = None
    tag: str | None = None
    new_output: bool = False
    continue_after_failure: bool | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if self.attempt < 1:
            raise ValueError("attempt must be at least 1")

    @property
    def output_key(self) -> str:
        """The key used to remember an output's sticky receiving tag."""
        return self.output if self.output is not None else self.group


@dataclass(frozen=True)
class DeliveryArchive:
    """The recorded group rounds and the rule for using a group again.

    ``continue_after_failure`` is either a single boolean or a mapping from
    group name to boolean.  ``tag_selector`` may override tag selection; it
    receives ``(group, round_index, candidate, available_tags)``.
    """

    rounds: tuple[GroupRound, ...]
    continue_after_failure: Any = False
    tag_selector: TagSelector | None = None
    tag_order: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rounds", tuple(self.rounds))
        if self.tag_order is not None:
            object.__setattr__(self, "tag_order", tuple(self.tag_order))
        if isinstance(self.continue_after_failure, Mapping):
            object.__setattr__(
                self, "continue_after_failure", dict(self.continue_after_failure)
            )

    def continue_for(self, group: str) -> bool:
        """Return whether *group* is used again after a failure."""
        if isinstance(self.continue_after_failure, Mapping):
            return bool(self.continue_after_failure.get(group, False))
        return bool(self.continue_after_failure)

    def iter_events(self) -> Iterator[ExpectedEvent]:
        """Yield every candidate in archive order with its absolute index."""
        global_index = 0
        for round_index, group_round in enumerate(self.rounds):
            for index, candidate in enumerate(group_round.candidates):
                yield ExpectedEvent(
                    group=group_round.group,
                    output=group_round.output_key,
                    round_index=round_index,
                    attempt=group_round.attempt,
                    index=index,
                    global_index=global_index,
                    candidate=candidate,
                )
                global_index += 1

    @property
    def length(self) -> int:
        """Number of candidates in the archive."""
        return sum(len(group_round.candidates) for group_round in self.rounds)

    def operations(self) -> OperationHistory:
        """Return the archive's candidates as a compressed history."""
        history = OperationHistory()
        for group_round in self.rounds:
            history.append(group_round.candidates)
        return history


@dataclass(frozen=True)
class ExpectedEvent:
    """A candidate together with where it sits in the archive."""

    group: str
    output: str
    round_index: int
    attempt: int
    index: int
    global_index: int
    candidate: Candidate


@dataclass(frozen=True)
class Receipt:
    """One record accepted by the receiver."""

    profile: str
    group: str
    output: str
    round_index: int
    attempt: int
    index: int
    global_index: int
    candidate: Candidate
    tag: str
    balance_before: int
    balance_after: int

    @property
    def number(self) -> Hashable:
        """The original record's number."""
        return self.candidate.number

    @property
    def category(self) -> str:
        """The original record's category."""
        return self.candidate.category

    @property
    def source(self) -> str:
        """The source that handed the record over."""
        return self.candidate.source


@dataclass(frozen=True)
class DeliveryFailure:
    """A record the receiver refused because its balance would go negative."""

    profile: str
    group: str
    output: str
    round_index: int
    attempt: int
    index: int
    global_index: int
    candidate: Candidate
    tag: str
    balance_before: int
    delta: int
    next_balance: int
    prefix: tuple[Receipt, ...]

    @property
    def number(self) -> Hashable:
        """The refused record's number."""
        return self.candidate.number

    @property
    def shortfall(self) -> int:
        """How far below zero the balance would have gone."""
        return -self.next_balance


@dataclass(frozen=True)
class SequenceIssue:
    """The first missing or duplicated original number in a run."""

    kind: str
    number: Hashable
    category: str
    source: str
    group: str
    output: str
    round_index: int
    attempt: int
    index: int
    global_index: int
    count: int = 0


@dataclass(frozen=True)
class RoundSummary:
    """One group round's outcome under one quota profile."""

    profile: str
    group: str
    output: str
    round_index: int
    attempt: int
    received: int
    failed: bool
    counts: Counter = field(default_factory=Counter)

    @property
    def delivered(self) -> dict[Hashable, int]:
        """Accepted counts for this round as a plain dictionary."""
        return dict(self.counts)


@dataclass(frozen=True)
class Divergence:
    """The first archive position where two profiles delivered differently.

    ``left`` and ``right`` are the receiving tags, or ``None`` when that
    profile did not deliver the record.  ``left_position`` and
    ``right_position`` are the profile's output positions: how many records
    it had already accepted before this handover.
    """

    event: ExpectedEvent
    left: str | None
    right: str | None
    left_position: int = 0
    right_position: int = 0


@dataclass
class ProfileResult:
    """The outcome of one quota profile over an archive."""

    profile: str
    receipts: list[Receipt] = field(default_factory=list)
    failures: list[DeliveryFailure] = field(default_factory=list)
    balances: dict[str, int] = field(default_factory=dict)
    stopped_groups: set[str] = field(default_factory=set)
    decisions: dict[int, str | None] = field(default_factory=dict)

    @property
    def counts(self) -> Counter:
        """Accepted count per original number."""
        return Counter(receipt.number for receipt in self.receipts)

    def number_counts(self) -> dict[Hashable, int]:
        """Return accepted counts as a plain dictionary."""
        return dict(self.counts)

    def counts_by_round(self) -> dict[tuple[str, int, int], Counter]:
        """Accepted counts per ``(group, round_index, attempt)``."""
        result: dict[tuple[str, int, int], Counter] = {}
        for receipt in self.receipts:
            key = (receipt.group, receipt.round_index, receipt.attempt)
            result.setdefault(key, Counter())[receipt.number] += 1
        return result

    def receipts_for(self, number: Hashable) -> tuple[Receipt, ...]:
        """All accepted receipts for one original number, in delivery order."""
        return tuple(r for r in self.receipts if r.number == number)

    def first_duplicate(self) -> SequenceIssue | None:
        """Return the first original number accepted more than once.

        The reported position is the receipt that made the number a duplicate,
        so it names the round, attempt and source of the repeat.
        """
        seen: Counter = Counter()
        for receipt in self.receipts:
            number = receipt.number
            if seen[number]:
                return SequenceIssue(
                    kind="duplicate",
                    number=number,
                    category=receipt.category,
                    source=receipt.source,
                    group=receipt.group,
                    output=receipt.output,
                    round_index=receipt.round_index,
                    attempt=receipt.attempt,
                    index=receipt.index,
                    global_index=receipt.global_index,
                    count=seen[number] + 1,
                )
            seen[number] += 1
        return None

    def first_missing(self, archive: DeliveryArchive) -> SequenceIssue | None:
        """Return the first archive number this profile never received.

        "Never" is taken over the whole archive, so a number that was received
        in one round is not reported even if another round refused it.
        """
        counts = self.counts
        for event in archive.iter_events():
            if counts[event.candidate.number] == 0:
                return SequenceIssue(
                    kind="missing",
                    number=event.candidate.number,
                    category=event.candidate.category,
                    source=event.candidate.source,
                    group=event.group,
                    output=event.output,
                    round_index=event.round_index,
                    attempt=event.attempt,
                    index=event.index,
                    global_index=event.global_index,
                )
        return None

    def outcome_at(self, global_index: int) -> str | None:
        """Return the tag used at *global_index*, or ``None`` if refused."""
        return self.decisions.get(global_index)


def _segment_counts(segment: HistorySegment, start: int, stop: int) -> Counter:
    """Count numbers in ``[start, stop)`` of one repeated segment."""
    operations = segment.operations
    size = len(operations)
    counts: Counter = Counter()
    if size == 0 or start >= stop:
        return counts

    first_cycle, first_offset = divmod(start, size)
    last_cycle, last_offset = divmod(stop - 1, size)
    if first_cycle == last_cycle:
        for operation in operations[first_offset : last_offset + 1]:
            counts[operation.number] += 1
        return counts

    for operation in operations[first_offset:]:
        counts[operation.number] += 1
    full_cycles = last_cycle - first_cycle - 1
    if full_cycles:
        cycle_counts: Counter = Counter(op.number for op in operations)
        for number, count in cycle_counts.items():
            counts[number] += count * full_cycles
    for operation in operations[: last_offset + 1]:
        counts[operation.number] += 1
    return counts


@dataclass
class HistoryWalk:
    """Walk a compressed history without materializing every repetition.

    A walk holds a cursor in the underlying :class:`OperationHistory`.  Records
    are produced only when asked for, so a segment repeated a billion times is
    read in a loop rather than stored in a list.
    """

    history: OperationHistory
    position: int = 0
    receipts: int = 0
    failures: int = 0
    rejected: list[tuple[int, Candidate]] = field(default_factory=list)
    max_rejections: int = 1000

    def __iter__(self) -> Iterator[tuple[int, Candidate]]:
        return self.iter_from(self.position)

    def iter_from(
        self, position: int | None = None, limit: int | None = None
    ) -> Iterator[tuple[int, Candidate]]:
        """Yield ``(index, candidate)`` pairs beginning at *position*."""
        start = self.position if position is None else position
        stop = (
            self.history.length
            if limit is None
            else min(start + limit, self.history.length)
        )
        yield from enumerate(self.history.iter_operations(start, stop), start)

    def consume(self, candidate: Candidate) -> None:
        """Record the outcome of one candidate."""
        self.receipts += 1

    def reject(self, index: int, candidate: Candidate) -> None:
        """Record a refused candidate, keeping a bounded sample."""
        self.failures += 1
        if len(self.rejected) < self.max_rejections:
            self.rejected.append((index, candidate))

    def interval(self, count: int) -> Iterator[tuple[int, Candidate]]:
        """Yield the next *count* pairs and advance the cursor."""
        start = self.position
        stop = min(start + count, self.history.length)
        yield from enumerate(self.history.iter_operations(start, stop), start)
        self.position = stop


@dataclass(frozen=True)
class HistorySegment:
    """A small operation segment repeated a number of times."""

    operations: tuple[Candidate, ...]
    repeat: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "operations", tuple(self.operations))
        if not isinstance(self.repeat, int):
            raise TypeError("repeat must be an integer")
        if not 1 <= self.repeat <= MAX_REPEAT:
            raise ValueError(f"repeat must be between 1 and {MAX_REPEAT}")

    @property
    def length(self) -> int:
        """Number of operation occurrences in this segment."""
        return len(self.operations) * self.repeat

    def counts(self) -> Counter:
        """Count numbers without expanding the repetitions."""
        result: Counter = Counter(op.number for op in self.operations)
        if self.repeat != 1:
            for number in result:
                result[number] *= self.repeat
        return result

    def iter_operations(
        self, start: int = 0, stop: int | None = None
    ) -> Iterator[Candidate]:
        """Yield operations in ``[start, stop)`` lazily."""
        if stop is None:
            stop = self.length
        stop = min(stop, self.length)
        if start < 0:
            start = max(self.length + start, 0)
        if stop < 0:
            stop = max(self.length + stop, 0)
        if start >= stop:
            return
        size = len(self.operations)
        if size == 0:
            return

        position = start
        _, offset = divmod(position, size)
        while position < stop:
            take = min(size - offset, stop - position)
            yield from self.operations[offset : offset + take]
            position += take
            offset = 0


class OperationHistory:
    """A compressed sequence of original operations.

    A history is a list of :class:`HistorySegment` objects.  Appending the
    same small segment with a large ``repeat`` keeps memory proportional to
    the segment, not to the number of repetitions.
    """

    def __init__(self, segments: Iterable[HistorySegment] = ()) -> None:
        self._segments: list[HistorySegment] = list(segments)

    @property
    def segments(self) -> tuple[HistorySegment, ...]:
        """The history's segments in order."""
        return tuple(self._segments)

    @property
    def length(self) -> int:
        """Total number of operations."""
        return sum(segment.length for segment in self._segments)

    def __len__(self) -> int:
        return self.length

    def __iter__(self) -> Iterator[Candidate]:
        return self.iter_operations()

    def __bool__(self) -> bool:
        return bool(self._segments)

    def append(
        self, operations: Iterable[Candidate], repeat: int = 1
    ) -> OperationHistory:
        """Append *operations*, optionally repeating the segment."""
        self._segments.append(HistorySegment(tuple(operations), repeat))
        return self

    def extend(self, other: OperationHistory) -> OperationHistory:
        """Append all segments of *other*."""
        self._segments.extend(other._segments)
        return self

    def iter_operations(
        self, start: int = 0, stop: int | None = None
    ) -> Iterator[Candidate]:
        """Yield operations in ``[start, stop)`` lazily."""
        if stop is None:
            stop = self.length
        stop = min(stop, self.length)
        if start < 0:
            start = max(self.length + start, 0)
        if stop < 0:
            stop = max(self.length + stop, 0)
        if start >= stop:
            return

        segment_start = 0
        for segment in self._segments:
            segment_end = segment_start + segment.length
            if start < segment_end and stop > segment_start:
                local_start = max(start, segment_start) - segment_start
                local_stop = min(stop, segment_end) - segment_start
                yield from segment.iter_operations(local_start, local_stop)
            segment_start = segment_end

    def counts_in_range(self, start: int = 0, stop: int | None = None) -> Counter:
        """Count numbers in ``[start, stop)`` without expanding repetitions."""
        if stop is None:
            stop = self.length
        stop = min(stop, self.length)
        if start < 0:
            start = max(self.length + start, 0)
        if stop < 0:
            stop = max(self.length + stop, 0)
        if start >= stop:
            return Counter()

        result: Counter = Counter()
        segment_start = 0
        for segment in self._segments:
            segment_end = segment_start + segment.length
            if start < segment_end and stop > segment_start:
                local_start = max(start, segment_start) - segment_start
                local_stop = min(stop, segment_end) - segment_start
                result.update(_segment_counts(segment, local_start, local_stop))
            segment_start = segment_end
        return result

    def counts(self) -> Counter:
        """Count every number in the history."""
        result: Counter = Counter()
        for segment in self._segments:
            result.update(segment.counts())
        return result

    def number_counts(self) -> dict[Hashable, int]:
        """Return all counts as a plain dictionary."""
        return dict(self.counts())

    def segment_counts(self) -> list[dict[str, Any]]:
        """Describe each segment without expanding it."""
        return [
            {
                "operations": list(segment.operations),
                "repeat": segment.repeat,
                "length": segment.length,
            }
            for segment in self._segments
        ]

    def compress(self) -> OperationHistory:
        """Merge adjacent equal segments and drop empty ones."""
        compressed: list[HistorySegment] = []
        for segment in self._segments:
            if not segment.operations:
                continue
            if (
                compressed
                and compressed[-1].operations == segment.operations
                and compressed[-1].repeat + segment.repeat <= MAX_REPEAT
            ):
                previous = compressed.pop()
                compressed.append(
                    HistorySegment(
                        previous.operations, previous.repeat + segment.repeat
                    )
                )
            else:
                compressed.append(segment)
        return OperationHistory(compressed)

    def head(self, count: int) -> list[Candidate]:
        """Return the first *count* operations (small requested windows only)."""
        return list(self.iter_operations(0, count))

    def tail(self, count: int) -> list[Candidate]:
        """Return the last *count* operations (small requested windows only)."""
        return list(self.iter_operations(max(self.length - count, 0), self.length))

    @classmethod
    def merge(cls, *histories: OperationHistory) -> OperationHistory:
        """Concatenate histories into a new one."""
        merged = cls()
        for history in histories:
            merged.extend(history)
        return merged

    @classmethod
    def merged_counts(cls, *histories: OperationHistory) -> Counter:
        """Sum the number counts of several histories."""
        result: Counter = Counter()
        for history in histories:
            result.update(history.counts())
        return result


def merged_counts(*histories: OperationHistory) -> Counter:
    """Sum the number counts of several histories."""
    return OperationHistory.merged_counts(*histories)


class DeliverySimulator:
    """Apply one quota profile to an archive in original order."""

    def __init__(self, archive: DeliveryArchive, profile: QuotaProfile) -> None:
        self.archive = archive
        self.profile = profile

    @property
    def available_tags(self) -> tuple[str, ...]:
        """Tags in the order the output factory selects from.

        The archive's tag order wins so both quota profiles select the same
        receiving tag.  Profiles otherwise declare the order.
        """
        if self.archive.tag_order is not None:
            return self.archive.tag_order
        return self.profile.tags

    def _choose_tag(
        self,
        group_round: GroupRound,
        round_index: int,
        available: Sequence[str],
        previous: str | None,
    ) -> str:
        candidate = group_round.candidates[0] if group_round.candidates else None
        selector = self.archive.tag_selector
        if selector is not None and candidate is not None:
            tag = selector(group_round.group, round_index, candidate, available)
        elif group_round.tag is not None:
            tag = group_round.tag
        elif previous is not None and not group_round.new_output:
            tag = previous
        else:
            tagged = next(
                (
                    candidate.tag
                    for candidate in group_round.candidates
                    if candidate.tag
                ),
                None,
            )
            tag = tagged or available[0]
        if tag not in self.profile.quotas:
            raise UnknownTag(tag, available)
        return tag

    def run(self) -> ProfileResult:
        """Run the profile and return its receipts, failures and balances."""
        result = ProfileResult(
            profile=self.profile.name,
            balances=self.profile.initial_balances(),
        )
        output_tags: dict[str, str] = {}
        stopped: set[str] = result.stopped_groups
        available = self.available_tags
        global_index = 0

        for round_index, group_round in enumerate(self.archive.rounds):
            round_length = len(group_round.candidates)
            output = group_round.output_key

            if group_round.group in stopped:
                for index in range(round_length):
                    result.decisions[global_index + index] = None
                global_index += round_length
                continue

            output_tag = self._choose_tag(
                group_round,
                round_index,
                available,
                output_tags.get(output),
            )
            output_tags[output] = output_tag
            prefix: list[Receipt] = []

            for index, candidate in enumerate(group_round.candidates):
                tag = candidate.tag if candidate.tag is not None else output_tag
                if tag not in self.profile.quotas:
                    raise UnknownTag(tag, available)
                quota = self.profile.quotas[tag]
                balance_before = result.balances[tag]
                try:
                    balance_after = quota.apply(balance_before, candidate.category)
                except QuotaExhausted as error:
                    result.failures.append(
                        DeliveryFailure(
                            profile=self.profile.name,
                            group=group_round.group,
                            output=output,
                            round_index=round_index,
                            attempt=group_round.attempt,
                            index=index,
                            global_index=global_index + index,
                            candidate=candidate,
                            tag=tag,
                            balance_before=balance_before,
                            delta=error.delta,
                            next_balance=error.next_balance,
                            prefix=tuple(prefix),
                        )
                    )
                    result.decisions[global_index + index] = None
                    keep_going = (
                        group_round.continue_after_failure
                        if group_round.continue_after_failure is not None
                        else self.archive.continue_for(group_round.group)
                    )
                    if not keep_going:
                        stopped.add(group_round.group)
                    continue

                result.balances[tag] = balance_after
                receipt = Receipt(
                    profile=self.profile.name,
                    group=group_round.group,
                    output=output,
                    round_index=round_index,
                    attempt=group_round.attempt,
                    index=index,
                    global_index=global_index + index,
                    candidate=candidate,
                    tag=tag,
                    balance_before=balance_before,
                    balance_after=balance_after,
                )
                result.receipts.append(receipt)
                result.decisions[global_index + index] = tag
                prefix.append(receipt)

            global_index += round_length

        return result


def simulate(
    archive: DeliveryArchive,
    profile: QuotaProfile,
    *,
    continue_after_failure: Any | None = None,
) -> ProfileResult:
    """Simulate one quota profile over *archive*.

    ``continue_after_failure`` optionally overrides the archive's rule.
    """
    if continue_after_failure is not None:
        archive = DeliveryArchive(
            rounds=archive.rounds,
            continue_after_failure=continue_after_failure,
            tag_selector=archive.tag_selector,
        )
    return DeliverySimulator(archive, profile).run()


@dataclass(frozen=True)
class FailureDetail:
    """A failure with its received prefix and the sources re-emitted later."""

    failure: DeliveryFailure
    prefix: tuple[Receipt, ...]
    later: tuple[Receipt, ...]
    reemitted: tuple[Receipt, ...]

    @property
    def prefix_numbers(self) -> tuple[Hashable, ...]:
        """Numbers received before the failure, in delivery order."""
        return tuple(receipt.number for receipt in self.prefix)

    @property
    def reemitted_sources(self) -> tuple[str, ...]:
        """Sources that handed the prefix or failed number over again."""
        return tuple(receipt.source for receipt in self.reemitted)


def _filter_round(
    result: ProfileResult,
    group: str | None,
    round_index: int | None,
    attempt: int | None,
) -> list[Receipt]:
    return [
        receipt
        for receipt in result.receipts
        if (group is None or receipt.group == group)
        and (round_index is None or receipt.round_index == round_index)
        and (attempt is None or receipt.attempt == attempt)
    ]


def _positions(result: ProfileResult, length: int) -> list[int]:
    """Return accepted-before counts for every archive position."""
    positions = [0] * length
    accepted = 0
    for index in range(length):
        positions[index] = accepted
        if result.decisions.get(index) is not None:
            accepted += 1
    return positions


class ReconciliationReport:
    """The two (or more) quota runs and the questions an auditor asks."""

    def __init__(
        self,
        archive: DeliveryArchive,
        profiles: Sequence[QuotaProfile],
        results: Mapping[str, ProfileResult],
    ) -> None:
        self.archive = archive
        self.profiles = tuple(profiles)
        self.results = dict(results)

    @property
    def profile_names(self) -> tuple[str, ...]:
        """Profile names in declaration order."""
        return tuple(profile.name for profile in self.profiles)

    def counts(self, profile: str | None = None) -> Any:
        """Accepted counts per number.

        With no profile, returns a mapping of profile name to counts.
        """
        if profile is None:
            return {name: self.results[name].counts for name in self.profile_names}
        return self.results[profile].counts

    def number_matrix(self) -> dict[Hashable, dict[str, int]]:
        """Counts per number across every profile."""
        matrix: dict[Hashable, dict[str, int]] = {}
        for name in self.profile_names:
            for number, count in self.results[name].counts.items():
                matrix.setdefault(number, {})[name] = count
        for counts in matrix.values():
            for name in self.profile_names:
                counts.setdefault(name, 0)
        return matrix

    def round_counts(
        self,
        group: str | None = None,
        round_index: int | None = None,
        attempt: int | None = None,
        profile: str | None = None,
    ) -> Any:
        """Accepted counts per round, for both sides or one profile."""
        names = (profile,) if profile is not None else self.profile_names
        result: dict[str, Counter] = {}
        for name in names:
            counts: Counter = Counter()
            for receipt in _filter_round(
                self.results[name], group, round_index, attempt
            ):
                counts[receipt.number] += 1
            result[name] = counts
        return result

    def rounds(
        self,
        profile: str | None = None,
        group: str | None = None,
        attempt: int | None = None,
    ) -> list[RoundSummary]:
        """Return one summary per archive round, for one or every profile."""
        names = (profile,) if profile is not None else self.profile_names
        summaries = []
        for name in names:
            result = self.results[name]
            failed_rounds = {
                (failure.group, failure.round_index, failure.attempt)
                for failure in result.failures
            }
            for round_index, group_round in enumerate(self.archive.rounds):
                if group is not None and group_round.group != group:
                    continue
                if attempt is not None and group_round.attempt != attempt:
                    continue
                counts: Counter = Counter()
                for receipt in result.receipts:
                    if (
                        receipt.group == group_round.group
                        and receipt.round_index == round_index
                        and receipt.attempt == group_round.attempt
                    ):
                        counts[receipt.number] += 1
                key = (group_round.group, round_index, group_round.attempt)
                summaries.append(
                    RoundSummary(
                        profile=name,
                        group=group_round.group,
                        output=group_round.output_key,
                        round_index=round_index,
                        attempt=group_round.attempt,
                        received=sum(counts.values()),
                        failed=key in failed_rounds,
                        counts=counts,
                    )
                )
        return summaries

    def first_issue(self, profile: str) -> SequenceIssue | None:
        """First missing or duplicated number for *profile*, in archive order."""
        missing = self.results[profile].first_missing(self.archive)
        duplicate = self.results[profile].first_duplicate()
        if missing is None:
            return duplicate
        if duplicate is None:
            return missing
        return missing if missing.global_index <= duplicate.global_index else duplicate

    def interval_counts(
        self, profile: str, start: int = 0, stop: int | None = None
    ) -> Counter:
        """Accepted counts for archive positions in ``[start, stop)``."""
        result = self.results[profile]
        if stop is None:
            stop = self.archive.length
        counts: Counter = Counter()
        for receipt in result.receipts:
            if start <= receipt.global_index < stop:
                counts[receipt.number] += 1
        return counts

    def merged_counts(
        self,
        start: int = 0,
        stop: int | None = None,
        profiles: Sequence[str] | None = None,
    ) -> Counter:
        """Merge interval counts over several profiles."""
        names = tuple(profiles) if profiles is not None else self.profile_names
        counts: Counter = Counter()
        for name in names:
            counts.update(self.interval_counts(name, start, stop))
        return counts

    def first_missing(self, profile: str | None = None) -> Any:
        """First missing original number, for one or every profile."""
        if profile is None:
            return {
                name: self.results[name].first_missing(self.archive)
                for name in self.profile_names
            }
        return self.results[profile].first_missing(self.archive)

    def first_duplicate(self, profile: str | None = None) -> Any:
        """First duplicated original number, for one or every profile."""
        if profile is None:
            return {
                name: self.results[name].first_duplicate()
                for name in self.profile_names
            }
        return self.results[profile].first_duplicate()

    def first_divergence(
        self,
        left: str | None = None,
        right: str | None = None,
    ) -> Divergence | None:
        """First archive position the two profiles delivered differently.

        ``None`` means neither profile delivered the record (both refused or
        both stopped).  The archive decides which receiving tag is used, so
        both profiles select the same tag; only delivery itself can differ.
        Check :attr:`ProfileResult.failures` for whether that position was
        actually refused by the quota.
        """
        if left is None or right is None:
            names = self.profile_names
            if left is None and right is None:
                if len(names) < 2:
                    raise ValueError("need two profiles to compare")
                left, right = names[0], names[1]
            elif left is None:
                left = next(name for name in names if name != right)
            else:
                right = next(name for name in names if name != left)
        left_result = self.results[left]
        right_result = self.results[right]
        left_positions = _positions(left_result, self.archive.length)
        right_positions = _positions(right_result, self.archive.length)
        for event in self.archive.iter_events():
            left_tag = left_result.outcome_at(event.global_index)
            right_tag = right_result.outcome_at(event.global_index)
            if left_tag != right_tag:
                return Divergence(
                    event=event,
                    left=left_tag,
                    right=right_tag,
                    left_position=left_positions[event.global_index],
                    right_position=right_positions[event.global_index],
                )
        return None

    def failure_detail(
        self,
        profile: str,
        group: str | None = None,
        round_index: int | None = None,
        attempt: int | None = None,
        occurrence: int = 0,
    ) -> FailureDetail:
        """Drill into one failure of *profile*.

        Filters select failures by group, round and attempt; ``occurrence``
        picks among the remaining ones in delivery order.  ``reemitted``
        contains later receipts for the failed number or any number in the
        received prefix, which is exactly what a retry replays.
        """
        matches = [
            failure
            for failure in self.results[profile].failures
            if (group is None or failure.group == group)
            and (round_index is None or failure.round_index == round_index)
            and (attempt is None or failure.attempt == attempt)
        ]
        if not matches:
            raise LookupError("no failure matches the requested filters")
        failure = matches[occurrence]
        prefix_numbers = {receipt.number for receipt in failure.prefix}
        prefix_numbers.add(failure.number)
        later = tuple(
            receipt
            for receipt in self.results[profile].receipts
            if receipt.global_index > failure.global_index
        )
        reemitted = tuple(
            receipt for receipt in later if receipt.number in prefix_numbers
        )
        return FailureDetail(
            failure=failure,
            prefix=failure.prefix,
            later=later,
            reemitted=reemitted,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly report."""

        def issue_dict(issue: SequenceIssue | None) -> dict[str, Any] | None:
            if issue is None:
                return None
            return {
                "kind": issue.kind,
                "number": str(issue.number),
                "category": issue.category,
                "source": issue.source,
                "group": issue.group,
                "output": issue.output,
                "round": issue.round_index,
                "attempt": issue.attempt,
                "index": issue.index,
                "global_index": issue.global_index,
                "count": issue.count,
            }

        def counts_dict(counts: Counter) -> dict[str, int]:
            return {str(number): count for number, count in counts.items()}

        divergence = self.first_divergence() if len(self.profile_names) > 1 else None
        first_missing = {
            name: issue_dict(self.first_missing(name)) for name in self.profile_names
        }
        first_duplicate = {
            name: issue_dict(self.first_duplicate(name)) for name in self.profile_names
        }
        rounds = [
            {
                "profile": summary.profile,
                "group": summary.group,
                "output": summary.output,
                "round": summary.round_index,
                "attempt": summary.attempt,
                "received": summary.received,
                "failed": summary.failed,
                "counts": counts_dict(summary.counts),
            }
            for summary in self.rounds()
        ]
        output: dict[str, Any] = {
            "profiles": list(self.profile_names),
            "archive_length": self.archive.length,
            "rounds": rounds,
            "counts": {
                name: counts_dict(self.results[name].counts)
                for name in self.profile_names
            },
            "numbers": {
                str(number): values for number, values in self.number_matrix().items()
            },
            "first_missing": first_missing,
            "first_duplicate": first_duplicate,
            "failures": {
                name: [
                    {
                        "group": failure.group,
                        "output": failure.output,
                        "round": failure.round_index,
                        "attempt": failure.attempt,
                        "number": str(failure.number),
                        "category": failure.candidate.category,
                        "source": failure.candidate.source,
                        "balance": failure.balance_before,
                        "delta": failure.delta,
                        "shortfall": failure.shortfall,
                        "prefix": [str(receipt.number) for receipt in failure.prefix],
                    }
                    for failure in self.results[name].failures
                ]
                for name in self.profile_names
            },
            "first_divergence": None
            if divergence is None
            else {
                "number": str(divergence.event.candidate.number),
                "category": divergence.event.candidate.category,
                "source": divergence.event.candidate.source,
                "group": divergence.event.group,
                "output": divergence.event.output,
                "round": divergence.event.round_index,
                "attempt": divergence.event.attempt,
                "index": divergence.event.index,
                "global_index": divergence.event.global_index,
                "left": divergence.left,
                "right": divergence.right,
                "left_position": divergence.left_position,
                "right_position": divergence.right_position,
            },
        }
        return output


def reconcile(
    archive: DeliveryArchive,
    profiles: Sequence[QuotaProfile],
) -> ReconciliationReport:
    """Simulate *profiles* independently and return their reconciliation."""
    profiles = tuple(profiles)
    if not profiles:
        raise ValueError("at least one quota profile is required")
    names = [profile.name for profile in profiles]
    if len(set(names)) != len(names):
        raise ValueError("profile names must be unique")
    results = {profile.name: simulate(archive, profile) for profile in profiles}
    return ReconciliationReport(archive=archive, profiles=profiles, results=results)


#: Handy alias for a report over two profiles.
deliveries = reconcile


@dataclass(frozen=True)
class Rejected:
    """A record a :class:`QuotaReceiver` refused."""

    record: Any
    tag: str
    category: str
    balance_before: int
    delta: int
    next_balance: int

    @property
    def number(self) -> Any:
        """The record's original number, if it carries one."""
        extra = getattr(self.record, "extra", None) or {}
        return extra.get("number", getattr(self.record, "msg", self.record))

    @property
    def source(self) -> str:
        """The record's source, if it carries one."""
        extra = getattr(self.record, "extra", None) or {}
        return extra.get("source", self.record.channel or "")

    @property
    def shortfall(self) -> int:
        """How far below zero the balance would have gone."""
        return -self.next_balance


@dataclass(frozen=True)
class Received:
    """A record a :class:`QuotaReceiver` accepted."""

    record: Any
    tag: str
    category: str
    balance_before: int
    balance_after: int

    @property
    def number(self) -> Any:
        """The record's original number, if it carries one."""
        extra = getattr(self.record, "extra", None) or {}
        return extra.get("number", getattr(self.record, "msg", self.record))

    @property
    def source(self) -> str:
        """The record's source, if it carries one."""
        extra = getattr(self.record, "extra", None) or {}
        return extra.get("source", self.record.channel or "")


class QuotaReceiver(Handler):
    """A handler that accepts records while a quota profile allows it.

    This is the receiver used by the fixtures: put it inside a real
    :class:`~logbook.GroupHandler` or
    :class:`~logbook.FingersCrossedHandler` and the handler's own batching and
    replay behavior drives it.  It charges the balance before accepting a
    record and raises :class:`QuotaExhausted` when the balance would go below
    zero.  The refused record is not counted and the balance is unchanged.

    ``tag`` fixes the receiving tag.  Otherwise a record's ``extra['tag']``
    wins, then ``tag_selector(record, tags)``, then the profile's first tag.

    A refused record is recorded in :attr:`rejected` and the rest of the batch
    is still settled, matching :class:`DeliverySimulator`.  Set
    ``raise_on_failure=True`` to raise :class:`QuotaExhausted` instead, which
    is useful when observing how a real outer handler reacts to an exception.
    """

    def __init__(
        self,
        profile: QuotaProfile,
        tag: str | None = None,
        tag_selector: Callable[[Any, Sequence[str]], str] | None = None,
        level: int = NOTSET,
        filter: Callable[[Any, Any], bool] | None = None,
        bubble: bool = False,
        raise_on_failure: bool = False,
    ) -> None:
        Handler.__init__(self, level, filter, bubble)
        self.profile = profile
        self.tag = tag
        self.tag_selector = tag_selector
        self.raise_on_failure = raise_on_failure
        self.balances = profile.initial_balances()
        self.receipts: list[Received] = []
        self.rejected: list[Rejected] = []

    def choose_tag(self, record: Any) -> str:
        """Return the tag that would receive *record*."""
        if self.tag is not None:
            return self.tag
        extra = getattr(record, "extra", None) or {}
        tagged = extra.get("tag")
        if tagged:
            return tagged
        if self.tag_selector is not None:
            return self.tag_selector(record, self.profile.tags)
        return self.profile.tags[0]

    def category_for(self, record: Any) -> str:
        """Return the record category used for the balance table."""
        extra = getattr(record, "extra", None) or {}
        category = extra.get("category")
        if category:
            return category
        return getattr(record, "level_name", "INFO") or "INFO"

    def emit(self, record: Any) -> None:
        """Accept *record* or raise :class:`QuotaExhausted`."""
        tag = self.choose_tag(record)
        if tag not in self.profile.quotas:
            raise UnknownTag(tag, self.profile.tags)
        category = self.category_for(record)
        quota = self.profile.quotas[tag]
        before = self.balances[tag]
        try:
            after = quota.apply(before, category)
        except QuotaExhausted as error:
            self.rejected.append(
                Rejected(
                    record=record,
                    tag=tag,
                    category=category,
                    balance_before=before,
                    delta=error.delta,
                    next_balance=error.next_balance,
                )
            )
            if self.raise_on_failure:
                raise
            return
        self.balances[tag] = after
        self.receipts.append(
            Received(
                record=record,
                tag=tag,
                category=category,
                balance_before=before,
                balance_after=after,
            )
        )

    def handle(self, record: Any) -> bool:
        """Emit *record*, letting reconciliation errors propagate.

        Ordinary handler errors keep Logbook's usual flags-controlled
        behavior, and a quota rejection is handled by :meth:`emit`.
        """
        try:
            self.emit(record)
        except ReconciliationError:
            raise
        except Exception:
            self.handle_error(record, sys.exc_info())
        return True


@dataclass(frozen=True)
class RecordBatch:
    """A batch handed to a receiver, as recorded by :class:`BatchRecorder`."""

    reason: str
    records: tuple[Any, ...]
    attempt: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))


class BatchRecorder(Handler):
    """Record emitted records and batches for later comparison.

    Put this inside a real :class:`~logbook.GroupHandler` or
    :class:`~logbook.FingersCrossedHandler` to capture exactly what the
    handler handed over and why.  :meth:`to_archive` converts the captured
    batches into the same :class:`DeliveryArchive` the simulator consumes.
    """

    def __init__(
        self,
        group: str = "default",
        *,
        number_key: str = "number",
        category_key: str = "category",
        source_key: str = "source",
        tag_key: str = "tag",
        bubble: bool = False,
    ) -> None:
        Handler.__init__(self, NOTSET, None, bubble)
        self.group = group
        self.number_key = number_key
        self.category_key = category_key
        self.source_key = source_key
        self.tag_key = tag_key
        self.batches: list[RecordBatch] = []

    def emit(self, record: Any) -> None:
        """Record a single record as its own batch."""
        self.batches.append(RecordBatch("emit", (record,)))

    def emit_batch(self, records: Iterable[Any], reason: str) -> None:
        """Record a batch with the reason supplied by the outer handler.

        Batches separated by a failed flush are numbered as successive
        attempts of the same group.
        """
        records = tuple(records)
        attempt = 1
        if reason in ("group", "escalation") and self.batches:
            previous = self.batches[-1]
            if previous.reason == reason:
                attempt = previous.attempt + 1
        self.batches.append(RecordBatch(reason, records, attempt))

    def to_candidate(self, record: Any) -> Candidate:
        """Convert a log record to a simulator candidate."""
        extra = getattr(record, "extra", None) or {}

        def value(key: str, fallback: Any) -> Any:
            found = extra.get(key) if hasattr(extra, "get") else None
            return fallback if found in (None, "") else found

        number = value(self.number_key, getattr(record, "message", record))
        category = value(self.category_key, getattr(record, "level_name", "INFO"))
        source = value(self.source_key, getattr(record, "channel", "") or "")
        tag = value(self.tag_key, None)
        attempt = value("attempt", 1)
        if not isinstance(attempt, int):
            attempt = 1
        return Candidate(
            number=number,
            category=category,
            tag=tag,
            source=source,
            attempt=attempt,
        )

    def to_rounds(self, group: str | None = None) -> tuple[GroupRound, ...]:
        """Convert captured batches to rounds in capture order."""
        rounds = []
        for batch in self.batches:
            candidates = tuple(self.to_candidate(record) for record in batch.records)
            attempt = next(
                (
                    candidate.attempt
                    for candidate in candidates
                    if candidate.attempt != 1
                ),
                batch.attempt,
            )
            rounds.append(
                GroupRound(
                    group=group if group is not None else self.group,
                    candidates=candidates,
                    attempt=attempt,
                )
            )
        return tuple(rounds)

    def to_archive(
        self,
        group: str | None = None,
        *,
        continue_after_failure: Any = False,
        tag_selector: TagSelector | None = None,
    ) -> DeliveryArchive:
        """Convert captured batches to a :class:`DeliveryArchive`."""
        return DeliveryArchive(
            rounds=self.to_rounds(group),
            continue_after_failure=continue_after_failure,
            tag_selector=tag_selector,
        )


def _candidate_from_dict(data: Mapping[str, Any]) -> Candidate:
    return Candidate(
        number=data["number"],
        category=data["category"],
        tag=data.get("tag"),
        source=data.get("source", ""),
        attempt=int(data.get("attempt", 1)),
    )


def archive_from_dict(data: Mapping[str, Any]) -> DeliveryArchive:
    """Build an archive from a JSON-compatible dictionary."""
    rounds = []
    for item in data["rounds"]:
        rounds.append(
            GroupRound(
                group=item["group"],
                candidates=tuple(
                    _candidate_from_dict(candidate)
                    for candidate in item.get("candidates", ())
                ),
                attempt=int(item.get("attempt", 1)),
                output=item.get("output"),
                tag=item.get("tag"),
                new_output=bool(item.get("new_output", False)),
                continue_after_failure=item.get("continue_after_failure"),
            )
        )
    tag_order = data.get("tag_order")
    return DeliveryArchive(
        rounds=tuple(rounds),
        continue_after_failure=data.get("continue_after_failure", False),
        tag_order=tuple(tag_order) if tag_order is not None else None,
    )


def profile_from_dict(name: str, data: Mapping[str, Any]) -> QuotaProfile:
    """Build a quota profile from a JSON-compatible dictionary."""
    quotas = {}
    for tag, item in data.items():
        quotas[tag] = Quota(
            limit=int(item["limit"]),
            initial=int(item.get("initial", 0)),
            deltas={key: int(value) for key, value in item.get("deltas", {}).items()},
        )
    return QuotaProfile(name=name, quotas=quotas)


def main(argv: Sequence[str] | None = None) -> int:
    """Command line entry point for an auditor's reconciliation query."""
    parser = argparse.ArgumentParser(
        description="Reconcile buffered Logbook deliveries under quota profiles."
    )
    parser.add_argument(
        "document",
        nargs="?",
        help="JSON document with 'quotas' and 'rounds'; defaults to stdin",
    )
    parser.add_argument(
        "--profile",
        action="append",
        help="only report these profile names (repeatable)",
    )
    args = parser.parse_args(argv)

    if args.document:
        with open(args.document, encoding="utf-8") as handle:
            data = json.load(handle)
    else:
        data = json.load(sys.stdin)

    archive = archive_from_dict(data)
    profiles = [
        profile_from_dict(name, quota_data)
        for name, quota_data in data["quotas"].items()
    ]
    if args.profile:
        wanted = set(args.profile)
        profiles = [profile for profile in profiles if profile.name in wanted]
    report = reconcile(archive, profiles)
    json.dump(report.to_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
