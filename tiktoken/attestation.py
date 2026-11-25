"""Joint byte-prefix attestation for two fixed tiktoken Encodings.

The public entry point is :func:`attest_catalog`.  A catalog contains immutable
``pieces``, an ordered ``received`` list of piece references, and a DAG whose
nodes append future piece occurrences.  Piece ids and DAG node ids are separate,
so the same source piece can occur repeatedly.

The attestation boundary is a UTF-8 byte boundary and a token boundary for both
encodings under every complete DAG path.  :func:`verify_small_catalog` is an
exhaustive native-entry oracle for small catalogs.
"""

from __future__ import annotations

import base64
import bisect
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import AbstractSet, Any, Mapping, Sequence

from .core import Encoding

Side = str
PathId = str


def coerce_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, Mapping):
        if "hex" in value:
            return bytes.fromhex(value["hex"])
        if "base64" in value:
            return base64.b64decode(value["base64"])
        if "utf8" in value:
            return str(value["utf8"]).encode("utf-8")
        if "text" in value:
            return str(value["text"]).encode("utf-8")
        if "bytes" in value:
            return bytes(value["bytes"])
    raise TypeError(f"unsupported byte value: {type(value).__name__}")


def is_utf8_boundary(data: bytes, offset: int) -> bool:
    if offset < 0 or offset > len(data):
        return False
    if offset == 0 or offset == len(data):
        return True
    return (data[offset] & 0xC0) != 0x80


def utf8_boundaries(data: bytes) -> list[int]:
    boundaries = [0]
    index = 0
    length = len(data)
    while index < length:
        byte = data[index]
        if byte < 0x80:
            width = 1
        elif 0xC2 <= byte <= 0xDF:
            width = 2
        elif 0xE0 <= byte <= 0xEF:
            width = 3
        elif 0xF0 <= byte <= 0xF4:
            width = 4
        else:
            break

        end = index + width
        if end > length:
            break
        valid = True
        for continuation_index in range(index + 1, end):
            if (data[continuation_index] & 0xC0) != 0x80:
                valid = False
                break
        if not valid:
            break
        index = end
        boundaries.append(index)
    return boundaries


def incomplete_utf8_width(data: bytes, offset: int) -> int | None:
    if offset >= len(data):
        return None
    byte = data[offset]
    if byte < 0x80:
        return 1
    if 0xC2 <= byte <= 0xDF:
        return 2
    if 0xE0 <= byte <= 0xEF:
        return 3
    if 0xF0 <= byte <= 0xF4:
        return 4
    return None


@dataclass(frozen=True)
class SourceLocation:
    occurrence: int
    piece_id: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Occurrence:
    index: int
    piece_id: str
    start: int
    end: int
    global_start: int
    global_end: int


@dataclass(frozen=True)
class ByteMap:
    data: bytes
    occurrences: tuple[Occurrence, ...]
    _starts: tuple[int, ...] = field(repr=False)

    @classmethod
    def build(cls, received: Sequence[Any], pieces: Mapping[str, bytes]) -> "ByteMap":
        occurrences: list[Occurrence] = []
        chunks: list[bytes] = []
        starts: list[int] = []
        position = 0

        for index, item in enumerate(received):
            raw_start = 0
            raw_end: int | None = None
            if isinstance(item, str):
                piece_id = item
            elif isinstance(item, Mapping):
                piece_id = str(item.get("piece") or item.get("id") or f"@raw-{index}")
                raw_start = int(item.get("start", 0))
                raw_end = item.get("end")
                if "bytes" in item or "data" in item:
                    raw = coerce_bytes(item.get("bytes", item.get("data")))
                    pieces = {**pieces, piece_id: raw}
            elif isinstance(item, (tuple, list)) and item:
                piece_id = str(item[0])
                if len(item) > 1:
                    raw_start = int(item[1])
                if len(item) > 2:
                    raw_end = int(item[2])
            else:
                raise TypeError(f"unsupported received piece reference: {item!r}")

            if piece_id not in pieces:
                raise KeyError(f"unknown piece {piece_id!r}")
            piece = pieces[piece_id]
            if raw_end is None:
                raw_end = len(piece)
            if not (0 <= raw_start <= raw_end <= len(piece)):
                raise ValueError(f"invalid source range for piece {piece_id!r}")

            chunk = piece[raw_start:raw_end]
            starts.append(position)
            occurrences.append(
                Occurrence(
                    index=index,
                    piece_id=piece_id,
                    start=raw_start,
                    end=raw_end,
                    global_start=position,
                    global_end=position + len(chunk),
                )
            )
            chunks.append(chunk)
            position += len(chunk)

        data = b"".join(chunks)
        return cls(data=data, occurrences=tuple(occurrences), _starts=tuple(starts))

    def source_locations(self, start: int, end: int) -> tuple[SourceLocation, ...]:
        if end <= start:
            return ()
        if not self.occurrences:
            return ()
        occurrence_index = bisect.bisect_right(self._starts, start) - 1
        occurrence_index = max(0, min(occurrence_index, len(self.occurrences) - 1))
        locations: list[SourceLocation] = []
        cursor = start
        while cursor < end:
            occurrence = self.occurrences[occurrence_index]
            intersection_end = min(end, occurrence.global_end)
            source_start = occurrence.start + cursor - occurrence.global_start
            source_end = occurrence.start + intersection_end - occurrence.global_start
            if intersection_end > cursor:
                locations.append(
                    SourceLocation(
                        occurrence=occurrence.index,
                        piece_id=occurrence.piece_id,
                        start=source_start,
                        end=source_end,
                    )
                )
            cursor = intersection_end
            if cursor >= end:
                break
            occurrence_index += 1
            if occurrence_index >= len(self.occurrences):
                break
        return tuple(locations)

    def source_positions_at(self, offset: int) -> list[dict[str, Any]]:
        positions: list[dict[str, Any]] = []
        for occurrence in self.occurrences:
            if occurrence.global_start <= offset <= occurrence.global_end:
                piece_offset = occurrence.start + offset - occurrence.global_start
                if offset == occurrence.global_start == occurrence.global_end:
                    relation = "empty"
                elif offset == occurrence.global_start:
                    relation = "start"
                elif offset == occurrence.global_end:
                    relation = "end"
                else:
                    relation = "inside"
                positions.append(
                    {
                        "occurrence": occurrence.index,
                        "piece_id": occurrence.piece_id,
                        "piece_byte": piece_offset,
                        "relation": relation,
                    }
                )
        return positions


