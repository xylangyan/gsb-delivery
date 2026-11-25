"""Forward-only row ingestion with atomic slice-correction orders and bounded windows.

The supplier table can be read *forward exactly once*.  This module ingests it into a shared buffer, keeps the
scheduler's inclusive ``(lower, upper)`` bound on the (still unknown) total row count, and serves two views from the
very same read rows - the source is never reopened or rewound:

* :meth:`RowFile.old_view` streams the original rows forward and may stop early (the exact total is unknown then, but
  everything actually read is returned);
* :meth:`RowFile.window` answers a *small* window of the corrected document, position by position, giving the actual
  origin of every determinate row and explicitly flagging positions that still need verification.

An **order** (a correction batch) is executed as a plain Python list would execute it: a sequence of slice
assignments (including step-1 replacements that change the middle length) and slice deletions, which may count from
the end (negative indices) and use negative steps.  The order is atomic: it only takes effect when it succeeds as a
whole; if a single extended-slice assignment has the wrong number of items (or a step is zero), the entire order is
void and the original rows are untouched.

Bounded total length
-------------------
When the total is only known as an interval, behaviour is not probed length-by-length and common-multiple periods are
not laid out across the interval.  The per-position outcome is eventually periodic in the total length, with a period
dividing the slice steps, so the interval is reduced to one period at each end (or, if that period is unknown or too
large, a bounded neighbourhood at each end).  Every position is evaluated on those representative lengths; positions
whose representatives all agree are returned with their actual origin, and positions whose representatives disagree
are returned as :class:`Pending` with two witness total counts and their different results.  A certified prefix -
positions that no correction can reach - is delivered without waiting for the tail.

Terminology
-----------
``Origin(index)``      the original row ``index`` now occupies that position.
``Inserted(value, n)`` a literal supplied by an assignment (token ``n`` keeps equal literals distinct, so two equal
                       values belonging to different original rows are never merged).
``Pending(...)``       candidates disagree; the position needs verification and both witness results are given.
``MissingBody(index)`` the origin is already recognised but its body has not been read yet.

After :meth:`RowFile.finish` the total is exact and every position is determinate.  :meth:`RowFile.verify` replays
each order against a plain list built from the same original input and checks every successful position (or the
void-order identity) one by one.
"""

from __future__ import unicode_literals

from collections import namedtuple

__all__ = [
    "APPLIED",
    "VOID",
    "UNDECIDED",
    "Inserted",
    "MissingBody",
    "Order",
    "OrderVoid",
    "Origin",
    "OutOfRange",
    "Pending",
    "PositionReport",
    "RowFile",
    "SliceEdit",
    "WindowReport",
]

APPLIED = "applied"
VOID = "void"
UNDECIDED = "undecided"

# The outcome of a fixed final position is periodic in the total length once past a small transient; the period is
# the least common multiple of the slice steps.  We never lay that period out across the whole (possibly 1e8-wide)
# interval: only one period at each interval end is examined.
_MAX_PERIOD = 8192
_NEIGHBOURHOOD = 16


# ------------------------------------------------------------------------------------------------------------------------------------------
# Slice arithmetic (mirrors ``slice(...).indices(length)`` exactly)
# ------------------------------------------------------------------------------------------------------------------------------------------


def _normalized(start, stop, step, length):
    start, stop, step = slice(start, stop, step).indices(length)
    if step == 0:
        raise OrderVoid("slice step cannot be zero")
    return start, stop, step


def _count(start, stop, step):
    if step > 0:
        return 0 if stop <= start else (stop - start + step - 1) // step
    return 0 if stop >= start else (start - stop + (-step) - 1) // (-step)


