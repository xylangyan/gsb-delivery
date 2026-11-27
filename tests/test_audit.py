from __future__ import unicode_literals

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "demo"))

from make_tables import (  # noqa: E402
    write_push_tables,
    write_secondary_tables,
    write_tables,
)
from pyuca.audit import (  # noqa: E402
    AuditError,
    audit,
    diff_tables,
    load_collator,
)


class AuditTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_path, self.new_path = write_tables(self.tmp.name)
        self.old = load_collator(self.old_path)
        self.new = load_collator(self.new_path)

    def catalog(self, records, **extra):
        data = {"old_table": self.old_path, "new_table": self.new_path,
                "records": records}
        data.update(extra)
        return data

    def run_audit(self, records, **options):
        return audit(self.old_path, self.new_path,
                     self.catalog(records), **options)


class NfdAndContractionTest(AuditTestCase):

    def test_nfd_marks_are_reordered_before_matching(self):
        self.assertEqual(
            self.old.sort_key_levels("á̧"),
            self.old.sort_key_levels("á̧"),
        )

    def test_discontiguous_contraction_changes_the_following_match(self):
        # Old: a+acute contraction, cedilla remains a secondary mark.
        # New: a+cedilla contraction can skip the acute; acute then rides on
        # the contraction instead of forming a+acute.
        old_levels = self.old.sort_key_levels("á̧")
        new_levels = self.new.sort_key_levels("á̧")
        self.assertEqual(old_levels[0], (0x1000,))
        self.assertEqual(new_levels[0], (0x0F00,))
        self.assertEqual(new_levels[1], (0x20, 0x40))

    def test_contraction_is_reported_separately(self):
        diff = diff_tables(self.old, self.new)
        contractions = [change for change in diff.rule_changes
                        if change["is_contraction"]]
        self.assertEqual(len(contractions), 1)
        self.assertEqual(contractions[0]["key"], "0061 0327")
        self.assertEqual(contractions[0]["kind"], "added")

    def test_sort_key_levels_matches_flat_sort_key(self):
        for name in ("a", "á", "á̧", "café"):
            levels = self.old.sort_key_levels(name)
            flat = self.old.sort_key(name)
            rebuilt = []
            for level in levels:
                if rebuilt:
                    rebuilt.append(0)
                rebuilt.extend(level)
            self.assertEqual(tuple(rebuilt), flat)


class RankTest(AuditTestCase):

    def test_primary_mode_ignores_secondary_reversal(self):
        old_path, new_path = write_secondary_tables(self.tmp.name)
        old = load_collator(old_path)
        new = load_collator(new_path)
        records = [
            {"entry": 1, "name": "a"},
            {"entry": 2, "name": "á"},
            {"entry": 3, "name": "a̧"},
        ]
        full = audit(old_path, new_path, {"records": records}, mode="full")
        primary = audit(old_path, new_path, {"records": records},
                        mode="primary")

        full_ranks = {
            row["entry_first"]: row["new_rank_first"]
            for row in full.to_dict()["records"]
        }
        primary_ranks = {
            row["entry_first"]: row["new_rank_first"]
            for row in primary.to_dict()["records"]
        }
        self.assertNotEqual(full_ranks, primary_ranks)
        # Full keys reverse the two accented forms; primary keys do not.
        self.assertEqual(full_ranks[2], 3)
        self.assertEqual(full_ranks[3], 2)
        self.assertEqual(primary_ranks, {1: 1, 2: 2, 3: 3})
        self.assertEqual(primary.key_changes, 0)
        self.assertGreater(full.key_changes, 0)

    def test_entry_tie_break_uses_first_accession_number(self):
        records = [
            {"entry": 20, "name": "same"},
            {"entry": 11, "name": "same"},
        ]
        report = self.run_audit(records, mode="full", tie_break="entry")
        ranks = [row["new_rank_first"]
                 for row in report.to_dict()["records"]]
        self.assertEqual(ranks, [1, 2])
        pair = report.pair_detail(20, 11)
        self.assertEqual(pair["new_decision"]["level"], "entry")

    def test_unbroken_ties_can_share_a_rank(self):
        records = [
            {"entry": 1, "name": "same"},
            {"entry": 2, "name": "same"},
        ]
        report = self.run_audit(records, tie_break="none")
        rows = report.to_dict(include_keys=True)["records"]
        self.assertEqual(rows[0]["new_rank_first"], 1)
        self.assertEqual(rows[1]["new_rank_first"], 1)

    def test_tied_weighted_run_shares_the_full_span(self):
        records = [
            {"entry_range": [10, 12], "name": "same"},
            {"entry": 1, "name": "same"},
            {"entry": 20, "name": "z"},
        ]
        report = self.run_audit(records, tie_break="none")
        rows = report.to_dict()["records"]
        by_entry = {row["entry_first"]: row for row in rows}
        self.assertEqual(
            (by_entry[10]["new_rank_first"], by_entry[10]["new_rank_last"]),
            (1, 4))
        self.assertEqual(
            (by_entry[1]["new_rank_first"], by_entry[1]["new_rank_last"]),
            (1, 4))
        self.assertEqual(by_entry[20]["new_rank_first"], 5)

    def test_ranks_include_the_whole_accession_range(self):
        records = [
            {"entry_range": [10, 14], "name": "repeat"},
            {"entry": 20, "name": "z"},
        ]
        report = self.run_audit(records)
        row = report.to_dict()["records"][0]
        self.assertEqual((row["old_rank_first"], row["old_rank_last"]),
                         (1, 5))
        self.assertEqual(row["new_rank_last"], 5)


