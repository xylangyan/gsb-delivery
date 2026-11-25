# BSD 3-Clause License; see https://github.com/scikit-hep/awkward/blob/main/LICENSE

from __future__ import annotations

from collections import defaultdict

import numpy as np

import awkward as ak
from awkward._dispatch import high_level_function

__all__ = ("to_buffer_views",)


@high_level_function()
def to_buffer_views(
    array,
    requests,
    *,
    shared=None,
    container=None,
    buffer_key="{form_key}-{attribute}",
    body_key="{prefix}.body{leaf}",
    prefix="view{request}",
    highlevel=True,
    behavior=None,
    attrs=None,
):
    """Builds independently readable row-subset views over one shared buffer pool.

    ``requests`` is an iterable of row requests; each request is an iterable of
    original row numbers, in any order and with repetitions allowed. Every
    request becomes its own Form and length, but all views share one container,
    so a body buffer that several views need is written, and counted, once.

    Each returned view matches the original library's ``array[request]`` in
    value, order, logical type, missingness, empty lists, and Union branch
    meaning. Node classes are preserved: only body buffers and structural
    buffers are rewritten, so a leaf never changes class.

    Structural buffers (offsets, starts, stops, tags, index, mask) are always
    rewritten because selection changes them; they are not body bytes.

    Body buffers (``NumpyArray`` leaves):

    * a contiguous run of an original body that lies inside one approved
      ``shared`` range is referenced directly (zero-copy, no body bytes);
    * otherwise all requests reaching that original body share one freshly
      written canvas holding the sorted union of their reached rows; each
      request reads its own rows from the canvas and the canvas is written
      once;
    * no unreached original value is placed in a canvas.

    Args:
        array: Array-like data (anything #ak.to_layout recognizes).
        requests (iterable of iterables of int): Row requests.
        shared (None or iterable of (str, int, int)): ``(buffer_key, start,
            stop)`` physical ranges of the original buffers that may be shared,
            in elements of that buffer. None means every original body buffer is
            fully shareable.
        container (None or MutableMapping): Receives the emitted buffers.
        buffer_key (str or callable): As in #ak.to_buffers, for both the
            original and the emitted buffers.
        body_key (str): Canvas key template with ``{prefix}`` and ``{leaf}``.
        prefix (str): Request name template with ``{request}``.
        highlevel (bool): Return #ak.Array if True, else low-level Content.
        behavior (None or dict): Custom behavior.
        attrs (None or dict): Custom attributes.

    Returns:
        ``(views, manifest)``. ``views`` is a list of ``(form, length, array)``
        triples in request order, each readable alone with
        ``ak.from_buffers(form, length, container, buffer_key=...)``. The
        manifest has ``"views"`` (per request), ``"buffers"`` (per emitted
        buffer with role, size, written flag, and physical sources),
        ``"rows"`` (per output row: original row, missingness, Union
        ``(tag, index)`` path, and physical ranges used), and ``"body_bytes"``
        (freshly written body bytes, each shared canvas counted once).
    """
    # Dispatch
    yield (array,)

    # Implementation
    return _impl(
        array,
        requests,
        shared,
        container,
        buffer_key,
        body_key,
        prefix,
        highlevel,
        behavior,
        attrs,
    )


# ---------------------------------------------------------------------------
# entry point


