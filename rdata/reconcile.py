"""Reconciliation of measurement tables stored in R files.

This module implements the reconciliation entry point used when an
experiment switches between two exports of the same measurement table
(for example, an old table with sensors in the first dimension and a new
one with time in the first dimension).  A handover sheet describes both
dimension orders and the common numbering of the axes, and optionally
restricts the comparison to one window.

Values are read exactly as the rest of :mod:`rdata` reads them: plain
vectors are indexed directly, while ``compact_intseq`` and
``compact_realseq`` ALTREP vectors are evaluated with the same
arithmetic that :func:`rdata.parser.parse_file` uses when expanding
them, but only at the positions required by the window.  This makes it
possible to reconcile windows of compact vectors with hundreds of
millions of points without ever expanding them.

Real values are compared by their actual floating-point bits (only
finite reals are accepted), so two exports of the same logical segment
are reported as different whenever the expansion rounds differently,
even if the values print identically.
"""
from __future__ import annotations

import abc
import enum
import math
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, TypeAlias, Union

import numpy as np
import numpy.typing as npt

from .parser._parser import (
    DEFAULT_ALTREP_MAP,
    RData,
    RObject,
    RObjectType,
    get_altrep_name,
    parse_file,
)

if TYPE_CHECKING:
    from .parser._parser import AcceptableFile, Traversable

__all__ = [
    "ArrayVectorView",
    "CompactIntSeqView",
    "CompactRealSeqView",
    "Difference",
    "DimensionWindow",
    "ReconcileReport",
    "SideLayout",
    "VectorKind",
    "VectorView",
    "column_major_strides",
    "reconcile",
    "vector_view",
]


class VectorKind(enum.Enum):
    """Kind of values stored in a vector."""

    INT = "int"
    REAL = "real"


class VectorView(abc.ABC):
    """
    Lazy view of a R numeric vector.

    Provides the values that the normal rdata expansion and conversion
    would produce, but computed only at the requested positions.
    """

    @property
    @abc.abstractmethod
    def kind(self) -> VectorKind:
        """Kind of values stored in the vector."""

    @property
    @abc.abstractmethod
    def length(self) -> int:
        """Number of elements of the expanded vector."""

    @abc.abstractmethod
    def values_at(
        self,
        positions: npt.NDArray[np.int64],
    ) -> npt.NDArray[Any]:
        """
        Values of the expanded vector at the given flat positions.

        Args:
            positions: Flat positions (0-based) in the expanded vector.

        Returns:
            Values at those positions, with the same dtype and bits as
            the expanded vector produced by rdata.
        """


class ArrayVectorView(VectorView):
    """
    View of a plain (already materialized) R vector.

    Accepts the arrays produced by the rdata conversion (``int32`` or
    ``float64`` arrays, possibly masked for integer NA).  Masked arrays
    are compared through their raw stored values, so two NA sentinels
    compare equal.  Arrays with more than one dimension (as produced by
    the conversion of R matrices) are flattened in column-major (R)
    order.
    """

    def __init__(self, data: npt.NDArray[Any] | np.ma.MaskedArray) -> None:
        """Build the view from a NumPy (possibly masked) array."""
        if isinstance(data, np.ma.MaskedArray):
            data = np.ma.getdata(data)
        array = np.ascontiguousarray(data)
        if array.ndim > 1:
            # R matrices are stored in column-major order
            array = array.ravel(order="F")

        if np.issubdtype(array.dtype, np.floating):
            self._kind = VectorKind.REAL
            array = array.astype(np.float64, copy=False)
        elif np.issubdtype(array.dtype, np.integer):
            self._kind = VectorKind.INT
        else:
            msg = f"Unsupported dtype for reconciliation: {array.dtype}"
            raise TypeError(msg)

        self._data: npt.NDArray[Any] = array

    @property
    def kind(self) -> VectorKind:
        """Kind of values stored in the vector."""
        return self._kind

    @property
    def length(self) -> int:
        """Number of elements of the expanded vector."""
        return int(self._data.shape[0])

    @property
    def data(self) -> npt.NDArray[Any]:
        """Underlying flat array."""
        return self._data

    def values_at(
        self,
        positions: npt.NDArray[np.int64],
    ) -> npt.NDArray[Any]:
        """Return the stored values at the given flat positions."""
        return self._data[positions]


