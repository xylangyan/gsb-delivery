"""
End-to-end coverage for receiving-ledger date cleaning.
"""

import csv
import datetime
import json
import os

import openpyxl
import pytest

from pyexcel.date_cleaning import (
    ACCEPTED,
    BLANK_CELL,
    CONFIRMATION_APPLIED,
    CONFIRMATION_REJECTED,
    FORMULA_WITHOUT_CACHE,
    INVALID_CALENDAR_DATE,
    PLAIN_NUMBER,
    UNCONFIRMED_FORMULA_CACHE,
    CleaningRun,
    Confirmation,
    load_delivery,
    parse_date_text,
    select_records,
    write_csv,
    write_problem_receipt,
    write_xlsx,
)

from examples.date_cleaning.build_deliveries import build_deliveries


@pytest.fixture()
def ledgers(tmp_path):
    first, second, control = build_deliveries(str(tmp_path / "ledgers"))
    return first, second, control


def _records_by_cell(delivery):
    return {
        (record.sheet, record.cell): record for record in delivery.records
    }


def _confirmed_run(ledgers):
    first, second, control = ledgers
    run = CleaningRun()
    delivery_one = run.add_delivery(first)
    delivery_two = run.add_delivery(second)
    csv_delivery = run.add_delivery(control)
    return run, delivery_one, delivery_two, csv_delivery


def test_deliveries_share_visible_dates_and_shipments(ledgers):
    first, second, _ = ledgers
    delivery_one = load_delivery(first)
    delivery_two = load_delivery(second)

    assert delivery_one.sheets == delivery_two.sheets
    second_records = _records_by_cell(delivery_two)
    shared = [
        record
        for record in delivery_one.records
        if (record.sheet, record.cell) in second_records
    ]
    assert len(shared) == len(delivery_one.records)
    for record in shared:
        other = second_records[(record.sheet, record.cell)]
        assert record.shipment_no == other.shipment_no
        assert record.row_values[2] == other.row_values[2]
        assert record.raw_value == other.raw_value or record.has_formula

    assert delivery_one.version != delivery_two.version


def test_formula_changed_but_cache_is_identical(ledgers):
    first, second, _ = ledgers
    delivery_one = load_delivery(first)
    delivery_two = load_delivery(second)

    one = _records_by_cell(delivery_one)[("North", "B3")]
    two = _records_by_cell(delivery_two)[("North", "B3")]
    assert one.formula == "=DATE(2026,1,6)"
    assert two.formula == "=DATE(2026,1,6)+0"
    assert one.cached_value == two.cached_value
    assert one.record_id != two.record_id


def test_same_cell_address_on_two_pages_has_different_shipments(ledgers):
    first, _, _ = ledgers
    delivery = load_delivery(first)
    records = _records_by_cell(delivery)

    north = records[("North", "B2")]
    south = records[("South", "B2")]
    assert north.cell == south.cell
    assert north.shipment_no != south.shipment_no


def test_literal_dates_accepted_without_confirmation(ledgers):
    run, delivery_one, _, _ = _confirmed_run(ledgers)
    north = _records_by_cell(delivery_one)[("North", "B2")]
    assert north.status == ACCEPTED
    assert north.source == "xlsx_literal"
    assert north.date_value == datetime.date(2026, 1, 5)


def test_formula_cache_waits_for_confirmation(ledgers):
    run, _, delivery_two, _ = _confirmed_run(ledgers)
    record = _records_by_cell(delivery_two)[("North", "B3")]
    assert record.status == UNCONFIRMED_FORMULA_CACHE
    assert record.accepted is False
    assert record.date_value == datetime.date(2026, 1, 6)


def test_uncached_formula_and_true_blank_are_distinct(ledgers):
    run, _, delivery_two, _ = _confirmed_run(ledgers)
    records = _records_by_cell(delivery_two)
    no_cache = records[("North", "B6")]
    blank = records[("North", "B7")]

    assert no_cache.status == FORMULA_WITHOUT_CACHE
    assert no_cache.has_formula is True
    assert no_cache.has_cached_value is False
    assert no_cache.raw_value == "=DATE(2026,1,9)"
    assert blank.status == BLANK_CELL
    assert blank.has_formula is False
    assert blank.raw_value is None
    assert no_cache.status != blank.status


