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

# ruff: noqa: RUF067 - might clean up in the future


"""Graph data types as returned by the DBMS."""

from __future__ import annotations as _

from collections.abc import Mapping as _Mapping

from .. import _typing as _t
from ._identity import (
    DatabaseIdentity,
    GraphBatch,
    Provenance,
)


if _t.TYPE_CHECKING:
    from typing_extensions import deprecated as _deprecated
else:
    from .._warnings import deprecated as _deprecated


__all__ = [
    "DatabaseIdentity",
    "Graph",
    "GraphBatch",
    "GraphView",
    "Node",
    "Path",
    "PendingPath",
    "Provenance",
    "Relationship",
]


_T = _t.TypeVar("_T")


class Graph:
    """
    A graph of nodes and relationships.

    Local, self-contained graph object that acts as a container for
    :class:`.Node` and :class:`.Relationship` instances.
    This is typically obtained via :meth:`.Result.graph` or
    :meth:`.AsyncResult.graph`.

    Entity ``element_id`` values are only unique within their originating
    database and graph batch.  Two graphs that do not agree on both cannot
    share entities, even if the element ids match.
    """

    def __init__(self, batch: GraphBatch | None = None) -> None:
        self._batch = batch
        self._nodes: dict[str, Node] = {}
        self._relationships: dict[str, Relationship] = {}
        self._relationship_types: dict[str, type[Relationship]] = {}
        self._node_set_view = EntitySetView(self._nodes)
        self._relationship_set_view = EntitySetView(self._relationships)

    @property
    def batch(self) -> GraphBatch | None:
        """
        The database identity and batch this graph was read from.

        ``None`` for graphs assembled by callers without batch information.
        """
        return self._batch

    @property
    def database(self) -> DatabaseIdentity | None:
        """The database identity of this graph, if known."""
        return None if self._batch is None else self._batch.database

    def _set_batch(self, batch: GraphBatch) -> None:
        if self._batch is not None and self._batch != batch:
            raise ValueError(
                "cannot mix entities from different graph batches in one "
                f"Graph ({self._batch} != {batch})"
            )
        self._batch = batch

    @property
    def nodes(self) -> EntitySetView[Node]:
        """Access a set view of the nodes in this graph."""
        return self._node_set_view

    @property
    def relationships(self) -> EntitySetView[Relationship]:
        """Access a set view of the relationships in this graph."""
        return self._relationship_set_view

    def relationship_type(self, name: str) -> type[Relationship]:
        """Obtain the relationship class for a given relationship type name."""
        try:
            cls = self._relationship_types[name]
        except KeyError:
            cls = self._relationship_types[name] = _t.cast(
                type[Relationship], type(str(name), (Relationship,), {})
            )
        return cls

    def merge(self, other: Graph) -> None:
        """
        Merge another graph read from the same batch into this one.

        Entities that appear in both graphs keep their identity here.  Their
        provenance is extended with the origin recorded in ``other``.

        Graphs from different databases or batches must not share an element
        cache.  Use :class:`.GraphView` to combine them, optionally after
        confirming a migration continuation.
        """
        if self._batch is None:
            self._batch = other._batch
        if other._batch is not None and other._batch != self._batch:
            raise ValueError(
                "cannot merge graphs from different graph batches "
                f"({self._batch} != {other._batch})"
            )

        for node in other.nodes:
            self._merge_node(node)
        for relationship in other.relationships:
            self._merge_relationship(relationship)

    def _merge_node(self, node: Node) -> Node:
        try:
            existing = self._nodes[node.element_id]
        except KeyError:
            existing = Node(
                self,
                node.element_id,
                node._id,
                node.labels,
                node._properties,
                provenances=node.provenance,
            )
            self._nodes[node.element_id] = existing
            return existing

        existing._labels = existing._labels.union(node._labels)
        existing._properties.update(node._properties)
        existing._add_provenances(node.provenance)
        return existing

    def _merge_relationship(self, relationship: Relationship) -> Relationship:
        try:
            existing = self._relationships[relationship.element_id]
        except KeyError:
            type_ = self.relationship_type(relationship.type)
            existing = type_(
                self,
                relationship.element_id,
                relationship._id,
                relationship._properties,
                provenances=relationship.provenance,
            )
            self._relationships[relationship.element_id] = existing
            if relationship.start_node is not None:
                existing._start_node = self._merge_node(
                    relationship.start_node
                )
            if relationship.end_node is not None:
                existing._end_node = self._merge_node(relationship.end_node)
            return existing

        if existing.type != relationship.type:
            raise ValueError(
                "relationship type conflict for element id "
                f"{relationship.element_id!r}: "
                f"{existing.type!r} != {relationship.type!r}"
            )
        existing_endpoints = {
            _entity_endpoint(existing.start_node),
            _entity_endpoint(existing.end_node),
        }
        new_endpoints = {
            _entity_endpoint(relationship.start_node),
            _entity_endpoint(relationship.end_node),
        }
        if (
            None not in existing_endpoints
            and None not in new_endpoints
            and existing_endpoints != new_endpoints
        ):
            raise ValueError(
                "relationship endpoint conflict for element id "
                f"{relationship.element_id!r}"
            )
        existing._properties.update(relationship._properties)
        existing._add_provenances(relationship.provenance)
        if existing._start_node is None:
            existing._start_node = relationship.start_node
        if existing._end_node is None:
            existing._end_node = relationship.end_node
        return existing

    def __reduce__(self):
        state = self.__dict__.copy()
        relationship_types = tuple(state.pop("_relationship_types", {}).keys())
        restore_args = (relationship_types,)
        return Graph._restore, restore_args, state

    @staticmethod
    def _restore(relationship_types: tuple[str, ...]) -> Graph:
        graph = Graph().__new__(Graph)
        graph.__dict__["_relationship_types"] = {
            name: type(str(name), (Relationship,), {})
            for name in relationship_types
        }
        return graph


