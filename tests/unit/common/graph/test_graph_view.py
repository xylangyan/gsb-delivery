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


import pytest

from neo4j.graph import (
    DatabaseIdentity,
    Graph,
    GraphBatch,
    GraphView,
    Node,
    Path,
    PendingPath,
    Provenance,
)


DB_A = DatabaseIdentity("graph-a", "server-1")
DB_B = DatabaseIdentity("graph-b", "server-1")
BATCH_A1 = GraphBatch(DB_A, "batch-1")
BATCH_B1 = GraphBatch(DB_B, "batch-1")


def _graph(batch):
    return Graph(batch)


def _node(graph, element_id, labels=(), properties=None):
    node = Node(graph, element_id, 0, labels, dict(properties or {}))
    graph._nodes[element_id] = node
    return node


def _relationship(graph, element_id, start, end, type_, properties=None):
    rel = graph.relationship_type(type_)(
        graph, element_id, 0, dict(properties or {})
    )
    rel._start_node = start
    rel._end_node = end
    graph._relationships[element_id] = rel
    return rel


def _broken_path_graph():
    """``e`` actually goes b -> a; the path traverses it a -> b."""
    graph = _graph(BATCH_A1)
    a = _node(graph, "a", {"Person"})
    b = _node(graph, "b", {"Person"})
    e = _relationship(graph, "e", b, a, "E", {"weight": 1})
    path = Path(a, e)
    return graph, a, b, e, path


def test_path_keeps_edge_direction_and_traversal_direction():
    graph, a, b, e, path = _broken_path_graph()

    view = GraphView.from_graph(graph, paths=[path])

    relationship = view.relationship("e")
    assert relationship.start_node is view.node("b")
    assert relationship.end_node is view.node("a")
    delivered = view.paths[0]
    assert delivered.nodes == (view.node("a"), view.node("b"))
    assert delivered.directions == (-1,)
    assert delivered.relationships == (relationship,)


def test_late_data_completes_original_relationship_and_old_result_stays():
    graph, a, b, e, path = _broken_path_graph()
    view = GraphView.from_graph(graph, paths=[path])

    late = _graph(BATCH_A1)
    a2 = _node(late, "a", {"Person"})
    b2 = _node(late, "b", {"Person"})
    _relationship(late, "e", b2, a2, "E", {"weight": 7, "since": 1999})
    newer = view.add_graph(late)

    assert len(newer.relationships_by_element_id("e")) == 1
    completed = newer.relationship("e")
    assert completed.get("weight") == 7
    assert completed.get("since") == 1999
    assert completed.start_node is newer.node("b")
    assert completed.end_node is newer.node("a")
    assert newer.paths[0].directions == (-1,)

    # the earlier view is still exactly what was delivered
    assert view.relationship("e").get("weight") == 1
    assert view.relationship("e").get("since") is None
    assert view.paths[0].directions == (-1,)


def test_unrelated_database_does_not_share_element_cache():
    graph, *_ = _broken_path_graph()
    view = GraphView.from_graph(graph)

    other = _graph(BATCH_B1)
    a2 = _node(other, "a", {"Person"})
    b2 = _node(other, "b", {"Person"})
    _relationship(other, "e", a2, b2, "E", {"weight": 2})
    combined = view.add_graph(other)

    assert len(combined.nodes_by_element_id("a")) == 2
    assert len(combined.relationships_by_element_id("e")) == 2
    a_from_a = combined.node("a", database=DB_A)
    a_from_b = combined.node("a", database=DB_B)
    assert a_from_a is not a_from_b
    assert combined.relationship("e", database=DB_A).start_node is not (
        combined.relationship("e", database=DB_B).start_node
    )
    # earlier view untouched
    assert len(view.nodes_by_element_id("a")) == 1


def test_without_confirmation_equal_ids_stay_independent():
    graph, *_ = _broken_path_graph()
    other = _graph(BATCH_B1)
    a2 = _node(other, "a")
    b2 = _node(other, "b")
    _relationship(other, "e", a2, b2, "E", {"weight": 2})

    view = GraphView.from_graph(graph).add_graph(other)
    with pytest.raises(LookupError):
        view.node("a")
    with pytest.raises(LookupError):
        view.relationship("e")


def test_confirmed_migration_keeps_every_physical_source():
    graph, *_ = _broken_path_graph()
    other = _graph(BATCH_B1)
    a2 = _node(other, "a", {"Person"}, {"age": 42})
    b2 = _node(other, "b")
    _relationship(other, "e", b2, a2, "E", {"weight": 2})

    view = GraphView.from_graph(graph).add_graph(other)
    merged = view.confirm_migration(
        view.node("a", database=DB_A),
        view.node("a", database=DB_B),
    )

    assert len(merged.nodes_by_element_id("a")) == 1
    node = merged.node("a")
    assert node.get("age") == 42
    assert [p.database for p in node.provenance] == [DB_A, DB_B]
    assert [p.batch for p in node.provenance] == ["batch-1", "batch-1"]
    assert merged.migrations == (
        (
            Provenance(DB_A, "batch-1", "a"),
            Provenance(DB_B, "batch-1", "a"),
        ),
    )
    # relationships with the same endpoints stay separate until confirmed
    assert len(merged.relationships_by_element_id("e")) == 2


