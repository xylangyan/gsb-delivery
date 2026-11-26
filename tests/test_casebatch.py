from __future__ import annotations

import json
import unittest

from casebatch.model import Case, TaskSpec
from casebatch.segments import plan_compressed
from casebatch.wire import PreparedMessage, build_command_wire, normalize_append_bytes
from imapclient.testable_imapclient import TestableIMAPClient
from solution import plan_task

TestableIMAPClient.__test__ = False


def case(case_id, length, category="default", predecessors=None):
    return {
        "id": case_id,
        "folder": "F",
        "category": category,
        "predecessors": list(predecessors or []),
        "messages": [
            {
                "source": f"src-{case_id}",
                "length": length,
                "flags": [],
                "date": "2024-01-01T00:00:00+08:00",
                "attribution": f"owner-{case_id}",
            }
        ],
    }


def fixture(name, command_limit, *, plus=False, tiers=None, transitions=None):
    return {
        "name": name,
        "literal_plus": plus,
        "tiers": tiers or [{"burst_limit": 10_000_000, "command_limit": command_limit}],
        "transitions": transitions or {"*": [0]},
    }


class WireTests(unittest.TestCase):
    def test_single_extra_cr_can_add_marker_digit(self):
        raw = b"a" * 998 + b"\n"
        normalized = normalize_append_bytes(raw)
        self.assertEqual(len(raw), 999)
        self.assertEqual(len(normalized), 1000)

        raw_report = build_command_wire(
            [PreparedMessage(length=len(raw))], "F", tag=b"tag"
        )
        normalized_report = build_command_wire(
            [PreparedMessage(length=len(normalized))], "F", tag=b"tag"
        )
        self.assertEqual(raw_report.length_fields[0]["length_digits"], 3)
        self.assertEqual(normalized_report.length_fields[0]["length_digits"], 4)
        self.assertEqual(
            normalized_report.total_bytes - raw_report.total_bytes, 2
        )

    def test_non_plus_names_continuation_boundary_and_source(self):
        report = build_command_wire(
            [
                PreparedMessage(length=999, source="first-source", case_id=7),
                PreparedMessage(length=2, source="second-source", case_id=8),
            ],
            "F",
            tag=b"tag",
        )
        violation = report.check_budget(80, 10**9)
        self.assertFalse(violation.ok)
        self.assertEqual(violation.boundary_id, "continuation:2")
        self.assertEqual(violation.message_number, 1)
        self.assertEqual(violation.source, "first-source")
        self.assertEqual(violation.case_id, 7)
        self.assertEqual(violation.part, "literal-data")

    def test_literal_plus_has_only_command_completion_budget_boundary(self):
        report = build_command_wire(
            [PreparedMessage(length=100, source="only", case_id=9)],
            "F",
            tag=b"tag",
            literal_plus=True,
        )
        self.assertEqual([b.boundary_id for b in report.boundaries], ["command-complete"])
        violation = report.check_budget(20, 10**9)
        self.assertFalse(violation.ok)
        self.assertEqual(violation.boundary_id, "command-complete")


class WireParityTests(unittest.TestCase):
    def test_report_matches_actual_multiappend_bytes(self):
        case_obj = Case.from_dict(
            {
                "id": 1,
                "folder": "F",
                "messages": [
                    {
                        "source": "a",
                        "body": "a" * 998 + "\n",
                        "flags": [],
                        "date": "2024-01-01T00:00:00+08:00",
                    },
                    {
                        "source": "b",
                        "body": "world\n",
                        "flags": [r"\Seen"],
                        "date": "2024-01-01T00:01:00+08:00",
                    },
                ],
            }
        )
        for plus in (False, True):
            with self.subTest(literal_plus=plus):
                client = TestableIMAPClient()
                capabilities = [b"MULTIAPPEND"] + ([b"LITERAL+"] if plus else [])
                client._cached_capabilities = tuple(capabilities)
                client._imap._command_complete.return_value = ("OK", [b"Good"])
                client._imap._get_response.side_effect = [None, None]

                client.multiappend(
                    "F",
                    [
                        {"msg": message.body, "flags": message.flags, "date": message.date}
                        for message in case_obj.messages
                    ],
                )
                report = build_command_wire(
                    case_obj.prepared_messages(),
                    "F",
                    tag=b"tag",
                    literal_plus=plus,
                    include_preview=True,
                )
                self.assertEqual(client._imap.sent, report.wire_preview)
                self.assertEqual(len(client._imap.sent), report.total_bytes)


