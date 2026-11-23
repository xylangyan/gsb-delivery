"""Shrink a relay log of numbered protobuf update fragments.

A relay store keeps, in order, the protobuf updates for one master-data
message.  Recovery applies the fragments one by one with BetterProto's
``Message.parse`` onto the message already present in the repository.  That
existing message is only known to be a *legal* message of its generation: it
may be an ordinary serializable object, or two of its repeated fields may
share one already-filled list object.  Downstream, two generations of
generated message classes read the same log.

This module finds the subsequence of fragments with the smallest total byte
size such that, for *both* generations and for *every* legal starting
message, applying the kept fragments (in their original order, original
bytes untouched) leaves the message in a state that is indistinguishable
from applying the complete log.  The observable state covers: field values,
explicitly present default values, the currently selected member of each
oneof group, the order of repeated values, map contents, the preserved
unknown-field bytes (in order) and the ``serialized_on_wire`` flag.

The semantics mirrored here are exactly those of ``Message.load`` in this
repository:

* unknown field numbers are appended to ``_unknown_fields`` in wire order;
* a singular scalar/message field is overwritten (last write wins), and
  setting a oneof member clears the other members of the group;
* a repeated field entry sent unpacked is *appended* to the existing list
  (which may be shared with another field), while a packed entry *replaces*
  the list with a fresh one;
* map entries are merged key by key (last put per key wins).

Because the starting message is arbitrary, two logs are equivalent only when
their *effects* match for every start state.  The effect of a log is
summarised symbolically (see :func:`_summarize`) and equivalence of the
summaries is necessary and sufficient for equivalence on all start states,
including states where two repeated fields alias the same list object.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field as dc_field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from betterproto import (
    PACKED_TYPES,
    TYPE_DOUBLE,
    TYPE_FIXED32,
    TYPE_FIXED64,
    TYPE_FLOAT,
    TYPE_MAP,
    TYPE_SFIXED32,
    TYPE_SFIXED64,
    WIRE_FIXED_32,
    WIRE_FIXED_64,
    WIRE_LEN_DELIM,
    WIRE_VARINT,
    Message,
    decode_varint,
    parse_fields,
)

__all__ = ["minimize_log", "summarize_log", "logs_equivalent"]

# A fragment is a (number, data) pair; the log is the ordered sequence.
Fragment = Tuple[int, bytes]

# Event kinds produced by ``_decode_fragment``:
#   ("u", raw)                    unknown field bytes, appended in order
#   ("s", field, value)           singular set (scalar / message / oneof)
#   ("a", field, value)           unpacked append to a repeated field
#   ("r", field, (values, ...))   packed replace of a repeated field
#   ("m", field, key, value)      map entry put


def _canon(value: Any) -> Any:
    """Canonical, hashable, equality-stable form of a decoded constant."""
    if isinstance(value, Message):
        return ("msg", bytes(value))
    if isinstance(value, Enum):
        return ("enum", int(value))
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, float):
        return ("float", "nan") if math.isnan(value) else ("float", value)
    if isinstance(value, (int, str, bytes)):
        return (type(value).__name__, value)
    if isinstance(value, (list, tuple)):
        return ("list", tuple(_canon(v) for v in value))
    try:
        hash(value)
    except TypeError:
        return ("repr", repr(value))
    return ("obj", value)


def _repeated_fields(cls: type) -> List[str]:
    meta = cls._betterproto
    return [
        name
        for name in meta.sorted_field_names
        if isinstance(meta.default_gen[name](), list)
    ]


def _decode_fragment(cls: type, data: bytes) -> List[tuple]:
    """Decode one fragment into events, mirroring ``Message.load`` exactly."""
    meta = cls._betterproto
    probe = cls()
    events: List[tuple] = []
    for parsed in parse_fields(data):
        field_name = meta.field_name_by_number.get(parsed.number)
        if not field_name:
            events.append(("u", parsed.raw))
            continue
        fmeta = meta.meta_by_field_name[field_name]
        if parsed.wire_type == WIRE_LEN_DELIM and fmeta.proto_type in PACKED_TYPES:
            packed: List[Any] = []
            pos = 0
            while pos < len(parsed.value):
                if fmeta.proto_type in (TYPE_FLOAT, TYPE_FIXED32, TYPE_SFIXED32):
                    decoded, pos = parsed.value[pos : pos + 4], pos + 4
                    wire_type = WIRE_FIXED_32
                elif fmeta.proto_type in (TYPE_DOUBLE, TYPE_FIXED64, TYPE_SFIXED64):
                    decoded, pos = parsed.value[pos : pos + 8], pos + 8
                    wire_type = WIRE_FIXED_64
                else:
                    decoded, pos = decode_varint(parsed.value, pos)
                    wire_type = WIRE_VARINT
                packed.append(
                    probe._postprocess_single(wire_type, fmeta, field_name, decoded)
                )
            value: Any = packed
        else:
            value = probe._postprocess_single(
                parsed.wire_type, fmeta, field_name, parsed.value
            )

        if fmeta.proto_type == TYPE_MAP:
            events.append(("m", field_name, value.key, value.value))
        elif isinstance(meta.default_gen[field_name](), list) and not isinstance(
            value, list
        ):
            events.append(("a", field_name, value))
        elif isinstance(meta.default_gen[field_name](), list):
            events.append(("r", field_name, tuple(value)))
        else:
            events.append(("s", field_name, value))
    return events


@dataclass
class _RepField:
    replaced: bool
    tail: Optional[tuple]  # last packed constant ++ later appends (if replaced)
    # Every appended value in order (only if never replaced; appends to a
    # replaced field before its first replace are only observable through
    # aliased neighbours, which ``_Summary.merges`` covers).
    appends: Optional[tuple]


@dataclass
class _Summary:
    """Everything about a log's effect that is observable on any start state."""

    nonempty: bool
    unknown: bytes
    scalars: Dict[str, Any]  # non-oneof singular fields: last set value
    oneofs: Dict[str, Tuple[str, Any]]  # group -> (last member, its value)
    maps: Dict[str, Dict[Any, Any]]  # field -> key -> last put value
    rep: Dict[str, _RepField]
    # For every never-replaced field f and every other repeated field g, the
    # in-order merge of f's appends with g's pre-first-replace appends.  This
    # is what a start state observes through f when f and g share one list.
    merges: Dict[Tuple[str, str], tuple] = dc_field(default_factory=dict)