def _entity_endpoint(entity: Node | None) -> str | None:
    if entity is None:
        return None
    return entity.element_id


class Entity(_t.Mapping[str, _t.Any]):
    """
    Graph entity base.

    Base class for :class:`.Node` and :class:`.Relationship` that
    provides :class:`.Graph` membership and property containment
    functionality.
    """

    def __init__(
        self,
        graph: Graph,
        element_id: str,
        id_: int,
        properties: dict[str, _t.Any] | None,
        provenances: _t.Iterable[Provenance] | None = None,
    ) -> None:
        self._graph = graph
        self._element_id = element_id
        self._id = id_
        self._properties = {
            k: v for k, v in (properties or {}).items() if v is not None
        }
        self._provenances = tuple(provenances or ())
        if not self._provenances:
            batch = getattr(graph, "batch", None)
            if batch is not None:
                self._provenances = (
                    Provenance(batch.database, batch.batch, element_id),
                )

    def __eq__(self, other: _t.Any) -> bool:
        try:
            return (
                type(self) is type(other)
                and self.graph == other.graph
                and self.element_id == other.element_id
            )
        except AttributeError:
            return NotImplemented

    def __hash__(self):
        return hash(self._element_id)

    def __len__(self) -> int:
        return len(self._properties)

    def __getitem__(self, name: str) -> _t.Any:
        return self._properties.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._properties

    def __iter__(self) -> _t.Iterator[str]:
        return iter(self._properties)

    @property
    def graph(self) -> Graph:
        """The :class:`.Graph` to which this entity belongs."""
        return self._graph

    @property
    def provenance(self) -> tuple[Provenance, ...]:
        """
        The physical occurrences that prove this entity.

        Entities repeated within one read view still have a single origin.
        Entities merged across a confirmed migration continuation keep one
        entry per physical source, in the order the driver learned about
        them.
        """
        return self._provenances

    @property
    def graph_batch(self) -> GraphBatch | None:
        """The batch in which this entity's ``element_id`` is valid."""
        return None if self._graph is None else self._graph.batch

    def _add_provenances(self, provenances: _t.Iterable[Provenance]) -> None:
        seen = set(self._provenances)
        merged = list(self._provenances)
        for provenance in provenances:
            if provenance in seen:
                continue
            seen.add(provenance)
            merged.append(provenance)
        self._provenances = tuple(merged)

    @property
    @_deprecated("`id` is deprecated, use `element_id` instead")
    def id(self) -> int:
        """
        The legacy identity of this entity in its container :class:`.Graph`.

        Depending on the version of the server this entity was retrieved from,
        this may be empty (None).

        .. warning::
            This value can change for the same entity across multiple
            transactions. Don't rely on it for cross-transactional
            computations.

        .. deprecated:: 5.0
            Use :attr:`.element_id` instead.
        """
        return self._id

    @property
    def element_id(self) -> str:
        """
        The identity of this entity in its container :class:`.Graph`.

        .. warning::
            This value can change for the same entity across multiple
            transactions. Don't rely on it for cross-transactional
            computations.
            It is only meaningful together with the database identity and
            graph batch the entity was read from.

        .. versionadded:: 5.0
        """
        return self._element_id

    def get(self, name: str, default: object = None) -> _t.Any:
        """Get a property value by name, optionally with a default."""
        return self._properties.get(name, default)

    def keys(self) -> _t.KeysView[str]:
        """Return an iterable of all property names."""
        return self._properties.keys()

    def values(self) -> _t.ValuesView[_t.Any]:
        """Return an iterable of all property values."""
        return self._properties.values()

    def items(self) -> _t.ItemsView[str, _t.Any]:
        """Return an iterable of all property name-value pairs."""
        return self._properties.items()


