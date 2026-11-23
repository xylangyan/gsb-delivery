# Copyright (c) "Neo4j"
# Neo4j Sweden AB [https://neo4j.com]
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import annotations

import pytest

from neo4j import Session
from neo4j._conf import SessionConfig
from neo4j._sync.home_db_cache import HomeDbCache
from neo4j._sync.io import (
    AcquisitionDatabase,
    RoutedBoltPool,
)
from neo4j.api import READ_ACCESS
from neo4j.exceptions import Neo4jError

from ...._async_compat import mark_sync_test


# The queries of the clean-up script under test: it writes the suppliers
# first and reads the related list afterwards.
ALIAS = "supplier_alias"
WRITE_QUERY = "CREATE (:Supplier {id: 1})"
READ_QUERY = "MATCH (s:Supplier)-[:SUPPLIES]->(p) RETURN p"
UNRELATED_QUERY = "MATCH (n:Unrelated) RETURN n"

U1 = "u1:7687"
U2 = "u2:7687"


def _routed_pool(fake_pool):
    fake_pool.mock_add_spec(RoutedBoltPool)
    fake_pool.is_direct_pool = False
    return fake_pool


def _session_config(**kwargs):
    config = SessionConfig()
    config.database = ALIAS
    for key, value in kwargs.items():
        setattr(config, key, value)
    return config


def _write_script(bookmark):
    return [
        ("run", {"on_success": ({"fields": []},), "on_summary": None}),
        (
            "pull",
            {
                "on_success": (
                    {"bookmark": bookmark, "has_more": False},
                ),
                "on_summary": None,
            },
        ),
    ]


def _read_script(records, bookmark):
    return [
        (
            "run",
            {"on_success": ({"fields": ["p"]},), "on_summary": None},
        ),
        (
            "pull",
            {
                "on_records": (records,),
                "on_success": (
                    {"bookmark": bookmark, "has_more": False},
                ),
                "on_summary": None,
            },
        ),
    ]


def _make_failure(neo4j_code, message):
    return {
        "neo4j_code": neo4j_code,
        "gql_status": "50N42",
        "message": message,
        "description": message,
        "diagnostic_record": {},
    }


def _bookmark_not_migrated_script():
    # U2 cannot (yet) prove that the bookmark m1 has been migrated: it
    # rejects the read and tells the caller to wait on the original
    # database.
    failure = _make_failure(
        "Neo.ClientError.Transaction.InvalidBookmark",
        "bookmark has not been migrated to this database yet; "
        "wait on the original database",
    )
    return [
        ("run", {"on_failure": (failure,), "on_summary": None}),
        ("pull", {"on_ignored": None, "on_summary": None}),
        ("reset", {"on_success": None, "on_summary": None}),
    ]


def _scripted_connection(generator, address, script):
    connection = generator()
    connection.unresolved_address = address
    connection.set_script(script)
    return connection


def _acquired_addresses(pool):
    return [
        connection.unresolved_address
        for connection in pool.acquired_connection_mocks
    ]


@mark_sync_test
def test_read_sees_own_write_after_alias_is_rerouted(
    fake_pool, scripted_connection_generator
):
    pool = _routed_pool(fake_pool)

    # An independent read has queried the same-named node on U2 earlier.
    early_reader = _scripted_connection(
        scripted_connection_generator,
        U2,
        _read_script([["stale-p"]], "u2:bm0"),
    )

    # The write commits on U1 and yields the bookmark m1.
    writer = _scripted_connection(
        scripted_connection_generator, U1, _write_script("u1:m1")
    )

    # The cached route towards U1 expires; the re-pointed alias now
    # resolves to U2.
    rerouted_reader = _scripted_connection(
        scripted_connection_generator,
        U2,
        _read_script([["p1"], ["p2"]], "u2:m2"),
    )

    # acquisitions happen in reverse buffering order
    pool.buffered_connection_mocks.extend(
        [rerouted_reader, writer, early_reader]
    )

    with Session(
        pool, _session_config(default_access_mode=READ_ACCESS)
    ) as session:
        result = session.run(READ_QUERY)
        assert result.values() == [["stale-p"]]

    with Session(pool, _session_config()) as session:
        write_result = session.run(WRITE_QUERY)
        write_result.consume()
        assert (session.last_bookmarks()).raw_values == {"u1:m1"}

        read_result = session.run(READ_QUERY)
        assert read_result.values() == [["p1"], ["p2"]]
        assert (session.last_bookmarks()).raw_values == {"u2:m2"}

    # the actual routing calls: every acquisition targeted the alias
    assert _acquired_addresses(pool) == [U2, U1, U2]
    assert pool.acquire.call_count == 3
    for acquire_call in pool.acquire.call_args_list:
        assert acquire_call.kwargs["database"] == AcquisitionDatabase(ALIAS)
    # the read must carry the bookmark of the write committed on U1 so
    # that it can see the write even though the alias now points to U2
    assert rerouted_reader.run.call_args.args[0] == READ_QUERY
    assert rerouted_reader.run.call_args.kwargs["bookmarks"] == ("u1:m1",)