def _impl(
    array,
    requests,
    shared,
    container,
    buffer_key,
    body_key,
    prefix,
    highlevel,
    behavior,
    attrs,
):
    layout = ak.operations.to_layout(
        array, allow_record=False, primitive_policy="error"
    )
    backend = layout.backend
    if not backend.nplike.known_data:
        raise TypeError(
            "cannot call 'to_buffer_views' on an array without concrete data"
        )

    requests = [[int(row) for row in request] for request in requests]
    for request_index, rows in enumerate(requests):
        for row in rows:
            if not 0 <= row < layout.length:
                raise IndexError(
                    f"request {request_index}: row {row} is out of range for "
                    f"an array of length {layout.length}"
                )

    prefixes = [prefix.format(request=i) for i in range(len(requests))]
    if len(set(prefixes)) != len(prefixes):
        raise ValueError("'prefix' must produce distinct names")

    if container is None:
        container = {}

    original_form, _, _ = ak.operations.to_buffers(layout, buffer_key=buffer_key)
    shared_ranges = _normalize_shared(
        shared, set(original_form.expected_from_buffers(buffer_key))
    )

    registry = _Registry(layout, original_form, buffer_key)

    per_row = []
    reached = []
    for rows in requests:
        found = _per_row(layout, rows, registry)
        per_row.append(found)
        reached.append(
            {
                leaf_id: [row for group in groups for row in group]
                for leaf_id, groups in found.items()
            }
        )

    plan = _Plan(
        registry=registry,
        reached=reached,
        per_row=per_row,
        shared_ranges=shared_ranges,
        backend=backend,
        body_key=body_key,
        buffer_key=buffer_key,
        prefixes=prefixes,
    )

    views = []
    rows_out = []
    for request_index, rows in enumerate(requests):
        builder = _Builder(
            registry=registry,
            plan=plan,
            request_index=request_index,
            backend=backend,
        )
        content = builder.build(layout, rows)
        form, length, emitted = ak.to_buffers(
            content,
            buffer_key=buffer_key,
            form_key=f"{prefixes[request_index]}.node{{id}}",
        )
        leaf_keys = dict(enumerate(_leaf_keys(form, buffer_key)))
        container.update(emitted)

        out = ak.from_buffers(
            form,
            length,
            emitted,
            buffer_key=buffer_key,
            highlevel=highlevel,
            behavior=behavior,
            attrs=attrs,
        )
        views.append(
            (
                form,
                length,
                out,
                {
                    "prefix": prefixes[request_index],
                    "form": form.to_dict(),
                    "form_json": form.to_json(),
                    "length": length,
                    "buffer_keys": sorted(emitted),
                },
                emitted,
            )
        )
        rows_out.append(
            _rows_manifest(
                layout,
                registry,
                plan,
                request_index,
                rows,
                prefixes[request_index],
                leaf_keys,
            )
        )

    plan.put_canvases(container)
    manifest = plan.manifest(views, rows_out)
    return [(v[0], v[1], v[2]) for v in views], manifest


# ---------------------------------------------------------------------------
# helpers


def _buffer_key(buffer_key, form_key, attribute):
    if isinstance(buffer_key, str):
        return buffer_key.format(form_key=form_key, attribute=attribute)
    return buffer_key(form_key=form_key, attribute=attribute)


def _prod(shape):
    out = 1
    for item in shape:
        out *= int(item)
    return out


def _flat(node):
    return node.backend.nplike.asarray(node.data).reshape(-1)


def _spans(rows):
    if not rows:
        return []
    out = []
    start = previous = rows[0]
    for value in rows[1:]:
        if value != previous + 1:
            out.append((start, previous + 1))
            start = value
        previous = value
    out.append((start, previous + 1))
    return out


def _contiguous(rows):
    if not rows:
        return None
    for left, right in zip(rows, rows[1:]):
        if left + 1 != right:
            return None
    return rows[0], rows[-1] + 1


class _Registry:
    def __init__(self, layout, form, buffer_key):
        self.layout = layout
        self.buffer_key = buffer_key
        self.entries = []
        self.leaf_ids = {}
        self._walk(layout, form)

    def _walk(self, node, node_form):
        if isinstance(node, ak.contents.EmptyArray):
            return
        if isinstance(node, ak.contents.NumpyArray):
            leaf_id = len(self.entries)
            self.leaf_ids[id(node)] = leaf_id
            self.entries.append(
                {
                    "id": leaf_id,
                    "node": node,
                    "data_key": _buffer_key(
                        self.buffer_key, node_form.form_key, "data"
                    ),
                    "inner": _prod(node.inner_shape),
                    "inner_shape": node.inner_shape,
                }
            )
            return
        if isinstance(node, (ak.contents.RecordArray, ak.contents.UnionArray)):
            for i in range(len(node.contents)):
                self._walk(node.content(i), node_form.content(i))
            return
        self._walk(node.content, node_form.content)

    def leaf(self, node):
        return self.entries[self.leaf_ids[id(node)]]


