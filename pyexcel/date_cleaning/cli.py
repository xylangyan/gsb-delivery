"""
pyexcel.date_cleaning.cli
~~~~~~~~~~~~~~~~~~~~~~~~~

Command-line workflow for receiving-ledger date cleaning::

    python -m pyexcel.date_cleaning.cli \\
        --delivery delivery-1.xlsx \\
        --delivery delivery-2.xlsx \\
        --delivery control.csv \\
        --confirmation confirmations.json \\
        --out-dir cleaned

Confirmations are JSON objects with ``ledger_version``, ``sheet``,
``cell``, ``cached_value`` and, when a page has two active ledgers on the
same address, ``shipment_no``.  Use ``target_delivery`` or
``target_ledger_version`` to test a confirmation against a specific
supply; the attempt is recorded even when that supply is the wrong one.
"""

import argparse
import json
import os

from .engine import CleaningRun
from .loader import load_confirmations
from .models import SOURCE_CSV_TEXT
from .output import (
    run_summary,
    select_records,
    write_csv,
    write_problem_receipt,
    write_xlsx,
)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pyexcel-date-cleaning",
        description="Clean receiving-ledger dates from XLSX and CSV",
    )
    parser.add_argument(
        "--delivery",
        action="append",
        required=True,
        help="ledger file; repeat for each supplied batch",
    )
    parser.add_argument(
        "--confirmation",
        action="append",
        default=[],
        help="JSON confirmation file; repeat as needed",
    )
    parser.add_argument(
        "--date-column",
        help="date header text, one-based index, or column letter",
    )
    parser.add_argument(
        "--shipment-column",
        help="shipment header text, one-based index, or column letter",
    )
    parser.add_argument(
        "--out-dir",
        default="date-cleaning-output",
        help="directory for cleaned outputs and receipts",
    )
    parser.add_argument(
        "--xlsx-output",
        default="cleaned.xlsx",
        help="XLSX filename within the output directory",
    )
    parser.add_argument(
        "--csv-output",
        default="cleaned.csv",
        help="CSV filename within the output directory",
    )
    parser.add_argument(
        "--receipt",
        default="problems.json",
        help="problem-receipt filename within the output directory",
    )
    parser.add_argument(
        "--include-shipment",
        action="append",
        default=None,
        help="only emit this shipment number; repeat as needed",
    )
    parser.add_argument(
        "--exclude-shipment",
        action="append",
        default=None,
        help="skip this shipment number; repeat as needed",
    )
    parser.add_argument(
        "--order-shipment",
        action="append",
        default=None,
        help="preferred shipment order; repeat as needed",
    )
    return parser


def _load_confirmation_files(paths):
    confirmations = []
    for path in paths:
        with open(path, encoding="utf-8") as stream:
            confirmations.extend(load_confirmations(json.load(stream)))
    return confirmations


def main(argv=None):
    parser = build_parser()
    arguments = parser.parse_args(argv)

    run = CleaningRun()
    for path in arguments.delivery:
        run.add_delivery(
            path,
            date_column=arguments.date_column,
            shipment_column=arguments.shipment_column,
        )

    for confirmation in _load_confirmation_files(arguments.confirmation):
        run.apply_confirmation(confirmation)

    os.makedirs(arguments.out_dir, exist_ok=True)
    ir = lambda records: select_records(  # noqa: E731
        records,
        include_shipments=arguments.include_shipment,
        exclude_shipments=arguments.exclude_shipment,
        order_by_shipment=arguments.order_shipment,
    )

    accepted = run.accepted_records()
    xlsx_path = os.path.join(arguments.out_dir, arguments.xlsx_output)
    csv_path = os.path.join(arguments.out_dir, arguments.csv_output)
    receipt_path = os.path.join(arguments.out_dir, arguments.receipt)

    xlsx_records = ir(
        [
            record
            for record in accepted
            if record.source != SOURCE_CSV_TEXT
        ]
    )
    write_xlsx(xlsx_records, xlsx_path)
    for delivery in run.deliveries:
        if delivery.kind == "csv":
            write_csv(
                ir(run.accepted_records(delivery)),
                csv_path,
                sheet=delivery.sheets[0],
            )
            break
    write_problem_receipt(
        run.issues(), receipt_path, deliveries=run.deliveries
    )

    summary = {
        "deliveries": run_summary(run),
        "confirmations": run.reconciliation(),
        "outputs": {
            "xlsx": xlsx_path,
            "csv": csv_path,
            "receipt": receipt_path,
        },
    }
    json.dump(summary, __import__("sys").stdout, ensure_ascii=False, indent=2)
    __import__("sys").stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