@dataclass(frozen=True)
class ReplayPath:
    node_ids: tuple[str, ...]
    piece_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"nodes": list(self.node_ids), "pieces": list(self.piece_ids)}


@dataclass(frozen=True)
class CompletionPath:
    replay: ReplayPath
    data: bytes


@dataclass(frozen=True)
class DagNode:
    node_id: str
    piece_id: str | None = None
    start: int = 0
    end: int | None = None
    children: tuple[str, ...] = ()
    terminal: bool = False


@dataclass(frozen=True)
class Catalog:
    pieces: dict[str, bytes]
    received: tuple[Any, ...]
    nodes: dict[str, DagNode]
    roots: tuple[str, ...] = ()
    terminals: frozenset[str] = frozenset()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Catalog":
        def first_key(*names: str, default: Any = None) -> Any:
            for name in names:
                if name in payload and payload[name] is not None:
                    return payload[name]
            return default

        raw_pieces = first_key(
            "pieces", "piece_inventory", "available_pieces", "sources", "blobs", default={}
        )
        pieces: dict[str, bytes] = {}
        if isinstance(raw_pieces, Mapping):
            piece_items = raw_pieces.items()
        else:
            piece_items = (
                (item.get("id", item.get("piece_id", item.get("name"))), item)
                for item in raw_pieces
            )
        for piece_id, piece in piece_items:
            if piece_id is None:
                raise ValueError("every piece needs an id")
            pieces[str(piece_id)] = coerce_bytes(piece)

        dag_payload: Any = first_key("dag", "graph", "directory", default=payload)
        if isinstance(dag_payload, Mapping) and "nodes" in dag_payload:
            raw_nodes: Any = dag_payload.get("nodes", {})
        elif isinstance(dag_payload, Mapping) and any(
            key in dag_payload for key in ("edges", "transitions")
        ):
            raw_nodes = {}
        else:
            raw_nodes = dag_payload if dag_payload is not payload else payload.get("nodes", {})

        edge_list: Sequence[Any] = ()
        for candidate in (
            payload.get("edges"),
            payload.get("transitions"),
            dag_payload.get("edges") if isinstance(dag_payload, Mapping) else None,
            dag_payload.get("transitions") if isinstance(dag_payload, Mapping) else None,
            dag_payload if isinstance(dag_payload, list) else None,
        ):
            if candidate:
                edge_list = candidate
                break

        children_by_node: dict[str, list[str]] = {}
        edge_pieces: dict[str, str] = {}
        for edge in edge_list:
            if not isinstance(edge, Mapping):
                continue
            source = edge.get("from", edge.get("source", edge.get("parent")))
            target = edge.get("to", edge.get("target", edge.get("child")))
            if source is None or target is None:
                continue
            source = str(source)
            target = str(target)
            children_by_node.setdefault(source, []).append(target)
            children_by_node.setdefault(target, [])
            edge_piece = edge.get("piece", edge.get("piece_id"))
            if edge_piece is not None:
                edge_pieces[target] = str(edge_piece)

        nodes: dict[str, DagNode] = {}
        if isinstance(raw_nodes, Mapping):
            node_items = raw_nodes.items()
        elif isinstance(raw_nodes, Sequence) and not isinstance(raw_nodes, (str, bytes)):
            node_items = ((node["id"], node) for node in raw_nodes)
        else:
            node_items = ()

        for node_id, node in node_items:
            node_id = str(node_id)
            if isinstance(node, (list, tuple, set, frozenset)):
                node = {"next": tuple(node), "piece": node_id if node_id in pieces else None}
            elif isinstance(node, str):
                node = {"piece": node}
            elif not isinstance(node, Mapping):
                raise TypeError(f"unsupported DAG node {node_id!r}: {type(node).__name__}")

            piece_value = node.get("piece", node.get("piece_id", edge_pieces.get(node_id)))
            if piece_value is None and node_id in pieces and "piece" not in node:
                piece_value = node_id
            piece_id = None if piece_value is None else str(piece_value)
            children = node.get(
                "next",
                node.get("children", node.get("edges", children_by_node.get(node_id, ()))),
            )
            if isinstance(children, str):
                children = (children,)
            nodes[node_id] = DagNode(
                node_id=node_id,
                piece_id=piece_id,
                start=int(node.get("start", 0)),
                end=node.get("end"),
                children=tuple(str(child) for child in children),
                terminal=bool(node.get("terminal", False)),
            )

        for source, children in children_by_node.items():
            existing = nodes.get(source)
            if existing is None:
                nodes[source] = DagNode(source, children=tuple(children))
            else:
                merged = tuple(dict.fromkeys((*existing.children, *children)))
                nodes[source] = DagNode(
                    existing.node_id,
                    existing.piece_id,
                    existing.start,
                    existing.end,
                    merged,
                    existing.terminal,
                )
            for target in children:
                nodes.setdefault(target, DagNode(target, piece_id=target if target in pieces else None))

        roots_value = first_key(
            "roots",
            "start",
            "start_nodes",
            "entrypoints",
            "next_pieces",
            default=(
                dag_payload.get("roots") if isinstance(dag_payload, Mapping) else None
            )
            or (dag_payload.get("start") if isinstance(dag_payload, Mapping) else None),
        )
        frontier = first_key(
            "frontier", "current_node", "last_node", "after", "current_piece", "last_piece"
        )
        if roots_value is None and frontier is not None and str(frontier) in nodes:
            roots_value = nodes[str(frontier)].children
        if roots_value is None:
            roots = ()
        elif isinstance(roots_value, str):
            roots = (roots_value,)
        else:
            roots = tuple(str(root) for root in roots_value)

        terminals_value = first_key(
            "terminals",
            "terminal_nodes",
            "sinks",
            default=dag_payload.get("terminals") if isinstance(dag_payload, Mapping) else None,
        )
        if terminals_value is None:
            terminals = frozenset()
        elif isinstance(terminals_value, str):
            terminals = frozenset({terminals_value})
        else:
            terminals = frozenset(str(item) for item in terminals_value)

        received = tuple(
            first_key(
                "received",
                "received_pieces",
                "current_batch",
                "batch",
                "prefix_refs",
                default=(),
            )
        )
        return cls(
            pieces=pieces,
            received=received,
            nodes=nodes,
            roots=roots,
            terminals=terminals,
        )

    def byte_map(self) -> ByteMap:
        return ByteMap.build(self.received, self.pieces)

    def node_bytes(self, node: DagNode) -> tuple[str | None, bytes]:
        if node.piece_id is None:
            return None, b""
        if node.piece_id not in self.pieces:
            raise KeyError(f"DAG node {node.node_id!r} references unknown piece {node.piece_id!r}")
        data = self.pieces[node.piece_id]
        end = len(data) if node.end is None else node.end
        if not (0 <= node.start <= end <= len(data)):
            raise ValueError(f"invalid range in DAG node {node.node_id!r}")
        return node.piece_id, data[node.start:end]

    def iter_completion_paths(
        self, max_paths: int | None = None
    ) -> Any:
        roots = self.roots
        if not roots:
            incoming: set[str] = set()
            for node in self.nodes.values():
                incoming.update(node.children)
            roots = tuple(node_id for node_id in self.nodes if node_id not in incoming)

        if not roots:
            yield CompletionPath(ReplayPath((), ()), b"")
            return

        stack: list[tuple[str, tuple[str, ...], tuple[str, ...], bytes, frozenset[str]]] = []
        for root in reversed(roots):
            stack.append((root, (), (), b"", frozenset()))

        produced = 0
        while stack:
            node_id, node_path, piece_path, data, active = stack.pop()
            if max_paths is not None and produced >= max_paths:
                return
            if node_id in active:
                raise ValueError(f"DAG contains a cycle at {node_id!r}")
            if node_id not in self.nodes:
                raise KeyError(f"DAG references unknown node {node_id!r}")

            node = self.nodes[node_id]
            piece_id, chunk = self.node_bytes(node)
            next_node_path = node_path + (node_id,)
            next_piece_path = piece_path + ((piece_id,) if piece_id is not None else ())
            next_data = data + chunk

            is_terminal = (
                node.terminal
                or node_id in self.terminals
                or (not node.children and not self.terminals)
            )
            if is_terminal:
                yield CompletionPath(
                    ReplayPath(next_node_path, next_piece_path), next_data
                )
                produced += 1
                if max_paths is not None and produced >= max_paths:
                    return
                if node.terminal or node_id in self.terminals:
                    continue

            next_active = active | {node_id}
            for child in reversed(node.children):
                stack.append(
                    (
                        child,
                        next_node_path,
                        next_piece_path,
                        next_data,
                        next_active,
                    )
                )

    def iter_completions(self, max_paths: int | None = None) -> list[CompletionPath]:
        return list(self.iter_completion_paths(max_paths))


