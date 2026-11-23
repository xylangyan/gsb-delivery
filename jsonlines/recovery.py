"""
Forensic recovery for jsonlines append streams.

This module reconstructs audit records from a binary byte range that was
written by old :class:`~jsonlines.Writer` instances appending to the same
stream. The old writer performs two independent stream writes per record::

    fp.write(line)   # JSON body, default serialization, no raw LF
    fp.write(b"\\n")  # line feed, a separate committed write

The append service makes each write atomic (all bytes or nothing), but
writes from different sources may be interleaved, so the body and the line
feed of one record are not necessarily adjacent in the resulting byte
stream. Recovery never modifies the analysed bytes; it only reads them and
reports byte ranges in the original coordinates, so additional evidence can
be re-checked later without shifting any coordinates.
"""

import enum
import json
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

import attr


# ---------------------------------------------------------------------------
# JSON type handling
# ---------------------------------------------------------------------------


class JsonType(enum.Enum):
    """
    JSON value types that sources may contractually produce.
    """

    OBJECT = "object"
    ARRAY = "array"
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    NULL = "null"


_PYTHON_TYPES: Tuple[Tuple[Any, JsonType], ...] = (
    (bool, JsonType.BOOLEAN),  # must precede int, since bool subclasses int
    (int, JsonType.INTEGER),
    (float, JsonType.NUMBER),
    (str, JsonType.STRING),
    (list, JsonType.ARRAY),
    (dict, JsonType.OBJECT),
)


def _json_type_of(value: Any) -> JsonType:
    if value is None:
        return JsonType.NULL
    for python_type, json_type in _PYTHON_TYPES:
        if isinstance(value, python_type):
            return json_type
    raise TypeError(f"value is not a JSON value: {value!r}")


def _coerce_json_type(json_type: Any) -> JsonType:
    if isinstance(json_type, JsonType):
        return json_type
    if isinstance(json_type, type):
        mapping = {
            dict: JsonType.OBJECT,
            list: JsonType.ARRAY,
            str: JsonType.STRING,
            int: JsonType.INTEGER,
            float: JsonType.NUMBER,
            bool: JsonType.BOOLEAN,
            type(None): JsonType.NULL,
        }
        try:
            return mapping[json_type]
        except KeyError:
            raise ValueError(f"unsupported json type: {json_type!r}") from None
    if isinstance(json_type, str):
        try:
            return JsonType(json_type)
        except ValueError:
            raise ValueError(f"unsupported json type: {json_type!r}") from None
    raise TypeError(f"unsupported json type specification: {json_type!r}")


# The encoder mirrors the old Writer default serialization exactly:
# json.JSONEncoder(ensure_ascii=False, separators=(", ", ": ")).
_DEFAULT_ENCODE = json.JSONEncoder(
    ensure_ascii=False, separators=(", ", ": "), sort_keys=False
).encode


# ---------------------------------------------------------------------------
# Receipt: facts remembered about writes
# ---------------------------------------------------------------------------


@attr.s(frozen=True, auto_attribs=True)
class SourceSpec:
    """
    Contractual properties of one writing source (device).

    :param name: source identifier
    :param json_type: JSON type the source emits
    :param min_length: minimum body length in bytes
    :param max_length: maximum body length in bytes
    """

    name: str
    json_type: JsonType
    min_length: int
    max_length: int

    def accepts(self, candidate: "BodyCandidate") -> bool:
        length = candidate.end - candidate.start
        return (
            candidate.json_type == self.json_type
            and self.min_length <= length <= self.max_length
        )


@attr.s(frozen=True, auto_attribs=True)
class RecordReceipt:
    """
    One record for which a source's Writer returned success.

    A successful Writer return means both the body write and the line feed
    write landed. Remembered positions are optional; all coordinates are
    absolute byte offsets in the original file. Per-source ordering is given
    by record order inside a :class:`Receipt`.

    :param source: source identifier
    :param body_start: remembered body write start, if any
    :param body_length: remembered body write byte length, if any
    :param line_feed_start: remembered line feed position, if any
    """

    source: str
    body_start: Optional[int] = None
    body_length: Optional[int] = None
    line_feed_start: Optional[int] = None

    def describe(self, sequence_index: int) -> str:
        return (
            f"record {self.source}#{sequence_index}"
            f"(body_start={self.body_start}, body_length={self.body_length},"
            f" line_feed_start={self.line_feed_start})"
        )


@attr.s(frozen=True, auto_attribs=True)
class OrphanBodyReceipt:
    """
    A body write whose Writer never returned success.

    The bytes may still be present, because the append service only ever
    leaves complete writes behind, but no successful record and therefore no
    matching line feed can be claimed for them.
    """

    source: str
    start: Optional[int] = None
    length: Optional[int] = None

    def describe(self, index: int) -> str:
        return (
            f"orphan body {self.source}#{index}"
            f"(start={self.start}, length={self.length})"
        )


