"""Two-round shipping-slip reconciliation.

Domain mapping (from the warehouse wording in the task statement):

- A shipment row (发货行) is one ticket; the bin slot on the index may be
  repeated across rows, so rows are never merged by slot.
- The Arrow parts list of a row can be in three raw states that must be kept
  distinct: a missing (null) list, an empty list, or a list containing a
  missing (null) part element.  A live part with an unrecorded field is not a
  missing part.
- Round one flattens the Arrow parts list (Akimbo) and issues correction
  slips against original slot positions.  A slip may correct fields, remove
  one original part, or insert immediately before/after an original slot.
  An anchor that is itself removed still denotes that original slot.
- Round two is re-expanded after round one has been backfilled.  Slips sign
  display row numbers of that expansion, carry partial field observations and
  the three raw list-state signs, and may point at parts inserted in round one.
- Copied-lost positions give two or more candidate original tickets/slots.
  The same insertion mark repeated on several slips denotes one physical part.
- Each round first drops resends of the same confirmation id, then checks
  conflicts against the actually located originals.  Two different slips
  setting the same field to different values, or changing and deleting the
  same part, reject every revision on that row for the round; the row keeps
  its pre-round shape for the next issue.  Removing a part and inserting with
  it as the anchor is allowed.
- Conflict-free inserts sit next to their anchor slots in confirmation order.

The engine enumerates backfills locally per shipment (never the cartesian
product of all parent/slot choices of the warehouse), keeps every distinct
outcome with a witness, and publishes a row only when all backfits that
satisfy every issued sign agree on the same final order, fields and
provenance.  The original ``.ak.unexplode`` interface is untouched.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, List, Optional, Sequence, Tuple, Union

__all__ = (
    "MISSING_FIELD",
    "MISSING_PART",
    "MISSING_LIST",
    "EMPTY_LIST",
    "Shipment",
    "Round1Slip",
    "Round2Slip",
    "ReconciledRow",
    "BackfillEvidence",
    "reconcile",
    "display_rows",
    "parts_to_arrow",
)

_UPDATE = "update"
_REMOVE = "remove"
_INSERT = "insert"
_BEFORE = "before"
_AFTER = "after"


class _Sentinel:
    __slots__ = ("_name",)

    def __init__(self, name: str):
        self._name = name

    def __repr__(self) -> str:
        return self._name


#: Observed field was not recorded; it places no constraint on the value.
MISSING_FIELD = _Sentinel("MISSING_FIELD")
#: Null element inside an otherwise present parts list.
MISSING_PART = _Sentinel("MISSING_PART")
#: The parts list itself is missing (null list).
MISSING_LIST = _Sentinel("MISSING_LIST")
#: The parts list is present but empty.
EMPTY_LIST = _Sentinel("EMPTY_LIST")


@dataclass(frozen=True)
class _Node:
    """One live or missing part inside a row.

    ``origin`` is ``("orig", slot)`` for original parts, ``("ins", mark)``
    for round-one inserts and ``("ins2", mark)`` for round-two inserts.
    ``fields`` is a sorted tuple of ``(name, value)`` for live parts and
    ``None`` for a missing part element.
    """

    origin: Tuple[Any, ...]
    fields: Optional[Tuple[Tuple[str, Any], ...]]


@dataclass
class Shipment:
    """One original shipping row.

    ``parts`` is ``None`` for a missing list, ``[]`` for an empty list,
    otherwise a list of field dicts or :data:`MISSING_PART`.
    ``slot_index`` is the bin position and may repeat between shipments.
    """

    shipment_no: Hashable
    slot_index: Hashable
    parts: Optional[List[Union[Dict[str, Any], _Sentinel]]]
    field_types: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Round1Slip:
    """A first-round correction slip.

    ``slot_choices`` has one entry when the original slot is known and two or
    more candidate original slots when the copy was lost.
    """

    confirm_id: Hashable
    shipment_no: Hashable
    op: str
    slot_choices: Sequence[int]
    side: Optional[str] = None
    mark: Optional[Hashable] = None
    fields: Optional[Dict[str, Any]] = None


@dataclass
class Round2Slip:
    """A second-round correction slip signed against a display row.

    ``display_choices`` lists the display row numbers the sign may point at
    (one when known, >=2 when copied-lost).  ``observed`` is a partial field
    dict for a live part, or one of :data:`MISSING_PART`, :data:`EMPTY_LIST`,
    :data:`MISSING_LIST`.
    """

    confirm_id: Hashable
    shipment_no: Hashable
    op: str
    display_choices: Sequence[int]
    observed: Union[Dict[str, Any], _Sentinel]
    side: Optional[str] = None
    mark: Optional[Hashable] = None
    fields: Optional[Dict[str, Any]] = None


@dataclass
class BackfillEvidence:
    """A concrete backfill witness: how every slip was resolved."""

    round1_resolution: Dict[Hashable, int]
    round2_resolution: Dict[Hashable, int]
    final_order: List[Union[Dict[str, Any], _Sentinel]]
    provenance: List[Tuple[Any, ...]]
    round1_rejected: bool = False
    round2_rejected: bool = False


@dataclass
class ReconciledRow:
    """Outcome for one original shipping row."""

    shipment_no: Hashable
    slot_index: Hashable
    status: str  # "approved" | "ambiguous" | "unsat" | "rejected"
    parts: Optional[List[Union[Dict[str, Any], _Sentinel]]]
    field_types: Dict[str, Any]
    field_order: List[str]
    unchanged: bool = False
    evidence: List[BackfillEvidence] = field(default_factory=list)
    first_fatal: Optional[Dict[str, Any]] = None
    returned_confirm_ids: List[Hashable] = field(default_factory=list)

    @property
    def publishable(self) -> bool:
        return self.status == "approved"


class _DeadBackfill(Exception):
    """Raised when one candidate assignment cannot satisfy the slips."""

    def __init__(self, reason: str, slip_id: Optional[Hashable] = None):
        super().__init__(reason)
        self.reason = reason  # "nomatch" or "conflict"
        self.slip_id = slip_id


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _dedupe(slips: Sequence[Any]) -> List[Any]:
    """Drop later resends sharing a confirmation id, preserving issue order."""
    seen = set()
    out = []
    for slip in slips:
        if slip.confirm_id in seen:
            continue
        seen.add(slip.confirm_id)
        out.append(slip)
    return out


def _infer_type(value: Any):
    import pyarrow as pa

    if isinstance(value, bool):
        return pa.bool_()
    if isinstance(value, int):
        return pa.int64()
    if isinstance(value, float):
        return pa.float64()
    if isinstance(value, str):
        return pa.string()
    if isinstance(value, bytes):
        return pa.binary()
    if value is None:
        return pa.null()
    raise TypeError(f"Cannot infer Arrow type for value {value!r}")


def _initial_nodes(
    shipment: Shipment,
) -> Tuple[Optional[List[_Node]], Dict[str, Any], List[str]]:
    """Return (nodes or None for missing list, field types, field order)."""
    field_types = dict(shipment.field_types)
    field_order: List[str] = []
    if shipment.parts is None:
        # A null list still carries the declared Arrow struct schema.
        return None, field_types, list(field_types)
    nodes: List[_Node] = []
    for slot, part in enumerate(shipment.parts):
        if part is MISSING_PART:
            nodes.append(_Node(("orig", slot), None))
            continue
        items = []
        for name, value in part.items():
            if name not in field_types:
                field_types[name] = _infer_type(value)
            if name not in field_order:
                field_order.append(name)
            items.append((name, value))
        nodes.append(_Node(("orig", slot), tuple(sorted(items))))
    # An empty list still has a declared schema to preserve.
    for name in field_types:
        if name not in field_order:
            field_order.append(name)
    return nodes, field_types, field_order


def _coerce(value: Any, pa_type) -> Any:
    import pyarrow as pa

    return pa.scalar(value, type=pa_type).as_py()


def _canonical(nodes: Optional[List[_Node]]) -> Any:
    if nodes is None:
        return None
    return tuple((node.origin, node.fields) for node in nodes)


def _render(
    nodes: Optional[List[_Node]], field_order: List[str]
) -> Optional[List[Union[Dict[str, Any], _Sentinel]]]:
    if nodes is None:
        return None
    rendered: List[Union[Dict[str, Any], _Sentinel]] = []
    for node in nodes:
        if node.fields is None:
            rendered.append(MISSING_PART)
        else:
            values = dict(node.fields)
            rendered.append(
                {name: values[name] for name in field_order if name in values}
            )
    return rendered


def _assignments(slip_choices: Sequence[Sequence[int]]):
    if not slip_choices:
        yield ()
    else:
        yield from itertools.product(*slip_choices)


def _validate_op(slip: Any, anchor_name: str) -> None:
    if slip.op not in (_UPDATE, _REMOVE, _INSERT):
        raise ValueError(f"unknown op {slip.op!r}")
    choices = getattr(slip, anchor_name)
    if not choices:
        raise ValueError(f"{anchor_name} must contain at least one candidate")
    if slip.op == _INSERT:
        if slip.side not in (_BEFORE, _AFTER):
            raise ValueError("insert slips require side 'before' or 'after'")
        if slip.mark is None:
            raise ValueError("insert slips require a mark")


# ---------------------------------------------------------------------------
# Display expansion (round two view)
# ---------------------------------------------------------------------------


def display_rows(
    parts: Optional[List[Union[Dict[str, Any], _Sentinel]]]
) -> List[Union[Dict[str, Any], _Sentinel]]:
    """Expand a parts list into the display rows shown after re-explosion.

    A missing list yields one :data:`MISSING_LIST` row, an empty list one
    :data:`EMPTY_LIST` row, and every list element (including a missing part)
    its own row.
    """
    if parts is None:
        return [MISSING_LIST]
    if len(parts) == 0:
        return [EMPTY_LIST]
    return list(parts)


def _display_nodes(nodes: Optional[List[_Node]]) -> List[Tuple[str, _Node]]:
    """Mirror of display_rows on internal nodes."""
    if nodes is None:
        return [("list", _Node(("missing-list",), None))]
    if not nodes:
        return [("list", _Node(("empty-list",), None))]
    return [("elem", node) for node in nodes]


# ---------------------------------------------------------------------------
# Round one application
# ---------------------------------------------------------------------------


def _apply_round1(
    slips: List[Round1Slip],
    combo: Tuple[int, ...],
    orig_nodes: Optional[List[_Node]],
    n_orig: int,
    field_types: Dict[str, Any],
    field_order: List[str],
) -> List[_Node]:
    if not slips:
        return orig_nodes
    if orig_nodes is None:
        # No original slot can be located inside a missing list.
        raise _DeadBackfill("nomatch", slips[0].confirm_id)

    # Same mark = one physical part: merge insert slips and demand agreement.
    mark_groups: Dict[Hashable, List[Tuple[Round1Slip, int]]] = {}
    for slip, slot in zip(slips, combo):
        if slip.op == _INSERT:
            mark_groups.setdefault(slip.mark, []).append((slip, slot))
    merged_insert: Dict[Hashable, Tuple[int, str, Dict[str, Any]]] = {}
    for mark, group in mark_groups.items():
        anchor = group[0][1]
        side = group[0][0].side
        values: Dict[str, Any] = {}
        for slip, slot in group:
            if slot != anchor or slip.side != side:
                raise _DeadBackfill("nomatch", slip.confirm_id)
            for name, value in (slip.fields or {}).items():
                if value is MISSING_FIELD:
                    continue
                if name in values and values[name] != value:
                    raise _DeadBackfill("conflict", slip.confirm_id)
                values[name] = value
        merged_insert[mark] = (anchor, side, values)

    # Locate targets and detect same-part conflicts between distinct confirms.
    updates: Dict[int, Dict[str, Tuple[Any, Hashable]]] = {}
    removes: Dict[int, Hashable] = {}
    for slip, slot in zip(slips, combo):
        if slot < 0 or slot >= n_orig:
            raise _DeadBackfill("nomatch", slip.confirm_id)
        # A null part element may be removed or used as an insert anchor;
        # only field correction requires a live part.
        if (
            orig_nodes[slot].fields is None
            and slip.op not in (_INSERT, _REMOVE)
        ):
            raise _DeadBackfill("nomatch", slip.confirm_id)
        if slip.op == _REMOVE:
            if slot in updates:
                raise _DeadBackfill("conflict", slip.confirm_id)
            removes[slot] = slip.confirm_id
        elif slip.op == _UPDATE:
            if slot in removes:
                raise _DeadBackfill("conflict", slip.confirm_id)
            bucket = updates.setdefault(slot, {})
            for name, value in (slip.fields or {}).items():
                if value is MISSING_FIELD:
                    continue
                if name in bucket and bucket[name][0] != value:
                    raise _DeadBackfill("conflict", slip.confirm_id)
                bucket[name] = (value, slip.confirm_id)

    # Insert anchors must name an original slot even if that part is removed.
    for mark, (anchor, _side, _values) in merged_insert.items():
        if anchor < 0 or anchor >= n_orig:
            raise _DeadBackfill("nomatch", mark)

    # Build field values and coerce to the original Arrow field types.
    current: List[Dict[str, Any]] = [
        dict(node.fields or ()) for node in orig_nodes
    ]
    for slot, bucket in updates.items():
        for name, (value, cid) in bucket.items():
            pa_type = field_types.get(name) or _infer_type(value)
            try:
                value = _coerce(value, pa_type)
            except Exception:
                raise _DeadBackfill("nomatch", cid)
            field_types.setdefault(name, pa_type)
            if name not in field_order:
                field_order.append(name)
            current[slot][name] = value

    # One node per mark; the merged dict's insertion order is confirmation
    # order.  Buckets sit on either side of every original slot, and the slot
    # is visited even when its part is removed (anchor removal is allowed).
    before: Dict[int, List[_Node]] = {}
    after: Dict[int, List[_Node]] = {}
    for mark, (anchor, side, values) in merged_insert.items():
        pa_values = {}
        for name, value in values.items():
            pa_type = field_types.get(name) or _infer_type(value)
            try:
                pa_values[name] = _coerce(value, pa_type)
            except Exception:
                raise _DeadBackfill("nomatch", mark)
            field_types.setdefault(name, pa_type)
            if name not in field_order:
                field_order.append(name)
        node = _Node(("ins", mark), tuple(sorted(pa_values.items())))
        (before if side == _BEFORE else after).setdefault(anchor, []).append(node)

    result: List[_Node] = []
    for slot in range(n_orig):
        result.extend(before.get(slot, ()))
        if slot not in removes:
            if orig_nodes[slot].fields is None:
                # Preserve the missing-part null element marker.
                result.append(orig_nodes[slot])
            else:
                result.append(
                    _Node(
                        orig_nodes[slot].origin,
                        tuple(sorted(current[slot].items())),
                    )
                )
        result.extend(after.get(slot, ()))
    return result


# ---------------------------------------------------------------------------
# Round two application
# ---------------------------------------------------------------------------


def _observation_matches(
    observed: Union[Dict[str, Any], _Sentinel], kind: str, node: _Node
) -> bool:
    if observed is MISSING_PART:
        return kind == "elem" and node.fields is None
    if observed is EMPTY_LIST:
        return kind == "list" and node.origin == ("empty-list",)
    if observed is MISSING_LIST:
        return kind == "list" and node.origin == ("missing-list",)
    if kind != "elem" or node.fields is None:
        return False
    values = dict(node.fields)
    for name, value in observed.items():
        if value is MISSING_FIELD:
            # The field was not copied: no constraint on this row.
            continue
        if name not in values or values[name] != value:
            return False
    return True


def _build_insert_node(
    origin: Tuple[Any, ...],
    values: Dict[str, Any],
    field_types: Dict[str, Any],
    field_order: List[str],
    confirm_id: Hashable,
) -> _Node:
    pa_values: Dict[str, Any] = {}
    for name, value in values.items():
        if value is MISSING_FIELD:
            continue
        pa_type = field_types.get(name) or _infer_type(value)
        try:
            pa_values[name] = _coerce(value, pa_type)
        except Exception:
            raise _DeadBackfill("nomatch", confirm_id)
        field_types.setdefault(name, pa_type)
        if name not in field_order:
            field_order.append(name)
    return _Node(origin, tuple(sorted(pa_values.items())))


def _apply_round2(
    slips: List[Round2Slip],
    combo: Tuple[int, ...],
    rows: List[Tuple[str, _Node]],
    r1_nodes: Optional[List[_Node]],
    field_types: Dict[str, Any],
    field_order: List[str],
    existing_marks: set,
) -> Optional[List[_Node]]:
    n_rows = len(rows)

    # Observations must match the signed display row.
    for slip, row in zip(slips, combo):
        if row < 0 or row >= n_rows:
            raise _DeadBackfill("nomatch", slip.confirm_id)
        kind, node = rows[row]
        if not _observation_matches(slip.observed, kind, node):
            raise _DeadBackfill("nomatch", slip.confirm_id)

    # Merge repeated marks and refuse reuse of a round-one insertion mark.
    mark_groups: Dict[Hashable, List[Tuple[Round2Slip, int]]] = {}
    for slip, row in zip(slips, combo):
        if slip.op == _INSERT:
            if slip.mark in existing_marks:
                raise _DeadBackfill("nomatch", slip.confirm_id)
            mark_groups.setdefault(slip.mark, []).append((slip, row))
    merged: Dict[Hashable, Tuple[int, str, Dict[str, Any]]] = {}
    for mark, group in mark_groups.items():
        anchor = group[0][1]
        side = group[0][0].side
        values: Dict[str, Any] = {}
        for slip, row in group:
            if row != anchor or slip.side != side:
                raise _DeadBackfill("nomatch", slip.confirm_id)
            for name, value in (slip.fields or {}).items():
                if value is MISSING_FIELD:
                    continue
                if name in values and values[name] != value:
                    raise _DeadBackfill("conflict", slip.confirm_id)
                values[name] = value
        merged[mark] = (anchor, side, values)

    list_state_row = bool(rows) and rows[0][0] == "list"

    # Operations on element rows locate the actual part; conflicts are checked
    # against the located original/inserted node.
    updates: Dict[Any, Dict[str, Tuple[Any, Hashable]]] = {}
    removes: Dict[Any, Hashable] = {}
    for slip, row in zip(slips, combo):
        if slip.op == _INSERT:
            continue
        kind, node = rows[row]
        if kind != "elem":
            raise _DeadBackfill("nomatch", slip.confirm_id)
        if slip.op == _REMOVE:
            if node.origin in updates:
                raise _DeadBackfill("conflict", slip.confirm_id)
            removes[node.origin] = slip.confirm_id
        else:
            if node.fields is None:
                raise _DeadBackfill("nomatch", slip.confirm_id)
            if node.origin in removes:
                raise _DeadBackfill("conflict", slip.confirm_id)
            bucket = updates.setdefault(node.origin, {})
            for name, value in (slip.fields or {}).items():
                if value is MISSING_FIELD:
                    continue
                if name in bucket and bucket[name][0] != value:
                    raise _DeadBackfill("conflict", slip.confirm_id)
                bucket[name] = (value, slip.confirm_id)

    if list_state_row:
        # Missing/empty list row: inserts materialise a fresh list in
        # confirmation order; update/remove could not locate anything.
        if not merged:
            return r1_nodes  # still None or []
        return [
            _build_insert_node(
                ("ins2", mark), values, field_types, field_order, mark
            )
            for mark, (_anchor, _side, values) in merged.items()
        ]

    # Element list: start from the round-one nodes.
    current: Dict[Any, Dict[str, Any]] = {
        node.origin: dict(node.fields or ()) for node in r1_nodes
    }
    for origin, bucket in updates.items():
        for name, (value, cid) in bucket.items():
            pa_type = field_types.get(name) or _infer_type(value)
            try:
                value = _coerce(value, pa_type)
            except Exception:
                raise _DeadBackfill("nomatch", cid)
            field_types.setdefault(name, pa_type)
            if name not in field_order:
                field_order.append(name)
            current[origin][name] = value

    before: Dict[Any, List[_Node]] = {}
    after: Dict[Any, List[_Node]] = {}
    for mark, (anchor_row, side, values) in merged.items():
        anchor_origin = rows[anchor_row][1].origin
        node = _build_insert_node(
            ("ins2", mark), values, field_types, field_order, mark
        )
        (before if side == _BEFORE else after).setdefault(
            anchor_origin, []
        ).append(node)

    result: List[_Node] = []
    for node in r1_nodes:
        # Anchor slots keep their insert buckets even when the anchor part is
        # removed in the same round.
        result.extend(before.get(node.origin, ()))
        if node.origin not in removes:
            if node.fields is None:
                result.append(node)
            else:
                result.append(
                    _Node(node.origin, tuple(sorted(current[node.origin].items())))
                )
        result.extend(after.get(node.origin, ()))
    return result


# ---------------------------------------------------------------------------
# Outcome enumeration
# ---------------------------------------------------------------------------


@dataclass
class _State:
    """One compatible reconstruction after some prefix of slips."""

    nodes: Optional[List[_Node]]
    field_types: Dict[str, Any]
    field_order: List[str]
    r1_resolution: Dict[Hashable, int]
    r2_resolution: Dict[Hashable, int]
    r1_rejected: bool = False
    r2_rejected: bool = False


def _collect_schema(
    shipment: Shipment, nodes: Optional[List[_Node]]
) -> Tuple[Dict[str, Any], List[str]]:
    _orig_nodes, field_types, field_order = _initial_nodes(shipment)
    field_types = dict(field_types)
    field_order = list(field_order)
    for node in nodes or []:
        if node.fields is None:
            continue
        for name, value in node.fields:
            if name not in field_types:
                field_types[name] = _infer_type(value)
            if name not in field_order:
                field_order.append(name)
    return field_types, field_order


def _enumerate_round1(
    shipment: Shipment, slips: List[Round1Slip]
) -> List[_State]:
    """All compatible round-one backfits; conflicts carry the original state."""
    for slip in slips:
        _validate_op(slip, "slot_choices")
    orig_nodes, field_types, field_order = _initial_nodes(shipment)
    n_orig = 0 if orig_nodes is None else len(orig_nodes)
    choices = [list(slip.slot_choices) for slip in slips]

    states: List[_State] = []
    for combo in _assignments(choices):
        resolution = {
            slip.confirm_id: slot for slip, slot in zip(slips, combo)
        }
        try:
            nodes = _apply_round1(
                slips,
                combo,
                orig_nodes,
                n_orig,
                dict(field_types),
                list(field_order),
            )
        except _DeadBackfill as dead:
            if dead.reason != "conflict":
                continue
            # Whole round returned: carry the pre-round (original) state.
            states.append(
                _State(
                    nodes=orig_nodes,
                    field_types=dict(field_types),
                    field_order=list(field_order),
                    r1_resolution=resolution,
                    r2_resolution={},
                    r1_rejected=True,
                )
            )
            continue
        ft, fo = _collect_schema(shipment, nodes)
        states.append(
            _State(
                nodes=nodes,
                field_types=ft,
                field_order=fo,
                r1_resolution=resolution,
                r2_resolution={},
            )
        )
    return states


def _enumerate_round2(states: List[_State], slips: List[Round2Slip]) -> List[_State]:
    """Extend round-one states with every compatible round-two backfit."""
    for slip in slips:
        _validate_op(slip, "display_choices")
    choices = [list(slip.display_choices) for slip in slips]

    out: List[_State] = []
    for state in states:
        rows = _display_nodes(state.nodes)
        existing_marks = {
            node.origin[1]
            for node in (state.nodes or [])
            if node.origin[0] == "ins"
        }
        for combo in _assignments(choices):
            try:
                nodes = _apply_round2(
                    slips,
                    combo,
                    rows,
                    state.nodes,
                    dict(state.field_types),
                    list(state.field_order),
                    set(existing_marks),
                )
            except _DeadBackfill as dead:
                if dead.reason != "conflict":
                    continue
                # Round-two revisions returned: carry the pre-round-two state.
                out.append(
                    _State(
                        nodes=state.nodes,
                        field_types=dict(state.field_types),
                        field_order=list(state.field_order),
                        r1_resolution=dict(state.r1_resolution),
                        r2_resolution={
                            slip.confirm_id: row
                            for slip, row in zip(slips, combo)
                        },
                        r1_rejected=state.r1_rejected,
                        r2_rejected=True,
                    )
                )
                continue
            ft, fo = _schema_of(state, nodes)
            out.append(
                _State(
                    nodes=nodes,
                    field_types=ft,
                    field_order=fo,
                    r1_resolution=dict(state.r1_resolution),
                    r2_resolution={
                        slip.confirm_id: row
                        for slip, row in zip(slips, combo)
                    },
                    r1_rejected=state.r1_rejected,
                )
            )
    return out


def _schema_of(state: _State, nodes: Optional[List[_Node]]):
    field_types = dict(state.field_types)
    field_order = list(state.field_order)
    for node in nodes or []:
        if node.fields is None:
            continue
        for name, value in node.fields:
            if name not in field_types:
                field_types[name] = _infer_type(value)
            if name not in field_order:
                field_order.append(name)
    return field_types, field_order


# ---------------------------------------------------------------------------
# First-fatal diagnosis: which observation empties the backfit space
# ---------------------------------------------------------------------------


def _first_fatal(
    shipment: Shipment,
    r1_slips: List[Round1Slip],
    r2_slips: List[Round2Slip],
) -> Dict[str, Any]:
    """Replay both rounds in issue order on prefixes of the slip sequences."""
    orig_nodes, field_types, field_order = _initial_nodes(shipment)
    n_orig = 0 if orig_nodes is None else len(orig_nodes)

    # Round-one prefixes: keyed by the chosen slot tuple.
    alive: Dict[Tuple[int, ...], _State] = {
        (): _State(orig_nodes, dict(field_types), list(field_order), {}, {})
    }
    for index, slip in enumerate(r1_slips):
        next_alive: Dict[Tuple[int, ...], _State] = {}
        for prefix, state in alive.items():
            for slot in slip.slot_choices:
                trial = prefix + (slot,)
                try:
                    nodes = _apply_round1(
                        r1_slips[: index + 1],
                        trial,
                        orig_nodes,
                        n_orig,
                        dict(field_types),
                        list(field_order),
                    )
                except _DeadBackfill as dead:
                    if dead.reason == "conflict":
                        next_alive.setdefault(
                            trial,
                            _State(
                                orig_nodes,
                                dict(field_types),
                                list(field_order),
                                {
                                    s.confirm_id: choice
                                    for s, choice in zip(
                                        r1_slips[: index + 1], trial
                                    )
                                },
                                {},
                                r1_rejected=True,
                            ),
                        )
                    continue
                next_alive.setdefault(
                    trial,
                    _State(
                        nodes,
                        *_schema_of_state(shipment, field_types, field_order, nodes),
                        {
                            s.confirm_id: choice
                            for s, choice in zip(r1_slips[: index + 1], trial)
                        },
                        {},
                    ),
                )
        if not next_alive:
            return {
                "round": 1,
                "confirm_id": slip.confirm_id,
                "round_sequence_position": index,
                "sequence_position": index,
            }
        alive = next_alive

    # Round-two prefixes branch from every surviving round-one state.
    current: Dict[Tuple[Any, ...], Tuple[Tuple[int, ...], Tuple[int, ...], _State]] = {
        (r1_combo, ()): (r1_combo, (), state) for r1_combo, state in alive.items()
    }
    for index, slip in enumerate(r2_slips):
        next_current: Dict[Tuple[Any, ...], Tuple[Tuple[int, ...], Tuple[int, ...], _State]] = {}
        for key, (r1_combo, r2_prefix, state) in current.items():
            rows = _display_nodes(state.nodes)
            existing_marks = {
                node.origin[1]
                for node in (state.nodes or [])
                if node.origin[0] == "ins"
            }
            for row in slip.display_choices:
                trial = r2_prefix + (row,)
                try:
                    nodes = _apply_round2(
                        r2_slips[: index + 1],
                        trial,
                        rows,
                        state.nodes,
                        dict(state.field_types),
                        list(state.field_order),
                        set(existing_marks),
                    )
                except _DeadBackfill as dead:
                    if dead.reason != "conflict":
                        continue
                    new_state = _State(
                        state.nodes,
                        dict(state.field_types),
                        list(state.field_order),
                        dict(state.r1_resolution),
                        {
                            s.confirm_id: choice
                            for s, choice in zip(r2_slips[: index + 1], trial)
                        },
                        state.r1_rejected,
                        r2_rejected=True,
                    )
                else:
                    ft, fo = _schema_of(state, nodes)
                    new_state = _State(
                        nodes,
                        ft,
                        fo,
                        dict(state.r1_resolution),
                        {
                            s.confirm_id: choice
                            for s, choice in zip(r2_slips[: index + 1], trial)
                        },
                        state.r1_rejected,
                    )
                next_current.setdefault((r1_combo, trial), (r1_combo, trial, new_state))
        if not next_current:
            return {
                "round": 2,
                "confirm_id": slip.confirm_id,
                "round_sequence_position": index,
                "sequence_position": len(r1_slips) + index,
            }
        current = next_current
    last_slip = r2_slips[-1] if r2_slips else r1_slips[-1]
    return {
        "round": 2 if r2_slips else 1,
        "confirm_id": last_slip.confirm_id,
        "round_sequence_position": (len(r2_slips) - 1) if r2_slips else len(r1_slips) - 1,
        "sequence_position": (len(r1_slips) + len(r2_slips) - 1),
    }


def _schema_of_state(shipment, field_types, field_order, nodes):
    ft, fo = _collect_schema(shipment, nodes)
    return ft, fo


# ---------------------------------------------------------------------------
# Top-level reconciliation
# ---------------------------------------------------------------------------


def _provenance(nodes: Optional[List[_Node]]) -> List[Tuple[Any, ...]]:
    if nodes is None:
        return []
    return [node.origin for node in nodes]


def _make_evidence(state: _State) -> BackfillEvidence:
    return BackfillEvidence(
        round1_resolution=dict(state.r1_resolution),
        round2_resolution=dict(state.r2_resolution),
        final_order=_render(state.nodes, state.field_order),
        provenance=_provenance(state.nodes),
        round1_rejected=state.r1_rejected,
        round2_rejected=state.r2_rejected,
    )


def _reconcile_one(
    shipment: Shipment,
    r1_slips: List[Round1Slip],
    r2_slips: List[Round2Slip],
) -> ReconciledRow:
    r1_slips = _dedupe(r1_slips)
    r2_slips = _dedupe(r2_slips)
    all_ids = [slip.confirm_id for slip in r1_slips + r2_slips]
    orig_nodes, orig_types, orig_order = _initial_nodes(shipment)
    orig_canon = _canonical(orig_nodes)
    original_render = _render(orig_nodes, orig_order)

    if not r1_slips and not r2_slips:
        return ReconciledRow(
            shipment_no=shipment.shipment_no,
            slot_index=shipment.slot_index,
            status="approved",
            parts=original_render,
            field_types=orig_types,
            field_order=orig_order,
            unchanged=True,
        )

    states = _enumerate_round1(shipment, r1_slips)
    if r2_slips:
        states = _enumerate_round2(states, r2_slips)

    if not states:
        return ReconciledRow(
            shipment_no=shipment.shipment_no,
            slot_index=shipment.slot_index,
            status="unsat",
            parts=original_render,
            field_types=orig_types,
            field_order=orig_order,
            first_fatal=_first_fatal(shipment, r1_slips, r2_slips),
            returned_confirm_ids=list(all_ids),
        )

    # Group compatible backfits by their full confirmation result.
    groups: Dict[Any, List[_State]] = {}
    for state in states:
        key = (
            _canonical(state.nodes),
            state.r1_rejected,
            state.r2_rejected,
        )
        groups.setdefault(key, []).append(state)

    if len(groups) > 1:
        witnesses = [group[0] for group in list(groups.values())[:2]]
        evidence = [_make_evidence(state) for state in witnesses]
        return ReconciledRow(
            shipment_no=shipment.shipment_no,
            slot_index=shipment.slot_index,
            status="ambiguous",
            parts=original_render,
            field_types=orig_types,
            field_order=orig_order,
            evidence=evidence,
            returned_confirm_ids=list(all_ids),
        )

    state = next(iter(groups.values()))[0]
    returned_ids: List[Hashable] = []
    if state.r1_rejected:
        returned_ids.extend(slip.confirm_id for slip in r1_slips)
    if state.r2_rejected:
        returned_ids.extend(slip.confirm_id for slip in r2_slips)

    if state.r1_rejected or state.r2_rejected:
        status = "rejected"
    else:
        status = "approved"
    return ReconciledRow(
        shipment_no=shipment.shipment_no,
        slot_index=shipment.slot_index,
        status=status,
        parts=_render(state.nodes, state.field_order),
        field_types=state.field_types,
        field_order=list(state.field_order),
        unchanged=_canonical(state.nodes) == orig_canon,
        evidence=[_make_evidence(state)],
        returned_confirm_ids=returned_ids,
    )


def reconcile(
    shipments: Sequence[Shipment],
    round1_slips: Sequence[Round1Slip] = (),
    round2_slips: Sequence[Round2Slip] = (),
) -> List[ReconciledRow]:
    """Merge both return slips into one checklist.

    Rows are processed per shipment, so the cost never explodes into a
    warehouse-wide cartesian of parent/slot choices.  Rows that no slip
    mentions are approved unchanged.
    """
    by_no: Dict[Hashable, Shipment] = {
        shipment.shipment_no: shipment for shipment in shipments
    }
    r1_by: Dict[Hashable, List[Round1Slip]] = {}
    r2_by: Dict[Hashable, List[Round2Slip]] = {}
    for slip in round1_slips:
        if slip.shipment_no not in by_no:
            raise KeyError(f"unknown shipment_no {slip.shipment_no!r}")
        r1_by.setdefault(slip.shipment_no, []).append(slip)
    for slip in round2_slips:
        if slip.shipment_no not in by_no:
            raise KeyError(f"unknown shipment_no {slip.shipment_no!r}")
        r2_by.setdefault(slip.shipment_no, []).append(slip)

    rows: List[ReconciledRow] = []
    for shipment in shipments:
        rows.append(
            _reconcile_one(
                shipment,
                r1_by.get(shipment.shipment_no, []),
                r2_by.get(shipment.shipment_no, []),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# Arrow output preserving the original field types and list null states
# ---------------------------------------------------------------------------


def parts_to_arrow(parts: Optional[List[Any]], field_types: Dict[str, Any],
                   field_order: Sequence[str]):
    """Build a nullable list-of-struct Arrow scalar for one reconciled row.

    The three raw states are preserved: ``None`` -> null list, ``[]`` ->
    empty list, :data:`MISSING_PART` -> null struct element.  Field values are
    held in the original Arrow types.
    """
    import pyarrow as pa

    struct_type = pa.struct(
        [pa.field(name, field_types[name]) for name in field_order]
    )
    list_type = pa.list_(struct_type)
    if parts is None:
        return pa.scalar(None, type=list_type)
    elements = []
    for part in parts:
        if part is MISSING_PART:
            elements.append(None)
        else:
            elements.append(
                {
                    name: (
                        pa.scalar(part[name], type=field_types[name]).as_py()
                        if name in part
                        else None
                    )
                    for name in field_order
                }
            )
    return pa.scalar(elements, type=list_type)
