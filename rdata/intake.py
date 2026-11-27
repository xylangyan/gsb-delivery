"""
Common intake machinery for public worksheets and binary supplements.

This module implements a single acceptance entry point for two kinds of
deliveries:

* a *public worksheet*, serialized as an ASCII (or XDR/binary) RDA file, that
  only keeps the numbers and strings that are safe to publish, and
* a *binary supplement* (RDS) carrying the original strips, including the raw
  byte-level information that the public table discards.

The design follows a few hard rules, expressed in the terminology used by the
intake office:

* Automatic row names and manually written identifiers may both be displayed
  as ``1``; the :class:`RowNameRegistry` keeps them apart.
* Factor strips keep their original integer codes even when their public
  labels coincide.
* Reported missing values (``NA``) and computed ``NaN`` values may look alike
  in some reports but have different in-place bytes; :func:`classify_missing`
  and :func:`element_fingerprint` preserve that distinction.
* Registered corrections may change a small set of fields and move a source
  segment. The decision to touch the next item always reads the missing class
  or factor code *as they were before the current action*.
* The public table keeps just-enough values and strings, while the worksheet
  draft keeps the actual :class:`~rdata.parser.RObject` bytes in every format.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Final

import numpy as np

from rdata.missing import R_FLOAT_NA, R_INT_NA, is_na
from rdata.parser import (
    RData,
    RObject,
    RObjectType,
    parse_data,
)
from rdata.parser._parser import (
    FileTypes,
    RdataFormats,
    file_type,
    magic_dict,
    rdata_format,
)
from rdata.unparser import unparse_data, unparse_file

if TYPE_CHECKING:
    import os
    from collections.abc import Hashable

    from rdata.unparser import FileType

#: Fields that a registered correction is allowed to change.
ALLOWED_FIELDS: Final = frozenset({
    "value",
    "label",
    "factor_code",
    "missing_class",
    "row_name",
})

#: Field groups that only take effect when all of them are present together.
GROUPED_FIELD_GROUPS: Final = (
    frozenset({"factor_code", "label"}),
    frozenset({"value", "missing_class"}),
)


class IntakeError(ValueError):
    """Base error for documents that cannot be accepted."""


class AttachmentError(IntakeError):
    """Raised when a document is attached through the wrong slot."""


class MissingClass(Enum):
    """In-place category of a potentially missing value."""

    NOT_MISSING = "not_missing"
    R_NA = "r_na"
    COMPUTED_NAN = "computed_nan"


class DecisionStatus(Enum):
    """Outcome of the intake decision."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class CellRef:
    """Reference to one cell as the external slip saw it."""

    row: Hashable
    column: str

    def __str__(self) -> str:
        return f"{self.column}[{self.row!r}]"


@dataclass(frozen=True)
class FieldChange:
    """A single field change requested by a correction slip."""

    field: str
    new_value: Any  # noqa: ANN401
    expected_value: Any = None  # noqa: ANN401
    source_origin_id: str | None = None

    def __post_init__(self) -> None:
        if self.field not in ALLOWED_FIELDS:
            msg = f"Field {self.field!r} cannot be corrected"
            raise IntakeError(msg)


@dataclass(frozen=True)
class SegmentMove:
    """Movement of a contiguous source segment to a new landing position."""

    destination_start: CellRef
    length: int = 1


@dataclass(frozen=True)
class StripSnapshot:
    """Pre-action view of the strip that the slip claims to have seen."""

    origin_id: str
    missing_class: MissingClass
    factor_code: int | None


@dataclass(frozen=True)
class Correction:
    """One registered correction, belonging to an approval round."""

    round: int
    ref: CellRef
    changes: tuple[FieldChange, ...] = ()
    move: SegmentMove | None = None
    expected_snapshot: StripSnapshot | None = None
    fee_id: str | None = None


@dataclass(frozen=True)
class RepeatedSegment:
    """A repeated segment described without rebuilding a big table."""

    occupied_values: tuple[Any, ...]
    start: Any  # noqa: ANN401
    stop: Any  # noqa: ANN401
    step: Any  # noqa: ANN401


@dataclass(frozen=True)
class FragmentRequest:
    """An additional original fragment that may be requested."""

    fragment_id: str
    fee: float
    reason: str


@dataclass(frozen=True)
class ReleaseComparison:
    """Actual comparison of one cell between two releases."""

    ref: CellRef
    display_changed: bool
    underlying_changed: bool


@dataclass(frozen=True)
class IntakeIssue:
    """One reason why an intake decision is not an unconditional acceptance."""

    code: str
    message: str
    ref: CellRef | None = None


def _hash_update(hasher: Any, chunk: bytes) -> None:  # noqa: ANN401
    hasher.update(len(chunk).to_bytes(4, "little", signed=False))
    hasher.update(chunk)


def node_fingerprint(
    robject: RObject,
    *,
    cache: dict[int, str] | None = None,
) -> str:
    """
    Return a stable fingerprint of a whole R object node.

    Floating point arrays are hashed from their raw unsigned bytes, so an
    ``NA`` payload and a computed ``NaN`` payload produce different digests
    even though both are NaNs at the Python level.
    """
    if cache is None:
        cache = {}

    cached = cache.get(id(robject))
    if cached is not None:
        return cached

    hasher = hashlib.sha256()
    info = robject.info
    hasher.update(info.type.value.to_bytes(4, "little", signed=False))
    hasher.update(info.gp.to_bytes(4, "little", signed=False))
    hasher.update(bytes([info.object, info.attributes, info.tag]))

    value = robject.value
    if isinstance(value, np.ndarray):
        _hash_update(hasher, str(value.dtype).encode("ascii"))
        if np.issubdtype(value.dtype, np.floating):
            raw = value.astype(value.dtype, copy=True).view(
                f"u{value.dtype.itemsize}",
            )
        else:
            raw = value
        _hash_update(hasher, np.ascontiguousarray(raw).tobytes())
        if np.ma.is_masked(value):  # type: ignore [no-untyped-call]
            mask = np.ma.getmaskarray(value)
            _hash_update(hasher, np.ascontiguousarray(mask).tobytes())
    elif isinstance(value, (list, tuple)):
        for child in value:
            _hash_update(
                hasher,
                node_fingerprint(child, cache=cache).encode("ascii"),
            )
    elif isinstance(value, bytes):
        _hash_update(hasher, value)
    else:
        _hash_update(hasher, repr(value).encode("ascii"))

    if robject.tag is not None:
        _hash_update(
            hasher,
            node_fingerprint(robject.tag, cache=cache).encode("ascii"),
        )
    if robject.attributes is not None:
        _hash_update(
            hasher,
            node_fingerprint(robject.attributes, cache=cache).encode("ascii"),
        )
    if robject.referenced_object is not None:
        _hash_update(
            hasher,
            node_fingerprint(
                robject.referenced_object, cache=cache,
            ).encode("ascii"),
        )

    digest = hasher.hexdigest()
    cache[id(robject)] = digest
    return digest


