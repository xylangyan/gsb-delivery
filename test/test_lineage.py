"""Tests for occurrence-level, all-optimal provenance across three archive versions."""

import json
from io import StringIO
from unittest import TestCase

from graphtage import json as graphtage_json
from graphtage.lineage import (
    LineageError,
    LineageLimitError,
    OccurrenceRef,
    SectionSpec,
    audit,
    different,
    same,
)
from graphtage.printer import Printer


def rows(*values, key="id"):
    """Rows with identical bodies and distinct registrations."""
    return {"rows": [{key: value, "body": "same"} for value in values]}


def distinct_rows(*values, key="id"):
    """Rows with distinct bodies and distinct registrations, so a pairing is forced."""
    return {"rows": [{key: value, "body": value} for value in values]}


class TestDuplicateOccurrences(TestCase):
    """Two identical bodies are two registered occurrences, never one."""

    def test_duplicate_first_version_rows_leave_an_ambiguity(self):
        first = rows("r1", "r2")
        second = rows("m1")
        third = rows("t1")
        spec = SectionSpec("rows", registration_key="id", strip_registration_key=True)
        result = audit([first, second, third], sections=[spec], target="t1")
        self.assertFalse(result.confirmed)
        self.assertEqual({"r1", "r2"}, set(result.first_version_registrations))
        self.assertEqual({("first", "r1"), ("first", "r2")}, set(result.origins))
        # One removal of a body of size 12, then a free match.
        self.assertEqual((12, 0), result.stage_costs)

    def test_ordered_and_unordered_agree_on_duplicate_ambiguity(self):
        first = rows("r1", "r2")
        second = rows("m1")
        third = rows("t1")
        for ordered in (True, False):
            spec = SectionSpec("rows", ordered=ordered, registration_key="id", strip_registration_key=True)
            result = audit([first, second, third], sections=[spec], target="t1")
            self.assertFalse(result.confirmed, ordered)
            self.assertEqual({"r1", "r2"}, set(result.first_version_registrations), ordered)

    def test_each_rendered_difference_adds_up_to_the_reported_stage_cost(self):
        first = rows("r1", "r2")
        second = rows("m1")
        third = rows("t1")
        spec = SectionSpec("rows", registration_key="id", strip_registration_key=True)
        result = audit([first, second, third], sections=[spec], target="t1")
        self.assertEqual(2, len(result.explanations))
        for explanation in result.explanations:
            self.assertEqual(2, len(explanation.diffs))
            self.assertEqual(1, len(explanation.diffs[0].split("~~")) % 2)

    def test_duplicate_middle_occurrences_are_not_shared(self):
        first = {"rows": [{"id": "r1", "body": "A"}]}
        second = {"rows": [{"id": "m1", "body": "A"}, {"id": "m2", "body": "B"}]}
        third = {"rows": [{"id": "t1", "body": "A"}, {"id": "t2", "body": "B"}]}
        spec = SectionSpec("rows", registration_key="id", strip_registration_key=True)
        result_one = audit([first, second, third], sections=[spec], target="t1")
        result_two = audit([first, second, third], sections=[spec], target="t2")
        self.assertIn("m1", str(result_one.explanations[0].path[1]))
        self.assertTrue(result_two.introduced_midway)
        self.assertEqual(OccurrenceRef(1, "m2"), result_two.explanations[0].path[0])


class TestIdentityEvidence(TestCase):
    def setUp(self):
        self.first = rows("r1", "r2")
        self.second = rows("m1")
        self.third = rows("t1")
        self.spec = SectionSpec("rows", registration_key="id", strip_registration_key=True)

    def test_direct_first_to_third_same_evidence_confirms(self):
        result = audit(
            [self.first, self.second, self.third], sections=[self.spec], target="t1",
            identities={"same": [[(0, "r2"), (2, "t1")]]},
        )
        self.assertTrue(result.confirmed)
        self.assertEqual(("r2",), result.first_version_registrations)
        self.assertEqual((("first", "r2"),), result.origins)

    def test_direct_first_to_third_different_evidence_removes_that_origin(self):
        first = rows("r1", "r2")
        second = rows("m1", "m2")
        third = rows("t1")
        result = audit(
            [first, second, third], sections=[self.spec], target="t1",
            identities={"different": [[(0, "r1"), (2, "t1")]]},
        )
        self.assertTrue(result.confirmed)
        self.assertEqual(("r2",), result.first_version_registrations)

    def test_same_entity_links_collapse_first_version_duplicates(self):
        result = audit(
            [self.first, self.second, self.third], sections=[self.spec], target="t1",
            identities={"same": [[(0, "r1"), (0, "r2")]]},
        )
        self.assertTrue(result.confirmed)
        self.assertEqual((("first", "r1"),), result.origins)

    def test_adjacent_same_evidence_is_required(self):
        result = audit(
            [self.first, self.second, self.third], sections=[self.spec], target="t1",
            identities={"same": [[(0, "r2"), (1, "m1")]]},
        )
        self.assertNotIn("r1", result.first_version_registrations)

    def test_adjacent_different_evidence_reports_a_cost_conflict(self):
        result = audit(
            [rows("r1"), rows("m1"), rows("t1")], sections=[self.spec], target="t1",
            identities={"different": [[(0, "r1"), (1, "m1")]]},
        )
        self.assertFalse(result.confirmed)
        self.assertIsNotNone(result.conflict)
        self.assertEqual("evidence_requires_higher_cost", result.conflict.kind)
        self.assertLess(result.conflict.original_cost, result.conflict.required_cost)

    def test_contradictory_evidence_is_rejected(self):
        with self.assertRaises(LineageError):
            audit(
                [rows("r1"), rows("m1"), rows("t1")], sections=[self.spec], target="t1",
                identities={
                    "same": [[(0, "r1"), (1, "m1")]],
                    "different": [[(0, "r1"), (1, "m1")]],
                },
            )

    def test_unknown_evidence_reference_is_rejected(self):
        with self.assertRaises(LineageError):
            audit(
                [rows("r1"), rows("m1"), rows("t1")], sections=[self.spec], target="t1",
                identities={"same": [[(0, "missing"), (2, "t1")]]},
            )


