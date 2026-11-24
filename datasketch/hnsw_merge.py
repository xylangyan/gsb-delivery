"""Evidence-capturing operation logs and deterministic merge replay for HNSW.

Two batch jobs can append document vectors to copies of the same base HNSW
index in parallel. Each branch records an :class:`OpRecord` per operation that
contains only the evidence the operation actually observed:

* the entry point read and the number of graph layers present,
* every node state read (point vector and soft-delete flag),
* every node-map and layer membership check,
* every adjacency read, reverse-edge lookup and whole-layer scan,
* the random generator state right before an automatic level draw,
* and the exact adjacency writes, deletions, layer and node changes committed.

At merge time the logs are replayed from the base index in one fixed total
order via :func:`replay_operation_log`. A recorded operation is reused only
when every piece of its evidence still matches the index produced by the
preceding operations. Its graph changes and random-state advancement are then
applied directly, and a guard makes sure no distance is evaluated. When the
evidence no longer holds, the operation is re-executed from the original entry
point instead. Every reuse or recompute decision, including its reason, is
returned in an audit trail.

The distance function is assumed deterministic and point vectors are fixed at
operation commit time, so matching structural evidence proves that the
operation would walk the same entry points, candidates and level decisions
again.
"""

from __future__ import annotations

import types
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

from datasketch.hnsw import HNSW, _Layer, _LayerWithReversedEdges, _Node


@dataclass
class OpRecord:
    """A single logged HNSW operation with the evidence needed to reuse it.

    Attributes:
        branch: Branch job that produced the operation.
        seq: Sequence number of the operation within the branch.
        op: Either ``"insert"`` or ``"remove"``.
        key: Document key targeted by the operation.
        point: Point vector committed by the operation. ``None`` for removes.
        ef: ef value used by the operation.
        level: Explicitly requested insert level. ``None`` for automatic.
        hard: Whether a remove was a hard remove.
        recovered: Whether an insert flipped an existing soft-deleted node back
            to live, which consumes no automatic level draw.
        rng_before: Random generator state captured before the automatic level
            draw, or ``None`` when no draw happened.
        entry_point: Entry point read at the start of the operation.
        layer_count: Number of graph layers present when the operation started.
        node_membership_reads: ``(key, present)`` for every node-map membership
            check.
        node_scans: Tuples of node keys observed while iterating the node map.
        node_reads: ``(key, point, is_deleted)`` for every node state read.
        edge_reads: ``(layer index, key, neighbor dict)`` for every adjacency
            read.
        reverse_edge_reads: ``(layer index, key, frozenset of reverse
            neighbors)`` for every reverse-edge index lookup on a layer that
            maintains reverse edges directly.
        membership_reads: ``(layer index, key, present)`` for every layer
            membership check.
        layer_scans: ``(layer index, tuple of keys)`` for every whole-layer
            iteration.
        edge_writes: ``(layer index, key, neighbor dict)`` for every adjacency
            overwrite committed.
        edge_deletes: ``(layer index, key)`` for every adjacency deletion.
        node_deletes: Keys of nodes removed from the index.
        layer_creates: Indices of graph layers created by the operation.
        layer_removes: Indices of graph layers removed by the operation.
        entry_point_after: Entry point after the operation.
        clears_index: Whether the operation left the index empty.
        distance_calls: Number of distance evaluations the operation made on
            the branch.
    """

    branch: str
    seq: int
    op: str
    key: Any
    point: Optional[np.ndarray]
    ef: int
    level: Optional[int]
    hard: bool
    recovered: bool
    rng_before: Optional[tuple]
    entry_point: Any
    layer_count: int
    node_membership_reads: list[tuple[Any, bool]] = field(default_factory=list)
    node_scans: list[tuple] = field(default_factory=list)
    node_reads: list[tuple[Any, np.ndarray, bool]] = field(default_factory=list)
    edge_reads: list[tuple[int, Any, dict[Any, float]]] = field(default_factory=list)
    reverse_edge_reads: list[tuple[int, Any, frozenset]] = field(default_factory=list)
    membership_reads: list[tuple[int, Any, bool]] = field(default_factory=list)
    layer_scans: list[tuple[int, tuple]] = field(default_factory=list)
    edge_writes: list[tuple[int, Any, dict[Any, float]]] = field(default_factory=list)
    edge_deletes: list[tuple[int, Any]] = field(default_factory=list)
    node_deletes: list[Any] = field(default_factory=list)
    layer_creates: list[int] = field(default_factory=list)
    layer_removes: list[int] = field(default_factory=list)
    entry_point_after: Any = None
    clears_index: bool = False
    distance_calls: int = 0

    def to_dict(self) -> dict:
        """Return a compact summary suitable for audit logs or serialization."""
        return {
            "branch": self.branch,
            "seq": self.seq,
            "op": self.op,
            "key": self.key,
            "hard": self.hard,
            "recovered": self.recovered,
            "entry_point": self.entry_point,
            "entry_point_after": self.entry_point_after,
            "layer_count": self.layer_count,
            "node_membership_reads": len(self.node_membership_reads),
            "node_scans": len(self.node_scans),
            "node_reads": len(self.node_reads),
            "edge_reads": len(self.edge_reads),
            "reverse_edge_reads": len(self.reverse_edge_reads),
            "membership_reads": len(self.membership_reads),
            "layer_scans": len(self.layer_scans),
            "edge_writes": len(self.edge_writes),
            "edge_deletes": len(self.edge_deletes),
            "node_deletes": len(self.node_deletes),
            "layer_creates": list(self.layer_creates),
            "layer_removes": list(self.layer_removes),
            "clears_index": self.clears_index,
            "distance_calls": self.distance_calls,
        }


