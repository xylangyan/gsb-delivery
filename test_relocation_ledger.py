"""Tests for the relocation ledger.

Run with the repository's virtual environment::

    .venv/bin/python -m unittest test_relocation_ledger -v
"""

import json
import random
import tempfile
import unittest
from pathlib import Path

import relocation_ledger as ledger
import strict_rfc3339


EXAMPLE_WALL = "2038-01-19T04:15:00"
EXAMPLE_UTC = "2038-01-19T02:15:00"


def labels_for(wall, offset, ticks, digits):
    return ledger.label_pair(
        ledger.parse_second(wall), ledger.parse_offset(offset), ticks, digits
    )


class TestExactKeys(unittest.TestCase):
    def test_matches_library(self):
        random.seed(20261122)
        for _ in range(4000):
            digits = random.randint(0, 9)
            wall = random.randint(-2_000_000_000, 4_100_000_000)
            offset = random.choice(
                [0, 3600, -3600, 7200, -7200, 20700, -20700, 86340, -86340]
            )
            ticks = random.randrange(10 ** digits) if digits else 0
            old_key, new_key = ledger.key_pair(wall, offset, ticks, digits)
            old_label, new_label = ledger.label_pair(wall, offset, ticks, digits)
            if not strict_rfc3339.validate_rfc3339(old_label):
                continue
            self.assertEqual(
                old_key, _scaled(strict_rfc3339.rfc3339_to_timestamp(old_label))
            )
            self.assertEqual(
                new_key, _scaled(strict_rfc3339.rfc3339_to_timestamp(new_label))
            )

    def test_prompt_example(self):
        old_two, new_two = labels_for(EXAMPLE_WALL, "+02:00", 2, 7)
        old_three, new_three = labels_for(EXAMPLE_WALL, "+02:00", 3, 7)
        self.assertEqual(old_two, "2038-01-19T04:15:00.0000002+02:00")
        self.assertEqual(new_two, "2038-01-19T02:15:00.0000002Z")
        self.assertEqual(old_three, "2038-01-19T04:15:00.0000003+02:00")
        self.assertEqual(new_three, "2038-01-19T02:15:00.0000003Z")
        old_value = strict_rfc3339.rfc3339_to_timestamp(old_two)
        new_value = strict_rfc3339.rfc3339_to_timestamp(new_two)
        self.assertLess(old_value, new_value)
        old_three_value = strict_rfc3339.rfc3339_to_timestamp(old_three)
        new_three_value = strict_rfc3339.rfc3339_to_timestamp(new_three)
        self.assertGreater(old_three_value, new_three_value)

    def test_civil_roundtrip(self):
        for text in ("1970-01-01T00:00:00", "2038-01-19T04:15:00", "1900-01-01T00:00:00"):
            self.assertEqual(ledger.format_second(ledger.parse_second(text)), text)


