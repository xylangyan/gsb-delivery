"""Tests for idempotent batch tag cleanup (Zotero.cleanup_tags)."""

from __future__ import annotations

import json
import os
import unittest
from urllib.parse import parse_qs, urlparse

from pyzotero import zotero as z
from pyzotero._tagclean import (
    TagCleanupJournal,
    TagCleanupRequest,
    TagEvent,
    make_record,
    plan_tag_cleanup,
)
from pyzotero.errors import HTTPError, ParamNotPassedError

from .mock_client import MockClient

LOCAL = "http://127.0.0.1:23119/api"
SERVER_ID = "Teu5iBeFPFcF"


def local_zotero(mock, **kwargs):
    """Return a Zotero instance in local mode, backed by the mock client."""
    return z.Zotero("0", "user", local=True, client=mock.client, **kwargs)


def item(key, tags, abstract_note="", version=3):
    """Build a minimal full item document as returned by the API."""
    return {
        "key": key,
        "version": version,
        "library": {"type": "user", "id": 0},
        "meta": {},
        "data": {
            "key": key,
            "version": version,
            "itemType": "book",
            "title": f"Title {key}",
            "abstractNote": abstract_note,
            "tags": [{"tag": tag} for tag in tags],
        },
    }


def item_key_from(request):
    """Extract the itemKey query parameter from a recorded request."""
    query = parse_qs(urlparse(request.url).query)
    return query["itemKey"][0].split(",")


class PlanTagCleanupTests(unittest.TestCase):
    """The planner is a pure function of intent, state and journal."""

    def setUp(self):
        self.request = TagCleanupRequest(key="T1", remove=("review",), add=("checked",))

    def test_first_run_removes_and_adds(self):
        result = plan_tag_cleanup(self.request, ["review"], [], [], index=0)
        self.assertEqual(result.status, "applied")
        self.assertEqual(result.target_tags, ["checked"])

    def test_completed_record_is_not_written_again(self):
        journal = TagCleanupJournal()
        journal.mark_dispatched(self.request)
        journal.append(make_record(self.request, ["checked"]))
        # Acknowledgement was lost: replay against the state the write produced.
        result = plan_tag_cleanup(
            self.request,
            ["checked"],
            journal.records_for("T1"),
            journal.events_for("T1"),
            dispatches=journal.dispatches_for("T1"),
        )
        self.assertEqual(result.status, "already_applied")
        self.assertIsNone(result.target_tags)
        self.assertIsNotNone(result.record)

    def test_dispatched_but_already_satisfied_settles_without_write(self):
        journal = TagCleanupJournal()
        journal.mark_dispatched(self.request)
        result = plan_tag_cleanup(
            self.request,
            ["checked"],
            journal.records_for("T1"),
            journal.events_for("T1"),
            dispatches=journal.dispatches_for("T1"),
        )
        self.assertEqual(result.status, "already_applied")
        self.assertIsNone(result.target_tags)

    def test_recreated_tag_with_server_history_is_a_conflict(self):
        # Another editor removed review and re-created a tag of the same name.
        events = [
            TagEvent("review", "removed"),
            TagEvent("review", "added", actor="someone-else"),
        ]
        result = plan_tag_cleanup(self.request, ["review"], [], events)
        self.assertEqual(result.status, "conflict")
        self.assertEqual([conflict.tag for conflict in result.conflicts], ["review"])
        self.assertEqual(result.conflicts[0].actor, "someone-else")
        self.assertIsNone(result.target_tags)
        self.assertEqual(result.current_tags, ["review"])

    def test_recreated_tag_after_our_completion_is_a_conflict(self):
        records = [make_record(self.request, ["checked"])]
        # Same request again, but review has been added back by someone else.
        replay = TagCleanupRequest(key="T1", remove=("review",), add=("checked",))
        result = plan_tag_cleanup(replay, ["checked", "review"], records, [])
        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.current_tags, ["checked", "review"])

    def test_unconfirmed_dispatch_with_same_name_needs_review(self):
        journal = TagCleanupJournal()
        journal.mark_dispatched(self.request)
        result = plan_tag_cleanup(
            self.request,
            ["review"],
            journal.records_for("T1"),
            journal.events_for("T1"),
            dispatches=journal.dispatches_for("T1"),
        )
        self.assertEqual(result.status, "needs_review")
        self.assertIsNone(result.target_tags)
        self.assertIn("review", result.reasons[0])

    def test_backfill_only_adds_missing_tags(self):
        # review is already gone; only the checked addition is missing.
        result = plan_tag_cleanup(self.request, ["kept"], [], [])
        self.assertEqual(result.status, "backfill_applied")
        self.assertEqual(result.target_tags, ["kept", "checked"])

    def test_already_matching_state_needs_no_write(self):
        result = plan_tag_cleanup(self.request, ["checked"], [], [])
        self.assertEqual(result.status, "already_applied")
        self.assertIsNone(result.target_tags)

    def test_unknown_present_tag_without_dispatch_is_applied(self):
        # No dispatch means this is the first attempt: the removal is safe.
        result = plan_tag_cleanup(self.request, ["review"], [], [], dispatches=[])
        self.assertEqual(result.status, "applied")