@dataclass
class ReplayDecision:
    """The merge-time decision for one operation."""

    order: int
    branch: str
    seq: int
    op: str
    key: Any
    reused: bool
    reason: str
    distance_calls: int
    node_reads: int
    edge_reads: int

    def to_dict(self) -> dict:
        return {
            "order": self.order,
            "branch": self.branch,
            "seq": self.seq,
            "op": self.op,
            "key": self.key,
            "decision": "reused" if self.reused else "recomputed",
            "reason": self.reason,
            "distance_calls": self.distance_calls,
            "node_reads": self.node_reads,
            "edge_reads": self.edge_reads,
        }


@dataclass
class ReplayResult:
    """The merged index together with a per-operation audit trail."""

    index: HNSW
    decisions: list[ReplayDecision]

    @property
    def reused_count(self) -> int:
        return sum(1 for decision in self.decisions if decision.reused)

    @property
    def recomputed_count(self) -> int:
        return sum(1 for decision in self.decisions if not decision.reused)

    @property
    def distance_calls(self) -> int:
        return sum(decision.distance_calls for decision in self.decisions)

    def audit_rows(self) -> list[dict]:
        """Return one audit dictionary per replayed operation."""
        return [decision.to_dict() for decision in self.decisions]

    def audit_report(self) -> str:
        """Return a human-readable report of every reuse or recompute reason."""
        lines = [
            "order\tbranch\tseq\top\tkey\tdecision\tdistance_calls\treason",
        ]
        for decision in self.decisions:
            lines.append(
                f"{decision.order}\t{decision.branch}\t{decision.seq}\t"
                f"{decision.op}\t{decision.key}\t"
                f"{'reused' if decision.reused else 'recomputed'}\t"
                f"{decision.distance_calls}\t{decision.reason}"
            )
        lines.append(
            f"total: {len(self.decisions)} ops, {self.reused_count} reused, "
            f"{self.recomputed_count} recomputed, {self.distance_calls} distance calls"
        )
        return "\n".join(lines)


class _TracedRandom:
    """Proxy that snapshots the RNG state immediately before each draw."""

    def __init__(self, random_state: np.random.RandomState, tracer: _Tracer) -> None:
        self._random_state = random_state
        self._tracer = tracer

    def random_sample(self) -> float:
        record = self._tracer.record
        if record is not None and record.rng_before is None:
            record.rng_before = self._random_state.get_state()
        return self._random_state.random_sample()

    def get_state(self) -> tuple:
        return self._random_state.get_state()

    def set_state(self, state: tuple) -> None:
        self._random_state.set_state(state)


class _TracedNode(_Node):
    """Node that records every read of its point or soft-delete flag."""

    def __init__(self, node: _Node, tracer: _Tracer) -> None:
        super().__init__(
            node.key,
            object.__getattribute__(node, "point"),
            object.__getattribute__(node, "is_deleted"),
        )
        self._tracer = tracer

    def __getattribute__(self, name: str) -> Any:
        if name in ("point", "is_deleted"):
            tracer = object.__getattribute__(self, "_tracer")
            tracer.note_node_read(self)
        return object.__getattribute__(self, name)