@attr.s(frozen=True, auto_attribs=True)
class Receipt:
    """
    All facts available about the analysed byte range.
    """

    sources: Mapping[str, SourceSpec]
    records: Sequence[RecordReceipt] = ()
    orphan_bodies: Sequence[OrphanBodyReceipt] = ()

    def records_for(self, source: str) -> Sequence[Tuple[int, RecordReceipt]]:
        return [
            (index, record)
            for index, record in enumerate(self.records)
            if record.source == source
        ]


def receipt_from_dict(data: Mapping[str, Any]) -> Receipt:
    """
    Build a :class:`Receipt` from a plain mapping (e.g. parsed JSON).

    Expected shape::

        {
          "sources": {
            "A": {"json_type": "integer", "min_length": 1, "max_length": 3}
          },
          "records": [
            {"source": "A", "body_start": 0, "body_length": 1,
             "line_feed_start": 1}
          ],
          "orphan_bodies": [
            {"source": "B", "start": 9, "length": 2}
          ]
        }

    Re-running a recovery with a rebuilt receipt that contains additional
    evidence keeps every byte coordinate stable.
    """
    sources: Dict[str, SourceSpec] = {}
    for name, spec in data.get("sources", {}).items():
        sources[name] = SourceSpec(
            name=name,
            json_type=_coerce_json_type(spec["json_type"]),
            min_length=int(spec["min_length"]),
            max_length=int(spec["max_length"]),
        )
    records = tuple(
        RecordReceipt(
            source=item["source"],
            body_start=item.get("body_start"),
            body_length=item.get("body_length"),
            line_feed_start=item.get("line_feed_start"),
        )
        for item in data.get("records", [])
    )
    orphan_bodies = tuple(
        OrphanBodyReceipt(
            source=item["source"],
            start=item.get("start"),
            length=item.get("length"),
        )
        for item in data.get("orphan_bodies", [])
    )
    return Receipt(sources=sources, records=records, orphan_bodies=orphan_bodies)


# ---------------------------------------------------------------------------
# Recovery result types
# ---------------------------------------------------------------------------


@attr.s(frozen=True, auto_attribs=True)
class ByteRange:
    """
    Half-open byte range ``[start, end)`` in absolute file coordinates.
    """

    start: int
    end: int

    def as_dict(self) -> Dict[str, int]:
        return {"start": self.start, "end": self.end}


@attr.s(frozen=True, auto_attribs=True)
class BodyCandidate:
    """
    A byte span that is a valid, old-Writer-shaped JSON serialization.
    """

    start: int  # relative to the analysed buffer
    end: int
    value: Any
    json_type: JsonType


@attr.s(frozen=True, auto_attribs=True)
class ConfirmedRecord:
    """
    A record whose source, body and line feed ranges are identical in every
    feasible interpretation of the receipt.
    """

    source: str
    sequence_index: int
    body: ByteRange
    line_feed: ByteRange
    value: Any

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "sequence_index": self.sequence_index,
            "body": self.body.as_dict(),
            "line_feed": self.line_feed.as_dict(),
            "value": self.value,
        }


@attr.s(frozen=True, auto_attribs=True)
class Alternative:
    """
    One possible placement of a remembered write inside an uncertain region.
    """

    kind: str  # "record" or "orphan_body"
    source: str
    sequence_index: int
    body: ByteRange
    line_feed: Optional[ByteRange]
    value: Any

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "sequence_index": self.sequence_index,
            "body": self.body.as_dict(),
            "line_feed": None if self.line_feed is None else self.line_feed.as_dict(),
            "value": self.value,
        }


@attr.s(frozen=True, auto_attribs=True)
class UncertainRegion:
    """
    A byte range that cannot be mapped to confirmed writes unambiguously.
    """

    byte_range: ByteRange
    reason: str
    alternatives: Tuple[Alternative, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "byte_range": self.byte_range.as_dict(),
            "reason": self.reason,
            "alternatives": [item.as_dict() for item in self.alternatives],
        }


@attr.s(frozen=True, auto_attribs=True)
class IncompatibleFact:
    """
    A receipt fact that participates in an unsatisfiable constraint set.
    """

    fact: str
    reason: str

    def as_dict(self) -> Dict[str, str]:
        return {"fact": self.fact, "reason": self.reason}


