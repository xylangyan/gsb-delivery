"""Opt-in provenance tracking for schema-guided repairs.

When :func:`json_repair.loads_with_provenance` (or one of its siblings) is used,
every value of the repaired output keeps a :class:`Provenance` record describing
where it came from:

* the original key (before alias/default renames), the parsed value and the
  character interval in the source text, for values read from the receipt;
* the effective schema branch that supplied a default, const or enum value;
* the rejected salvage fragments, with the schema error that discarded them.

The normal reading path never builds these records, so regular
``repair_json``/``loads``/``load`` calls keep their existing behavior and cost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .utils.constants import JSONReturnType

ProvenanceOrigin = Literal["source", "default", "const", "enum", "inferred", "repaired_string"]
PROVENANCE_ORIGINS: tuple[ProvenanceOrigin, ...] = (
    "source",
    "default",
    "const",
    "enum",
    "inferred",
    "repaired_string",
)


@dataclass(frozen=True)
class SourceSpan:
    """Character interval of a parsed value inside the source text.

    Attributes:
        start: Index of the first character consumed for the value.
        end: Index just past the last consumed character (half-open interval).
        fragment_index: Zero-based index of the top-level fragment when salvage
            tried several sequential fragments, otherwise always zero.
    """

    start: int
    end: int
    fragment_index: int = 0

    def as_slice(self) -> slice:
        return slice(self.start, self.end)


@dataclass(frozen=True)
class SchemaRef:
    """Pointer to the schema branch that was effective for a value.

    Schema objects are untrusted and may contain ``$ref`` aliases, so the branch
    is identified by the JSON path at which the library resolved it together
    with its object identity. Local references are always interpreted against
    the schema branch that actually produced the value.

    Attributes:
        path: JSON path of the resolved branch inside the root schema.
        schema_id: Python object id of the resolved schema dict.
    """

    path: str
    schema_id: int


@dataclass(frozen=True)
class Provenance:
    """Evidence record attached to a single output value.

    Attributes:
        path: JSON path of the value in the repaired output (e.g. ``$.items[2].id``).
        origin: How the value was produced. ``source`` means taken from the
            source text, possibly after a safe type coercion; ``default``,
            ``const`` and ``enum`` mean the schema supplied the value;
            ``inferred`` means a missing-value heuristic filled it;
            ``repaired_string`` means a malformed JSON string was unwrapped.
        original_key: Key under which the value appeared in the source object,
            or the output key when the value was not read from the source.
        original_value: Value as parsed before schema coercion/defaulting.
        span: Character interval of the source value, or ``None`` when the
            value does not correspond to a source interval.
        type_changed: Whether the stored value's type differs from the parsed
            value because of a schema coercion.
        schema_ref: Effective schema branch for the value, when a schema guided
            the repair.
        actions: Human-readable repair actions applied to the value, in order.
        original_index: Position of the value inside its source array before
            salvage drops shifted the output indices, or ``None`` otherwise.
    """

    path: str
    origin: ProvenanceOrigin
    original_key: str
    original_value: Any
    span: SourceSpan | None = None
    type_changed: bool = False
    schema_ref: SchemaRef | None = None
    actions: tuple[str, ...] = field(default_factory=tuple)
    original_index: int | None = None

    @property
    def from_source(self) -> bool:
        """Whether the value was read from the source text."""
        return self.span is not None

    @property
    def coerced(self) -> bool:
        """Whether a schema coercion changed the parsed value's type."""
        return self.type_changed


@dataclass(frozen=True)
class RejectedFragment:
    """A salvage candidate that was parsed but failed schema validation.

    Attributes:
        fragment_index: Zero-based position of the fragment among the
            sequential top-level fragments of the source text.
        span: Character interval of the whole fragment.
        value: Value parsed from the fragment before schema repair.
        reason: Schema error message that caused the fragment to be skipped.
    """

    fragment_index: int
    span: SourceSpan
    value: JSONReturnType
    reason: str


@dataclass(frozen=True)
class DroppedItem:
    """An array item that was parsed from the source but removed from the output.

    Attributes:
        path: JSON path of the array in the output.
        original_index: Position of the item in the source array.
        reason: Repair action that removed it.
        span: Character interval of the item in the source text, when known.
    """

    path: str
    original_index: int
    reason: str
    span: SourceSpan | None = None


@dataclass
class ProvenanceRecord:
    """Full evidence for one repaired document.

    Returned by the provenance entry points alongside the repaired value. It is
    a live view: :meth:`provenance_for` resolves output keys against the records
    collected during parsing, so duplicate source keys that collapse into one
    output key always point at the occurrence that actually supplied the value.

    Attributes:
        entries: Provenance records keyed by output JSON path.
        rejected_fragments: Salvage candidates that were not selected.
        source_text: Full source text, also used to materialize source spans.
        fragment_spans: Character interval of each parsed top-level fragment.
        dropped_items: Source array items removed by salvage, with their
            original positions.
    """

    entries: dict[str, Provenance]
    rejected_fragments: list[RejectedFragment]
    source_text: str
    fragment_spans: list[SourceSpan] = field(default_factory=list)
    dropped_items: list[DroppedItem] = field(default_factory=list)

    def provenance_for(self, key: str) -> Provenance:
        """Return the provenance of an output key.

        Args:
            key: Output JSON path. A bare object key is resolved against the
                root object (``"id"`` is equivalent to ``"$.id"``); array items
                use the usual ``$.items[0]`` notation.

        Returns:
            The provenance record of the value stored at that key.

        Raises:
            KeyError: When the key is absent from the repaired output.
        """
        path = self._normalize_key(key)
        try:
            return self.entries[path]
        except KeyError as exc:
            raise KeyError(f"No provenance for output key {key!r}.") from exc

    def source_span_for(self, key: str) -> SourceSpan | None:
        """Return the source interval of an output key, or ``None`` if schema-supplied."""
        return self.provenance_for(key).span

    def source_text_for(self, key: str) -> str | None:
        """Return the exact source substring behind an output key, or ``None``."""
        span = self.source_span_for(key)
        if span is None:
            return None
        return self.source_text[span.start : span.end]

    def rejection_reasons(self) -> list[RejectedFragment]:
        """Return the salvage candidates that were parsed but not selected."""
        return list(self.rejected_fragments)

    def paths(self) -> list[str]:
        """Return every output key carrying a provenance record, in path order."""
        return sorted(self.entries, key=_path_sort_key)

    @staticmethod
    def _normalize_key(key: str) -> str:
        if key.startswith("$"):
            return key
        if key.startswith("["):
            return "$" + key
        return "$." + key


def _path_sort_key(path: str) -> tuple[Any, ...]:
    parts: list[Any] = []
    token = ""
    is_index = False
    for char in path[2:] if path.startswith("$.") else path[1:]:
        if char == ".":
            if token:
                parts.append((0, int(token)) if is_index else (1, token))
            token = ""
            is_index = False
        elif char == "[":
            if token:
                parts.append((1, token))
            token = ""
            is_index = True
        elif char == "]":
            if token:
                parts.append((0, int(token)))
            token = ""
            is_index = False
        else:
            token += char
    if token:
        parts.append((0, int(token)) if is_index else (1, token))
    return tuple(parts)