class _TracedNodeMap(OrderedDict):
    """OrderedDict that records membership checks, scans and hard deletions."""

    def __init__(self, nodes: OrderedDict, tracer: _Tracer) -> None:
        super().__init__(nodes)
        self._tracer = tracer

    def __contains__(self, key: Any) -> bool:
        present = super().__contains__(key)
        record = self._tracer.record
        # Membership of the operation's own key is decided by the operation
        # itself, not observed from the pre-operation index.
        if record is not None and key != record.key:
            record.node_membership_reads.append((key, present))
        return present

    def __iter__(self) -> Iterator:
        record = self._tracer.record
        if record is not None:
            record.node_scans.append(tuple(super().__iter__()))
        return super().__iter__()

    def __reversed__(self) -> Iterator:
        record = self._tracer.record
        if record is not None:
            record.node_scans.append(tuple(reversed(list(super().__iter__()))))
        return super().__reversed__()

    def __setitem__(self, key: Any, value: _Node) -> None:
        if not isinstance(value, _TracedNode):
            value = _TracedNode(value, self._tracer)
        super().__setitem__(key, value)

    def __delitem__(self, key: Any) -> None:
        record = self._tracer.record
        if record is not None:
            record.node_deletes.append(key)
        super().__delitem__(key)


def _make_traced_layer_class(base_class: type, tracer: _Tracer) -> type:
    """Build a tracing layer class bound to one tracer instance."""

    class _TracedLayer(base_class):
        _tracer = tracer

        def __init__(self, key: Any) -> None:
            super().__init__(key)

        def _layer_idx(self) -> int:
            graphs = self._tracer.index._graphs
            return next(i for i, layer in enumerate(graphs) if layer is self)

        def _was_written(self, layer_idx: int, key: Any) -> bool:
            record = self._tracer.record
            return record is not None and any(
                written_layer == layer_idx and written_key == key
                for written_layer, written_key, _ in record.edge_writes
            )

        def __contains__(self, key: Any) -> bool:
            present = super().__contains__(key)
            record = self._tracer.record
            layer_idx = self._layer_idx()
            if record is not None and not self._was_written(layer_idx, key):
                record.membership_reads.append((layer_idx, key, present))
            return present

        def __getitem__(self, key: Any) -> dict:
            neighbors = super().__getitem__(key)
            record = self._tracer.record
            layer_idx = self._layer_idx()
            # Reads of adjacency the current operation already wrote observe
            # the operation's own changes, not pre-operation evidence.
            if record is not None and not self._was_written(layer_idx, key):
                record.edge_reads.append((layer_idx, key, dict(neighbors)))
            return neighbors

        def __setitem__(self, key: Any, value: dict) -> None:
            record = self._tracer.record
            if record is not None:
                record.edge_writes.append((self._layer_idx(), key, dict(value)))
            super().__setitem__(key, value)

        def __delitem__(self, key: Any) -> None:
            record = self._tracer.record
            if record is not None:
                record.edge_deletes.append((self._layer_idx(), key))
            super().__delitem__(key)

        def __iter__(self) -> Iterator:
            record = self._tracer.record
            if record is not None:
                record.layer_scans.append((self._layer_idx(), tuple(super().__iter__())))
            return super().__iter__()

        def get_reverse_edges(self, key: Any) -> set:
            record = self._tracer.record
            result = super().get_reverse_edges(key)
            if record is not None:
                layer_idx = self._layer_idx()
                if hasattr(self, "_reverse_edges"):
                    # The reversed-edge layer answers from a dedicated index,
                    # so only that lookup becomes evidence.
                    record.reverse_edge_reads.append((layer_idx, key, frozenset(result)))
                else:
                    # The plain layer scans every adjacency dict to answer.
                    for node_key in super().__iter__():
                        record.edge_reads.append(
                            (layer_idx, node_key, dict(super().__getitem__(node_key)))
                        )
            return result

    return _TracedLayer


