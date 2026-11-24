from ldaptor.protocols.ldap import roster
from ldaptor.protocols import pureber
from twisted.trial import unittest

import json


def record(source, number, name, **attributes):
    attrs = [(b"employeeNumber", (number.encode("ascii"),)), (b"cn", (name.encode(),))]
    for key, values in attributes.items():
        attrs.append((key.encode(), tuple(value.encode() for value in values)))
    return roster.RosterRecord(
        number=number.encode("ascii"),
        object_name=("uid=%s,%s" % (number, source)).encode("ascii"),
        attributes=tuple(attrs),
        source=source,
    )


def page(source, records=(), next_number=None, ended=False, version=None):
    return roster.SourcePage(
        source=source,
        records=tuple(records),
        next_number=None if next_number is None else next_number.encode("ascii"),
        ended=ended,
        version=version,
    )


def new_query(names=("primary", "secondary"), primary="primary", size=3):
    return roster.RosterQuery(
        query_id="q1",
        key=("search",),
        page_size=size,
        source_names=names,
        primary_source=primary,
    )


class NumberTests(unittest.TestCase):
    def test_numeric_equals_leading_zero(self):
        self.assertEqual(roster.number_key(b"84"), roster.number_key(b"084"))

    def test_numeric_order(self):
        values = [b"100", b"99", b"084"]
        self.assertEqual(
            sorted(values, key=roster.number_key),
            [b"084", b"99", b"100"],
        )


class PageBoundaryTests(unittest.TestCase):
    def test_084_stays_open_until_every_source_moves_past_it(self):
        query = new_query(size=10)
        query.feed(
            page(
                "primary",
                [record("primary", "100", "a")],
                next_number="100",
            )
        )
        query.feed(
            page(
                "secondary",
                [record("secondary", "084", "amber")],
                next_number="084",
            )
        )

        result = query.build_page()
        self.assertEqual(result.entries, [])
        self.assertEqual(result.need, ["secondary"])

        # A second response from B may still add a registration for 084.
        query.feed(
            page(
                "secondary",
                [record("secondary", "084", "amber2")],
                next_number="090",
            )
        )

        result = query.build_page()
        self.assertEqual([entry.number for entry in result.entries], [b"084"])
        entry = result.entries[0]
        self.assertEqual(entry.occurrences, (("secondary", 2),))
        self.assertEqual(len(entry.attribute_dict()[b"cn"]), 2)

    def test_watermark_equal_to_last_record_does_not_close_it(self):
        query = new_query(names=("primary",), size=10)
        query.feed(
            page(
                "primary",
                [record("primary", "084", "one")],
                next_number="084",
            )
        )
        result = query.build_page()
        self.assertEqual(result.entries, [])
        self.assertEqual(result.need, ["primary"])

        query.feed(
            page(
                "primary",
                [record("primary", "084", "two")],
                next_number="085",
            )
        )
        result = query.build_page()
        self.assertEqual([entry.number for entry in result.entries], [b"084"])
        self.assertEqual(result.entries[0].occurrences, (("primary", 2),))

    def test_late_same_number_batch_is_kept(self):
        query = new_query(names=("secondary",), primary="secondary", size=10)
        query.feed(
            page(
                "secondary",
                [record("secondary", "084", "first")],
                next_number="084",
            )
        )
        query.feed(
            page(
                "secondary",
                [record("secondary", "084", "late")],
                next_number="085",
            )
        )
        result = query.build_page()
        self.assertEqual([entry.number for entry in result.entries], [b"084"])
        self.assertEqual(result.entries[0].occurrences, (("secondary", 2),))

    def test_sources_are_read_in_ascending_order(self):
        query = new_query(size=10)
        query.feed(
            page(
                "primary",
                [
                    record("primary", "002", "b"),
                    record("primary", "004", "d"),
                ],
                ended=True,
            )
        )
        query.feed(
            page(
                "secondary",
                [
                    record("secondary", "001", "a"),
                    record("secondary", "004", "d2"),
                ],
                ended=True,
            )
        )
        result = query.build_page()
        self.assertEqual(
            [entry.number for entry in result.entries],
            [b"001", b"002", b"004"],
        )
        self.assertTrue(result.complete)


