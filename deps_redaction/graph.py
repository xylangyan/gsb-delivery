"""Directed enhanced graph and label-sequence path queries."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable, Sequence


@dataclass(frozen=True)
class Edge:
    """One enhanced relation ``src --label--> dst`` (ids are raw CoNLL-U ids)."""

    src: str
    dst: str
    label: str


class EnhancedGraph:
    """A simple directed multigraph keyed by CoNLL-U token ids."""

    def __init__(self, edges: Iterable[Edge] = ()) -> None:
        self._edges: set[Edge] = set(edges)

    @property
    def edges(self) -> frozenset[Edge]:
        return frozenset(self._edges)

    def add(self, edge: Edge) -> None:
        self._edges.add(edge)

    def discard(self, edge: Edge) -> None:
        self._edges.discard(edge)

    def __len__(self) -> int:
        return len(self._edges)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, EnhancedGraph) and other._edges == self._edges

    def __hash__(self) -> int:  # pragma: no cover - graphs are mutable
        raise TypeError("EnhancedGraph is not hashable")

    def adjacency(self) -> dict[str, list[tuple[str, str, Edge]]]:
        adj: dict[str, list[tuple[str, str, Edge]]] = {}
        for edge in self._edges:
            adj.setdefault(edge.src, []).append((edge.dst, edge.label, edge))
        return adj

    def nodes(self) -> set[str]:
        result: set[str] = set()
        for edge in self._edges:
            result.add(edge.src)
            result.add(edge.dst)
        return result

    def find_path(
        self, source: str, target: str, labels: Sequence[str]
    ) -> tuple[Edge, ...] | None:
        """Return one directed path matching ``labels`` exactly, or ``None``.

        A label sequence of length ``k`` requires a path of exactly ``k`` edges
        whose labels equal ``labels`` in order.  Repeated nodes are allowed
        (enhanced graphs are not guaranteed acyclic); the search is bounded by
        the sequence length so it always terminates.
        """

        adj = self.adjacency()

        def dfs(node: str, depth: int, trail: tuple[Edge, ...]) -> tuple[Edge, ...] | None:
            if depth == len(labels):
                return trail if node == target else None
            for nxt, edge_label, edge in adj.get(node, ()):
                if edge_label != labels[depth]:
                    continue
                found = dfs(nxt, depth + 1, trail + (edge,))
                if found is not None:
                    return found
            return None

        if not labels:
            return () if source == target else None
        return dfs(source, 0, ())

    def query_yes(self, source: str, target: str, labels: Sequence[str]) -> bool:
        return self.find_path(source, target, labels) is not None

    def answers(self, queries: Iterable["QuerySpec"]) -> dict[str, bool]:
        return {
            query.name: self.query_yes(query.source, query.target, query.labels)
            for query in queries
        }


@dataclass(frozen=True)
class QuerySpec:
    name: str
    source: str
    target: str
    labels: tuple[str, ...]


def label_sequence_matches(actual: Sequence[str], expected: Sequence[str]) -> bool:
    return tuple(actual) == tuple(expected)