class CleanupTagsClientTests(unittest.TestCase):
    """End-to-end behaviour through the mocked local API."""

    def setUp(self):
        cwd = os.path.dirname(os.path.realpath(__file__))
        with open(os.path.join(cwd, "api_responses", "item_fields.json")) as f:
            self.item_fields = f.read()
        self.mock = MockClient()
        self.mock.register("GET", f"{LOCAL}/itemFields", body=self.item_fields)
        self.zot = local_zotero(self.mock, server_id=SERVER_ID, local_api_key="abc123")

    def register_items(self, documents):
        """Register an items endpoint that filters by the itemKey parameter."""
        by_key = {doc["key"]: doc for doc in documents}

        def callback(recorded, uri, headers):
            keys = item_key_from(recorded)
            body = json.dumps([by_key[key] for key in keys if key in by_key])
            return 200, headers, body

        self.mock.register("GET", f"{LOCAL}/users/0/items", body=callback)

    def write_requests(self):
        return [
            request
            for request in self.mock.requests
            if request.method == "POST"
            and request.url.split("?")[0] == f"{LOCAL}/users/0/items"
        ]

    def test_t1_t2_t3_batch_classification_and_payloads(self):
        documents = [
            item("T1", ["review"]),
            item("T2", ["review"]),
            item("T3", ["kept"], abstract_note="human abstract edit"),
        ]
        self.register_items(documents)
        self.mock.register("POST", f"{LOCAL}/users/0/items", body="{}")
        journal = TagCleanupJournal()
        events = {
            "T2": [
                TagEvent("review", "removed"),
                TagEvent("review", "added", actor="someone-else"),
            ]
        }
        requests = [
            {"key": "T1", "remove": ["review"], "add": ["checked"]},
            {"key": "T2", "remove": ["review"], "add": ["checked"]},
            {"key": "T3", "remove": ["review"], "add": ["checked"]},
        ]

        report = self.zot.cleanup_tags(requests, journal=journal, events=events)

        self.assertEqual([result.index for result in report.results], [0, 1, 2])
        self.assertEqual([result.key for result in report.results], ["T1", "T2", "T3"])
        self.assertEqual(report.results[0].status, "applied")
        self.assertEqual(report.results[1].status, "conflict")
        self.assertEqual(report.results[2].status, "backfill_applied")
        self.assertEqual(report.conflicts[0].key, "T2")
        self.assertEqual(report.needs_review, [])

        writes = self.write_requests()
        self.assertEqual(len(writes), 1)
        body = writes[0].json()
        self.assertEqual({entry["key"] for entry in body}, {"T1", "T3"})
        t1 = next(entry for entry in body if entry["key"] == "T1")
        t3 = next(entry for entry in body if entry["key"] == "T3")
        self.assertEqual(t1["tags"], [{"tag": "checked"}])
        self.assertEqual(t3["tags"], [{"tag": "kept"}, {"tag": "checked"}])
        # The human's abstract edit survives: only tags are rewritten.
        self.assertEqual(t3["abstractNote"], "human abstract edit")
        # T2's re-created review was never sent in a write.
        self.assertNotIn("T2", [entry["key"] for entry in body])
        # Confirmed writes are journalised, the conflict is not.
        self.assertEqual(len(journal), 2)

    def test_replay_after_lost_acknowledgement_does_not_rewrite(self):
        first_mock = MockClient()
        first_mock.register("GET", f"{LOCAL}/itemFields", body=self.item_fields)

        def serve(tags):
            by_key = {"T1": item("T1", tags)}

            def callback(recorded, uri, headers):
                keys = item_key_from(recorded)
                return 200, headers, json.dumps([by_key[key] for key in keys])

            return callback

        first_mock.register("GET", f"{LOCAL}/users/0/items", body=serve(["review"]))
        first_mock.register("POST", f"{LOCAL}/users/0/items", body="{}")
        first_client = local_zotero(
            first_mock, server_id=SERVER_ID, local_api_key="abc123"
        )
        journal = TagCleanupJournal()
        request = [{"key": "T1", "remove": ["review"], "add": ["checked"]}]

        first = first_client.cleanup_tags(request, journal=journal)
        self.assertEqual(first.results[0].status, "applied")
        self.assertEqual(len(journal), 1)

        # Reopen against a server that applied the write; a fresh mock has no
        # POST route, so a second write would surface as an error.
        second_mock = MockClient()
        second_mock.register("GET", f"{LOCAL}/itemFields", body=self.item_fields)
        second_mock.register("GET", f"{LOCAL}/users/0/items", body=serve(["checked"]))
        reopened = local_zotero(
            second_mock, server_id=SERVER_ID, local_api_key="abc123"
        )
        second = reopened.cleanup_tags(request, journal=journal)
        self.assertEqual(second.results[0].status, "already_applied")
        self.assertEqual(
            [request for request in second_mock.requests if request.method == "POST"],
            [],
        )

    def test_replay_with_unconfirmed_dispatch_and_same_tag_needs_review(self):
        self.register_items([item("T1", ["review"])])
        journal = TagCleanupJournal()
        journal.mark_dispatched(
            TagCleanupRequest(key="T1", remove=("review",), add=("checked",))
        )
        report = self.zot.cleanup_tags(
            [{"key": "T1", "remove": ["review"], "add": ["checked"]}],
            journal=journal,
        )
        self.assertEqual(report.results[0].status, "needs_review")
        self.assertEqual(self.write_requests(), [])

    def test_missing_key_is_rejected(self):
        with self.assertRaises(ParamNotPassedError):
            self.zot.cleanup_tags([{"remove": ["review"]}])

    def test_unretrievable_item_is_needs_review(self):
        self.register_items([])
        report = self.zot.cleanup_tags(
            [{"key": "GONE", "remove": ["review"], "add": ["checked"]}]
        )
        self.assertEqual(report.results[0].status, "needs_review")
        self.assertEqual(self.write_requests(), [])

    def test_reads_are_chunked_at_fifty_keys(self):
        documents = [item(f"K{idx:02d}", ["checked"]) for idx in range(51)]
        self.register_items(documents)
        requests = [
            {"key": doc["key"], "remove": ["review"], "add": ["checked"]}
            for doc in documents
        ]
        report = self.zot.cleanup_tags(requests)
        item_reads = [
            request
            for request in self.mock.requests
            if request.method == "GET" and "itemKey" in request.url
        ]
        self.assertEqual(len(item_reads), 2)
        self.assertEqual(len(item_key_from(item_reads[0])), 50)
        self.assertEqual(len(item_key_from(item_reads[1])), 1)
        # Every item already matches, so nothing is written.
        self.assertEqual(self.write_requests(), [])
        self.assertEqual(len(report.results), 51)
        self.assertTrue(
            all(result.status == "already_applied" for result in report.results)
        )

    def test_positions_are_stable_across_chunking(self):
        documents = [
            item("T1", ["review"]),
            item("T2", ["review"]),
            item("T3", ["kept"]),
        ]
        self.register_items(documents)
        self.mock.register("POST", f"{LOCAL}/users/0/items", body="{}")
        events = {"T2": [TagEvent("review", "removed"), TagEvent("review", "added")]}
        report = self.zot.cleanup_tags(
            [
                {"key": "T1", "remove": ["review"], "add": ["checked"]},
                {"key": "T2", "remove": ["review"], "add": ["checked"]},
                {"key": "T3", "remove": ["review"], "add": ["checked"]},
            ],
            events=events,
        )
        self.assertEqual([result.index for result in report.results], [0, 1, 2])
        self.assertEqual(
            [result.status for result in report.results],
            ["applied", "conflict", "backfill_applied"],
        )

    def test_accepts_request_objects(self):
        self.register_items([item("T1", ["review"])])
        self.mock.register("POST", f"{LOCAL}/users/0/items", body="{}")
        report = self.zot.cleanup_tags(
            [TagCleanupRequest(key="T1", remove=("review",), add=("checked",))]
        )
        self.assertEqual(report.results[0].status, "applied")
        self.assertEqual(
            self.write_requests()[0].json()[0]["tags"], [{"tag": "checked"}]
        )

    def test_surviving_tag_metadata_is_preserved(self):
        document = item("T1", [])
        document["data"]["tags"] = [
            {"tag": "automatic", "type": 1},
            {"tag": "review"},
        ]
        self.register_items([document])
        self.mock.register("POST", f"{LOCAL}/users/0/items", body="{}")
        self.zot.cleanup_tags([{"key": "T1", "remove": ["review"], "add": ["checked"]}])
        tags = self.write_requests()[0].json()[0]["tags"]
        self.assertEqual(
            tags,
            [
                {"tag": "automatic", "type": 1},
                {"tag": "checked"},
            ],
        )

    def test_empty_batch_is_a_noop(self):
        report = self.zot.cleanup_tags([])
        self.assertEqual(report.results, [])
        self.assertEqual(self.mock.requests, [])

    def test_failed_write_leaves_a_dispatch_for_the_retry(self):
        self.register_items([item("T1", ["review"])])
        self.mock.register(
            "POST",
            f"{LOCAL}/users/0/items",
            body="boom",
            status=500,
            content_type="text/plain",
        )
        journal = TagCleanupJournal()
        request = [{"key": "T1", "remove": ["review"], "add": ["checked"]}]
        with self.assertRaises(HTTPError):
            self.zot.cleanup_tags(request, journal=journal)
        # The dispatch predates the request, so the failed acknowledgement
        # leaves evidence that a write may have landed.
        self.assertEqual(len(journal.dispatches_for("T1")), 1)
        self.assertEqual(len(journal), 0)

        # Retry: review is still present and no history explains it.
        self.register_items([item("T1", ["review"])])
        writes_before_retry = len(self.write_requests())
        report = self.zot.cleanup_tags(request, journal=journal)
        self.assertEqual(report.results[0].status, "needs_review")
        self.assertEqual(len(self.write_requests()), writes_before_retry)

    def test_events_persist_in_the_journal_across_calls(self):
        self.register_items([item("T2", ["review"])])
        journal = TagCleanupJournal()
        request = [{"key": "T2", "remove": ["review"], "add": ["checked"]}]
        first = self.zot.cleanup_tags(
            request,
            journal=journal,
            events={"T2": [TagEvent("review", "added", actor="someone-else")]},
        )
        self.assertEqual(first.results[0].status, "conflict")
        # A later call passes no events: those ingested earlier still apply.
        second = self.zot.cleanup_tags(request, journal=journal)
        self.assertEqual(second.results[0].status, "conflict")
        self.assertEqual(second.results[0].conflicts[0].actor, "someone-else")

    def test_default_instance_journal_tracks_completion_across_calls(self):
        self.register_items([item("T1", ["review"])])
        self.mock.register("POST", f"{LOCAL}/users/0/items", body="{}")
        request = [{"key": "T1", "remove": ["review"], "add": ["checked"]}]
        first = self.zot.cleanup_tags(request)
        self.assertEqual(first.results[0].status, "applied")

        # State reflects the completed write; no journal argument is given.
        self.register_items([item("T1", ["checked"])])
        second = self.zot.cleanup_tags(request)
        self.assertEqual(second.results[0].status, "already_applied")
        self.assertEqual(len(self.write_requests()), 1)


if __name__ == "__main__":
    unittest.main()