def test_csv_control_rejects_missing_calendar_day_and_plain_number(ledgers):
    run, _, _, csv_delivery = _confirmed_run(ledgers)
    records = {
        record.shipment_no: record for record in csv_delivery.records
    }
    assert records["C-001"].status == ACCEPTED
    assert records["C-002"].status == ACCEPTED
    assert records["C-003"].status == BLANK_CELL
    assert records["C-004"].status == INVALID_CALENDAR_DATE
    assert records["C-004"].date_value is None
    assert records["C-005"].status == PLAIN_NUMBER
    assert records["C-005"].date_value is None


def test_first_delivery_confirmation_cannot_promote_second(ledgers):
    run, delivery_one, delivery_two, _ = _confirmed_run(ledgers)
    first_record = _records_by_cell(delivery_one)[("North", "B3")]
    second_record = _records_by_cell(delivery_two)[("North", "B3")]

    confirmation = Confirmation(
        confirmation_id="conf-1",
        ledger_version=delivery_one.version,
        sheet="North",
        cell="B3",
        cached_value=first_record.cached_value,
        shipment_no=first_record.shipment_no,
    )
    result = run.apply_confirmation(confirmation, target=delivery_two)

    assert result.outcome == CONFIRMATION_REJECTED
    assert "not" in result.reason
    assert second_record.status == UNCONFIRMED_FORMULA_CACHE
    assert any(
        entry["event"] == "confirmation_rejected"
        for entry in second_record.history
    )
    assert first_record.status == UNCONFIRMED_FORMULA_CACHE


def test_page_confirmation_does_not_leak_to_same_address(ledgers):
    run, _, delivery_two, _ = _confirmed_run(ledgers)
    north = _records_by_cell(delivery_two)[("North", "B3")]

    confirmation = Confirmation(
        confirmation_id="conf-north",
        ledger_version=delivery_two.version,
        sheet="South",
        cell="B3",
        cached_value=datetime.date(2026, 1, 8),
        shipment_no="S-S-002",
    )
    result = run.apply_confirmation(confirmation)

    assert result.outcome == "no_change"
    assert result.target_version == delivery_two.version
    assert north.status == UNCONFIRMED_FORMULA_CACHE
    assert north.confirmation_id is None


def test_follow_up_confirmation_promotes_only_target(ledgers):
    run, _, delivery_two, _ = _confirmed_run(ledgers)
    records = _records_by_cell(delivery_two)
    before = {
        key: record.status
        for key, record in records.items()
        if key != ("North", "B3")
    }

    confirmation = Confirmation(
        confirmation_id="conf-2",
        ledger_version=delivery_two.version,
        sheet="North",
        cell="B3",
        cached_value=datetime.date(2026, 1, 6),
        shipment_no="S-N-002",
    )
    result = run.apply_confirmation(confirmation)

    assert result.outcome == CONFIRMATION_APPLIED
    assert result.record is records[("North", "B3")]
    assert records[("North", "B3")].status == ACCEPTED
    assert (
        records[("North", "B3")].confirmation_id == "conf-2"
    )
    for key, status in before.items():
        assert records[key].status == status


def test_prior_rejection_is_preserved_for_reconciliation(ledgers):
    run, delivery_one, delivery_two, _ = _confirmed_run(ledgers)
    target = _records_by_cell(delivery_two)[("North", "B3")]

    stale = Confirmation(
        confirmation_id="stale",
        ledger_version=delivery_two.version,
        sheet="North",
        cell="B3",
        cached_value=datetime.date(2025, 12, 31),
        shipment_no="S-N-002",
    )
    rejected = run.apply_confirmation(stale)
    assert rejected.outcome == CONFIRMATION_REJECTED
    assert rejected.reason == "confirmed cached value does not match the cell"

    good = Confirmation(
        confirmation_id="good",
        ledger_version=delivery_two.version,
        sheet="North",
        cell="B3",
        cached_value=datetime.date(2026, 1, 6),
        shipment_no="S-N-002",
    )
    applied = run.apply_confirmation(good)
    assert applied.outcome == CONFIRMATION_APPLIED

    history = [
        entry
        for entry in target.history
        if entry["event"] == "confirmation_rejected"
    ]
    assert history
    assert history[0]["confirmation_id"] == "stale"
    reconciliation = run.reconciliation()
    assert reconciliation[0]["outcome"] == CONFIRMATION_REJECTED
    assert reconciliation[0]["reason"] == (
        "confirmed cached value does not match the cell"
    )