class MergeTests(unittest.TestCase):
    def test_primary_fields_win_but_sources_are_reported(self):
        query = new_query(size=10)
        query.feed(
            page(
                "primary",
                [record("primary", "7", "primary-name", mail=["primary@example"])],
                ended=True,
            )
        )
        query.feed(
            page(
                "secondary",
                [
                    record(
                        "secondary",
                        "7",
                        "secondary-name",
                        mail=["secondary@example"],
                    )
                ],
                ended=True,
            )
        )
        result = query.build_page()
        entry = result.entries[0]
        attributes = entry.attribute_dict()
        self.assertEqual(attributes[b"cn"], (b"primary-name",))
        self.assertEqual(attributes[b"mail"], (b"primary@example",))
        self.assertEqual(
            attributes[roster.SOURCE_ATTRIBUTE], (b"primary", b"secondary")
        )
        self.assertEqual(
            attributes[roster.OCCURRENCE_ATTRIBUTE],
            (b"primary:1", b"secondary:1"),
        )

    def test_fields_missing_from_primary_are_filled(self):
        query = new_query(size=10)
        query.feed(
            page(
                "primary",
                [record("primary", "7", "primary-name")],
                ended=True,
            )
        )
        query.feed(
            page(
                "secondary",
                [record("secondary", "7", "secondary-name", phone=["123"])],
                ended=True,
            )
        )
        entry = query.build_page().entries[0]
        attributes = entry.attribute_dict()
        self.assertEqual(attributes[b"cn"], (b"primary-name",))
        self.assertEqual(attributes[b"phone"], (b"123",))

    def test_duplicate_original_registrations_are_counted(self):
        query = new_query(names=("primary",), size=10)
        query.feed(
            page(
                "primary",
                [
                    record("primary", "7", "one"),
                    record("primary", "7", "two"),
                ],
                ended=True,
            )
        )
        entry = query.build_page().entries[0]
        self.assertEqual(entry.occurrences, (("primary", 2),))
        self.assertEqual(sorted(entry.attribute_dict()[b"cn"]), [b"one", b"two"])


class QueryIsolationTests(unittest.TestCase):
    def test_two_queries_have_independent_cursors_and_completion(self):
        night = new_query(size=2)
        other = new_query(size=2)

        night.feed(page("primary", [record("primary", "010", "n1")], ended=True))
        night.feed(page("secondary", [record("secondary", "010", "n2")], ended=True))

        other.feed(page("primary", [record("primary", "005", "o1")], ended=True))
        other.feed(page("secondary", [], next_number="084"))

        night_page = night.build_page()
        self.assertTrue(night_page.complete)
        self.assertEqual([entry.number for entry in night_page.entries], [b"010"])

        other_page = other.build_page()
        self.assertFalse(other_page.complete)
        self.assertEqual([entry.number for entry in other_page.entries], [b"005"])

    def test_page_size_is_per_query(self):
        night = new_query(size=1)
        for query in (night,):
            query.feed(
                page(
                    "primary",
                    [
                        record("primary", "001", "a"),
                        record("primary", "002", "b"),
                    ],
                    ended=True,
                )
            )
            query.feed(page("secondary", [], ended=True))
        result = night.build_page()
        self.assertEqual([entry.number for entry in result.entries], [b"001"])
        self.assertFalse(result.complete)


class ContinuationTests(unittest.TestCase):
    def test_confirmed_page_advances_the_resume_point(self):
        query = new_query(size=1)
        query.feed(
            page(
                "primary",
                [record("primary", "001", "a"), record("primary", "002", "b")],
                ended=True,
            )
        )
        query.feed(page("secondary", [], ended=True))

        first = query.build_page()
        query.mark_served(first.entries)
        self.assertEqual(query.confirmed_after, None)
        self.assertEqual(query.served_after, b"001")

        query.confirm_served()
        self.assertEqual(query.confirmed_after, b"001")

        second = query.build_page()
        self.assertEqual([entry.number for entry in second.entries], [b"002"])

    def test_unconfirmed_page_is_replayed_after_reconnect(self):
        query = new_query(size=1)
        query.feed(
            page(
                "primary",
                [record("primary", "001", "a"), record("primary", "002", "b")],
                ended=True,
            )
        )
        query.feed(page("secondary", [], ended=True))
        first = query.build_page()
        query.mark_served(first.entries)

        query.prepare_resume()
        replayed = query.build_page()
        self.assertEqual([entry.number for entry in replayed.entries], [b"001"])
        self.assertEqual(query.confirmed_after, None)


