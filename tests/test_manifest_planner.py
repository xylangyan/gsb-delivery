import copy
import unittest

import manifest_planner as mp


def ledger_bundle():
    left_rows = [
        ("L0", "B0", "G0", "2025-12-31 23:00", 0),
        ("L1", "B1", "G1", "2026-01-01 10:00", 1),
        ("L2", "B2", "G1", "2026-01-01 11:00", 2),
        ("L3", "B3", "G2", "2026-01-01 09:00", 1),
        ("L4", "B4", "G3", "2026-01-02 09:00", 1),
        ("L5", "B4", "G3", "2026-01-02 09:00", 2),
    ]
    right_rows = [
        ("R0", "B0", "F0", "2025-12-31 22:59", "hidden"),
        ("R1", "B1", "F1", "2026-01-01 09:59", "winner"),
        ("R2", "B2", "F2", "2026-01-01 10:59", None),
        ("R3", "B3", "F3", "2026-01-01 08:59", None),
    ]
    return {
        "tables": [
            {
                "name": "bill_left",
                "rows": [
                    {
                        "row_id": row_id,
                        "storage_cost": 10,
                        "columns": {
                            "bill_no": bill,
                            "grp": group,
                            "event_time": event_time,
                            "row_seq": sequence,
                        },
                    }
                    for row_id, bill, group, event_time, sequence in left_rows
                ],
            },
            {
                "name": "flow_right",
                "rows": [
                    {
                        "row_id": row_id,
                        "storage_cost": 4,
                        "columns": {
                            "bill_no": bill,
                            "flow_id": flow_id,
                            "event_time": event_time,
                            "display_value": display,
                        },
                    }
                    for row_id, bill, flow_id, event_time, display in right_rows
                ],
            },
        ],
        "queries": [
            {
                "id": "rank",
                "sql": """
                    WITH joined AS (
                      SELECT l.bill_no, l.grp, r.event_time AS right_time,
                             r.flow_id, r.display_value,
                             ROW_NUMBER() OVER (
                               PARTITION BY l.grp
                               ORDER BY r.event_time, l.event_time, l.row_seq, r.flow_id
                             ) AS rn
                      FROM bill_left l
                      LEFT JOIN flow_right r ON l.bill_no = r.bill_no
                      WHERE l.event_time >= '2026-01-01'
                    )
                    SELECT grp, bill_no, flow_id, display_value, rn
                    FROM joined
                    WHERE rn = 1
                    ORDER BY grp, bill_no
                """,
            },
            {
                "id": "match",
                "sql": """
                    SELECT l.bill_no,
                           CASE WHEN r.flow_id IS NOT NULL
                                THEN 'matched' ELSE 'unmatched' END AS match_state,
                           r.display_value
                    FROM bill_left l
                    LEFT JOIN flow_right r ON l.bill_no = r.bill_no
                    WHERE l.event_time >= '2026-01-01'
                    ORDER BY l.bill_no, r.flow_id
                """,
            },
            {
                "id": "duplicates",
                "sql": """
                    SELECT bill_no, row_seq
                    FROM bill_left
                    WHERE event_time >= '2026-01-01'
                    ORDER BY bill_no, row_seq
                """,
            },
            {
                "id": "distinct_values",
                "sql": """
                    SELECT DISTINCT bill_no, grp
                    FROM bill_left
                    WHERE event_time >= '2026-01-01'
                    ORDER BY bill_no
                """,
            },
        ],
    }


def sign_bundle(bundle):
    signed = copy.deepcopy(bundle)
    rows, queries, allowed, table_schemas = mp.normalize_bundle(signed)
    with mp.ReplayEngine(rows, queries, table_schemas) as engine:
        engine.set_selection(allowed)
        for query, raw_query in zip(queries, signed["queries"]):
            result = engine.run_query(query)
            assert result["error"] is None, result["error"]
            raw_query["expected"] = {
                "columns": result["columns"],
                "rows": [list(row) for row in result["rows"]],
            }
    return signed


class ManifestPlannerTest(unittest.TestCase):
    def test_parser_extracts_join_distinct_and_window_order(self):
        summary = mp.parse_sql_requirements(
            """
            SELECT DISTINCT l.bill_no
            FROM bill_left l
            LEFT JOIN flow_right r ON l.bill_no = r.bill_no
            WINDOW w AS (PARTITION BY l.grp ORDER BY l.event_time, r.flow_id)
            """
        )
        self.assertTrue(summary["distinct"])
        self.assertEqual({table["name"] for table in summary["tables"]}, {"bill_left", "flow_right"})
        self.assertTrue(summary["join_conditions"])

    def test_shared_manifest_keeps_null_match_and_duplicates(self):
        manifest = mp.build_manifest(sign_bundle(ledger_bundle()))
        ids = {(row["source_table"], row["row_id"]) for row in manifest["rows"]}

        self.assertNotIn(("bill_left", "L0"), ids)
        self.assertNotIn(("flow_right", "R0"), ids)
        self.assertIn(("flow_right", "R3"), ids)
        self.assertIn(("bill_left", "L4"), ids)
        self.assertIn(("bill_left", "L5"), ids)
        self.assertEqual(manifest["row_count"], 8)
        self.assertTrue(all({"source_table", "row_id", "columns"} <= set(row) for row in manifest["rows"]))

    def test_cheapest_distinct_source_can_represent_distinct_only_query(self):
        bundle = {
            "tables": [
                {
                    "name": "t",
                    "rows": [
                        {"row_id": "expensive", "storage_cost": 10, "columns": {"k": "X"}},
                        {"row_id": "cheap", "storage_cost": 2, "columns": {"k": "X"}},
                    ],
                }
            ],
            "queries": [
                {"id": "d", "sql": "SELECT DISTINCT k FROM t ORDER BY k", "expected": [["X"]]}
            ],
        }
        manifest = mp.build_manifest(bundle)
        self.assertEqual([row["row_id"] for row in manifest["rows"]], ["cheap"])
        self.assertEqual(manifest["total_cost"], 2)

    def test_missing_right_match_reports_join_condition(self):
        bundle = sign_bundle(ledger_bundle())
        bundle["allowed_rows"] = {
            "bill_left": ["L1", "L2", "L3", "L4", "L5"],
            "flow_right": ["R1", "R2"],
        }
        with self.assertRaises(mp.InsufficientAllowedRows) as raised:
            mp.build_manifest(bundle)
        failure = raised.exception.diagnosis["failure"]
        self.assertEqual(failure["query_id"], "rank")
        self.assertEqual(failure["kind"], "join_condition")
        self.assertTrue(failure["join_conditions"])


if __name__ == "__main__":
    unittest.main()