def _summarize(
    cls: type, frag_events: Sequence[List[tuple]], keep: Callable[[int], bool]
) -> _Summary:
    meta = cls._betterproto
    rep_fields = _repeated_fields(cls)
    unknown = bytearray()
    scalars: Dict[str, Any] = {}
    oneofs: Dict[str, Tuple[str, Any]] = {}
    maps: Dict[str, Dict[Any, Any]] = {}
    appends: Dict[str, List[Tuple[int, Any]]] = {f: [] for f in rep_fields}
    replaces: Dict[str, List[Tuple[int, Any]]] = {f: [] for f in rep_fields}
    nonempty = False
    order = 0
    for index, events in enumerate(frag_events):
        if not keep(index):
            continue
        nonempty = True
        for event in events:
            kind = event[0]
            if kind == "u":
                unknown += event[1]
            elif kind == "s":
                group = meta.meta_by_field_name[event[1]].group
                if group:
                    oneofs[group] = (event[1], _canon(event[2]))
                else:
                    scalars[event[1]] = _canon(event[2])
            elif kind == "m":
                maps.setdefault(event[1], {})[_canon(event[2])] = _canon(event[3])
            elif kind == "a":
                appends[event[1]].append((order, _canon(event[2])))
                order += 1
            elif kind == "r":
                replaces[event[1]].append((order, _canon(list(event[2]))))
                order += 1

    rep: Dict[str, _RepField] = {}
    for name in rep_fields:
        reps = replaces[name]
        apps = appends[name]
        if reps:
            last = reps[-1][0]
            tail = reps[-1][1] + tuple(v for pos, v in apps if pos > last)
            rep[name] = _RepField(True, tail, None)
        else:
            rep[name] = _RepField(False, None, tuple(v for _, v in apps))

    merges: Dict[Tuple[str, str], tuple] = {}
    for name in rep_fields:
        if rep[name].replaced:
            continue
        for other in rep_fields:
            if other == name:
                continue
            if replaces[other]:
                first = replaces[other][0][0]
                other_pre = [(p, v) for p, v in appends[other] if p < first]
            else:
                other_pre = appends[other]
            merges[(name, other)] = tuple(
                v for _, v in sorted(appends[name] + other_pre)
            )
    return _Summary(
        nonempty,
        bytes(unknown),
        scalars,
        oneofs,
        maps,
        rep,
        merges,
    )


def summarize_log(cls: type, fragments: Sequence[Fragment]) -> _Summary:
    """The observable effect of a whole log for one message generation."""
    events = [_decode_fragment(cls, bytes(data)) for _, data in fragments]
    return _summarize(cls, events, lambda _: True)