def test_filter_one_row_and_reorder_without_reattaching_confirmation(ledgers):
    run, delivery_one, delivery_two, _ = _confirmed_run(ledgers)
    target = _records_by_cell(delivery_two)[("North", "B3")]
    confirmation = Confirmation(
        confirmation_id="conf-3",
        ledger_version=delivery_two.version,
        sheet="North",
        cell="B3",
        cached_value=datetime.date(2026, 1, 6),
        shipment_no="S-N-002",
    )
    run.apply_confirmation(confirmation)

    accepted = run.accepted_records(delivery_two)
    selected = select_records(
        accepted,
        include_shipments=["S-N-001", "S-N-002", "S-N-003"],
        exclude_shipments=["S-N-003"],
        order_by_shipment=["S-N-002", "S-N-001"],
    )
    assert [record.shipment_no for record in selected] == [
        "S-N-002",
        "S-N-001",
    ]
    assert target.record_id in {record.record_id for record in selected}
    assert "S-N-002" in {record.shipment_no for record in selected}
    assert "S-N-003" not in {record.shipment_no for record in selected}


def test_same_address_elsewhere_does_not_inherit_confirmation(ledgers):
    run, _, delivery_two, _ = _confirmed_run(ledgers)
    records = _records_by_cell(delivery_two)
    target = records[("North", "B3")]
    other_page = records[("South", "B3")]

    confirmation = Confirmation(
        confirmation_id="scoped",
        ledger_version=delivery_two.version,
        sheet="North",
        cell="B3",
        cached_value=datetime.date(2026, 1, 6),
        shipment_no="S-N-002",
    )
    run.apply_confirmation(confirmation)

    assert target.status == ACCEPTED
    assert target.confirmation_id == "scoped"
    assert other_page.status == ACCEPTED
    assert other_page.confirmation_id is None
    assert other_page.source == "xlsx_literal"

    selected = select_records(
        run.accepted_records(delivery_two),
        include_shipments=["S-N-002"],
        order_by_shipment=["S-N-002"],
    )
    assert [record.shipment_no for record in selected] == ["S-N-002"]


def test_receipt_keeps_formula_and_true_source(ledgers):
    run, _, delivery_two, _ = _confirmed_run(ledgers)
    issues = run.issues(delivery_two)
    from pyexcel.date_cleaning import problem_receipt

    receipt = problem_receipt(issues)

    by_cell = {
        (issue["sheet"], issue["cell"]): issue
        for issue in receipt["issues"]
    }
    uncached = by_cell[("North", "B6")]
    blank = by_cell[("North", "B7")]
    pending = by_cell[("North", "B3")]
    plain = by_cell[("North", "B5")]

    assert uncached["category"] == "formula_without_cache"
    assert uncached["formula"] == "=DATE(2026,1,9)"
    assert uncached["source"] == "formula"
    assert uncached["cached_value"] is None
    assert blank["category"] == "blank_cell"
    assert blank["raw_value"] is None
    assert blank["source"] == "xlsx_literal"
    assert blank["formula"] is None
    assert pending["category"] == "unconfirmed_formula_cache"
    assert pending["cached_value"] == "2026-01-06"
    assert pending["formula"] == "=DATE(2026,1,6)+0"
    assert pending["source"] == "formula"
    assert plain["category"] == "plain_number"
    assert plain["raw_value"] == 46000
    assert plain["source"] == "xlsx_literal"