class OldKeyTest(AuditTestCase):

    def test_stored_old_keys_are_the_old_side_authority(self):
        records = [
            {"entry": 1, "name": "a", "old_key": "2000 | 0020 | 0002 |"},
            {"entry": 2, "name": "b", "old_key": "1000 | 0020 | 0002 |"},
        ]
        report = self.run_audit(records, trust_old_keys=True,
                                verify_old_keys=False)
        rows = report.to_dict()["records"]
        self.assertEqual(rows[0]["old_rank_first"], 2)
        self.assertEqual(rows[1]["old_rank_first"], 1)

    def test_small_order_recomputes_both_sides_by_default(self):
        records = [
            {"entry": 1, "name": "a", "old_key": "2000 | 0020 | 0002 |"},
            {"entry": 2, "name": "b", "old_key": "1000 | 0020 | 0002 |"},
        ]
        report = self.run_audit(records)
        rows = report.to_dict()["records"]
        # The old table puts a before b regardless of the stored keys.
        self.assertEqual(rows[0]["old_rank_first"], 1)
        self.assertEqual(rows[1]["old_rank_first"], 2)
        self.assertEqual(
            len([w for w in report.warnings
                 if w["kind"] == "old_key_mismatch"]), 2)

    def test_primary_old_index_is_upgraded_from_old_table(self):
        records = [
            {"entry": 1, "name": "á", "old_key": "1000"},
            {"entry": 2, "name": "a̧", "old_key": "1000"},
        ]
        report = self.run_audit(records, mode="full")
        self.assertTrue(any(warning["kind"] == "old_key_incomplete"
                            for warning in report.warnings))
        rows = report.to_dict()["records"]
        self.assertEqual(rows[0]["old_rank_first"], 1)
        self.assertEqual(rows[1]["old_rank_first"], 2)

    def test_mismatched_stored_key_is_flagged(self):
        records = [{"entry": 1, "name": "a",
                    "old_key": "FFFF | 0020 | 0002 |"}]
        report = self.run_audit(records)
        self.assertTrue(any(warning["kind"] == "old_key_mismatch"
                            for warning in report.warnings))
        # The old table remains authoritative for the board.
        self.assertEqual(report.to_dict()["records"][0]["old_rank_first"], 1)


class LargeOrderTest(AuditTestCase):

    def test_large_order_reuses_keys_for_untouched_names(self):
        records = [
            {"entry": 1, "name": "e", "old_key": "1400 | 0020 | 0002 |"},
            {"entry": 2, "name": "á", "old_key": "1000 | 0020 0021 | 0002 0002 |"},
        ]
        # Force the large-order path without a million rows.
        report = audit(self.old_path, self.new_path, {"records": records},
                       large_threshold=1, trust_old_keys=True,
                       verify_old_keys=False)
        rows = report.to_dict(include_keys=True)["records"]
        plain = [row for row in rows if row["name"] == "e"][0]
        accented = [row for row in rows if row["name"] == "á"][0]
        self.assertEqual(plain["new_key"], plain["old_key"])
        self.assertNotEqual(accented["new_key"], accented["old_key"])

    def test_large_order_decomposes_before_deciding_reuse(self):
        # A precomposed character whose NFD form contains a changed mark must
        # not be treated as untouched just because the raw character differs
        # from the changed codepoint.
        records = [
            {"entry": 1, "name": "é",
             "old_key": "1400 | 0020 0021 | 0002 0002 |"},
        ]
        report = audit(self.old_path, self.new_path, {"records": records},
                       large_threshold=1, trust_old_keys=True,
                       verify_old_keys=False)
        row = report.to_dict(include_keys=True)["records"][0]
        self.assertNotEqual(row["new_key"], row["old_key"])

    def test_approval_marks_unapproved_changes(self):
        data = self.catalog([
            {"entry": 1, "name": "a"},
        ], approvals={"rules": ["0061 0327", "0301"],
                      "implicit_ranges": []})
        report = audit(self.old_path, self.new_path, data,
                       approvals=data["approvals"])
        self.assertEqual(report.table_diff.unapproved, [])
        data["approvals"] = {"rules": ["0061 0327"]}
        report = audit(self.old_path, self.new_path, data,
                       approvals=data["approvals"])
        self.assertEqual(
            [item["key"] for item in report.table_diff.unapproved],
            ["0301"])