@mark_sync_test
def test_read_waits_for_bookmark_migration_proof(
    fake_pool, scripted_connection_generator
):
    pool = _routed_pool(fake_pool)

    writer = _scripted_connection(
        scripted_connection_generator, U1, _write_script("u1:m1")
    )

    # U2 cannot prove the migration of m1 yet.
    unready_reader = _scripted_connection(
        scripted_connection_generator,
        U2,
        _bookmark_not_migrated_script(),
    )

    # The migration proof has been completed: U2 now accepts m1 and
    # answers with the data corresponding to the new bookmark.
    ready_reader = _scripted_connection(
        scripted_connection_generator,
        U2,
        _read_script([["p1"]], "u2:m2"),
    )

    pool.buffered_connection_mocks.extend(
        [ready_reader, unready_reader, writer]
    )

    with Session(pool, _session_config()) as session:
        write_result = session.run(WRITE_QUERY)
        write_result.consume()

        with pytest.raises(Neo4jError) as exc_info:
            session.run(READ_QUERY)
        assert (
            exc_info.value.code
            == "Neo.ClientError.Transaction.InvalidBookmark"
        )
        # the failed read must not hand out any bookmarks
        assert (session.last_bookmarks()).raw_values == {"u1:m1"}

        read_result = session.run(READ_QUERY)
        assert read_result.values() == [["p1"]]
        assert (session.last_bookmarks()).raw_values == {"u2:m2"}

    assert _acquired_addresses(pool) == [U1, U2, U2]
    # the retried read still presents the bookmark obtained on U1
    assert ready_reader.run.call_args.kwargs["bookmarks"] == ("u1:m1",)


@mark_sync_test
def test_unrelated_queries_are_unaffected_by_pending_migration(
    fake_pool, scripted_connection_generator
):
    pool = _routed_pool(fake_pool)

    writer = _scripted_connection(
        scripted_connection_generator, U1, _write_script("u1:m1")
    )

    blocked_reader = _scripted_connection(
        scripted_connection_generator,
        U2,
        _bookmark_not_migrated_script(),
    )

    unrelated_reader = _scripted_connection(
        scripted_connection_generator,
        U2,
        _read_script([["n1"]], "u2:bm1"),
    )

    pool.buffered_connection_mocks.extend(
        [unrelated_reader, blocked_reader, writer]
    )

    with Session(pool, _session_config()) as session:
        write_result = session.run(WRITE_QUERY)
        write_result.consume()
        with pytest.raises(Neo4jError):
            session.run(READ_QUERY)

    # a query of the same batch that does not touch the suppliers goes
    # through as usual: it neither needs nor sends the bookmark chain
    with Session(
        pool, _session_config(default_access_mode=READ_ACCESS)
    ) as other_session:
        result = other_session.run(UNRELATED_QUERY)
        assert result.values() == [["n1"]]

    assert unrelated_reader.run.call_args.args[0] == UNRELATED_QUERY
    assert unrelated_reader.run.call_args.kwargs["bookmarks"] == ()


@mark_sync_test
def test_interleaved_transactions_keep_their_starting_bookmarks(
    fake_pool, scripted_connection_generator
):
    pool = _routed_pool(fake_pool)

    # transaction started before the write commits
    early_tx_connection = _scripted_connection(
        scripted_connection_generator,
        U1,
        [
            ("begin", {"on_success": None, "on_summary": None}),
            (
                "run",
                {"on_success": ({"fields": ["n"]},), "on_summary": None},
            ),
            (
                "pull",
                {
                    "on_records": ([["n1"]],),
                    "on_success": ({"has_more": False},),
                    "on_summary": None,
                },
            ),
            ("commit", {"on_success": None, "on_summary": None}),
        ]
    )

    writer = _scripted_connection(
        scripted_connection_generator, U1, _write_script("u1:m1")
    )

    # transaction started after the write commits (on the re-pointed
    # alias, i.e., towards U2)
    late_tx_connection = _scripted_connection(
        scripted_connection_generator,
        U2,
        [
            ("begin", {"on_success": None, "on_summary": None}),
            (
                "run",
                {"on_success": ({"fields": ["p"]},), "on_summary": None},
            ),
            (
                "pull",
                {
                    "on_records": ([["p1"]],),
                    "on_success": ({"has_more": False},),
                    "on_summary": None,
                },
            ),
            (
                "commit",
                {
                    "on_success": ({"bookmark": "u2:m2"},),
                    "on_summary": None,
                },
            ),
        ]
    )

    pool.buffered_connection_mocks.extend(
        [late_tx_connection, writer, early_tx_connection]
    )

    with Session(pool, _session_config()) as session_a:
        with Session(pool, _session_config()) as session_b:
            tx_a = session_a.begin_transaction()

            write_result = session_b.run(WRITE_QUERY)
            write_result.consume()

            tx_b = session_b.begin_transaction()

            assert (tx_a.run(UNRELATED_QUERY)).values() == [
                ["n1"]
            ]
            assert (tx_b.run(READ_QUERY)).values() == [["p1"]]

            tx_a.commit()
            tx_b.commit()

    # each transaction behaves according to its actual starting point:
    # the interleaved one began without m1, the later one with it
    assert early_tx_connection.begin.call_args.kwargs["bookmarks"] == ()
    assert late_tx_connection.begin.call_args.kwargs["bookmarks"] == (
        "u1:m1",
    )
    assert _acquired_addresses(pool) == [U1, U1, U2]