class _Tracer:
    """Collects evidence for operations executed on one HNSW index instance."""

    def __init__(self, index: HNSW, branch: str) -> None:
        self.index = index
        self.branch = branch
        self.next_seq = 0
        self.record: Optional[OpRecord] = None
        self.distance_calls = 0
        self.original_distance_func = index._distance_func
        self.original_layer_class = index._layer_class
        # HNSW.copy() does not propagate the reversed_edges flag, so the actual
        # graph layers are a more reliable signal than _layer_class.
        if index._graphs:
            self.restore_layer_class = (
                _LayerWithReversedEdges
                if isinstance(index._graphs[0], _LayerWithReversedEdges)
                else _Layer
            )
        else:
            self.restore_layer_class = (
                _LayerWithReversedEdges
                if issubclass(index._layer_class, _LayerWithReversedEdges)
                else _Layer
            )
        self.original_random = index._random
        self.original_methods: dict[str, Any] = {}

    def note_node_read(self, node: _TracedNode) -> None:
        if self.record is None:
            return
        key = object.__getattribute__(node, "key")
        if key == self.record.key:
            # Reads of the operation's own node observe changes made by the
            # operation itself, not pre-operation evidence.
            return
        self.record.node_reads.append(
            (
                key,
                np.array(object.__getattribute__(node, "point"), copy=True),
                object.__getattribute__(node, "is_deleted"),
            )
        )

    def traced_distance(self, x: np.ndarray, y: np.ndarray) -> float:
        self.distance_calls += 1
        return self.original_distance_func(x, y)


class OperationRecorder:
    """Records every operation applied to an HNSW index.

    Args:
        index: The branch index to observe. It is mutated in place exactly as it
            would be without the recorder.
        branch: Branch name embedded in every record.

    Example:

        .. code-block:: python

            with OperationRecorder(index, "left") as recorder:
                index.insert("doc-a", vector_a)
                index.remove("doc-b", hard=True)
            records = recorder.records
    """

    def __init__(self, index: HNSW, branch: str) -> None:
        self._index = index
        self._branch = branch
        self.records: list[OpRecord] = []
        self._tracer = _Tracer(index, branch)
        self._installed = False

    def __enter__(self) -> OperationRecorder:
        self.install()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def __iter__(self) -> Iterator[OpRecord]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def install(self) -> None:
        """Attach tracing wrappers to the index. Idempotent."""
        if self._installed:
            return
        index = self._index
        tracer = self._tracer
        index._merge_recorder = self
        index._distance_func = tracer.traced_distance
        index._random = _TracedRandom(index._random, tracer)
        use_reversed = bool(index._graphs) and isinstance(index._graphs[0], _LayerWithReversedEdges)
        traced_layer_class = _make_traced_layer_class(
            _LayerWithReversedEdges if use_reversed else _Layer,
            tracer,
        )
        index._layer_class = traced_layer_class
        self._wrap_structures()
        tracer.original_methods["insert"] = index.__dict__.get("insert")
        tracer.original_methods["remove"] = index.__dict__.get("remove")
        index.insert = types.MethodType(_traced_insert, index)
        index.remove = types.MethodType(_traced_remove, index)
        self._installed = True

    def close(self) -> None:
        """Detach the recorder and restore the index's original objects."""
        if not self._installed:
            return
        index = self._index
        tracer = self._tracer
        index._distance_func = tracer.original_distance_func
        index._layer_class = tracer.restore_layer_class
        index._random = tracer.original_random
        index._nodes = OrderedDict((key, node.copy()) for key, node in index._nodes.items())
        index._graphs = [layer.copy() for layer in index._graphs]
        for name, original in tracer.original_methods.items():
            if original is None:
                delattr(index, name)
            else:
                setattr(index, name, original)
        del index._merge_recorder
        self._installed = False

    def _wrap_structures(self) -> None:
        index = self._index
        tracer = self._tracer
        wrapped_nodes = OrderedDict(
            (key, node if isinstance(node, _TracedNode) else _TracedNode(node, tracer))
            for key, node in index._nodes.items()
        )
        index._nodes = _TracedNodeMap(wrapped_nodes, tracer)
        traced_layers = []
        traced_class = index._layer_class
        for layer in index._graphs:
            if isinstance(layer, traced_class):
                traced_layers.append(layer)
                continue
            traced_layer = traced_class(None)
            traced_layer._graph = layer._graph
            if isinstance(layer, _LayerWithReversedEdges):
                traced_layer._reverse_edges = layer._reverse_edges
            traced_layers.append(traced_layer)
        index._graphs = traced_layers

    def _begin(
        self,
        op: str,
        key: Any,
        point: Optional[np.ndarray],
        ef: int,
        level: Optional[int],
        hard: bool,
        recovered: bool,
    ) -> None:
        index = self._index
        tracer = self._tracer
        self._wrap_structures()
        tracer.distance_calls = 0
        tracer.record = OpRecord(
            branch=self._branch,
            seq=tracer.next_seq,
            op=op,
            key=key,
            point=None if point is None else np.array(point, copy=True),
            ef=ef,
            level=level,
            hard=hard,
            recovered=recovered,
            rng_before=None,
            entry_point=index._entry_point,
            layer_count=len(index._graphs),
        )

    def _abort(self) -> None:
        self._tracer.record = None

    def _finalize(self) -> OpRecord:
        index = self._index
        tracer = self._tracer
        record = tracer.record
        before_count = record.layer_count
        after_count = len(index._graphs)
        if after_count > before_count:
            record.layer_creates = list(range(before_count, after_count))
        elif after_count < before_count:
            record.layer_removes = list(range(after_count, before_count))
        record.entry_point_after = index._entry_point
        record.clears_index = len(index._nodes) == 0 and len(index._graphs) == 0
        record.distance_calls = tracer.distance_calls
        tracer.record = None
        tracer.next_seq += 1
        self.records.append(record)
        return record