def test_outputs_contain_only_accepted_dates_as_real_dates(tmp_path, ledgers):
    run, _, delivery_two, csv_delivery = _confirmed_run(ledgers)
    confirmation = Confirmation(
        confirmation_id="conf-out",
        ledger_version=delivery_two.version,
        sheet="North",
        cell="B3",
        cached_value=datetime.date(2026, 1, 6),
        shipment_no="S-N-002",
    )
    run.apply_confirmation(confirmation)

    xlsx_path = str(tmp_path / "cleaned.xlsx")
    csv_path = str(tmp_path / "cleaned.csv")
    select = lambda records: select_records(records)  # noqa: E731
    write_xlsx(select(run.accepted_records(delivery_two)), xlsx_path)
    write_csv(select(run.accepted_records(csv_delivery)), csv_path)

    workbook = openpyxl.load_workbook(xlsx_path)
    sheet = workbook["North"]
    written = {
        sheet.cell(row=row, column=1).value: sheet.cell(row=row, column=2)
        for row in range(2, sheet.max_row + 1)
    }
    assert set(written) == {"S-N-001", "S-N-002", "S-N-003"}
    assert isinstance(written["S-N-002"].value, datetime.datetime)
    assert written["S-N-002"].value.date() == datetime.date(2026, 1, 6)
    assert written["S-N-002"].number_format == "yyyy-mm-dd"
    assert written["S-N-002"].data_type == "d"
    assert written["S-N-001"].data_type == "d"
    assert written["S-N-003"].data_type == "d"

    south = workbook["South"]
    south_shipments = [
        south.cell(row=row, column=1).value
        for row in range(2, south.max_row + 1)
    ]
    assert south_shipments == ["S-S-001", "S-S-002"]

    with open(csv_path, encoding="utf-8", newline="") as stream:
        rows = list(csv.reader(stream))
    data = {row[0]: row for row in rows[1:]}
    assert data["C-001"][1] == "2026-01-05"
    assert data["C-002"][1] == "2026-01-06"
    assert "C-003" not in data
    assert "C-004" not in data
    assert "C-005" not in data


def test_only_designated_date_column_is_changed(tmp_path, ledgers):
    run, delivery_one, _, _ = _confirmed_run(ledgers)
    path = str(tmp_path / "one-column.xlsx")
    accepted = run.accepted_records(delivery_one)
    write_xlsx(accepted, path)
    workbook = openpyxl.load_workbook(path)
    sheet = workbook["North"]
    written_shipments = [
        sheet.cell(row=row, column=1).value
        for row in range(2, sheet.max_row + 1)
    ]
    assert written_shipments == ["S-N-001", "S-N-003"]
    note_values = [
        sheet.cell(row=row, column=3).value
        for row in range(2, sheet.max_row + 1)
    ]
    assert note_values == ["literal", "literal"]


def test_parse_date_text_rejects_numbers_and_impossible_dates():
    assert parse_date_text("46000")[1] == "plain_number"
    assert parse_date_text("2026-02-30")[1] == "invalid_calendar_date"
    assert parse_date_text("not a date")[1] == "invalid_date_text"
    assert parse_date_text("2026-01-05")[1] == "valid_date"
    assert parse_date_text("")[1] == "blank"


