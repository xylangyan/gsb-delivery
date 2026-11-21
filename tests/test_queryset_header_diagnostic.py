"""Offline reproduction for the query_sets first-order accounting loss.

The sample rows carry a ``source_id`` so that a row whose values happen to
equal the column names is still identified by provenance, never by value.
Every read gets a fresh :class:`OneShotQuery`; the query object fails loudly
if it is iterated twice, and the trace is collected during that same
iteration.

Run the demonstration with::

    python -m tests.test_queryset_header_diagnostic

Run the assertions with::

    pytest tests/test_queryset_header_diagnostic.py -q
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import pytest

import pyexcel as pe
import pyexcel.core as pe_core
from pyexcel.sheet import Sheet
from pyexcel_io.constants import SKIP_DATA, STOP_ITERATION, TAKE_DATA
from pyexcel_io.database.querysets import QuerysetsReader
from pyexcel_io.sheet import SheetReader
from pyexcel_io.utils import _index_filter

SYNTHETIC_ID = "<synthetic-column-names>"
COLUMNS = ["order_no", "carrier__name", "amount"]
MISSING = "<missing>"


@dataclass
class Carrier:
    name: str


@dataclass
class Order:
    source_id: str
    order_no: str
    carrier: Optional[Carrier]
    amount: str


ROWS = [
    Order("legacy-000", "order_no", Carrier("carrier__name"), "amount"),
    Order("order-001", "SO-1001", Carrier("Carrier A"), "100.00"),
    Order("order-002", "SO-1002", Carrier("Carrier B"), "200.00"),
    Order("order-003", "SO-1003", None, "300.00"),
    Order("order-004", "SO-1004", Carrier("Carrier D"), "400.00"),
    Order("order-005", "SO-1005", Carrier("Carrier F"), "500.00"),
    Order("order-006", "SO-1006", Carrier("Carrier G"), "600.00"),
    Order("order-007", "SO-1007", Carrier("Carrier H"), "700.00"),
]

EXPECTED_RECORDS = [
    {
        "order_no": "SO-1001",
        "carrier__name": "Carrier A",
        "amount": "100.00",
    },
    {
        "order_no": "SO-1002",
        "carrier__name": "Carrier B",
        "amount": "200.00",
    },
    {
        "order_no": "SO-1003",
        "carrier__name": MISSING,
        "amount": "300.00",
    },
]


class IdentifiedRow(list):
    """A row that keeps its provenance through the sheet payload."""

    def __init__(self, values: Sequence[Any], source_id: str):
        super().__init__(values)
        self.source_id = source_id


class OneShotQuery:
    """Small query-set stand-in that cannot be re-evaluated."""

    def __init__(self, rows: Sequence[Order]):
        self._rows = tuple(rows)
        self.iterations = 0
        self.len_calls = 0
        self.seen: List[str] = []

    def __len__(self):
        self.len_calls += 1
        return len(self._rows)

    def __iter__(self):
        if self.iterations:
            raise AssertionError("query object was iterated more than once")
        self.iterations += 1
        for row in self._rows:
            self.seen.append(row.source_id)
            yield row


def normalize_missing(row: Sequence[Any]) -> List[Any]:
    """Normalize a missing relation identically for every record API."""
    return [MISSING if value is None else value for value in row]


def source_id(value: Any) -> str:
    if isinstance(value, IdentifiedRow):
        return value.source_id
    if isinstance(value, list):
        return SYNTHETIC_ID
    return getattr(value, "source_id", "<unknown>")


@dataclass
class Trace:
    timeline: List[Dict[str, Any]] = field(default_factory=list)
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    payloads: List[Dict[str, Any]] = field(default_factory=list)
    headers: List[Dict[str, Any]] = field(default_factory=list)

    def _event(self, stage: str, **fields) -> Dict[str, Any]:
        event = {"seq": len(self.timeline), "stage": stage}
        event.update(fields)
        self.timeline.append(event)
        return event

    def candidate(self, index: int, row: Any) -> None:
        event = self._event(
            "source.row_iterator",
            candidate_index=index,
            source_id=source_id(row),
        )
        self.candidates.append(event)

    def decision(
        self,
        index: int,
        row: Any,
        decision: int,
        filter_source: str,
    ) -> None:
        event = self._event(
            "filter.skip_row",
            candidate_index=index,
            source_id=source_id(row),
            decision=decision,
            filter_source=filter_source,
        )
        self.decisions.append(event)

    def payload(self, position: int, row: Any, values: Sequence[Any]) -> None:
        event = self._event(
            "payload.to_array",
            payload_position=position,
            source_id=source_id(row),
            values=list(values),
        )
        self.payloads.append(event)

    def header(self, position: int, row: Any, consumer: str) -> None:
        if any(
            item["payload_position"] == position
            and item["source_id"] == source_id(row)
            for item in self.headers
        ):
            return
        event = self._event(
            "record.header",
            payload_position=position,
            source_id=source_id(row),
            consumer=consumer,
        )
        self.headers.append(event)

    def candidate_ids(self) -> List[str]:
        return [item["source_id"] for item in self.candidates]

    def payload_ids(self) -> List[str]:
        return [item["source_id"] for item in self.payloads]

    def decision_for(self, index: int) -> Optional[int]:
        for item in self.decisions:
            if item["candidate_index"] == index:
                return item["decision"]
        return None

    def header_id(self) -> Optional[str]:
        if not self.headers:
            return None
        return self.headers[0]["source_id"]

    def stages_for(self, row_source_id: str) -> List[str]:
        return [
            item["stage"]
            for item in self.timeline
            if item["source_id"] == row_source_id
        ]


@contextmanager
def trace_query_read():
    """Trace candidates, filter decisions, payload rows and header use."""

    trace = Trace()
    original_row_iterator = QuerysetsReader.row_iterator
    original_sheet_init = SheetReader.__init__
    original_to_array = SheetReader.to_array
    original_name_columns = Sheet.name_columns_by_row
    original_zip_longest = pe_core.zip_longest

    def row_iterator(self):
        for index, row in enumerate(original_row_iterator(self)):
            self._trace_row = row
            trace.candidate(index, row)
            yield row

    def sheet_init(self, sheet, *args, **kwargs):
        user_skip = kwargs.get("skip_row_func")

        def traced_skip(index, start, limit):
            if user_skip is None:
                decision = _index_filter(index, start, limit)
                filter_source = "start_row/row_limit"
            else:
                decision = user_skip(index, start, limit)
                filter_source = "caller skip_row_func"
            trace.decision(
                index,
                getattr(sheet, "_trace_row", None),
                decision,
                filter_source,
            )
            return decision

        kwargs["skip_row_func"] = traced_skip
        original_sheet_init(self, sheet, *args, **kwargs)

    def to_array(self):
        for values in original_to_array(self):
            row = getattr(self._native_sheet, "_trace_row", None)
            identified = IdentifiedRow(values, source_id(row))
            trace.payload(len(trace.payloads), row, identified)
            yield identified

    def name_columns_by_row(self, row_index):
        try:
            row = self.get_internal_array()[row_index]
        except (AttributeError, IndexError):
            row = None
        trace.header(row_index, row, "sheet.name_columns_by_row")
        return original_name_columns(self, row_index)

    def zip_longest(*args, **kwargs):
        if args and isinstance(args[0], IdentifiedRow):
            trace.header(0, args[0], "iget_records")
        return original_zip_longest(*args, **kwargs)

    QuerysetsReader.row_iterator = row_iterator
    SheetReader.__init__ = sheet_init
    SheetReader.to_array = to_array
    Sheet.name_columns_by_row = name_columns_by_row
    pe_core.zip_longest = zip_longest
    try:
        yield trace
    finally:
        QuerysetsReader.row_iterator = original_row_iterator
        SheetReader.__init__ = original_sheet_init
        SheetReader.to_array = original_to_array
        Sheet.name_columns_by_row = original_name_columns
        pe_core.zip_longest = original_zip_longest


@dataclass
class ReadResult:
    label: str
    api: str
    result: Any
    query: OneShotQuery
    trace: Trace

    def payload_ids(self) -> List[str]:
        return self.trace.payload_ids()

    def header_id(self) -> Optional[str]:
        return self.trace.header_id()

    def record_pairs(self):
        """Pair each record with the payload identity from the same read."""
        payloads = self.trace.payloads
        if not payloads:
            return []
        data = payloads[1:]
        assert len(data) == len(self.result)
        return [
            (payload["source_id"], record)
            for payload, record in zip(data, self.result)
        ]


def run_read(
    label: str,
    api: str,
    rows: Sequence[Order] = ROWS,
    **keywords,
) -> ReadResult:
    query = OneShotQuery(rows)
    keywords.setdefault("row_renderer", normalize_missing)

    def call():
        if api == "get_records":
            return pe.get_records(
                column_names=COLUMNS, query_sets=query, **keywords
            )
        if api == "iget_records":
            return list(
                pe.iget_records(
                    column_names=COLUMNS, query_sets=query, **keywords
                )
            )
        if api == "sheet_to_records":
            sheet = pe.get_sheet(
                column_names=COLUMNS,
                query_sets=query,
                name_columns_by_row=0,
                **keywords,
            )
            return list(sheet.to_records())
        if api == "get_array":
            return pe.get_array(
                column_names=COLUMNS, query_sets=query, **keywords
            )
        if api == "iget_array":
            return list(
                pe.iget_array(
                    column_names=COLUMNS, query_sets=query, **keywords
                )
            )
        raise ValueError(api)

    with trace_query_read() as trace:
        result = call()
    assert query.iterations == 1, (label, api, query.iterations)
    return ReadResult(label, api, result, query, trace)


def skip_legacy_and_stop(index, start, limit):
    if index == 0:
        return TAKE_DATA
    if index == 1:
        return SKIP_DATA
    if index >= 5:
        return STOP_ITERATION
    return TAKE_DATA


class PagePlan:
    """One skip function whose returned decisions are swapped per page."""

    def __init__(self, decisions: Optional[Dict[int, int]] = None):
        self.decisions = dict(decisions or {})
        self.calls = 0

    def set_page(self, decisions: Dict[int, int]) -> None:
        self.decisions = dict(decisions)

    def __call__(self, index, start, limit):
        self.calls += 1
        return self.decisions.get(index, TAKE_DATA)


PAGE_ONE_DECISIONS = {
    0: TAKE_DATA,
    1: SKIP_DATA,
    2: TAKE_DATA,
    3: TAKE_DATA,
    4: TAKE_DATA,
    5: STOP_ITERATION,
}
PAGE_TWO_DECISIONS = {
    0: TAKE_DATA,
    1: SKIP_DATA,
    2: SKIP_DATA,
    3: SKIP_DATA,
    4: SKIP_DATA,
    5: TAKE_DATA,
    6: TAKE_DATA,
    7: TAKE_DATA,
    8: STOP_ITERATION,
}


def status(trace: Trace, index: int) -> str:
    candidate_ids = trace.candidate_ids()
    if index >= len(candidate_ids):
        return "not-entered"
    decision = trace.decision_for(index)
    if decision == SKIP_DATA:
        return "explicit-skip"
    if decision == STOP_ITERATION:
        return "page-truncated"
    row_id = candidate_ids[index]
    if trace.header_id() == row_id:
        return "header"
    if row_id in trace.payload_ids():
        return "record"
    return "taken"


def order_numbers(result: ReadResult) -> List[str]:
    return [record["order_no"] for record in result.result]


def test_start_row_repro_uses_first_order_as_header():
    result = run_read(
        "start-row", "get_records", start_row=1, row_limit=4
    )

    assert result.result == EXPECTED_RECORDS
    assert result.payload_ids() == [
        "legacy-000",
        "order-001",
        "order-002",
        "order-003",
    ]
    assert result.header_id() == "legacy-000"
    assert status(result.trace, 0) == "explicit-skip"
    assert status(result.trace, 1) == "header"
    assert status(result.trace, 5) == "page-truncated"
    assert status(result.trace, 6) == "not-entered"


def test_skip_func_repro_excludes_first_order_before_records():
    result = run_read(
        "skip-func",
        "get_records",
        skip_row_func=skip_legacy_and_stop,
    )

    assert result.result == EXPECTED_RECORDS
    assert result.payload_ids() == [
        SYNTHETIC_ID,
        "order-001",
        "order-002",
        "order-003",
    ]
    assert result.header_id() == SYNTHETIC_ID
    assert status(result.trace, 0) == "header"
    assert status(result.trace, 1) == "explicit-skip"
    assert status(result.trace, 5) == "page-truncated"
    assert status(result.trace, 6) == "not-entered"


def test_record_apis_agree_on_both_paths():
    cases = {
        "start-row": {"start_row": 1, "row_limit": 4},
        "skip-func": {"skip_row_func": skip_legacy_and_stop},
    }
    for label, keywords in cases.items():
        get_records = run_read(label + "/get", "get_records", **keywords)
        iget_records = run_read(label + "/iget", "iget_records", **keywords)
        sheet_records = run_read(
            label + "/sheet", "sheet_to_records", **keywords
        )

        assert get_records.result == EXPECTED_RECORDS
        assert iget_records.result == EXPECTED_RECORDS
        assert sheet_records.result == EXPECTED_RECORDS
        assert get_records.result == iget_records.result
        assert get_records.result == sheet_records.result
        assert get_records.header_id() == iget_records.header_id()
        assert get_records.header_id() == sheet_records.header_id()
        if label == "start-row":
            assert get_records.header_id() == "legacy-000"
        else:
            assert get_records.header_id() == SYNTHETIC_ID


def test_one_query_object_is_not_reused_across_read_paths():
    query = OneShotQuery(ROWS)
    pe.get_records(
        column_names=COLUMNS,
        query_sets=query,
        start_row=1,
        row_limit=4,
        row_renderer=normalize_missing,
    )
    with pytest.raises(AssertionError):
        list(
            pe.iget_records(
                column_names=COLUMNS,
                query_sets=query,
                row_renderer=normalize_missing,
            )
        )


def test_sheet_without_named_columns_has_no_record_series():
    query = OneShotQuery(ROWS)
    with trace_query_read() as trace:
        sheet = pe.get_sheet(
            column_names=COLUMNS,
            query_sets=query,
            row_renderer=normalize_missing,
            start_row=1,
            row_limit=4,
        )
        assert sheet.colnames == []
        with pytest.raises(ValueError):
            list(sheet.to_records())

    assert query.iterations == 1
    assert trace.payload_ids() == [
        "legacy-000",
        "order-001",
        "order-002",
        "order-003",
    ]


def test_raw_row_entry_keeps_provenance_not_value_equality():
    start_rows = run_read(
        "start-row/raw",
        "get_array",
        start_row=1,
        row_limit=4,
    )
    skip_rows = run_read(
        "skip-func/raw",
        "get_array",
        skip_row_func=skip_legacy_and_stop,
    )

    start_first = start_rows.result[0]
    skip_first = skip_rows.result[0]

    assert list(start_first) == COLUMNS
    assert list(skip_first) == COLUMNS
    assert start_first.source_id == "legacy-000"
    assert skip_first.source_id == SYNTHETIC_ID

    start_iget = run_read(
        "start-row/iget-raw",
        "iget_array",
        start_row=1,
        row_limit=4,
    )
    skip_iget = run_read(
        "skip-func/iget-raw",
        "iget_array",
        skip_row_func=skip_legacy_and_stop,
    )

    assert list(start_iget.result[0]) == COLUMNS
    assert list(skip_iget.result[0]) == COLUMNS
    assert start_iget.result[0].source_id == "legacy-000"
    assert skip_iget.result[0].source_id == SYNTHETIC_ID


def test_missing_carrier_order_is_retained_with_columns_and_values():
    cases = [
        run_read("start-row", "get_records", start_row=1, row_limit=4),
        run_read(
            "skip-func",
            "get_records",
            skip_row_func=skip_legacy_and_stop,
        ),
    ]
    for result in cases:
        pairs = result.record_pairs()
        ids = [source_id for source_id, _ in pairs]
        assert "order-003" in ids
        record = dict(pairs)["order-003"]
        assert list(record.keys()) == COLUMNS
        assert record == {
            "order_no": "SO-1003",
            "carrier__name": MISSING,
            "amount": "300.00",
        }


def test_two_page_revision_changes_only_skip_decisions():
    plan = PagePlan()
    plan.set_page(PAGE_ONE_DECISIONS)
    page_one = run_read(
        "page-1", "get_records", skip_row_func=plan
    )

    plan.set_page(PAGE_TWO_DECISIONS)
    page_two = run_read(
        "page-2", "get_records", skip_row_func=plan
    )

    assert order_numbers(page_one) == ["SO-1001", "SO-1002", "SO-1003"]
    assert order_numbers(page_two) == ["SO-1004", "SO-1005", "SO-1006"]
    assert page_one.header_id() == SYNTHETIC_ID
    assert page_two.header_id() == SYNTHETIC_ID

    assert status(page_one.trace, 0) == "header"
    assert status(page_one.trace, 1) == "explicit-skip"
    assert status(page_one.trace, 5) == "page-truncated"
    assert status(page_one.trace, 6) == "not-entered"

    assert status(page_two.trace, 0) == "header"
    assert status(page_two.trace, 1) == "explicit-skip"
    assert status(page_two.trace, 4) == "explicit-skip"
    assert status(page_two.trace, 5) == "record"
    assert status(page_two.trace, 8) == "page-truncated"
    assert status(page_two.trace, 9) == "not-entered"

    assert "order-004" not in page_one.payload_ids()
    assert "order-004" in page_two.payload_ids()
    assert "legacy-000" not in page_one.payload_ids()
    assert "legacy-000" not in page_two.payload_ids()


def test_timeline_reaches_source_filter_payload_and_header():
    result = run_read(
        "timeline", "sheet_to_records", start_row=1, row_limit=4
    )

    assert result.trace.stages_for("legacy-000") == [
        "source.row_iterator",
        "filter.skip_row",
        "payload.to_array",
        "record.header",
    ]
    assert result.trace.stages_for("order-001") == [
        "source.row_iterator",
        "filter.skip_row",
        "payload.to_array",
    ]

    header = result.trace.headers[0]
    assert header["payload_position"] == 0
    assert header["source_id"] == "legacy-000"
    assert header["consumer"] == "sheet.name_columns_by_row"


def test_naive_start_row_pagination_is_not_the_verified_method():
    page_with_header = run_read(
        "naive-zero", "get_records", start_row=0, row_limit=2
    )
    page_after_header = run_read(
        "naive-one", "get_records", start_row=1, row_limit=2
    )

    assert order_numbers(page_with_header) == ["order_no"]
    assert order_numbers(page_after_header) == ["SO-1001"]


def test_query_slice_pages_keep_the_real_first_order():
    pages = [
        (0, 2, ["order_no", "SO-1001"]),
        (2, 2, ["SO-1002", "SO-1003"]),
        (4, 2, ["SO-1004", "SO-1005"]),
        (6, 2, ["SO-1006", "SO-1007"]),
    ]
    expected_ids = [
        ["legacy-000", "order-001"],
        ["order-002", "order-003"],
        ["order-004", "order-005"],
        ["order-006", "order-007"],
    ]
    for (offset, size, expected_orders), expected in zip(pages, expected_ids):
        result = run_read(
            "slice-%d" % offset,
            "get_records",
            rows=ROWS[offset : offset + size],
        )
        assert order_numbers(result) == expected_orders
        assert result.payload_ids()[1:] == expected
        assert result.query.iterations == 1

    first_page = run_read("slice-first", "get_records", rows=ROWS[0:2])
    assert first_page.header_id() == SYNTHETIC_ID
    assert first_page.payload_ids() == [
        SYNTHETIC_ID,
        "legacy-000",
        "order-001",
    ]


def print_result(result: ReadResult) -> None:
    print("\n== %s (%s) ==" % (result.label, result.api))
    print("records:", result.result)
    print("query iterations:", result.query.iterations)
    print("candidate ids:", result.trace.candidate_ids())
    decisions = [
        (
            item["candidate_index"],
            item["source_id"],
            item["decision"],
        )
        for item in result.trace.decisions
    ]
    print("decisions:", decisions)
    print("payload ids:", result.payload_ids())
    print("headers:", result.trace.headers)


def main() -> None:
    a = run_read(
        "A/start_row=1,row_limit=4",
        "get_records",
        start_row=1,
        row_limit=4,
    )
    b = run_read(
        "B/skip_row_func",
        "get_records",
        skip_row_func=skip_legacy_and_stop,
    )
    print_result(a)
    print_result(b)
    print("\nparity:", a.result == b.result)
    print("A header source:", a.header_id())
    print("B header source:", b.header_id())

    plan = PagePlan()
    plan.set_page(PAGE_ONE_DECISIONS)
    page_one = run_read("page-1", "get_records", skip_row_func=plan)
    plan.set_page(PAGE_TWO_DECISIONS)
    page_two = run_read("page-2", "get_records", skip_row_func=plan)
    print_result(page_one)
    print_result(page_two)
    print("\npage one orders:", order_numbers(page_one))
    print("page two orders:", order_numbers(page_two))


if __name__ == "__main__":
    main()