class EntitySetView(_Mapping, _t.Generic[_T]):
    """View of a set of :class:`.Entity` instances within a :class:`.Graph`."""

    def __init__(
        self,
        entity_dict: dict[str, _T],
    ) -> None:
        self._entity_dict = entity_dict

    def __getitem__(self, e_id: str) -> _T:
        return self._entity_dict[e_id]

    def __len__(self) -> int:
        return len(self._entity_dict)

    def __iter__(self) -> _t.Iterator[_T]:
        return iter(self._entity_dict.values())


class Node(Entity):
    """Self-contained graph node."""

    def __init__(
        self,
        graph: Graph,
        element_id: str,
        id_: int,
        n_labels: _t.Iterable[str] | None = None,
        properties: dict[str, _t.Any] | None = None,
        provenances: _t.Iterable[Provenance] | None = None,
    ) -> None:
        Entity.__init__(self, graph, element_id, id_, properties, provenances)
        self._labels = frozenset(n_labels or ())

    def __repr__(self) -> str:
        return (
            f"<Node element_id={self._element_id!r} "
            f"labels={self._labels!r} properties={self._properties!r}>"
        )

    @property
    def labels(self) -> frozenset[str]:
        """The set of labels attached to this node."""
        return self._labels


class Relationship(Entity):
    """Self-contained graph relationship."""

    def __init__(
        self,
        graph: Graph,
        element_id: str,
        id_: int,
        properties: dict[str, _t.Any],
        provenances: _t.Iterable[Provenance] | None = None,
    ) -> None:
        Entity.__init__(self, graph, element_id, id_, properties, provenances)
        self._start_node: Node | None = None
        self._end_node: Node | None = None
        self._conflicts: tuple[str, ...] = ()

    @property
    def conflicts(self) -> tuple[str, ...]:
        """
        Conflicts seen while reconciling data for this relationship.

        A non-empty tuple means later data disagreed with an earlier
        indication (for example, the direction a path was read with).
        """
        return self._conflicts

    def _add_conflict(self, message: str) -> None:
        if message not in self._conflicts:
            self._conflicts = (*self._conflicts, message)

    def __repr__(self) -> str:
        return (
            f"<Relationship element_id={self._element_id!r} "
            f"nodes={self.nodes!r} type={self.type!r} "
            f"properties={self._properties!r}>"
        )

    @property
    def nodes(self) -> tuple[Node | None, Node | None]:
        """The pair of nodes which this relationship connects."""
        return self._start_node, self._end_node

    @property
    def start_node(self) -> Node | None:
        """The start node of this relationship."""
        return self._start_node

    @property
    def end_node(self) -> Node | None:
        """The end node of this relationship."""
        return self._end_node

    @property
    def type(self) -> str:
        """
        The type name of this relationship.

        This is functionally equivalent to ``type(relationship).__name__``.
        """
        return type(self).__name__

    def __reduce__(self):
        return Relationship._restore, (self.graph, self.type), self.__dict__

    @staticmethod
    def _restore(graph: Graph, name: str) -> Relationship:
        type_ = graph.relationship_type(name)
        return type_.__new__(type_)