class CompactIntSeqView(VectorView):
    """
    Lazy view of a ``compact_intseq`` ALTREP vector.

    The values match, element by element, the expansion performed by
    :func:`rdata.parser._parser.compact_intseq_constructor`, that is,
    ``np.array(range(start, stop + 1, step), dtype=np.int32)`` with
    ``stop = start + (n - 1) * step``.
    """

    def __init__(self, n: int, start: int, step: int) -> None:
        """Build the view from the compact sequence parameters."""
        stop = start + (n - 1) * step
        # The expansion in rdata uses range(start, stop + 1, step),
        # whose length may differ from n (e.g. for negative steps)
        self._start = start
        self._step = step
        self._length = len(range(start, stop + 1, step))

    @classmethod
    def from_state(cls, state: Any) -> CompactIntSeqView:  # noqa: ANN401
        """Build the view from the ALTREP state ``[n, start, step]``."""
        return cls(n=int(state[0]), start=int(state[1]), step=int(state[2]))

    @property
    def kind(self) -> VectorKind:
        """Kind of values stored in the vector."""
        return VectorKind.INT

    @property
    def length(self) -> int:
        """Number of elements of the expanded vector."""
        return self._length

    def values_at(
        self,
        positions: npt.NDArray[np.int64],
    ) -> npt.NDArray[np.int32]:
        """Return ``start + position * step`` as ``int32`` values."""
        positions = np.asarray(positions, dtype=np.int64)
        values = self._start + positions * self._step
        return values.astype(np.int32)


class CompactRealSeqView(VectorView):
    """
    Lazy view of a ``compact_realseq`` ALTREP vector.

    The values match, bit by bit, the expansion performed by
    :func:`rdata.parser._parser.compact_realseq_constructor`, that is,
    ``np.linspace(start, stop, n)`` with ``stop = start + (n - 1) * step``
    computed in floating-point arithmetic.  Only the requested positions
    are evaluated, so vectors of any length can be viewed.
    """

    def __init__(self, n: int, start: float, step: float) -> None:
        """Build the view from the compact sequence parameters."""
        # Same floating-point arithmetic as compact_seq_constructor
        self._length = n
        self._start = start
        self._stop = start + (n - 1) * step
        # Same computation as numpy.linspace(start, stop, n)
        self._delta = self._stop - start
        self._div = n - 1
        self._linstep = self._delta / self._div if self._div > 0 else math.nan

    @classmethod
    def from_state(cls, state: Any) -> CompactRealSeqView:  # noqa: ANN401
        """Build the view from the ALTREP state ``[n, start, step]``."""
        return cls(
            n=int(state[0]),
            start=float(state[1]),
            step=float(state[2]),
        )

    @property
    def kind(self) -> VectorKind:
        """Kind of values stored in the vector."""
        return VectorKind.REAL

    @property
    def length(self) -> int:
        """Number of elements of the expanded vector."""
        return self._length

    def values_at(
        self,
        positions: npt.NDArray[np.int64],
    ) -> npt.NDArray[np.float64]:
        """Return the ``numpy.linspace`` values at the given positions."""
        positions = np.asarray(positions, dtype=np.int64)
        if self._length == 0:
            return np.empty(0, dtype=np.float64)
        indices = positions.astype(np.float64)
        if self._div <= 0:
            # Single element sequence: linspace computes 0 * delta + start
            values = indices * self._delta + self._start
        elif self._linstep == 0:
            # Special handling of zero steps in numpy.linspace (gh-5437)
            values = indices / self._div * self._delta + self._start
        else:
            values = indices * self._linstep + self._start
        # numpy.linspace sets the last element to stop exactly
        return np.where(positions == self._length - 1, self._stop, values)


def _view_from_value(info_type: RObjectType, value: Any) -> VectorView:  # noqa: ANN401
    """Build a view from an expanded (info, value) pair."""
    if info_type in {RObjectType.INT, RObjectType.REAL}:
        return ArrayVectorView(np.asarray(np.ma.getdata(value)))
    msg = f"Cannot reconcile vectors of type {info_type}"
    raise TypeError(msg)


