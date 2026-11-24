import sqlite3
import unittest

import pygrametl
from pygrametl.reconciliation import OutboundReconciliationService, reconcile_receipts
from pygrametl.tables import AccumulatingSnapshotFactTable


def ticket(number, hour):
    return {
        "ticket_no": number,
        "shipped_at": "2026-11-25 %02d:00:00" % hour,
    }


def receipt(number, hour, candidates, carrier="C"):
    minute = "00"
    if isinstance(hour, str):
        hour, minute = hour.split(":")
    return {
        "carrier_code": carrier,
        "receipt_no": number,
        "signed_at": "2026-11-25 %02d:%s:00" % (int(hour), minute),
        "candidate_ticket_nos": candidates,
    }


class ReconciliationAlgorithmTest(unittest.TestCase):
    def test_finance_scenario(self):
        result_41 = reconcile_receipts(
            tickets=[ticket("P17", 8), ticket("P18", 9), ticket("P19", 10)],
            receipts=[
                receipt("R1", 11, ["P17", "P18"]),
                receipt("R2", 12, ["P18", "P19"]),
                receipt("R3", 14, ["P19"]),
            ],
        )
        self.assertEqual("R1", result_41["P17"]["receipt_no"])
        self.assertEqual("R2", result_41["P18"]["receipt_no"])
        self.assertEqual("R3", result_41["P19"]["receipt_no"])

        service = OutboundReconciliationService(default_carrier_code="C")
        service.publish_correction(
            41,
            tickets=[ticket("P17", 8), ticket("P18", 9), ticket("P19", 10)],
            receipts=[
                receipt("R1", 11, ["P17", "P18"]),
                receipt("R2", 12, ["P18", "P19"]),
                receipt("R3", 14, ["P19"]),
            ],
        )
        result_42 = service.publish_correction(
            42,
            receipts=[receipt("R4", 13, ["P17"]), receipt("R5", "13:30", ["P17"])],
            withdraw_receipts=["R3"],
        )
        self.assertEqual(
            {"P17": "R4", "P18": "R1", "P19": "R2"},
            {key: value["receipt_no"] for key, value in result_42.items()},
        )
        self.assertEqual(5, result_42["P17"]["transit_hours"])
        self.assertEqual(2, result_42["P18"]["transit_hours"])
        self.assertEqual(2, result_42["P19"]["transit_hours"])

        result_43 = service.publish_correction(43, withdraw_receipts=["R2"])
        self.assertEqual("R4", result_43["P17"]["receipt_no"])
        self.assertEqual("R1", result_43["P18"]["receipt_no"])
        self.assertIsNone(result_43["P19"]["receipt_no"])
        self.assertIsNone(result_43["P19"]["carrier_code"])
        self.assertIsNone(result_43["P19"]["signed_at"])
        self.assertIsNone(result_43["P19"]["transit_hours"])

    def test_maximum_count_precedes_ticket_preferences(self):
        result = reconcile_receipts(
            tickets=[{"ticket_no": "A"}, {"ticket_no": "B"}],
            receipts=[
                {
                    "carrier_code": "C",
                    "receipt_no": "EARLY",
                    "signed_at": "2026-01-01 08:00",
                    "candidate_ticket_nos": ["A"],
                },
                {
                    "carrier_code": "C",
                    "receipt_no": "LATE",
                    "signed_at": "2026-01-01 18:00",
                    "candidate_ticket_nos": ["A", "B"],
                },
            ],
        )
        self.assertEqual("EARLY", result["A"]["receipt_no"])
        self.assertEqual("LATE", result["B"]["receipt_no"])

    def test_tie_uses_carrier_and_receipt_number(self):
        rows = [
            {"ticket_no": "A", "shipped_at": "2026-01-01 08:00"},
        ]
        receipts = [
            {
                "carrier_code": "B",
                "receipt_no": "R9",
                "signed_at": "2026-01-01 10:00",
                "candidate_ticket_nos": ["A"],
            },
            {
                "carrier_code": "A",
                "receipt_no": "R2",
                "signed_at": "2026-01-01 10:00",
                "candidate_ticket_nos": ["A"],
            },
            {
                "carrier_code": "A",
                "receipt_no": "R1",
                "signed_at": "2026-01-01 10:00",
                "candidate_ticket_nos": ["A"],
            },
        ]
        result = reconcile_receipts(rows, receipts)
        self.assertEqual("A", result["A"]["carrier_code"])
        self.assertEqual("R1", result["A"]["receipt_no"])

    def test_unrelated_ticket_is_not_rewritten(self):
        service = OutboundReconciliationService(default_carrier_code="C")
        service.publish_correction(
            41,
            tickets=[ticket("P17", 8), ticket("P20", 7)],
            receipts=[receipt("R1", 11, ["P17"]), receipt("R9", 10, ["P20"])],
        )
        service.write_log[:] = []
        service.publish_correction(
            42,
            receipts=[receipt("R4", 13, ["P17"])],
            withdraw_receipts=["R1"],
        )
        self.assertIn("P17", service.write_log)
        self.assertNotIn("P20", service.write_log)