@attr.s(frozen=True, auto_attribs=True)
class RecoveryResult:
    """
    Outcome of a recovery run.
    """

    analyzed_range: ByteRange
    confirmed: Tuple[ConfirmedRecord, ...]
    uncertain: Tuple[UncertainRegion, ...]
    incompatible: Tuple[IncompatibleFact, ...]
    solution_count: int
    truncated: bool = False

    @property
    def satisfiable(self) -> bool:
        return not self.incompatible

    def as_dict(self) -> Dict[str, Any]:
        return {
            "analyzed_range": self.analyzed_range.as_dict(),
            "satisfiable": self.satisfiable,
            "solution_count": self.solution_count,
            "truncated": self.truncated,
            "confirmed": [item.as_dict() for item in self.confirmed],
            "uncertain": [item.as_dict() for item in self.uncertain],
            "incompatible": [item.as_dict() for item in self.incompatible],
        }


# ---------------------------------------------------------------------------
# Candidate enumeration
# ---------------------------------------------------------------------------


def _decode_with_map(data: bytes) -> Tuple[str, List[int]]:
    """
    Decode UTF-8 while building a character-index to byte-offset map.

    Each character is decoded using the shortest valid UTF-8 sequence at the
    current offset. Invalid bytes become one-byte replacement characters, so
    the offset map stays accurate; tokens containing them can never match the
    canonical serialization and are rejected later.
    """
    parts: List[str] = []
    char_to_byte = [0]
    offset = 0
    while offset < len(data):
        char = "�"
        width = 1
        for candidate_width in (1, 2, 3, 4):
            try:
                decoded = data[offset : offset + candidate_width].decode("utf-8")
            except UnicodeDecodeError:
                continue
            if len(decoded) == 1:
                char = decoded
                width = candidate_width
                break
        parts.append(char)
        offset += width
        char_to_byte.append(offset)
    return "".join(parts), char_to_byte


_NUMBER_CHARS = frozenset("-+0123456789.eE")


def _matching_bracket(
    text: str, start: int, open_char: str, close_char: str
) -> Optional[int]:
    """
    Return the exclusive char index after the bracket matching ``text[start]``.

    String contents (including escape sequences) are skipped. ``None`` is
    returned when no matching bracket exists.
    """
    depth = 0
    index = start
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def _lexical_token_end(text: str, start: int) -> Optional[int]:
    """
    Find the exclusive char index of the maximal JSON token at ``start``.

    This is purely lexical: the returned span is still validated as JSON
    later. Returning a bounded span (rather than relying on the decoder)
    keeps adjacent values such as ``123[1]`` enumerable independently.
    """
    char = text[start]
    if char in "-0123456789":
        end = start + 1
        while end < len(text) and text[end] in _NUMBER_CHARS:
            end += 1
        return end
    if char == '"':
        end = start + 1
        while end < len(text):
            if text[end] == "\\":
                end += 2
                continue
            if text[end] == '"':
                return end + 1
            end += 1
        return None
    if char == "{":
        return _matching_bracket(text, start, "{", "}")
    if char == "[":
        return _matching_bracket(text, start, "[", "]")
    for literal in ("true", "false", "null"):
        if text.startswith(literal, start):
            return start + len(literal)
    return None


def _body_candidates(
    data: bytes, max_body_length: Optional[int] = None
) -> Tuple[BodyCandidate, ...]:
    text, char_to_byte = _decode_with_map(data)
    candidates: List[BodyCandidate] = []
    value_starts = frozenset('{["tfn-0123456789')
    for char_index in range(len(text)):
        if text[char_index] not in value_starts:
            continue
        maximal_end = _lexical_token_end(text, char_index)
        if maximal_end is None:
            continue
        start = char_to_byte[char_index]
        # Every prefix of the maximal token that is itself a canonical JSON
        # value is a possible independent body write, notably number prefixes
        # ("1" and "12" inside "123") and strings ending at an escaped quote.
        for prefix_end in range(char_index + 1, maximal_end + 1):
            end = char_to_byte[prefix_end]
            if max_body_length is not None and end - start > max_body_length:
                break
            span = data[start:end]
            if b"\n" in span:
                # The old Writer never emits an unescaped LF inside a body.
                continue
            try:
                value = json.loads(text[char_index:prefix_end])
            except ValueError:
                continue
            if _DEFAULT_ENCODE(value).encode("utf-8") != span:
                # Non-canonical bytes (e.g. "01", "1.00", "1e10") cannot come
                # from the default Writer serialization.
                continue
            candidates.append(
                BodyCandidate(
                    start=start,
                    end=end,
                    value=value,
                    json_type=_json_type_of(value),
                )
            )
    # Deduplicate identical spans (e.g. reached via overlapping token paths).
    unique = {(candidate.start, candidate.end): candidate for candidate in candidates}
    return tuple(unique[key] for key in sorted(unique))


# ---------------------------------------------------------------------------
# Independent fact validation
# ---------------------------------------------------------------------------


def _abs_to_rel(position: int, base_offset: int) -> int:
    return position - base_offset