def _child_rows(node, rows):
    if isinstance(node, ak.contents.ListOffsetArray):
        offsets = node.offsets.data
        out = []
        for row in rows:
            out.extend(range(int(offsets[row]), int(offsets[row + 1])))
        return out
    if isinstance(node, ak.contents.ListArray):
        starts = node.starts.data
        stops = node.stops.data
        out = []
        for row in rows:
            out.extend(range(int(starts[row]), int(stops[row])))
        return out
    if isinstance(node, ak.contents.RegularArray):
        size = int(node.size)
        out = []
        for row in rows:
            out.extend(range(row * size, (row + 1) * size))
        return out
    if isinstance(node, ak.contents.IndexedOptionArray):
        index = node.index.data
        return [int(index[row]) for row in rows if int(index[row]) >= 0]
    if isinstance(node, ak.contents.IndexedArray):
        index = node.index.data
        return [int(index[row]) for row in rows]
    if isinstance(
        node,
        (
            ak.contents.ByteMaskedArray,
            ak.contents.BitMaskedArray,
            ak.contents.UnmaskedArray,
        ),
    ):
        return list(rows)
    if isinstance(node, ak.contents.RecordArray):
        return {i: list(rows) for i in range(len(node.contents))}
    if isinstance(node, ak.contents.UnionArray):
        tags = node.tags.data
        index = node.index.data
        per_tag = defaultdict(list)
        for row in rows:
            per_tag[int(tags[row])].append(int(index[row]))
        return {i: per_tag.get(i, []) for i in range(len(node.contents))}
    raise AssertionError(f"unexpected layout: {type(node).__name__}")


def _per_row(node, rows, registry):
    """leaf_id -> list (one entry per input row) of original leaf rows used.

    Concatenating the per-row lists gives exactly the ordered sequence of
    original leaf rows the rebuild will place in that leaf's body.
    """

    if isinstance(node, ak.contents.EmptyArray):
        return {}

    if isinstance(node, ak.contents.NumpyArray):
        return {registry.leaf(node)["id"]: [[row] for row in rows]}

    if isinstance(node, ak.contents.RecordArray):
        out = defaultdict(lambda: [[] for _ in rows])
        for i in range(len(node.contents)):
            child = _per_row(node.content(i), rows, registry)
            for leaf_id, per_row in child.items():
                for row_index in range(len(rows)):
                    out[leaf_id][row_index].extend(per_row[row_index])
        return dict(out)

    if isinstance(node, ak.contents.UnionArray):
        tags = node.tags.data
        index = node.index.data
        by_tag = defaultdict(list)
        back = []
        for row in rows:
            tag = int(tags[row])
            back.append((tag, len(by_tag[tag])))
            by_tag[tag].append(int(index[row]))
        out = defaultdict(lambda: [[] for _ in rows])
        for tag in range(len(node.contents)):
            child = _per_row(node.content(tag), by_tag.get(tag, []), registry)
            for leaf_id, per_row in child.items():
                for row_index, (row_tag, slot) in enumerate(back):
                    if row_tag == tag:
                        out[leaf_id][row_index].extend(per_row[slot])
        return dict(out)

    # single-content nodes: each parent row expands to its own child rows
    pieces = [_child_rows(node, [row]) for row in rows]
    child_rows = [value for piece in pieces for value in piece]
    child = _per_row(node.content, child_rows, registry)
    out = {}
    for leaf_id, per_row in child.items():
        merged = []
        position = 0
        for piece in pieces:
            take = len(piece)
            rows_for_leaf = []
            for value in per_row[position : position + take]:
                rows_for_leaf.extend(value)
            merged.append(rows_for_leaf)
            position += take
        out[leaf_id] = merged
    return out


# ---------------------------------------------------------------------------
# allocation