def _removed_before(position, start, stop, step):
    """Number of positions in ``range(start, stop, step)`` strictly below ``position``."""
    total = _count(start, stop, step)
    if total == 0 or position <= 0:
        return 0
    width = abs(step)
    lowest = start if step > 0 else start - (total - 1) * width
    if position <= lowest:
        return 0
    return min(total, (position - lowest + width - 1) // width)


def _contains(position, start, stop, step):
    if step > 0:
        return start <= position < stop and (position - start) % step == 0
    return stop < position <= start and (start - position) % (-step) == 0


def _position_in_range(position, start, step):
    if step > 0:
        return (position - start) // step
    return (start - position) // (-step)


# ------------------------------------------------------------------------------------------------------------------------------------------
# Order description
# ------------------------------------------------------------------------------------------------------------------------------------------


class _TokenSource(object):
    def __init__(self):
        self.value = 0

    def next(self):
        token = self.value
        self.value += 1
        return token


class SliceEdit(object):
    """One slice assignment (``items``) or deletion (``delete``)."""

    __slots__ = ("start", "stop", "step", "items", "delete")

    def __init__(self, start, stop, step, items, delete):
        self.start = start
        self.stop = stop
        self.step = step
        self.items = items
        self.delete = delete

    def __repr__(self):
        if self.delete:
            return "delete[{!r}:{!r}:{!r}]".format(self.start, self.stop, self.step)
        return "assign[{!r}:{!r}:{!r}] <- {} item(s)".format(self.start, self.stop, self.step, len(self.items))


class Order(object):
    """A correction batch applied atomically.

    Example::

        order = Order().delete(-1, None, -2).assign(-3, -1, ["fix"])
        rowfile.apply(order)
    """

    def __init__(self, tokens=None):
        self.edits = []
        self._tokens = tokens if tokens is not None else _TokenSource()

    def assign(self, start, stop, items, step=None):
        wrapped = tuple(value if isinstance(value, Inserted) else Inserted(value, self._tokens.next()) for value in items)
        self.edits.append(SliceEdit(start, stop, step, wrapped, False))
        return self

    def at(self, index, values):
        """``container[index:index] = values`` - an insertion."""
        return self.assign(index, index, values, None)

    def delete(self, start, stop=None, step=None):
        self.edits.append(SliceEdit(start, stop, step, None, True))
        return self

    def __len__(self):
        return len(self.edits)

    def __iter__(self):
        return iter(self.edits)

    def __repr__(self):
        return "Order({!r})".format(self.edits)


# ------------------------------------------------------------------------------------------------------------------------------------------
# Plan / trace for a concrete length
# ------------------------------------------------------------------------------------------------------------------------------------------


class _Stage(object):
    __slots__ = ("edit", "start", "stop", "step", "before", "after", "replaced", "items", "delete")

    def __init__(self, edit, length):
        start, stop, step = _normalized(edit.start, edit.stop, edit.step, length)
        self.edit = edit
        self.start, self.stop, self.step = start, stop, step
        self.delete = edit.delete
        self.items = edit.items
        self.before = length
        if edit.delete:
            self.replaced = _count(start, stop, step)
            self.after = length - self.replaced
        elif step == 1:
            self.replaced = max(0, stop - start)
            self.after = length + len(edit.items) - self.replaced
        else:
            self.replaced = _count(start, stop, step)
            if self.replaced != len(edit.items):
                raise OrderVoid(
                    "extended slice assignment needs {} items but got {}".format(self.replaced, len(edit.items))
                )
            self.after = length
        if self.after < 0:
            raise OrderVoid("order would make the row count negative")

    def __repr__(self):
        return "_Stage({!r}, {}->{})".format(self.edit, self.before, self.after)


def _plan(edits, length):
    stages = []
    current = length
    for edit in edits:
        stage = _Stage(edit, current)
        stages.append(stage)
        current = stage.after
    return stages, current


def _trace(stages, position):
    current = position
    provenance = None
    for stage in reversed(stages):
        if provenance is not None:
            continue
        if stage.delete:
            moving = current
            while True:
                shifted = current + _removed_before(moving + 1, stage.start, stage.stop, stage.step)
                if shifted == moving:
                    break
                moving = shifted
            current = moving
        elif stage.step == 1:
            count = len(stage.items)
            if current < stage.start:
                continue
            if current < stage.start + count:
                provenance = stage.items[current - stage.start]
            else:
                current = current - count + stage.replaced
        else:
            if _contains(current, stage.start, stage.stop, stage.step):
                provenance = stage.items[_position_in_range(current, stage.start, stage.step)]
    return Origin(current) if provenance is None else provenance


# ------------------------------------------------------------------------------------------------------------------------------------------
# Reports
# ------------------------------------------------------------------------------------------------------------------------------------------


class PositionReport(namedtuple("PositionReport", ["position", "outcome"])):
    __slots__ = ()

    @property
    def determinate(self):
        return not isinstance(self.outcome, Pending)

    @property
    def needs_verification(self):
        return isinstance(self.outcome, Pending)

    @property
    def body_missing(self):
        return isinstance(self.outcome, MissingBody)

    def __repr__(self):
        return "PositionReport({}, {!r})".format(self.position, self.outcome)


class WindowReport(object):
    def __init__(self, start, stop, positions):
        self.start = start
        self.stop = stop
        self.positions = list(positions)

    @property
    def rows(self):
        """Determinate positions (their actual origin/insertion is known)."""
        return [report for report in self.positions if report.determinate]

    @property
    def pending(self):
        """Positions that still need verification."""
        return [report for report in self.positions if report.needs_verification]

    def __iter__(self):
        return iter(self.positions)

    def __len__(self):
        return len(self.positions)

    def __repr__(self):
        return "WindowReport([{}:{}], {} position(s), {} pending)".format(
            self.start, self.stop, len(self.positions), len(self.pending)
        )


# ------------------------------------------------------------------------------------------------------------------------------------------
# Public result vocabulary
# ------------------------------------------------------------------------------------------------------------------------------------------

class _Outcome(object):
    """Outcome base class; subclasses compare by their ``kind`` and payload (never as bare tuples)."""

    kind = None
    __slots__ = ()

    def __eq__(self, other):
        if not isinstance(other, _Outcome):
            return NotImplemented
        return self.kind == other.kind and self._payload() == other._payload()

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash((self.kind, self._payload()))

    def _payload(self):
        raise NotImplementedError


class Origin(_Outcome):
    kind = "origin"
    __slots__ = ("index",)

    def __init__(self, index):
        self.index = index

    def _payload(self):
        return (self.index,)

    def __repr__(self):
        return "Origin({})".format(self.index)


class Inserted(_Outcome):
    kind = "inserted"
    __slots__ = ("value", "token")

    def __init__(self, value, token):
        self.value = value
        self.token = token

    def _payload(self):
        return (self.token,)

    def __repr__(self):
        return "Inserted({!r}, token={})".format(self.value, self.token)


class MissingBody(_Outcome):
    kind = "missing_body"
    __slots__ = ("index",)

    def __init__(self, index):
        self.index = index

    def _payload(self):
        return (self.index,)

    def __repr__(self):
        return "MissingBody({})".format(self.index)


class OutOfRange(_Outcome):
    kind = "out_of_range"
    __slots__ = ("position",)

    def __init__(self, position):
        self.position = position

    def _payload(self):
        return (self.position,)

    def __repr__(self):
        return "OutOfRange({})".format(self.position)


class Pending(_Outcome):
    kind = "pending"
    __slots__ = ("position", "alternatives")

    def __init__(self, position, alternatives):
        self.position = position
        self.alternatives = tuple(alternatives)

    def _payload(self):
        return (self.position, tuple(self.alternatives))

    def __repr__(self):
        return "Pending({}, {!r})".format(self.position, self.alternatives)


class OrderVoid(Exception):
    """An order does not succeed as a plain list and is discarded as a whole."""


# ------------------------------------------------------------------------------------------------------------------------------------------
# The reader
# ------------------------------------------------------------------------------------------------------------------------------------------


class RowFile(object):
    def __init__(self, iterable, total_bounds, tokens=None):
        lower, upper = total_bounds
        if lower < 0 or upper < lower:
            raise ValueError("invalid total bounds: {!r}".format(total_bounds))
        self._source = iter(iterable)
        self._lower = lower
        self._upper = upper
        self._rows = []
        self._exhausted = False
        self._tokens = tokens if tokens is not None else _TokenSource()
        self._orders = []
        self._status = []

    # -- shared ingestion --------------------------------------------------

    def _pull(self):
        try:
            row = next(self._source)
        except StopIteration:
            self._exhausted = True
            return None
        self._rows.append(row)
        return row

    def _fill_to(self, count):
        while not self._exhausted and len(self._rows) < count:
            self._pull()

    def _drain(self):
        while not self._exhausted:
            self._pull()

    def _length_interval(self):
        if self._exhausted:
            return len(self._rows), len(self._rows)
        lower = max(self._lower, len(self._rows))
        upper = max(self._upper, len(self._rows))
        return lower, upper

    def _period(self):
        """A period of the length-dependent outcome, or ``None`` when it is too large to enumerate.

        For any fixed final position the outcome is eventually periodic in the total length with a period dividing the
        least common multiple of the absolute slice steps.  Step-1 edits act as length-changing replacements whose
        *left* anchor moves linearly with the length, so they are handled separately (see ``_candidate_lengths``).
        """
        period = 1
        for order in self._orders:
            for edit in order:
                if edit.step is not None and edit.step != 0:
                    period = _lcm(period, abs(edit.step))
                    if period > _MAX_PERIOD:
                        return None
        return period

    def _candidate_lengths(self):
        """Representative total lengths used to decide determinate vs pending.

        A narrow interval is enumerated.  A wide interval (such as the scheduler's 20M-100M range) is *not* walked
        length by length: because the per-position outcome is eventually periodic with a period dividing the slice
        steps, one full period at each interval end is sufficient to expose every distinct result.  If the period is
        unknown or too large to materialize, a bounded neighbourhood at each end is used and the caller's results stay
        conservative (disagreements become :class:`Pending` rather than wrong determinate answers).
        """
        lower, upper = self._length_interval()
        if lower == upper:
            return [lower]
        width = upper - lower
        period = self._period()
        if period is not None and width <= max(period, _NEIGHBOURHOOD):
            return list(range(lower, upper + 1))
        if period is None or period > _MAX_PERIOD:
            span = _NEIGHBOURHOOD
        else:
            span = period
        candidates = {lower, upper}
        for base in (lower, upper - span):
            for offset in range(span + 1):
                value = base + offset
                if lower <= value <= upper:
                    candidates.add(value)
        return sorted(candidates)

    @property
    def total_length(self):
        return len(self._rows) if self._exhausted else None

    @property
    def total_bounds(self):
        return self._lower, self._upper

    @property
    def rows_read(self):
        return len(self._rows)

    @property
    def final_length(self):
        """Corrected document length when the original length is known, otherwise ``None``.

        The original length is known once the source is exhausted or when the scheduler's bound is a single value.
        """
        lower, upper = self._length_interval()
        if not self._exhausted and lower != upper:
            return None
        length = lower
        for order in self._orders:
            if not order.edits:
                continue
            try:
                _, length = _plan(order.edits, length)
            except OrderVoid:
                continue
        return length

    # -- old reader --------------------------------------------------------

    def old_view(self):
        """Stream original rows forward; may stop early and never reopens the source."""
        for index in range(len(self._rows)):
            yield self._rows[index]
        while not self._exhausted:
            row = self._pull()
            if row is not None:
                yield row

    def __iter__(self):
        return self.old_view()

    def __len__(self):
        self._drain()
        return len(self._rows)

    # -- orders ------------------------------------------------------------

    def _status_at(self, index, length):
        """Status of order ``index`` when the whole prefix starts from base ``length``."""
        current = max(length, 0)
        status = APPLIED
        for position in range(index + 1):
            order = self._orders[position]
            if not order.edits:
                if position == index:
                    status = APPLIED
                continue
            try:
                _, current = _plan(order.edits, current)
            except OrderVoid:
                if position == index:
                    status = VOID
                continue
            if position == index:
                status = APPLIED
        return status

    def apply(self, order):
        """Schedule ``order``; report ``APPLIED``/``VOID``/``UNDECIDED`` over the length interval."""
        if not isinstance(order, Order):
            raise TypeError("expected Order, got {}".format(type(order).__name__))
        self._orders.append(order)
        index = len(self._orders) - 1
        statuses = {self._status_at(index, length) for length in self._candidate_lengths()}
        if statuses == {APPLIED}:
            status = APPLIED
        elif statuses == {VOID}:
            status = VOID
        else:
            status = UNDECIDED
        self._status.append(status)
        return status

    # -- provenance --------------------------------------------------------

    def _outcome_at(self, position, length):
        length = max(length, 0)
        stages = []
        current = length
        for order in self._orders:
            if not order.edits:
                continue
            try:
                order_stages, current = _plan(order.edits, current)
            except OrderVoid:
                continue
            stages.extend(order_stages)
        if position < 0 or position >= current:
            return OutOfRange(position)
        return _trace(stages, position)

    def _outcome_alternatives(self, position):
        op = self._outcome_at
        first = None
        for length in self._candidate_lengths():
            outcome = op(position, length)
            if first is None:
                first = (length, outcome)
            elif outcome != first[1]:
                return (first, (length, outcome))
        if first is None:
            return ()
        return (first,)

    def _body(self, index, read):
        if 0 <= index < len(self._rows):
            return self._rows[index]
        if read and not self._exhausted:
            self._fill_to(index + 1)
            if 0 <= index < len(self._rows):
                return self._rows[index]
        return None

    def _resolve(self, position, read):
        alternatives = self._outcome_alternatives(position)
        if len(alternatives) == 1:
            outcome = alternatives[0][1]
            if isinstance(outcome, Origin):
                body = self._body(outcome.index, read)
                return outcome if body is not None else MissingBody(outcome.index)
            return outcome
        return Pending(position, tuple(alternatives))

    def safe_prefix_stop(self):
        """Largest ``stop`` such that every final position below it is an untouched original row.

        A position below this bound is delivered without consulting the tail: it is the same original row whether the
        order applies or is void.  The bound is zero when a correction can reach the beginning.
        """
        return _safe_prefix(self._orders, self._candidate_lengths())

    def window(self, start, stop, read=False):
        """Provenance for the small final-position window ``[start, stop)``."""
        if start < 0 or stop < start:
            raise ValueError("invalid window: [{}, {})".format(start, stop))
        reports = [PositionReport(position, self._resolve(position, read)) for position in range(start, stop)]
        return WindowReport(start, stop, reports)

    def row(self, index, read=True):
        """Body of original row ``index``; reads forward only as far as needed."""
        return self._body(index, read)

    # -- finishing / replay ------------------------------------------------

    def finish(self):
        """Read the source to the end and resolve every order with the now exact total length."""
        self._drain()
        length = len(self._rows)
        self._status = [self._status_at(index, length) for index in range(len(self._orders))]
        return list(self._status)

    def verify(self):
        """Replay each order on a plain list of the same original input and check every position.

        Returns ``(ok, details)``.  ``details`` reports each order's replayed status and any position whose traced
        origin disagrees with the literal plain-list result, or where a void order was not identity.
        """
        self.finish()
        length = len(self._rows)
        plain = [Origin(index) for index in range(length)]
        details = []
        ok = True
        applied_stages = []
        for index, (order, declared) in enumerate(zip(self._orders, self._status)):
            snapshot = list(plain)
            try:
                _plain_execute(plain, order)
                replayed = APPLIED
            except OrderVoid:
                replayed = VOID
                plain = snapshot
            if declared in (VOID, APPLIED) and declared != replayed:
                ok = False
                details.append({"order": index, "declared": declared, "replayed": replayed})
            if replayed == APPLIED:
                stages, _ = _plan(order.edits, len(snapshot))
                applied_stages.extend(stage for stage in stages)
            else:
                for position, value in enumerate(snapshot):
                    if plain[position] != value:
                        ok = False
                        details.append({"order": index, "position": position, "void_but_changed": True})
        final_length = len(plain)
        for position in range(final_length):
            traced = _trace(applied_stages, position)
            if traced != plain[position]:
                ok = False
                details.append({"position": position, "traced": traced, "replayed": plain[position]})
        return ok, details


# ------------------------------------------------------------------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------------------------------------------------------------------


def _gcd(a, b):
    while b:
        a, b = b, a % b
    return a


def _lcm(a, b):
    return a // _gcd(a, b) * b


def _plain_execute(container, order):
    for edit in order:
        start, stop, step = _normalized(edit.start, edit.stop, edit.step, len(container))
        positions = list(range(start, stop, step))
        if edit.delete:
            for position in reversed(sorted(positions)):
                del container[position]
        elif step == 1:
            container[start:stop] = list(edit.items)
        else:
            if len(positions) != len(edit.items):
                raise OrderVoid("extended slice length mismatch")
            for offset, position in enumerate(positions):
                container[position] = edit.items[offset]


def _safe_prefix(orders, lengths):
    """Largest prefix of final positions untouched by any correction across all candidate lengths.

    Every stage of an applied order can only move material at document indices at or above
    ``min(0, lowest index the edit can touch)``, so positions below the smallest such edge are the original rows.
    Insertions at a position shift everything after them, but not the positions before; the minimum edge therefore
    accounts for both deletions and step-1 replacements.  A void order changes nothing at all.
    """
    boundary = None
    for length in lengths:
        stage_length = max(length, 0)
        stages = []
        for order in orders:
            if not order.edits:
                continue
            try:
                order_stages, stage_length = _plan(order.edits, stage_length)
            except OrderVoid:
                # A void order changes nothing; it neither moves material nor relaxes earlier constraints.
                continue
            stages.extend(order_stages)
        lowest = None
        for stage in stages:
            # Every edit's resolved target/removal positions are at or above the lowest position it touches; a
            # step-1 replacement also shifts the tail, but never positions below `stage.start`.
            edge = stage.start if stage.step == 1 else _lowest_touched(stage.start, stage.stop, stage.step)
            edge = max(edge, 0)
            lowest = edge if lowest is None else min(lowest, edge)
        candidate = lowest if lowest is not None else max(length, 0)
        boundary = candidate if boundary is None else min(boundary, candidate)
    return max(0, boundary or 0)


def _lowest_touched(start, stop, step):
    """Lowest position in ``range(start, stop, step)`` (arithmetic only; ranges may be huge)."""
    total = _count(start, stop, step)
    if total == 0:
        return start if step > 0 else max(stop + 1, 0)
    if step > 0:
        return start
    return start - (total - 1) * (-step)