def _validate_receipt(
    data: bytes,
    receipt: Receipt,
    base_offset: int,
    candidates: Sequence[BodyCandidate],
    line_feed_positions: Sequence[int],
) -> List[IncompatibleFact]:
    problems: List[IncompatibleFact] = []
    n = len(data)
    candidates_by_start: Dict[int, List[BodyCandidate]] = {}
    for candidate in candidates:
        candidates_by_start.setdefault(candidate.start, []).append(candidate)
    line_feed_set = set(line_feed_positions)

    def in_bounds(relative: int) -> bool:
        return 0 <= relative < n

    for record_index, record in enumerate(receipt.records):
        description = record.describe(record_index)
        spec = receipt.sources.get(record.source)
        if spec is None:
            problems.append(IncompatibleFact(description, "unknown source identifier"))
            continue

        body_start = (
            None
            if record.body_start is None
            else _abs_to_rel(record.body_start, base_offset)
        )
        if body_start is not None:
            if not in_bounds(body_start):
                problems.append(
                    IncompatibleFact(
                        description, "body start is outside the analysed byte range"
                    )
                )
                continue
            if record.body_length is not None:
                if record.body_length <= 0:
                    problems.append(
                        IncompatibleFact(description, "body length must be positive")
                    )
                elif not in_bounds(body_start + record.body_length - 1):
                    problems.append(
                        IncompatibleFact(
                            description,
                            "body write extends past the analysed range",
                        )
                    )
            at_start = candidates_by_start.get(body_start, [])
            if not at_start:
                problems.append(
                    IncompatibleFact(
                        description,
                        "no canonical JSON body starts at the remembered position",
                    )
                )
                continue
            matching = at_start
            if record.body_length is not None:
                matching = [
                    candidate
                    for candidate in matching
                    if candidate.end - candidate.start == record.body_length
                ]
                if not matching:
                    problems.append(
                        IncompatibleFact(
                            description,
                            "remembered body length does not match any JSON token"
                            " at that position",
                        )
                    )
                    continue
            matching = [
                candidate for candidate in matching if spec.accepts(candidate)
            ]
            if not matching:
                sample = at_start[0]
                problems.append(
                    IncompatibleFact(
                        description,
                        "body token type or length violates the source contract"
                        f" ({sample.json_type.value},"
                        f" {sample.end - sample.start} byte(s))",
                    )
                )
        elif record.body_length is not None and not (
            spec.min_length <= record.body_length <= spec.max_length
        ):
            problems.append(
                IncompatibleFact(
                    description, "remembered body length violates the source contract"
                )
            )

        if record.line_feed_start is not None:
            lf = _abs_to_rel(record.line_feed_start, base_offset)
            if lf not in line_feed_set:
                problems.append(
                    IncompatibleFact(
                        description,
                        "remembered line feed position does not contain a LF byte",
                    )
                )

    for index, orphan in enumerate(receipt.orphan_bodies):
        description = orphan.describe(index)
        spec = receipt.sources.get(orphan.source)
        if spec is None:
            problems.append(IncompatibleFact(description, "unknown source identifier"))
            continue
        if orphan.start is None:
            if orphan.length is not None and not (
                spec.min_length <= orphan.length <= spec.max_length
            ):
                problems.append(
                    IncompatibleFact(
                        description,
                        "remembered body length violates the source contract",
                    )
                )
            continue
        start = _abs_to_rel(orphan.start, base_offset)
        if not in_bounds(start):
            problems.append(
                IncompatibleFact(
                    description, "body start is outside the analysed byte range"
                )
            )
            continue
        if orphan.length is not None and not in_bounds(start + orphan.length - 1):
            problems.append(
                IncompatibleFact(
                    description, "body write extends past the analysed range"
                )
            )
        at_start = candidates_by_start.get(start, [])
        if not at_start:
            problems.append(
                IncompatibleFact(
                    description,
                    "no canonical JSON body starts at the remembered position",
                )
            )
            continue
        matching = at_start
        if orphan.length is not None:
            matching = [
                candidate
                for candidate in matching
                if candidate.end - candidate.start == orphan.length
            ]
        matching = [candidate for candidate in matching if spec.accepts(candidate)]
        if not matching:
            problems.append(
                IncompatibleFact(
                    description,
                    "body token type or length violates the source contract",
                )
            )

    # Distinct writes cannot share pinned positions.
    seen_body_starts: Dict[int, str] = {}
    for record_index, record in enumerate(receipt.records):
        if record.body_start is None:
            continue
        description = record.describe(record_index)
        if record.body_start in seen_body_starts:
            problems.append(
                IncompatibleFact(
                    description,
                    "duplicate body start; already claimed by"
                    f" {seen_body_starts[record.body_start]}",
                )
            )
        else:
            seen_body_starts[record.body_start] = description
    seen_lf: Dict[int, str] = {}
    for record_index, record in enumerate(receipt.records):
        if record.line_feed_start is None:
            continue
        description = record.describe(record_index)
        if record.line_feed_start in seen_lf:
            problems.append(
                IncompatibleFact(
                    description,
                    "duplicate line feed position; already claimed by"
                    f" {seen_lf[record.line_feed_start]}",
                )
            )
        else:
            seen_lf[record.line_feed_start] = description

    # Per-source Writer call order: body_i, LF_i, body_i+1, LF_i+1, ...
    for source in receipt.sources:
        entries = receipt.records_for(source)
        for (index_a, record_a), (index_b, record_b) in zip(entries, entries[1:]):
            if (
                record_a.line_feed_start is not None
                and record_b.body_start is not None
                and record_a.line_feed_start >= record_b.body_start
            ):
                problems.append(
                    IncompatibleFact(
                        record_b.describe(index_b),
                        "source ordering violated: previous line feed at"
                        f" {record_a.line_feed_start} must precede this body at"
                        f" {record_b.body_start}",
                    )
                )
            if (
                record_a.body_start is not None
                and record_b.body_start is not None
                and record_a.body_start >= record_b.body_start
            ):
                problems.append(
                    IncompatibleFact(
                        record_b.describe(index_b),
                        "source ordering violated: body positions are not in"
                        " success order",
                    )
                )

    return problems