def _view_from_robject(obj: RObject) -> VectorView:
    """Build a view from a parsed R object."""
    obj_type = obj.info.type

    if obj_type is RObjectType.ALTREP:
        altrep_info, altrep_state, _ = obj.value
        altrep_name = get_altrep_name(altrep_info)

        if altrep_name == b"compact_intseq":
            return CompactIntSeqView.from_state(altrep_state.value)
        if altrep_name == b"compact_realseq":
            return CompactRealSeqView.from_state(altrep_state.value)
        if altrep_name in {
            b"wrap_real",
            b"wrap_integer",
            b"wrap_logical",
        }:
            return _view_from_robject(altrep_state.value[0])

        # Fall back to the expansion used by the parser
        new_info, value = DEFAULT_ALTREP_MAP[altrep_name](altrep_state)
        return _view_from_value(new_info.type, value)

    return _view_from_value(obj_type, obj.value)


VectorSource: TypeAlias = Union[
    VectorView,
    npt.NDArray[Any],
    np.ma.MaskedArray,
    RObject,
    RData,
    "AcceptableFile",
    os.PathLike[Any],
    str,
    "Traversable",
]


def vector_view(source: VectorSource) -> VectorView:
    """
    Build a lazy view of a R numeric vector.

    Args:
        source: One of the following:

            - a path or file-like object with the vector in RDS/RDA
              format (parsed without expanding ALTREP vectors, so
              compact sequences of any length are supported),
            - a parsed :class:`~rdata.parser.RData` or
              :class:`~rdata.parser.RObject` (expanded or not),
            - a NumPy (possibly masked) array with the converted values,
            - an already built :class:`VectorView`.

    Returns:
        A lazy view of the vector.
    """
    if isinstance(source, VectorView):
        return source
    if isinstance(source, RData):
        return _view_from_robject(source.object)
    if isinstance(source, RObject):
        return _view_from_robject(source)
    if isinstance(source, (np.ndarray, np.ma.MaskedArray)):
        return ArrayVectorView(source)

    parsed = parse_file(source, expand_altrep=False)
    return _view_from_robject(parsed.object)


class DimensionWindow(NamedTuple):
    """
    Window along one logical dimension of the handover sheet.

    Attributes:
        name: Name of the logical dimension in the handover sheet.
        start: First compared index, in the common numbering.
        size: Number of compared indices along this dimension.
        stride: Step between consecutive compared indices, in the
            common numbering.

    """

    name: str
    start: int
    size: int
    stride: int = 1


class SideLayout(NamedTuple):
    """
    Layout of one side of the reconciliation.

    Attributes:
        strides: Flat stride of the side's vector for each dimension of
            the handover sheet, in the same order.  For a R array these
            are the cumulative products of the preceding dimension
            sizes (see :func:`column_major_strides`), permuted to the
            order of the handover sheet.
        offset: Flat position of the common index 0 of every dimension
            in the side's vector.

    """

    strides: tuple[int, ...]
    offset: int = 0


def column_major_strides(
    dim_sizes: tuple[int, ...] | list[int],
) -> tuple[int, ...]:
    """
    Compute the flat strides of a R (column-major) array.

    Args:
        dim_sizes: Sizes of the dimensions in storage order.

    Returns:
        Flat stride of each dimension in storage order.

    Examples:
        >>> column_major_strides([4, 100])
        (1, 4)

    """
    strides = []
    accumulated = 1
    for size in dim_sizes:
        strides.append(accumulated)
        accumulated *= size
    return tuple(strides)


class Difference(NamedTuple):
    """
    Evidence of one differing cell.

    Attributes:
        coordinates: Index of the cell along each dimension of the
            handover sheet, in the common numbering.
        old_position: Flat position of the cell in the old vector.
        new_position: Flat position of the cell in the new vector.
        old_value: Value actually read from the old vector.
        new_value: Value actually read from the new vector.

    """

    coordinates: tuple[int, ...]
    old_position: int
    new_position: int
    old_value: float | int
    new_value: float | int


@dataclass(frozen=True)
class ReconcileReport:
    """
    Result of a reconciliation.

    Attributes:
        compared_cells: Number of compared cells.
        different_cells: Number of cells whose values differ.
        differences: Evidence of the first differing cells, in the
            dimension order of the handover sheet.

    """

    compared_cells: int
    different_cells: int
    differences: tuple[Difference, ...]

    @property
    def first_difference(self) -> Difference | None:
        """First differing cell in the order of the handover sheet."""
        return self.differences[0] if self.differences else None

    @property
    def reusable(self) -> bool:
        """Whether the old cells can be reused as they are."""
        return self.different_cells == 0