@dataclass(frozen=True)
class TokenAnchor:
    index: int
    token: int
    start: int
    end: int

    def to_dict(self, byte_map: ByteMap | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "index": self.index,
            "token": self.token,
            "start_byte": self.start,
            "end_byte": self.end,
        }
        if byte_map is not None and self.end <= len(byte_map.data):
            result["sources"] = [
                location.to_dict()
                for location in byte_map.source_locations(self.start, self.end)
            ]
        elif byte_map is not None:
            result["received_sources"] = [
                location.to_dict()
                for location in byte_map.source_locations(
                    self.start, min(self.end, len(byte_map.data))
                )
            ]
        return result


@dataclass(frozen=True)
class PathPrefixView:
    replay: ReplayPath
    anchors: tuple[TokenAnchor, ...]


@dataclass(frozen=True)
class Witness:
    side: str
    kind: str
    boundary: int
    message: str
    replay_path: ReplayPath
    token: int | None = None
    start_byte: int | None = None
    end_byte: int | None = None
    sources: tuple[SourceLocation, ...] = ()
    other_replay_path: ReplayPath | None = None
    other_token: int | None = None
    other_start_byte: int | None = None
    other_end_byte: int | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["replay_path"] = self.replay_path.to_dict()
        if self.other_replay_path is not None:
            result["other_replay_path"] = self.other_replay_path.to_dict()
        result["sources"] = [location.to_dict() for location in self.sources]
        return result