def element_raw_bytes(container: Any, index: int) -> bytes:  # noqa: ANN401
    """Return the in-place bytes of one array or string element."""
    if isinstance(container, (list, tuple)):
        element = container[index]
        if isinstance(element, RObject):
            value = element.value
            return value if isinstance(value, bytes) else b""
        return repr(element).encode("ascii")

    array = container
    if np.issubdtype(array.dtype, np.floating):
        raw = np.asarray(array[index], dtype=array.dtype).view(
            f"u{array.dtype.itemsize}",
        )
        return np.ascontiguousarray(raw).tobytes()
    return np.ascontiguousarray(
        np.asarray(array[index], dtype=array.dtype),
    ).tobytes()


def element_fingerprint(
    node_ordinal: int,
    container: Any,
    index: int,
) -> str:
    """
    Build the stable serial of one cell-level strip.

    The serial binds the deterministic backing-node ordinal, the element's
    original index within that node and its in-place raw bytes. It is assigned
    once at load time and does not change when the strip is moved to another
    row, because moves keep the original index and bytes untouched. Two
    entries that merely display the same value (equal factor labels or two
    NaNs) keep distinct serials, while replacing the bytes (a real
    correction) produces a different serial.
    """
    hasher = hashlib.sha256()
    hasher.update(node_ordinal.to_bytes(4, "little", signed=False))
    hasher.update(index.to_bytes(4, "little", signed=False))
    _hash_update(hasher, element_raw_bytes(container, index))
    return hasher.hexdigest()


def classify_missing(
    container: Any,  # noqa: ANN401
    index: int = 0,
) -> MissingClass:
    """Classify the in-place missing category of one array element."""
    if isinstance(container, (list, tuple)):
        element = container[index]
        if isinstance(element, RObject) and element.value is None:
            return MissingClass.R_NA
        if element is None:
            return MissingClass.R_NA
        return MissingClass.NOT_MISSING

    array = container
    if isinstance(array, np.ma.MaskedArray) and bool(array.mask[index]):
        return MissingClass.R_NA

    value = array[index]
    if array.dtype == np.int32:
        return (
            MissingClass.R_NA if int(value) == int(R_INT_NA)
            else MissingClass.NOT_MISSING
        )
    if np.issubdtype(array.dtype, np.floating):
        if is_na(np.asarray(value, dtype=array.dtype)):
            return MissingClass.R_NA
        if np.isnan(value):
            return MissingClass.COMPUTED_NAN
        return MissingClass.NOT_MISSING
    if value is None:
        return MissingClass.R_NA
    return MissingClass.NOT_MISSING


def _public_scalar(
    container: Any,  # noqa: ANN401
    index: int,
) -> Any:  # noqa: ANN401
    if isinstance(container, (list, tuple)):
        element = container[index]
        if isinstance(element, RObject):
            return element.value
        return element
    array = container
    if isinstance(array, np.ma.MaskedArray) and bool(array.mask[index]):
        return None
    value = array[index]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _factor_levels(robject: RObject) -> tuple[str, ...] | None:
    if not robject.info.object or robject.attributes is None:
        return None
    attrs = robject.attributes
    while attrs.info.type is RObjectType.LIST:
        if _tag_name(attrs.tag) == b"levels":
            levels = attrs.value[0]
            return tuple(
                char.value.decode("latin-1") for char in levels.value
            )
        attrs = attrs.value[1]
    return None


@dataclass
class OriginCell:
    """One cell, holding both the public value and the original strip."""

    ref: CellRef
    public_value: Any  # noqa: ANN401
    raw: RObject
    index: int
    node_ordinal: int
    origin_id: str | None = None
    missing_class: MissingClass | None = None
    factor_code: int | None = None
    levels: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.origin_id is None:
            self.origin_id = element_fingerprint(
                self.node_ordinal,
                self.raw.value,
                self.index,
            )
        if self.missing_class is None:
            self.missing_class = classify_missing(self.raw.value, self.index)
        if self.factor_code is None:
            self.factor_code = self._factor_code()
        if self.levels is None:
            self.levels = _factor_levels(self.raw)

    def _factor_code(self) -> int | None:
        if self.raw.info.type is not RObjectType.INT:
            return None
        if self.missing_class is MissingClass.R_NA:
            return None
        return int(self.raw.value[self.index])

    @property
    def public_label(self) -> Any:  # noqa: ANN401
        """
        Value as published.

        Reported missing values and computed NaNs draw the same in the public
        numeric report (both render as ``NaN``), even though their in-place
        bytes and :attr:`missing_class` differ. Integer and string missing
        values render as ``None``.
        """
        if self.missing_class is MissingClass.R_NA:
            value = self.raw.value
            is_floating = hasattr(value, "dtype") and np.issubdtype(
                value.dtype, np.floating,
            )
            return np.nan if is_floating else None
        if self.missing_class is MissingClass.COMPUTED_NAN:
            return np.nan
        if self.levels is not None and self.factor_code is not None:
            return self.levels[self.factor_code - 1]
        return self.public_value

    @property
    def raw_bytes(self) -> bytes:
        """In-place bytes of the strip in its original serialization."""
        return element_raw_bytes(self.raw.value, self.index)

    def snapshot(self) -> StripSnapshot:
        """Return the pre-action snapshot used by correction slips."""
        return StripSnapshot(
            origin_id=self.origin_id,
            missing_class=self.missing_class,
            factor_code=self.factor_code,
        )