class _Plan:
    def __init__(
        self,
        *,
        registry,
        reached,
        per_row,
        shared_ranges,
        backend,
        body_key,
        buffer_key,
        prefixes,
    ):
        self.registry = registry
        self.reached = reached
        self.per_row = per_row
        self.backend = backend
        self.body_key = body_key
        self.buffer_key = buffer_key
        self.prefixes = prefixes

        self.kind = [dict() for _ in reached]
        self.canvases = {}
        self.gathered = {}
        self.records = {}
        self.payloads = {}
        self.owner_bytes = defaultdict(int)
        self.total_body_bytes = 0

        fresh_runs = defaultdict(list)
        gathered = defaultdict(list)

        for request_index, found in enumerate(reached):
            for leaf_id, rows in found.items():
                if not rows:
                    self.kind[request_index][leaf_id] = {"kind": "empty"}
                    continue
                entry = registry.entries[leaf_id]
                run = _contiguous(rows)
                if run is not None and _is_shareable(
                    shared_ranges,
                    entry["data_key"],
                    run[0] * entry["inner"],
                    run[1] * entry["inner"],
                ):
                    self.kind[request_index][leaf_id] = {
                        "kind": "direct",
                        "start": run[0],
                        "stop": run[1],
                    }
                elif run is not None:
                    fresh_runs[leaf_id].append((run[0], run[1], request_index))
                else:
                    gathered[(leaf_id, tuple(rows))].append(request_index)

        # Merge overlapping or touching fresh runs into one canvas per leaf.
        canvases = defaultdict(list)
        for leaf_id, items in fresh_runs.items():
            items.sort()
            for start, stop, request_index in items:
                if canvases[leaf_id] and start <= canvases[leaf_id][-1]["stop"]:
                    component = canvases[leaf_id][-1]
                    component["stop"] = max(component["stop"], stop)
                    component["members"].append((request_index, start, stop))
                else:
                    canvases[leaf_id].append(
                        {
                            "start": start,
                            "stop": stop,
                            "members": [(request_index, start, stop)],
                        }
                    )

        for leaf_id, components in canvases.items():
            entry = registry.entries[leaf_id]
            inner = entry["inner"]
            for component_index, component in enumerate(components):
                members = component["members"]
                owner = min(member[0] for member in members)
                key = body_key.format(
                    prefix=prefixes[owner],
                    leaf=f"{entry['data_key']}.{component_index}",
                )
                rows = list(range(component["start"], component["stop"]))
                index = [
                    row * inner + offset
                    for row in rows
                    for offset in range(inner)
                ]
                payload = backend.nplike.ascontiguousarray(
                    _flat(entry["node"])[index]
                )
                payload = backend.nplike.reshape(
                    payload, (len(rows), *entry["inner_shape"])
                )
                self.payloads[key] = payload
                self.records[key] = {
                    "key": key,
                    "role": "body",
                    "nbytes": int(payload.nbytes),
                    "written": True,
                    "aliases": [],
                    "sources": [
                        {
                            "buffer": entry["data_key"],
                            "start": component["start"] * inner,
                            "stop": component["stop"] * inner,
                        }
                    ],
                }
                self.total_body_bytes += int(payload.nbytes)
                self.owner_bytes[owner] += int(payload.nbytes)
                for request_index, start, stop in members:
                    self.kind[request_index][leaf_id] = {
                        "kind": "canvas",
                        "leaf": leaf_id,
                        "payload": payload,
                        "key": key,
                        "offset": (start - component["start"]) * inner,
                        "length": (stop - start) * inner,
                    }

        for (leaf_id, rows), members in gathered.items():
            entry = registry.entries[leaf_id]
            inner = entry["inner"]
            owner = min(members)
            key = body_key.format(prefix=prefixes[owner], leaf=entry["data_key"])
            index = [
                row * inner + offset for row in rows for offset in range(inner)
            ]
            payload = backend.nplike.ascontiguousarray(
                _flat(entry["node"])[index]
            )
            payload = backend.nplike.reshape(
                payload, (len(rows), *entry["inner_shape"])
            )
            self.gathered[(leaf_id, tuple(rows))] = {
                "key": key,
                "payload": payload,
            }
            self.payloads[key] = payload
            self.records[key] = {
                "key": key,
                "role": "body",
                "nbytes": int(payload.nbytes),
                "written": True,
                "aliases": [],
                "sources": [
                    {
                        "buffer": entry["data_key"],
                        "start": start * inner,
                        "stop": stop * inner,
                    }
                    for start, stop in _spans(list(rows))
                ],
            }
            self.total_body_bytes += int(payload.nbytes)
            self.owner_bytes[owner] += int(payload.nbytes)
            for request_index in members:
                self.kind[request_index][leaf_id] = {
                    "kind": "gathered",
                    "leaf": leaf_id,
                    "key": key,
                    "payload": payload,
                }

    def put_canvases(self, container):
        for key, payload in self.payloads.items():
            container.setdefault(key, payload)

    def manifest(self, views, row_records):
        for request_index, view in enumerate(views):
            view[3]["body_bytes"] = self.owner_bytes.get(request_index, 0)

        records = dict(self.records)
        for request_index, view in enumerate(views):
            form, _, _, _, emitted = view
            form_keys = _leaf_keys(form, self.buffer_key)
            for leaf_id, actual_key in enumerate(form_keys):
                if actual_key not in emitted:
                    continue
                entry = self.registry.entries[leaf_id]
                assignment = self.kind[request_index].get(leaf_id)
                if assignment is None:
                    continue
                actual = emitted[actual_key]
                if assignment["kind"] == "direct":
                    records[actual_key] = {
                        "key": actual_key,
                        "role": "body",
                        "nbytes": int(actual.nbytes),
                        "written": False,
                        "aliases": [],
                        "sources": [
                            {
                                "buffer": entry["data_key"],
                                "start": assignment["start"] * entry["inner"],
                                "stop": assignment["stop"] * entry["inner"],
                            }
                        ],
                    }
                elif assignment["kind"] == "canvas":
                    records[actual_key] = {
                        "key": actual_key,
                        "role": "body",
                        "nbytes": int(actual.nbytes),
                        "written": False,
                        "aliases": [assignment["key"]],
                        "sources": records[assignment["key"]]["sources"],
                    }
                elif assignment["kind"] == "gathered":
                    records[actual_key] = {
                        "key": actual_key,
                        "role": "body",
                        "nbytes": int(actual.nbytes),
                        "written": False,
                        "aliases": [assignment["key"]],
                        "sources": records[assignment["key"]]["sources"],
                    }
                else:
                    records[actual_key] = {
                        "key": actual_key,
                        "role": "body",
                        "nbytes": int(actual.nbytes),
                        "written": False,
                        "aliases": [],
                        "sources": [
                            {"buffer": entry["data_key"], "start": 0, "stop": 0}
                        ],
                    }

        all_emitted = set()
        for view in views:
            all_emitted.update(view[4])
        for request_index, view in enumerate(views):
            _, _, _, _, emitted = view
            for key, payload in emitted.items():
                if key in records:
                    continue
                records[key] = {
                    "key": key,
                    "role": "structural",
                    "nbytes": int(payload.nbytes),
                    "written": True,
                    "aliases": [],
                    "sources": None,
                }

        return {
            "views": [view[3] for view in views],
            "buffers": [
                record
                for key, record in records.items()
                if record["written"] or key in all_emitted
            ],
            "rows": row_records,
            "body_bytes": self.total_body_bytes,
        }