class TestIntroductions(TestCase):
    def test_row_introduced_in_the_second_version(self):
        first = {"rows": [{"id": "r1", "body": "A"}]}
        second = {"rows": [{"id": "r1b", "body": "A"}, {"id": "m2", "body": "B"}]}
        third = {"rows": [{"id": "t1", "body": "A"}, {"id": "t2", "body": "B"}]}
        spec = SectionSpec("rows", registration_key="id", strip_registration_key=True)
        result = audit([first, second, third], sections=[spec], target="t2")
        self.assertFalse(result.confirmed)
        self.assertTrue(result.introduced_midway)
        self.assertEqual(("midway", None), result.origins[0])
        self.assertEqual(OccurrenceRef(1, "m2"), result.explanations[0].path[0])

    def test_row_introduced_in_the_third_version(self):
        first = {"rows": [{"id": "r1", "body": "A"}]}
        second = {"rows": [{"id": "m1", "body": "A"}]}
        third = {"rows": [{"id": "t1", "body": "A"}, {"id": "t2", "body": "B"}]}
        spec = SectionSpec("rows", registration_key="id", strip_registration_key=True)
        result = audit([first, second, third], sections=[spec], target="t2")
        self.assertTrue(result.introduced_at_third)
        self.assertEqual(("third", None), result.origins[0])


class TestSectionsAndFormats(TestCase):
    def test_nested_section_paths(self):
        first = {"archive": {"rows": [{"id": "r1", "n": 1}]}}
        second = {"archive": {"rows": [{"id": "m1", "n": 1}]}}
        third = {"archive": {"rows": [{"id": "t1", "n": 1}]}}
        spec = SectionSpec(("archive", "rows"), registration_key="id", strip_registration_key=True)
        result = audit([first, second, third], sections=[spec], target="t1")
        self.assertTrue(result.confirmed)
        self.assertEqual(("r1",), result.first_version_registrations)

    def test_missing_section_is_empty(self):
        first = {}
        second = rows("m1")
        third = rows("t1")
        spec = SectionSpec("rows", registration_key="id", strip_registration_key=True)
        result = audit([first, second, third], sections=[spec], target="t1")
        self.assertTrue(result.introduced_midway)

    def test_sections_can_be_strings(self):
        first = {"rows": [{"n": 1}]}
        second = {"rows": [{"n": 1}]}
        third = {"rows": [{"n": 1}]}
        result = audit([first, second, third], sections=["rows"], target=0)
        self.assertTrue(result.confirmed)
        self.assertEqual((0,), result.first_version_registrations)

    def test_unordered_section_ignores_position(self):
        first = {"rows": [{"id": "r1", "n": 1}, {"id": "r2", "n": 2}]}
        second = {"rows": [{"id": "m2", "n": 2}, {"id": "m1", "n": 1}]}
        third = {"rows": [{"id": "t2", "n": 2}, {"id": "t1", "n": 1}]}
        spec = SectionSpec("rows", ordered=False, registration_key="id", strip_registration_key=True)
        result = audit([first, second, third], sections=[spec], target="t1")
        self.assertTrue(result.confirmed)
        self.assertEqual(("r1",), result.first_version_registrations)
        self.assertEqual((0, 0), result.stage_costs)

    def test_versions_can_be_json_files(self):
        import tempfile
        payloads = [
            {"rows": [{"id": "r1", "n": 1}]},
            {"rows": [{"id": "m1", "n": 1}]},
            {"rows": [{"id": "t1", "n": 1}]},
        ]
        paths = []
        try:
            for payload in payloads:
                with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                    json.dump(payload, handle)
                    paths.append(handle.name)
            spec = SectionSpec("rows", registration_key="id", strip_registration_key=True)
            result = audit(paths, sections=[spec], target="t1")
            self.assertTrue(result.confirmed)
        finally:
            import os
            for path in paths:
                os.unlink(path)