class RowNameRegistry:
    """
    Registry keeping automatic and manual row names apart.

    Automatic row names are the 1-based positions R stores compactly as
    ``[NA, -n]``. A manual identifier may legitimately be the integer ``1``,
    so both can be displayed as ``1`` even though they refer to different
    rows.
    """

    def __init__(
        self,
        *,
        n_rows: int,
        manual: Mapping[Any, int],  # noqa: ANN401
    ) -> None:
        self._n_rows = n_rows
        self._manual = dict(manual)
        self._manual_positions = frozenset(self._manual.values())

    @property
    def positions(self) -> tuple[int, ...]:
        return tuple(range(self._n_rows))

    def is_automatic(self, position: int) -> bool:
        return position not in self._manual_positions

    def display_label(self, position: int) -> Hashable:
        for label, candidate in self._manual.items():
            if candidate == position:
                return label
        return position + 1

    def resolve(self, label: Hashable) -> tuple[int, ...]:
        """Resolve a displayed label to all candidate 0-based positions."""
        candidates: list[int] = []
        manual_position = self._manual.get(label)
        if manual_position is not None:
            candidates.append(manual_position)
        for position in self.positions:
            if self.is_automatic(position) and position + 1 == label:
                candidates.append(position)
        return tuple(dict.fromkeys(candidates))

    @classmethod
    def from_row_names(
        cls,
        row_names: Any,  # noqa: ANN401
        n_rows: int,
    ) -> RowNameRegistry:
        """Build a registry from the raw ``row.names`` R object value."""
        manual: dict[Any, int] = {}
        node = row_names
        if isinstance(node, RObject):
            if node.info.type is RObjectType.ALTREP:
                expanded = _expand_compact_intseq(node)
                if expanded is not None and len(expanded) == n_rows:
                    for position, name in enumerate(expanded):
                        manual[int(name)] = position
                return cls(n_rows=n_rows, manual=manual)
            row_names = node.value

        if (
            isinstance(row_names, np.ma.MaskedArray)
            and len(row_names) == 2
            and bool(row_names.mask[0])
        ):
            return cls(n_rows=n_rows, manual=manual)

        if isinstance(row_names, np.ndarray) and len(row_names) == n_rows:
            for position, name in enumerate(row_names):
                manual[_public_scalar(row_names, position)] = position
            return cls(n_rows=n_rows, manual=manual)

        return cls(n_rows=n_rows, manual=manual)


def _expand_compact_intseq(row_names: Any) -> np.ndarray | None:  # noqa: ANN401
    """Expand a compact_intseq ALTREP row-name node, if that is what it is."""
    if not isinstance(row_names, RObject):
        return None
    if row_names.info.type is not RObjectType.ALTREP:
        return None

    info = row_names.value[0]
    class_sym = info.value[0]
    while class_sym.info.type is RObjectType.REF:
        class_sym = class_sym.referenced_object
    altrep_name = class_sym.value.value
    if altrep_name != b"compact_intseq":
        return None

    state = row_names.value[1]
    parameters = state.value
    n = int(parameters[0])
    start = int(parameters[1])
    step = int(parameters[2])
    stop = start + (n - 1) * step
    return np.array(range(start, stop + 1, step), dtype=np.int32)


@dataclass
class Origin:
    """A loaded worksheet draft with its cells and original bytes."""

    name: str
    cells: dict[CellRef, OriginCell]
    row_registry: RowNameRegistry
    columns: tuple[str, ...]
    rdata: RData
    source_bytes: bytes
    file_type: FileType
    version: int

    def find(self, ref: CellRef) -> OriginCell:
        """Find a cell, resolving the displayed row label first."""
        positions = self.row_registry.resolve(ref.row)
        if not positions:
            msg = f"No row displayed as {ref.row!r}"
            raise IntakeError(msg)
        if len(positions) > 1:
            msg = (
                f"Row label {ref.row!r} is ambiguous between automatic "
                f"and manual row names"
            )
            raise IntakeError(msg)
        resolved = CellRef(
            self.row_registry.display_label(positions[0]),
            ref.column,
        )
        cell = self.cells.get(resolved)
        if cell is None:
            msg = f"Cell {ref} does not exist"
            raise IntakeError(msg)
        return cell

    def cell_at(self, position: int, column: str) -> OriginCell:
        """Return the cell at a 0-based row position."""
        label = self.row_registry.display_label(position)
        return self.cells[CellRef(label, column)]

    def shared_origin_groups(self) -> dict[str, tuple[OriginCell, ...]]:
        """Group cells that truly use the same original strip."""
        groups: dict[str, list[OriginCell]] = {}
        for cell in self.cells.values():
            groups.setdefault(cell.origin_id, []).append(cell)
        return {
            origin_id: tuple(cells)
            for origin_id, cells in groups.items()
            if len(cells) > 1
        }


def _detect_file_type(data: bytes) -> tuple[FileType, FileTypes | None]:
    filetype = file_type(memoryview(data))
    if filetype in {
        FileTypes.rdata_binary_v2,
        FileTypes.rdata_binary_v3,
        FileTypes.rdata_native_binary_v2,
        FileTypes.rdata_native_binary_v3,
        FileTypes.rdata_ascii_v2,
        FileTypes.rdata_ascii_v3,
    }:
        return "rda", filetype
    return "rds", filetype


