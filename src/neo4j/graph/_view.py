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


"""Immutable read views over graph responses."""

from __future__ import annotations

from dataclasses import dataclass

from .. import _typing as t
from . import (
    DatabaseIdentity,
    Graph,
    GraphBatch,
    Node,
    Path,
    PendingPath,
    Provenance,
    Relationship,
)


__all__ = ["GraphView"]


_ANONYMOUS_DATABASE = DatabaseIdentity("<anonymous>")
_ANY_BATCH = GraphBatch(_ANONYMOUS_DATABASE, "any")


_NodeKey: t.TypeAlias = tuple[GraphBatch, str]
_RelKey: t.TypeAlias = tuple[GraphBatch, str, str | None, str | None, str]


@dataclass(frozen=True)
class _Source:
    """One observation of a physical relationship."""

    provenance: Provenance
    start_id: str | None
    end_id: str | None
    type: str


@dataclass(frozen=True)
class _PathSpec:
    """A path expressed through physical keys, not live objects."""

    nodes: tuple[_NodeKey, ...]
    relationships: tuple[_RelKey, ...]
    directions: tuple[int, ...]
    source: Path | None = None

    @property
    def start(self) -> _NodeKey:
        return self.nodes[0]


@dataclass(frozen=True)
class _PendingSpec:
    path: _PathSpec
    conflict: str
    conflicting: tuple[_RelKey, ...] = ()
    source: Path | None = None


