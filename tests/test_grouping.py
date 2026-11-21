import os
import shutil
import tempfile
import unittest

import pyexcel as pe

from .nose_tools import eq_, raises


SAMPLE = [
    ["A", "same"],
    ["B", "one"],
    ["A", "same"],
    ["C", "only"],
    ["B", "two"],
]


class CatalogCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.input = os.path.join(self.directory, "input.csv")
        self.catalog_path = os.path.join(self.directory, "catalog")
        pe.save_as(array=SAMPLE, dest_file_name=self.input)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def build(self, complete=True):
        return pe.build_catalog(
            self.catalog_path,
            group_by=0,
            file_name=self.input,
            source_row_start=0,
            complete=complete,
        )

    def reopen(self):
        return pe.open_catalog(self.catalog_path)


class TestBuild(CatalogCase):
    def test_groups_reported_in_first_seen_order(self):
        catalog = self.build()
        eq_(catalog.group_names(), ["A", "B", "C"])
        eq_(list(catalog), ["A", "B", "C"])

    def test_same_content_keeps_distinct_source_rows(self):
        catalog = self.build()
        rows = list(catalog.reader("A"))
        eq_([row.source_row for row in rows], [0, 2])
        eq_(rows[0].values, rows[1].values)

    def test_missing_group_cannot_be_opened(self):
        catalog = self.build()
        with self.assertRaises(pe.exceptions.UnknownGroup):
            catalog.reader("Z")

    def test_source_not_needed_after_build(self):
        catalog = self.build()
        catalog.close()
        os.unlink(self.input)
        reopened = self.reopen()
        eq_(reopened.group_names(), ["A", "B", "C"])
        eq_(len(list(reopened.reader("B"))), 2)


class TestAcknowledgement(CatalogCase):
    def test_acknowledge_only_the_delivered_row(self):
        catalog = self.build()
        reader = catalog.reader("A")
        first = reader.next()
        first.acknowledge()
        second = reader.next()
        eq_(second.source_row, 2)
        reader.acknowledge(second)
        eq_(reader.next(), None)

    def test_acknowledging_one_group_leaves_another_untouched(self):
        catalog = self.build()
        group_a = catalog.reader("A")
        group_b = catalog.reader("B")
        group_a.next().acknowledge()
        eq_(len(list(group_b)), 2)

    def test_two_readers_of_one_group_do_not_skip_for_each_other(self):
        catalog = self.build()
        first_reader = catalog.reader("B", reader_id="one")
        second_reader = catalog.reader("B", reader_id="two")
        eq_(first_reader.next().source_row, 1)
        eq_(second_reader.next().source_row, 1)
        eq_(first_reader.next().source_row, 4)

    def test_ack_does_not_make_an_open_reader_skip(self):
        catalog = self.build()
        first_reader = catalog.reader("B", reader_id="one")
        second_reader = catalog.reader("B", reader_id="two")
        first_reader.next().acknowledge()
        eq_(second_reader.next().source_row, 1)
        eq_(first_reader.next().source_row, 4)

    def test_cross_catalog_acknowledgement_is_rejected(self):
        first = self.build()
        delivered = first.reader("A").next()
        other_input = os.path.join(self.directory, "other.csv")
        pe.save_as(array=SAMPLE, dest_file_name=other_input)
        second = pe.build_catalog(
            os.path.join(self.directory, "other"),
            group_by=0,
            file_name=other_input,
        )
        second.reader("A").next()
        with self.assertRaises(pe.exceptions.UnknownGroup):
            second.reader("A").acknowledge(delivered)