class PlannerTests(unittest.TestCase):
    def test_minimum_plan_and_cut_tie_break(self):
        data = {
            "tag": "tag",
            "allow_reorder": False,
            "fixtures": [fixture("A", 120), fixture("B", 120, plus=True)],
            "cases": [case(1, 10), case(2, 10), case(3, 10)],
        }
        result = plan_task(TaskSpec.from_dict(data))
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["command_count"], 2)
        self.assertEqual(result["cuts"], [1, 3])
        self.assertEqual([c["case_ids"] for c in result["commands"]], [[1], [2, 3]])

    def test_dual_fixture_dynamic_tier_failure_has_origin(self):
        data = {
            "tag": "tag",
            "allow_reorder": False,
            "order": [1, 2],
            "fixtures": [
                fixture("A", 10_000),
                {
                    "name": "B",
                    "literal_plus": False,
                    "byte_edges": [95, 160],
                    "tiers": [
                        {"name": "big", "burst_limit": 10_000, "command_limit": 100},
                        {"name": "small", "burst_limit": 10_000, "command_limit": 70},
                    ],
                    "initial_tier": 0,
                    "transitions": {"trigger": [1, 1], "*": [0, 0]},
                },
            ],
            "cases": [
                case(1, 40, category="trigger"),
                case(2, 20, predecessors=[1]),
            ],
        }
        result = plan_task(TaskSpec.from_dict(data))
        self.assertEqual(result["status"], "infeasible")
        failure = result["failure"]
        self.assertEqual(failure["group"]["case_ids"], [2])
        b_budget = failure["group"]["budgets"][1]
        self.assertFalse(b_budget["ok"])
        self.assertEqual(b_budget["violation"]["limit"], 70)
        self.assertEqual(b_budget["violation"]["sent_bytes"], 75)
        self.assertEqual(b_budget["before_tier"], 1)
        origin = failure["budget_origins"][1]
        self.assertEqual(origin["origin"], "after_command")
        self.assertEqual(origin["last_case_id"], 1)

    def test_free_cases_use_lexicographic_case_order(self):
        data = {
            "tag": "tag",
            "fixtures": [fixture("A", 10_000), fixture("B", 10_000, plus=True)],
            "cases": [case(30, 1), case(10, 1), case(20, 1)],
        }
        result = plan_task(TaskSpec.from_dict(data))
        self.assertEqual(result["status"], "feasible")
        self.assertEqual(result["command_count"], 1)
        self.assertEqual(result["tie_key"][0], (10, 20, 30))

    def test_receipt_verification_checks_date_flags_attribution(self):
        data = {
            "tag": "tag",
            "fixtures": [fixture("A", 10_000), fixture("B", 10_000)],
            "cases": [case(100, 3)],
        }
        result = plan_task(TaskSpec.from_dict(data))
        self.assertTrue(result["verification"]["ok"])
        self.assertEqual(result["verification"]["checked_messages"], 1)


class CompressedSegmentTests(unittest.TestCase):
    def test_locate_large_case_without_expanding_repetitions(self):
        data = {
            "tag": "tag",
            "max_group_cases": 2,
            "fixtures": [fixture("A", 120), fixture("B", 120, plus=True)],
            "segments": [
                {
                    "repeat": 5_000_000,
                    "id_start": 1,
                    "id_step": 1,
                    "id_block_step": 10,
                    "cases": [
                        case(0, 10),
                        case(0, 10),
                    ],
                }
            ],
        }
        result = plan_compressed(data)
        manifest = result["manifest"]
        located = manifest.locate_case(1232)
        self.assertEqual(located["case_id"], 1232)
        self.assertEqual(located["global_case_index"], 247)
        self.assertEqual(located["command_index"], 124)
        self.assertEqual(located["command_case_ids"], [1231, 1232])
        self.assertEqual(located["cycle_repetition"], 123)
        self.assertIn("A", located["bytes_by_fixture"])
        self.assertEqual(result["summary"]["command_count"], 5_000_000)
        json.dumps(result["summary"])
        self.assertIn("segment_sheet", result["summary"])

    def test_locate_ten_million_scale_id(self):
        data = {
            "tag": "tag",
            "max_group_cases": 2,
            "fixtures": [fixture("A", 120), fixture("B", 120)],
            "segments": [
                {
                    "repeat": 5_000_000,
                    "id_start": 10_000_001,
                    "id_step": 1,
                    "id_block_step": 2,
                    "cases": [case(0, 10), case(0, 10)],
                }
            ],
        }
        result = plan_compressed(data)
        located = result["manifest"].locate_case(19_999_999)
        self.assertEqual(located["command_index"], 5_000_000)
        self.assertEqual(located["command_case_ids"], [19_999_999, 20_000_000])


if __name__ == "__main__":
    unittest.main()
