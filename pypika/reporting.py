"""
Helpers for building reports whose window metrics must remain correct at the
edges of a visible date range.

A common reporting mistake is to filter the source table first and then apply
``LAG``/``LEAD``.  The first visible reading then has no previous value and the
last visible reading has no next value, even though both neighbours exist in
the history.  The helpers in this module build a bounded calculation window
instead:

* every valid row in the visible half-open range is part of the calculation
  window;
* for each partition, at most one valid row before the range and one valid row
  at or after the range are added;
* the window metrics are calculated in that bounded window; and
* the outer query returns only the visible rows -- invalid ones included, with
  null metrics -- joining the metrics back by the partition key and unique
  record id.

Rows outside the visible range therefore influence the metrics but are never
returned by the report.
"""

from __future__ import annotations

from typing import Any

from pypika.analytics import Lag, Lead, RowNumber
from pypika.enums import Order
from pypika.queries import Query, QueryBuilder, Table
from pypika.terms import Term

__author__ = "Timothy Heys"
__email__ = "theys@kayak.com"

VISIBLE_CTE = "visible_rows"
VISIBLE_VALID_CTE = "visible_valid_rows"
PREVIOUS_CTE = "previous_rows"
NEXT_CTE = "next_rows"
WINDOW_CTE = "window_rows"
VALID_CTE = "valid_rows"

_NEIGHBOR_RANK = "neighbor_rank"