class Path:
    """Self-contained graph path."""

    def __init__(
        self,
        start_node: Node,
        *relationships: Relationship,
        directions: _t.Iterable[int] | None = None,
    ) -> None:
        assert isinstance(start_node, Node)
        nodes = [start_node]
        steps: list[int] = []
        hinted = None if directions is None else tuple(directions)
        if hinted is not None and len(hinted) != len(relationships):
            raise ValueError("directions must have one entry per relationship")
        for i, relationship in enumerate(relationships, start=1):
            assert isinstance(relationship, Relationship)
            hint = None if hinted is None else hinted[i - 1]
            if hint not in (None, 1, -1):
                raise ValueError(
                    f"direction of relationship {i} must be 1 or -1"
                )
            if relationship.start_node == nodes[-1]:
                direction = 1
                nodes.append(_t.cast(Node, relationship.end_node))
            elif relationship.end_node == nodes[-1]:
                direction = -1
                nodes.append(_t.cast(Node, relationship.start_node))
            else:
                raise ValueError(
                    f"Relationship {i} does not connect to the last node"
                )
            if hint is not None and hint != direction:
                raise ValueError(
                    f"Relationship {i} is traversed against its endpoints"
                )
            steps.append(direction)
        self._nodes = tuple(nodes)
        self._relationships = relationships
        self._directions = tuple(steps)

    def __repr__(self) -> str:
        return (
            f"<Path start={self.start_node!r} end={self.end_node!r} "
            f"size={len(self)}>"
        )

    def __eq__(self, other: _t.Any) -> bool:
        try:
            return (
                self.start_node == other.start_node
                and self.relationships == other.relationships
                and self.directions == other.directions
            )
        except AttributeError:
            return NotImplemented

    def __hash__(self):
        value = hash(self._nodes[0])
        for relationship, direction in zip(
            self._relationships, self._directions, strict=True
        ):
            value ^= hash(relationship) * direction
        return value

    def __len__(self) -> int:
        return len(self._relationships)

    def __iter__(self) -> _t.Iterator[Relationship]:
        return iter(self._relationships)

    @property
    def graph(self) -> Graph:
        """The :class:`.Graph` to which this path belongs."""
        return self._nodes[0].graph

    @property
    def nodes(self) -> tuple[Node, ...]:
        """The sequence of :class:`.Node` objects in this path."""
        return self._nodes

    @property
    def start_node(self) -> Node:
        """The first :class:`.Node` in this path."""
        return self._nodes[0]

    @property
    def end_node(self) -> Node:
        """The last :class:`.Node` in this path."""
        return self._nodes[-1]

    @property
    def relationships(self) -> tuple[Relationship, ...]:
        """The sequence of :class:`.Relationship` objects in this path."""
        return self._relationships

    @property
    def directions(self) -> tuple[int, ...]:
        """
        How each relationship is traversed in this path.

        ``1`` means start node to end node, ``-1`` means the reverse.  This
        is independent of the actual direction of the relationship itself.
        """
        return self._directions

    @property
    def traversal(self) -> tuple[tuple[Relationship, int], ...]:
        """The relationships paired with their traversal direction."""
        return tuple(zip(self._relationships, self._directions, strict=True))


class PendingPath:
    """
    A path whose late data conflicts with the direction it was read with.

    The path itself is still delivered.  The conflict is attached to the
    path so callers can review it without changing any already delivered
    result.
    """

    def __init__(
        self,
        path: Path,
        conflict: str,
        conflicting_relationships: _t.Iterable[Relationship] = (),
    ) -> None:
        self._path = path
        self._conflict = conflict
        self._conflicting_relationships = tuple(conflicting_relationships)

    def __repr__(self) -> str:
        return f"<PendingPath path={self._path!r} conflict={self._conflict!r}>"

    @property
    def path(self) -> Path:
        return self._path

    @property
    def conflict(self) -> str:
        return self._conflict

    @property
    def conflicting_relationships(
        self,
    ) -> tuple[Relationship, ...]:
        return self._conflicting_relationships

    def __eq__(self, other: _t.Any) -> bool:
        try:
            return (
                self.path == other.path
                and self.conflict == other.conflict
                and self.conflicting_relationships
                == other.conflicting_relationships
            )
        except AttributeError:
            return NotImplemented


from ._view import GraphView  # noqa: E402 (avoid circular import)


def _mark_relationship_conflict(
    relationship: Relationship, message: str
) -> None:
    relationship._add_conflict(message)


def _bind_relationship(
    relationship: Relationship,
    expected_start: Node,
    expected_end: Node,
) -> None:
    """Bind a path's traversal without changing the relationship direction.

    Paths may present a relationship in either direction.  The relationship
    keeps the start and end proven by its own data; the path records how it
    was traversed separately.  ``expected_start`` and ``expected_end`` are
    always the relationship's actual endpoints as indicated by this path.
    """
    if relationship._start_node is None and relationship._end_node is None:
        relationship._start_node = expected_start
        relationship._end_node = expected_end
        return
    actual_start = relationship._start_node
    actual_end = relationship._end_node
    if (
        actual_start is not None
        and actual_end is not None
        and (
            actual_start is not expected_start
            or actual_end is not expected_end
        )
    ):
        _mark_relationship_conflict(
            relationship,
            "path traversal disagrees with the start/end indication for "
            f"relationship {relationship.element_id!r}",
        )


def _remember_path_binding(
    relationship: Relationship,
    start_node: Node,
    end_node: Node,
) -> None:
    """Remember the node pair a path saw, so later data can be reconciled."""
    relationship._path_bound = (start_node, end_node)
