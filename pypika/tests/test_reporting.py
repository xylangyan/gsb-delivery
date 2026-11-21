import sqlite3
import unittest
from contextlib import closing

from pypika import Order, Table
from pypika.reporting import DeviceReadingsReport, build_device_readings_report


class DeviceReadingsReportTests(unittest.TestCase):
    start = "2026-11-10"
    end = "2026-11-11"

    def setUp(self):
        self.table = Table("readings")
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE readings ("
            "device TEXT, observed_at TEXT, record_id INTEGER, reading INTEGER, is_valid INTEGER)"
        )
        self.connection.executemany(
            "INSERT INTO readings VALUES (?, ?, ?, ?, ?)",
            [
                # A history before the visible day.  Only the last valid row may become the
                # left neighbour; the rows in between are invalid and must not contribute.
                ("A", "2026-11-09 22:00:00", 100, 40, 1),
                ("A", "2026-11-09 23:30:00", 104, 50, 1),
                ("A", "2026-11-09 23:40:00", 105, 45, 0),
                ("A", "2026-11-09 23:50:00", 106, 46, 0),
                ("A", "2026-11-09 23:55:00", 107, 47, 0),
                # Visible day for A: the first visible reading differs in time from the
                # previous valid reading and several invalid readings sit between them.
                ("A", "2026-11-10 00:05:00", 110, 10, 1),
                ("A", "2026-11-10 00:15:00", 111, 15, 0),
                ("A", "2026-11-10 05:00:00", 112, 20, 1),
                ("A", "2026-11-10 23:59:59", 113, 30, 1),
                # Two valid readings exactly at the exclusive right boundary.  Only the
                # first in (observed_at, record_id) order may be the next neighbour.
                ("A", "2026-11-11 00:00:00", 114, 60, 1),
                ("A", "2026-11-11 00:00:00", 115, 70, 1),
                # B has no readings outside the visible range, but does have a valid
                # reading at the same timestamp as A's invalid visible reading.
                ("B", "2026-11-10 00:15:00", 120, 99, 1),
                ("B", "2026-11-10 12:00:00", 121, 88, 1),
            ],
        )
        self.template = DeviceReadingsReport(self.table)

    def tearDown(self):
        self.connection.close()

    def _rows(self, query):
        cursor = self.connection.execute(str(query))
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def _by_record_id(self, rows):
        return {row["record_id"]: row for row in rows}

    def test_cross_midnight_neighbors_are_used_without_being_displayed(self):
        rows = self._rows(self.template.build(self.start, self.end))

        self.assertEqual({110, 111, 112, 113, 120, 121}, set(self._by_record_id(rows)))
        metrics = {
            record_id: (row["previous_reading"], row["next_reading"])
            for record_id, row in self._by_record_id(rows).items()
        }
        self.assertEqual(
            {
                110: (50, 20),
                111: (None, None),
                112: (10, 30),
                113: (20, 60),
                120: (None, 88),
                121: (99, None),
            },
            metrics,
        )

    def test_right_boundary_tie_only_uses_the_nearest_valid_record(self):
        rows = self._by_record_id(self._rows(self.template.build(self.start, self.end)))

        self.assertEqual(60, rows[113]["next_reading"])
        self.assertNotIn(114, rows)
        self.assertNotIn(115, rows)

    def test_invalid_visible_reading_stays_visible_without_metrics(self):
        row = self._by_record_id(self._rows(self.template.build(self.start, self.end)))[111]

        self.assertEqual(0, row["is_valid"])
        self.assertEqual(15, row["reading"])
        self.assertIsNone(row["previous_reading"])
        self.assertIsNone(row["next_reading"])

    def test_window_contains_visible_rows_and_at_most_one_neighbor_per_side(self):
        window = self._rows(self.template.window(self.start, self.end))
        keys = {row["record_id"] for row in window}

        self.assertEqual({104, 110, 112, 113, 114, 120, 121}, keys)
        # Invalid rows and rows that are neither visible nor a true neighbour must
        # not enter the calculation window, even though they exist in the history.
        self.assertTrue({100, 105, 106, 107, 111, 115}.isdisjoint(keys))
        for device in ("A", "B"):
            device_rows = [row for row in window if row["device"] == device]
            before = [row for row in device_rows if row["observed_at"] < self.start and row["is_valid"]]
            after = [row for row in device_rows if row["observed_at"] >= self.end and row["is_valid"]]
            self.assertLessEqual(len(before), 1)
            self.assertLessEqual(len(after), 1)

    def test_device_without_visible_metrics_adds_no_neighbors(self):
        self.connection.executemany(
            "INSERT INTO readings VALUES (?, ?, ?, ?, ?)",
            [
                ("C", "2026-11-09 12:00:00", 130, 70, 1),
                ("C", "2026-11-11 12:00:00", 131, 80, 1),
            ],
        )

        window = self._rows(self.template.window(self.start, self.end))
        report = self._rows(self.template.build(self.start, self.end))

        self.assertTrue(all(row["device"] != "C" for row in window))
        self.assertTrue(all(row["device"] != "C" for row in report))

    def test_narrow_range_keeps_the_correct_neighbors_on_both_sides(self):
        rows = self._rows(
            self.template.build("2026-11-10 00:00:00", "2026-11-10 00:10:00")
        )

        self.assertEqual(1, len(rows))
        self.assertEqual(110, rows[0]["record_id"])
        self.assertEqual(50, rows[0]["previous_reading"])
        self.assertEqual(20, rows[0]["next_reading"])

    def test_display_order_does_not_change_metrics(self):
        ascending = self._rows(self.template.build(self.start, self.end, order=Order.asc))
        descending = self._rows(self.template.build(self.start, self.end, order=Order.desc))

        self.assertEqual(
            {row["record_id"] for row in ascending},
            {row["record_id"] for row in descending},
        )
        self.assertEqual(
            {
                record_id: (row["previous_reading"], row["next_reading"])
                for record_id, row in self._by_record_id(ascending).items()
            },
            {
                record_id: (row["previous_reading"], row["next_reading"])
                for record_id, row in self._by_record_id(descending).items()
            },
        )
        self.assertEqual(
            [10, 15, 20, 30, 88, 99],
            [row["reading"] for row in ascending if row["reading"] is not None],
        )
        self.assertEqual(
            [99, 88, 30, 20, 15, 10],
            [row["reading"] for row in descending if row["reading"] is not None],
        )

    def test_reusing_template_for_other_dates_and_directions_keeps_original_query(self):
        original = self.template.build(self.start, self.end)
        original_sql = str(original)
        original_rows = self._rows(original)

        self.template.build("2026-11-10 00:00:00", "2026-11-10 00:10:00")
        self.template.build(self.start, self.end, order=Order.desc)
        regenerated = self.template.build(self.start, self.end)

        self.assertEqual(original_sql, str(regenerated))
        self.assertEqual(original_rows, self._rows(regenerated))

    def test_convenience_wrapper_builds_the_same_report(self):
        direct = self.template.build(self.start, self.end)

        wrapped = build_device_readings_report(self.table, self.start, self.end)

        self.assertEqual(str(direct), str(wrapped))
        self.assertEqual(self._rows(direct), self._rows(wrapped))

    def test_column_names_can_be_mapped_without_renaming_the_source(self):
        connection = sqlite3.connect(":memory:")
        with closing(connection):
            connection.execute(
                "CREATE TABLE telemetry ("
                "station TEXT, measured_at TEXT, id INTEGER, value INTEGER, good INTEGER)"
            )
            connection.executemany(
                "INSERT INTO telemetry VALUES (?, ?, ?, ?, ?)",
                [
                    ("A", "2026-11-09 23:00:00", 1, 5, 1),
                    ("A", "2026-11-10 01:00:00", 2, 7, 1),
                    ("A", "2026-11-11 00:00:00", 3, 9, 1),
                ],
            )
            report = build_device_readings_report(
                "telemetry",
                self.start,
                self.end,
                device="station",
                observed_at="measured_at",
                record_id="id",
                reading="value",
                is_valid="good",
                previous_reading="before",
                next_reading="after",
            )
            cursor = connection.execute(str(report))
            names = [column[0] for column in cursor.description]
            rows = [dict(zip(names, row)) for row in cursor.fetchall()]

        self.assertEqual(["station", "measured_at", "id", "value", "good", "before", "after"], names)
        self.assertEqual(1, len(rows))
        self.assertEqual(5, rows[0]["before"])
        self.assertEqual(9, rows[0]["after"])
