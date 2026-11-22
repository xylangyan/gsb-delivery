from __future__ import annotations

import pytest
import redis

from dramatiq.batches import Batch, COMPLETED
from dramatiq.batches.backends import RedisBatchStateBackend


@pytest.fixture()
def redis_backend():
    backend = RedisBatchStateBackend()
    try:
        backend.client.ping()
    except redis.ConnectionError:
        pytest.skip("No connection to Redis server.")
    backend.client.flushall()
    yield backend
    backend.client.flushall()
    backend.client.close()


def test_batch_lifecycle_against_redis(redis_backend):
    # Given a batch stored in Redis with the two initial obligations
    batch = Batch.open(
        "nightly",
        backend=redis_backend,
        obligations=[
            {"key": "East1", "site": "East"},
            {"key": "West1", "site": "West"},
        ],
    )

    # When West1 completes and East1 atomically registers the East2 branch
    batch.complete_obligation("West1", range={"pages": [1]})
    batch.submit_page(
        "East1",
        range={"pages": [1]},
        discovered=[{"key": "East2", "site": "East"}],
    )
    # And East2 is cancelled against the current effective version
    batch.cancel_obligation("East2", effective_version=batch.version)

    # Then a fresh handle reads the same committed state and can settle
    reloaded = Batch("nightly", backend=redis_backend)
    assert reloaded.get_obligation("West1")["status"] == COMPLETED
    settlement = reloaded.settle()
    assert reloaded.is_settled

    # And the document persists as a decoded Redis value
    stored = redis_backend.read("nightly")
    assert stored["settlement"]["version"] == settlement["version"]
    assert [r["key"] for r in stored["settlement"]["ranges"]["East"]] == ["East1", "East2"]


def test_redis_compare_and_swap_rejects_stale_versions(redis_backend):
    # Given a document at version 1
    redis_backend.create("b", {"batch_id": "b", "version": 1}, ttl=10_000)

    # Then a stale compare fails and a current compare succeeds
    assert redis_backend.replace_if_version("b", 2, {"batch_id": "b", "version": 2}, ttl=10_000) is False
    assert redis_backend.read("b")["version"] == 1
    assert redis_backend.replace_if_version("b", 1, {"batch_id": "b", "version": 2}, ttl=10_000) is True
    assert redis_backend.read("b")["version"] == 2
