"""Runtime instrumentation behind the provenance entry points.

The tracer is attached to a :class:`~json_repair.json_parser.JSONParser` and its
:class:`~json_repair.schema_repair.SchemaRepairer` only when one of the
provenance entry points is used. Regular reads keep the shared no-op instance,
whose methods do nothing, so the parsing hot path is unchanged.

Two layers are correlated structurally by JSON path:

* the parser records the tight character interval and the pre-repair value of
  every node it reads, including the source position of array items and the
  original key of object members;
* the repairer records how each output node was produced (coerced, defaulted,
  dropped) and which resolved schema branch was effective.

Joining by path rather than by value is what keeps two identical values such as
two ``7`` occurrences pointing at their own source interval. Salvage drops only
remove output positions: the original array index of every surviving item is
recovered through the per-array remap recorded at drop time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .provenance import DroppedItem, Provenance, ProvenanceRecord, RejectedFragment, SchemaRef, SourceSpan
from .utils.constants import JSONReturnType

if TYPE_CHECKING:
    from .schema_repair import SchemaRepairer


@dataclass
class _RawSpan:
    """Parser-side evidence for a node before schema repair."""

    path: str
    start: int
    end: int
    value: Any
    original_key: str | None = None
    original_index: int | None = None


@dataclass
class _RepairFrame:
    """Repairer-side frame for one ``repair_value`` call."""

    path: str
    input_value: Any
    schema_ref: SchemaRef | None
    origin: str = "source"
    actions: list[str] = field(default_factory=list)


class _NoopTracer:
    """Shared tracer used by every ordinary parse call."""

    collects_provenance = False
    fragment_index = 0

    def mark_pending(self, path: str, index: int) -> None:
        return None

    def mark_pending_index(self, path: str, source_index: int) -> None:
        return None

    def commit_span(self, path: str, value: Any, end_index: int) -> None:
        return None

    def discard_pending(self, path: str) -> None:
        return None

    def retract(self, path: str) -> None:
        return None

    def record_member(self, container_path: str, key: str, key_start: int, key_end: int, value_path: str) -> None:
        return None

    def begin_fragment(self, start_index: int) -> None:
        return None

    def end_fragment(self, end_index: int) -> None:
        return None

    def reject_fragment(self, reason: str, value: JSONReturnType) -> None:
        return None

    def select_fragment(self) -> None:
        return None

    def enter_node(self, path: str, value: Any, schema: Any, repairer: "SchemaRepairer | None") -> None:
        return None

    def label(self, origin: str | None = None, action: str | None = None) -> None:
        return None

    def mark_origin(self, path: str, origin: str, schema: Any = None, repairer: Any = None) -> None:
        return None

    def exit_node(self, output: Any) -> None:
        return None

    def fail_node(self) -> None:
        return None

    def discard_frames(self) -> None:
        return None

    def map_alias(self, output_path: str, source_path: str, original_key: str) -> None:
        return None

    def mark_dropped_item(self, path: str, source_index: int, reason: str) -> None:
        return None

    def mark_repair_drop(self, path: str, parser_order: int, output_index: int, reason: str) -> None:
        return None

    def source_index_for(self, path: str, parser_order: int) -> int:
        return parser_order

    def attach_source_text(self, source_text: str) -> None:
        return None

    def reconcile(self, output: Any) -> None:
        return None

    def build_record(self, source_text: str | None = None, output: Any = None) -> ProvenanceRecord:
        return ProvenanceRecord(entries={}, rejected_fragments=[], source_text=source_text or "")


NOOP_TRACER = _NoopTracer()


class ProvenanceTracer:
    collects_provenance = True

    def __init__(self) -> None:
        self.fragment_index = 0
        self._fragment_start: int | None = None
        self._fragment_spans: list[SourceSpan] = []
        # Pending value start per parse path, refreshed at every parser loop.
        self._pending: dict[str, int] = {}
        # Source array index per item path; survives span re-commits and drops.
        self._source_indices: dict[str, int] = {}
        # Raw spans of the fragment currently being parsed.
        self._raw_spans: dict[str, _RawSpan] = {}
        # Raw spans of the selected fragment, kept for joining with repairs.
        self._selected_spans: dict[str, _RawSpan] = {}
        self._rejected: list[RejectedFragment] = []
        self._frames: list[_RepairFrame] = []
        self._entries: dict[str, Provenance] = {}
        # Output path -> parse path, for schema alias renames.
        self._alias_paths: dict[str, str] = {}
        self._dropped_items: list[DroppedItem] = []
        # For each array: the source index of every parser-emitted item, in the
        # order the parser produced it. Parser-layer drops never enter this list.
        self._array_source_order: dict[str, list[int]] = {}
        # Parser-order indices removed later by salvage repair, per array.
        self._repair_dropped_order: dict[str, set[int]] = {}
        # Explicit origin markers keyed by path (defaults, const, enum, inferred).
        self._origin_markers: dict[str, str] = {}
        self._origin_schema_refs: dict[str, SchemaRef] = {}
        self._selected_fragment = 0
        self._source_text: str | None = None

    def attach_source_text(self, source_text: str) -> None:
        self._source_text = source_text

    # -- parser hooks -------------------------------------------------------

    def mark_pending(self, path: str, index: int) -> None:
        self._pending[path] = index

    def mark_pending_index(self, path: str, source_index: int) -> None:
        self._source_indices[path] = source_index
        array_path, _ = _split_array_path(path)
        if array_path is not None:
            self._array_source_order.setdefault(array_path, []).append(source_index)

    def commit_span(self, path: str, value: Any, end_index: int) -> None:
        start = self._pending.pop(path, end_index)
        # Re-parses (empty-object recovery, missing-brace insertion) replace the
        # earlier attempt at the same path, but array source indices are recorded
        # separately and must survive the replacement.
        previous = self._raw_spans.get(path)
        self._raw_spans[path] = _RawSpan(
            path=path,
            start=start,
            end=end_index,
            value=value,
            original_key=previous.original_key if previous is not None else None,
            original_index=self._source_indices.get(path, previous.original_index if previous is not None else None),
        )

    def discard_pending(self, path: str) -> None:
        self._pending.pop(path, None)

    def retract(self, path: str) -> None:
        # Stray or empty array items are not part of the output.
        self._pending.pop(path, None)
        self._raw_spans.pop(path, None)
        self._source_indices.pop(path, None)
        array_path, source_index = _split_array_path(path)
        if array_path is not None and source_index is not None:
            order = self._array_source_order.get(array_path)
            if order is not None and source_index in order:
                order.remove(source_index)

    def record_member(self, container_path: str, key: str, key_start: int, key_end: int, value_path: str) -> None:
        raw = self._raw_spans.get(value_path)
        if raw is None:
            raw = _RawSpan(path=value_path, start=key_start, end=key_end, value=None)
            self._raw_spans[value_path] = raw
        raw.original_key = key

    def begin_fragment(self, start_index: int) -> None:
        self._fragment_start = start_index

    def end_fragment(self, end_index: int) -> None:
        if self._fragment_start is not None:
            self._fragment_spans.append(
                SourceSpan(start=self._fragment_start, end=end_index, fragment_index=self.fragment_index)
            )
        self._fragment_start = None

    def reject_fragment(self, reason: str, value: JSONReturnType) -> None:
        span = self._fragment_spans[-1] if self._fragment_spans else SourceSpan(0, 0, self.fragment_index)
        self._rejected.append(
            RejectedFragment(fragment_index=self.fragment_index, span=span, value=value, reason=reason)
        )
        # All evidence collected for the rejected fragment belongs to that
        # fragment only; the next fragment must start from a clean slate.
        self._entries.clear()
        self._raw_spans.clear()
        self._pending.clear()
        self._source_indices.clear()
        self._origin_markers.clear()
        self._origin_schema_refs.clear()
        self._alias_paths.clear()
        self._array_source_order.clear()
        self._repair_dropped_order.clear()
        self._dropped_items.clear()
        self.fragment_index += 1

    def select_fragment(self) -> None:
        self._selected_spans = dict(self._raw_spans)
        self._selected_fragment = self.fragment_index

    # -- repairer hooks -----------------------------------------------------

    def enter_node(self, path: str, value: Any, schema: Any, repairer: "SchemaRepairer | None") -> None:
        schema_ref = None
        if repairer is not None and isinstance(schema, dict):
            try:
                resolved = repairer.resolve_schema(schema)
            except ValueError:
                resolved = schema
            if isinstance(resolved, dict):
                schema_ref = SchemaRef(path=path, schema_id=id(resolved))
        self._frames.append(_RepairFrame(path=path, input_value=value, schema_ref=schema_ref))

    def label(self, origin: str | None = None, action: str | None = None) -> None:
        if not self._frames:
            return
        frame = self._frames[-1]
        if origin is not None:
            frame.origin = origin
            self._origin_markers[frame.path] = origin
        if action is not None and action not in frame.actions:
            frame.actions.append(action)

    def mark_origin(
        self,
        path: str,
        origin: str,
        schema: Any = None,
        repairer: "SchemaRepairer | None" = None,
    ) -> None:
        self._origin_markers[path] = origin
        schema_ref = self._schema_ref(schema, path, repairer)
        if schema_ref is not None:
            self._origin_schema_refs[path] = schema_ref
        for frame in reversed(self._frames):
            if frame.path == path:
                frame.origin = origin
                if schema_ref is not None:
                    frame.schema_ref = schema_ref
                return

    @staticmethod
    def _schema_ref(schema: Any, path: str, repairer: "SchemaRepairer | None") -> SchemaRef | None:
        if not isinstance(schema, dict):
            return None
        resolved: Any = schema
        if repairer is not None:
            try:
                resolved = repairer.resolve_schema(schema)
            except ValueError:
                resolved = schema
        if isinstance(resolved, dict):
            return SchemaRef(path=path, schema_id=id(resolved))
        return None

    def fail_node(self) -> None:
        if self._frames:
            self._frames.pop()

    def discard_frames(self) -> None:
        # A rejected salvage fragment may carry nested frames from partial
        # coercion attempts; none of them describe output values.
        self._frames.clear()

    def _find_raw(self, path: str) -> _RawSpan | None:
        # Entries are built while the parser runs, but the selected-fragment
        # snapshot is only taken afterwards in non-salvage mode, so consult both.
        return self._selected_spans.get(path) or self._raw_spans.get(path)

    def exit_node(self, output: Any) -> None:
        frame = self._frames.pop()
        source_path = self._alias_paths.get(frame.path, frame.path)
        raw = self._lookup_raw_span(source_path, frame.path)
        origin = self._origin_markers.get(frame.path, frame.origin)
        if raw is None and origin == "source":
            # No parser evidence: the value was synthesized by schema rules.
            origin = "inferred"
        original_key = frame.path.rsplit(".", 1)[-1] if "." in frame.path else frame.path
        original_index = None
        original_value = frame.input_value
        span = None
        if raw is not None:
            span = SourceSpan(start=raw.start, end=raw.end, fragment_index=self._selected_fragment)
            original_value = raw.value
            if raw.original_key is not None:
                original_key = raw.original_key
            original_index = self._recover_original_index(frame.path, raw)
        type_changed = (
            raw is not None and type(output) is not type(raw.value) and not _same_json_type_family(output, raw.value)
        )
        entry = Provenance(
            path=frame.path,
            origin=origin,  # type: ignore[arg-type]
            original_key=original_key,
            original_value=original_value,
            span=span,
            type_changed=type_changed,
            schema_ref=frame.schema_ref,
            actions=tuple(frame.actions),
            original_index=original_index,
        )
        # The same node is repaired twice in the parser-then-validator pipeline;
        # merge within a fragment, but a later selected fragment replaces the
        # stale evidence collected for an earlier (possibly rejected) fragment.
        previous_entry = self._entries.get(frame.path)
        if previous_entry is not None and not self._belongs_to_new_fragment(previous_entry, entry):
            entry = self._merge_entries(previous_entry, entry)
        self._entries[frame.path] = entry

    def _belongs_to_new_fragment(self, existing: Provenance, replacement: Provenance) -> bool:
        existing_fragment = existing.span.fragment_index if existing.span is not None else None
        replacement_fragment = replacement.span.fragment_index if replacement.span is not None else None
        return existing_fragment != replacement_fragment

    @staticmethod
    def _merge_entries(existing: Provenance, replacement: Provenance) -> Provenance:
        actions = tuple(dict.fromkeys((*existing.actions, *replacement.actions)))
        return Provenance(
            path=existing.path,
            origin=replacement.origin if replacement.origin != "source" else existing.origin,
            original_key=existing.original_key,
            original_value=existing.original_value
            if existing.original_value is not None
            else replacement.original_value,
            span=existing.span or replacement.span,
            type_changed=existing.type_changed or replacement.type_changed,
            schema_ref=existing.schema_ref or replacement.schema_ref,
            actions=actions,
            original_index=existing.original_index
            if existing.original_index is not None
            else replacement.original_index,
        )

    def _lookup_raw_span(self, source_path: str, output_path: str) -> _RawSpan | None:
        # Repair frames use compact output indices after salvage drops, while
        # parser spans keep source positions. Rewrite every array segment of the
        # output path so nested objects inside dropped arrays resolve correctly.
        rewritten = self._output_path_to_source_path(output_path)
        return self._find_raw(rewritten) or self._find_raw(source_path)

    def _output_path_to_source_path(self, output_path: str) -> str:
        segments = _split_json_path(output_path)
        source_segments: list[str] = []
        prefix = "$"
        for segment in segments:
            index = _array_index_from_segment(segment)
            if index is not None:
                source_index = self._output_to_source_index(prefix, index)
                source_segments.append(f"[{source_index}]")
                prefix = f"{prefix}[{source_index}]"
            else:
                source_segments.append(f".{segment}")
                prefix = f"{prefix}.{segment}"
        return "$" + "".join(source_segments)

    def _surviving_source_indices(self, array_path: str) -> list[int]:
        """Source indices of parser-emitted items minus salvage repair drops."""
        order = self._array_source_order.get(array_path, [])
        dropped_orders = self._repair_dropped_order.get(array_path, set())
        return [source_index for order_index, source_index in enumerate(order) if order_index not in dropped_orders]

    def _is_dropped(self, array_path: str, source_index: int) -> bool:
        return source_index not in self._surviving_source_indices(array_path)

    def _output_to_source_index(self, array_path: str, output_index: int) -> int:
        survivors = self._surviving_source_indices(array_path)
        if output_index < len(survivors):
            return survivors[output_index]
        # Defensive fallback for nested frames produced after the array loop.
        order = self._array_source_order.get(array_path, [])
        return order[output_index] if output_index < len(order) else output_index

    def _source_to_output_index(self, array_path: str, source_index: int) -> int | None:
        survivors = self._surviving_source_indices(array_path)
        try:
            return survivors.index(source_index)
        except ValueError:
            return None

    def _recover_original_index(self, output_path: str, raw: _RawSpan) -> int | None:
        if raw.original_index is not None:
            return raw.original_index
        array_path, output_index = _split_array_path(output_path)
        if array_path is None or output_index is None:
            return None
        if array_path in self._array_source_order:
            return self._output_to_source_index(array_path, output_index)
        return output_index

    def map_alias(self, output_path: str, source_path: str, original_key: str) -> None:
        self._alias_paths[output_path] = source_path

    def mark_dropped_item(self, path: str, source_index: int, reason: str) -> None:
        """Record a drop made by the parser before schema repair (e.g. tuple tail)."""
        raw = self._find_raw(f"{path}[{source_index}]")
        span = (
            SourceSpan(start=raw.start, end=raw.end, fragment_index=self._selected_fragment)
            if raw is not None
            else None
        )
        self._dropped_items.append(DroppedItem(path=path, original_index=source_index, reason=reason, span=span))
        order = self._array_source_order.get(path)
        if order is not None and source_index in order:
            order.remove(source_index)

    def mark_repair_drop(self, path: str, parser_order: int, output_index: int, reason: str) -> None:
        """Record a salvage repair drop, indexed by its position among parser items."""
        order = self._array_source_order.get(path, [])
        source_index = order[parser_order] if parser_order < len(order) else parser_order
        raw = self._find_raw(f"{path}[{source_index}]")
        span = (
            SourceSpan(start=raw.start, end=raw.end, fragment_index=self._selected_fragment)
            if raw is not None
            else None
        )
        self._dropped_items.append(DroppedItem(path=path, original_index=source_index, reason=reason, span=span))
        self._repair_dropped_order.setdefault(path, set()).add(parser_order)

    def source_index_for(self, path: str, parser_order: int) -> int:
        """Map a parser-order index (the indices used inside schema repair) to its source index."""
        order = self._array_source_order.get(path)
        if order is None or parser_order >= len(order):
            return parser_order
        return order[parser_order]

    # -- output -------------------------------------------------------------

    def build_record(self, source_text: str | None = None, output: Any = None) -> ProvenanceRecord:
        self._backfill_plain_reads()
        if output is not None:
            self.reconcile(output)
        return ProvenanceRecord(
            entries=dict(self._entries),
            rejected_fragments=list(self._rejected),
            source_text=source_text if source_text is not None else (self._source_text or ""),
            fragment_spans=list(self._fragment_spans),
            dropped_items=list(self._dropped_items),
        )

    def reconcile(self, output: Any) -> None:
        """Ensure every node of the final output carries a provenance entry.

        Schema-only nodes (defaults, const/enum fills, inferred containers) do
        not have a parser frame of their own, so walk the output and synthesize
        entries from the recorded origin markers.
        """
        self._reconcile_node("$", output)

    def _reconcile_node(self, path: str, output: Any) -> None:
        if path not in self._entries:
            origin = self._origin_markers.get(path, "inferred")
            schema_ref = self._origin_schema_refs.get(path) or self._nearest_schema_ref(path)
            self._entries[path] = Provenance(
                path=path,
                origin=origin,  # type: ignore[arg-type]
                original_key=_leaf_key(path),
                original_value=None,
                schema_ref=schema_ref,
            )
        if isinstance(output, dict):
            for key, child in output.items():
                self._reconcile_node(f"{path}.{key}", child)
        elif isinstance(output, list):
            for index, child in enumerate(output):
                self._reconcile_node(f"{path}[{index}]", child)

    def _nearest_schema_ref(self, path: str) -> SchemaRef | None:
        current = path
        while current:
            frame_entry = self._entries.get(current)
            if frame_entry is not None and frame_entry.schema_ref is not None:
                return frame_entry.schema_ref
            if "." in current:
                current = current.rsplit(".", 1)[0]
            elif "[" in current:
                current = current[: current.rfind("[")]
            else:
                break
        return None

    def _source_path_to_output_path(self, source_path: str) -> str | None:
        segments = _split_json_path(source_path)
        output_segments: list[str] = []
        prefix = "$"
        for segment in segments:
            index = _array_index_from_segment(segment)
            if index is not None:
                output_index = self._source_to_output_index(prefix, index)
                if output_index is None:
                    return None
                output_segments.append(f"[{output_index}]")
                prefix = f"{prefix}[{output_index}]"
            else:
                output_segments.append(f".{segment}")
                prefix = f"{prefix}.{segment}"
        return "$" + "".join(output_segments)

    def _backfill_plain_reads(self) -> None:
        # Without schema guidance every node was committed by the parser but no
        # repair frame ran, so turn raw spans into source provenance directly.
        for path, raw in self._selected_spans.items():
            if path in self._entries:
                continue
            array_path, source_index = _split_array_path(path)
            if array_path is not None and source_index is not None and self._is_dropped(array_path, source_index):
                continue
            output_path = self._source_path_to_output_path(path)
            if output_path is None:
                continue
            self._entries[output_path] = Provenance(
                path=output_path,
                origin="source",
                original_key=raw.original_key if raw.original_key is not None else _leaf_key(output_path),
                original_value=raw.value,
                span=SourceSpan(start=raw.start, end=raw.end, fragment_index=self._selected_fragment),
                type_changed=False,
                original_index=raw.original_index,
            )


def _leaf_key(path: str) -> str:
    if "." in path:
        return path.rsplit(".", 1)[-1]
    return path


def _split_array_path(path: str) -> tuple[str | None, int | None]:
    if not path.endswith("]"):
        return None, None
    opening = path.rfind("[")
    if opening == -1:
        return None, None
    index_token = path[opening + 1 : -1]
    if not index_token.isdigit():
        return None, None
    return path[:opening], int(index_token)


_PATH_SEGMENT = re.compile(r"\.([^.[\]]+)|\[(\d+)\]")


def _split_json_path(path: str) -> list[str | int]:
    """Split a JSON path into ordered key/index segments (root ``$`` excluded)."""
    segments: list[str | int] = []
    for match in _PATH_SEGMENT.finditer(path):
        if match.group(1) is not None:
            segments.append(match.group(1))
        else:
            segments.append(int(match.group(2)))
    return segments


def _array_index_from_segment(segment: str | int) -> int | None:
    return segment if isinstance(segment, int) else None


def _same_json_type_family(left: Any, right: Any) -> bool:
    # int/float are both JSON numbers and coercions between them are not type changes.
    left_is_number = isinstance(left, (int, float)) and not isinstance(left, bool)
    right_is_number = isinstance(right, (int, float)) and not isinstance(right, bool)
    return left_is_number and right_is_number


# Common structural type shared by the real tracer and the parser's no-op default.
Tracer = ProvenanceTracer | _NoopTracer