def test_cli_writes_outputs_and_receipt(tmp_path, ledgers, capsys):
    from pyexcel.date_cleaning.cli import main

    first, second, control = ledgers
    delivery_one = load_delivery(first)
    delivery_two = load_delivery(second)
    stale = tmp_path / "stale.json"
    stale.write_text(
        json.dumps(
            {
                "confirmations": [
                    {
                        "confirmation_id": "stale",
                        "ledger_version": delivery_one.version,
                        "sheet": "North",
                        "cell": "B3",
                        "cached_value": "2026-01-06",
                        "shipment_no": "S-N-002",
                        "target_delivery": os.path.basename(second),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    follow_up = tmp_path / "follow-up.json"
    follow_up.write_text(
        json.dumps(
            {
                "confirmations": [
                    {
                        "confirmation_id": "follow-up",
                        "ledger_version": delivery_two.version,
                        "sheet": "North",
                        "cell": "B3",
                        "cached_value": "2026-01-06",
                        "shipment_no": "S-N-002",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "out"
    exit_code = main(
        [
            "--delivery",
            first,
            "--delivery",
            second,
            "--delivery",
            control,
            "--confirmation",
            str(stale),
            "--confirmation",
            str(follow_up),
            "--out-dir",
            str(out_dir),
        ]
    )
    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["outputs"]["xlsx"].endswith("cleaned.xlsx")
    assert os.path.exists(summary["outputs"]["xlsx"])
    assert os.path.exists(summary["outputs"]["csv"])
    assert os.path.exists(summary["outputs"]["receipt"])

    workbook = openpyxl.load_workbook(summary["outputs"]["xlsx"])
    north = workbook["North"]
    rows = {
        north.cell(row=row, column=1).value: north.cell(row=row, column=2)
        for row in range(2, north.max_row + 1)
    }
    assert rows["S-N-002"].value.date() == datetime.date(2026, 1, 6)
    assert rows["S-N-002"].number_format == "yyyy-mm-dd"

    with open(summary["outputs"]["receipt"], encoding="utf-8") as stream:
        receipt = json.load(stream)
    categories = {issue["category"] for issue in receipt["issues"]}
    assert "formula_without_cache" in categories
    assert "blank_cell" in categories

    outcomes = [
        item["outcome"] for item in summary["confirmations"]
    ]
    assert outcomes == [CONFIRMATION_REJECTED, CONFIRMATION_APPLIED]


def test_confirmation_without_shipment_still_uses_the_page(ledgers):
    run, _, delivery_two, _ = _confirmed_run(ledgers)
    north = _records_by_cell(delivery_two)[("North", "B3")]
    south = _records_by_cell(delivery_two)[("South", "B3")]

    confirmation = Confirmation(
        confirmation_id="page-only",
        ledger_version=delivery_two.version,
        sheet="North",
        cell="B3",
        cached_value=datetime.date(2026, 1, 6),
    )
    result = run.apply_confirmation(confirmation, target=delivery_two)

    assert result.outcome == CONFIRMATION_APPLIED
    assert north.status == ACCEPTED
    assert north.confirmation_id == "page-only"
    assert south.status == ACCEPTED
    assert south.confirmation_id is None


def test_text_cached_formula_result_is_still_scoped(ledgers):
    run, _, delivery_two, _ = _confirmed_run(ledgers)
    record = _records_by_cell(delivery_two)[("North", "B3")]
    record.cached_value = "2026-01-06"
    record.set_status(UNCONFIRMED_FORMULA_CACHE, "pending", accepted=False)

    good = Confirmation(
        confirmation_id="text-cache",
        ledger_version=delivery_two.version,
        sheet="North",
        cell="B3",
        cached_value="2026-01-06",
        shipment_no="S-N-002",
    )
    result = run.apply_confirmation(good, target=delivery_two)
    assert result.outcome == CONFIRMATION_APPLIED
    assert record.status == ACCEPTED


def test_writers_refuse_unaccepted_records(tmp_path, ledgers):
    run, _, delivery_two, _ = _confirmed_run(ledgers)
    pending = _records_by_cell(delivery_two)[("North", "B3")]
    with pytest.raises(ValueError, match="only accepted records"):
        write_xlsx([pending], str(tmp_path / "bad.xlsx"))
    with pytest.raises(ValueError, match="only accepted records"):
        write_csv([pending], str(tmp_path / "bad.csv"))


def test_problem_receipt_is_json_serializable(tmp_path, ledgers):
    run, _, delivery_two, csv_delivery = _confirmed_run(ledgers)
    path = str(tmp_path / "receipt.json")
    payload = write_problem_receipt(
        run.issues(), path, deliveries=run.deliveries
    )
    with open(path, encoding="utf-8") as stream:
        loaded = json.load(stream)
    assert loaded == payload
    assert loaded["issues"]
    assert loaded["deliveries"]
