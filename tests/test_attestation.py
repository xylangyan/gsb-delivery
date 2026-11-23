from __future__ import annotations

import unittest

from sqlglot.attestation import ReceiptPolicy, attest
from sqlglot.errors import SqlglotError
from sqlglot.executor import execute


def _tables():
    return {
        "receipts": {
            "r1": {"part": "P1", "qty": 10},
            "r2": {"part": "P1", "qty": 10},
        },
        "checklist": {"c1": {"part": None}},
        "returns": {
            "rt1": {"part": "P1", "qty": 10},
            "rt2": {"part": "P1", "qty": 10},
        },
    }


SQL = """
SELECT part, qty FROM receipts WHERE part NOT IN (SELECT part FROM checklist)
EXCEPT ALL
SELECT part, qty FROM returns
"""
TARGET = {"part": "P1", "qty": 10}
FIXED = {"receipts": ["r1", "r2"], "checklist": [], "returns": []}


class TestAttestation(unittest.TestCase):
    maxDiff = None

    def test_not_in_null_pending_keeps_range_open(self):
        result = attest(SQL, TARGET, FIXED, _tables())

        self.assertEqual((result.min_count, result.max_count), (0, 2))
        self.assertFalse(result.is_absent)
        self.assertEqual(len(result.scenarios), 8)

        min_case, max_case = result.bounds
        self.assertEqual((min_case.bound, min_case.count), ("min", 0))
        self.assertIn(("checklist", "c1"), min_case.present)
        self.assertEqual((max_case.bound, max_case.count), ("max", 2))
        self.assertNotIn(("checklist", "c1"), max_case.present)

        for case in result.bounds:
            count = self._recount(case.present)
            self.assertEqual(count, case.count)

    def test_null_in_checklist_certifies_absence_when_received(self):
        fixed = {"receipts": ["r1", "r2"], "checklist": ["c1"], "returns": []}
        result = attest(SQL, TARGET, fixed, _tables())

        self.assertTrue(result.is_absent)
        self.assertEqual(result.max_count, 0)
        self.assertTrue(result.witnesses)
        witness = result.witnesses[0]
        self.assertEqual([(row.table, row.row_id) for row in witness.rows], [("checklist", "c1")])
        self.assertTrue(any("NULL" in reason for reason in [witness.reason]))
        self.assertIn("SELECT part FROM checklist", witness.blocking_branches)

    def test_except_all_group_policy_always_absent(self):
        policy = ReceiptPolicy(
            "returns_together",
            frozenset({("returns", "rt1"), ("returns", "rt2")}),
            "group",
            min_count=2,
            max_count=2,
        )
        result = attest(SQL, TARGET, FIXED, _tables(), policies=[policy])

        self.assertTrue(result.is_absent)
        self.assertEqual(len(result.scenarios), 2)
        self.assertTrue(any("EXCEPT ALL" in witness.reason for witness in result.witnesses))

    def test_except_all_exclusive_policy_ranges_one_to_two(self):
        policy = ReceiptPolicy(
            "returns_exclusive",
            frozenset({("returns", "rt1"), ("returns", "rt2")}),
            "exclusive",
        )
        result = attest(SQL, TARGET, FIXED, _tables(), policies=[policy])

        self.assertFalse(result.is_absent)
        self.assertEqual((result.min_count, result.max_count), (0, 2))
        self.assertEqual(len(result.scenarios), 6)
        self.assertEqual(result.witnesses, ())

    def test_witnesses_only_use_received_rows(self):
        fixed = {"receipts": ["r1", "r2"], "checklist": ["c1"], "returns": []}
        result = attest(SQL, TARGET, fixed, _tables())

        for witness in result.witnesses:
            ids = {(row.table, row.row_id) for row in witness.rows}
            self.assertTrue(ids <= result.fixed)
            for arrival in result.scenarios:
                world = ids | (arrival.present - result.fixed)
                self.assertEqual(self._recount(world), 0)

    def test_identical_rows_are_not_merged(self):
        result = attest(
            "SELECT part, qty FROM receipts",
            TARGET,
            {"receipts": ["r1", "r2"]},
            _tables(),
        )
        self.assertEqual((result.min_count, result.max_count), (2, 2))
        self.assertEqual(len(result.table_rows["receipts"]), 2)

    def test_null_target_uses_sql_equality(self):
        result = attest(
            "SELECT part FROM receipts",
            {"part": None},
            {"receipts": ["r3"]},
            {"receipts": {"r3": {"part": None}, "r4": {"part": "P9"}}},
        )
        self.assertEqual((result.min_count, result.max_count), (1, 1))

    def test_where_branch_is_located(self):
        result = attest(
            "SELECT part, qty FROM receipts WHERE qty > 100",
            TARGET,
            {"receipts": ["r1", "r2"]},
            _tables(),
        )
        self.assertTrue(result.is_absent)
        self.assertIn("WHERE", result.witnesses[0].reason)

    def test_unknown_part_is_structurally_absent(self):
        result = attest(
            "SELECT part, qty FROM receipts",
            {"part": "NO_SUCH_PART", "qty": 10},
            {"receipts": ["r1", "r2"]},
            _tables(),
        )
        self.assertTrue(result.is_absent)
        self.assertEqual(result.witnesses[0].rows, ())

    def test_intersect_branch_is_located(self):
        result = attest(
            "SELECT part FROM r INTERSECT SELECT part FROM s",
            {"part": "P1"},
            {"r": ["r1"], "s": ["s1"]},
            {"r": {"r1": {"part": "P1"}}, "s": {"s1": {"part": "P2"}}},
        )
        self.assertTrue(result.is_absent)
        self.assertIn("SELECT part FROM s", result.witnesses[0].blocking_branches)

    def test_union_all_traces_both_branches(self):
        result = attest(
            "SELECT part, qty FROM r UNION ALL SELECT part, qty FROM s",
            TARGET,
            {"r": ["r1"], "s": []},
            {
                "r": {"r1": {"part": "P1", "qty": 10}},
                "s": {"s1": {"part": "P1", "qty": 10}},
            },
        )
        self.assertEqual((result.min_count, result.max_count), (1, 2))
        self.assertEqual(
            [trace.kind for trace in result.traces], ["UNION ALL left", "UNION ALL right"]
        )

    def test_correlated_subquery_does_not_break_traces(self):
        result = attest(
            "SELECT part FROM receipts r WHERE NOT EXISTS "
            "(SELECT 1 FROM returns t WHERE t.part = r.part)",
            {"part": "P1"},
            {"receipts": ["r1"], "returns": ["rt1"]},
            {"receipts": {"r1": {"part": "P1"}}, "returns": {"rt1": {"part": "P1"}}},
        )
        self.assertEqual((result.min_count, result.max_count), (0, 0))
        self.assertEqual([trace for trace in result.traces if trace.kind.endswith("subquery")], [])

    def test_policy_validation(self):
        with self.assertRaises(SqlglotError):
            ReceiptPolicy("bad", frozenset(), "nope")

        with self.assertRaises(SqlglotError):
            attest(
                SQL,
                TARGET,
                FIXED,
                _tables(),
                policies=[
                    ReceiptPolicy(
                        "fixed_row",
                        frozenset({("receipts", "r1")}),
                        "exclusive",
                    )
                ],
            )

    def test_unknown_received_row_rejected(self):
        with self.assertRaises(SqlglotError):
            attest(
                SQL,
                TARGET,
                {"receipts": ["r1", "missing"], "checklist": [], "returns": []},
                _tables(),
            )

    def test_limit_rejected(self):
        with self.assertRaises(SqlglotError):
            attest(
                "SELECT part FROM receipts LIMIT 1",
                {"part": "P1"},
                {"receipts": ["r1"]},
                _tables(),
            )

    def _recount(self, present):
        all_tables = _tables()
        materialized = {table_name: [] for table_name in all_tables}
        for table_name, row_id in present:
            materialized[table_name].append(all_tables[table_name][row_id])
        schema = {
            table_name: {column: "VARCHAR" for column in next(iter(by_id.values()))}
            for table_name, by_id in all_tables.items()
        }
        result = execute(SQL, schema=schema, tables=materialized)
        key = tuple(TARGET[column] for column in result.columns)
        return result.rows.count(key)


if __name__ == "__main__":
    unittest.main()