class SqlitePublicationTest(unittest.TestCase):
    def setUp(self):
        self.wrapper = pygrametl.ConnectionWrapper(sqlite3.connect(":memory:"))
        self.wrapper.execute(
            "CREATE TABLE outbound_fact ("
            "ticket_no VARCHAR PRIMARY KEY, carrier VARCHAR, receipt VARCHAR, "
            "signed VARCHAR, shipped VARCHAR, hours REAL, milestone VARCHAR)"
        )
        self.wrapper.commit()
        self.fact_table = AccumulatingSnapshotFactTable(
            name="outbound_fact",
            keyrefs=["ticket_no"],
            otherrefs=["carrier", "receipt", "signed"],
            measures=["shipped", "hours", "milestone"],
            targetconnection=self.wrapper,
        )
        self.service = OutboundReconciliationService(
            fact_table=self.fact_table,
            connection=self.wrapper,
            column_mapping={
                "carrier_code": "carrier",
                "receipt_no": "receipt",
                "signed_at": "signed",
                "shipped_at": "shipped",
                "transit_hours": "hours",
            },
            table_name="corrections",
            assignment_table_name="versions",
            default_carrier_code="C",
        )

    def _publish_41(self):
        return self.service.publish_correction(
            41,
            tickets=[ticket("P17", 8), ticket("P18", 9), ticket("P19", 10)],
            receipts=[
                receipt("R1", 11, ["P17", "P18"]),
                receipt("R2", 12, ["P18", "P19"]),
                receipt("R3", 14, ["P19"]),
            ],
        )

    def test_fact_rows_are_updated_and_cleared(self):
        self._publish_41()
        self.service.publish_correction(
            42,
            receipts=[receipt("R4", 13, ["P17"]), receipt("R5", "13:30", ["P17"])],
            withdraw_receipts=["R3"],
        )
        self.service.publish_correction(43, withdraw_receipts=["R2"])

        p17 = self.fact_table.lookup({"ticket_no": "P17"})
        p18 = self.fact_table.lookup({"ticket_no": "P18"})
        p19 = self.fact_table.lookup({"ticket_no": "P19"})
        self.assertEqual(
            ("C", "R4", 5.0), (p17["carrier"], p17["receipt"], p17["hours"])
        )
        self.assertEqual(
            ("C", "R1", 2.0), (p18["carrier"], p18["receipt"], p18["hours"])
        )
        self.assertIsNone(p19["carrier"])
        self.assertIsNone(p19["receipt"])
        self.assertIsNone(p19["signed"])
        self.assertIsNone(p19["hours"])

    def test_idempotent_old_correction_after_newer_publication(self):
        first = self._publish_41()
        result_42 = self.service.publish_correction(
            42,
            receipts=[receipt("R4", 13, ["P17"]), receipt("R5", "13:30", ["P17"])],
            withdraw_receipts=["R3"],
        )
        self.service.publish_correction(43, withdraw_receipts=["R2"])
        replayed = self.service.publish_correction(42)

        self.assertTrue(replayed.metadata["idempotent_replay"])
        self.assertEqual(43, replayed.metadata["current_version"])
        self.assertEqual(dict(result_42), dict(replayed))
        self.assertEqual(dict(first), dict(self.service.query_version(41)))

    def test_failed_write_keeps_previous_version_and_can_retry(self):
        self._publish_41()
        original_execute = self.wrapper.execute
        failure = {"armed": True}

        def fail_once(statement, arguments=None, namemapping=None):
            if (
                failure["armed"]
                and statement.lstrip().startswith("UPDATE")
                and arguments
                and arguments.get("ticket_no") == "P19"
            ):
                failure["armed"] = False
                raise RuntimeError("connection lost")
            return original_execute(statement, arguments, namemapping)

        self.wrapper.execute = fail_once
        with self.assertRaises(RuntimeError):
            self.service.publish_correction(
                42,
                receipts=[receipt("R4", 13, ["P17"]), receipt("R5", "13:30", ["P17"])],
                withdraw_receipts=["R3"],
            )
        self.wrapper.execute = original_execute

        self.assertEqual("R1", self.fact_table.lookup({"ticket_no": "P17"})["receipt"])
        self.assertEqual("R3", self.fact_table.lookup({"ticket_no": "P19"})["receipt"])
        retried = self.service.publish_correction(
            42,
            receipts=[receipt("R4", 13, ["P17"]), receipt("R5", "13:30", ["P17"])],
            withdraw_receipts=["R3"],
        )
        self.assertEqual("R4", retried["P17"]["receipt_no"])
        self.assertEqual("R2", retried["P19"]["receipt_no"])


if __name__ == "__main__":
    unittest.main()
