"""Tests for the enhanced graph and label-sequence queries."""

from deps_redaction.graph import Edge, EnhancedGraph
from deps_redaction.model import Query


def test_exact_length_label_path():
    graph = EnhancedGraph(
        [
            Edge("1", "2", "a"),
            Edge("2", "3", "b"),
            Edge("3", "4", "c"),
        ]
    )
    assert graph.query_yes("1", "4", ["a", "b", "c"])
    assert graph.query_yes("1", "3", ["a", "b"])
    assert not graph.query_yes("1", "4", ["a", "b"])
    assert not graph.query_yes("1", "3", ["a", "c"])


def test_parallel_labels_and_cycles():
    graph = EnhancedGraph(
        [
            Edge("1", "2", "a"),
            Edge("1", "2", "b"),
            Edge("2", "1", "a"),
        ]
    )
    assert graph.query_yes("1", "1", ["a", "a"])
    assert graph.query_yes("1", "2", ["b"])
    assert not graph.query_yes("1", "2", ["c"])


def test_answers_mapping():
    graph = EnhancedGraph([Edge("1", "2", "x")])
    queries = [
        Query("hit", "1", "2", ("x",)),
        Query("miss", "2", "1", ("x",)),
    ]
    assert graph.answers(queries) == {"hit": True, "miss": False}