def _traced_insert(
    self: HNSW,
    key: Any,
    new_point: np.ndarray,
    ef: Optional[int] = None,
    level: Optional[int] = None,
) -> None:
    recorder = self._merge_recorder
    if ef is None:
        ef = self._ef_construction
    recovered = key in self._nodes and self._nodes[key].is_deleted
    recorder._begin("insert", key, new_point, ef, level, False, recovered)
    try:
        HNSW.insert(self, key, new_point, ef=ef, level=level)
    except Exception:
        recorder._abort()
        raise
    else:
        recorder._finalize()


def _traced_remove(
    self: HNSW,
    key: Any,
    hard: bool = False,
    ef: Optional[int] = None,
) -> None:
    if not self._nodes or key not in self._nodes:
        raise KeyError(key)
    recorder = self._merge_recorder
    if ef is None:
        ef = self._ef_construction
    recorder._begin("remove", key, None, ef, None, hard, False)
    try:
        HNSW.remove(self, key, hard=hard, ef=ef)
    except Exception:
        recorder._abort()
        raise
    else:
        recorder._finalize()


def record_operations(index: HNSW, branch: str) -> OperationRecorder:
    """Start recording operations on a branch index.

    Args:
        index: The HNSW index to observe.
        branch: Branch name embedded in every produced record.

    Returns:
        OperationRecorder: Installed recorder. Prefer using it as a context
        manager; call :meth:`OperationRecorder.close` to detach it.
    """
    recorder = OperationRecorder(index, branch)
    recorder.install()
    return recorder


def _edge_matches(layer: _Layer, key: Any, expected: dict[Any, float]) -> tuple[bool, str]:
    if key not in layer:
        return False, "missing adjacency"
    actual = layer[key]
    if set(actual) != set(expected):
        return False, "neighbor set"
    for neighbor, dist in expected.items():
        if actual[neighbor] != dist:
            return False, "edge distance"
    return True, ""


def _verify_evidence(index: HNSW, record: OpRecord) -> tuple[bool, str]:
    """Check all recorded evidence against the current merge index.

    The check is purely structural: it never evaluates the distance function.
    """
    if index._entry_point != record.entry_point:
        return False, "entry point changed since the operation was recorded"
    if len(index._graphs) != record.layer_count:
        return False, "number of graph layers changed since the operation was recorded"
    for key, present in record.node_membership_reads:
        if (key in index._nodes) != present:
            return False, f"node-map membership of {key} changed"
    for keys in record.node_scans:
        if set(index._nodes) != set(keys):
            return False, "node-map membership observed during a scan changed"
    for layer_idx, keys in record.layer_scans:
        if set(index._graphs[layer_idx]) != set(keys):
            return False, f"membership of scanned layer {layer_idx} changed"
    for layer_idx, key, present in record.membership_reads:
        actual_present = key in index._graphs[layer_idx]
        if actual_present != present:
            return False, f"membership of {key} in layer {layer_idx} changed"
    for layer_idx, key, neighbors in record.edge_reads:
        matches, what = _edge_matches(index._graphs[layer_idx], key, neighbors)
        if not matches:
            return False, f"adjacency {what} of {key} in layer {layer_idx} changed"
    for layer_idx, key, reverse_neighbors in record.reverse_edge_reads:
        actual = index._graphs[layer_idx].get_reverse_edges(key)
        if frozenset(actual) != reverse_neighbors:
            return False, f"reverse edges of {key} in layer {layer_idx} changed"
    for key, point, is_deleted in record.node_reads:
        node = index._nodes.get(key)
        if node is None:
            return False, f"node {key} read by the operation no longer exists"
        if node.is_deleted != is_deleted:
            return False, f"soft-delete state of {key} changed"
        if not np.array_equal(node.point, point):
            return False, f"point vector of {key} changed"
    return True, "all recorded entry points, candidates and adjacency still match"