class GraphView:
    """
    An immutable view over graph data read from one or more responses.

    A view resolves the identity of the data it contains:

    * an entity repeated within one view (for example, in a node list and in
      a path) is represented by one object;
    * the same ``element_id`` in an unrelated database or graph batch remains
      a different object and never shares an element cache;
    * adding data returns a new view; views handed out earlier are not
      changed by later additions.

    Without a confirmed migration continuation, entities from different
    physical sources stay independent.  Use :meth:`confirm_migration` after
    the input proves that a node (or a relationship with the same endpoints)
    continues across sources.
    """

    def __init__(self) -> None:
        self._graph = Graph()
        self._nodes: tuple[Node, ...] = ()
        self._relationships: tuple[Relationship, ...] = ()
        self._paths: tuple[Path, ...] = ()
        self._pending_paths: tuple[PendingPath, ...] = ()
        self._migrations: tuple[tuple[Provenance, ...], ...] = ()
        self._conflicts: dict[tuple[GraphBatch, str], tuple[str, ...]] = {}
        self._node_index: dict[_NodeKey, Node] = {}
        self._rel_index: dict[_RelKey, Relationship] = {}
        self._rel_sources: dict[
            tuple[GraphBatch, str], tuple[_Source, ...]
        ] = {}
        self._path_specs: tuple[_PathSpec, ...] = ()
        self._pending_specs: tuple[_PendingSpec, ...] = ()

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    @property
    def graph(self) -> Graph:
        """The container graph backing this view."""
        return self._graph

    @property
    def nodes(self) -> tuple[Node, ...]:
        """The deduplicated nodes in this view, in discovery order."""
        return self._nodes

    @property
    def relationships(self) -> tuple[Relationship, ...]:
        """The deduplicated relationships in this view, in discovery order."""
        return self._relationships

    @property
    def paths(self) -> tuple[Path, ...]:
        """The paths this view can deliver."""
        return self._paths

    @property
    def pending_paths(self) -> tuple[PendingPath, ...]:
        """Paths left for review because their data conflicts."""
        return self._pending_paths

    @property
    def migrations(self) -> tuple[tuple[Provenance, ...], ...]:
        """Confirmed migration continuations, one tuple per continuation."""
        return self._migrations

    @property
    def conflicts(self) -> dict[tuple[GraphBatch, str], tuple[str, ...]]:
        """Late-data conflicts keyed by physical relationship identity."""
        return dict(self._conflicts)

    def nodes_by_element_id(self, element_id: str) -> tuple[Node, ...]:
        """All independent nodes with this element id in the view."""
        return tuple(
            node for node in self._nodes if node.element_id == element_id
        )

    def relationships_by_element_id(
        self, element_id: str
    ) -> tuple[Relationship, ...]:
        """All independent relationships with this element id in the view."""
        return tuple(
            relationship
            for relationship in self._relationships
            if relationship.element_id == element_id
        )

    def node(
        self,
        element_id: str,
        *,
        database: DatabaseIdentity | str | None = None,
        batch: str | None = None,
    ) -> Node:
        """Look up one node, refusing to guess between independent sources."""
        database = (
            None if database is None else DatabaseIdentity.coerce(database)
        )
        matches = [
            node
            for node in self._nodes
            if node.element_id == element_id
            and any(
                (database is None or provenance.database == database)
                and (batch is None or provenance.batch == batch)
                for provenance in node.provenance
            )
        ]
        return _one(matches, "node", element_id)

    def relationship(
        self,
        element_id: str,
        *,
        database: DatabaseIdentity | str | None = None,
        batch: str | None = None,
        start_node_element_id: str | None = None,
        end_node_element_id: str | None = None,
    ) -> Relationship:
        """Look up one relationship, refusing to guess between sources."""
        database = (
            None if database is None else DatabaseIdentity.coerce(database)
        )
        matches = [
            relationship
            for relationship in self._relationships
            if relationship.element_id == element_id
            and any(
                (database is None or provenance.database == database)
                and (batch is None or provenance.batch == batch)
                for provenance in relationship.provenance
            )
            and (
                start_node_element_id is None
                or _node_id(relationship.start_node) == start_node_element_id
            )
            and (
                end_node_element_id is None
                or _node_id(relationship.end_node) == end_node_element_id
            )
        ]
        return _one(matches, "relationship", element_id)

    # ------------------------------------------------------------------
    # Building views
    # ------------------------------------------------------------------

    @classmethod
    def from_graph(
        cls,
        graph: Graph,
        *,
        database: DatabaseIdentity | str | None = None,
        batch: str | None = None,
        paths: t.Iterable[Path] = (),
    ) -> GraphView:
        """Create a view from one hydrated graph."""
        return cls().add_graph(
            graph, database=database, batch=batch, paths=paths
        )

    def add_graph(
        self,
        graph: Graph,
        *,
        database: DatabaseIdentity | str | None = None,
        batch: str | None = None,
        paths: t.Iterable[Path] = (),
    ) -> GraphView:
        """
        Return a new view with ``graph`` and ``paths`` added.

        ``database`` and ``batch`` override the graph's own batch.  A graph
        without batch information is treated as its own anonymous source, so
        it can never accidentally share an element cache with another graph.
        """
        graph_batch = _resolve_graph_batch(graph, database, batch)
        return _Builder(self).add_response(graph_batch, graph, tuple(paths))

    def add_response(
        self,
        graph: Graph,
        *,
        database: DatabaseIdentity | str | None = None,
        batch: str | None = None,
        paths: t.Iterable[Path] = (),
    ) -> GraphView:
        """Add one whole response (its graph and its paths)."""
        return self.add_graph(
            graph, database=database, batch=batch, paths=paths
        )

    def add_result(
        self,
        graph: Graph,
        *,
        database: DatabaseIdentity | str,
        batch: str,
        paths: t.Iterable[Path] = (),
    ) -> GraphView:
        """
        Add one response whose database identity and batch are known.

        The explicit response identity wins over any batch an entity
        carries from an earlier graph it happened to be shared with.
        """
        graph_batch = GraphBatch(
            _coerce_database(database, graph.batch), batch
        )
        return _Builder(self).add_response(
            graph_batch, graph, tuple(paths), explicit_batch=True
        )

    def add_paths(
        self,
        *paths: Path,
        database: DatabaseIdentity | str | None = None,
        batch: str | None = None,
    ) -> GraphView:
        """Add paths read in the given database and batch."""
        graph_batch = _resolve_loose_batch(paths, database, batch)
        return _Builder(self).add_response(graph_batch, None, paths)

    def add_nodes(
        self,
        *nodes: Node,
        database: DatabaseIdentity | str | None = None,
        batch: str | None = None,
    ) -> GraphView:
        """Add standalone nodes read from the same database and batch."""
        graph_batch = _resolve_nodes_batch(nodes, database, batch)
        graph = Graph(graph_batch)
        for node in nodes:
            graph._nodes.setdefault(
                node.element_id,
                Node(
                    graph,
                    node.element_id,
                    node._id,
                    node.labels,
                    dict(node._properties),
                ),
            )
        return _Builder(self).add_response(graph_batch, graph, ())

    def confirm_migration(
        self, *entities: Node | Relationship | Provenance
    ) -> GraphView:
        """
        Confirm that entities from different sources are one continuation.

        Sources are matched by ``element_id``.  Every physical source is kept
        in discovery order on the merged entity.  Relationships are merged
        only when their type and actual endpoints agree; otherwise they stay
        independent and a :exc:`ValueError` is raised.
        """
        provenances = _collect_provenances(entities)
        element_id = provenances[0].element_id

        node_candidates = [
            node
            for node in self._nodes
            if node.element_id == element_id
            and any(p in node.provenance for p in provenances)
        ]
        rel_candidates = [
            relationship
            for relationship in self._relationships
            if relationship.element_id == element_id
            and any(p in relationship.provenance for p in provenances)
        ]
        if node_candidates and rel_candidates:
            raise ValueError(
                "a node and a relationship cannot be one continuation"
            )
        if not node_candidates and not rel_candidates:
            raise ValueError(
                "none of the confirmed sources are present in this view"
            )

        builder = _Builder(self)
        if node_candidates:
            builder.merge_nodes(
                [node.element_id for node in node_candidates], provenances
            )
        else:
            builder.merge_relationships(
                [
                    _rel_key(source, relationship)
                    for relationship in rel_candidates
                    for source in relationship.provenance
                ],
                provenances,
            )
        view = builder.finish()
        view._migrations = self._migrations + (provenances,)
        return view

    def deliver(self) -> GraphView:
        """Hand the view out.

        Views are immutable, so this returns the same object.  It exists to
        make the delivery point explicit in calling code.
        """
        return self

    # ------------------------------------------------------------------
    # Internal construction
    # ------------------------------------------------------------------

    @classmethod
    def _assemble(
        cls,
        nodes: tuple[Node, ...],
        relationships: tuple[Relationship, ...],
        path_specs: tuple[_PathSpec, ...],
        pending_specs: tuple[_PendingSpec, ...],
        conflicts: dict[tuple[GraphBatch, str], tuple[str, ...]],
        migrations: tuple[tuple[Provenance, ...], ...],
    ) -> GraphView:
        view = cls()
        view._graph = _backing_graph(nodes, relationships)
        view._nodes = nodes
        view._relationships = relationships
        for entity in (*nodes, *relationships):
            entity._graph = view._graph
        view._migrations = migrations
        view._conflicts = conflicts
        view._rebuild_indexes()
        _fill_graph_caches(view._graph, view._nodes, view._relationships)
        view._path_specs = path_specs
        view._pending_specs = pending_specs
        view._build_paths()
        return view

    def _rebuild_indexes(self) -> None:
        self._node_index = {}
        for node in self._nodes:
            for provenance in node.provenance:
                key = (provenance.graph_batch, provenance.element_id)
                previous = self._node_index.get(key)
                if previous is not None and previous is not node:
                    raise ValueError(
                        "one physical node ended up in two objects"
                    )
                self._node_index[key] = node

        self._rel_index = {}
        self._rel_sources = {}
        for relationship in self._relationships:
            for provenance in relationship.provenance:
                start_id, end_id = _end_ids(relationship)
                key = (
                    provenance.graph_batch,
                    provenance.element_id,
                    start_id,
                    end_id,
                    relationship.type,
                )
                self._rel_index[key] = relationship
                source_key = (
                    provenance.graph_batch,
                    provenance.element_id,
                )
                self._rel_sources[source_key] = self._rel_sources.get(
                    source_key, ()
                ) + (
                    _Source(
                        provenance,
                        start_id,
                        end_id,
                        relationship.type,
                    ),
                )

    def _build_paths(self) -> None:
        delivered: list[Path] = []
        pending_specs = list(self._pending_specs)
        for spec in self._path_specs:
            try:
                delivered.append(self._path_from_spec(spec))
            except (KeyError, ValueError):
                pending_specs.append(
                    _PendingSpec(
                        spec,
                        "path no longer matches the physical data in this "
                        "view",
                        tuple(
                            key
                            for key in spec.relationships
                            if key not in self._rel_index
                        ),
                        source=spec.source,
                    )
                )
        pending: list[PendingPath] = []
        for spec in pending_specs:
            conflicting = tuple(
                self._rel_index[key]
                for key in spec.conflicting
                if key in self._rel_index
            )
            # A pending path keeps the traversal and endpoints as they were
            # indicated when it was delivered.  Late contradictory data is
            # reported alongside it, not applied to it.
            if spec.source is not None:
                path = self._reproject(spec.source)
            else:
                try:
                    path = self._path_from_spec(spec.path)
                except (KeyError, ValueError):
                    continue
            pending.append(PendingPath(path, spec.conflict, conflicting))
        self._paths = tuple(delivered)
        self._pending_paths = tuple(pending)

    def _reproject(self, path: Path) -> Path:
        """Rebind a path to this view's entities, keeping its traversal."""
        nodes = []
        for node in path.nodes:
            batch = _node_batch(node, node.graph.batch or _ANY_BATCH)
            try:
                nodes.append(self._node_index[(batch, node.element_id)])
            except KeyError:
                return path
        relationships = []
        for relationship in path.relationships:
            for provenance in relationship.provenance:
                start_id, end_id = _end_ids(relationship)
                key = (
                    provenance.graph_batch,
                    provenance.element_id,
                    start_id,
                    end_id,
                    relationship.type,
                )
                try:
                    relationships.append(self._rel_index[key])
                    break
                except KeyError:
                    continue
            else:
                return path
        return Path(nodes[0], *relationships, directions=path.directions)

    def _path_from_spec(self, spec: _PathSpec) -> Path:
        try:
            nodes = tuple(self._node_index[key] for key in spec.nodes)
        except KeyError as error:
            raise KeyError(error.args[0][1]) from None
        relationships = []
        directions = []
        for index, key in enumerate(spec.relationships):
            try:
                relationship = self._rel_index[key]
            except KeyError:
                raise KeyError(key[1]) from None
            relationships.append(relationship)
            direction = _direction_between(
                nodes[index],
                nodes[index + 1],
                relationship,
            )
            if (
                index < len(spec.directions)
                and direction != spec.directions[index]
            ):
                raise ValueError(
                    "path traversal no longer matches the physical "
                    "relationship data"
                )
            directions.append(direction)
        return Path(nodes[0], *relationships, directions=directions)