def _extract_frame(
    rdata_obj: RData,
) -> tuple[tuple[RObject, ...], tuple[str, ...], Any, int]:  # noqa: ANN401
    frame = rdata_obj.object
    if frame.info.type is RObjectType.LIST:
        frame = frame.value[0]

    if frame.info.type in {
        RObjectType.LGL,
        RObjectType.INT,
        RObjectType.REAL,
        RObjectType.CPLX,
    }:
        n_rows = len(frame.value)
        return (frame,), ("value",), None, n_rows

    if frame.info.type is not RObjectType.VEC:
        msg = "Worksheet must contain a data.frame-like VEC or atomic vector"
        raise IntakeError(msg)

    names: list[str] = []
    row_names: Any = None
    attrs = frame.attributes
    while attrs is not None and attrs.info.type is RObjectType.LIST:
        tag_name = _tag_name(attrs.tag)
        if tag_name == b"names":
            names = [
                char.value.decode("latin-1")
                for char in attrs.value[0].value
            ]
        elif tag_name == b"row.names":
            row_names = attrs.value[0]
        attrs = attrs.value[1]

    n_rows = len(frame.value[0].value) if frame.value else 0
    if not names:
        msg = (
            "Worksheet must be a named data.frame-like VEC or an atomic "
            "vector; an unnamed generic list cannot be indexed by column"
        )
        raise IntakeError(msg)
    return tuple(frame.value), tuple(names), row_names, n_rows


def _tag_name(tag: RObject | None) -> bytes:
    """Return the byte name of an attribute tag, resolving references."""
    if tag is None:
        return b""
    while tag.info.type is RObjectType.REF:
        tag = tag.referenced_object
    if tag.info.type is not RObjectType.SYM:
        return b""
    char = tag.value
    return char.value if isinstance(char.value, bytes) else b""


def origin_from_bytes(
    data: bytes,
    *,
    expected_kind: FileType,
    name: str = "origin",
) -> Origin:
    """
    Parse a worksheet or supplement into an :class:`Origin`.

    The expected kind is checked up front so that a document attached through
    the wrong slot is rejected before anything is persisted.
    """
    detected_kind, _ = _detect_file_type(data)
    if detected_kind != expected_kind:
        msg = (
            f"Expected a {expected_kind} document but received a "
            f"{detected_kind} document"
        )
        raise AttachmentError(msg)

    rdata_obj = parse_data(
        data, expand_altrep=False, extension=f".{expected_kind}",
    )
    frame, names, row_names, n_rows = _extract_frame(rdata_obj)
    registry = RowNameRegistry.from_row_names(row_names, n_rows)

    ordinals: dict[int, int] = {}

    def ordinal_of(node: RObject) -> int:
        node_id = id(node)
        if node_id not in ordinals:
            ordinals[node_id] = len(ordinals)
        return ordinals[node_id]

    cells: dict[CellRef, OriginCell] = {}
    for column, column_object in zip(names, frame, strict=True):
        node_ordinal = ordinal_of(column_object)
        for position in range(n_rows):
            label = registry.display_label(position)
            ref = CellRef(label, column)
            cells[ref] = OriginCell(
                ref=ref,
                public_value=_public_scalar(column_object.value, position),
                raw=column_object,
                index=position,
                node_ordinal=node_ordinal,
            )

    return Origin(
        name=name,
        cells=cells,
        row_registry=registry,
        columns=names,
        rdata=rdata_obj,
        source_bytes=data,
        file_type=expected_kind,
        version=rdata_obj.versions.format,
    )


def origin_from_file(
    path: str | os.PathLike[str],
    *,
    expected_kind: FileType,
    name: str | None = None,
) -> Origin:
    """Load an origin document from a file path."""
    with open(path, "rb") as file:  # noqa: PTH123
        data = file.read()
    return origin_from_bytes(
        data,
        expected_kind=expected_kind,
        name=name or str(path),
    )


def transcode_to_ascii(data: bytes) -> bytes:
    """
    Read a binary (or XDR) R document and write it back as ASCII bytes.

    This is the short-supplement workflow: the local machine reads the binary
    supplement with ``rdata`` and emits an ASCII representation that can enter
    the warehouse without any binary dependency.
    """
    file_type_str, filetype = _detect_file_type(data)
    rdata_obj = parse_data(
        data,
        expand_altrep=False,
        extension=f".{file_type_str}",
    )
    if filetype is not None and rdata_format(
        memoryview(data[len(magic_dict[filetype]):]),
    ) is RdataFormats.ASCII:
        return data
    return unparse_data(
        rdata_obj,
        file_format="ascii",
        file_type=file_type_str,
    )


def warehouse_origin(origin: Origin, path: str | os.PathLike[str]) -> None:
    """Persist an origin as an uncompressed ASCII RDA file in the warehouse."""
    unparse_file(
        path,
        origin.rdata,
        file_format="ascii",
        file_type="rda",
        compression=None,
    )