class _DistanceGuard:
    """Raises if any distance is evaluated while a record is reused."""

    def __call__(self, x: np.ndarray, y: np.ndarray) -> float:
        raise RuntimeError("Reused operations must not evaluate the distance function")


class _CountingDistance:
    def __init__(self, distance_func: Callable) -> None:
        self.distance_func = distance_func
        self.calls = 0

    def __call__(self, x: np.ndarray, y: np.ndarray) -> float:
        self.calls += 1
        return self.distance_func(x, y)


def _apply_record(index: HNSW, record: OpRecord, distance_func: Callable) -> None:
    """Replay the committed changes and RNG advancement of one record.

    A distance guard guarantees reuse comes for free: no distance is evaluated
    while the recorded graph changes are re-applied.
    """
    index._distance_func = _DistanceGuard()
    try:
        if record.clears_index:
            index.clear()
            return

        if record.op == "insert":
            if record.key in index._nodes:
                if record.recovered:
                    index._nodes[record.key].is_deleted = False
                index._nodes[record.key].point = record.point
            else:
                index._nodes[record.key] = _Node(record.key, record.point)
        else:
            index._nodes[record.key].is_deleted = True

        if record.rng_before is not None:
            # Restore the pre-draw state and consume the same single draw so
            # the automatic level selection advances the RNG identically.
            index._random.set_state(record.rng_before)
            index._random.random_sample()

        # Entry-point reassignment pops every layer that became empty. The
        # recorded range is always the top of the stack and contiguous.
        for _ in record.layer_removes:
            index._graphs.pop()

        for layer_idx in record.layer_creates:
            if layer_idx >= len(index._graphs):
                index._graphs.append(index._layer_class(record.key))
                index._entry_point = record.key

        for layer_idx, key, neighbors in record.edge_writes:
            index._graphs[layer_idx][key] = dict(neighbors)
        for layer_idx, key in record.edge_deletes:
            del index._graphs[layer_idx][key]
        for key in record.node_deletes:
            del index._nodes[key]

        index._entry_point = record.entry_point_after
    finally:
        index._distance_func = distance_func


def _recompute(index: HNSW, record: OpRecord) -> int:
    counter = _CountingDistance(index._distance_func)
    index._distance_func = counter
    try:
        if record.op == "insert":
            HNSW.insert(index, record.key, record.point, ef=record.ef, level=record.level)
        else:
            HNSW.remove(index, record.key, hard=record.hard, ef=record.ef)
    finally:
        index._distance_func = counter.distance_func
    return counter.calls


def _normalize_layer_class(index: HNSW) -> None:
    # HNSW.copy() does not propagate reversed_edges, so make _layer_class match
    # the actual layer objects before any new layer can be created.
    if index._graphs and isinstance(index._graphs[0], _LayerWithReversedEdges):
        index._layer_class = _LayerWithReversedEdges
    elif index._graphs:
        index._layer_class = _Layer


def _index_logs(logs: Mapping[str, Sequence[OpRecord]]) -> dict[tuple[str, int], OpRecord]:
    indexed = {}
    for branch, records in logs.items():
        for record in records:
            if record.branch != branch:
                raise ValueError(
                    f"Record branch {record.branch!r} does not match log branch {branch!r}"
                )
            identity = (branch, record.seq)
            if identity in indexed:
                raise ValueError(f"Duplicate sequence number {record.seq} in branch {branch!r}")
            indexed[identity] = record
    return indexed


def _prepare_rng(reference: HNSW, record: OpRecord) -> None:
    # The automatic level draw is part of the operation's committed evidence,
    # so the reference replay restores the same pre-draw RNG state before each
    # operation that drew a level. This keeps branch interleavings and draw
    # counts (e.g. soft-delete recovery draws nothing) deterministic.
    if record.rng_before is not None:
        reference._random.set_state(record.rng_before)


