"""
Receive two ledger deliveries, attempt a stale confirmation, then apply
the correct follow-up confirmation and write cleaned outputs.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from build_deliveries import build_deliveries

from pyexcel.date_cleaning import (
    CleaningRun,
    Confirmation,
    problem_receipt,
    select_records,
    write_csv,
    write_problem_receipt,
    write_xlsx,
)


def main(workdir=None):
    if workdir is None:
        workdir = tempfile.mkdtemp(prefix="date-cleaning-")
    else:
        os.makedirs(workdir, exist_ok=True)
    first, second, control = build_deliveries(workdir)
    run = CleaningRun()
    delivery_one = run.add_delivery(first)
    delivery_two = run.add_delivery(second)
    csv_delivery = run.add_delivery(control)

    def cell(delivery, sheet, address):
        return next(
            record
            for record in delivery.records
            if record.sheet == sheet and record.cell == address
        )

    original = cell(delivery_one, "North", "B3")
    changed = cell(delivery_two, "North", "B3")
    print("batch one formula:", original.formula)
    print("batch two formula:", changed.formula)
    print("both cached dates:", original.cached_value, changed.cached_value)

    stale = Confirmation(
        confirmation_id="batch-one-confirmation",
        ledger_version=delivery_one.version,
        sheet="North",
        cell="B3",
        cached_value=original.cached_value,
        shipment_no="S-N-002",
    )
    stale_result = run.apply_confirmation(stale, target=delivery_two)
    print(
        "stale confirmation:",
        stale_result.outcome,
        "-",
        stale_result.reason,
    )

    follow_up = Confirmation(
        confirmation_id="batch-two-confirmation",
        ledger_version=delivery_two.version,
        sheet="North",
        cell="B3",
        cached_value=changed.cached_value,
        shipment_no="S-N-002",
    )
    follow_up_result = run.apply_confirmation(follow_up, target=delivery_two)
    print(
        "follow-up confirmation:",
        follow_up_result.outcome,
        "-",
        follow_up_result.reason,
    )

    accepted_xlsx = select_records(
        [
            record
            for record in run.accepted_records(delivery_two)
            if record.source != "csv_text"
        ]
    )
    accepted_csv = select_records(run.accepted_records(csv_delivery))

    xlsx_path = os.path.join(workdir, "cleaned.xlsx")
    csv_path = os.path.join(workdir, "cleaned.csv")
    receipt_path = os.path.join(workdir, "problems.json")
    write_xlsx(accepted_xlsx, xlsx_path)
    write_csv(accepted_csv, csv_path)
    write_problem_receipt(
        run.issues(), receipt_path, deliveries=run.deliveries
    )

    print("xlsx:", xlsx_path)
    print("csv:", csv_path)
    print("issues:", len(problem_receipt(run.issues())["issues"]))
    print("reconciliation:", run.reconciliation())

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main(sys.argv[1] if len(sys.argv) > 1 else "date-cleaning-output")
    )