@mark_sync_test
def test_retry_after_transient_failure_uses_current_bookmarks(
    fake_pool, scripted_connection_generator
):
    pool = _routed_pool(fake_pool)

    writer = _scripted_connection(
        scripted_connection_generator, U1, _write_script("u1:m1")
    )

    # the first attempt of the read hits a transient failure on U2
    flaky_reader = _scripted_connection(
        scripted_connection_generator,
        U2,
        [
            ("begin", {"on_success": None, "on_summary": None}),
            (
                "run",
                {
                    "on_failure": (
                        _make_failure(
                            "Neo.TransientError.General.DatabaseUnavailable",
                            "database temporarily unavailable",
                        ),
                    ),
                    "on_summary": None,
                },
            ),
            ("pull", {"on_ignored": None, "on_summary": None}),
            ("rollback", {"on_success": None, "on_summary": None}),
            ("reset", {"on_success": None, "on_summary": None}),
        ]
    )

    # the retry may come over a different connection
    retry_reader = _scripted_connection(
        scripted_connection_generator,
        U2,
        [
            ("begin", {"on_success": None, "on_summary": None}),
            (
                "run",
                {"on_success": ({"fields": ["p"]},), "on_summary": None},
            ),
            (
                "pull",
                {
                    "on_records": ([["p1"]],),
                    "on_success": ({"has_more": False},),
                    "on_summary": None,
                },
            ),
            (
                "commit",
                {
                    "on_success": ({"bookmark": "u2:m2"},),
                    "on_summary": None,
                },
            ),
        ]
    )

    pool.buffered_connection_mocks.extend(
        [retry_reader, flaky_reader, writer]
    )

    config = _session_config()
    config.initial_retry_delay = 0
    with Session(pool, config) as session:
        write_result = session.run(WRITE_QUERY)
        write_result.consume()

        def work(tx):
            result = tx.run(READ_QUERY)
            return result.values()

        assert session.execute_read(work) == [["p1"]]
        assert (session.last_bookmarks()).raw_values == {"u2:m2"}

    # both the failed attempt and the retry start from the bookmark
    # that was current when the read was issued
    assert flaky_reader.begin.call_args.kwargs["bookmarks"] == ("u1:m1",)
    assert retry_reader.begin.call_args.kwargs["bookmarks"] == ("u1:m1",)
    assert _acquired_addresses(pool) == [U1, U2, U2]


@mark_sync_test
def test_default_database_uses_home_db_cache_per_resolution(
    fake_pool, scripted_connection_generator, mocker
):
    pool = _routed_pool(fake_pool)
    pool.ssr_enabled = True

    def update_routing_table_side_effect(**kwargs):
        # routing towards the alias resolves the actual database
        database_callback = kwargs["database_callback"]
        if database_callback is not None:
            database_callback("u1-home-db")

    pool.update_routing_table.side_effect = (
        update_routing_table_side_effect
    )

    cache_spy = mocker.Mock(spec=HomeDbCache, wraps=HomeDbCache())
    pool.home_db_cache = cache_spy

    writer = _scripted_connection(
        scripted_connection_generator, U1, _write_script("u1:m1")
    )

    reader = _scripted_connection(
        scripted_connection_generator,
        U2,
        _read_script([["p1"]], "u2:m2"),
    )

    pool.buffered_connection_mocks.extend([reader, writer])

    # no explicit database: the default database of the session's user
    with Session(pool, SessionConfig()) as session:
        write_result = session.run(WRITE_QUERY)
        write_result.consume()
        bookmarks = session.last_bookmarks()
        assert bookmarks.raw_values == {"u1:m1"}

    # the follow-up read is causally chained to the write
    with Session(
        pool,
        SessionConfig(
            default_access_mode=READ_ACCESS, bookmarks=bookmarks
        ),
    ) as session:
        read_result = session.run(READ_QUERY)
        assert read_result.values() == [["p1"]]

    # the first session resolved the home database and cached it
    assert cache_spy.set.call_count == 1
    assert cache_spy.set.call_args.args[1] == "u1-home-db"
    # the second session routed towards the cached default database
    assert pool.acquire.call_args.kwargs["database"] == AcquisitionDatabase(
        "u1-home-db", guessed=True
    )
    # ... while still carrying the bookmark chain of the write
    assert reader.run.call_args.kwargs["bookmarks"] == ("u1:m1",)