# ---------------------------------------------------------------------------
# Constraint solving
# ---------------------------------------------------------------------------


@attr.s(frozen=True, auto_attribs=True)
class _RecordOption:
    candidate: BodyCandidate
    line_feed: int


@attr.s(frozen=True, auto_attribs=True)
class _Variable:
    kind: str  # "record" or "orphan"
    index: int
    source: str
    options: Tuple[Any, ...]  # _RecordOption or BodyCandidate


def _build_variables(
    receipt: Receipt,
    base_offset: int,
    candidates: Sequence[BodyCandidate],
    line_feed_positions: Sequence[int],
) -> List[_Variable]:
    variables: List[_Variable] = []

    for record_index, record in enumerate(receipt.records):
        spec = receipt.sources[record.source]
        body_start = (
            None
            if record.body_start is None
            else _abs_to_rel(record.body_start, base_offset)
        )
        lf_start = (
            None
            if record.line_feed_start is None
            else _abs_to_rel(record.line_feed_start, base_offset)
        )
        options: List[_RecordOption] = []
        for candidate in candidates:
            if not spec.accepts(candidate):
                continue
            if body_start is not None and candidate.start != body_start:
                continue
            if record.body_length is not None and (
                candidate.end - candidate.start != record.body_length
            ):
                continue
            for line_feed in line_feed_positions:
                if lf_start is not None and line_feed != lf_start:
                    continue
                if line_feed < candidate.end:
                    # The line feed write happens after the body write of
                    # the same Writer call.
                    continue
                options.append(_RecordOption(candidate, line_feed))
        variables.append(
            _Variable(
                kind="record",
                index=record_index,
                source=record.source,
                options=tuple(options),
            )
        )

    for orphan_index, orphan in enumerate(receipt.orphan_bodies):
        spec = receipt.sources[orphan.source]
        start = None if orphan.start is None else _abs_to_rel(orphan.start, base_offset)
        options_orphan = tuple(
            candidate
            for candidate in candidates
            if spec.accepts(candidate)
            and (start is None or candidate.start == start)
            and (
                orphan.length is None
                or candidate.end - candidate.start == orphan.length
            )
        )
        variables.append(
            _Variable(
                kind="orphan",
                index=orphan_index,
                source=orphan.source,
                options=options_orphan,
            )
        )

    return variables


def _variable_key(variable: _Variable) -> str:
    return f"{variable.kind}:{variable.index}"


def _option_span(option: Any) -> Tuple[int, int]:
    if isinstance(option, _RecordOption):
        return option.candidate.start, option.candidate.end
    assert isinstance(option, BodyCandidate)
    return option.start, option.end


def _option_line_feed(option: Any) -> Optional[int]:
    if isinstance(option, _RecordOption):
        return option.line_feed
    return None