class TestAgainstBruteForce(unittest.TestCase):
    def test_positions(self):
        random.seed(7)
        for _ in range(60):
            digits = random.choice([2, 3, 4])
            base = random.choice([2 ** 31 - 2, 2 ** 31, 2 ** 31 + 1, 10 ** 9])
            segments = []
            line = 1
            for index in range(random.randint(1, 4)):
                count = random.randint(1, 40)
                stride = random.choice([1, 2, 3])
                segments.append(
                    ledger.Segment(
                        f"s{index}",
                        base + random.randint(-2, 2),
                        random.choice([0, 7200, -7200, 3600, -1800]),
                        digits,
                        random.randrange(10 ** digits),
                        random.choice([1, 2, 3, 7]),
                        count,
                        line,
                        stride,
                    )
                )
                line += count * stride + 5
            rows = []
            for segment in segments:
                for index in range(segment.count):
                    old_key, new_key = segment.key_pair(index)
                    rows.append((segment.name, index, segment.line(index), old_key, new_key))
            old_order = sorted(rows, key=lambda row: (row[3], row[2]))
            new_order = sorted(rows, key=lambda row: (row[4], row[2]))
            old_rank = {row[:2]: rank for rank, row in enumerate(old_order)}
            new_rank = {row[:2]: rank for rank, row in enumerate(new_order)}
            engine = ledger.RelocationLedger(segments)
            for row in rows:
                position = engine.position(row[0], row[1])
                self.assertEqual(position.old_rank, old_rank[row[:2]])
                self.assertEqual(position.new_rank, new_rank[row[:2]])

    def test_range_counts(self):
        random.seed(11)
        wall = ledger.parse_second(EXAMPLE_WALL)
        segments = [
            ledger.Segment("A", wall, 7200, 7, 2, 1, 900, 1, 2),
            ledger.Segment("B", ledger.parse_second(EXAMPLE_UTC), 0, 7, 2, 1, 900, 2, 2),
        ]
        engine = ledger.RelocationLedger(segments)
        rows = []
        for segment in segments:
            for index in range(segment.count):
                old_key, new_key = segment.key_pair(index)
                rows.append((segment.name, index, segment.line(index), old_key, new_key))
        old_order = sorted(rows, key=lambda row: (row[3], row[2]))
        new_order = sorted(rows, key=lambda row: (row[4], row[2]))
        old_rank = {row[:2]: rank for rank, row in enumerate(old_order)}
        new_rank = {row[:2]: rank for rank, row in enumerate(new_order)}
        for _ in range(8):
            low = random.randint(1, 1801)
            high = random.randint(low, 1801)
            selected = [row for row in rows if low <= row[2] <= high]
            deltas = [
                new_rank[row[:2]] - old_rank[row[:2]] for row in selected
            ]
            summary = engine.summarize_lines(low, high)
            self.assertEqual(summary.rows, len(selected))
            self.assertEqual(summary.moved, sum(1 for delta in deltas if delta))
            self.assertEqual(summary.net_displacement, sum(deltas))
            self.assertEqual(
                summary.max_displacement, max((abs(delta) for delta in deltas), default=0)
            )
            self.assertEqual(
                summary.in_place_key_changed,
                sum(
                    1
                    for row, delta in zip(selected, deltas)
                    if delta == 0 and row[3] != row[4]
                ),
            )

    def test_interior_not_just_corners(self):
        """A certified period must match every phase, not only endpoints."""

        engine = ledger.RelocationLedger(
            [
                ledger.Segment("A", ledger.parse_second(EXAMPLE_WALL), 7200, 7, 2, 1, 200_000, 1, 2),
                ledger.Segment("B", ledger.parse_second(EXAMPLE_UTC), 0, 7, 2, 1, 200_000, 2, 2),
            ]
        )
        segment = engine.segment("A")
        period = engine.index_period(segment)
        deltas = [
            engine.rank_pair(segment, index)[1] - engine.rank_pair(segment, index)[0]
            for index in range(period)
        ]
        second = [
            engine.rank_pair(segment, index)[1] - engine.rank_pair(segment, index)[0]
            for index in range(period, 2 * period)
        ]
        self.assertGreater(len(set(deltas)), 1)
        self.assertEqual(deltas, second)


class TestEqualitySources(unittest.TestCase):
    def test_cross_station_equal_keys(self):
        segments = [
            ledger.Segment("A", ledger.parse_second(EXAMPLE_WALL), 7200, 7, 2, 1, 30, 1, 2),
            ledger.Segment("B", ledger.parse_second(EXAMPLE_UTC), 0, 7, 2, 1, 30, 2, 2),
        ]
        engine = ledger.RelocationLedger(segments)
        equalities = engine.equalities()
        row = [item for item in equalities if item.side == "old"]
        self.assertTrue(row)
        self.assertTrue(all(item.count_a >= 1 and item.count_b >= 1 for item in equalities))
        positions = {segments[0].line(1): engine.position(segments[0], 1)}
        # The two stations share new keys for this row pair.
        self.assertEqual(
            engine.new_key("A", 1), engine.new_key("B", 1)
        )