def logs_equivalent(
    cls: type, full: Sequence[Fragment], short: Sequence[Fragment]
) -> bool:
    """True if ``short`` has the same effect as ``full`` for generation ``cls``
    on every legal starting message."""
    return summarize_log(cls, full) == summarize_log(cls, short)


def _mandatory_keeps(cls: type, frag_events: Sequence[List[tuple]]) -> Set[int]:
    """Fragment indices that no valid subsequence may drop (one generation)."""
    rep_fields = _repeated_fields(cls)
    mandatory: Set[int] = set()
    for index, events in enumerate(frag_events):
        if any(event[0] == "u" for event in events):
            # Dropping it would remove bytes from the middle of the preserved
            # unknown-field byte stream.
            mandatory.add(index)

    order = 0
    first_replace: Dict[str, int] = {}
    last_replace: Dict[str, int] = {}
    field_appends: Dict[str, List[Tuple[int, int]]] = {f: [] for f in rep_fields}
    for index, events in enumerate(frag_events):
        for event in events:
            if event[0] == "a":
                field_appends[event[1]].append((order, index))
                order += 1
            elif event[0] == "r":
                first_replace.setdefault(event[1], order)
                last_replace[event[1]] = order
                order += 1

    has_never_replaced = any(f not in last_replace for f in rep_fields)
    for name in rep_fields:
        if name not in last_replace:
            # Every append lands in the final list, whatever the start state.
            mandatory.update(i for _, i in field_appends[name])
        else:
            last = last_replace[name]
            # Appends after the last replace survive in the final list.
            mandatory.update(i for pos, i in field_appends[name] if pos > last)
            if has_never_replaced:
                # Appends before the first replace are observable through any
                # never-replaced field that may share this field's list.
                first = first_replace[name]
                mandatory.update(i for pos, i in field_appends[name] if pos < first)
    return mandatory


def _constraints(
    cls: type, frag_events: Sequence[List[tuple]], target: _Summary
) -> List[Tuple[frozenset, Dict[int, frozenset]]]:
    """Necessary last-writer conditions, used to prune the search.

    Each constraint is ``(satisfiers, bad_after)``: a kept set is only viable
    if some satisfier ``i`` is kept while every fragment in ``bad_after[i]``
    is dropped.
    """
    n = len(frag_events)
    meta = cls._betterproto
    scalar_w: List[Dict[str, Any]] = [dict() for _ in range(n)]
    group_w: List[Dict[str, Any]] = [dict() for _ in range(n)]
    map_w: List[Dict[Any, Any]] = [dict() for _ in range(n)]
    rep_last: List[Dict[str, Tuple[Any, int]]] = [dict() for _ in range(n)]
    app_pos: List[Dict[str, List[int]]] = [dict() for _ in range(n)]
    order = 0
    for index, events in enumerate(frag_events):
        for event in events:
            kind = event[0]
            if kind == "s":
                group = meta.meta_by_field_name[event[1]].group
                if group:
                    group_w[index][group] = (event[1], _canon(event[2]))
                else:
                    scalar_w[index][event[1]] = _canon(event[2])
            elif kind == "m":
                map_w[index][(event[1], _canon(event[2]))] = _canon(event[3])
            elif kind == "r":
                rep_last[index][event[1]] = (_canon(list(event[2])), order)
                order += 1
            elif kind == "a":
                app_pos[index].setdefault(event[1], []).append(order)
                order += 1

    constraints: List[Tuple[frozenset, Dict[int, frozenset]]] = []

    def last_writer(writes: List[Dict[Any, Any]], key: Any, want: Any) -> None:
        sats = {i for i in range(n) if writes[i].get(key) == want}
        bad = {
            i: frozenset(
                j
                for j in range(i + 1, n)
                if key in writes[j] and writes[j][key] != want
            )
            for i in sats
        }
        constraints.append((frozenset(sats), bad))

    for name, value in target.scalars.items():
        last_writer(scalar_w, name, value)
    for group, member_value in target.oneofs.items():
        last_writer(group_w, group, member_value)
    for name, entries in target.maps.items():
        for key, value in entries.items():
            last_writer(map_w, (name, key), value)

    for name, rep in target.rep.items():
        if not rep.replaced:
            continue
        last_const = rep_last_gidx = None
        for index in range(n):
            if name in rep_last[index]:
                last_const, rep_last_gidx = rep_last[index][name]
        post = {
            pos
            for index in range(n)
            for pos in app_pos[index].get(name, ())
            if pos > rep_last_gidx
        }
        sats = set()
        for index in range(n):
            entry = rep_last[index].get(name)
            if entry and entry[0] == last_const and (not post or entry[1] < min(post)):
                sats.add(index)
        bad: Dict[int, frozenset] = {}
        for i in sats:
            my_pos = rep_last[i][name][1]
            dropped: Set[int] = set()
            for j in range(n):
                if j == i:
                    continue
                other = rep_last[j].get(name)
                if other and other[1] > my_pos and other[0] != last_const:
                    dropped.add(j)
                    continue
                if any(p > my_pos and p not in post for p in app_pos[j].get(name, ())):
                    dropped.add(j)
            bad[i] = frozenset(dropped)
        constraints.append((frozenset(sats), bad))
    return constraints