def _solve(
    receipt: Receipt,
    variables: Sequence[_Variable],
    solution_limit: int,
    node_budget: int,
    trial_budget: int,
) -> Tuple[Tuple[Dict[str, Any], ...], str]:
    """
    Bounded depth-first constraint search.

    Variables are tried most-constrained first (static ordering). Conflict
    checks are incremental against the current partial assignment only. Two
    budgets bound the total work: ``node_budget`` caps search nodes and
    ``trial_budget`` caps individual option trials, so a huge option list can
    never make a single node unbounded. Returns the collected solutions and
    one of ``"satisfied"``, ``"unsatisfiable"`` or ``"budget_exhausted"``.
    """
    if any(not variable.options for variable in variables):
        return (), "unsatisfiable"

    ordered = sorted(variables, key=lambda variable: len(variable.options))
    source_sequence = {
        source: tuple(index for index, _ in receipt.records_for(source))
        for source in receipt.sources
    }
    # Precompute, per record variable, the record keys that precede/follow
    # it within its own source, so ordering checks stay O(neighbours).
    predecessors: Dict[str, Tuple[str, ...]] = {}
    successors: Dict[str, Tuple[str, ...]] = {}
    for source, sequence in source_sequence.items():
        for position, record_index in enumerate(sequence):
            key = f"record:{record_index}"
            predecessors[key] = tuple(
                f"record:{sequence[pos]}" for pos in range(position)
            )
            successors[key] = tuple(
                f"record:{sequence[pos]}"
                for pos in range(position + 1, len(sequence))
            )

    assignments: Dict[str, Any] = {}
    occupied_bodies: List[Tuple[int, int]] = []
    used_line_feeds: Set[int] = set()
    solutions: List[Dict[str, Any]] = []
    counters = {"nodes": 0, "trials": 0}

    def conflicts(variable: _Variable, option: Any) -> bool:
        start, end = _option_span(option)
        for other_start, other_end in occupied_bodies:
            if start < other_end and other_start < end:
                return True
        line_feed = _option_line_feed(option)
        if line_feed is None:
            return False
        if line_feed in used_line_feeds:
            return True
        key = _variable_key(variable)
        for prior_key in predecessors[key]:
            prior = assignments.get(prior_key)
            if prior is not None and prior.line_feed >= start:
                return True
        for later_key in successors[key]:
            later = assignments.get(later_key)
            if later is not None and line_feed >= later.candidate.start:
                return True
        return False

    def search(depth: int) -> str:
        if len(solutions) >= solution_limit:
            return "satisfied"
        if depth == len(ordered):
            solutions.append(dict(assignments))
            return "satisfied"
        counters["nodes"] += 1
        if counters["nodes"] > node_budget:
            return "budget_exhausted"
        variable = ordered[depth]
        for option in variable.options:
            counters["trials"] += 1
            if counters["trials"] > trial_budget:
                return "budget_exhausted"
            if conflicts(variable, option):
                continue
            start, end = _option_span(option)
            occupied_bodies.append((start, end))
            line_feed = _option_line_feed(option)
            if line_feed is not None:
                used_line_feeds.add(line_feed)
            assignments[_variable_key(variable)] = option
            status = search(depth + 1)
            del assignments[_variable_key(variable)]
            occupied_bodies.pop()
            if line_feed is not None:
                used_line_feeds.discard(line_feed)
            if status == "budget_exhausted":
                return status
            if len(solutions) >= solution_limit:
                return "satisfied"
        return "satisfied" if solutions else "unsatisfiable"

    status = search(0)
    return tuple(solutions), status


def _describe_variable(receipt: Receipt, variable: _Variable) -> str:
    if variable.kind == "record":
        return receipt.records[variable.index].describe(variable.index)
    return receipt.orphan_bodies[variable.index].describe(variable.index)


def _diagnose_unsatisfiable(
    receipt: Receipt,
    variables: Sequence[_Variable],
    solution_limit: int,
    node_budget: int,
    trial_budget: int,
) -> List[IncompatibleFact]:
    """
    Name the facts that participate in the unsatisfiable constraint set.

    A remembered write with no legal placement at all is reported directly.
    Otherwise each remembered write is relaxed independently; a write whose
    removal provably restores feasibility is reported as incompatible. A
    budget stop without a solution proves nothing and is not reported.
    """
    incompatible: List[IncompatibleFact] = []
    constrained = [variable for variable in variables if variable.options]
    for variable in variables:
        if variable.options:
            continue
        incompatible.append(
            IncompatibleFact(
                _describe_variable(receipt, variable),
                "no canonical JSON body (and, for a record, line feed)"
                " satisfies its remembered positions, source contract and"
                " per-source ordering",
            )
        )

    for variable in constrained:
        remaining = [other for other in constrained if other is not variable]
        solutions, status = _solve(
            receipt, remaining, solution_limit, node_budget, trial_budget
        )
        if status != "satisfied":
            continue
        reason = (
            "cannot be placed jointly with the other receipt facts; no"
            " disjoint canonical body and distinct line feed satisfy its"
            " contract and ordering"
            if variable.kind == "record"
            else "cannot be placed jointly with the other receipt facts"
        )
        incompatible.append(
            IncompatibleFact(_describe_variable(receipt, variable), reason)
        )

    if not incompatible:
        incompatible.append(
            IncompatibleFact(
                "receipt",
                "no feasible ordering satisfies the source contracts, the"
                " available canonical JSON bodies and the line feeds",
            )
        )
    return incompatible