class TestPeriodicPathAgainstLibrary(unittest.TestCase):
    def test_forced_periodic_path(self):
        """Force the periodic branch and compare to the real library sort."""

        original = ledger.DIRECT_LIMIT
        ledger.DIRECT_LIMIT = 200
        try:
            random.seed(20261127)
            for _ in range(8):
                digits = random.choice([2, 3])
                wall = random.choice([-8, -2, 0, 1, 250, 2 ** 31 - 100, 2 ** 31 + 100])
                segments = []
                line = 1
                for index in range(random.randint(1, 3)):
                    count = random.randint(400, 700)
                    stride = random.choice([1, 2, 3])
                    segments.append(
                        ledger.Segment(
                            f"s{index}",
                            wall + random.randint(-3, 3),
                            random.choice([0, 7200, -7200]),
                            digits,
                            random.randrange(10 ** digits),
                            random.choice([1, 2, 3]),
                            count,
                            line,
                            stride,
                        )
                    )
                    line += count * stride + 7
                engine = ledger.RelocationLedger(segments)
                rows = []
                for segment in segments:
                    for index in range(segment.count):
                        old_label, new_label = segment.labels(index)
                        if not strict_rfc3339.validate_rfc3339(old_label):
                            rows = []
                            break
                        rows.append(
                            (
                                segment.name,
                                index,
                                segment.line(index),
                                strict_rfc3339.rfc3339_to_timestamp(old_label),
                                strict_rfc3339.rfc3339_to_timestamp(new_label),
                            )
                        )
                if not rows:
                    continue
                old_order = sorted(rows, key=lambda row: (row[3], row[2]))
                new_order = sorted(rows, key=lambda row: (row[4], row[2]))
                old_rank = {row[:2]: rank for rank, row in enumerate(old_order)}
                new_rank = {row[:2]: rank for rank, row in enumerate(new_order)}
                for segment in segments:
                    deltas = [
                        new_rank[(segment.name, index)] - old_rank[(segment.name, index)]
                        for index in range(segment.count)
                    ]
                    summary = engine.summarize_segment(segment)
                    self.assertEqual(summary.rows, segment.count)
                    self.assertEqual(summary.moved, sum(1 for delta in deltas if delta))
                    self.assertEqual(summary.net_displacement, sum(deltas))
                    self.assertEqual(
                        summary.max_displacement,
                        max((abs(delta) for delta in deltas), default=0),
                    )
                    self.assertEqual(
                        summary.in_place_key_changed,
                        sum(
                            1
                            for index, delta in enumerate(deltas)
                            if delta == 0
                            and segment.key_pair(index)[0] != segment.key_pair(index)[1]
                        ),
                    )
        finally:
            ledger.DIRECT_LIMIT = original


class TestReport(unittest.TestCase):
    def test_end_to_end(self):
        dossier = {
            "segments": [
                {
                    "name": "A",
                    "wall_start": EXAMPLE_WALL,
                    "offset": "+02:00",
                    "digits": 7,
                    "tick_start": 2,
                    "step_ticks": 1,
                    "count": 4000,
                    "line_start": 1,
                    "line_step": 2,
                },
                {
                    "name": "B",
                    "wall_start": EXAMPLE_UTC,
                    "offset": "Z",
                    "digits": 7,
                    "tick_start": 2,
                    "step_ticks": 1,
                    "count": 4000,
                    "line_start": 2,
                    "line_step": 2,
                },
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dossier.json"
            path.write_text(json.dumps(dossier))
            segments = ledger.load_dossier(path)
            engine = ledger.RelocationLedger(segments)
            report = ledger.build_report(engine, [(1, 4000)])
            self.assertEqual(len(report["segments"]), 2)
            self.assertEqual(report["ranges"][0]["rows"], 4000)
            self.assertTrue(report["equalities"])
            segment = report["segments"][0]
            self.assertEqual(segment["offset"], "+02:00")
            self.assertEqual(segment["fraction"]["step_ticks"], 1)
            self.assertEqual(segment["line_step"], 2)
            self.assertTrue(segment["summary"]["moved"] > 0)

    def test_long_range_uses_period_not_rows(self):
        engine = ledger.RelocationLedger(
            [
                ledger.Segment("A", ledger.parse_second(EXAMPLE_WALL), 7200, 7, 2, 1, 400_000, 1, 2),
                ledger.Segment("B", ledger.parse_second(EXAMPLE_UTC), 0, 7, 2, 1, 400_000, 2, 2),
            ]
        )
        summary = engine.summarize_segment("A")
        self.assertEqual(summary.rows, 400_000)
        # Interior phases vary; the two period corners alone would not show it.
        self.assertGreater(summary.moved, 0)
        self.assertGreater(summary.in_place_key_changed, 0)


def _scaled(value):
    numerator, denominator = value.as_integer_ratio()
    return numerator << (ledger.SCALE_BITS - (denominator.bit_length() - 1))


if __name__ == "__main__":
    unittest.main()