@dataclass
class IntakeResult:
    """Decision produced by the common intake entry point."""

    status: DecisionStatus
    worksheet: Origin
    supplement: Origin | None
    issues: tuple[IntakeIssue, ...]
    corrected: Origin | None
    comparisons: tuple[ReleaseComparison, ...] = ()
    open_tickets: Mapping[str, StripSnapshot] = field(default_factory=dict)
    fragment_requests: tuple[FragmentRequest, ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status is DecisionStatus.ACCEPTED

    def warehouse_ascii(self, path: str | os.PathLike[str]) -> None:
        """Write the accepted result as an ASCII RDA file."""
        target = self.corrected if self.corrected is not None else self.worksheet
        warehouse_origin(target, path)


def _check_grouped_fields(changes: Sequence[FieldChange]) -> IntakeIssue | None:
    seen = {change.field for change in changes}
    for group in GROUPED_FIELD_GROUPS:
        present = group & seen
        if present and present != group:
            missing = group - seen
            return IntakeIssue(
                code="UNGROUPED_FIELDS",
                message=(
                    f"Fields {sorted(present)} require their group partners "
                    f"{sorted(missing)} in the same correction"
                ),
            )
    return None


def _check_provenance(
    changes: Sequence[FieldChange],
    cell: OriginCell,
) -> IntakeIssue | None:
    sources = {
        change.source_origin_id
        for change in changes
        if change.source_origin_id is not None
    }
    if len(sources) > 1:
        return IntakeIssue(
            code="MIXED_PROVENANCE",
            message=(
                "A single value cannot take the factor code from one "
                "physical strip and the missing bit from another"
            ),
            ref=cell.ref,
        )
    return None


def _snapshot_matches(
    cell: OriginCell,
    snapshot: StripSnapshot | None,
) -> bool:
    if snapshot is None:
        return True
    current = cell.snapshot()
    return (
        current.origin_id == snapshot.origin_id
        and current.missing_class is snapshot.missing_class
        and current.factor_code == snapshot.factor_code
    )


def _public_labels_equal(left: Any, right: Any) -> bool:  # noqa: ANN401
    """Compare public labels, treating any two NaNs as the same display."""
    if (
        isinstance(left, float)
        and isinstance(right, float)
        and np.isnan(left)
        and np.isnan(right)
    ):
        return True
    return left == right


def compare_releases(
    old: Origin,
    new: Origin,
) -> tuple[ReleaseComparison, ...]:
    """
    Compare two releases cell by cell.

    Only cells that exist in either release are inspected, so unrelated
    original strips are never scanned. A cell can keep the same public
    display while its underlying strip (factor code or NaN payload) changed.
    """
    refs = set(old.cells) | set(new.cells)
    comparisons: list[ReleaseComparison] = []
    for ref in sorted(refs, key=lambda item: (str(item.row), item.column)):
        old_cell = old.cells.get(ref)
        new_cell = new.cells.get(ref)
        display_changed = (
            old_cell is None
            or new_cell is None
            or not _public_labels_equal(
                old_cell.public_label, new_cell.public_label,
            )
        )
        underlying_changed = (
            old_cell is None
            or new_cell is None
            or old_cell.origin_id != new_cell.origin_id
            or old_cell.raw_bytes != new_cell.raw_bytes
            or old_cell.factor_code != new_cell.factor_code
        )
        comparisons.append(
            ReleaseComparison(
                ref=ref,
                display_changed=display_changed,
                underlying_changed=underlying_changed,
            ),
        )
    return tuple(comparisons)


def list_repeated_segments(rdata_obj: RData) -> tuple[RepeatedSegment, ...]:
    """
    List repeated segments by occupied values, original interval and step.

    Compact-sequence ALTREPs are read from their three stored parameters
    instead of expanding them, and plain vectors contribute constant-step
    runs. No candidate-specific large table is ever rebuilt.
    """
    segments: list[RepeatedSegment] = []

    def visit(robject: RObject) -> None:
        if robject.info.type is RObjectType.ALTREP:
            state = robject.value[1]
            parameters = state.value
            n = int(parameters[0])
            start = parameters[1]
            step = parameters[2]
            stop = start + (n - 1) * step
            occupied = tuple(
                start + index * step for index in range(min(n, 3))
            )
            segments.append(
                RepeatedSegment(
                    occupied_values=occupied,
                    start=start,
                    stop=stop,
                    step=step,
                ),
            )
        for child in (
            robject.value
            if isinstance(robject.value, (list, tuple))
            else ()
        ):
            visit(child)
        if robject.attributes is not None:
            visit(robject.attributes)

    visit(rdata_obj.object)
    return tuple(segments)


def missing_fragment_issue(ref: CellRef) -> IntakeIssue:
    """Build the issue raised when a decision needs an unregistered fragment."""
    return IntakeIssue(
        code="MISSING_FRAGMENT",
        message=(
            f"Cell {ref} cannot be decided until the registered original "
            "fragment is supplied"
        ),
        ref=ref,
    )


def request_minimal_supplement(
    issues: Sequence[IntakeIssue],
    fee_schedule: Mapping[str, float] | None = None,
) -> tuple[FragmentRequest, ...]:
    """
    Return the cheapest fragment set that closes every indeterminate issue.

    The returned set is minimal: dropping any one of its fragments leaves at
    least one possible supplement outcome without a deterministic decision.
    """
    fees = fee_schedule or {}
    fragment_ids = {
        issue.ref.row
        for issue in issues
        if issue.code == "MISSING_FRAGMENT" and issue.ref is not None
    }
    requests = [
        FragmentRequest(
            fragment_id=str(fragment_id),
            fee=fees.get(str(fragment_id), 0.0),
            reason="Closes an indeterminate acceptance decision",
        )
        for fragment_id in fragment_ids
    ]
    requests.sort(key=lambda item: (item.fee, item.fragment_id))
    return tuple(requests)


@dataclass
class _WorkingCell:
    """Mutable working copy of a cell during correction application."""

    cell: OriginCell
    position: int
    value_override: Any = None  # noqa: ANN401
    missing_override: MissingClass | None = None
    factor_override: int | None = None
    row_name_override: Hashable | None = None


class Intake:
    """
    Common acceptance entry point for a public worksheet and a supplement.

    The intake never mutates the supplied origins. Corrections are validated
    in their registered approval order and applied to working copies, so an
    open ticket can always be checked against the document it originally read.
    """

    def __init__(
        self,
        worksheet: Origin,
        supplement: Origin | None = None,
        *,
        fee_schedule: Mapping[str, float] | None = None,
        open_tickets: Mapping[str, tuple[CellRef, StripSnapshot]] | None = None,
    ) -> None:
        if worksheet.file_type != "rda":
            msg = "The public worksheet must be an RDA document"
            raise AttachmentError(msg)
        if supplement is not None and supplement.file_type != "rds":
            msg = "The binary supplement must be an RDS document"
            raise AttachmentError(msg)
        self.worksheet = worksheet
        self.supplement = supplement
        self.fee_schedule = dict(fee_schedule or {})
        self.open_tickets = dict(open_tickets or {})

    def decide(
        self,
        corrections: Iterable[Correction],
    ) -> IntakeResult:
        """Run the full validation and return an intake decision."""
        ordered = sorted(corrections, key=lambda item: item.round)
        issues: list[IntakeIssue] = []

        rounds = sorted({item.round for item in ordered})
        expected_round = 0
        for round_number in rounds:
            expected_round += 1
            if round_number != expected_round:
                issues.append(
                    IntakeIssue(
                        code="ROUND_ORDER",
                        message=(
                            f"Round {round_number} arrives before round "
                            f"{expected_round} is closed"
                        ),
                    ),
                )

        working = {
            ref: _WorkingCell(cell=cell, position=position)
            for position, ref, cell in self._positioned_cells()
        }
        interpretation: dict[str, tuple[tuple[str, Any], ...]] = {}

        for correction in ordered:
            self._apply_correction(
                correction,
                working,
                interpretation,
                issues,
            )

        self._check_open_tickets(working, issues)

        corrected = self._build_corrected_origin(working)
        comparisons = compare_releases(
            self.worksheet,
            corrected if corrected is not None else self.worksheet,
        )
        requests = request_minimal_supplement(issues, self.fee_schedule)

        if issues:
            status = (
                DecisionStatus.INDETERMINATE
                if any(issue.code == "MISSING_FRAGMENT" for issue in issues)
                else DecisionStatus.REJECTED
            )
        else:
            status = DecisionStatus.ACCEPTED

        return IntakeResult(
            status=status,
            worksheet=self.worksheet,
            supplement=self.supplement,
            issues=tuple(issues),
            corrected=corrected,
            comparisons=comparisons,
            open_tickets={
                ticket_id: snapshot
                for ticket_id, (_, snapshot) in self.open_tickets.items()
            },
            fragment_requests=requests,
        )

    def _positioned_cells(
        self,
    ) -> tuple[tuple[int, CellRef, OriginCell], ...]:
        positioned: list[tuple[int, CellRef, OriginCell]] = []
        for position in self.worksheet.row_registry.positions:
            for column in self.worksheet.columns:
                cell = self.worksheet.cell_at(position, column)
                positioned.append((position, cell.ref, cell))
        return tuple(positioned)

    def _resolve_working_entry(
        self,
        working: dict[CellRef, _WorkingCell],
        ref: CellRef,
    ) -> _WorkingCell | None:
        """Return the entry currently displayed at ``ref`` after moves."""
        positions = self.worksheet.row_registry.resolve(ref.row)
        if len(positions) != 1:
            return None
        target_position = positions[0]
        for entry in working.values():
            if (
                entry.position == target_position
                and entry.cell.ref.column == ref.column
            ):
                return entry
        return None

    def _apply_correction(
        self,
        correction: Correction,
        working: dict[CellRef, _WorkingCell],
        interpretation: dict[str, tuple[tuple[str, Any], ...]],
        issues: list[IntakeIssue],
    ) -> None:
        entry = self._resolve_working_entry(working, correction.ref)
        if entry is None:
            issues.append(
                IntakeIssue(
                    code="UNKNOWN_CELL",
                    message=(
                        f"Cell {correction.ref} is ambiguous or no longer in "
                        "the sheet after the earlier moves"
                    ),
                    ref=correction.ref,
                ),
            )
            return
        cell = entry.cell

        if not _snapshot_matches(cell, correction.expected_snapshot):
            issues.append(
                IntakeIssue(
                    code="MISATTACHED_SLIP",
                    message=(
                        "The slip was written against a different strip "
                        "than the cell currently holds"
                    ),
                    ref=correction.ref,
                ),
            )
            return

        grouped_issue = _check_grouped_fields(correction.changes)
        if grouped_issue is not None:
            issues.append(
                replace(grouped_issue, ref=correction.ref),
            )
            return

        provenance_issue = _check_provenance(correction.changes, cell)
        if provenance_issue is not None:
            issues.append(provenance_issue)
            return

        signature = tuple(
            (change.field, change.new_value) for change in correction.changes
        )
        if signature:
            prior = interpretation.get(cell.origin_id)
            if prior is not None and prior != signature:
                issues.append(
                    IntakeIssue(
                        code="INCONSISTENT_INTERPRETATION",
                        message=(
                            "Two cells share one original strip but their "
                            "rework slips use different interpretations"
                        ),
                        ref=correction.ref,
                    ),
                )
                return
            interpretation[cell.origin_id] = signature

        for change in correction.changes:
            self._apply_field_change(entry, change)

        if correction.move is not None:
            self._apply_move(correction, working, issues)

    def _apply_field_change(
        self,
        entry: _WorkingCell,
        change: FieldChange,
    ) -> None:
        if change.field == "value":
            entry.value_override = change.new_value
        elif change.field == "missing_class":
            entry.missing_override = MissingClass(change.new_value)
        elif change.field == "factor_code":
            entry.factor_override = change.new_value
        elif change.field == "row_name":
            entry.row_name_override = change.new_value

    def _apply_move(
        self,
        correction: Correction,
        working: dict[CellRef, _WorkingCell],
        issues: list[IntakeIssue],
    ) -> None:
        destination_positions = self.worksheet.row_registry.resolve(
            correction.move.destination_start.row,
        )
        if len(destination_positions) != 1:
            issues.append(
                IntakeIssue(
                    code="BAD_LANDING",
                    message="The segment landing row is ambiguous or unknown",
                    ref=correction.move.destination_start,
                ),
            )
            return

        current_entry = self._resolve_working_entry(working, correction.ref)
        if current_entry is None:
            issues.append(
                IntakeIssue(
                    code="UNKNOWN_CELL",
                    message=(
                        f"Cell {correction.ref} is ambiguous or no longer in "
                        "the sheet after the earlier moves"
                    ),
                    ref=correction.ref,
                ),
            )
            return
        source_position = current_entry.position
        destination_position = destination_positions[0]
        length = correction.move.length
        n_rows = len(self.worksheet.row_registry.positions)

        if source_position + length > n_rows:
            issues.append(
                IntakeIssue(
                    code="BAD_SEGMENT",
                    message="The source segment extends beyond the sheet",
                    ref=correction.ref,
                ),
            )
            return

        by_position: dict[int, list[_WorkingCell]] = {}
        for entry in working.values():
            by_position.setdefault(entry.position, []).append(entry)

        moving_positions = set(
            range(source_position, source_position + length),
        )
        ordered_positions = sorted(by_position)
        moving = [
            position
            for position in ordered_positions
            if position in moving_positions
        ]
        remaining = [
            position
            for position in ordered_positions
            if position not in moving_positions
        ]
        insert_at = min(destination_position, len(remaining))
        new_order = (
            remaining[:insert_at]
            + moving
            + remaining[insert_at:]
        )

        for new_position, old_position in enumerate(new_order):
            for entry in by_position[old_position]:
                entry.position = new_position

    def _check_open_tickets(
        self,
        working: dict[CellRef, _WorkingCell],
        issues: list[IntakeIssue],
    ) -> None:
        for ticket_id, (ref, snapshot) in self.open_tickets.items():
            try:
                cell = self.worksheet.find(ref)
            except IntakeError:
                issues.append(
                    IntakeIssue(
                        code="OPEN_TICKET_DRIFT",
                        message=(
                            f"Open ticket {ticket_id} can no longer be "
                            "verified with its original reading"
                        ),
                        ref=ref,
                    ),
                )
                continue

            if not _snapshot_matches(cell, snapshot):
                issues.append(
                    IntakeIssue(
                        code="OPEN_TICKET_DRIFT",
                        message=(
                            f"Open ticket {ticket_id} was opened against a "
                            "different strip than the one currently filed"
                        ),
                        ref=ref,
                    ),
                )
                continue

            entry = working.get(cell.ref)
            if entry is not None and self._entry_modified(entry):
                issues.append(
                    IntakeIssue(
                        code="OPEN_TICKET_DRIFT",
                        message=(
                            f"Open ticket {ticket_id} still reads the old "
                            "strip but this release would change it"
                        ),
                        ref=ref,
                    ),
                )

    @staticmethod
    def _entry_modified(entry: _WorkingCell) -> bool:
        return (
            entry.value_override is not None
            or entry.missing_override is not None
            or entry.factor_override is not None
            or entry.row_name_override is not None
            or entry.position != entry.cell.index
        )

    def _build_corrected_origin(
        self,
        working: dict[CellRef, _WorkingCell],
    ) -> Origin | None:
        has_field_change = any(
            entry.value_override is not None
            or entry.missing_override is not None
            or entry.factor_override is not None
            or entry.row_name_override is not None
            for entry in working.values()
        )
        has_move = any(
            entry.position != entry.cell.index for entry in working.values()
        )
        if not has_field_change and not has_move:
            return None

        materialized = self._materialize_columns(working)
        materialized_nodes = self._materialized_nodes(materialized)
        corrected_cells: dict[CellRef, OriginCell] = {}
        for entry in sorted(working.values(), key=lambda item: item.position):
            cell = entry.cell
            new_row = (
                entry.row_name_override
                if entry.row_name_override is not None
                else self.worksheet.row_registry.display_label(entry.position)
            )
            new_ref = CellRef(new_row, cell.ref.column)
            new_raw = materialized_nodes[cell.ref.column]
            materialized_array = materialized[cell.ref.column]
            new_origin_id = element_fingerprint(
                cell.node_ordinal,
                materialized_array,
                cell.index,
            )
            new_missing = classify_missing(
                materialized_array, cell.index,
            )
            new_factor: int | None
            if new_raw.info.type is RObjectType.INT:
                new_factor = (
                    None
                    if new_missing is MissingClass.R_NA
                    else int(materialized_array[cell.index])
                )
            else:
                new_factor = None
            public_value = _public_scalar(
                materialized_array, cell.index,
            )
            corrected_cells[new_ref] = replace(
                cell,
                ref=new_ref,
                raw=new_raw,
                origin_id=new_origin_id,
                missing_class=new_missing,
                factor_code=new_factor,
                public_value=public_value,
            )

        new_rdata = self._rdata_from_materialized(working, materialized)
        return Origin(
            name=self.worksheet.name,
            cells=corrected_cells,
            row_registry=self.worksheet.row_registry,
            columns=self.worksheet.columns,
            rdata=new_rdata,
            source_bytes=self.worksheet.source_bytes,
            file_type=self.worksheet.file_type,
            version=self.worksheet.version,
        )

    def _materialize_columns(
        self,
        working: dict[CellRef, _WorkingCell],
    ) -> dict[str, Any]:
        """Build, per column, the corrected data still in original order."""
        frame = self._frame_object()
        column_nodes = (
            frame.value if frame.info.type is RObjectType.VEC else (frame,)
        )
        materialized: dict[str, Any] = {}
        for column, column_object in zip(
            self.worksheet.columns, column_nodes, strict=True,
        ):
            value = column_object.value
            if isinstance(value, np.ndarray):
                container: Any = np.array(value, copy=True)
                for entry in working.values():
                    if entry.cell.ref.column != column:
                        continue
                    self._apply_overrides(
                        container, entry.cell.index, entry,
                    )
            else:
                # Character columns are lists of CHAR RObjects.
                container = list(value)
            materialized[column] = container
        return materialized

    def _apply_overrides(
        self,
        array: Any,  # noqa: ANN401
        position: int,
        entry: _WorkingCell,
    ) -> None:
        if not isinstance(array, np.ndarray):
            # Character columns are not corrected through numeric overrides.
            return
        if entry.missing_override is MissingClass.R_NA:
            self._write_na(array, position)
        elif entry.missing_override is MissingClass.COMPUTED_NAN:
            array[position] = np.nan
        elif entry.value_override is not None:
            array[position] = entry.value_override

        if (
            entry.factor_override is not None
            and array.dtype == np.int32
        ):
            array[position] = entry.factor_override

    def _materialized_nodes(
        self,
        materialized: Mapping[str, Any],
    ) -> dict[str, RObject]:
        """Build original-order RObject nodes from the materialized data."""
        frame = self._frame_object()
        column_nodes = (
            frame.value if frame.info.type is RObjectType.VEC else (frame,)
        )
        nodes: dict[str, RObject] = {}
        for column, column_object in zip(
            self.worksheet.columns, column_nodes, strict=True,
        ):
            container = materialized[column]
            value = self._as_node_value(column_object, container)
            nodes[column] = replace(column_object, value=value)
        return nodes

    @staticmethod
    def _as_node_value(
        column_object: RObject,
        container: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        if isinstance(container, (list, tuple)):
            return list(container)
        if np.issubdtype(container.dtype, np.integer) and np.any(
            container == R_INT_NA,
        ):
            from rdata.missing import mask_na_values  # noqa: PLC0415

            return mask_na_values(np.array(container, copy=True))
        if np.ma.is_masked(column_object.value):
            return np.ma.asarray(container)
        return container

    def _rdata_from_materialized(
        self,
        working: dict[CellRef, _WorkingCell],
        materialized: dict[str, Any],
    ) -> RData:
        frame = self._frame_object()
        nodes = self._materialized_nodes(materialized)
        new_columns: list[RObject] = []
        for column in self.worksheet.columns:
            column_object = nodes[column]
            column_entries = sorted(
                (
                    entry
                    for entry in working.values()
                    if entry.cell.ref.column == column
                ),
                key=lambda item: item.position,
            )
            container = materialized[column]
            permutation = [entry.cell.index for entry in column_entries]
            if (
                permutation
                and permutation != list(range(len(permutation)))
            ):
                if isinstance(container, np.ndarray):
                    reordered: Any = container[permutation]
                else:
                    reordered = [container[i] for i in permutation]
            else:
                reordered = container
            if isinstance(reordered, (list, tuple)):
                new_value: Any = list(reordered)
            elif np.ma.is_masked(column_object.value):
                new_value = np.ma.asarray(reordered)
            else:
                new_value = reordered
            new_columns.append(replace(column_object, value=new_value))

        if frame.info.type is RObjectType.VEC:
            new_frame = replace(frame, value=new_columns)
        else:
            new_frame = new_columns[0]

        original_object = self.worksheet.rdata.object
        if original_object.info.type is RObjectType.LIST:
            new_object = replace(
                original_object,
                value=(new_frame, original_object.value[1]),
            )
        else:
            new_object = new_frame
        return replace(
            self.worksheet.rdata,
            object=new_object,
        )

    def _frame_object(self) -> RObject:
        frame = self.worksheet.rdata.object
        if frame.info.type is RObjectType.LIST:
            frame = frame.value[0]
        return frame

    @staticmethod
    def _write_na(array: Any, position: int) -> None:  # noqa: ANN401
        if array.dtype == np.int32:
            array[position] = R_INT_NA
        else:
            array[position] = R_FLOAT_NA


@dataclass(frozen=True)
class DraftReconciliation:
    """Result of reconciling several original worksheet drafts."""

    accepted: bool
    issues: tuple[IntakeIssue, ...]
    field_sets: tuple[tuple[FieldChange, ...], ...]
    readable_results: tuple[RData, ...]


def reconcile_drafts(
    drafts: Sequence[tuple[Origin, Sequence[Correction], Origin | None]],
) -> DraftReconciliation:
    """
    Reconcile the first original drafts with the table and supplement.

    A joint release is only accepted when every draft requires exactly the
    same corrections and receives the same new delivery. Otherwise two
    complete, mutually compatible field sets are returned together with
    results that remain readable through the original library.
    """
    if not drafts:
        msg = "At least one draft is required for reconciliation"
        raise IntakeError(msg)

    signatures = {
        tuple(
            (
                correction.round,
                correction.ref,
                tuple(
                    (change.field, change.new_value)
                    for change in correction.changes
                ),
            )
            for correction in corrections
        )
        for _, corrections, _ in drafts
    }
    deliveries = {
        None
        if supplement is None
        else node_fingerprint(supplement.rdata.object)
        for _, _, supplement in drafts
    }

    issues: list[IntakeIssue] = []
    accepted = len(signatures) == 1 and len(deliveries) == 1
    if not accepted:
        issues.append(
            IntakeIssue(
                code="DRAFT_MISMATCH",
                message=(
                    "The drafts require different corrections or different "
                    "new deliveries; both compatible field sets are attached"
                ),
            ),
        )

    field_sets = tuple(
        tuple(
            change
            for correction in corrections
            for change in correction.changes
        )
        for _, corrections, _ in drafts
    )
    readable_results = tuple(origin.rdata for origin, _, _ in drafts)
    return DraftReconciliation(
        accepted=accepted,
        issues=tuple(issues),
        field_sets=field_sets,
        readable_results=readable_results,
    )


def accept(
    worksheet: Origin | bytes | str | os.PathLike[str],
    supplement: Origin | bytes | str | os.PathLike[str] | None = None,
    corrections: Iterable[Correction] = (),
    *,
    fee_schedule: Mapping[str, float] | None = None,
    open_tickets: Mapping[str, tuple[CellRef, StripSnapshot]] | None = None,
) -> IntakeResult:
    """
    Common acceptance entry point for a public worksheet and a supplement.

    Both slots accept either a parsed :class:`Origin`, raw document bytes or a
    file path. The kind of each document is verified before anything is
    admitted, so a slip attached to the wrong slot is reported as an issue
    instead of being persisted.

    Args:
        worksheet: Public worksheet (RDA) as an origin, bytes or path.
        supplement: Optional binary supplement (RDS), in the same shapes.
        corrections: Registered corrections in their approval rounds.
        fee_schedule: Fees of the additional fragments that may be requested.
        open_tickets: Tickets already open, mapped to the cell they read and
            the snapshot recorded when the ticket was opened.

    Returns:
        The :class:`IntakeResult` describing the decision.
    """
    worksheet_origin = _coerce_origin(worksheet, expected_kind="rda")
    supplement_origin = (
        None
        if supplement is None
        else _coerce_origin(supplement, expected_kind="rds")
    )
    intake = Intake(
        worksheet_origin,
        supplement_origin,
        fee_schedule=fee_schedule,
        open_tickets=open_tickets,
    )
    return intake.decide(corrections)


def _coerce_origin(
    document: Origin | bytes | str | os.PathLike[str],
    *,
    expected_kind: FileType,
) -> Origin:
    if isinstance(document, Origin):
        if document.file_type != expected_kind:
            msg = (
                f"Expected a {expected_kind} document but received a "
                f"{document.file_type} document"
            )
            raise AttachmentError(msg)
        return document
    if isinstance(document, bytes | bytearray | memoryview):
        return origin_from_bytes(
            bytes(document),
            expected_kind=expected_kind,
        )
    return origin_from_file(
        document,
        expected_kind=expected_kind,
    )
