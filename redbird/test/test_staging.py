
from datetime import timedelta
from typing import Optional

import pytest
from pydantic import BaseModel

from redbird.repos import CSVFileRepo, SQLRepo
from redbird.staging import (
    CursorExpiredError,
    CursorNotFoundError,
    StagingQueue,
)


class Draft(BaseModel):
    key: str
    account: str
    priority: int
    payload: Optional[str]


class Verified(BaseModel):
    key: str
    status: str


def make_queue(tmpdir, **kwargs):
    drafts = CSVFileRepo(filename=str(tmpdir / "drafts.csv"), model=Draft)
    verified = SQLRepo(
        model=Verified,
        table="verified",
        conn_string=f"sqlite:///{tmpdir / 'verified.db'}",
        if_missing="create",
        id_field="key",
    )
    return StagingQueue(drafts, verified, control=str(tmpdir / "control.db"), **kwargs)


def make_scenario(tmpdir):
    """Accounts A, B, C (registration order).

    A waits with A1 (high) and A2 (normal), B with B1 (high).
    C1 is revoked in the verified layer and has an old draft,
    C also has C2 (normal).
    """
    queue = make_queue(tmpdir)
    queue.register_batch([
        Draft(key="A1", account="A", priority=10, payload="a1"),
        Draft(key="A2", account="A", priority=0, payload="a2"),
        Draft(key="B1", account="B", priority=10, payload="b1"),
        Draft(key="C1", account="C", priority=0, payload="c1-old"),
        Draft(key="C2", account="C", priority=0, payload="c2"),
    ])
    queue.verified.add(Verified(key="C1", status="revoked"))
    return queue


def keys(page):
    return [item.key for item in page.items]


def test_example_scenario(tmpdir):
    queue = make_scenario(tmpdir)
    assert queue.registered_accounts() == ["A", "B", "C"]

    # First page of two: round-robin A then B, high priority first
    page1 = queue.next_page(2)
    assert keys(page1) == ["A1", "B1"]
    assert not page1.exhausted

    # Sent to manual review: B1 is revoked in the verified layer
    # and a new high-priority B2 arrives for B
    queue.verified.add(Verified(key="B1", status="revoked"))
    queue.register_batch([Draft(key="B2", account="B", priority=10, payload="b2")])

    # The review session continues with the first page's cursor
    page2 = queue.next_page(2, cursor=page1.cursor)
    assert keys(page2) == ["C2", "A2"]

    # C1 never showed up because a revoked record in the verified
    # layer shadows its old draft
    explanation = queue.explain("C1")
    assert not explanation.visible
    assert explanation.reason == "shadowed"
    assert "revoked" in explanation.detail

    # A new session sees the fresh checklist
    fresh = queue.next_page(4)
    assert keys(fresh) == ["A1", "B2", "C2", "A2"]

    tail = queue.next_page(4, cursor=fresh.cursor)
    assert tail.items == []
    assert tail.exhausted


def test_sessions_survive_process_restart(tmpdir):
    queue = make_scenario(tmpdir)
    page1 = queue.next_page(2)
    assert keys(page1) == ["A1", "B1"]

    queue.verified.add(Verified(key="B1", status="revoked"))
    queue.register_batch([Draft(key="B2", account="B", priority=10, payload="b2")])

    # Both session types are resumed from a "restarted" queue
    resumed = make_queue(tmpdir)
    page2 = resumed.next_page(2, cursor=page1.cursor)
    assert keys(page2) == ["C2", "A2"]

    fresh = resumed.next_page(4)
    assert keys(fresh) == ["A1", "B2", "C2", "A2"]


def test_cursor_retention(tmpdir):
    queue = make_scenario(tmpdir)
    page = queue.next_page(2)

    with pytest.raises(CursorNotFoundError):
        queue.next_page(2, cursor="no-such-cursor")

    expiring = make_queue(tmpdir.mkdir("expiring"), cursor_ttl=timedelta(seconds=-1))
    expiring.register_batch([Draft(key="X1", account="X", priority=1, payload="x")])
    stale = expiring.next_page(1)
    with pytest.raises(CursorExpiredError):
        expiring.next_page(1, cursor=stale.cursor)
    assert expiring.prune_cursors() == 1
    assert queue.prune_cursors() == 0


def test_page_turns_do_not_rescan_csv(tmpdir, monkeypatch):
    queue = make_scenario(tmpdir)

    def forbidden(self):
        raise AssertionError("CSV file was rescanned")

    monkeypatch.setattr(CSVFileRepo, "read_file", forbidden)
    page1 = queue.next_page(2)
    assert keys(page1) == ["A1", "B1"]
    page2 = queue.next_page(2, cursor=page1.cursor)
    assert keys(page2) == ["C2", "A2"]
    assert queue.explain("C1").reason == "shadowed"


def test_verified_record_shadows_draft(tmpdir):
    queue = make_queue(tmpdir)
    queue.register_batch([
        Draft(key="A1", account="A", priority=10, payload="a1"),
        Draft(key="A2", account="A", priority=0, payload="a2"),
    ])
    queue.verified.add(Verified(key="A1", status="verified"))

    page = queue.next_page(5)
    assert keys(page) == ["A2"]
    assert page.exhausted

    explanation = queue.explain("A1")
    assert not explanation.visible
    assert explanation.reason == "shadowed"
    assert "revoked" not in explanation.detail


def test_empty_account_does_not_consume_turn(tmpdir):
    queue = make_queue(tmpdir)
    queue.register_account("A")
    queue.register_account("B")
    queue.register_batch([Draft(key="B1", account="B", priority=1, payload="b1")])
    queue.register_batch([Draft(key="A1", account="A", priority=1, payload="a1")])

    # A is first in registration order but had nothing when B1 was
    # registered; both rows still come out in account order
    page = queue.next_page(5)
    assert keys(page) == ["A1", "B1"]


def test_explain_visible_and_missing(tmpdir):
    queue = make_scenario(tmpdir)
    explanation = queue.explain("A2")
    assert explanation.visible
    assert explanation.reason == "visible"
    assert "'A'" in explanation.detail

    explanation = queue.explain("ZZ")
    assert not explanation.visible
    assert explanation.reason == "missing"


def test_layers_keep_explicit_backend_filtering(tmpdir):
    queue = make_scenario(tmpdir)
    queue.next_page(2)

    # The existing access pattern of the repositories is untouched
    drafts_a = queue.drafts.filter_by(account="A").all()
    assert [item.key for item in drafts_a] == ["A1", "A2"]
    revoked = queue.verified.filter_by(status="revoked").all()
    assert [item.key for item in revoked] == ["C1"]