def _validate_dimensions(
    dimensions: list[DimensionWindow] | tuple[DimensionWindow, ...],
) -> tuple[DimensionWindow, ...]:
    """Validate the window dimensions of the handover sheet."""
    dimensions = tuple(dimensions)
    if not dimensions:
        msg = "At least one dimension is required"
        raise ValueError(msg)
    for dim in dimensions:
        if dim.start < 0 or dim.size < 0 or dim.stride < 1:
            msg = f"Invalid dimension window: {dim!r}"
            raise ValueError(msg)
    return dimensions


def _validate_layout(
    side: str,
    layout: SideLayout,
    n_dimensions: int,
) -> None:
    """Validate the layout of one side against the dimensions."""
    if len(layout.strides) != n_dimensions:
        msg = (
            f"The {side} layout has {len(layout.strides)} strides "
            f"for {n_dimensions} dimensions"
        )
        raise ValueError(msg)
    if layout.offset < 0 or any(s < 1 for s in layout.strides):
        msg = f"Invalid {side} layout: {layout!r}"
        raise ValueError(msg)


def _check_bounds(
    side: str,
    layout: SideLayout,
    view: VectorView,
    dimensions: tuple[DimensionWindow, ...],
) -> None:
    """Check that the window fits inside one side's vector."""
    # Positions are affine in the common indices, so the extremes are
    # reached at the window ends
    first = layout.offset + sum(
        dim.start * stride
        for dim, stride in zip(dimensions, layout.strides, strict=True)
    )
    last = layout.offset + sum(
        (dim.start + (dim.size - 1) * dim.stride) * stride
        for dim, stride in zip(dimensions, layout.strides, strict=True)
    )
    if first < 0 or last >= view.length:
        msg = (
            f"The window spans positions {first}..{last} of the "
            f"{side} vector, which has length {view.length}"
        )
        raise ValueError(msg)


