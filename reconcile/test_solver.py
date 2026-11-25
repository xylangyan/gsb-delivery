import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reconcile import solver as solver_module
from reconcile.solver import Instance, Solver, native_pairs, report, solve_dict


# Two "待领" rows D (batch b1/b2) around a unique anchor X.  In the whole
# ledger the batch value occurs twice, so the patience matcher pairs the
# duplicate D lines positionally; the section view keeps only one D each.
INSTANCE_A = {
    "old_rows": [
        {"id": "o0", "text": "D", "allowed_tags": ["b1"]},
        {"id": "o1", "text": "X", "allowed_tags": []},
        {"id": "o2", "text": "D", "allowed_tags": ["b2"]},
    ],
    "new_rows": [
        {"id": "n0", "text": "D", "allowed_tags": ["b2"]},
        {"id": "n1", "text": "X", "allowed_tags": []},
        {"id": "n2", "text": "D", "allowed_tags": ["b1"]},
    ],
    "views": [
        {"name": "full", "old": ["o0", "o1", "o2"], "new": ["n0", "n1", "n2"]},
        {"name": "section", "old": ["o0", "o1"], "new": ["n1", "n2"]},
    ],
    "confirmed": [["o0", "n2"]],
    "forbidden": [["o0", "n0"], ["o2", "n2"]],
}

# Genuinely ambiguous instance: two different one-row taggings are both
# minimal and make the full view produce different native matching pairs.
INSTANCE_AMBIGUOUS = {
    "old_rows": [
        {"id": "o0", "text": "D", "allowed_tags": ["u"]},
        {"id": "o1", "text": "E", "allowed_tags": ["u"]},
        {"id": "o2", "text": "E", "allowed_tags": []},
        {"id": "o3", "text": "D", "allowed_tags": ["t", "u"]},
    ],
    "new_rows": [
        {"id": "n0", "text": "E", "allowed_tags": ["t", "u"]},
        {"id": "n1", "text": "E", "allowed_tags": ["u"]},
        {"id": "n2", "text": "D", "allowed_tags": ["t"]},
    ],
    "views": [
        {
            "name": "full",
            "old": ["o0", "o1", "o2", "o3"],
            "new": ["n0", "n1", "n2"],
        },
        {"name": "sec", "old": ["o2", "o3"], "new": ["n1", "n0"]},
    ],
    "confirmed": [],
    "forbidden": [["o3", "n2"], ["o2", "n0"]],
}


class ComparisonKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instance = Instance.from_dict(INSTANCE_A)
        self.view = self.instance.views[0]

    def test_untagged_equal_lines_match(self) -> None:
        pairs = native_pairs(self.instance, self.view, {})
        # Without tags the duplicate D lines pair positionally, which is
        # exactly the mis-pairing the archivists reported.
        self.assertEqual(
            pairs, frozenset({("o0", "n0"), ("o1", "n1"), ("o2", "n2")})
        )

    def test_tag_must_match_on_both_sides(self) -> None:
        # Only the old row is tagged: keys differ, so that pair must vanish.
        pairs = native_pairs(self.instance, self.view, {"o0": "b1"})
        self.assertNotIn(("o0", "n2"), pairs)
        self.assertNotIn(("o0", "n0"), pairs)

    def test_matching_tag_creates_unique_pair(self) -> None:
        pairs = native_pairs(
            self.instance, self.view, {"o0": "b1", "n2": "b1"}
        )
        self.assertIn(("o0", "n2"), pairs)
        self.assertNotIn(("o0", "n0"), pairs)
        self.assertNotIn(("o2", "n2"), pairs)

    def test_original_text_is_never_bridged(self) -> None:
        data = json.loads(json.dumps(INSTANCE_A))
        data["old_rows"][0]["text"] = "D9"
        instance = Instance.from_dict(data)
        result = Solver(instance).solve()
        self.assertEqual("infeasible", result.status)
        self.assertTrue(
            any("different original text" in item for item in result.blockers)
        )

    def test_equal_text_with_different_tags_stays_unmatched(self) -> None:
        # The two rows keep identical original text and remain a removal and
        # an insertion; a tag mismatch must never pair them.
        instance = Instance.from_dict(
            {
                "old_rows": [
                    {"id": "o0", "text": "D", "allowed_tags": ["b1"]}
                ],
                "new_rows": [
                    {"id": "n0", "text": "D", "allowed_tags": ["b2"]}
                ],
                "views": [
                    {"name": "v", "old": ["o0"], "new": ["n0"]}
                ],
                "confirmed": [],
                "forbidden": [],
            }
        )
        pairs = native_pairs(
            instance, instance.views[0], {"o0": "b1", "n0": "b2"}
        )
        self.assertEqual(frozenset(), pairs)
        # With no tags the identical text does match.
        self.assertEqual(
            frozenset({("o0", "n0")}),
            native_pairs(instance, instance.views[0], {}),
        )

    def test_confirmed_pair_never_cooccurring_is_not_enforceable(self) -> None:
        data = json.loads(json.dumps(INSTANCE_A))
        # n2 is dropped from every view, so (o0, n2) cannot be checked.
        data["views"] = [
            {"name": "full", "old": ["o0", "o1", "o2"], "new": ["n0", "n1"]}
        ]
        solver = Solver(Instance.from_dict(data))
        self.assertNotIn(("o0", "n2"), solver._enforceable_confirmed)
        result = solver.solve()
        self.assertNotEqual("infeasible", result.status)


class OptimalTaggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instance = Instance.from_dict(INSTANCE_A)
        self.result = Solver(self.instance).solve()

    def test_status_and_minimum(self) -> None:
        self.assertEqual("optimal", self.result.status)
        self.assertEqual(2, self.result.minimum_tagged_rows)
        self.assertEqual({"o0": "b1", "n2": "b1"}, self.result.tags)

    def test_confirmed_inside_and_forbidden_outside_every_view(self) -> None:
        for view in self.instance.views:
            pairs = self.result.view_results[view.name]
            self.assertIn(("o0", "n2"), pairs)
            self.assertNotIn(("o0", "n0"), pairs)
            self.assertNotIn(("o2", "n2"), pairs)

    def test_section_view_uses_native_patience_output(self) -> None:
        # Native patience on DX vs XD keeps only one pair; the solver must
        # report that real output rather than inventing an extra match.
        self.assertEqual(
            frozenset({("o0", "n2")}), self.result.view_results["section"]
        )

    def test_tag_is_global_per_row(self) -> None:
        # o0 and n2 both occur in both views; the single row->tag mapping
        # is what every view receives.
        tagging = self.result.tags
        for view in self.instance.views:
            rebuilt = {
                row: tagging[row]
                for row in view.old_rows + view.new_rows
                if row in tagging
            }
            self.assertEqual(
                native_pairs(self.instance, view, tagging),
                native_pairs(self.instance, view, rebuilt),
            )

    def test_minimality_by_brute_force(self) -> None:
        solver = Solver(self.instance)
        rows = solver._relevant_rows
        cheaper: list = []

        def enumerate_taggings(index: int, tagging: dict) -> None:
            if len(tagging) >= self.result.minimum_tagged_rows:
                return
            if index == len(rows):
                if solver._accepted_tagging(tagging):
                    cheaper.append(dict(tagging))
                return
            row = rows[index]
            enumerate_taggings(index + 1, tagging)
            for option in solver.instance.allowed_tags[row]:
                tagging[row] = option
                enumerate_taggings(index + 1, tagging)
                del tagging[row]

        enumerate_taggings(0, {})
        self.assertEqual([], cheaper)


class InfeasibilityTests(unittest.TestCase):
    def test_no_allowed_tags_blocks_duplicate_resolution(self) -> None:
        data = json.loads(json.dumps(INSTANCE_A))
        for row in data["old_rows"] + data["new_rows"]:
            row["allowed_tags"] = []
        result = Solver(Instance.from_dict(data)).solve()
        self.assertEqual("infeasible", result.status)
        self.assertTrue(
            any("full" in item for item in result.blockers),
            result.blockers,
        )

    def test_cross_view_conflict_is_localized(self) -> None:
        data = {
            "old_rows": [
                {"id": "o1", "text": "D", "allowed_tags": []},
                {"id": "o0", "text": "D", "allowed_tags": ["t", "u"]},
            ],
            "new_rows": [
                {"id": "n0", "text": "D", "allowed_tags": ["t"]},
                {"id": "n1", "text": "D", "allowed_tags": ["u"]},
            ],
            "views": [
                {"name": "v1", "old": ["o1", "o0"], "new": ["n0"]},
                {"name": "v2", "old": ["o1", "o0"], "new": ["n1"]},
            ],
            "confirmed": [["o0", "n0"], ["o0", "n1"]],
            "forbidden": [["o1", "n0"], ["o1", "n1"]],
        }
        instance = Instance.from_dict(data)
        result = Solver(instance).solve()
        self.assertEqual("infeasible", result.status)
        self.assertTrue(
            any("between views" in item for item in result.blockers)
        )
        # Each view is fixable on its own, but demands a different tag on o0.
        alone_tags = {}
        for view in instance.views:
            must = frozenset(
                pair for pair in instance.confirmed if pair[1] in view.new_rows
            )
            ban = frozenset(
                pair for pair in instance.forbidden if pair[1] in view.new_rows
            )
            sub = Instance(
                instance.texts, instance.allowed_tags, (view,), must, ban
            )
            sub_result = Solver(sub).solve()
            self.assertEqual("optimal", sub_result.status)
            alone_tags[view.name] = sub_result.tags["o0"]
        self.assertNotEqual(alone_tags["v1"], alone_tags["v2"])


class AmbiguityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instance = Instance.from_dict(INSTANCE_AMBIGUOUS)
        self.result = Solver(self.instance).solve()

    def test_two_minimal_taggings_diverge_in_full_view(self) -> None:
        self.assertEqual("ambiguous", self.result.status)
        self.assertEqual(1, self.result.minimum_tagged_rows)
        self.assertIn("full", self.result.ambiguous_views)
        variants = self.result.ambiguous_views["full"]
        distinct_pairs = {pairs for pairs, _ in variants}
        self.assertEqual(2, len(distinct_pairs))
        # Both real native results must still respect the forbidden pairs.
        for pairs, example in variants:
            self.assertFalse(pairs & self.instance.forbidden)
            self.assertEqual(1, len(example))

    def test_every_attached_result_is_reproducible_natively(self) -> None:
        for view_name, variants in self.result.ambiguous_views.items():
            view = next(v for v in self.instance.views if v.name == view_name)
            for pairs, example in variants:
                self.assertEqual(
                    pairs, native_pairs(self.instance, view, example)
                )


class MemoizationTests(unittest.TestCase):
    def test_view_result_cached_per_local_configuration(self) -> None:
        instance = Instance.from_dict(INSTANCE_A)
        solver = Solver(instance)
        calls = {"count": 0}
        original = solver_module.native_pairs

        def counting_native(inst, view, tagging):
            calls["count"] += 1
            return original(inst, view, tagging)

        solver_module.native_pairs = counting_native
        try:
            solver.solve()
            first = calls["count"]
            # Re-asking the same (view, local configuration) must be a hit.
            view = instance.views[0]
            cached_a = solver._pairs_cached(view, self.result_tagging())
            cached_b = solver._pairs_cached(view, self.result_tagging())
            self.assertIs(cached_a, cached_b)
            self.assertEqual(first, calls["count"])
        finally:
            solver_module.native_pairs = original

    @staticmethod
    def result_tagging() -> dict:
        return {"o0": "b1", "n2": "b1"}


class AnchorTaggingTests(unittest.TestCase):
    # old = D B D, new = B D B.  None of the D relation rows carry any
    # allowed tag; only the new-side anchor n0 may be tagged.  Patience
    # returns no matches until the anchor is disambiguated.
    INSTANCE = {
        "old_rows": [
            {"id": "o0", "text": "D", "allowed_tags": []},
            {"id": "o1", "text": "B", "allowed_tags": []},
            {"id": "o2", "text": "D", "allowed_tags": []},
        ],
        "new_rows": [
            {"id": "n0", "text": "B", "allowed_tags": ["q"]},
            {"id": "n1", "text": "D", "allowed_tags": []},
            {"id": "n2", "text": "B", "allowed_tags": []},
        ],
        "views": [
            {
                "name": "full",
                "old": ["o0", "o1", "o2"],
                "new": ["n0", "n1", "n2"],
            }
        ],
        "confirmed": [["o0", "n1"]],
        "forbidden": [["o2", "n1"]],
    }

    def test_untagged_patience_finds_nothing(self) -> None:
        instance = Instance.from_dict(self.INSTANCE)
        self.assertEqual(
            frozenset(), native_pairs(instance, instance.views[0], {})
        )

    def test_tagging_non_relation_anchor_resolves_relation_rows(self) -> None:
        instance = Instance.from_dict(self.INSTANCE)
        result = Solver(instance).solve()
        self.assertEqual("optimal", result.status)
        # n0 is part of no confirmed/forbidden relation, yet tagging it is
        # the only available lever; the candidate set must include it.
        self.assertEqual(1, result.minimum_tagged_rows)
        self.assertEqual({"n0": "q"}, result.tags)
        pairs = result.view_results["full"]
        self.assertIn(("o0", "n1"), pairs)
        self.assertNotIn(("o2", "n1"), pairs)