class PairAndPageTest(AuditTestCase):

    def test_swapped_pair_count_is_weighted_not_expanded(self):
        old_path, new_path = write_secondary_tables(self.tmp.name)
        records = [
            {"entry_range": [1, 5], "name": "a̧"},
            {"entry_range": [10, 12], "name": "á"},
        ]
        report = audit(old_path, new_path, {"records": records})
        self.assertEqual(report.pair_count, 15)
        self.assertEqual(report.group_pair_count, 1)
        self.assertEqual(len(report.sample_pairs), 1)

    def test_pair_detail_reports_earliest_deciding_level(self):
        records = [
            {"entry": 1, "name": "á"},
            {"entry": 2, "name": "a̧"},
        ]
        report = self.run_audit(records, mode="full")
        pair = report.pair_detail(1, 2)
        self.assertEqual(pair["old_decision"]["level"], "secondary")
        self.assertEqual(pair["new_decision"]["level"], "primary")

    def test_pair_inside_one_repeated_block_uses_entry_tie_break(self):
        records = [{"entry_range": [7, 9], "name": "same"}]
        report = self.run_audit(records)
        pair = report.pair_detail(7, 9)
        self.assertFalse(pair["swapped"])
        self.assertEqual(pair["old_decision"]["level"], "entry")
        self.assertEqual(pair["old_order"], "left_first")

    def test_page_moves_include_a_kept_key_pushed_by_a_neighbour(self):
        old_path, new_path = write_push_tables(self.tmp.name)
        # `d` moves from last to between a and b.  Entries 2 and 3 keep
        # identical keys but are pushed one rank later, across a page edge.
        report = audit(
            old_path, new_path,
            {"old_table": old_path, "new_table": new_path,
             "records": [
                 {"entry": 1, "name": "a"},
                 {"entry": 2, "name": "b"},
                 {"entry": 3, "name": "c"},
                 {"entry": 4, "name": "d"},
             ]},
            pages={"sizes": [3, 1]},
        )
        moves = {move["entry_first"]: move for move in report.page_moves}
        self.assertIn(3, moves)
        self.assertFalse(moves[3]["key_changed"])
        self.assertEqual((moves[3]["from_page"], moves[3]["to_page"]),
                         (1, 2))

    def test_boundary_report_uses_fixed_accession_label(self):
        records = [
            {"entry": 1, "name": "a"},
            {"entry": 2, "name": "b"},
            {"entry": 3, "name": "c"},
            {"entry": 4, "name": "d"},
        ]
        report = audit(
            self.old_path, self.new_path,
            self.catalog(records),
            pages={"anchors": [3]}, boundaries=[3],
        )
        self.assertEqual(report.old_pages.label(1), 3)
        self.assertTrue(all(move["crossed_boundaries"] == ["3"]
                            for move in report.page_moves))

    def test_page_ranges_can_stay_in_original_accession_segments(self):
        records = [
            {"entry_range": [10, 14], "name": "repeat"},
            {"entry": 20, "name": "z"},
        ]
        report = self.run_audit(records, pages={"size": 3},
                                boundaries=[1])
        old_ranges = report.page_ranges["old"]
        self.assertEqual(old_ranges[0]["ranges"], [[10, 12]])
        self.assertEqual(old_ranges[1]["ranges"], [[13, 14], [20, 20]])
        self.assertEqual(report.to_dict(expand=True)["records"][0]["entries"],
                         [10, 11, 12, 13, 14])