class _Builder:
    """Builds one new immutable view without touching the previous one."""

    def __init__(self, previous: GraphView) -> None:
        self._previous = previous
        self._graph = Graph()
        self._nodes: list[Node] = []
        self._relationships: list[Relationship] = []
        self._path_specs: list[_PathSpec] = list(previous._path_specs)
        self._pending_specs: list[_PendingSpec] = list(previous._pending_specs)
        self._conflicts = dict(previous._conflicts)
        self._migrations = previous._migrations
        self._node_index: dict[_NodeKey, Node] = {}
        self._rel_index: dict[_RelKey, Relationship] = {}
        self._rel_sources: dict[tuple[GraphBatch, str], list[_Source]] = {}

        for node in previous._nodes:
            cloned = _clone_node(node, self._graph)
            self._nodes.append(cloned)
            for provenance in cloned.provenance:
                self._node_index[
                    (provenance.graph_batch, provenance.element_id)
                ] = cloned

        for relationship in previous._relationships:
            cloned = _clone_relationship(
                relationship, self._graph, self._node_index
            )
            self._relationships.append(cloned)
            for provenance in cloned.provenance:
                start_id, end_id = _end_ids(cloned)
                key = (
                    provenance.graph_batch,
                    provenance.element_id,
                    start_id,
                    end_id,
                    cloned.type,
                )
                self._rel_index[key] = cloned
                self._rel_sources.setdefault(
                    (provenance.graph_batch, provenance.element_id), []
                ).append(_Source(provenance, start_id, end_id, cloned.type))

    # -- adding a response ----------------------------------------------

    def add_response(
        self,
        batch: GraphBatch,
        graph: Graph | None,
        paths: tuple[Path, ...],
        *,
        explicit_batch: bool = False,
    ) -> GraphView:
        incoming_nodes: list[Node] = []
        incoming_relationships: list[Relationship] = []

        if graph is not None:
            incoming_nodes.extend(graph.nodes)
            incoming_relationships.extend(graph.relationships)
        for path in paths:
            incoming_nodes.extend(path.nodes)
            incoming_relationships.extend(path.relationships)

        seen_nodes: set[int] = set()
        for node in incoming_nodes:
            if id(node) in seen_nodes:
                continue
            seen_nodes.add(id(node))
            self._intern_node(node, batch, explicit_batch)

        seen_relationships: set[int] = set()
        for relationship in incoming_relationships:
            if id(relationship) in seen_relationships:
                continue
            seen_relationships.add(id(relationship))
            self._intern_relationship(relationship, batch, explicit_batch)

        for path in paths:
            spec = self._path_spec(path, batch, explicit_batch)
            if spec is None:
                self._pending_specs.append(
                    _PendingSpec(
                        _fallback_spec(path),
                        "path cannot be resolved to one physical source",
                        source=path,
                    )
                )
                continue
            conflicting = tuple(
                key
                for key in spec.relationships
                if (key[0], key[1]) in self._conflicts
            )
            if conflicting:
                self._pending_specs.append(
                    _PendingSpec(
                        spec,
                        "path uses relationship with conflicting late data",
                        conflicting,
                        source=path,
                    )
                )
            else:
                self._path_specs.append(spec)

        # A late conflict can also affect paths delivered earlier.
        moved: list[_PathSpec] = []
        still_delivered: list[_PathSpec] = []
        for spec in self._path_specs:
            conflicting = tuple(
                key
                for key in spec.relationships
                if (key[0], key[1]) in self._conflicts
            )
            if conflicting:
                moved.append(spec)
                self._pending_specs.append(
                    _PendingSpec(
                        spec,
                        "path uses relationship with conflicting late data",
                        conflicting,
                        source=spec.source,
                    )
                )
            else:
                still_delivered.append(spec)
        if moved:
            self._path_specs = still_delivered

        return self.finish()

    def _intern_node(
        self,
        node: Node,
        fallback: GraphBatch,
        explicit_batch: bool = False,
    ) -> Node:
        batch = _node_batch(node, fallback, explicit_batch)
        key = (batch, node.element_id)
        provenance = Provenance(batch.database, batch.batch, node.element_id)
        try:
            existing = self._node_index[key]
        except KeyError:
            cloned = Node(
                self._graph,
                node.element_id,
                node._id,
                node.labels,
                dict(node._properties),
                provenances=(provenance,),
            )
            self._nodes.append(cloned)
            self._node_index[key] = cloned
            return cloned
        existing._labels = existing._labels.union(node._labels)
        existing._properties.update(node._properties)
        existing._add_provenances((provenance,))
        return existing

    def _intern_relationship(
        self,
        relationship: Relationship,
        fallback: GraphBatch,
        explicit_batch: bool = False,
    ) -> Relationship:
        batch = _relationship_batch(relationship, fallback, explicit_batch)
        provenance = Provenance(
            batch.database, batch.batch, relationship.element_id
        )
        start_id, end_id = _end_ids(relationship)
        key = (
            batch,
            relationship.element_id,
            start_id,
            end_id,
            relationship.type,
        )
        try:
            existing = self._rel_index[key]
        except KeyError:
            existing = _compatible_relationship(
                self._rel_index,
                batch,
                relationship.element_id,
                relationship.type,
                start_id,
                end_id,
            )
            if existing is not None:
                existing._properties.update(relationship._properties)
                existing._add_provenances((provenance,))
                _fill_endpoints(existing, start_id, end_id, self._node_index)
                self._rel_index[key] = existing
                return existing
        else:
            existing._properties.update(relationship._properties)
            existing._add_provenances((provenance,))
            return existing

        source_key = (batch, relationship.element_id)
        previous_sources = self._rel_sources.get(source_key, [])
        for source in previous_sources:
            if source.type != relationship.type:
                self._record_conflict(
                    source_key,
                    "late data conflicts with the type of relationship "
                    f"{relationship.element_id!r}",
                )
                break
            if (
                None not in (source.start_id, source.end_id)
                and None not in (start_id, end_id)
                and (source.start_id, source.end_id) != (start_id, end_id)
            ):
                self._record_conflict(
                    source_key,
                    "late data conflicts with the start/end indication for "
                    f"relationship {relationship.element_id!r}",
                )
                break

        cloned = _new_relationship(
            self._graph,
            relationship.type,
            relationship.element_id,
            relationship._id,
            dict(relationship._properties),
            (provenance,),
            relationship.start_node,
            relationship.end_node,
            self._node_index,
            start_id,
            end_id,
        )
        self._relationships.append(cloned)
        self._rel_index[key] = cloned
        self._rel_sources.setdefault(source_key, []).append(
            _Source(provenance, start_id, end_id, relationship.type)
        )
        return cloned

    def _record_conflict(
        self, key: tuple[GraphBatch, str], message: str
    ) -> None:
        self._conflicts[key] = self._conflicts.get(key, ()) + (message,)

    def _path_spec(
        self,
        path: Path,
        fallback: GraphBatch,
        explicit_batch: bool = False,
    ) -> _PathSpec | None:
        node_keys: list[_NodeKey] = []
        for node in path.nodes:
            batch = _node_batch(node, fallback, explicit_batch)
            node_keys.append((batch, node.element_id))
            if node_keys[-1] not in self._node_index:
                return None

        rel_keys: list[_RelKey] = []
        for relationship in path.relationships:
            batch = _relationship_batch(relationship, fallback, explicit_batch)
            start_id, end_id = _end_ids(relationship)
            key = (
                batch,
                relationship.element_id,
                start_id,
                end_id,
                relationship.type,
            )
            if key not in self._rel_index:
                candidates = [
                    candidate
                    for candidate in self._rel_index
                    if candidate[0] == batch
                    and candidate[1] == relationship.element_id
                    and candidate[4] == relationship.type
                ]
                if len(candidates) != 1:
                    return None
                key = candidates[0]
            rel_keys.append(key)
        if len(rel_keys) != len(path.directions):
            return None
        if len(node_keys) != len(rel_keys) + 1:
            return None
        return _PathSpec(
            tuple(node_keys),
            tuple(rel_keys),
            tuple(path.directions),
            source=path,
        )

    # -- migration ------------------------------------------------------

    def merge_nodes(
        self,
        element_ids: list[str],
        provenances: tuple[Provenance, ...],
    ) -> None:
        candidates = [
            node
            for node in self._nodes
            if node.element_id in element_ids
            and any(p in node.provenance for p in provenances)
        ]
        if len(candidates) < 2:
            raise ValueError(
                "a node migration needs at least two physical sources "
                "visible in this view"
            )
        canonical = candidates[0]
        merged_ids = {id(node) for node in candidates[1:]}
        for candidate in candidates[1:]:
            canonical._labels = canonical._labels.union(candidate._labels)
            canonical._properties.update(candidate._properties)
            canonical._add_provenances(candidate.provenance)
        canonical._provenances = _unique((*canonical.provenance, *provenances))

        self._nodes = [
            node for node in self._nodes if id(node) not in merged_ids
        ]
        self._rebuild_node_index()

        for relationship in self._relationships:
            relationship._start_node = _repoint_node(
                relationship.start_node, self._node_index
            )
            relationship._end_node = _repoint_node(
                relationship.end_node, self._node_index
            )
        self._rebuild_rel_index()

    def _rebuild_node_index(self) -> None:
        self._node_index = {}
        for node in self._nodes:
            for provenance in node.provenance:
                self._node_index[
                    (provenance.graph_batch, provenance.element_id)
                ] = node

    def merge_relationships(
        self,
        keys: list[_RelKey],
        provenances: tuple[Provenance, ...],
    ) -> None:
        candidates = {
            id(self._rel_index[key]): self._rel_index[key]
            for key in keys
            if key in self._rel_index
        }
        if len(candidates) < 2:
            raise ValueError(
                "a relationship migration needs at least two physical "
                "sources visible in this view"
            )
        identity = {
            (_end_ids(relationship), relationship.type)
            for relationship in candidates.values()
        }
        if len(identity) != 1:
            raise ValueError(
                "relationships with different endpoints cannot be merged; "
                "they stay independent without further confirmation"
            )
        ordered = [
            relationship
            for relationship in self._relationships
            if id(relationship) in candidates
        ]
        canonical = ordered[0]
        for candidate in ordered[1:]:
            canonical._properties.update(candidate._properties)
            canonical._add_provenances(candidate.provenance)
        canonical._provenances = _unique((*canonical.provenance, *provenances))

        self._relationships = [
            relationship
            for relationship in self._relationships
            if id(relationship) not in candidates
            or id(relationship) == id(canonical)
        ]
        self._rebuild_rel_index()

    def _rebuild_rel_index(self) -> None:
        self._rel_index = {}
        self._rel_sources = {}
        for relationship in self._relationships:
            for provenance in relationship.provenance:
                start_id, end_id = _end_ids(relationship)
                self._rel_index[
                    (
                        provenance.graph_batch,
                        provenance.element_id,
                        start_id,
                        end_id,
                        relationship.type,
                    )
                ] = relationship
                self._rel_sources.setdefault(
                    (provenance.graph_batch, provenance.element_id), []
                ).append(
                    _Source(provenance, start_id, end_id, relationship.type)
                )

    def finish(self) -> GraphView:
        return GraphView._assemble(
            tuple(self._nodes),
            tuple(self._relationships),
            tuple(self._path_specs),
            tuple(self._pending_specs),
            self._conflicts,
            self._migrations,
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _backing_graph(
    nodes: tuple[Node, ...],
    relationships: tuple[Relationship, ...],
) -> Graph:
    """Give the view's entities one shared container graph.

    The container carries the batch when every entity belongs to one.
    """
    batches = {
        provenance.graph_batch
        for entity in (*nodes, *relationships)
        for provenance in entity.provenance
    }
    return Graph(batches.pop() if len(batches) == 1 else None)


def _fill_graph_caches(
    graph: Graph,
    nodes: tuple[Node, ...],
    relationships: tuple[Relationship, ...],
) -> None:
    """Make the backing graph expose the view's unambiguous entities.

    The cache is keyed by element id.  Ids that occur in more than one
    physical source cannot be cached there; use the view to reach them.
    """
    node_counts: dict[str, int] = {}
    for node in nodes:
        node_counts[node.element_id] = node_counts.get(node.element_id, 0) + 1
    graph._nodes.clear()
    for node in nodes:
        if node_counts[node.element_id] == 1:
            graph._nodes[node.element_id] = node

    rel_counts: dict[str, int] = {}
    for relationship in relationships:
        rel_counts[relationship.element_id] = (
            rel_counts.get(relationship.element_id, 0) + 1
        )
    graph._relationships.clear()
    for relationship in relationships:
        if rel_counts[relationship.element_id] == 1:
            graph._relationships[relationship.element_id] = relationship


def _unique(
    provenances: t.Iterable[Provenance],
) -> tuple[Provenance, ...]:
    seen: set[Provenance] = set()
    result: list[Provenance] = []
    for provenance in provenances:
        if provenance in seen:
            continue
        seen.add(provenance)
        result.append(provenance)
    return tuple(result)


def _collect_provenances(
    entities: tuple[Node | Relationship | Provenance, ...],
) -> tuple[Provenance, ...]:
    provenances: list[Provenance] = []
    for entity in entities:
        if isinstance(entity, Provenance):
            provenances.append(entity)
            continue
        entity_provenances = getattr(entity, "provenance", ())
        if not entity_provenances:
            raise ValueError(
                "cannot confirm a migration for an entity without provenance"
            )
        provenances.extend(entity_provenances)
    provenances = list(_unique(provenances))
    if len(provenances) < 2:
        raise ValueError(
            "a migration continuation needs at least two physical sources"
        )
    if len({provenance.element_id for provenance in provenances}) != 1:
        raise ValueError(
            "only entities with the same element id can be one continuation"
        )
    return tuple(provenances)


def _one(matches: list[t.Any], kind: str, element_id: str) -> t.Any:
    if not matches:
        raise KeyError(f"no {kind} with element id {element_id!r}")
    if len(matches) > 1:
        raise LookupError(
            f"{kind} element id {element_id!r} is ambiguous; specify the "
            "database and batch"
        )
    return matches[0]


def _coerce_database(
    database: DatabaseIdentity | str,
    source_batch: GraphBatch | None = None,
) -> DatabaseIdentity:
    """Resolve a database identity, filling in the address when implied.

    A bare database name means "the database with that name on the same
    server this graph came from".  An explicit address always wins.
    """
    database = DatabaseIdentity.coerce(database)
    if (
        database.address is None
        and source_batch is not None
        and source_batch.database.name == database.name
        and source_batch.database.address is not None
    ):
        return DatabaseIdentity(database.name, source_batch.database.address)
    return database


def _resolve_graph_batch(
    graph: Graph,
    database: DatabaseIdentity | str | None,
    batch: str | None,
) -> GraphBatch:
    if database is None and batch is None:
        if graph.batch is not None:
            return graph.batch
        return GraphBatch(_ANONYMOUS_DATABASE, f"anonymous-{id(graph):x}")
    graph_batch = graph.batch
    resolved_database = (
        DatabaseIdentity.coerce(database)
        if database is not None
        else None
        if graph_batch is None
        else graph_batch.database
    )
    resolved_batch = (
        batch
        if batch is not None
        else None
        if graph_batch is None
        else graph_batch.batch
    )
    if resolved_database is None or resolved_batch is None:
        raise ValueError(
            "both a database identity and a batch name are needed unless "
            "the graph already carries a batch"
        )
    return GraphBatch(resolved_database, resolved_batch)


def _resolve_nodes_batch(
    nodes: tuple[Node, ...],
    database: DatabaseIdentity | str | None,
    batch: str | None,
) -> GraphBatch:
    if database is not None and batch is not None:
        return GraphBatch(DatabaseIdentity.coerce(database), batch)
    provenances = [
        provenance for node in nodes for provenance in node.provenance
    ]
    if not provenances:
        return _resolve_loose_batch((), database, batch)
    source = provenances[0]
    return GraphBatch(
        _coerce_database(database, source.graph_batch)
        if database is not None
        else source.database,
        source.batch if batch is None else batch,
    )


def _resolve_loose_batch(
    paths: tuple[Path, ...],
    database: DatabaseIdentity | str | None,
    batch: str | None,
) -> GraphBatch:
    if database is not None and batch is not None:
        return GraphBatch(
            _coerce_database(
                database,
                None if not paths else paths[0].graph.batch,
            ),
            batch,
        )
    for path in paths:
        if path.graph.batch is not None:
            graph_batch = path.graph.batch
            return GraphBatch(
                _coerce_database(database, graph_batch)
                if database is not None
                else graph_batch.database,
                graph_batch.batch if batch is None else batch,
            )
        for relationship in path.relationships:
            if relationship.provenance:
                provenance = relationship.provenance[0]
                return GraphBatch(
                    _coerce_database(database, provenance.graph_batch)
                    if database is not None
                    else provenance.database,
                    provenance.batch if batch is None else batch,
                )
    raise ValueError(
        "a database identity and a batch name are needed to add paths"
    )


def _node_batch(
    node: Node, fallback: GraphBatch, explicit: bool = False
) -> GraphBatch:
    if explicit:
        return fallback
    if node.provenance:
        return node.provenance[0].graph_batch
    if node.graph.batch is not None:
        return node.graph.batch
    return fallback


def _relationship_batch(
    relationship: Relationship,
    fallback: GraphBatch,
    explicit: bool = False,
) -> GraphBatch:
    if explicit:
        return fallback
    if relationship.provenance:
        return relationship.provenance[0].graph_batch
    if relationship.graph.batch is not None:
        return relationship.graph.batch
    return fallback


def _direction_between(
    start_node: Node,
    end_node: Node,
    relationship: Relationship,
) -> int:
    """How a path traverses a relationship between two consecutive nodes."""
    actual_start = relationship.start_node
    actual_end = relationship.end_node
    if (
        actual_start is not None
        and actual_start is start_node
        and (actual_end is None or actual_end is end_node)
    ):
        return 1
    if (
        actual_end is not None
        and actual_end is start_node
        and (actual_start is None or actual_start is end_node)
    ):
        return -1
    raise ValueError(
        "relationship "
        f"{relationship.element_id!r} does not connect the path nodes"
    )


def _node_id(node: Node | None) -> str | None:
    return None if node is None else node.element_id


def _compatible_relationship(
    rel_index: dict[_RelKey, Relationship],
    batch: GraphBatch,
    element_id: str,
    type_: str,
    start_id: str | None,
    end_id: str | None,
) -> Relationship | None:
    """Find an existing entry this data adds to without contradicting it.

    A relationship first seen inside a path has no proven endpoints yet.
    Later data may fill them in.  Once endpoints are known, they must agree.
    """
    for (
        candidate_batch,
        candidate_id,
        candidate_start,
        candidate_end,
        candidate_type,
    ), candidate in rel_index.items():
        if (
            candidate_batch != batch
            or candidate_id != element_id
            or candidate_type != type_
        ):
            continue
        for known, new in (
            (candidate_start, start_id),
            (candidate_end, end_id),
        ):
            if known is not None and new is not None and known != new:
                break
        else:
            return candidate
    return None


def _fill_endpoints(
    relationship: Relationship,
    start_id: str | None,
    end_id: str | None,
    node_index: dict[_NodeKey, Node],
) -> None:
    if relationship.start_node is None and start_id is not None:
        relationship._start_node = _repoint_node(None, node_index, start_id)
    if relationship.end_node is None and end_id is not None:
        relationship._end_node = _repoint_node(None, node_index, end_id)


def _end_ids(
    relationship: Relationship,
) -> tuple[str | None, str | None]:
    return _node_id(relationship.start_node), _node_id(relationship.end_node)


def _rel_key(provenance: Provenance, relationship: Relationship) -> _RelKey:
    start_id, end_id = _end_ids(relationship)
    return (
        provenance.graph_batch,
        provenance.element_id,
        start_id,
        end_id,
        relationship.type,
    )


def _fallback_spec(path: Path) -> _PathSpec:
    start_batch = path.graph.batch or next(
        (
            relationship.provenance[0].graph_batch
            for relationship in path.relationships
            if relationship.provenance
        ),
        None,
    )
    if start_batch is None:
        start_batch = GraphBatch(_ANONYMOUS_DATABASE, "unresolved")
    nodes = tuple(
        (_node_batch(node, start_batch), node.element_id)
        for node in path.nodes
    )
    relationships = []
    for relationship in path.relationships:
        batch = _relationship_batch(relationship, start_batch)
        start_id, end_id = _end_ids(relationship)
        relationships.append(
            (
                batch,
                relationship.element_id,
                start_id,
                end_id,
                relationship.type,
            )
        )
    return _PathSpec(
        nodes,
        tuple(relationships),
        tuple(path.directions),
        source=path,
    )


def _clone_node(node: Node, graph: Graph) -> Node:
    return Node(
        graph,
        node.element_id,
        node._id,
        node.labels,
        dict(node._properties),
        provenances=node.provenance,
    )


def _clone_relationship(
    relationship: Relationship,
    graph: Graph,
    node_index: dict[_NodeKey, Node],
) -> Relationship:
    return _new_relationship(
        graph,
        relationship.type,
        relationship.element_id,
        relationship._id,
        dict(relationship._properties),
        relationship.provenance,
        relationship.start_node,
        relationship.end_node,
        node_index,
    )


def _new_relationship(
    graph: Graph,
    type_: str,
    element_id: str,
    id_: int,
    properties: dict[str, t.Any],
    provenances: tuple[Provenance, ...],
    start_node: Node | None,
    end_node: Node | None,
    node_index: dict[_NodeKey, Node],
    start_id: str | None = None,
    end_id: str | None = None,
) -> Relationship:
    cloned = graph.relationship_type(type_)(
        graph,
        element_id,
        id_,
        properties,
        provenances=provenances,
    )
    cloned._start_node = _repoint_node(start_node, node_index, start_id)
    cloned._end_node = _repoint_node(end_node, node_index, end_id)
    return cloned


def _repoint_node(
    node: Node | None,
    node_index: dict[_NodeKey, Node],
    element_id: str | None = None,
) -> Node | None:
    if node is not None:
        for provenance in node.provenance:
            resolved = node_index.get(
                (provenance.graph_batch, provenance.element_id)
            )
            if resolved is not None:
                return resolved
        if element_id is None:
            element_id = node.element_id
    if element_id is None:
        return None
    candidates = {
        candidate
        for (_, candidate_id), candidate in node_index.items()
        if candidate_id == element_id
    }
    if len(candidates) == 1:
        return candidates.pop()
    if not candidates:
        return None
    raise LookupError(
        f"node {element_id!r} is ambiguous without a graph batch"
    )