class LongLedgerCachingTests(unittest.TestCase):
    def setUp(self) -> None:
        random = __import__("random")
        random.seed(11)
        size = 30
        old = [f"o{k}" for k in range(size)]
        new = [f"n{k}" for k in range(size)]
        old_rows = [
            {
                "id": f"o{k}",
                "text": (
                    "D"
                    if k in (5, 17)
                    else "E"
                    if k % 3 == 0
                    else f"Z{k}"
                ),
                "allowed_tags": (
                    ["b1"] if k == 5 else ["b2"] if k == 17 else []
                ),
            }
            for k in range(size)
        ]
        new_rows = [
            {
                "id": f"n{k}",
                "text": (
                    "D"
                    if k in (5, 17)
                    else "E"
                    if k % 3 == 0
                    else f"Z{k}"
                ),
                "allowed_tags": (
                    ["b2"] if k == 5 else ["b1"] if k == 17 else []
                ),
            }
            for k in range(size)
        ]
        views = []
        for index in range(10):
            lo = random.randint(0, size - 12)
            hi = lo + random.randint(10, 12)
            views.append(
                {
                    "name": f"w{index}",
                    "old": old[lo:hi],
                    "new": new[lo:hi],
                }
            )
        self.data = {
            "old_rows": old_rows,
            "new_rows": new_rows,
            "views": views,
            "confirmed": [["o5", "n17"]],
            "forbidden": [["o5", "n5"], ["o17", "n17"]],
        }

    def test_native_diff_runs_once_per_view_configuration(self) -> None:
        instance = Instance.from_dict(self.data)
        solver = Solver(instance)
        calls = {"count": 0}
        original = solver_module.native_pairs

        def counting_native(inst, view, tagging):
            calls["count"] += 1
            return original(inst, view, tagging)

        solver_module.native_pairs = counting_native
        try:
            result = solver.solve()
            distinct_configs = sum(
                len(cache) for cache in solver._pair_cache.values()
            )
        finally:
            solver_module.native_pairs = original

        self.assertEqual("optimal", result.status)
        self.assertEqual(2, result.minimum_tagged_rows)
        # Every cache miss is exactly one native patience run; shared rows
        # across views never cause a full re-diff per global combination.
        self.assertEqual(distinct_configs, calls["count"])
        for view in instance.views:
            pairs = result.view_results[view.name]
            if "o5" in view.old_rows and "n17" in view.new_rows:
                self.assertIn(("o5", "n17"), pairs)
            self.assertNotIn(("o5", "n5"), pairs)
            self.assertNotIn(("o17", "n17"), pairs)

        # A second solve on a fresh solver re-derives the same tagging.
        second = Solver(instance).solve()
        self.assertEqual(result.tags, second.tags)


class ReportAndCliTests(unittest.TestCase):
    def test_report_shape(self) -> None:
        result, instance = solve_dict(INSTANCE_A)
        payload = report(result, instance)
        self.assertEqual("optimal", payload["status"])
        self.assertEqual(2, payload["minimum_tagged_rows"])
        self.assertEqual(
            [
                {"row": "n2", "tag": "b1"},
                {"row": "o0", "tag": "b1"},
            ],
            payload["tags_by_original_row"],
        )
        view_names = {entry["name"] for entry in payload["views"]}
        self.assertEqual({"full", "section"}, view_names)

    def test_cli_writes_report_file(self) -> None:
        from reconcile.solver import main

        with tempfile.TemporaryDirectory() as directory:
            instance_path = os.path.join(directory, "instance.json")
            output_path = os.path.join(directory, "report.json")
            with open(instance_path, "w", encoding="utf-8") as handle:
                json.dump(INSTANCE_A, handle, ensure_ascii=False)
            code = main([instance_path, "-o", output_path])
            self.assertEqual(0, code)
            with open(output_path, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertEqual("optimal", payload["status"])


if __name__ == "__main__":
    unittest.main()