# ---------------------------------------------------------------------------
# Result derivation
# ---------------------------------------------------------------------------


def _record_label(index: int) -> str:
    return f"record:{index}"


def _orphan_label(index: int) -> str:
    return f"orphan:{index}"


# A placement key: (body start, body end, line feed or None); the decoded
# value is stored alongside it because dict/list values are unhashable.
_PlacementKey = Tuple[int, int, Optional[int]]


def _derive_result(
    data: bytes,
    receipt: Receipt,
    base_offset: int,
    solutions: Sequence[Mapping[str, Any]],
    truncated: bool,
) -> RecoveryResult:
    n = len(data)
    labels_per_solution: List[List[Optional[str]]] = []
    placements: Dict[str, Dict[_PlacementKey, Any]] = {}

    for solution in solutions:
        labels: List[Optional[str]] = [None] * n
        for variable_key, option in solution.items():
            if variable_key.startswith("record:"):
                index = int(variable_key.split(":", 1)[1])
                candidate = option.candidate
                for offset in range(candidate.start, candidate.end):
                    labels[offset] = _record_label(index)
                labels[option.line_feed] = _record_label(index)
                placements.setdefault(variable_key, {})[
                    (candidate.start, candidate.end, option.line_feed)
                ] = candidate.value
            else:
                index = int(variable_key.split(":", 1)[1])
                for offset in range(option.start, option.end):
                    labels[offset] = _orphan_label(index)
                placements.setdefault(variable_key, {})[
                    (option.start, option.end, None)
                ] = option.value
        labels_per_solution.append(labels)

    def stable_label(offset: int) -> Optional[str]:
        label = labels_per_solution[0][offset]
        for labels in labels_per_solution[1:]:
            if labels[offset] != label:
                return None
        return label

    confirmed: List[ConfirmedRecord] = []
    confirmed_bytes = [False] * n
    for index, record in enumerate(receipt.records):
        if truncated:
            # Enumeration stopped early; uniqueness cannot be proven.
            break
        options_seen = placements.get(_record_label(index), {})
        if len(options_seen) != 1:
            continue
        (start, end, line_feed), value = next(iter(options_seen.items()))
        assert line_feed is not None
        positions = list(range(start, end)) + [line_feed]
        if any(stable_label(offset) != _record_label(index) for offset in positions):
            continue
        confirmed.append(
            ConfirmedRecord(
                source=record.source,
                sequence_index=index,
                body=ByteRange(base_offset + start, base_offset + end),
                line_feed=ByteRange(
                    base_offset + line_feed, base_offset + line_feed + 1
                ),
                value=value,
            )
        )
        for offset in positions:
            confirmed_bytes[offset] = True

    uncertain: List[UncertainRegion] = []
    offset = 0
    while offset < n:
        if confirmed_bytes[offset]:
            offset += 1
            continue
        start = offset
        while offset < n and not confirmed_bytes[offset]:
            offset += 1
        uncertain.append(
            _build_region(
                data,
                receipt,
                base_offset,
                placements,
                labels_per_solution,
                stable_label,
                start,
                offset,
                truncated,
            )
        )

    return RecoveryResult(
        analyzed_range=ByteRange(base_offset, base_offset + n),
        confirmed=tuple(confirmed),
        uncertain=tuple(uncertain),
        incompatible=(),
        solution_count=len(solutions),
        truncated=truncated,
    )


def _build_region(
    data: bytes,
    receipt: Receipt,
    base_offset: int,
    placements: Mapping[str, Mapping[_PlacementKey, Any]],
    labels_per_solution: Sequence[Sequence[Optional[str]]],
    stable_label: Callable[[int], Optional[str]],
    start: int,
    end: int,
    truncated: bool,
) -> UncertainRegion:
    region_labels = {stable_label(position) for position in range(start, end)}
    alternatives = _region_alternatives(
        receipt, base_offset, placements, start, end
    )

    always_uncovered = all(
        all(label is None for label in labels[start:end])
        for labels in labels_per_solution
    )
    if always_uncovered:
        if all(data[position] == 0x0A for position in range(start, end)):
            reason = "line feed byte not paired with a successful record"
        else:
            reason = "bytes not covered by any remembered write"
    elif region_labels and all(
        label is not None and label.startswith("orphan:")
        for label in region_labels
    ):
        reason = "body write without a successful Writer return"
    else:
        reason = "multiple interpretations satisfy the receipt"
    if truncated:
        reason += "; interpretation enumeration was truncated"
    return UncertainRegion(
        byte_range=ByteRange(base_offset + start, base_offset + end),
        reason=reason,
        alternatives=tuple(alternatives),
    )