@dataclass
class SideAttestation:
    side: str
    signed_byte: int
    tokens: list[int]
    anchors: list[dict[str, Any]]
    endpoints: list[int]
    path_views: list[PathPrefixView] = field(repr=False)
    allowed_special: frozenset[str] = field(default_factory=frozenset)


@dataclass
class AttestationReport:
    signed_byte: int
    utf8_character_boundary: bool
    prefix_hex: str
    prefix_text: str
    left: SideAttestation
    right: SideAttestation
    next_unsignable_boundary: int | None
    next_unsignable_character_index: int | None = None
    witness: Witness | None = None
    boundary_evidence: dict[str, Any] = field(default_factory=dict)
    occurrences: list[dict[str, Any]] = field(default_factory=list)
    signed_source_positions: list[dict[str, Any]] = field(default_factory=list)
    native_verification: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        def side_dict(side: SideAttestation) -> dict[str, Any]:
            return {
                "signed_byte": side.signed_byte,
                "tokens": side.tokens,
                "anchors": side.anchors,
                "endpoints": side.endpoints,
                "allowed_special": sorted(side.allowed_special),
            }

        return {
            "signed_byte": self.signed_byte,
            "signed_character_index": len(self.prefix_text),
            "utf8_character_boundary": self.utf8_character_boundary,
            "prefix_hex": self.prefix_hex,
            "prefix_text": self.prefix_text,
            "left": side_dict(self.left),
            "right": side_dict(self.right),
            "next_unsignable_boundary": self.next_unsignable_boundary,
            "next_unsignable_character_index": self.next_unsignable_character_index,
            "witness": None if self.witness is None else self.witness.to_dict(),
            "boundary_evidence": self.boundary_evidence,
            "occurrences": self.occurrences,
            "signed_source_positions": self.signed_source_positions,
            "native_verification": self.native_verification,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)