def replay_operation_log(
    base: HNSW,
    logs: Mapping[str, Sequence[OpRecord]],
    total_order: Iterable[tuple[str, int]],
) -> ReplayResult:
    """Replay branch logs in a fixed total order, reusing evidence when valid.

    Args:
        base: The original index both branch jobs started from. It is not
            mutated; a copy is replayed into.
        logs: Mapping of branch name to the ordered records produced by that
            branch.
        total_order: ``(branch name, branch sequence number)`` pairs giving the
            definitive global order of operations.

    Returns:
        ReplayResult: The merged index and an audit decision for every step.
    """
    indexed = _index_logs(logs)
    merged = base.copy()
    _normalize_layer_class(merged)
    distance_func = merged._distance_func
    decisions: list[ReplayDecision] = []
    for order, (branch, seq) in enumerate(total_order):
        record = indexed.get((branch, seq))
        if record is None:
            raise KeyError(f"No record found for branch {branch!r} seq {seq}")
        valid, reason = _verify_evidence(merged, record)
        if valid:
            _apply_record(merged, record, distance_func)
            distance_calls = 0
            reused = True
        else:
            _prepare_rng(merged, record)
            distance_calls = _recompute(merged, record)
            reused = False
        decisions.append(
            ReplayDecision(
                order=order,
                branch=branch,
                seq=seq,
                op=record.op,
                key=record.key,
                reused=reused,
                reason=reason,
                distance_calls=distance_calls,
                node_reads=len(record.node_reads),
                edge_reads=len(record.edge_reads),
            )
        )
    return ReplayResult(index=merged, decisions=decisions)


def full_replay(
    base: HNSW,
    logs: Mapping[str, Sequence[OpRecord]],
    total_order: Iterable[tuple[str, int]],
) -> HNSW:
    """Re-execute every operation from the base index in the total order.

    This is the reference implementation: no evidence is reused and every
    distance is recomputed. Each operation still restores its recorded
    pre-draw random state before an automatic level selection, so the draw is
    part of the deterministic operation semantics rather than of branch-local
    generator timing. Its result must match :func:`replay_operation_log`.
    """
    indexed = _index_logs(logs)
    reference = base.copy()
    _normalize_layer_class(reference)
    for branch, seq in total_order:
        record = indexed.get((branch, seq))
        if record is None:
            raise KeyError(f"No record found for branch {branch!r} seq {seq}")
        _prepare_rng(reference, record)
        if record.op == "insert":
            HNSW.insert(reference, record.key, record.point, ef=record.ef, level=record.level)
        else:
            HNSW.remove(reference, record.key, hard=record.hard, ef=record.ef)
    return reference


def index_snapshot(index: HNSW) -> dict:
    """Capture every externally observable structure named in the merge spec.

    Returns a dictionary with live keys, soft-deleted but still traversable
    keys, per-layer adjacency, entry point and random generator state.
    """
    live_keys = {key for key, node in index._nodes.items() if not node.is_deleted}
    soft_deleted_keys = {key for key, node in index._nodes.items() if node.is_deleted}
    layers = [{key: dict(neighbors) for key, neighbors in layer._graph.items()} for layer in index._graphs]
    return {
        "live_keys": live_keys,
        "soft_deleted_keys": soft_deleted_keys,
        "nodes": {
            key: (np.array(node.point, copy=True), node.is_deleted) for key, node in index._nodes.items()
        },
        "layers": layers,
        "entry_point": index._entry_point,
        "random_state": index._random.get_state(),
    }


def snapshots_match(left: HNSW, right: HNSW) -> bool:
    """Return whether two indexes have identical nodes, graphs and RNG state."""
    return left == right


def compare_query_behavior(
    left: HNSW,
    right: HNSW,
    queries: Sequence[np.ndarray],
    k: int = 10,
    ef: Optional[int] = None,
) -> list[list[tuple[Any, float]]]:
    """Run the same queries against two indexes and assert identical results.

    Returns the query results from ``left``. Raises ``AssertionError`` if the
    two indexes disagree on any query.
    """
    all_results = []
    for query in queries:
        left_results = left.query(query, k=k, ef=ef)
        right_results = right.query(query, k=k, ef=ef)
        if left_results != right_results:
            raise AssertionError(
                f"Query behavior diverged:\nleft={left_results}\nright={right_results}"
            )
        all_results.append(left_results)
    return all_results
