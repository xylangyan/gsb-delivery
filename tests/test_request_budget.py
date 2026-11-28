"""Deterministic shared-budget tests.

The clock in this module never advances on its own, so every assertion below is
about the budget state machine rather than wall-clock timing. No test contacts
the real arXiv service.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
import pytest
from requests import Response

import arxiv


ORIGIN = "export.arxiv.org"
OTHER = "other.example.org"


def budget_at(
    path: Path,
    clock: arxiv.Clock,
    *,
    poll_interval: float = 0.05,
    min_interval: float = 3.0,
) -> arxiv.RequestBudget:
    return arxiv.RequestBudget(
        path, clock=clock, poll_interval=poll_interval, min_interval=min_interval
    )


def at(clock: arxiv.ManualClock, t: float) -> None:
    clock.set_time(t)


class TestRetryAfterTimeline:
    def test_a_response_at_4_defers_b_and_restart_to_20(self, tmp_path: Path) -> None:
        db = tmp_path / "budget.sqlite3"
        clock = arxiv.ManualClock(0)

        # A is granted at t=0 and actually sends.
        a_budget = budget_at(db, clock)
        a = a_budget.acquire(ORIGIN, job_id="A", page_key="start=0")
        assert a.granted_at == 0
        a.mark_sent()
        assert a_budget.state(ORIGIN).in_flight_id == a.request_id

        # B has waited its own client-side delay and asks at t=3. A is still
        # in flight, so B may not send even though three seconds elapsed.
        at(clock, 3)
        b_budget = budget_at(db, clock, poll_interval=0.01)
        assert b_budget.can_acquire(ORIGIN) is False
        with pytest.raises(arxiv.BudgetTimeout):
            b_budget.acquire(
                ORIGIN,
                job_id="B",
                page_key="start=10",
                wait=False,
            )

        # A reads its response at t=4; the origin must stay paused until 20.
        at(clock, 4)
        a.complete(status_code=200, retry_after=16)
        assert a_budget.state(ORIGIN).in_flight_id is None
        assert a_budget.state(ORIGIN).not_before == pytest.approx(20)
        assert a_budget.next_allowed(ORIGIN) == pytest.approx(20)

        # B is still waiting. At 19 it cancels that page. A cancelled wait is
        # not an upstream request and must not consume a slot.
        at(clock, 19)
        token = arxiv.CancellationToken()
        token.cancel()
        with pytest.raises(arxiv.RequestCancelled):
            b_budget.acquire(
                ORIGIN,
                job_id="B",
                page_key="start=10",
                token=token,
            )
        cancelled = b_budget.records(job_id="B", status="cancelled")
        assert len(cancelled) == 1
        assert cancelled[0].sent_at is None
        assert cancelled[0].granted_at is None
        # Only A's real request reached upstream in this timeline.
        assert [r.job_id for r in b_budget.records(sent_only=True)] == ["A"]

        # The daemon restarts: a fresh budget object on the same database.
        at(clock, 19.999)
        restarted = budget_at(db, clock, poll_interval=0.01)
        assert restarted.can_acquire(ORIGIN) is False
        with pytest.raises(arxiv.BudgetTimeout):
            restarted.acquire(ORIGIN, job_id="restarted", wait=False)

        # At exactly the persisted boundary the new task may send one request.
        at(clock, 20)
        c = restarted.acquire(ORIGIN, job_id="restarted", page_key="start=20")
        assert c.granted_at == pytest.approx(20)
        c.mark_sent()
        c.complete(status_code=200, result_ids=["one"])
        report = restarted.budget_report() if hasattr(restarted, "budget_report") else None
        assert report is None  # RequestBudget itself has no client report
        fetched = restarted.records(job_id="restarted", fetched_only=True)
        assert [r.result_ids for r in fetched] == [("one",)]

    def test_at_most_one_sent_request_and_three_second_gap(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        first = b.acquire(ORIGIN, job_id="one")
        first.mark_sent()
        # A competing acquisition cannot even be granted while it is in flight.
        assert b.try_acquire(ORIGIN, job_id="two") is None

        at(clock, 1)
        first.complete(status_code=200)
        # The request ended at 1; the next send may happen at 4, not 3.999.
        at(clock, 3.999)
        assert b.try_acquire(ORIGIN, job_id="two") is None
        at(clock, 4)
        second = b.acquire(ORIGIN, job_id="two")
        assert second.granted_at == pytest.approx(4)
        second.mark_sent()
        second.complete(status_code=200)

    def test_other_origin_is_not_blocked(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        a = b.acquire(ORIGIN, job_id="A")
        a.mark_sent()
        a.complete(status_code=200, retry_after=100)

        at(clock, 1)
        other = b.acquire(OTHER, job_id="other")
        assert other.granted_at == pytest.approx(1)
        other.mark_sent()
        other.complete(status_code=200)


class TestLateCallbacks:
    def test_stale_callback_cannot_shorten_a_later_pause(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        a = b.acquire(ORIGIN, job_id="A")
        a.mark_sent()
        at(clock, 4)
        a.complete(status_code=503, retry_after=16)  # pause to 20

        at(clock, 20)
        n = b.acquire(ORIGIN, job_id="new")
        n.mark_sent()
        at(clock, 21)
        n.complete(status_code=503, retry_after=29)  # pause to 50
        assert b.state(ORIGIN).not_before == pytest.approx(50)

        # A's parser/retry scheduler delivers an old callback that only knew
        # about the earlier 20-second boundary. It must not shorten 50.
        at(clock, 22)
        a.complete(status_code=503, retry_after=16)
        assert b.state(ORIGIN).not_before == pytest.approx(50)
        assert b.can_acquire(ORIGIN) is False

    def test_stale_callback_cannot_clear_a_new_in_flight_request(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        a = b.acquire(ORIGIN, job_id="A")
        a.mark_sent()
        at(clock, 4)
        a.complete(status_code=200, retry_after=6)  # pause to 10

        at(clock, 10)
        n = b.acquire(ORIGIN, job_id="new")
        n.mark_sent()
        assert b.state(ORIGIN).in_flight_id == n.request_id

        at(clock, 11)
        a.complete(status_code=200, retry_after=0)
        state = b.state(ORIGIN)
        assert state.in_flight_id == n.request_id
        assert state.not_before == pytest.approx(10)

    def test_late_retry_after_can_only_extend(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        a = b.acquire(ORIGIN, job_id="A")
        a.mark_sent()
        at(clock, 1)
        a.complete(status_code=503, retry_after=5)  # pause to 6
        at(clock, 2)
        a.complete(status_code=503, retry_after=30)  # late, but later -> 32
        assert b.state(ORIGIN).not_before == pytest.approx(32)


class TestCrashAndUnknownInFlight:
    def test_sent_unconfirmed_request_survives_restart_and_blocks(self, tmp_path: Path) -> None:
        db = tmp_path / "b.sqlite3"
        clock = arxiv.ManualClock(0)
        first = budget_at(db, clock)
        lease = first.acquire(ORIGIN, job_id="crashed")
        lease.mark_sent()
        # Simulate process death: no complete(), no release().
        first.close()

        restarted = budget_at(db, clock, poll_interval=0.01)
        at(clock, 10_000)
        assert restarted.in_flight(ORIGIN)[0].job_id == "crashed"
        # A long timeout is not a credential and acquires nothing.
        with pytest.raises(arxiv.BudgetTimeout):
            restarted.acquire(ORIGIN, job_id="later", wait=False)
        with pytest.raises(arxiv.UnknownInFlightError):
            restarted.resolve_unknown(ORIGIN)

        resolved = restarted.resolve_unknown(ORIGIN, manual_record="operator saw request finish")
        assert resolved.status == "resolved"
        assert restarted.can_acquire(ORIGIN) is False
        at(clock, 10_003)
        assert restarted.can_acquire(ORIGIN) is True

    def test_upstream_credential_resolves_unknown_request(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        lease = b.acquire(ORIGIN, job_id="A")
        lease.mark_sent()
        lease.mark_uncertain("connection reset")
        assert [r.status for r in b.in_flight(ORIGIN)] == ["uncertain"]

        b.resolve_unknown(
            ORIGIN,
            request_id=lease.request_id,
            credential="receipt:req-123",
            completed_at=5,
        )
        at(clock, 8)
        assert b.can_acquire(ORIGIN) is True
        done = b.request(lease.request_id)
        assert done is not None and done.status == "resolved"

    def test_timeout_while_another_is_in_flight_does_not_grant(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0, auto_advance=True)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        lease = b.acquire(ORIGIN, job_id="A")
        lease.mark_sent()
        at(clock, 100)
        with pytest.raises(arxiv.BudgetTimeout):
            b.acquire(ORIGIN, job_id="B", timeout=0.001)
        # The timeout recorded a non-upstream attempt only.
        attempts = b.records(job_id="B")
        assert attempts[0].status == "timed_out"
        assert attempts[0].sent_at is None
        assert b.state(ORIGIN).in_flight_id == lease.request_id

    def test_unsent_grant_can_be_reclaimed_but_sent_never_is(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        abandoned = b.acquire(ORIGIN, job_id="abandoned")
        # A grant that never called mark_sent provably did not reach upstream.
        at(clock, b.grant_timeout + 1)
        assert b.can_acquire(ORIGIN) is True
        expiry = b.acquire(ORIGIN, job_id="next")
        abandoned_record = b.request(abandoned.request_id)
        assert abandoned_record is not None and abandoned_record.status == "cancelled"
        expiry.mark_sent()
        at(clock, b.grant_timeout + 1_000)
        # Once sent, elapsed time never reclaims the slot.
        assert b.can_acquire(ORIGIN) is False
        assert b.try_acquire(ORIGIN, job_id="blocked") is None

    def test_cancel_returns_waiting_attempt_count(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        lease = b.acquire(ORIGIN, job_id="holder")
        lease.mark_sent()

        for job in ("waiter-1", "waiter-2"):
            with pytest.raises(arxiv.BudgetTimeout):
                b.acquire(ORIGIN, job_id=job, wait=False)
                raise AssertionError("unreachable")
        assert b.cancel() == 0  # nonzero count only for active waiting rows
        assert b.records(job_id="holder", sent_only=True)[0].sent_at == 0


class TestAuditQueries:
    def test_job_credentials_and_fetched_records_are_separate(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        a = b.acquire(ORIGIN, job_id="job-A", page_key="start=0", url="u0")
        a.mark_sent()
        a.complete(status_code=200, result_ids=["a1", "a2"])
        at(clock, 3)
        retry = b.acquire(ORIGIN, job_id="job-A", page_key="start=10", url="u10")
        retry.mark_sent()
        retry.complete(status_code=500, result_ids=[])
        at(clock, 6)
        last = b.acquire(ORIGIN, job_id="job-A", page_key="start=10", url="u10b")
        last.mark_sent()
        last.complete(status_code=200, result_ids=["a3"])

        granted = b.used_credentials(job_id="job-A")
        assert [r.request_id for r in granted] == [
            a.request_id,
            retry.request_id,
            last.request_id,
        ]
        assert all(r.granted_at is not None for r in granted)

        fetched = b.records(job_id="job-A", fetched_only=True)
        assert [r.result_ids for r in fetched] == [("a1", "a2"), ("a3",)]
        assert [
            r.status_code for r in b.records(job_id="job-A", status="completed", sent_only=True)
        ] == [
            200,
            500,
            200,
        ]
        assert b.jobs() == ["job-A"]


class TestClientIntegration:
    def make_client(
        self,
        clock: arxiv.ManualClock,
        budget: arxiv.RequestBudget,
        job_id: str,
        responses: list[Response] | None = None,
    ) -> tuple[arxiv.Client, list[tuple[float, str]]]:
        client = arxiv.Client(budget=budget, job_id=job_id, page_size=2)
        calls: list[tuple[float, str]] = []
        pending = list(responses or [])

        def fake_get(url: str, **_kwargs: object) -> Response:
            calls.append((clock.time(), url))
            return pending.pop(0)

        client._session.get = fake_get  # type: ignore[method-assign]
        return client, calls

    def response(
        self,
        body: bytes,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> Response:
        r = arxiv.requests.Response()
        r.status_code = status
        r._content = body
        r.headers.update(headers or {})
        return r

    def feed(self, ids: list[str]) -> bytes:
        entries = "".join(
            f"<entry><id>http://arxiv.org/abs/{i}</id>"
            f"<updated>2025-01-02T03:04:05Z</updated>"
            f"<published>2025-01-01T03:04:05Z</published>"
            f"<title>{i}</title><summary>s</summary>"
            f"<author><name>A</name></author>"
            f"<category term='cs.AI'/>"
            f"<arxiv:primary_category term='cs.AI'/>"
            f"</entry>"
            for i in ids
        )
        return (
            "<feed xmlns='http://www.w3.org/2005/Atom' "
            "xmlns:arxiv='http://arxiv.org/schemas/atom' "
            "xmlns:opensearch='http://a9.com/-/spec/opensearch/1.1/'>"
            f"<opensearch:totalResults>4</opensearch:totalResults>"
            f"{entries}</feed>"
        ).encode()

    def test_pagination_uses_one_shared_lease_per_page(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0, auto_advance=True)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        client, calls = self.make_client(
            clock,
            b,
            "job-pages",
            [
                self.response(self.feed(["1", "2"])),
                self.response(self.feed(["3", "4"])),
            ],
        )
        results = list(client.results(arxiv.Search(query="testing", max_results=4)))
        assert [r.entry_id for r in results] == [
            "http://arxiv.org/abs/1",
            "http://arxiv.org/abs/2",
            "http://arxiv.org/abs/3",
            "http://arxiv.org/abs/4",
        ]
        assert [t for t, _ in calls] == [0.0, 3.0]
        report = client.budget_report()
        assert report["job_id"] == "job-pages"
        assert len(report["granted"]) == 2
        assert report["fetched_ids"] == [
            "http://arxiv.org/abs/1",
            "http://arxiv.org/abs/2",
            "http://arxiv.org/abs/3",
            "http://arxiv.org/abs/4",
        ]

    def test_retry_honors_retry_after_across_jobs(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0, auto_advance=True)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        client, calls = self.make_client(
            clock,
            b,
            "job-retry",
            [
                self.response(b"", status=503, headers={"Retry-After": "10"}),
                self.response(self.feed(["1", "2"])),
            ],
        )
        client.num_retries = 1
        results = list(client.results(arxiv.Search(query="testing", max_results=2)))
        assert len(results) == 2
        # First at 0; Retry-After 10 wins over the 3s minimum.
        assert [t for t, _ in calls] == [0.0, 10.0]
        records = b.records(job_id="job-retry")
        assert [r.status for r in records] == ["failed", "completed"]
        assert records[0].status_code == 503

    def test_cancelled_wait_never_reaches_upstream(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(0)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        a = b.acquire(ORIGIN, job_id="A")
        a.mark_sent()
        client, calls = self.make_client(clock, b, "job-cancel")
        token = arxiv.CancellationToken()

        def cancel_later() -> None:
            time.sleep(0.05)
            token.cancel()
            clock.wake()

        thread = threading.Thread(target=cancel_later)
        thread.start()
        with pytest.raises(arxiv.RequestCancelled):
            list(client.results(arxiv.Search(query="testing", max_results=2), token=token))
        thread.join(timeout=2)
        assert calls == []
        assert b.records(job_id="job-cancel", status="cancelled")[0].sent_at is None

    def test_two_clients_in_separate_budgets_share_one_slot(self, tmp_path: Path) -> None:
        db = tmp_path / "b.sqlite3"
        clock = arxiv.ManualClock(0)
        a_budget = budget_at(db, clock, poll_interval=0.01)
        b_budget = budget_at(db, clock, poll_interval=0.01)
        a = a_budget.acquire(ORIGIN, job_id="job-A", page_key="start=0")
        a.mark_sent()

        b, b_calls = self.make_client(
            clock, b_budget, "job-B", [self.response(self.feed(["3", "4"]))]
        )

        def run_b() -> list[arxiv.Result]:
            return list(b.results(arxiv.Search(query="testing", max_results=2)))

        waiter = threading.Thread(target=run_b)
        waiter.start()
        # While A's request is in flight, B has no slot even though its own
        # client delay would have elapsed.
        time.sleep(0.05)
        assert b_calls == []

        # A ends at t=1; B still waits for the shared 3s interval.
        at(clock, 1)
        a.complete(status_code=200)
        at(clock, 2)
        time.sleep(0.05)
        assert b_calls == []

        # At 4 (one full interval after A ended) B may send.
        at(clock, 4)
        waiter.join(timeout=2)
        assert waiter.is_alive() is False
        assert [t for t, _ in b_calls] == [4.0]
        assert [r.job_id for r in b_budget.used_credentials()] == ["job-A", "job-B"]

    def test_unknown_in_flight_blocks_client_until_credential(self, tmp_path: Path) -> None:
        db = tmp_path / "b.sqlite3"
        clock = arxiv.ManualClock(0)
        first = budget_at(db, clock)
        lease = first.acquire(ORIGIN, job_id="crashed")
        lease.mark_sent()
        first.close()

        restarted = budget_at(db, clock, poll_interval=0.01)
        client, calls = self.make_client(
            clock,
            restarted,
            "job-after-crash",
            [self.response(self.feed(["1", "2"]))],
        )

        def run() -> list[arxiv.Result]:
            return list(client.results(arxiv.Search(query="testing", max_results=2)))

        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.05)
        # Time alone never frees the slot, so no request has gone out.
        at(clock, 10_000)
        time.sleep(0.05)
        assert calls == []
        assert [r.job_id for r in restarted.in_flight(ORIGIN)] == ["crashed"]

        restarted.resolve_unknown(
            ORIGIN,
            request_id=lease.request_id,
            credential="receipt:req-123",
            completed_at=10_001,
        )
        at(clock, 10_004)
        thread.join(timeout=2)
        assert thread.is_alive() is False
        assert [t for t, _ in calls] == [10_004]


class TestParsingHelpers:
    def test_pause_only_extends(self, tmp_path: Path) -> None:
        clock = arxiv.ManualClock(10)
        b = budget_at(tmp_path / "b.sqlite3", clock)
        assert b.pause(ORIGIN, retry_after=10) == 20
        at(clock, 15)
        assert b.pause(ORIGIN, until=12) == 20
        assert b.pause(ORIGIN, until=30) == 30
        assert b.pause(ORIGIN, retry_after=0) == 30

    def test_retry_after_http_date(self) -> None:
        # 2026-11-28T00:00:00Z == 1795824000
        assert arxiv.parse_retry_after(
            "Sat, 28 Nov 2026 00:00:00 GMT", now=1795823990
        ) == pytest.approx(1795824000)
        assert arxiv.parse_retry_after("12", now=100) == pytest.approx(112)
        assert arxiv.parse_retry_after(None, now=100) is None

    def test_header_picks_latest_boundary(self) -> None:
        headers = {"Retry-After": "5", "X-RateLimit-Reset": "120"}
        assert arxiv.retry_after_from_headers(headers, now=100) == pytest.approx(120)
        # X-RateLimit-Reset is absolute, not a relative offset.
        assert arxiv.retry_after_from_headers({"X-RateLimit-Reset": "20"}, now=100) == 100

    def test_origin_key_accepts_urls_and_labels(self) -> None:
        assert arxiv.origin_key("https://export.arxiv.org/api/query?x=1") == ORIGIN
        assert arxiv.origin_key(" Export.ArXiv.org/ ") == ORIGIN