def _leaf_keys(form, buffer_key):
    out = []

    def walk(node_form):
        if isinstance(node_form, ak.forms.NumpyForm):
            out.append(_buffer_key(buffer_key, node_form.form_key, "data"))
            return
        if isinstance(node_form, ak.forms.RecordForm):
            for content in node_form.contents:
                walk(content)
            return
        if isinstance(node_form, ak.forms.UnionForm):
            for content in node_form.contents:
                walk(content)
            return
        try:
            content = node_form.content
        except AttributeError:
            return
        walk(content)

    walk(form)
    return out


# ---------------------------------------------------------------------------
# rebuilding


class _Builder:
    def __init__(self, *, registry, plan, request_index, backend):
        self.registry = registry
        self.plan = plan
        self.request_index = request_index
        self.backend = backend

    def build(self, node, rows):
        rows = list(rows)

        if isinstance(node, ak.contents.EmptyArray):
            return ak.contents.EmptyArray()

        if isinstance(node, ak.contents.NumpyArray):
            return self._numpy(node, rows)

        if isinstance(node, ak.contents.ListOffsetArray):
            offsets = node.offsets.data
            child = self.build(node.content, _child_rows(node, rows))
            values = [0]
            for row in rows:
                values.append(
                    values[-1] + int(offsets[row + 1]) - int(offsets[row])
                )
            return ak.contents.ListOffsetArray(
                ak.index.Index(
                    self.backend.nplike.asarray(values, dtype=offsets.dtype)
                ),
                child,
                parameters=node._parameters,
            )

        if isinstance(node, ak.contents.ListArray):
            starts = node.starts.data
            stops = node.stops.data
            child = self.build(node.content, _child_rows(node, rows))
            new_starts = []
            new_stops = []
            position = 0
            for row in rows:
                length = int(stops[row]) - int(starts[row])
                new_starts.append(position)
                position += length
                new_stops.append(position)
            return ak.contents.ListArray(
                ak.index.Index(
                    self.backend.nplike.asarray(new_starts, dtype=starts.dtype)
                ),
                ak.index.Index(
                    self.backend.nplike.asarray(new_stops, dtype=stops.dtype)
                ),
                child,
                parameters=node._parameters,
            )

        if isinstance(node, ak.contents.RegularArray):
            child = self.build(node.content, _child_rows(node, rows))
            return ak.contents.RegularArray(
                child, int(node.size), len(rows), parameters=node._parameters
            )

        if isinstance(node, ak.contents.IndexedOptionArray):
            index = node.index.data
            child = self.build(node.content, _child_rows(node, rows))
            new_index = []
            position = 0
            for row in rows:
                if int(index[row]) < 0:
                    new_index.append(-1)
                else:
                    new_index.append(position)
                    position += 1
            return ak.contents.IndexedOptionArray(
                ak.index.Index(
                    self.backend.nplike.asarray(new_index, dtype=index.dtype)
                ),
                child,
                parameters=node._parameters,
            )

        if isinstance(node, ak.contents.IndexedArray):
            index = node.index.data
            child = self.build(node.content, _child_rows(node, rows))
            new_index = list(range(len(rows)))
            return ak.contents.IndexedArray(
                ak.index.Index(
                    self.backend.nplike.asarray(new_index, dtype=index.dtype)
                ),
                child,
                parameters=node._parameters,
            )

        if isinstance(node, ak.contents.ByteMaskedArray):
            child = self.build(node.content, rows)
            mask = self.backend.nplike.asarray(node.mask.data)[rows]
            return ak.contents.ByteMaskedArray(
                ak.index.Index(
                    self.backend.nplike.asarray(mask, dtype=node.mask.data.dtype)
                ),
                child,
                node._valid_when,
                parameters=node._parameters,
            )

        if isinstance(node, ak.contents.BitMaskedArray):
            child = self.build(node.content, rows)
            valid = self.backend.nplike.asarray(node.mask_as_bool())
            bits = valid[rows].astype(np.uint8)
            if not node._valid_when:
                bits = 1 - bits
            packed = self.backend.nplike.packbits(
                bits, bitorder="little" if node._lsb_order else "big"
            )
            return ak.contents.BitMaskedArray(
                ak.index.IndexU8(
                    self.backend.nplike.asarray(packed, dtype=np.uint8)
                ),
                child,
                node._valid_when,
                len(rows),
                node._lsb_order,
                parameters=node._parameters,
            )

        if isinstance(node, ak.contents.UnmaskedArray):
            child = self.build(node.content, rows)
            return ak.contents.UnmaskedArray(child, parameters=node._parameters)

        if isinstance(node, ak.contents.RecordArray):
            children = _child_rows(node, rows)
            contents = [
                self.build(node.content(i), children[i])
                for i in range(len(node.contents))
            ]
            return ak.contents.RecordArray(
                contents, node.fields, len(rows), parameters=node._parameters
            )

        if isinstance(node, ak.contents.UnionArray):
            tags = node.tags.data
            index = node.index.data
            children = _child_rows(node, rows)
            contents = [
                self.build(node.content(i), children[i])
                for i in range(len(node.contents))
            ]
            counters = defaultdict(int)
            new_index = []
            for row in rows:
                tag = int(tags[row])
                new_index.append(counters[tag])
                counters[tag] += 1
            return ak.contents.UnionArray(
                ak.index.Index8(
                    self.backend.nplike.asarray(
                        [int(tags[row]) for row in rows], dtype=np.int8
                    )
                ),
                ak.index.Index(
                    self.backend.nplike.asarray(new_index, dtype=index.dtype)
                ),
                contents,
                parameters=node._parameters,
            )

        raise AssertionError(f"unexpected layout: {type(node).__name__}")

    def _numpy(self, node, rows):
        entry = self.registry.leaf(node)
        leaf_id = entry["id"]
        inner = entry["inner"]
        assignment = self.plan.kind[self.request_index].get(
            leaf_id, {"kind": "empty"}
        )
        kind = assignment["kind"]
        origin = _flat(node)

        if kind == "direct":
            flat = origin[assignment["start"] * inner : assignment["stop"] * inner]
        elif kind == "canvas":
            flat = self.backend.nplike.asarray(
                assignment["payload"]
            ).reshape(-1)
            flat = flat[assignment["offset"] : assignment["offset"] + assignment["length"]]
        elif kind == "gathered":
            flat = self.backend.nplike.asarray(
                assignment["payload"]
            ).reshape(-1)
        else:
            flat = origin[:0]

        if node.inner_shape != ():
            flat = self.backend.nplike.reshape(
                flat, (len(flat) // inner, *node.inner_shape)
            )
        return ak.contents.NumpyArray(flat, parameters=node._parameters)


# ---------------------------------------------------------------------------
# sharing policy


def _normalize_shared(shared, original_keys):
    if shared is None:
        return None
    out = defaultdict(list)
    for item in shared:
        try:
            key, start, stop = item
        except (TypeError, ValueError):
            raise TypeError(
                "each 'shared' entry must be a (buffer_key, start, stop) triple"
            ) from None
        if key not in original_keys:
            raise ValueError(
                f"the shareable range {key!r} does not name an original buffer"
            )
        start, stop = int(start), int(stop)
        if start < 0 or stop < start:
            raise ValueError(
                f"the shareable range {key!r}: [{start}, {stop}) is not valid"
            )
        out[key].append((start, stop))
    return out


def _is_shareable(shared_ranges, key, start, stop):
    if shared_ranges is None:
        return True
    return any(
        lo <= start and stop <= hi for lo, hi in shared_ranges.get(key, ())
    )


# ---------------------------------------------------------------------------
# per-row manifest


def _rows_manifest(layout, registry, plan, request_index, rows, prefix, leaf_keys):
    per_row = plan.per_row[request_index]
    flat = {}
    bounds = {}
    for leaf_id, groups in per_row.items():
        sequence = []
        row_bounds = []
        for group in groups:
            row_bounds.append((len(sequence), len(sequence) + len(group)))
            sequence.extend(group)
        flat[leaf_id] = sequence
        bounds[leaf_id] = row_bounds

    out = []
    for output_row, original_row in enumerate(rows):
        missing, tags, _ = _row_usage(layout, original_row, registry)
        if isinstance(layout, ak.contents.IndexedOptionArray):
            missing = int(layout.index[original_row]) < 0
        elif isinstance(layout, ak.contents.ByteMaskedArray):
            missing = not bool(layout.mask_as_bool()[original_row])
        elif isinstance(layout, ak.contents.BitMaskedArray):
            missing = not bool(layout.mask_as_bool()[original_row])
        elif isinstance(layout, ak.contents.EmptyArray):
            missing = False
        else:
            missing = False
        ranges = []
        for leaf_id, groups in per_row.items():
            entry = registry.entries[leaf_id]
            assignment = plan.kind[request_index].get(leaf_id)
            if assignment is None or assignment["kind"] == "empty":
                continue
            start, stop = bounds[leaf_id][output_row]
            if start == stop:
                continue
            sequence = flat[leaf_id][start:stop]
            inner = entry["inner"]
            kind = assignment["kind"]
            if kind == "direct":
                base = assignment["start"]
                emitted_start = (sequence[0] - base) * inner
                emitted_stop = (sequence[-1] + 1 - base) * inner
            elif kind == "canvas":
                base = plan.reached[request_index][leaf_id][0]
                emitted_start = (sequence[0] - base) * inner
                emitted_stop = (sequence[-1] + 1 - base) * inner
            else:
                emitted_start = start * inner
                emitted_stop = stop * inner
            ranges.append(
                {
                    "leaf": leaf_id,
                    "buffer": leaf_keys.get(leaf_id),
                    "start": emitted_start,
                    "stop": emitted_stop,
                    "source": [
                        {
                            "buffer": entry["data_key"],
                            "start": source_start * inner,
                            "stop": source_stop * inner,
                        }
                        for source_start, source_stop in _spans(sequence)
                    ],
                }
            )
        out.append(
            {
                "prefix": prefix,
                "output_row": output_row,
                "row": original_row,
                "missing": missing,
                "tags": tags,
                "ranges": ranges,
            }
        )
    return out


def _group(rows):
    ordered = sorted(set(rows))
    if not ordered:
        return []
    out = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value != previous + 1:
            out.append((start, previous + 1))
            start = value
        previous = value
    out.append((start, previous + 1))
    return out


def _row_usage(layout, row, registry):
    uses = defaultdict(list)
    tags = []
    missing = [False]

    def visit(node, index):
        if isinstance(node, ak.contents.EmptyArray):
            return
        if isinstance(node, ak.contents.NumpyArray):
            uses[registry.leaf(node)["id"]].append(index)
            return
        if isinstance(node, ak.contents.ListOffsetArray):
            visit_range(
                node.content,
                int(node.offsets[index]),
                int(node.offsets[index + 1]),
            )
            return
        if isinstance(node, ak.contents.ListArray):
            visit_range(
                node.content,
                int(node.starts[index]),
                int(node.stops[index]),
            )
            return
        if isinstance(node, ak.contents.RegularArray):
            start = index * int(node.size)
            visit_range(node.content, start, start + int(node.size))
            return
        if isinstance(node, ak.contents.IndexedOptionArray):
            value = int(node.index[index])
            if value < 0:
                missing[0] = True
            else:
                visit(node.content, value)
            return
        if isinstance(node, ak.contents.IndexedArray):
            visit(node.content, int(node.index[index]))
            return
        if isinstance(node, ak.contents.ByteMaskedArray):
            if not bool(node.mask_as_bool()[index]):
                missing[0] = True
            else:
                visit(node.content, index)
            return
        if isinstance(node, ak.contents.BitMaskedArray):
            if not bool(node.mask_as_bool()[index]):
                missing[0] = True
            else:
                visit(node.content, index)
            return
        if isinstance(node, ak.contents.UnmaskedArray):
            visit(node.content, index)
            return
        if isinstance(node, ak.contents.RecordArray):
            for content in node.contents:
                visit(content, index)
            return
        if isinstance(node, ak.contents.UnionArray):
            tag = int(node.tags[index])
            value = int(node.index[index])
            tags.append({"tag": tag, "index": value})
            visit(node.contents[tag], value)
            return
        raise AssertionError(f"unexpected layout: {type(node).__name__}")

    def visit_range(node, start, stop):
        for child in range(start, stop):
            visit(node, child)

    visit(layout, row)
    return missing[0], tags, uses