class TestRecovery(CatalogCase):
    def test_reopen_replays_only_pending_rows(self):
        catalog = self.build()
        group_a = catalog.reader("A")
        group_b = catalog.reader("B")
        group_a.next().acknowledge()
        group_a.next()
        group_b.next().acknowledge()
        group_b.next().acknowledge()
        catalog.close()

        reopened = self.reopen()
        report = reopened.recover()
        eq_(report.group_names(), ["A", "B", "C"])
        eq_(report["A"].summary(), {
            "acknowledged": [0],
            "pending": [2],
            "unread": [],
        })
        eq_(report["B"].summary(), {
            "acknowledged": [1, 4],
            "pending": [],
            "unread": [],
        })
        eq_(report["C"].summary(), {
            "acknowledged": [],
            "pending": [],
            "unread": [3],
        })

        replay = reopened.reader("A")
        eq_(replay.next().source_row, 2)
        eq_(replay.next(), None)
        eq_(reopened.reader("B").next(), None)
        eq_(reopened.reader("C").next().source_row, 3)

    def test_recovery_does_not_consume_other_groups(self):
        catalog = self.build()
        catalog.reader("A").next().acknowledge()
        catalog.reader("B").next()
        catalog.close()

        reopened = self.reopen()
        reopened.reader("A").next()
        report = reopened.recover()
        eq_(report["B"].summary()["pending"], [1])
        eq_(report["C"].summary()["unread"], [3])

    def test_incomplete_catalog_is_undeliverable(self):
        catalog = self.build(complete=False)
        catalog.close()
        reopened = self.reopen()
        with self.assertRaises(pe.exceptions.CatalogIncomplete):
            reopened.recover()
        with self.assertRaises(pe.exceptions.CatalogIncomplete):
            reopened.reader("A")
        report = reopened.recover(require_complete=False)
        eq_(report.group_names(), ["A", "B", "C"])
        eq_(report["A"].counts()["unread"], 2)
        assert not reopened.deliverable

    def test_incomplete_catalog_states_are_visible(self):
        catalog = self.build(complete=False)
        catalog.close()
        reopened = self.reopen()
        eq_(
            reopened.states()["A"],
            {"acknowledged": 0, "pending": 0, "unread": 2},
        )

    def test_failed_source_leaves_incomplete_catalog(self):
        def broken():
            yield ["A", 1]
            yield ["B", 2]
            raise IOError("source truncated")

        path = os.path.join(self.directory, "broken")
        try:
            pe.build_catalog(path, group_by=0, source={"array": broken()})
        except IOError:
            pass
        reopened = pe.open_catalog(path)
        eq_(reopened.report().group_names(), ["A", "B"])
        with self.assertRaises(pe.exceptions.CatalogIncomplete):
            reopened.reader("A")

    @raises(IOError)
    def test_failed_source_raises(self):
        def broken():
            yield ["A", 1]
            raise IOError("source truncated")

        pe.build_catalog(
            os.path.join(self.directory, "broken"),
            group_by=0,
            source={"array": broken()},
        )


class TestKeys(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_default_source_rows_start_at_one(self):
        catalog = pe.build_catalog(
            os.path.join(self.directory, "cat"),
            group_by=0,
            array=[["A", "x"], ["B", "y"]],
        )
        eq_(
            [row.source_row for row in catalog.reader("A")],
            [1],
        )
        eq_(
            [row.source_row for row in catalog.reader("B")],
            [2],
        )

    def test_group_by_header_name(self):
        catalog = pe.build_catalog(
            os.path.join(self.directory, "cat"),
            group_by="supplier",
            header_row=0,
            array=[
                ["supplier", "amount"],
                ["A", 10],
                ["B", 20],
                ["A", 30],
            ],
        )
        eq_(catalog.group_names(), ["A", "B"])
        eq_([row.source_row for row in catalog.reader("A")], [2, 4])

    def test_group_by_callable(self):
        catalog = pe.build_catalog(
            os.path.join(self.directory, "cat"),
            group_by=lambda row: row[1],
            array=[["A", 7], ["B", 8], ["A", 7]],
        )
        eq_(catalog.group_names(), ["7", "8"])
        eq_(len(catalog.reader("7").next().values), 2)

    def test_rebuilding_replaces_catalog_identity(self):
        path = os.path.join(self.directory, "cat")
        first = pe.build_catalog(path, group_by=0, array=[["A", 1]])
        identity = first.uuid
        first.close()
        second = pe.build_catalog(path, group_by=0, array=[["B", 2]])
        assert second.uuid != identity
        eq_(second.group_names(), ["B"])


class TestLargeGroup(CatalogCase):
    def test_rows_delivered_one_at_a_time(self):
        def many():
            for index in range(5000):
                yield ["A", index]

        catalog = pe.build_catalog(
            os.path.join(self.directory, "large"),
            group_by=0,
            source={"array": many()},
        )
        reader = catalog.reader("A")
        count = 0
        while True:
            row = reader.next()
            if row is None:
                break
            row.acknowledge()
            count += 1
        eq_(count, 5000)
        eq_(reader.next(), None)

    def test_large_group_resumes_after_close(self):
        def many():
            for index in range(300):
                yield ["A", index]

        path = os.path.join(self.directory, "large")
        catalog = pe.build_catalog(
            path,
            group_by=0,
            source={"array": many()},
            source_row_start=0,
        )
        reader = catalog.reader("A")
        for _ in range(120):
            reader.next().acknowledge()
        reader.next()
        catalog.close()

        reopened = pe.open_catalog(path)
        report = reopened.recover()
        eq_(len(report["A"].acknowledged), 120)
        eq_([row.source_row for row in report["A"].pending], [120])
        remaining = []
        reader = reopened.reader("A")
        while True:
            row = reader.next()
            if row is None:
                break
            row.acknowledge()
            remaining.append(row.source_row)
        eq_(remaining, list(range(120, 300)))