def minimize_log(
    gen1: type, gen2: type, fragments: Sequence[Fragment]
) -> Tuple[int, List[int]]:
    """Return ``(min_bytes, kept_numbers)`` for the shortest safe subsequence.

    ``gen1`` and ``gen2`` are the two generations of generated message
    classes; ``fragments`` is the ordered log of ``(number, data)`` pairs.
    The result is the smallest total byte size of a subsequence whose effect
    on every legal starting message equals the full log's effect for both
    generations, together with the original numbers of the kept fragments in
    log order.
    """
    frags: List[Fragment] = [(int(number), bytes(data)) for number, data in fragments]
    n = len(frags)
    if n == 0:
        return 0, []

    gens = (gen1, gen2)
    events = [[_decode_fragment(cls, data) for _, data in frags] for cls in gens]
    targets = [_summarize(gens[g], events[g], lambda _: True) for g in (0, 1)]

    mandatory: Set[int] = set()
    for g in (0, 1):
        mandatory |= _mandatory_keeps(gens[g], events[g])

    weights = [len(data) for _, data in frags]
    constraints: List[Tuple[frozenset, Dict[int, frozenset]]] = []
    for g in (0, 1):
        constraints += _constraints(gens[g], events[g], targets[g])

    def is_valid(kept: Set[int]) -> bool:
        if not kept:
            return False  # the full log parses at least once; so must we
        for g in (0, 1):
            summary = _summarize(gens[g], events[g], kept.__contains__)
            if summary != targets[g]:
                return False
        return True

    base_weight = sum(weights[i] for i in mandatory)
    if is_valid(set(mandatory)):
        return base_weight, [frags[i][0] for i in sorted(mandatory)]

    best_weight = sum(weights)
    best_count = n
    best_keep: Set[int] = set(range(n))
    free = sorted((i for i in range(n) if i not in mandatory), key=lambda i: -weights[i])

    def feasible(kept: Set[int], dropped: Set[int]) -> bool:
        # A constraint can still be met if some satisfier is kept or
        # undecided and no fragment that would contradict it is kept yet.
        kept_all = mandatory | kept
        for sats, bad_after in constraints:
            if not any(
                i not in dropped and not (bad_after[i] & kept_all) for i in sats
            ):
                return False
        return True

    sys.setrecursionlimit(max(1000, 4 * len(free) + 100))

    def dfs(pos: int, kept: Set[int], dropped: Set[int], weight: int) -> None:
        nonlocal best_weight, best_count, best_keep
        # minimise total bytes, then fragment count among byte ties
        if (weight, len(mandatory) + len(kept)) >= (best_weight, best_count):
            return
        if not feasible(kept, dropped):
            return
        if pos == len(free):
            candidate = mandatory | kept
            if is_valid(candidate):
                best_weight = weight
                best_count = len(candidate)
                best_keep = candidate
            return
        index = free[pos]
        dfs(pos + 1, kept, dropped | {index}, weight)
        kept.add(index)
        dfs(pos + 1, kept, dropped, weight + weights[index])
        kept.discard(index)

    dfs(0, set(), set(), base_weight)
    return best_weight, [frags[i][0] for i in sorted(best_keep)]


def main(argv: Sequence[str]) -> int:
    """CLI: ``python log_compactor.py spec.py``.

    The spec file must define ``GEN1`` and ``GEN2`` (betterproto message
    classes) and ``FRAGMENTS`` (a list of ``(number, bytes)`` pairs).
    Prints ``{"min_bytes": ..., "keep": [...]}`` as JSON.
    """
    import importlib.util
    import json

    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    spec = importlib.util.spec_from_file_location("log_compactor_spec", argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    min_bytes, keep = minimize_log(module.GEN1, module.GEN2, module.FRAGMENTS)
    print(json.dumps({"min_bytes": min_bytes, "keep": keep}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