def _region_alternatives(
    receipt: Receipt,
    base_offset: int,
    placements: Mapping[str, Mapping[_PlacementKey, Any]],
    region_start: int,
    region_end: int,
) -> List[Alternative]:
    alternatives: List[Alternative] = []
    for variable_key, options_seen in placements.items():
        kind, index_text = variable_key.split(":", 1)
        index = int(index_text)
        for (start, end, line_feed), value in sorted(
            options_seen.items(),
            key=lambda item: (item[0][0], item[0][1], item[0][2] is None),
        ):
            touches_region = start < region_end and region_start < end
            if line_feed is not None and region_start <= line_feed < region_end:
                touches_region = True
            if not touches_region:
                continue
            if kind == "record":
                record = receipt.records[index]
                line_feed_range = (
                    None
                    if line_feed is None
                    else ByteRange(
                        base_offset + line_feed, base_offset + line_feed + 1
                    )
                )
                alternatives.append(
                    Alternative(
                        kind="record",
                        source=record.source,
                        sequence_index=index,
                        body=ByteRange(base_offset + start, base_offset + end),
                        line_feed=line_feed_range,
                        value=value,
                    )
                )
            else:
                orphan = receipt.orphan_bodies[index]
                alternatives.append(
                    Alternative(
                        kind="orphan_body",
                        source=orphan.source,
                        sequence_index=index,
                        body=ByteRange(base_offset + start, base_offset + end),
                        line_feed=None,
                        value=value,
                    )
                )
    alternatives.sort(
        key=lambda item: (item.kind, item.sequence_index, item.body.start)
    )
    return alternatives


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def recover(
    data: bytes,
    receipt: Receipt,
    *,
    base_offset: int = 0,
    solution_limit: int = 10000,
    node_budget: int = 200000,
    trial_budget: int = 1000000,
) -> RecoveryResult:
    """
    Recover audit records from an affected byte range.

    :param data: raw bytes of the affected range; never modified
    :param receipt: remembered write facts, using absolute file coordinates
    :param base_offset: absolute file offset of ``data[0]``
    :param solution_limit: maximum number of feasible interpretations to
        enumerate; if the limit is reached the result is flagged as
        truncated and no record is claimed as confirmed
    :param node_budget: maximum number of search nodes spent testing
        interpretations
    :param trial_budget: maximum number of individual option trials; together
        with ``node_budget`` this gives the search a hard runtime bound
    :returns: :class:`RecoveryResult` with confirmed records, uncertain
        regions (including the exact alternatives that remain) and any
        incompatible receipt facts
    """
    analyzed_range = ByteRange(base_offset, base_offset + len(data))
    max_body_length = (
        max(spec.max_length for spec in receipt.sources.values())
        if receipt.sources
        else None
    )
    candidates = _body_candidates(data, max_body_length)
    line_feed_positions = [index for index, byte in enumerate(data) if byte == 0x0A]

    problems = _validate_receipt(
        data, receipt, base_offset, candidates, line_feed_positions
    )
    if problems:
        return RecoveryResult(
            analyzed_range=analyzed_range,
            confirmed=(),
            uncertain=(),
            incompatible=tuple(problems),
            solution_count=0,
        )

    variables = _build_variables(
        receipt, base_offset, candidates, line_feed_positions
    )
    solutions, status = _solve(
        receipt, variables, solution_limit, node_budget, trial_budget
    )
    if status == "unsatisfiable":
        return RecoveryResult(
            analyzed_range=analyzed_range,
            confirmed=(),
            uncertain=(),
            incompatible=tuple(
                _diagnose_unsatisfiable(
                    receipt,
                    variables,
                    solution_limit,
                    node_budget,
                    trial_budget,
                )
            ),
            solution_count=0,
        )
    if not solutions:
        # Budget exhausted before feasibility or infeasibility was proven.
        return RecoveryResult(
            analyzed_range=analyzed_range,
            confirmed=(),
            uncertain=(
                UncertainRegion(
                    byte_range=analyzed_range,
                    reason="interpretation enumeration exhausted its search"
                    " budget; neither feasibility nor infeasibility is proven",
                ),
            ),
            incompatible=(),
            solution_count=0,
            truncated=True,
        )

    truncated = status != "satisfied" or len(solutions) >= solution_limit
    return _derive_result(data, receipt, base_offset, solutions, truncated)


def recover_file(
    path: Any,
    receipt: Receipt,
    *,
    base_offset: int = 0,
    solution_limit: int = 10000,
    node_budget: int = 200000,
    trial_budget: int = 1000000,
) -> RecoveryResult:
    """
    Run :func:`recover` against a file opened in read-only binary mode.

    The file is never opened for writing, so the original bytes remain
    untouched.
    """
    with open(path, "rb") as fp:
        data = fp.read()
    return recover(
        data,
        receipt,
        base_offset=base_offset,
        solution_limit=solution_limit,
        node_budget=node_budget,
        trial_budget=trial_budget,
    )