def _allowed_set(value: AbstractSet[str] | Sequence[str] | str | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    if value == "all":
        raise ValueError("allowed_special='all' requires the Encoding's special set")
    return frozenset(value)


def _manifest_allowed_special(
    payload: Mapping[str, Any] | None,
    side: str,
    explicit: AbstractSet[str] | Sequence[str] | str | None,
) -> AbstractSet[str] | Sequence[str] | str | None:
    if explicit is not None:
        return explicit
    if payload is None:
        return None
    side_value = payload.get(f"allowed_special_{side}")
    if side_value is not None:
        return side_value
    shared = payload.get("allowed_special", payload.get("allowed_special_tokens"))
    if isinstance(shared, Mapping):
        return shared.get(side, shared.get("default"))
    return shared


def tokenize_native(
    encoding: Encoding,
    data: bytes,
    allowed_special: AbstractSet[str] | Sequence[str] | str | None = None,
) -> tuple[list[int], list[int], list[int]]:
    if allowed_special == "all":
        allowed = frozenset(encoding.special_tokens_set)
    else:
        allowed = _allowed_set(allowed_special)
    text = data.decode("utf-8", errors="strict")
    tokens = encoding.encode(text, allowed_special=allowed, disallowed_special=())
    decoded, starts, ends = encoding.decode_with_byte_offsets(tokens)
    if decoded != data:
        raise AssertionError("decoded token bytes do not reconstruct native input")
    return tokens, starts, ends


def _prefix_view(
    encoding: Encoding,
    prefix: bytes,
    completion: CompletionPath,
    allowed_special: AbstractSet[str] | Sequence[str] | None,
) -> PathPrefixView:
    allowed = _allowed_set(allowed_special)
    data = prefix + completion.data
    text = data.decode("utf-8", errors="strict")
    tokens = encoding.encode(text, allowed_special=allowed, disallowed_special=())

    anchors: list[TokenAnchor] = []
    position = 0
    for index, token in enumerate(tokens):
        token_bytes = encoding.decode_single_token_bytes(token)
        start = position
        end = start + len(token_bytes)
        position = end
        if start >= len(prefix):
            break
        anchors.append(TokenAnchor(index=index, token=token, start=start, end=end))
    return PathPrefixView(completion.replay, tuple(anchors))


def _intersect_prefixes(
    common: Sequence[TokenAnchor], candidates: Sequence[TokenAnchor]
) -> tuple[TokenAnchor, ...]:
    next_common: list[TokenAnchor] = []
    for index, anchor in enumerate(common):
        if index >= len(candidates):
            break
        candidate = candidates[index]
        if (
            anchor.token != candidate.token
            or anchor.start != candidate.start
            or anchor.end != candidate.end
        ):
            break
        next_common.append(anchor)
    return tuple(next_common)


def _intersect_views(views: Sequence[PathPrefixView]) -> tuple[TokenAnchor, ...]:
    if not views:
        return ()
    common = views[0].anchors
    for view in views[1:]:
        common = _intersect_prefixes(common, view.anchors)
        if not common:
            break
    return tuple(common)


def _side_attestation(
    side: str,
    byte_map: ByteMap,
    views: Sequence[PathPrefixView],
    common: Sequence[TokenAnchor],
    signed_byte: int,
    allowed_special: frozenset[str],
) -> SideAttestation:
    signed_anchors = [anchor for anchor in common if anchor.end <= signed_byte]
    endpoints = sorted({0, *(anchor.end for anchor in common if anchor.end <= len(byte_map.data))})
    return SideAttestation(
        side=side,
        signed_byte=signed_byte,
        tokens=[anchor.token for anchor in signed_anchors],
        anchors=[anchor.to_dict(byte_map) for anchor in signed_anchors],
        endpoints=endpoints,
        path_views=list(views),
        allowed_special=allowed_special,
    )


def _anchor_at(
    anchors: Sequence[TokenAnchor], boundary: int
) -> tuple[TokenAnchor | None, TokenAnchor | None, TokenAnchor | None]:
    crossing = None
    ending = None
    starting = None
    for anchor in anchors:
        if anchor.start < boundary < anchor.end:
            crossing = anchor
        if anchor.end == boundary:
            ending = anchor
        if anchor.start == boundary:
            starting = anchor
    return crossing, ending, starting


def _boundary_evidence(
    side: str,
    boundary: int,
    views: Sequence[PathPrefixView],
    byte_map: ByteMap,
) -> dict[str, Any]:
    crossing_examples: list[dict[str, Any]] = []
    variants: dict[str, dict[str, Any]] = {}

    for view in views:
        crossing, ending, starting = _anchor_at(view.anchors, boundary)
        target = crossing or ending or starting
        if crossing is not None and len(crossing_examples) < 3:
            crossing_examples.append(
                {
                    "token": crossing.token,
                    "start_byte": crossing.start,
                    "end_byte": crossing.end,
                    "replay_path": view.replay.to_dict(),
                    "sources": [
                        location.to_dict()
                        for location in byte_map.source_locations(
                            crossing.start, min(crossing.end, len(byte_map.data))
                        )
                    ],
                }
            )
        if target is not None:
            key = f"{target.token}:{target.start}:{target.end}"
            variants.setdefault(
                key,
                {
                    "token": target.token,
                    "start_byte": target.start,
                    "end_byte": target.end,
                    "relation": "crossing"
                    if crossing is not None
                    else "ending"
                    if ending is not None
                    else "starting",
                    "example_path": view.replay.to_dict(),
                },
            )

    return {
        "side": side,
        "boundary": boundary,
        "crossing_examples": crossing_examples,
        "variants": list(variants.values())[:8],
        "is_stable_boundary": len(variants) == 1 and not crossing_examples,
    }


def _choose_witness(
    boundary: int,
    left_views: Sequence[PathPrefixView],
    right_views: Sequence[PathPrefixView],
    byte_map: ByteMap,
) -> tuple[Witness | None, dict[str, Any]]:
    evidence = {
        "left": _boundary_evidence("left", boundary, left_views, byte_map),
        "right": _boundary_evidence("right", boundary, right_views, byte_map),
    }

    for side_name, views in (("left", left_views), ("right", right_views)):
        for view in views:
            crossing, _, _ = _anchor_at(view.anchors, boundary)
            if crossing is not None:
                sources = byte_map.source_locations(
                    crossing.start, min(crossing.end, len(byte_map.data))
                )
                return (
                    Witness(
                        side=side_name,
                        kind="token_crosses_boundary",
                        boundary=boundary,
                        message=(
                            f"{side_name} token {crossing.token} spans "
                            f"[{crossing.start}, {crossing.end}) across byte {boundary}"
                        ),
                        replay_path=view.replay,
                        token=crossing.token,
                        start_byte=crossing.start,
                        end_byte=crossing.end,
                        sources=sources,
                    ),
                    evidence,
                )

    for side_name, views in (("left", left_views), ("right", right_views)):
        signatures: dict[tuple[int, int, int], PathPrefixView] = {}
        for view in views:
            _, ending, starting = _anchor_at(view.anchors, boundary)
            target = ending or starting
            if target is None:
                continue
            signature = (target.token, target.start, target.end)
            if signature not in signatures:
                signatures[signature] = view
            if len(signatures) >= 2:
                items = list(signatures.items())
                (first_sig, first_view), (second_sig, second_view) = items[0], items[-1]
                return (
                    Witness(
                        side=side_name,
                        kind="token_changed_under_completion",
                        boundary=boundary,
                        message=(
                            f"{side_name} token at boundary changes from "
                            f"{first_sig[0]} [{first_sig[1]}, {first_sig[2]}) to "
                            f"{second_sig[0]} [{second_sig[1]}, {second_sig[2]})"
                        ),
                        replay_path=second_view.replay,
                        token=second_sig[0],
                        start_byte=second_sig[1],
                        end_byte=second_sig[2],
                        other_replay_path=first_view.replay,
                        other_token=first_sig[0],
                        other_start_byte=first_sig[1],
                        other_end_byte=first_sig[2],
                    ),
                    evidence,
                )

    return None, evidence


def attest_catalog(
    left: Encoding,
    right: Encoding,
    catalog: Catalog | Mapping[str, Any],
    *,
    allowed_special_left: AbstractSet[str] | Sequence[str] | str | None = None,
    allowed_special_right: AbstractSet[str] | Sequence[str] | str | None = None,
    max_paths: int | None = None,
) -> AttestationReport:
    """Find the maximum jointly attestable byte prefix for a DAG corpus.

    The result is exact with respect to all complete DAG paths.  Small catalogs
    should additionally be checked with :func:`verify_small_catalog`.
    """
    manifest_mapping = catalog if isinstance(catalog, Mapping) else None
    if isinstance(catalog, Mapping):
        nested = catalog.get("catalog")
        if isinstance(nested, Mapping) and (
            "pieces" in nested or "dag" in nested or "nodes" in nested
        ):
            catalog = nested
        catalog = Catalog.from_dict(catalog)

    allowed_special_left = _manifest_allowed_special(
        manifest_mapping, "left", allowed_special_left
    )
    allowed_special_right = _manifest_allowed_special(
        manifest_mapping, "right", allowed_special_right
    )
    allowed_left = (
        frozenset(left.special_tokens_set)
        if allowed_special_left == "all"
        else _allowed_set(allowed_special_left)
    )
    allowed_right = (
        frozenset(right.special_tokens_set)
        if allowed_special_right == "all"
        else _allowed_set(allowed_special_right)
    )

    byte_map = catalog.byte_map()
    prefix = byte_map.data
    completion_paths = catalog.iter_completion_paths(max_paths=max_paths)
    try:
        first_completion = next(completion_paths)
    except StopIteration as error:
        raise ValueError("DAG has no complete terminal path") from error

    first_left_view = _prefix_view(left, prefix, first_completion, allowed_left)
    first_right_view = _prefix_view(right, prefix, first_completion, allowed_right)
    left_views = [first_left_view]
    right_views = [first_right_view]
    left_common = first_left_view.anchors
    right_common = first_right_view.anchors

    for completion in completion_paths:
        left_view = _prefix_view(left, prefix, completion, allowed_left)
        right_view = _prefix_view(right, prefix, completion, allowed_right)
        left_views.append(left_view)
        right_views.append(right_view)
        left_common = _intersect_prefixes(left_common, left_view.anchors)
        right_common = _intersect_prefixes(right_common, right_view.anchors)

    left_endpoints = {0}
    left_endpoints.update(anchor.end for anchor in left_common if anchor.end <= len(prefix))
    right_endpoints = {0}
    right_endpoints.update(anchor.end for anchor in right_common if anchor.end <= len(prefix))
    valid_boundaries = set(utf8_boundaries(prefix))

    joint_endpoints = left_endpoints & right_endpoints & valid_boundaries
    signed_byte = max(joint_endpoints)

    left_attestation = _side_attestation(
        "left", byte_map, left_views, left_common, signed_byte, allowed_left
    )
    right_attestation = _side_attestation(
        "right", byte_map, right_views, right_common, signed_byte, allowed_right
    )

    next_boundary = next(
        (boundary for boundary in utf8_boundaries(prefix) if boundary > signed_byte),
        None,
    )
    next_character_index = (
        len(prefix[:next_boundary].decode("utf-8", errors="strict"))
        if next_boundary is not None
        else None
    )
    witness = None
    evidence: dict[str, Any] = {}
    if next_boundary is not None:
        witness, evidence = _choose_witness(
            next_boundary, left_views, right_views, byte_map
        )

    prefix_signed = prefix[:signed_byte]
    occurrences = [
        {
            "occurrence": occurrence.index,
            "piece_id": occurrence.piece_id,
            "piece_start": occurrence.start,
            "piece_end": occurrence.end,
            "global_start": occurrence.global_start,
            "global_end": occurrence.global_end,
        }
        for occurrence in byte_map.occurrences
    ]

    return AttestationReport(
        signed_byte=signed_byte,
        utf8_character_boundary=is_utf8_boundary(prefix, signed_byte),
        prefix_hex=prefix_signed.hex(),
        prefix_text=prefix_signed.decode("utf-8", errors="strict"),
        left=left_attestation,
        right=right_attestation,
        next_unsignable_boundary=next_boundary,
        next_unsignable_character_index=next_character_index,
        witness=witness,
        boundary_evidence=evidence,
        occurrences=occurrences,
        signed_source_positions=byte_map.source_positions_at(signed_byte),
    )


def verify_small_catalog(
    left: Encoding,
    right: Encoding,
    catalog: Catalog | Mapping[str, Any],
    report: AttestationReport | None = None,
    *,
    allowed_special_left: AbstractSet[str] | Sequence[str] | str | None = None,
    allowed_special_right: AbstractSet[str] | Sequence[str] | str | None = None,
) -> dict[str, Any]:
    """Exhaustively verify a small DAG through public and Rust native entries."""
    manifest_mapping = catalog if isinstance(catalog, Mapping) else None
    if isinstance(catalog, Mapping):
        nested = catalog.get("catalog")
        if isinstance(nested, Mapping) and (
            "pieces" in nested or "dag" in nested or "nodes" in nested
        ):
            catalog = nested
        catalog = Catalog.from_dict(catalog)

    allowed_special_left = _manifest_allowed_special(
        manifest_mapping, "left", allowed_special_left
    )
    allowed_special_right = _manifest_allowed_special(
        manifest_mapping, "right", allowed_special_right
    )
    if report is None:
        report = attest_catalog(
            left,
            right,
            catalog,
            allowed_special_left=allowed_special_left,
            allowed_special_right=allowed_special_right,
        )

    allowed_left = (
        frozenset(left.special_tokens_set)
        if allowed_special_left == "all"
        else _allowed_set(allowed_special_left)
    )
    allowed_right = (
        frozenset(right.special_tokens_set)
        if allowed_special_right == "all"
        else _allowed_set(allowed_special_right)
    )
    allowed = (allowed_left, allowed_right)
    encodings = (left, right)
    prefix = catalog.byte_map().data
    completions = catalog.iter_completions()

    checked = 0
    for completion in completions:
        full_bytes = prefix + completion.data
        text = full_bytes.decode("utf-8", errors="strict")
        for side_name, encoding, special in zip(("left", "right"), encodings, allowed):
            python_tokens = encoding.encode(
                text, allowed_special=special, disallowed_special=()
            )
            rust_tokens = encoding._core_bpe.encode(text, set(special))
            if python_tokens != rust_tokens:
                raise AssertionError(f"{side_name}: Python/Rust encode mismatch")

            decoded, starts, ends = encoding.decode_with_byte_offsets(python_tokens)
            if decoded != full_bytes:
                raise AssertionError(f"{side_name}: byte offset reconstruction failed")
            char_text, char_offsets = encoding.decode_with_offsets(python_tokens)
            if char_text != text:
                raise AssertionError(f"{side_name}: character offset text mismatch")
            for index, byte_start in enumerate(starts):
                character_start = byte_start
                while (
                    character_start > 0
                    and (full_bytes[character_start] & 0xC0) == 0x80
                ):
                    character_start -= 1
                expected_character_offset = len(
                    full_bytes[:character_start].decode("utf-8", errors="strict")
                )
                if char_offsets[index] != expected_character_offset:
                    raise AssertionError(
                        f"{side_name}: character/byte offset mismatch at token {index}"
                    )

            signed = report.signed_byte
            signed_count = sum(end <= signed for end in ends)
            if starts[:signed_count] and starts[signed_count - 1] >= signed:
                raise AssertionError("signed token does not end at attestation boundary")
            if any(start < signed < end for start, end in zip(starts, ends)):
                raise AssertionError(f"{side_name}: native token crosses signed boundary")
            expected_tokens = (
                report.left.tokens if side_name == "left" else report.right.tokens
            )
            if python_tokens[:signed_count] != expected_tokens:
                raise AssertionError(f"{side_name}: signed token ids differ from native encode")
        checked += 1

    boundaries = utf8_boundaries(prefix)
    for boundary in boundaries:
        if boundary <= report.signed_byte:
            continue
        jointly_safe = True
        for encoding, special in ((left, allowed_left), (right, allowed_right)):
            reference_signature = None
            for completion in completions:
                data = prefix + completion.data
                tokens, starts, ends = tokenize_native(encoding, data, special)
                if any(start < boundary < end for start, end in zip(starts, ends)):
                    jointly_safe = False
                    break
                count = sum(end <= boundary for end in ends)
                signature = tuple(
                    (tokens[index], starts[index], ends[index]) for index in range(count)
                )
                if reference_signature is None:
                    reference_signature = signature
                elif reference_signature != signature:
                    jointly_safe = False
                    break
            if not jointly_safe:
                break
        if jointly_safe:
            raise AssertionError(f"report is not maximal: byte {boundary} is jointly safe")

    verification = {
        "verified": True,
        "complete_paths_checked": checked,
        "utf8_boundaries_checked": len(boundaries),
        "native_entries": [
            "tiktoken.Encoding.encode",
            "tiktoken.Encoding.decode_with_offsets",
            "tiktoken.Encoding.decode_with_byte_offsets",
            "tiktoken._tiktoken.CoreBPE.encode",
        ],
    }
    report.native_verification = verification
    return verification


def write_report(report: AttestationReport, path: str | Path) -> None:
    Path(path).write_text(report.to_json(), encoding="utf-8")


find_joint_attestation = attest_catalog
find_maximum_signable_prefix = attest_catalog
check_corpus = attest_catalog
attest = attest_catalog


def build_encoding(spec: Encoding | str | Mapping[str, Any]) -> Encoding:
    if isinstance(spec, Encoding):
        return spec
    if isinstance(spec, str):
        from .registry import get_encoding

        return get_encoding(spec)

    name = str(spec.get("name", "task-encoding"))
    pat_str = str(spec["pat_str"])
    raw_ranks = spec.get("mergeable_ranks", {})
    mergeable_ranks: dict[bytes, int] = {}

    rank_items: Any = raw_ranks.items() if isinstance(raw_ranks, Mapping) else raw_ranks
    for key, rank in rank_items:
        if isinstance(key, str):
            key_bytes = key.encode("utf-8")
        elif isinstance(key, int):
            key_bytes = bytes([key])
        elif isinstance(key, Mapping):
            key_bytes = coerce_bytes(key)
        else:
            key_bytes = coerce_bytes(key)
        mergeable_ranks[key_bytes] = int(rank)

    special_tokens = {
        str(token): int(value)
        for token, value in dict(spec.get("special_tokens", {})).items()
    }
    explicit_n_vocab = spec.get("explicit_n_vocab")
    return Encoding(
        name=name,
        pat_str=pat_str,
        mergeable_ranks=mergeable_ranks,
        special_tokens=special_tokens,
        explicit_n_vocab=None if explicit_n_vocab is None else int(explicit_n_vocab),
    )


def _demo_manifest() -> dict[str, Any]:
    return {
        "pieces": {"ab": "ab", "c": "c", "stop": ""},
        "received": ["ab"],
        "dag": {
            "roots": ["stop", "c"],
            "nodes": {
                "stop": {"piece": "stop", "terminal": True},
                "c": {"piece": "c", "terminal": True},
            },
        },
        "left_encoding": {
            "name": "demo-left",
            "pat_str": r"[a-z]+|(?s:.)",
            "mergeable_ranks": [
                *[[index, index] for index in range(256)],
                ["ab", 256],
                ["abc", 257],
            ],
            "special_tokens": {},
        },
        "right_encoding": {
            "name": "demo-right",
            "pat_str": r"[a-z]+|(?s:.)",
            "mergeable_ranks": [
                *[[index, index + 1000] for index in range(256)],
                ["ab", 1256],
            ],
            "special_tokens": {},
        },
        "allowed_special_left": [],
        "allowed_special_right": [],
        "verify": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Attest a joint byte prefix for two tiktoken Encodings")
    parser.add_argument("manifest", nargs="?", help="JSON task manifest")
    parser.add_argument("--output", "-o", help="write JSON report to this path")
    parser.add_argument("--demo", action="store_true", help="run the built-in small catalog")
    args = parser.parse_args(argv)

    if args.demo or not args.manifest:
        payload = _demo_manifest()
    else:
        payload = json.loads(Path(args.manifest).read_text(encoding="utf-8"))

    left = build_encoding(payload.get("left_encoding", payload.get("encoding_left")))
    right = build_encoding(payload.get("right_encoding", payload.get("encoding_right")))
    catalog_payload = payload.get("catalog", payload)
    report = attest_catalog(
        left,
        right,
        catalog_payload,
        allowed_special_left=payload.get("allowed_special_left"),
        allowed_special_right=payload.get("allowed_special_right"),
    )
    if payload.get("verify", args.demo):
        report.native_verification = verify_small_catalog(
            left,
            right,
            catalog_payload,
            report,
            allowed_special_left=payload.get("allowed_special_left"),
            allowed_special_right=payload.get("allowed_special_right"),
        )

    rendered = report.to_json()
    if args.output:
        write_report(report, args.output)
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
