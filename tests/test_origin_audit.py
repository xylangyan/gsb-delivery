import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import origin_audit as oa  # noqa: E402


class OriginAuditTests(unittest.TestCase):
    def setUp(self):
        self.case = oa.build_case(oa.EXAMPLE_CASE)

    def test_duplicate_17_update_hits_both_originals(self):
        event = next(e for e in self.case.events["A"] if e.ref == "A:a3")
        outcome = oa.simulate(event, self.case.initial_state, self.case.bits)
        self.assertTrue(outcome.ok)
        self.assertEqual(
            {match["origin"] for match in outcome.matches},
            {"O1-first-17", "O2-second-17"},
        )

    def test_range_reports_certain_and_possible_targets(self):
        report = oa.analyze(self.case, "CP1:CP2", "B:b2")
        self.assertTrue(report["ok"], report)
        target = report["target"]
        self.assertEqual(target["certain_originals"], ["O3-original-18"])
        self.assertIn("O1-first-17", target["possible_originals"])
        self.assertIn("O2-second-17", target["possible_originals"])
        self.assertGreaterEqual(report["written_including_deletes"]["min"], 3)

    def test_impossible_receipt_names_event(self):
        raw = oa.json.loads(oa.json.dumps(oa.EXAMPLE_CASE))
        raw["streams"]["A"].append(
            {"id": "a4", "op": "getById", "pk": 17, "result": {"id": 17}}
        )
        report = oa.analyze(oa.build_case(raw), None, "B:b2")
        self.assertFalse(report["ok"])
        first = report["diagnosis"]["first_impossible"]
        self.assertIsNotNone(first)
        self.assertEqual(first["ref"], "A:a4")

    def test_deleting_front_row_changes_schema_reference(self):
        state = tuple(
            (origin, oa.canonical(row))
            for origin, row in [
                ("O0", {"id": 1, "legacy": "front"}),
                ("O1", {"id": 17, "name": "x", "note": "n"}),
            ]
        )
        bits = {"O0": 1, "O1": 2}
        field_update = oa.Event(
            "A", 1, "A:update", "updateByQuery",
            {"query": {"id": 17}, "data": {"name": "y"}},
        )
        before = oa.simulate(field_update, state, bits)
        self.assertFalse(before.ok)
        self.assertEqual(before.error, "SchemaError")

        delete = oa.Event("A", 0, "A:delete", "deleteById", {"pk": 1})
        deleted = oa.simulate(delete, state, bits)
        self.assertTrue(deleted.ok)
        after = oa.simulate(field_update, deleted.state, bits)
        self.assertTrue(after.ok, after.error)
        self.assertEqual({m["origin"] for m in after.matches}, {"O1"})

    def test_text_log_parser(self):
        events = oa.parse_text_log(
            "# workstation A\n"
            "01 getById(17) => {'id': 17}\n"
            "02 updateById(17, {'id': 18})\n"
            "03 getById(17) => IdNotFoundError\n"
        )
        self.assertEqual([e["op"] for e in events], ["getById", "updateById", "getById"])
        self.assertEqual(events[2]["error"], "IdNotFoundError")


if __name__ == "__main__":
    unittest.main()