def _flat_positions(
    linear_indices: npt.NDArray[np.int64],
    dimensions: tuple[DimensionWindow, ...],
    blocks: tuple[int, ...],
    layout: SideLayout,
) -> npt.NDArray[np.int64]:
    """Compute the flat positions of cells in one side's vector."""
    positions = np.full(linear_indices.shape, layout.offset, dtype=np.int64)
    for dim, block, stride in zip(
        dimensions,
        blocks,
        layout.strides,
        strict=True,
    ):
        coordinate = (linear_indices // block) % dim.size
        common_index = dim.start + coordinate * dim.stride
        positions += common_index * stride
    return positions


def _chunk_mismatch(
    view_old: VectorView,
    view_new: VectorView,
    positions_old: npt.NDArray[np.int64],
    positions_new: npt.NDArray[np.int64],
    *,
    is_real: bool,
) -> tuple[npt.NDArray[Any], npt.NDArray[Any], npt.NDArray[np.bool_]]:
    """Read and compare the values of both sides at the given positions."""
    values_old = view_old.values_at(positions_old)
    values_new = view_new.values_at(positions_new)

    mismatch: npt.NDArray[np.bool_]
    if is_real:
        if not (
            np.isfinite(values_old).all() and np.isfinite(values_new).all()
        ):
            msg = "Non-finite real values in the reconciled window"
            raise ValueError(msg)
        mismatch = values_old.view(np.uint64) != values_new.view(np.uint64)
    else:
        mismatch = values_old != values_new

    return values_old, values_new, mismatch


@dataclass(frozen=True)
class _ChunkData:
    """Data of the chunk where a differing cell was found."""

    dimensions: tuple[DimensionWindow, ...]
    blocks: tuple[int, ...]
    positions_old: npt.NDArray[np.int64]
    positions_new: npt.NDArray[np.int64]
    values_old: npt.NDArray[Any]
    values_new: npt.NDArray[Any]
    is_real: bool


def _build_difference(
    chunk: _ChunkData,
    linear_index: int,
    chunk_index: int,
) -> Difference:
    """Build the evidence of one differing cell."""
    coordinates = tuple(
        dim.start + ((linear_index // block) % dim.size) * dim.stride
        for dim, block in zip(chunk.dimensions, chunk.blocks, strict=True)
    )
    value_old: float | int
    value_new: float | int
    if chunk.is_real:
        value_old = float(chunk.values_old[chunk_index])
        value_new = float(chunk.values_new[chunk_index])
    else:
        value_old = int(chunk.values_old[chunk_index])
        value_new = int(chunk.values_new[chunk_index])
    return Difference(
        coordinates=coordinates,
        old_position=int(chunk.positions_old[chunk_index]),
        new_position=int(chunk.positions_new[chunk_index]),
        old_value=value_old,
        new_value=value_new,
    )


def reconcile(
    old: VectorSource,
    new: VectorSource,
    *,
    dimensions: list[DimensionWindow] | tuple[DimensionWindow, ...],
    old_layout: SideLayout,
    new_layout: SideLayout,
    max_differences: int | None = 1,
    chunk_size: int = 1 << 20,
) -> ReconcileReport:
    """
    Reconcile a window of two exports of the same measurement table.

    The cells of the window are traversed in the dimension order of the
    handover sheet, with the first dimension varying fastest (the R
    convention).  Real values are compared by their floating-point bits
    and integer values by exact equality; only finite real values are
    accepted.  Compact (ALTREP) vectors are evaluated only at the
    positions required by the window, so segments with hundreds of
    millions of points are never fully expanded.

    Args:
        old: Old export of the table (see :func:`vector_view`).
        new: New export of the table (see :func:`vector_view`).
        dimensions: Window of the comparison, one entry per logical
            dimension of the handover sheet, in its dimension order.
        old_layout: Layout of the old export.
        new_layout: Layout of the new export.
        max_differences: Maximum number of differing cells collected in
            the report (``None`` collects all of them).  The count of
            differing cells is always exact.
        chunk_size: Number of cells compared per chunk.  Memory usage is
            proportional to this value, not to the vector lengths.

    Returns:
        The reconciliation report.

    Examples:
        Reconcile a window of a long compact sequence against a short
        compact sequence that should start at that window:

        >>> old = CompactRealSeqView(n=6, start=0.0, step=0.5)
        >>> new = CompactRealSeqView(n=3, start=1.0, step=0.5)
        >>> report = reconcile(
        ...     old,
        ...     new,
        ...     dimensions=[DimensionWindow("time", start=0, size=3)],
        ...     old_layout=SideLayout(strides=(1,), offset=2),
        ...     new_layout=SideLayout(strides=(1,)),
        ... )
        >>> report.compared_cells, report.different_cells
        (3, 0)
        >>> report.reusable
        True

    """
    view_old = vector_view(old)
    view_new = vector_view(new)

    if view_old.kind is not view_new.kind:
        msg = (
            f"Cannot reconcile a {view_old.kind.value} vector against "
            f"a {view_new.kind.value} vector"
        )
        raise ValueError(msg)

    dimensions = _validate_dimensions(dimensions)
    _validate_layout("old", old_layout, len(dimensions))
    _validate_layout("new", new_layout, len(dimensions))

    if chunk_size < 1:
        msg = f"chunk_size must be positive, got {chunk_size}"
        raise ValueError(msg)
    if max_differences is not None and max_differences < 0:
        msg = f"max_differences must be non-negative, got {max_differences}"
        raise ValueError(msg)

    counts = tuple(dim.size for dim in dimensions)
    total = math.prod(counts)

    if total:
        _check_bounds("old", old_layout, view_old, dimensions)
        _check_bounds("new", new_layout, view_new, dimensions)

    blocks = tuple(math.prod(counts[:k]) for k in range(len(counts)))

    limit = math.inf if max_differences is None else max_differences
    differences: list[Difference] = []
    different_cells = 0
    is_real = view_old.kind is VectorKind.REAL

    for chunk_start in range(0, total, chunk_size):
        chunk_stop = min(chunk_start + chunk_size, total)
        linear = np.arange(chunk_start, chunk_stop, dtype=np.int64)

        positions_old = _flat_positions(linear, dimensions, blocks, old_layout)
        positions_new = _flat_positions(linear, dimensions, blocks, new_layout)

        values_old, values_new, mismatch = _chunk_mismatch(
            view_old,
            view_new,
            positions_old,
            positions_new,
            is_real=is_real,
        )

        different_cells += int(np.count_nonzero(mismatch))

        if len(differences) < limit:
            chunk = _ChunkData(
                dimensions=dimensions,
                blocks=blocks,
                positions_old=positions_old,
                positions_new=positions_new,
                values_old=values_old,
                values_new=values_new,
                is_real=is_real,
            )
            for index in np.flatnonzero(mismatch):
                if len(differences) >= limit:
                    break
                differences.append(_build_difference(
                    chunk,
                    linear_index=chunk_start + int(index),
                    chunk_index=int(index),
                ))

    return ReconcileReport(
        compared_cells=total,
        different_cells=different_cells,
        differences=tuple(differences),
    )