class DeviceReadingsReport:
    """Immutable template for per-device, neighbor-aware reading reports.

    The source table is expected to contain one row per reading:

    ``device``
        Partition key. Metrics are calculated independently per device.
    ``observed_at``
        Event timestamp. The report uses a half-open ``[start, end)`` range.
    ``record_id``
        Unique record identifier. ``(observed_at, record_id)`` is the total
        ordering used for both the neighbour lookup and ``LAG``/``LEAD``.
    ``reading``
        The value exposed by the two window metrics.
    ``is_valid``
        Truthy for readings that participate in the metrics.

    The constructor arguments can be used to map a differently named source
    table without copying or renaming columns.

    The template itself is stateless.  Calling :meth:`build` repeatedly with
    different dates or display orders does not modify the template or any
    previously returned query.
    """

    def __init__(
        self,
        table: Table | str,
        *,
        device: str = "device",
        observed_at: str = "observed_at",
        record_id: str = "record_id",
        reading: str = "reading",
        is_valid: str = "is_valid",
        previous_reading: str = "previous_reading",
        next_reading: str = "next_reading",
    ) -> None:
        self.table = table if isinstance(table, Table) else Table(table)
        self.device = device
        self.observed_at = observed_at
        self.record_id = record_id
        self.reading = reading
        self.is_valid = is_valid
        self.previous_reading = previous_reading
        self.next_reading = next_reading

        source_columns = (device, observed_at, record_id, reading, is_valid)
        if len(set(source_columns)) != len(source_columns):
            raise ValueError("Device reading source column names must be distinct")
        if previous_reading == next_reading or previous_reading in source_columns or next_reading in source_columns:
            raise ValueError("Window metric aliases must be distinct from source column names and from each other")

    @property
    def columns(self) -> tuple[str, ...]:
        """Source columns carried through the calculation window, in output order."""
        return (self.device, self.observed_at, self.record_id, self.reading, self.is_valid)

    def _field(self, name: str) -> Term:
        return self.table.field(name)

    def _select_columns(self, table: Table | QueryBuilder) -> tuple[Term, ...]:
        return tuple(table.field(name) for name in self.columns)

    def _visible_query(self, start: Any, end: Any) -> QueryBuilder:
        """Visible rows for display.  Invalid rows are deliberately kept."""
        observed_at = self._field(self.observed_at)
        return (
            Query.from_(self.table)
            .select(*self._select_columns(self.table))
            .where(observed_at >= start)
            .where(observed_at < end)
        )

    def _visible_valid_query(self, start: Any, end: Any) -> QueryBuilder:
        """Visible valid rows that participate in the window calculation."""
        observed_at = self._field(self.observed_at)
        return (
            Query.from_(self.table)
            .select(*self._select_columns(self.table))
            .where(observed_at >= start)
            .where(observed_at < end)
            .where(self._field(self.is_valid) == True)  # noqa: E712 - SQL boolean expression
        )

    def _neighbor_query(self, start: Any, end: Any, *, before: bool) -> QueryBuilder:
        """Return at most one valid neighbour per device on one side of the range.

        Only devices with at least one valid visible reading are considered:
        for a device with no visible valid rows there is no visible metric to
        calculate, so its history must not enter the window.

        The ranking always uses ``(observed_at, record_id)``.  For the right
        edge this means that two valid readings with the same timestamp are
        ordered by their record id, so only the first one after the range can
        become the next neighbour.
        """
        direction = Order.desc if before else Order.asc
        observed_at = self._field(self.observed_at)
        record_id = self._field(self.record_id)
        devices_with_visible_metrics = Query.from_(VISIBLE_VALID_CTE).select(
            Table(VISIBLE_VALID_CTE).field(self.device)
        )
        rank = (
            RowNumber()
            .over(self._field(self.device))
            .orderby(observed_at, order=direction)
            .orderby(record_id, order=direction)
            .as_(_NEIGHBOR_RANK)
        )
        ranked = (
            Query.from_(self.table)
            .select(*self._select_columns(self.table), rank)
            .where(self._field(self.is_valid) == True)  # noqa: E712 - SQL boolean expression
            .where(self._field(self.device).isin(devices_with_visible_metrics))
        )
        if before:
            ranked = ranked.where(observed_at < start)
        else:
            ranked = ranked.where(observed_at >= end)

        return (
            Query.from_(ranked)
            .select(*self._select_columns(ranked))
            .where(ranked.field(_NEIGHBOR_RANK) == 1)
        )

    def _window_query(self, start: Any, end: Any) -> QueryBuilder:
        """Build the bounded calculation window.

        ``wrap_set_operation_queries`` is disabled here because the result is
        used directly as a CTE body.  Wrapping each branch in parentheses
        produces ``WITH x AS ((...) UNION ALL (...))``, which SQLite rejects.
        The branches are pairwise disjoint (visible valid ``[start, end)``,
        previous ``< start`` and next ``>= end``), so ``UNION ALL`` is safe.
        """
        visible = Table(VISIBLE_VALID_CTE)
        previous = Table(PREVIOUS_CTE)
        following = Table(NEXT_CTE)

        return (
            Query.from_(visible, wrap_set_operation_queries=False)
            .select(*self._select_columns(visible))
            .union_all(Query.from_(previous).select(*self._select_columns(previous)))
            .union_all(Query.from_(following).select(*self._select_columns(following)))
        )

    def _valid_query(self, start: Any, end: Any) -> QueryBuilder:
        """Calculate the two window metrics inside the bounded window."""
        window = Table(WINDOW_CTE)
        observed_at = window.field(self.observed_at)
        record_id = window.field(self.record_id)

        previous = (
            Lag(window.field(self.reading), 1)
            .over(window.field(self.device))
            .orderby(observed_at)
            .orderby(record_id)
            .as_(self.previous_reading)
        )
        following = (
            Lead(window.field(self.reading), 1)
            .over(window.field(self.device))
            .orderby(observed_at)
            .orderby(record_id)
            .as_(self.next_reading)
        )

        return (
            Query.from_(window)
            .select(
                *self._select_columns(window),
                previous,
                following,
            )
            .where(window.field(self.is_valid) == True)  # noqa: E712 - SQL boolean expression
        )

    def _cte_queries(self, start: Any, end: Any) -> dict[str, QueryBuilder]:
        return {
            VISIBLE_CTE: self._visible_query(start, end),
            VISIBLE_VALID_CTE: self._visible_valid_query(start, end),
            PREVIOUS_CTE: self._neighbor_query(start, end, before=True),
            NEXT_CTE: self._neighbor_query(start, end, before=False),
            WINDOW_CTE: self._window_query(start, end),
            VALID_CTE: self._valid_query(start, end),
        }

    def window(self, start: Any, end: Any) -> QueryBuilder:
        """Return the bounded calculation window for inspection or testing.

        The result contains every valid visible row plus at most one valid row
        per device on each side of the range.  Invalid visible rows are not
        part of the calculation window -- the window metrics never need them,
        and they must not displace a real neighbour.  They are still returned
        by :meth:`build`, with null metrics.
        """
        ctes = self._cte_queries(start, end)
        window = Table(WINDOW_CTE)
        query = (
            Query.from_(window)
            .select(*self._select_columns(window))
            .orderby(window.field(self.device))
            .orderby(window.field(self.observed_at))
            .orderby(window.field(self.record_id))
        )
        for name, cte in ctes.items():
            query = query.with_(cte, name)
        return query

    def build(self, start: Any, end: Any, *, order: Order = Order.asc) -> QueryBuilder:
        """Build the report for the half-open range ``[start, end)``.

        :param start: Inclusive lower bound of the visible range.
        :param end: Exclusive upper bound of the visible range.
        :param order: Display order for the reading column.  The report adds
            deterministic tie-breakers on device, event time and record id.
            This order only affects presentation, never the calculated
            metrics.

        Invalid visible readings are returned with null metrics.  Neighbour
        rows outside the visible range are used for calculation but are not
        returned.
        """
        ctes = self._cte_queries(start, end)

        visible = Table(VISIBLE_CTE)
        valid = Table(VALID_CTE)
        query = (
            Query.from_(visible)
            .select(
                *self._select_columns(visible),
                valid.field(self.previous_reading).as_(self.previous_reading),
                valid.field(self.next_reading).as_(self.next_reading),
            )
            .left_join(valid)
            .on(
                (visible.field(self.device) == valid.field(self.device))
                & (visible.field(self.record_id) == valid.field(self.record_id))
            )
            .orderby(visible.field(self.reading), order=order)
            .orderby(visible.field(self.device))
            .orderby(visible.field(self.observed_at))
            .orderby(visible.field(self.record_id))
        )
        for name, cte in ctes.items():
            query = query.with_(cte, name)
        return query


def build_device_readings_report(
    table: Table | str,
    start: Any,
    end: Any,
    *,
    order: Order = Order.asc,
    device: str = "device",
    observed_at: str = "observed_at",
    record_id: str = "record_id",
    reading: str = "reading",
    is_valid: str = "is_valid",
    previous_reading: str = "previous_reading",
    next_reading: str = "next_reading",
) -> QueryBuilder:
    """Convenience wrapper around :class:`DeviceReadingsReport`.

    ``start`` is inclusive and ``end`` is exclusive.  See
    :class:`DeviceReadingsReport` for the meaning of the column arguments.
    """
    template = DeviceReadingsReport(
        table,
        device=device,
        observed_at=observed_at,
        record_id=record_id,
        reading=reading,
        is_valid=is_valid,
        previous_reading=previous_reading,
        next_reading=next_reading,
    )
    return template.build(start, end, order=order)