class FragmentAndRangeTest(AuditTestCase):

    def test_fragments_are_joined_before_hashing(self):
        records = [
            {"entry": 1, "fragments": ["head", {"ref": "tail"}]},
            {"entry": 2, "name": "a̧"},
        ]
        catalog = self.catalog(records)
        catalog["fragments"] = {
            "head": "á",
            "tail": ["̧"],
        }
        report = audit(self.old_path, self.new_path, catalog)
        rows = report.to_dict(include_keys=True)["records"]
        self.assertEqual(rows[0]["name"], "á̧")
        self.assertNotEqual(rows[0]["new_key"], rows[1]["new_key"])

    def test_repeated_fragment_does_not_expand_a_giant_card(self):
        catalog = self.catalog([
            {"entry_range": [1, 10 ** 9], "fragments": [
                {"ref": "page", "repeat": 1000000},
            ]},
        ])
        catalog["fragments"] = {"page": "x"}
        with self.assertRaises(AuditError):
            audit(self.old_path, self.new_path, catalog)

    def test_stepped_ranges_keep_their_step(self):
        records = [{"entry_range": {"start": 10, "end": 20, "step": 2},
                    "name": "a"}]
        report = self.run_audit(records, pages={"size": 2})
        ranges = report.page_ranges["old"][1]["ranges"]
        self.assertIsInstance(ranges[0], dict)
        self.assertEqual(ranges[0]["step"], 2)

    def test_overlapping_ranges_are_rejected(self):
        with self.assertRaises(AuditError):
            self.run_audit([
                {"entry_range": [1, 5], "name": "a"},
                {"entry_range": [5, 9], "name": "b"},
            ])

    def test_fragments_are_not_keyed_individually(self):
        # A fragment used before many different heads must be combined with
        # its actual prefix before key generation.  The same suffix produces
        # different keys and different positions for `ja` and `ma`.
        old = _table_text([
            ("0061", "[.1000.0020.0002]"),
            ("006A", "[.1050.0020.0002]"),
            ("006D", "[.2000.0020.0002]"),
            ("0301", "[.0000.0021.0002]"),
        ])
        new = _table_text([
            ("0061", "[.1000.0020.0002]"),
            ("006A", "[.1050.0020.0002]"),
            ("006D", "[.2000.0020.0002]"),
            ("0301", "[.0000.0060.0002]"),
            ("006A 0301", "[.1040.0020.0002]"),
        ])
        old_path = os.path.join(self.tmp.name, "frag-old.txt")
        new_path = os.path.join(self.tmp.name, "frag-new.txt")
        with open(old_path, "w", encoding="utf-8") as handle:
            handle.write(old)
        with open(new_path, "w", encoding="utf-8") as handle:
            handle.write(new)
        catalog = {
            "fragments": {"tail": "́"},
            "records": [
                {"entry": 1, "fragments": [{"literal": "ja"}, "tail"]},
                {"entry": 2, "fragments": [{"literal": "ma"}, "tail"]},
            ],
        }
        report = audit(old_path, new_path, catalog)
        keys = {row["name"]: row["new_key"]
                for row in report.to_dict(include_keys=True)["records"]}
        self.assertNotEqual(keys["já"], keys["má"])
        self.assertEqual(report.rank_at(1)["rank_first"], 1)


def _table_text(rules):
    return "@version demo\n" + "\n".join(
        "{0} ; {1}".format(codes, elements) for codes, elements in rules
    ) + "\n"


class CliTest(AuditTestCase):

    def test_cli_outputs_json(self):
        catalog = self.catalog([
            {"entry": 1, "name": "á"},
            {"entry": 2, "name": "a̧"},
        ])
        path = os.path.join(self.tmp.name, "catalog.json")
        with open(path, "w", encoding="utf-8") as catalog_file:
            json.dump(catalog, catalog_file)
        run = subprocess.run(
            [sys.executable, "-m", "pyuca.audit_cli", path,
             "--format", "json", "--pair", "1", "2"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["summary"]["entries"], 2)
        self.assertEqual(payload["pair"]["old_order"], "left_first")
        self.assertEqual(payload["pair"]["new_order"], "right_first")

    def test_cli_table_diff_without_catalog(self):
        run = subprocess.run(
            [sys.executable, "-m", "pyuca.audit_cli", "--compare",
             "{0}:{1}".format(self.old_path, self.new_path),
             "--format", "json"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["summary"]["rules"], 2)
        self.assertEqual(payload["summary"]["contractions"], 1)

    def test_cli_page_range_is_compact(self):
        catalog = self.catalog([
            {"entry_range": [1, 4], "name": "same"},
            {"entry": 9, "name": "z"},
        ], pages={"size": 3})
        path = os.path.join(self.tmp.name, "catalog.json")
        with open(path, "w", encoding="utf-8") as catalog_file:
            json.dump(catalog, catalog_file)
        run = subprocess.run(
            [sys.executable, "-m", "pyuca.audit_cli", path,
             "--format", "json", "--page-range", "1"],
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(run.returncode, 0, run.stderr)
        payload = json.loads(run.stdout)
        self.assertEqual(payload["old"]["ranges"], [[1, 3]])
        self.assertEqual(payload["new"]["ranges"], [[1, 3]])


if __name__ == "__main__":
    unittest.main()
