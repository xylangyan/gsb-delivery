from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from dramatiq.batches.backends import StubBatchStateBackend
from dramatiq.encoder import JSONEncoder


@pytest.fixture()
def backend():
    return StubBatchStateBackend()


def test_read_returns_none_for_unknown_batches(backend):
    # Given an empty backend
    # Then reading an unknown batch id yields None
    assert backend.read("missing") is None


def test_create_is_idempotent(backend):
    # Given a fresh state document
    state = {"batch_id": "b", "version": 1, "obligations": {}}

    # Then the first create succeeds and the second is a no-op
    assert backend.create("b", state, ttl=1000) is True
    assert backend.create("b", state, ttl=1000) is False
    assert backend.read("b")["version"] == 1


def test_replace_succeeds_only_when_the_version_matches(backend):
    # Given a persisted document at version 1
    backend.create("b", {"batch_id": "b", "version": 1}, ttl=1000)

    # Then a mismatched expected version refuses the write
    assert backend.replace_if_version("b", 2, {"batch_id": "b", "version": 2}, ttl=1000) is False
    assert backend.read("b")["version"] == 1

    # And a matching version applies the write
    assert backend.replace_if_version("b", 1, {"batch_id": "b", "version": 2}, ttl=1000) is True
    assert backend.read("b")["version"] == 2


def test_replace_unknown_batch_fails(backend):
    # Given no persisted document
    # Then replacing it fails rather than creating it
    assert backend.replace_if_version("missing", 1, {"batch_id": "missing", "version": 1}, ttl=1000) is False


def test_documents_round_trip_through_the_encoder(backend):
    # Given a document containing only JSON-serializable values
    state = {
        "batch_id": "b",
        "version": 3,
        "status": "open",
        "obligations": {
            "East1": {
                "key": "East1",
                "site": "East",
                "status": "pending",
                "range": None,
                "late": [],
            }
        },
        "order": ["East1"],
        "settlement_callbacks": [],
        "settlement": None,
    }
    backend.create("b", state, ttl=1000)

    # Then what comes back equals the original (JSON normalizes tuples)
    assert backend.read("b") == state


def test_custom_encoder_is_used(backend):
    # Given a backend configured with the default JSON encoder explicitly
    backend = StubBatchStateBackend(encoder=JSONEncoder())
    backend.create("b", {"batch_id": "b", "version": 1, "names": ["a", "b"]}, ttl=1000)

    # Then the document is stored and read back through that encoder
    assert backend.read("b")["names"] == ["a", "b"]


def test_mutate_retries_until_the_compare_and_swap_succeeds(backend):
    # Given a document at version 1
    backend.create("b", {"batch_id": "b", "version": 1, "counter": 0}, ttl=1000)

    # When many clients mutate concurrently
    def bump(_):
        def apply(document):
            document["counter"] += 1
            document["version"] += 1
            return document

        backend.mutate("b", apply, ttl=1000)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(bump, range(20)))

    # Then every bump is applied exactly once and no update was lost
    document = backend.read("b")
    assert document["counter"] == 20
    assert document["version"] == 21


def test_mutate_aborts_without_writing_when_the_callback_raises(backend):
    # Given a persisted document
    backend.create("b", {"batch_id": "b", "version": 1}, ttl=1000)

    # When the mutation callback raises
    def boom(document):
        document["version"] = 99
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        backend.mutate("b", boom, ttl=1000)

    # Then the committed document is untouched
    assert backend.read("b")["version"] == 1