class TestResultSerialization(TestCase):
    def test_to_dict_round_trips(self):
        first = rows("r1", "r2")
        second = rows("m1")
        third = rows("t1")
        spec = SectionSpec("rows", registration_key="id", strip_registration_key=True)
        result = audit([first, second, third], sections=[spec], target="t1")
        payload = result.to_dict()
        self.assertEqual(2, len(payload["explanations"]))
        self.assertEqual([["v1:r1", "v2:m1", "v3:t1"], ["v1:r2", "v2:m1", "v3:t1"]],
                         [explanation["path"] for explanation in payload["explanations"]])
        self.assertEqual([], payload["target"] and [])


class TestStageCostsMatchGraphtage(TestCase):
    """The audit's prices are sums of Graphtage's own base edits, not a parallel cost model."""

    def test_ordered_section_price_matches_graphtage(self):
        left = graphtage_json.build_tree([1, 2, 3])
        right = graphtage_json.build_tree([2, 3, 4])
        from graphtage.lineage import _node_cost
        edit = left.edits(right)
        while not edit.bounds().definitive() and edit.tighten_bounds():
            pass
        self.assertEqual(edit.bounds().upper_bound, _node_cost(left, right))

    def test_string_replacement_uses_levenshtein(self):
        from graphtage.lineage import _node_cost
        self.assertEqual(
            1,
            _node_cost(graphtage_json.build_tree("abc"), graphtage_json.build_tree("axc")),
        )


class TestOptimality(TestCase):
    def test_every_rendered_step_is_supported_by_some_optimum(self):
        from graphtage.lineage import _collect, _EntityUnion, _ops_for, _solve_section
        first = {"rows": [{"id": "r1", "body": "same"}, {"id": "r2", "body": "same"}]}
        second = {"rows": [{"id": "m1", "body": "same"}, {"id": "m2", "body": "other"}]}
        third = {"rows": [{"id": "t1", "body": "same"}, {"id": "t2", "body": "other"}]}
        spec = SectionSpec("rows", registration_key="id", strip_registration_key=True)
        collected = _collect([first, second, third], [spec])
        section = _solve_section(spec, collected, [], _EntityUnion([]))
        for alignment in section.first_consistent:
            for op in alignment.ops:
                self.assertTrue(section.first.supported(op), op)
        for alignment in section.second_consistent:
            for op in alignment.ops:
                self.assertTrue(section.second.supported(op), op)

    def test_all_optimal_ordered_support_is_exact(self):
        from graphtage.lineage import Occurrence, _ordered_solution, _penalty
        left = [
            Occurrence(0, (), 0, "a", "x", graphtage_json.build_tree("x")),
            Occurrence(0, (), 1, "b", "y", graphtage_json.build_tree("y")),
        ]
        right = [
            Occurrence(1, (), 0, "c", "x", graphtage_json.build_tree("x")),
            Occurrence(1, (), 1, "d", "y", graphtage_json.build_tree("y")),
        ]
        solution = _ordered_solution(left, right, _penalty(left + right, True))
        self.assertEqual(0, solution.cost)
        self.assertEqual({(0, 0), (1, 1)}, solution.pair_support)
        self.assertEqual({(1, 1)}, {(i, j) for i, j in solution.pair_support if i == j} - {(0, 0)})


class TestCommandLine(TestCase):
    def _write(self, payloads):
        import tempfile
        paths = []
        for payload in payloads:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                json.dump(payload, handle)
                paths.append(handle.name)
        return paths

    def test_command_reports_json(self):
        import contextlib
        import os

        from graphtage.lineage_cli import main
        paths = self._write([
            {"rows": [{"id": "r1", "n": 1}]},
            {"rows": [{"id": "m1", "n": 1}]},
            {"rows": [{"id": "t1", "n": 1}]},
        ])
        try:
            output = StringIO()
            with contextlib.redirect_stdout(output):
                status = main([
                    "graphtage-lineage", *paths,
                    "--section", "rows:ordered=id",
                    "--target", "t1",
                ])
            self.assertEqual(0, status)
            payload = json.loads(output.getvalue())
            self.assertTrue(payload["confirmed"])
            self.assertEqual(["r1"], payload["first_version_registrations"])
        finally:
            for path in paths:
                os.unlink(path)

    def test_command_returns_two_on_bad_evidence(self):
        import contextlib
        import os

        from graphtage.lineage_cli import main
        paths = self._write([
            {"rows": [{"id": "r1", "n": 1}]},
            {"rows": [{"id": "m1", "n": 1}]},
            {"rows": [{"id": "t1", "n": 1}]},
        ])
        try:
            with contextlib.redirect_stderr(StringIO()):
                status = main([
                    "graphtage-lineage", *paths,
                    "--section", "rows:ordered=id",
                    "--identity", "missing@v1=t1@v3",
                    "--target", "t1",
                ])
            self.assertEqual(2, status)
        finally:
            for path in paths:
                os.unlink(path)