class TokenTests(unittest.TestCase):
    def test_token_is_bound_to_connection_and_single_use(self):
        pager = roster.RosterPager()
        query = pager.new_query(
            key=("k",), page_size=1, source_names=("a",), primary_source="a"
        )
        token = pager.issue_token(query, "conn-1")

        with self.assertRaises(roster.InvalidContinuationToken):
            pager.consume_token(token, "conn-2")

        # Consuming from the wrong connection already invalidated it.
        with self.assertRaises(roster.InvalidContinuationToken):
            pager.consume_token(token, "conn-1")

    def test_connection_loss_invalidates_tokens_but_keeps_query(self):
        pager = roster.RosterPager()
        query = pager.new_query(
            key=("k",), page_size=1, source_names=("a",), primary_source="a"
        )
        token = pager.issue_token(query, "conn-1")
        pager.connection_lost("conn-1")

        with self.assertRaises(roster.InvalidContinuationToken):
            pager.consume_token(token, "conn-2")
        self.assertIs(pager.get(query.query_id), query)

    def test_token_confirms_the_previously_served_page(self):
        pager = roster.RosterPager()
        query = pager.new_query(
            key=("k",), page_size=1, source_names=("a",), primary_source="a"
        )
        query.feed(page("a", [record("a", "001", "x")], ended=True))
        result = query.build_page()
        query.mark_served(result.entries)

        token = pager.issue_token(query, "conn")
        pager.consume_token(token, "conn")
        self.assertEqual(query.confirmed_after, b"001")


class LossTests(unittest.TestCase):
    def test_lost_version_reports_range_and_keeps_earlier_prefix(self):
        query = new_query(size=1)
        query.feed(
            page(
                "primary",
                [
                    record("primary", "001", "a"),
                    record("primary", "050", "z"),
                ],
                ended=True,
            )
        )
        query.feed(
            page(
                "secondary",
                [record("secondary", "001", "a2")],
                next_number="084",
            )
        )

        first = query.build_page()
        self.assertEqual([entry.number for entry in first.entries], [b"001"])
        query.mark_served(first.entries)
        query.confirm_served()

        query.mark_source_lost("secondary", start=b"084", reason="version gone")

        result = query.build_page()
        self.assertEqual([entry.number for entry in result.entries], [b"050"])
        self.assertTrue(result.blocked)
        self.assertEqual(len(result.unsettled), 1)
        span = result.unsettled[0]
        self.assertEqual(span.query_id, "q1")
        self.assertEqual(span.start, b"084")

    def test_already_complete_query_still_pages_with_another_lost(self):
        night = new_query(size=1)
        other = new_query(size=10)

        night.feed(
            page(
                "primary",
                [
                    record("primary", "001", "a"),
                    record("primary", "002", "b"),
                ],
                ended=True,
            )
        )
        night.feed(page("secondary", [], ended=True))

        other.feed(
            page("primary", [record("primary", "001", "x")], ended=True)
        )
        other.mark_source_lost("secondary", start=b"050")

        first = night.build_page()
        self.assertEqual([entry.number for entry in first.entries], [b"001"])
        night.mark_served(first.entries)
        night.confirm_served()

        second = night.build_page()
        self.assertEqual([entry.number for entry in second.entries], [b"002"])
        night.mark_served(second.entries)

        other_result = other.build_page()
        self.assertEqual(
            [entry.number for entry in other_result.entries], [b"001"]
        )
        self.assertTrue(other_result.blocked)
        self.assertEqual(len(other_result.unsettled), 1)


class ControlTests(unittest.TestCase):
    def test_paged_control_round_trip(self):
        control = roster.encode_paged_results_control(17, b"cookie")
        self.assertEqual(
            roster.decode_paged_results_control([control]),
            (17, b"cookie"),
        )

    def test_resume_control_round_trip(self):
        control = roster.encode_resume_control("q1", b"v1", b"084")
        self.assertEqual(
            roster.decode_resume_control([control]),
            ("q1", b"v1", b"084"),
        )

    def test_state_control_round_trip(self):
        query = new_query()
        query.confirmed_after = b"084"
        control = roster.encode_state_control(query)
        query_id, version, after = roster.decode_state_control([control])
        self.assertEqual(query_id, "q1")
        self.assertEqual(version, query.version)
        self.assertEqual(after, b"084")

    def test_unsettled_control_round_trip(self):
        control = roster.encode_unsettled_control(
            [roster.UnsettledRange("q1", b"084", b"100", "version gone")]
        )
        self.assertEqual(
            roster.decode_unsettled_control([control]),
            [
                {
                    "query_id": b"q1",
                    "start": b"084",
                    "end": b"100",
                    "reason": b"version gone",
                }
            ],
        )

    def test_malformed_paged_control_is_rejected(self):
        with self.assertRaises(roster.RosterProtocolError):
            roster.decode_paged_results_control(
                [(roster.PAGED_RESULTS_OID, None, pureber.BERInteger(4).toWire())]
            )

    def test_state_payload_is_plain_data(self):
        query = new_query()
        payload = query.state_payload()
        self.assertEqual(payload[0], "q1")
        self.assertIsInstance(payload[1], bytes)
        json.dumps([payload[0], payload[1].hex(), None])