def test_relationships_with_different_endpoints_stay_independent():
    graph, *_ = _broken_path_graph()
    other = _graph(BATCH_B1)
    a2 = _node(other, "a")
    b2 = _node(other, "b")
    _relationship(other, "e", a2, b2, "E", {"weight": 2})

    view = GraphView.from_graph(graph).add_graph(other)
    with pytest.raises(ValueError):
        view.confirm_migration(*view.relationships_by_element_id("e"))
    assert len(view.relationships_by_element_id("e")) == 2


def test_same_endpoints_can_be_confirmed_as_migration():
    graph, *_ = _broken_path_graph()
    other = _graph(BATCH_B1)
    a2 = _node(other, "a")
    b2 = _node(other, "b")
    _relationship(other, "e", b2, a2, "E", {"weight": 2})

    view = GraphView.from_graph(graph).add_graph(other)
    merged = view.confirm_migration(*view.relationships_by_element_id("e"))

    assert len(merged.relationships_by_element_id("e")) == 1
    relationship = merged.relationship("e")
    assert relationship.get("weight") == 2
    assert [p.database for p in relationship.provenance] == [DB_A, DB_B]
    assert relationship.start_node is merged.node("b", database=DB_A)
    assert relationship.end_node is merged.node("a", database=DB_A)


def test_late_conflicting_direction_makes_related_path_pending():
    graph, a, b, e, path = _broken_path_graph()
    view = GraphView.from_graph(graph, paths=[path])
    assert len(view.paths) == 1

    # the same physical relationship, now said to go a -> b
    late = _graph(BATCH_A1)
    a2 = _node(late, "a")
    b2 = _node(late, "b")
    _relationship(late, "e", a2, b2, "E", {"weight": 9})
    newer = view.add_graph(late)

    assert newer.paths == ()
    assert len(newer.pending_paths) == 1
    pending = newer.pending_paths[0]
    assert isinstance(pending, PendingPath)
    assert pending.path.nodes == (newer.node("a"), newer.node("b"))
    assert pending.path.directions == (-1,)
    assert pending.conflicting_relationships
    assert newer.conflicts

    # the old view still shows the path as delivered
    assert view.paths[0].directions == (-1,)
    assert view.pending_paths == ()


def test_independent_path_is_still_delivered():
    graph, a, b, e, path = _broken_path_graph()
    view = GraphView.from_graph(graph, paths=[path])

    other = _graph(BATCH_B1)
    a2 = _node(other, "a")
    b2 = _node(other, "b")
    c2 = _node(other, "c")
    _relationship(other, "e", a2, b2, "E", {"weight": 9})
    f = _relationship(other, "f", a2, c2, "F")
    other_path = Path(a2, f)

    newer = view.add_graph(other, paths=[other_path])

    assert len(newer.paths) == 2
    assert newer.pending_paths == ()
    assert len(newer.relationships_by_element_id("e")) == 2


def test_repeated_entity_in_one_view_shares_identity():
    graph = _graph(BATCH_A1)
    a = _node(graph, "a", {"Person"})
    b = _node(graph, "b", {"Person"})
    e = _relationship(graph, "e", a, b, "E")
    path = Path(a, e)

    view = GraphView.from_graph(graph, paths=[path])

    assert view.paths[0].start_node is view.node("a")
    assert view.paths[0].relationships[0] is view.relationship("e")
    assert view.paths[0].relationships[0].start_node is view.node("a")
    assert view.paths[0].relationships[0].end_node is view.node("b")


def test_batch_scoped_element_cache():
    graph, *_ = _broken_path_graph()
    view = GraphView.from_graph(graph)

    # same database, different batch: same element ids are different objects
    same_db_new_batch = _graph(GraphBatch(DB_A, "batch-2"))
    a2 = _node(same_db_new_batch, "a")
    b2 = _node(same_db_new_batch, "b")
    _relationship(same_db_new_batch, "e", a2, b2, "E")

    newer = view.add_graph(same_db_new_batch)
    assert len(newer.nodes_by_element_id("a")) == 2
    assert len(newer.relationships_by_element_id("e")) == 2


def test_explicit_response_batch_wins_over_shared_entity_batch():
    graph, *_ = _broken_path_graph()
    view = GraphView.from_graph(graph)

    # an entity object from another source is re-announced in this response
    view.add_result(
        graph,
        database=DB_A,
        batch="batch-3",
    )
    assert len(view.nodes) == 2
