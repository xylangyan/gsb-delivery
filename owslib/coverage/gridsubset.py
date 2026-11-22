# -*- coding: ISO-8859-15 -*-
# =============================================================================
# Copyright (c) 2026
#
# Authors:
#          OWSLib contributors
#
# Contact email: owslib@lists.osgeo.org
# =============================================================================

"""Plan WCS GetCoverage index ranges from a coverage grid description.

A WCS 2.0 coverage description describes its storage grid with GML ``Grid``,
``RectifiedGrid`` or ``ReferenceableGridByVectors``.  A GetCoverage request is
normally expressed in request axes (longitude, latitude and time), while the
storage layout may use a different axis order, a negative step, a rotated grid,
and an explicitly listed non-uniform time axis.

:func:`plan_grid_subset` maps such a request onto

* inclusive physical (storage) index ranges,
* the same ranges expressed in request-axis order,
* the true position of every selected cell,
* separate read and local-clip ranges for axes the service cannot subset.

Coverage is never invented.  A cell that is only partly inside the requested
rectangle, or outside it, is marked as such (``partial``/``outside``) and is
not ``proven``.  ``GridSubset.fill_value`` is ``None``; callers must carry that
state through and must not substitute zero.
"""

from collections import OrderedDict
from collections.abc import Sequence
from datetime import datetime, timedelta
from itertools import product
from math import ceil, floor

import dateutil.parser as parser

__all__ = [
    "GridSubsetError",
    "IndexRange",
    "ClipRange",
    "AxisRangeMap",
    "AxisSelection",
    "Cell",
    "GridSubset",
    "plan_grid_subset",
    "plan_grid_batches",
    "plan_subset_from_subsets",
]


TIME_AXIS_ALIASES = frozenset(
    ["ansi", "unix", "time", "t", "date", "datetime", "timeposition", "time_position"]
)
X_AXIS_ALIASES = frozenset(["x", "lon", "long", "longitude", "east", "easting", "e"])
Y_AXIS_ALIASES = frozenset(["y", "lat", "latitude", "north", "northing", "n"])

_AREA_EPS = 1e-12
_INDEX_EPS = 1e-10


class GridSubsetError(ValueError):
    """Raised when a grid subset cannot be planned from the grid definition."""


def _norm(name):
    return str(name).strip().strip('"').strip("'").casefold()


def _is_number(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _number(value):
    try:
        return float(str(value).strip().strip('"').strip("'"))
    except (TypeError, ValueError) as err:
        raise GridSubsetError("Expected a numeric grid ordinate, got %r" % (value,)) from err


def _as_datetime(value):
    """Parse an ISO time or ANSI day number into a naive datetime."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().strip('"').strip("'")
        if _is_number(text):
            return datetime(1600, 12, 31) + timedelta(days=float(text))
        try:
            parsed = parser.parse(text)
        except (ValueError, TypeError) as err:
            raise GridSubsetError("Cannot parse time value %r" % (value,)) from err
    if parsed.tzinfo is not None:
        # Keep the wall-clock value; WCS time axes are normally published in
        # UTC and OWSLib's other time helpers also return naive datetimes.
        parsed = parsed.replace(tzinfo=None)
    return parsed


def _as_tolerance(value):
    if value is None:
        return None
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=float(value))
    raise GridSubsetError("time_tolerance must be a number of seconds or a timedelta")


class IndexRange(Sequence):
    """Inclusive native index range ``low..high``.

    Behaves like ``(low, high)`` for unpacking and comparison, and also exposes
    ``start``/``stop`` and ``count``.  ``IndexRange.empty().count == 0``.
    """

    __slots__ = ("low", "high")

    def __init__(self, low, high):
        self.low = int(low)
        self.high = int(high)

    @classmethod
    def empty(cls):
        return cls(0, -1)

    @property
    def start(self):
        return self.low

    @property
    def stop(self):
        return self.high

    @property
    def count(self):
        return max(0, self.high - self.low + 1)

    @property
    def is_empty(self):
        return self.count == 0

    @property
    def slice(self):
        """Python slice selecting this inclusive range (``stop`` exclusive)."""
        return slice(self.low, self.high + 1)

    def indices(self):
        return tuple(range(self.low, self.high + 1))

    def __iter__(self):
        return iter((self.low, self.high))

    def __len__(self):
        return 2

    def __getitem__(self, item):
        return (self.low, self.high)[item]

    def __eq__(self, other):
        if isinstance(other, IndexRange):
            return (self.low, self.high) == (other.low, other.high)
        if isinstance(other, (tuple, list)) and len(other) == 2:
            return (self.low, self.high) == (int(other[0]), int(other[1]))
        return NotImplemented

    def __hash__(self):
        return hash((self.low, self.high))

    def __repr__(self):
        return "IndexRange(%d, %d)" % (self.low, self.high)

    def __str__(self):
        return "(%d, %d)" % (self.low, self.high)

    def as_tuple(self):
        return (self.low, self.high)


class ClipRange(Sequence):
    """Zero-based slice into the array that was actually read.

    ``stop`` is exclusive, so ``array[start:stop]`` is directly usable.  When
    the wanted cells are not contiguous inside the read block (for example one
    instant on a fully read time axis), ``indices`` lists the exact wanted
    offsets and ``gaps`` is true; callers must then select ``indices`` rather
    than the whole slice.
    """

    __slots__ = ("start", "stop", "indices")

    def __init__(self, start, stop, indices=None):
        self.start = int(start)
        self.stop = int(stop)
        if indices is None:
            indices = tuple(range(self.start, self.stop))
        self.indices = tuple(indices)

    @property
    def count(self):
        return max(0, self.stop - self.start)

    @property
    def slice(self):
        return slice(self.start, self.stop)

    @property
    def gaps(self):
        return len(self.indices) != self.count

    @property
    def contiguous(self):
        return not self.gaps

    def __iter__(self):
        return iter((self.start, self.stop))

    def __len__(self):
        return 2

    def __getitem__(self, item):
        return (self.start, self.stop)[item]

    def __eq__(self, other):
        if isinstance(other, ClipRange):
            return (self.start, self.stop) == (other.start, other.stop)
        if isinstance(other, (tuple, list)) and len(other) == 2:
            return (self.start, self.stop) == (int(other[0]), int(other[1]))
        return NotImplemented

    def __hash__(self):
        return hash((self.start, self.stop))

    def __repr__(self):
        return "ClipRange(%d, %d)" % (self.start, self.stop)

    def __str__(self):
        return "(%d, %d)" % (self.start, self.stop)

    def as_tuple(self):
        return (self.start, self.stop)


class AxisRangeMap(OrderedDict):
    """Mapping axis name -> range whose order is storage-axis order."""

    def __eq__(self, other):
        if isinstance(other, dict):
            return dict(self) == dict(other)
        if isinstance(other, (tuple, list)):
            values = list(self.values())
            if len(values) != len(other):
                return False
            for value, item in zip(values, other):
                if (
                    isinstance(item, (tuple, list))
                    and len(item) == 2
                    and isinstance(item[0], str)
                ):
                    if str(value) != str(item):
                        return False
                elif value != item:
                    return False
            return True
        return NotImplemented

    def __hash__(self):
        return None

    def as_list(self):
        return list(self.values())

    def as_pairs(self):
        return list(self.items())


class _FixedSequence(Sequence):
    __slots__ = ("_items",)

    def __init__(self, items):
        self._items = tuple(items)

    def __len__(self):
        return len(self._items)

    def __getitem__(self, item):
        return self._items[item]

    def __iter__(self):
        return iter(self._items)

    def __eq__(self, other):
        if isinstance(other, _FixedSequence):
            return self._items == other._items
        if isinstance(other, Sequence):
            return self._items == tuple(other)
        return NotImplemented

    def __hash__(self):
        return hash(self._items)

    def as_tuple(self):
        return self._items

    def __repr__(self):
        return "AxisValues(%r)" % (self._items,)


class _SelectedPositions(Sequence):
    """Positions of the selected native indices, in the order selected."""

    __slots__ = ("_all", "_low", "_indices")

    def __init__(self, all_positions, indices, low=0):
        self._all = all_positions
        self._low = int(low)
        self._indices = tuple(indices)

    def __len__(self):
        return len(self._indices)

    def __getitem__(self, item):
        if isinstance(item, slice):
            return tuple(self._all[i - self._low] for i in self._indices[item])
        return self._all[self._indices[item] - self._low]

    def __iter__(self):
        for index in self._indices:
            yield self._all[index - self._low]

    def __eq__(self, other):
        if isinstance(other, (Sequence, _SelectedPositions)):
            return tuple(self) == tuple(other)
        return NotImplemented

    def as_tuple(self):
        return tuple(self)

    def __repr__(self):
        return "SelectedPositions(%r)" % (tuple(self),)


class AxisSelection:
    """Selection of one physical axis.

    ``indices`` are the native indices the request wants.  ``read_indices`` are
    the indices actually requested; they differ only when the service cannot
    subset this axis.  ``clip_indices`` then gives the zero-based positions of
    the wanted cells inside the read array, and ``clip_range`` their bounding
    slice.
    """

    __slots__ = (
        "name",
        "request_name",
        "request_axis",
        "kind",
        "native",
        "indices",
        "read_indices",
        "clip_indices",
        "irregular",
        "supported",
        "positions",
        "read_positions",
        "_all_positions",
    )

    def __init__(
        self,
        name,
        native,
        indices,
        all_positions,
        *,
        request_name=None,
        request_axis=None,
        kind="other",
        irregular=False,
        supported=True,
        read_indices=None,
    ):
        self.name = name
        self.request_name = request_name or name
        self.request_axis = request_axis or self.request_name
        self.kind = kind
        self.native = IndexRange(native[0], native[1])
        self.indices = _FixedSequence(indices)
        self.irregular = bool(irregular)
        self.supported = bool(supported)
        self._all_positions = all_positions
        if read_indices is None:
            read_indices = indices
        self.read_indices = _FixedSequence(read_indices)
        self.positions = _SelectedPositions(all_positions, indices, self.native.low)
        self.read_positions = _SelectedPositions(
            all_positions, read_indices, self.native.low
        )

        read_low = min(read_indices) if len(read_indices) else 0
        self.clip_indices = tuple(i - read_low for i in indices)

    @property
    def index_range(self):
        if not len(self.indices):
            return IndexRange.empty()
        return IndexRange(min(self.indices), max(self.indices))

    @property
    def read_range(self):
        if not len(self.read_indices):
            return IndexRange.empty()
        return IndexRange(min(self.read_indices), max(self.read_indices))

    @property
    def count(self):
        return len(self.indices)

    @property
    def read_count(self):
        return len(self.read_indices)

    @property
    def extra_read(self):
        return self.read_count - self.count

    @property
    def contiguous(self):
        values = tuple(self.indices)
        return not values or values == tuple(range(values[0], values[-1] + 1))

    @property
    def extra_indices(self):
        wanted = frozenset(self.indices)
        return tuple(i for i in self.read_indices if i not in wanted)

    @property
    def clip_index_range(self):
        """Inclusive native index range actually needed after the local clip."""
        if not self.clip_indices:
            return IndexRange.empty()
        return IndexRange(min(self.clip_indices), max(self.clip_indices))

    @property
    def clip_range(self):
        if not self.clip_indices:
            return ClipRange(0, 0, ())
        return ClipRange(
            min(self.clip_indices),
            max(self.clip_indices) + 1,
            self.clip_indices,
        )

    @property
    def clip_slice(self):
        return self.clip_range.slice

    @property
    def needs_post_mask(self):
        """True when ``clip_range`` contains cells that were not requested."""
        return len(self.clip_indices) != self.clip_range.count

    def index_of(self, value):
        return tuple(self.indices).index(value)

    def __getitem__(self, item):
        return self.indices[item]

    def __iter__(self):
        return iter(self.indices)

    def __len__(self):
        return len(self.indices)

    def __repr__(self):
        return (
            "AxisSelection(%r, indices=%r, native=%r, supported=%r)"
            % (self.name, self.indices, self.native, self.supported)
        )

    def to_dict(self):
        return {
            "name": self.name,
            "request_name": self.request_name,
            "kind": self.kind,
            "native": self.native.as_tuple(),
            "indices": self.indices.as_tuple(),
            "index_range": self.index_range.as_tuple(),
            "read_indices": self.read_indices.as_tuple(),
            "read_range": self.read_range.as_tuple(),
            "clip_indices": self.clip_indices,
            "clip_range": self.clip_range.as_tuple(),
            "clip_range_indices": self.clip_range.indices,
            "clip_range_gaps": self.clip_range.gaps,
            "supported": self.supported,
            "irregular": self.irregular,
            "positions": self.positions.as_tuple(),
            "read_positions": self.read_positions.as_tuple(),
        }


class Cell:
    """One storage cell of the selected block and its true position.

    ``storage_index`` follows the grid's storage-axis order, while
    ``request_index`` follows the request order.  ``center`` maps every axis
    name to its ordinate and ``xy`` holds the true CRS position computed from
    the full two-dimensional transform (so rotated grids are exact).

    ``coverage`` is ``"full"`` when the cell lies wholly inside the requested
    rectangle, ``"partial"`` when it only intersects it, and ``"outside"`` for
    a bounding cell of the requested storage block that does not intersect the
    rectangle at all.  Only ``"full"`` cells are ``proven``.  No value is
    attached to a cell; the caller must treat non-proven cells as unknown.
    """

    __slots__ = (
        "storage_index",
        "request_index",
        "index",
        "center",
        "request_position",
        "xy",
        "time",
        "polygon",
        "coverage_fraction",
        "coverage",
        "proven",
    )

    def __init__(
        self,
        storage_index,
        request_index,
        index,
        center,
        request_position,
        *,
        xy=None,
        time=None,
        polygon=(),
        coverage_fraction=1.0,
        coverage="full",
    ):
        self.storage_index = storage_index
        self.request_index = request_index
        self.index = index
        self.center = center
        self.request_position = request_position
        self.xy = xy
        self.time = time
        self.polygon = tuple(polygon)
        self.coverage_fraction = float(coverage_fraction)
        self.coverage = coverage
        self.proven = coverage == "full"

    def __repr__(self):
        return "Cell(storage_index=%r, xy=%r, time=%r, coverage=%r)" % (
            self.storage_index,
            self.xy,
            self.time,
            self.coverage,
        )

    @property
    def coverage_kind(self):
        """``full``, ``partial`` or ``outside`` for this cell."""
        return self.coverage

    @property
    def is_fully_covered(self):
        return self.coverage == "full"

    @property
    def value(self):
        """Cell value placeholder; unproven coverage has no value."""
        return None

    def to_dict(self):
        return {
            "storage_index": self.storage_index,
            "request_index": self.request_index,
            "index": dict(self.index),
            "center": dict(self.center),
            "request_position": self.request_position,
            "xy": self.xy,
            "time": self.time,
            "polygon": self.polygon,
            "coverage_fraction": self.coverage_fraction,
            "coverage": self.coverage,
            "proven": self.proven,
        }


class _CellSequence(Sequence):
    """Lazy Cartesian product of selected axes, in storage-axis order."""

    __slots__ = ("_plan",)

    def __init__(self, plan):
        self._plan = plan

    def __len__(self):
        total = 1
        for axis in self._plan.axes.values():
            total *= len(axis)
        return total

    def __iter__(self):
        names = tuple(self._plan.axes)
        axes = [tuple(self._plan.axes[name]) for name in names]
        for storage_index in product(*axes):
            yield self._plan._make_cell(storage_index)

    def __getitem__(self, item):
        total = len(self)
        if isinstance(item, slice):
            start, stop, step = item.indices(total)
            return tuple(self[i] for i in range(start, stop, step))
        if item < 0:
            item += total
        if item < 0 or item >= total:
            raise IndexError(item)
        names = tuple(self._plan.axes)
        storage_index = []
        for pos in range(len(names) - 1, -1, -1):
            axis = self._plan.axes[names[pos]]
            count = len(axis)
            storage_index.append(axis[item % count])
            item //= count
        storage_index.reverse()
        return self._plan._make_cell(tuple(storage_index))

    def __eq__(self, other):
        if isinstance(other, Sequence):
            return tuple(self) == tuple(other)
        return NotImplemented

    def __repr__(self):
        return "Cells(count=%d)" % len(self)


class GridSubset:
    """Result of :func:`plan_grid_subset`.

    The primary ranges are storage-ordered.  Request-ordered counterparts and
    helpers for converting between the two orders are also provided.
    """

    def __init__(
        self,
        grid,
        axes,
        request_axes,
        spatial_axes,
        bbox,
        coverage_by_cell,
        unmatched_times=(),
        outside_bbox=None,
        time_axis=None,
    ):
        self.grid = grid
        self.axes = OrderedDict(axes)
        self.request_axes = tuple(request_axes)
        self.spatial_axes = tuple(spatial_axes)
        self.bbox = bbox
        self.unmatched_times = tuple(unmatched_times)
        self.outside_bbox = outside_bbox
        self.time_axis = time_axis

        self._coverage_by_cell = coverage_by_cell
        self._cells = _CellSequence(self)

    def __getitem__(self, name):
        return self.axis(name)

    def __iter__(self):
        return iter(self.axes.values())

    def __len__(self):
        return len(self.axes)

    def axis(self, name):
        """Return an :class:`AxisSelection` by storage name or request name."""
        wanted = _norm(name)
        for axis_name, axis in self.axes.items():
            if _norm(axis_name) == wanted or _norm(axis.request_name) == wanted:
                return axis
        if wanted == "time" and self.time_axis is not None:
            return self.axes[self.time_axis]
        if len(self.spatial_axes) >= 2 and wanted in ("x", "lon", "long", "longitude"):
            return self.axes[self.spatial_axes[0]]
        if len(self.spatial_axes) >= 2 and wanted in ("y", "lat", "latitude"):
            return self.axes[self.spatial_axes[1]]
        raise KeyError("No axis named %s" % name)

    @property
    def index_ranges(self):
        """Inclusive native index ranges in storage-axis order."""
        ranges = AxisRangeMap()
        for name, axis in self.axes.items():
            ranges[name] = axis.index_range
        return ranges

    @property
    def read_ranges(self):
        """Requested inclusive ranges (whole axis where subsetting is absent)."""
        ranges = AxisRangeMap()
        for name, axis in self.axes.items():
            ranges[name] = axis.read_range
        return ranges

    @property
    def clip_ranges(self):
        """Local zero-based slice ranges into each read array."""
        ranges = AxisRangeMap()
        for name, axis in self.axes.items():
            ranges[name] = axis.clip_range
        return ranges

    @property
    def positions(self):
        positions = AxisRangeMap()
        for name, axis in self.axes.items():
            positions[name] = axis.positions
        return positions

    @property
    def request_index_ranges(self):
        """The same inclusive ranges, listed in request-axis order."""
        return tuple(self.axes[name].index_range for name in self.request_axes)

    @property
    def request_read_ranges(self):
        return tuple(self.axes[name].read_range for name in self.request_axes)

    @property
    def request_clip_ranges(self):
        return tuple(self.axes[name].clip_range for name in self.request_axes)

    def request_index_map(self):
        """Ordered mapping request name -> inclusive range, in request order."""
        result = AxisRangeMap()
        for name in self.request_axes:
            result[self.axes[name].request_name] = self.axes[name].index_range
        return result

    def request_read_map(self):
        result = AxisRangeMap()
        for name in self.request_axes:
            result[self.axes[name].request_name] = self.axes[name].read_range
        return result

    def request_clip_map(self):
        result = AxisRangeMap()
        for name in self.request_axes:
            result[self.axes[name].request_name] = self.axes[name].clip_range
        return result

    @property
    def index_range_by_request_axis(self):
        """Alias for :attr:`request_index_ranges`, listed in request order."""
        return self.request_index_ranges

    @property
    def read_range_by_request_axis(self):
        return self.request_read_ranges

    @property
    def clip_range_by_request_axis(self):
        return self.request_clip_ranges

    @property
    def unsupported_axes(self):
        return tuple(name for name, axis in self.axes.items() if not axis.supported)

    @property
    def needs_local_clip(self):
        return any(axis.extra_read > 0 for axis in self.axes.values())

    @property
    def cells(self):
        return self._cells

    @property
    def fill_value(self):
        """The value used for cells without proven coverage: unknown."""
        return None

    @property
    def coverage_mask(self):
        """Storage-indexed spatial coverage for cells that are not fully covered.

        The mapping is sparse: a missing key means the cell lies wholly inside
        the requested rectangle.  Every present key carries ``(fraction,
        "partial"|"outside")`` and must be treated as unknown, not zero.
        """
        return dict(self._coverage_by_cell)

    @property
    def proven(self):
        """True when every selected spatial cell is fully inside the request.

        The mask is sparse: only cells that are not fully covered are recorded,
        so an empty mask means every selected cell is proven.
        """
        return not self._coverage_by_cell

    @property
    def unproven_cells(self):
        """Spatial storage indices whose coverage is not proven."""
        return tuple(
            key
            for key, (_, kind) in self._coverage_by_cell.items()
            if kind != "full"
        )

    @property
    def ranges(self):
        """Alias for :attr:`index_ranges` (storage order)."""
        return self.index_ranges

    @property
    def axis_order(self):
        return tuple(self.axes)

    @property
    def request_axis_order(self):
        return self.request_axes

    @property
    def unsupported_read_ranges(self):
        """Mapping of unsupported axes to what must be read and clipped."""
        ranges = OrderedDict()
        for name, axis in self.axes.items():
            if not axis.supported:
                ranges[name] = {
                    "read": axis.read_range,
                    "read_slice": axis.read_range.slice,
                    "clip": axis.clip_range,
                    "clip_indices": axis.clip_indices,
                }
        return ranges

    @property
    def extra_read_count(self):
        return sum(axis.extra_read for axis in self.axes.values())

    def storage_index_of(self, request_index):
        """Convert a request-order index tuple to storage order.

        ``request_index`` may be a sequence in ``request_axes`` order or a
        mapping keyed by axis name.  An empty sequence of the right length is
        accepted and returns an empty tuple (useful for empty selections).
        """
        if isinstance(request_index, dict):
            by_name = {_norm(key): value for key, value in request_index.items()}
            values = []
            for name in self.axes:
                axis = self.axes[name]
                key = _norm(axis.name)
                request_key = _norm(axis.request_name)
                if key in by_name:
                    values.append(by_name[key])
                elif request_key in by_name:
                    values.append(by_name[request_key])
                else:
                    raise KeyError("Missing index for axis %s" % axis.name)
            return tuple(values)
        values = tuple(request_index)
        if len(values) != len(self.request_axes):
            raise ValueError(
                "Expected %d request indices, got %d"
                % (len(self.request_axes), len(values))
            )
        request_values = dict(zip(self.request_axes, values))
        return tuple(request_values[name] for name in self.axes)

    def request_index_of(self, storage_index):
        """Convert a storage-order index tuple to request order."""
        if isinstance(storage_index, dict):
            by_name = {_norm(key): value for key, value in storage_index.items()}
            values = []
            for name in self.request_axes:
                axis = self.axes[name]
                key = _norm(axis.name)
                if key not in by_name:
                    raise KeyError("Missing index for axis %s" % axis.name)
                values.append(by_name[key])
            return tuple(values)
        values = tuple(storage_index)
        if len(values) != len(self.axes):
            raise ValueError(
                "Expected %d storage indices, got %d" % (len(self.axes), len(values))
            )
        storage_values = dict(zip(self.axes, values))
        return tuple(storage_values[name] for name in self.request_axes)

    def cell_at(self, request_index):
        """Return the :class:`Cell` at a request-order index tuple."""
        return self._make_cell(self.storage_index_of(request_index))

    def _make_cell(self, storage_index):
        index = OrderedDict(
            (name, storage_index[pos]) for pos, name in enumerate(self.axes)
        )
        center = OrderedDict(
            (
                name,
                self.axes[name]._all_positions[index[name] - self.axes[name].native.low],
            )
            for name in self.axes
        )

        spatial_index = ()
        xy = None
        polygon = ()
        coverage_fraction = 1.0
        coverage = "full"
        if len(self.spatial_axes) >= 2:
            x_name, y_name = self.spatial_axes[0], self.spatial_axes[1]
            spatial_index = (index[x_name], index[y_name])
            geometry = self.grid._subset_geometry
            xy = geometry.cell_center(index[x_name], index[y_name])
            polygon = geometry.cell_polygon(index[x_name], index[y_name])
            # A rotated grid makes each coordinate depend on both indices;
            # report the true position rather than a single-axis trace.
            center[x_name] = xy[0]
            center[y_name] = xy[1]
            coverage_fraction, coverage = self._coverage_by_cell.get(
                spatial_index, (1.0, "full")
            )
            if self.bbox is None:
                coverage_fraction, coverage = 1.0, "full"

        time_value = None
        if self.time_axis is not None:
            time_value = center[self.time_axis]

        request_position = tuple(
            center[name] for name in self.request_axes
        )
        request_index = self.request_index_of(storage_index)
        return Cell(
            storage_index=tuple(storage_index),
            request_index=request_index,
            index=index,
            center=center,
            request_position=request_position,
            xy=xy,
            time=time_value,
            polygon=polygon,
            coverage_fraction=coverage_fraction,
            coverage=coverage,
        )

    def to_dict(self):
        return {
            "storage_axes": tuple(self.axes),
            "request_axes": self.request_axes,
            "spatial_axes": self.spatial_axes,
            "time_axis": self.time_axis,
            "bbox": self.bbox,
            "index_ranges": {
                name: axis.index_range.as_tuple() for name, axis in self.axes.items()
            },
            "read_ranges": {
                name: axis.read_range.as_tuple() for name, axis in self.axes.items()
            },
            "clip_ranges": {
                name: axis.clip_range.as_tuple() for name, axis in self.axes.items()
            },
            "request_index_ranges": tuple(
                r.as_tuple() for r in self.request_index_ranges
            ),
            "request_read_ranges": tuple(r.as_tuple() for r in self.request_read_ranges),
            "request_clip_ranges": tuple(r.as_tuple() for r in self.request_clip_ranges),
            "unsupported_axes": self.unsupported_axes,
            "unmatched_times": self.unmatched_times,
            "outside_bbox": self.outside_bbox,
            "axes": OrderedDict(
                (name, axis.to_dict()) for name, axis in self.axes.items()
            ),
        }

    def __repr__(self):
        return "GridSubset(storage_axes=%r, request_axes=%r, ranges=%r)" % (
            tuple(self.axes),
            self.request_axes,
            self.index_ranges.as_pairs(),
        )


class _GridGeometry:
    """Two-dimensional cell geometry for the grid's spatial axes.

    Grid ordinates and offset vectors are in the grid's own component order,
    which normally follows the axis labels (for example ``Lat Long`` stores
    latitude first).  ``x_component``/``y_component`` record which component
    carries CRS x and y so the full transform stays correct for rotated grids.
    """

    __slots__ = (
        "origin",
        "x_name",
        "y_name",
        "x_offset",
        "y_offset",
        "det",
        "native_x",
        "native_y",
        "x_component",
        "y_component",
    )

    def __init__(
        self,
        origin,
        offsets,
        x_pos,
        y_pos,
        x_name,
        y_name,
        native_x,
        native_y,
    ):
        if x_pos >= len(origin) or y_pos >= len(origin):
            raise GridSubsetError(
                "Spatial axes have no matching origin ordinates"
            )
        x_component, y_component = _component_assignment(offsets, x_pos, y_pos)
        self.x_component = x_component
        self.y_component = y_component
        self.origin = (float(origin[x_component]), float(origin[y_component]))
        self.x_name = x_name
        self.y_name = y_name
        self.x_offset = (
            float(offsets[x_pos][x_component]),
            float(offsets[x_pos][y_component]),
        )
        self.y_offset = (
            float(offsets[y_pos][x_component]),
            float(offsets[y_pos][y_component]),
        )
        self.native_x = native_x
        self.native_y = native_y
        self.det = (
            self.x_offset[0] * self.y_offset[1]
            - self.x_offset[1] * self.y_offset[0]
        )
        if abs(self.det) <= _AREA_EPS:
            raise GridSubsetError(
                "Spatial offset vectors are singular: %r and %r"
                % (self.x_offset, self.y_offset)
            )

    def index_to_xy(self, u, v):
        """Map continuous grid coordinates (cell edges) to CRS x/y."""
        x = self.origin[0] + u * self.x_offset[0] + v * self.y_offset[0]
        y = self.origin[1] + u * self.x_offset[1] + v * self.y_offset[1]
        return (x, y)

    def xy_to_index(self, x, y):
        """Map a CRS x/y point to continuous grid coordinates."""
        dx = x - self.origin[0]
        dy = y - self.origin[1]
        u = (dx * self.y_offset[1] - dy * self.y_offset[0]) / self.det
        v = (dy * self.x_offset[0] - dx * self.x_offset[1]) / self.det
        return (u, v)

    def cell_center(self, i, j):
        return self.index_to_xy(i + 0.5, j + 0.5)

    def cell_polygon(self, i, j):
        return (
            self.index_to_xy(i, j),
            self.index_to_xy(i + 1, j),
            self.index_to_xy(i + 1, j + 1),
            self.index_to_xy(i, j + 1),
        )

    def cell_area(self):
        return abs(self.det)


class _IdentityGridGeometry:
    """Index-space fallback for a plain gml:Grid without coordinates."""

    __slots__ = ("x_name", "y_name", "native_x", "native_y")

    def __init__(self, x_name, y_name, native_x, native_y):
        self.x_name = x_name
        self.y_name = y_name
        self.native_x = native_x
        self.native_y = native_y

    def index_to_xy(self, u, v):
        return (u, v)

    def xy_to_index(self, x, y):
        return (x, y)

    def cell_center(self, i, j):
        return (i + 0.5, j + 0.5)

    def cell_polygon(self, i, j):
        return (
            (i, j),
            (i + 1, j),
            (i + 1, j + 1),
            (i, j + 1),
        )

    def cell_area(self):
        return 1.0


def _clip_polygon(polygon, rect):
    """Sutherland-Hodgman clip of a convex polygon to an axis-aligned rect."""

    def clip_edge(points, inside, intersect):
        if not points:
            return []
        result = []
        previous = points[-1]
        previous_inside = inside(previous)
        for current in points:
            current_inside = inside(current)
            if current_inside:
                if not previous_inside:
                    result.append(intersect(previous, current))
                result.append(current)
            elif previous_inside:
                result.append(intersect(previous, current))
            previous = current
            previous_inside = current_inside
        return result

    minx, miny, maxx, maxy = rect

    def intersect_x(p1, p2, x):
        if p2[0] == p1[0]:
            return (x, p1[1])
        t = (x - p1[0]) / (p2[0] - p1[0])
        return (x, p1[1] + t * (p2[1] - p1[1]))

    def intersect_y(p1, p2, y):
        if p2[1] == p1[1]:
            return (p1[0], y)
        t = (y - p1[1]) / (p2[1] - p1[1])
        return (p1[0] + t * (p2[0] - p1[0]), y)

    points = list(polygon)
    points = clip_edge(
        points, lambda p: p[0] >= minx, lambda a, b: intersect_x(a, b, minx)
    )
    points = clip_edge(
        points, lambda p: p[0] <= maxx, lambda a, b: intersect_x(a, b, maxx)
    )
    points = clip_edge(
        points, lambda p: p[1] >= miny, lambda a, b: intersect_y(a, b, miny)
    )
    points = clip_edge(
        points, lambda p: p[1] <= maxy, lambda a, b: intersect_y(a, b, maxy)
    )
    return points


def _polygon_area(points):
    if len(points) < 3:
        return 0.0
    area = 0.0
    for pos, point in enumerate(points):
        nxt = points[(pos + 1) % len(points)]
        area += point[0] * nxt[1] - nxt[0] * point[1]
    return abs(area) / 2.0


def _bbox_pairs(bbox, x_name, y_name):
    if bbox is None:
        return None
    if isinstance(bbox, dict):
        pairs = []
        for name, bounds in bbox.items():
            if len(bounds) != 2:
                raise GridSubsetError("Named bbox %s must have two bounds" % name)
            pairs.append((name, float(bounds[0]), float(bounds[1])))
        if len(pairs) != 2:
            raise GridSubsetError("A bbox must name exactly two spatial axes")
        return pairs
    if (
        isinstance(bbox, (list, tuple))
        and len(bbox) == 2
        and all(isinstance(item, (list, tuple)) and len(item) == 2 for item in bbox)
    ):
        bbox = (bbox[0][0], bbox[0][1], bbox[1][0], bbox[1][1])
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise GridSubsetError(
            "bbox must be (minx, miny, maxx, maxy) or a named mapping"
        )
    minx, miny, maxx, maxy = (float(value) for value in bbox)
    if minx > maxx:
        minx, maxx = maxx, minx
    if miny > maxy:
        miny, maxy = maxy, miny
    if minx == maxx or miny == maxy:
        raise GridSubsetError("A spatial bbox must have a non-zero area")
    return [(x_name, minx, maxx), (y_name, miny, maxy)]


def _find_axis(labels, aliases):
    for pos, label in enumerate(labels):
        if _norm(label) in aliases:
            return pos
    return None


def _resolve_axis_name(labels, value):
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0 or value >= len(labels):
            raise GridSubsetError("Axis position %r is outside the grid" % value)
        return value
    wanted = _norm(value)
    for pos, label in enumerate(labels):
        if _norm(label) == wanted:
            return pos
    raise GridSubsetError("No grid axis named %r (axes: %r)" % (value, labels))


def _component_assignment(offsets, x_pos, y_pos):
    """Choose which offset-vector component carries CRS x and y.

    GML axis labels and CRS component order do not always match (``Lat Long``
    with a lat-first CRS is the common case).  Prefer the assignment with the
    largest diagonal weight, keeping identity on ties so ordinary grids are
    unaffected.
    """
    x_vector = offsets[x_pos]
    y_vector = offsets[y_pos]
    if len(x_vector) < 2 or len(y_vector) < 2:
        return x_pos if x_pos < 2 else 0, y_pos if y_pos < 2 else 1
    candidates = [(0, 1), (1, 0)]
    best = None
    best_score = None
    for cx, cy in candidates:
        score = abs(x_vector[cx]) + abs(y_vector[cy])
        if best_score is None or score > best_score + 1e-15:
            best = (cx, cy)
            best_score = score
    return best


def _grid_parts(grid):
    labels = list(getattr(grid, "axislabels", []) or [])
    low = getattr(grid, "lowlimits", None)
    high = getattr(grid, "highlimits", None)
    origin = getattr(grid, "origin", None)
    offsets = getattr(grid, "offsetvectors", None)
    if low is None or high is None:
        raise GridSubsetError("The grid does not declare GridEnvelope limits")
    low = [int(value) for value in low]
    high = [int(value) for value in high]
    if not labels:
        labels = ["axis%d" % pos for pos in range(len(low))]
    if len(low) != len(labels) or len(high) != len(labels):
        raise GridSubsetError(
            "GridEnvelope limits (%d) do not match axis labels (%d)"
            % (len(low), len(labels))
        )

    has_transform = origin is not None and offsets
    if origin is None:
        # A plain gml:Grid declares no coordinates.  Use zero ordinates so the
        # native ranges remain available; a geometry that needs a real
        # transform is rejected by _GridGeometry.
        origin = [0.0] * len(labels)
    else:
        origin = [
            float(value)
            if _is_number(str(value).strip().strip('"').strip("'"))
            else str(value).strip().strip('"').strip("'")
            for value in origin
        ]

    if offsets:
        normalized = []
        for vector in offsets:
            converted = []
            for value in vector:
                converted.append(_number(value) if _is_number(value) else value)
            normalized.append(converted)
        offsets = normalized
        if len(offsets) != len(labels):
            raise GridSubsetError(
                "Grid has %d offset vectors for %d axes"
                % (len(offsets), len(labels))
            )
    else:
        offsets = [[0.0] * len(origin) for _ in labels]
        if has_transform:
            raise GridSubsetError("The grid does not declare offset vectors")
    for vector in offsets:
        if len(vector) < len(origin):
            vector.extend([0.0] * (len(origin) - len(vector)))
    grid._subset_has_transform = bool(has_transform)
    return labels, low, high, origin, offsets


def _axis_coefficients(grid, labels):
    raw = getattr(grid, "axis_coefficients", None)
    if isinstance(raw, dict):
        return {
            _norm(name): list(values)
            for name, values in raw.items()
            if values
        }
    result = {}
    if raw:
        for pos, values in enumerate(raw):
            if values:
                result[_norm(labels[pos])] = list(values)
    return result


def _parse_coefficients(values, kind):
    parsed = []
    for value in values:
        text = str(value).strip().strip('"').strip("'")
        if kind == "time":
            parsed.append(_as_datetime(text))
        elif _is_number(text):
            parsed.append(float(text))
        else:
            parsed.append(text)
    return parsed


def _axis_kind(name, coefficients):
    normal = _norm(name)
    if normal in TIME_AXIS_ALIASES:
        return "time"
    if coefficients and any(isinstance(value, datetime) for value in coefficients):
        return "time"
    if coefficients and _looks_like_time(coefficients[0]):
        return "time"
    if normal in X_AXIS_ALIASES:
        return "x"
    if normal in Y_AXIS_ALIASES:
        return "y"
    return "other"


def _looks_like_time(value):
    """Heuristic for an ISO-like coefficient that names a sampling instant."""
    if isinstance(value, datetime):
        return True
    text = str(value).strip().strip('"').strip("'")
    if _is_number(text):
        return False
    if not any(token in text for token in ("-", "T", ":", "Z")):
        return False
    try:
        parser.parse(text)
        return True
    except (ValueError, TypeError, OverflowError):
        return False


def _determine_spatial_axes(labels, kinds, offsets, x_axis, y_axis, axis_map):
    map_x = map_y = None
    for key, value in (axis_map or {}).items():
        if _norm(key) in X_AXIS_ALIASES or _norm(key) == "x":
            map_x = value
        elif _norm(key) in Y_AXIS_ALIASES or _norm(key) == "y":
            map_y = value

    x_pos = _resolve_axis_name(labels, x_axis if x_axis is not None else map_x)
    y_pos = _resolve_axis_name(labels, y_axis if y_axis is not None else map_y)
    if x_pos is None:
        x_pos = _find_axis(labels, X_AXIS_ALIASES)
    if y_pos is None:
        y_pos = _find_axis(labels, Y_AXIS_ALIASES)

    candidates = [pos for pos, kind in enumerate(kinds) if kind != "time"]

    def dominance(pos, component):
        vector = offsets[pos]
        return abs(vector[component]) if len(vector) > component else 0.0

    if x_pos is None and candidates:
        x_pos = max(candidates, key=lambda pos: dominance(pos, 0))
    if y_pos is None and candidates:
        y_pos = max(
            (pos for pos in candidates if pos != x_pos),
            key=lambda pos: dominance(pos, 1),
            default=None,
        )
    if x_pos is None or y_pos is None or x_pos == y_pos:
        raise GridSubsetError("Cannot identify two distinct spatial axes")
    return x_pos, y_pos


def _select_spatial(geometry, bbox_pairs):
    native_x = geometry.native_x
    native_y = geometry.native_y

    if bbox_pairs is None:
        x_indices = native_x.indices()
        y_indices = native_y.indices()
        # No spatial request: every cell of the block is fully wanted, so
        # leave the mask sparse and let cell construction default to "full".
        return x_indices, y_indices, {}, None

    bounds = {}
    for name, lower, upper in bbox_pairs:
        key = _norm(name)
        if key == _norm(geometry.x_name) or key in X_AXIS_ALIASES:
            bounds["x"] = (float(lower), float(upper))
        elif key == _norm(geometry.y_name) or key in Y_AXIS_ALIASES:
            bounds["y"] = (float(lower), float(upper))
        else:
            raise GridSubsetError("Spatial bbox names an unknown axis %r" % (name,))
    for required in ("x", "y"):
        if required not in bounds:
            raise GridSubsetError("A spatial bbox must give both x and y bounds")

    minx, maxx = sorted(bounds["x"])
    miny, maxy = sorted(bounds["y"])
    if minx == maxx or miny == maxy:
        raise GridSubsetError("A spatial bbox must have a non-zero area")

    if _is_axis_aligned(geometry):
        return _select_spatial_aligned(
            geometry, (minx, miny, maxx, maxy)
        )
    return _select_spatial_rotated(geometry, (minx, miny, maxx, maxy))


def _is_axis_aligned(geometry, tolerance=1e-12):
    return (
        abs(geometry.x_offset[1]) <= tolerance
        and abs(geometry.y_offset[0]) <= tolerance
        and abs(geometry.x_offset[0]) > tolerance
        and abs(geometry.y_offset[1]) > tolerance
    )


def _index_span(native, origin, step, lower, upper, eps):
    """Index ranges that intersect and that lie fully inside a value range."""
    u_lower = (lower - origin) / step
    u_upper = (upper - origin) / step
    u_min, u_max = sorted((u_lower, u_upper))

    overlap_start = max(native.low, int(floor(u_min + eps)))
    overlap_stop = min(native.high, int(ceil(u_max - eps)) - 1)
    if overlap_stop < overlap_start:
        return (), ()

    full_start = max(native.low, int(ceil(u_min - eps)))
    full_stop = min(native.high, int(floor(u_max + eps)) - 1)
    if full_stop < full_start:
        full = ()
    else:
        full = tuple(range(full_start, full_stop + 1))
    return tuple(range(overlap_start, overlap_stop + 1)), full


def _overlap_fraction(start, stop, lower, upper):
    overlap = min(stop, upper) - max(start, lower)
    if overlap <= 0:
        return 0.0
    span = abs(stop - start)
    return overlap / span if span else 0.0


def _select_spatial_aligned(geometry, rect):
    """Exact and cheap selection for an axis-aligned grid."""
    minx, miny, maxx, maxy = rect
    ox, oy = geometry.origin
    sx = geometry.x_offset[0]
    sy = geometry.y_offset[1]
    native_x = geometry.native_x
    native_y = geometry.native_y

    span = max(1.0, abs(ox), abs(oy), abs(minx), abs(maxx), abs(miny), abs(maxy))
    eps = _INDEX_EPS * span

    x_indices, full_x = _index_span(native_x, ox, sx, minx, maxx, eps)
    y_indices, full_y = _index_span(native_y, oy, sy, miny, maxy, eps)
    if not x_indices or not y_indices:
        return (), (), {}, (minx, miny, maxx, maxy)

    full_x_set = frozenset(full_x)
    full_y_set = frozenset(full_y)
    coverage = {}

    def classify(i, j):
        x0 = ox + i * sx
        x1 = ox + (i + 1) * sx
        y0 = oy + j * sy
        y1 = oy + (j + 1) * sy
        fraction = _overlap_fraction(
            min(x0, x1), max(x0, x1), minx, maxx
        ) * _overlap_fraction(min(y0, y1), max(y0, y1), miny, maxy)
        if fraction <= _AREA_EPS:
            coverage[(i, j)] = (0.0, "outside")
        elif fraction >= 1.0 - 1e-9:
            coverage[(i, j)] = (1.0, "full")
        else:
            coverage[(i, j)] = (min(1.0, fraction), "partial")

    # Only the boundary bands can be non-full; the interior defaults to full.
    for i in x_indices:
        if i in full_x_set:
            continue
        for j in y_indices:
            classify(i, j)
    for i in full_x:
        for j in y_indices:
            if j not in full_y_set:
                classify(i, j)

    return x_indices, y_indices, coverage, None


def _select_spatial_rotated(geometry, rect):
    minx, miny, maxx, maxy = rect
    native_x = geometry.native_x
    native_y = geometry.native_y
    corners = [
        geometry.xy_to_index(x, y)
        for x in (minx, maxx)
        for y in (miny, maxy)
    ]
    u_values = [corner[0] for corner in corners]
    v_values = [corner[1] for corner in corners]
    span = max(1.0, *(abs(value) for value in u_values + v_values))
    eps = _INDEX_EPS * span

    def _snap(value):
        nearest = round(value)
        return float(nearest) if abs(value - nearest) <= eps else value

    u_values = [_snap(value) for value in u_values]
    v_values = [_snap(value) for value in v_values]

    u_start = max(native_x.low, int(floor(min(u_values) + eps)))
    u_stop = min(native_x.high, int(ceil(max(u_values) - eps)) - 1)
    v_start = max(native_y.low, int(floor(min(v_values) + eps)))
    v_stop = min(native_y.high, int(ceil(max(v_values) - eps)) - 1)

    if u_stop < u_start or v_stop < v_start:
        return (), (), {}, (minx, miny, maxx, maxy)

    cell_area = geometry.cell_area()
    coverage = {}
    hit_x = set()
    hit_y = set()
    for i in range(u_start, u_stop + 1):
        for j in range(v_start, v_stop + 1):
            polygon = geometry.cell_polygon(i, j)
            intersection = _polygon_area(_clip_polygon(polygon, rect))
            if intersection <= _AREA_EPS:
                coverage[(i, j)] = (0.0, "outside")
                continue
            hit_x.add(i)
            hit_y.add(j)
            fraction = min(1.0, max(0.0, intersection / cell_area))
            if fraction < 1.0 - 1e-9:
                coverage[(i, j)] = (fraction, "partial")
            # Fully covered cells are omitted; the mask defaults to "full".
    if not hit_x:
        return (), (), coverage, (minx, miny, maxx, maxy)
    return (
        tuple(range(min(hit_x), max(hit_x) + 1)),
        tuple(range(min(hit_y), max(hit_y) + 1)),
        coverage,
        None,
    )


def _select_listed_times(times, positions, native, tolerance, on_missing):
    """Exact selection among the explicitly listed instants of an axis."""
    if times is None:
        return tuple(range(native.low, native.high + 1)), ()
    if isinstance(times, (str, datetime)):
        times = [times]
    if tolerance is None:
        tolerance = timedelta(0)
    selected = []
    unmatched = []
    for value in times:
        wanted = _as_datetime(value)
        best_index = None
        best_delta = None
        for pos, position in enumerate(positions):
            delta = abs(position - wanted)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_index = pos + native.low
        if best_delta is not None and best_delta <= tolerance:
            if best_index not in selected:
                selected.append(best_index)
        else:
            unmatched.append(wanted)
    if unmatched and on_missing == "raise":
        raise GridSubsetError(
            "Requested time(s) are not present in the grid: %r" % (unmatched,)
        )
    return tuple(selected), tuple(unmatched)


def _select_regular_times(times, native, start, step, on_missing, tolerance):
    """Select the sampling position nearest each requested instant.

    A regular time axis publishes cell centres.  An instant within half a step
    (or the caller's explicit tolerance, if larger) of a published centre
    selects that cell; anything farther is reported as unmatched rather than
    replaced by a value the server did not provide.
    """
    if times is None:
        return tuple(range(native.low, native.high + 1)), ()
    if isinstance(times, (str, datetime)):
        times = [times]
    count = native.count
    selected = []
    unmatched = []
    if tolerance is None:
        tolerance = abs(step) / 2 if step else timedelta(0)
    for value in times:
        wanted = _as_datetime(value)
        best_index = None
        best_delta = None
        for offset in range(count):
            index = native.low + offset
            centre = start + step * (offset + 0.5)
            delta = abs(centre - wanted)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_index = index
        if best_delta is not None and best_delta <= tolerance:
            if best_index not in selected:
                selected.append(best_index)
        else:
            unmatched.append(wanted)
    if unmatched and on_missing == "raise":
        raise GridSubsetError(
            "Requested time(s) are not present in the grid: %r" % (unmatched,)
        )
    return tuple(selected), tuple(unmatched)


def _select_values(values, positions, native, tolerance, on_missing):
    if values is None:
        return tuple(range(native.low, native.high + 1)), ()
    if isinstance(values, (int, float)):
        values = [values]
    if len(values) == 2 and all(isinstance(v, (int, float)) for v in values):
        lower, upper = sorted(float(v) for v in values)
        selected = tuple(
            index
            for index, position in zip(range(native.low, native.high + 1), positions)
            if isinstance(position, (int, float))
            and lower - tolerance <= float(position) <= upper + tolerance
        )
        return selected, ()
    selected = []
    unmatched = []
    for value in values:
        best_index = None
        best_delta = None
        for pos, position in enumerate(positions):
            if not isinstance(position, (int, float)):
                continue
            delta = abs(float(position) - float(value))
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_index = pos + native.low
        if best_delta is not None and best_delta <= tolerance:
            if best_index not in selected:
                selected.append(best_index)
        else:
            unmatched.append(value)
    if unmatched and on_missing == "raise":
        raise GridSubsetError(
            "Requested value(s) are not present in the axis: %r" % (unmatched,)
        )
    return tuple(selected), tuple(unmatched)


def _regular_positions(origin, offset, low, high, component, kind):
    values = []
    for index in range(low, high + 1):
        if kind == "time":
            if component >= len(origin):
                raise GridSubsetError(
                    "A time axis has no time origin ordinate"
                )
            if component >= len(offset):
                raise GridSubsetError(
                    "A time axis has no offset vector component"
                )
            start = _as_datetime(origin[component])
            values.append(
                start + timedelta(days=offset[component] * (index - low + 0.5))
            )
        else:
            if component >= len(origin):
                raise GridSubsetError(
                    "Axis %d has no origin ordinate" % component
                )
            if component >= len(offset):
                raise GridSubsetError(
                    "Axis %d has no offset vector component" % component
                )
            values.append(
                float(origin[component])
                + (index - low + 0.5) * offset[component]
            )
    return values


def plan_grid_subset(
    grid,
    bbox=None,
    times=None,
    *,
    x_axis=None,
    y_axis=None,
    axis_map=None,
    supported_axes=None,
    time_tolerance=None,
    on_missing_time="raise",
    subsets=None,
):
    """Plan physical index ranges for a WCS grid subset.

    :param grid: a Grid, RectifiedGrid or ReferenceableGridByVectors instance.
    :param bbox: ``(minx, miny, maxx, maxy)`` in the grid's CRS, a named
        mapping such as ``{"Long": (-10, 0), "Lat": (40, 50)}``, or ``None``
        for the whole spatial domain.
    :param times: one or several sampling datetimes/ISO strings.  When omitted
        the whole time axis is selected.  Times are matched against the grid's
        actual positions; a missing time raises unless
        ``on_missing_time="skip"``.
    :param x_axis, y_axis: storage axis used for request x/y.
    :param axis_map: alternative mapping, e.g. ``{"x": "Long", "y": "Lat"}``.
    :param supported_axes: axis names the service can subset.  Every other
        axis is read over its whole native range and clipped locally.
    :param time_tolerance: seconds or timedelta allowed when matching times.
    :param on_missing_time: ``"raise"`` (default) or ``"skip"``.
    :param subsets: optional mapping or list of ``(axis, lower, upper)`` value
        subsets for additional non-spatial, non-time axes.

    :returns: :class:`GridSubset`.
    """
    labels, low, high, origin, offsets = _grid_parts(grid)
    raw_coefficients = _axis_coefficients(grid, labels)

    kinds = []
    coefficients = []
    for name in labels:
        values = raw_coefficients.get(_norm(name))
        kind = _axis_kind(name, values)
        kinds.append(kind)
        coefficients.append(
            _parse_coefficients(values, kind) if values else None
        )

    x_pos, y_pos = _determine_spatial_axes(
        labels, kinds, offsets, x_axis, y_axis, axis_map
    )
    native_x = IndexRange(low[x_pos], high[x_pos])
    native_y = IndexRange(low[y_pos], high[y_pos])
    if getattr(grid, "_subset_has_transform", False):
        geometry = _GridGeometry(
            origin,
            offsets,
            x_pos,
            y_pos,
            labels[x_pos],
            labels[y_pos],
            native_x,
            native_y,
        )
    else:
        if bbox is not None:
            raise GridSubsetError(
                "The grid has no origin/offset vectors, so a CRS bbox cannot "
                "be mapped to physical indices"
            )
        geometry = _IdentityGridGeometry(
            labels[x_pos],
            labels[y_pos],
            native_x,
            native_y,
        )
    # The GridSubset uses this to compute true cell positions lazily.
    grid._subset_geometry = geometry

    bbox_pairs = _bbox_pairs(bbox, labels[x_pos], labels[y_pos])
    x_indices, y_indices, coverage_by_cell, outside = _select_spatial(
        geometry, bbox_pairs
    )

    time_pos = next(
        (pos for pos, kind in enumerate(kinds) if kind == "time"), None
    )
    tolerance = _as_tolerance(time_tolerance)
    if times is not None and time_pos is None:
        raise GridSubsetError(
            "times were requested but the grid has no time axis (%r)" % (labels,)
        )

    named_subsets = {}
    if subsets:
        items = subsets.items() if isinstance(subsets, dict) else subsets
        for item in items:
            item = tuple(item)
            if isinstance(subsets, dict):
                name, bounds = item
                if len(bounds) != 2:
                    raise GridSubsetError(
                        "Each named subset must have two bounds"
                    )
                lower, upper = bounds
            else:
                if len(item) != 3:
                    raise GridSubsetError(
                        "Each named subset must be (axis, lower, upper)"
                    )
                name, lower, upper = item
            named_subsets[_norm(name)] = (lower, upper)
    for name in named_subsets:
        if name not in {_norm(label) for label in labels}:
            raise GridSubsetError(
                "Named subset axis %r is not in the grid (%r)" % (name, labels)
            )

    axis_selections = OrderedDict()
    unmatched_times = ()

    for pos, name in enumerate(labels):
        native = IndexRange(low[pos], high[pos])
        kind = kinds[pos]
        coeffs = coefficients[pos]
        component = pos if pos < len(origin) else len(origin) - 1

        if coeffs:
            all_positions = coeffs
            irregular = True
        elif kind == "time":
            all_positions = _regular_positions(
                origin, offsets[pos], native.low, native.high, component, kind
            )
            irregular = False
        else:
            if component >= len(origin):
                raise GridSubsetError(
                    "Axis %d has no origin ordinate" % component
                )
            all_positions = _regular_positions(
                origin, offsets[pos], native.low, native.high, component, kind
            )
            irregular = False

        if pos == x_pos:
            selected = x_indices
        elif pos == y_pos:
            selected = y_indices
        elif kind == "time":
            if coeffs:
                selected, unmatched = _select_listed_times(
                    times,
                    all_positions,
                    native,
                    tolerance,
                    on_missing_time,
                )
            else:
                start = _as_datetime(origin[component])
                step = timedelta(days=float(offsets[pos][component]))
                selected, unmatched = _select_regular_times(
                    times,
                    native,
                    start,
                    step,
                    on_missing_time,
                    tolerance,
                )
            unmatched_times = tuple(unmatched)
        elif _norm(name) in named_subsets:
            selected, unmatched = _select_values(
                named_subsets[_norm(name)],
                all_positions,
                native,
                1e-9,
                on_missing_time,
            )
        else:
            selected = tuple(range(native.low, native.high + 1))

        supported = True
        if supported_axes is not None:
            supported = _norm(name) in {_norm(axis) for axis in supported_axes}
        if supported:
            read_indices = tuple(selected)
        else:
            read_indices = tuple(range(native.low, native.high + 1))

        axis = AxisSelection(
            name,
            native,
            selected,
            all_positions,
            request_name=name,
            request_axis=(
                "x" if pos == x_pos else "y" if pos == y_pos else (
                    "time" if kind == "time" else name
                )
            ),
            kind=kind,
            irregular=irregular,
            supported=supported,
            read_indices=read_indices,
        )
        axis_selections[name] = axis

    request_axes = [labels[x_pos], labels[y_pos]]
    if time_pos is not None:
        request_axes.append(labels[time_pos])
    for pos, name in enumerate(labels):
        if pos in (x_pos, y_pos, time_pos):
            continue
        if _norm(name) in named_subsets:
            request_axes.append(name)
    seen = set()
    request_axes = tuple(
        name for name in request_axes if not (name in seen or seen.add(name))
    )

    return GridSubset(
        grid,
        axis_selections,
        request_axes,
        (labels[x_pos], labels[y_pos]),
        bbox,
        coverage_by_cell,
        unmatched_times=unmatched_times,
        outside_bbox=outside,
        time_axis=labels[time_pos] if time_pos is not None else None,
    )


def plan_subset_from_subsets(
    grid,
    subsets,
    *,
    supported_axes=None,
    time_tolerance=None,
    on_missing_time="raise",
):
    """Plan a grid subset from WCS ``SUBSET`` triples.

    ``subsets`` uses the same shape as :meth:`getCoverage`: a list (or dict) of
    ``(axis, lower, upper)`` value ranges in request terms, for example
    ``[("Long", -10, 0), ("Lat", 40, 50), ("ansi", "2000-02-01T00:00:00Z")]``.

    Two axes must describe the spatial rectangle.  A single-valued time subset
    is treated as one sampling instant; a time range is expanded to the time
    positions that fall inside it.  All remaining axes are selected exactly as
    given.
    """
    if isinstance(subsets, dict):
        items = [(name, *bounds) for name, bounds in subsets.items()]
    else:
        items = [tuple(item) for item in subsets]

    labels, low, high, origin, offsets = _grid_parts(grid)
    raw_coefficients = _axis_coefficients(grid, labels)
    kinds = []
    coefficients = []
    for name in labels:
        values = raw_coefficients.get(_norm(name))
        kind = _axis_kind(name, values)
        kinds.append(kind)
        coefficients.append(_parse_coefficients(values, kind) if values else None)

    x_pos = _find_axis(labels, X_AXIS_ALIASES)
    y_pos = _find_axis(labels, Y_AXIS_ALIASES)
    if x_pos is None or y_pos is None:
        raise GridSubsetError(
            "Cannot identify spatial axes from labels %r; pass x_axis/y_axis"
            % (labels,)
        )
    time_pos = next((pos for pos, kind in enumerate(kinds) if kind == "time"), None)

    by_name = {}
    for item in items:
        if len(item) == 2:
            name, value = item
            item = (name, value, value)
        elif len(item) != 3:
            raise GridSubsetError("Each subset must be (axis, lower, upper)")
        name, lower, upper = item
        by_name[_norm(name)] = (lower, upper)

    spatial_bounds = {}
    for name, bounds in by_name.items():
        if name == _norm(labels[x_pos]) or name in X_AXIS_ALIASES:
            spatial_bounds["x"] = bounds
        elif name == _norm(labels[y_pos]) or name in Y_AXIS_ALIASES:
            spatial_bounds["y"] = bounds
    if len(spatial_bounds) != 2:
        raise GridSubsetError(
            "Subsets must provide exactly one x and one y spatial range"
        )

    bbox = {
        labels[x_pos]: spatial_bounds["x"],
        labels[y_pos]: spatial_bounds["y"],
    }

    def _range_bounds(bounds, all_positions, native, kind):
        lower, upper = bounds
        if kind == "time":
            low_value = _as_datetime(lower)
            high_value = _as_datetime(upper)
        else:
            low_value = float(lower)
            high_value = float(upper)
        if low_value > high_value:
            low_value, high_value = high_value, low_value
        return tuple(
            index
            for index, position in zip(
                range(native.low, native.high + 1), all_positions
            )
            if low_value <= position <= high_value
        )

    extras = {}
    times = None
    for pos, name in enumerate(labels):
        key = _norm(name)
        if pos in (x_pos, y_pos) or key not in by_name:
            continue
        native = IndexRange(low[pos], high[pos])
        kind = kinds[pos]
        values = coefficients[pos]
        if values:
            all_positions = values
        elif kind == "time":
            all_positions = _regular_positions(
                origin, offsets[pos], native.low, native.high, pos, kind
            )
        else:
            component = pos if pos < len(origin) else len(origin) - 1
            all_positions = _regular_positions(
                origin, offsets[pos], native.low, native.high, component, kind
            )
        bounds = by_name[key]
        if kind == "time":
            if bounds[0] == bounds[1] or _norm(str(bounds[0])) == _norm(str(bounds[1])):
                times = [bounds[0]]
                continue
            times = _time_range_instants(all_positions, native, bounds)
        else:
            extras[name] = _range_bounds(bounds, all_positions, native, kind)

    return plan_grid_subset(
        grid,
        bbox=bbox,
        times=times,
        supported_axes=supported_axes,
        time_tolerance=time_tolerance,
        on_missing_time=on_missing_time,
        subsets=extras,
    )


def _time_range_instants(positions, native, bounds):
    lower = _as_datetime(bounds[0])
    upper = _as_datetime(bounds[1])
    if lower > upper:
        lower, upper = upper, lower
    return [
        position
        for position in positions
        if lower <= position <= upper
    ]


def plan_grid_batches(
    grid,
    bbox=None,
    times=None,
    *,
    batch_times=False,
    chunk=None,
    **kwargs,
):
    """Plan one or more ingest batches for the same spatial request.

    ``batch_times`` splits the sampling instants into one plan per instant, so
    each batch carries exactly one time range and the true time position of its
    cell.  ``chunk`` (an integer) additionally splits a single plan's spatial
    index ranges into contiguous blocks when the full range would be too large
    to fetch at once, while each returned plan keeps the exact covered cell
    positions.

    Returns a list of :class:`GridSubset` objects.
    """
    if chunk is not None:
        if not isinstance(chunk, int) or chunk <= 0:
            raise GridSubsetError("chunk must be a positive integer")

    def plan_for(selected_times):
        return plan_grid_subset(grid, bbox=bbox, times=selected_times, **kwargs)

    if not batch_times:
        base = plan_for(times)
        return _chunk_plan(base, chunk)

    if times is None:
        seed = plan_for(None)
        axis = seed.axis(seed.time_axis) if seed.time_axis else None
        if axis is None:
            return _chunk_plan(seed, chunk)
        instants = list(axis.positions)
    elif isinstance(times, (str, datetime)):
        instants = [times]
    else:
        instants = list(times)

    plans = []
    for instant in instants:
        plan = plan_for([instant])
        plans.extend(_chunk_plan(plan, chunk))
    return plans


def _chunk_plan(plan, chunk):
    if not chunk:
        return [plan]
    spatial = plan.spatial_axes
    if not spatial:
        return [plan]
    x_axis = plan.axis(spatial[0])
    y_axis = plan.axis(spatial[1])
    x_values = tuple(x_axis)
    y_values = tuple(y_axis)
    if len(x_values) <= chunk and len(y_values) <= chunk:
        return [plan]

    blocks = []
    for x_start in range(0, len(x_values), chunk):
        for y_start in range(0, len(y_values), chunk):
            x_part = x_values[x_start : x_start + chunk]
            y_part = y_values[y_start : y_start + chunk]
            blocks.append(_subplan(plan, x_part, y_part))
    return blocks


def _subplan(plan, x_values, y_values):
    """Build a plan restricted to a contiguous sub-block of the spatial axes."""
    axis_selections = OrderedDict()
    x_name, y_name = plan.spatial_axes[0], plan.spatial_axes[1]
    for name, axis in plan.axes.items():
        if name == x_name:
            indices = tuple(x_values)
        elif name == y_name:
            indices = tuple(y_values)
        else:
            indices = tuple(axis)
        if axis.supported:
            read_indices = indices
        else:
            read_indices = tuple(axis.read_indices)
        axis_selections[name] = AxisSelection(
            name,
            axis.native,
            indices,
            axis._all_positions,
            request_name=axis.request_name,
            request_axis=axis.request_axis,
            kind=axis.kind,
            irregular=axis.irregular,
            supported=axis.supported,
            read_indices=read_indices,
        )

    coverage = {
        key: value
        for key, value in plan._coverage_by_cell.items()
        if key[0] in x_values and key[1] in y_values
    }
    return GridSubset(
        plan.grid,
        axis_selections,
        plan.request_axes,
        plan.spatial_axes,
        plan.bbox,
        coverage,
        unmatched_times=plan.unmatched_times,
        outside_bbox=plan.outside_bbox,
        time_axis=plan.time_axis,
    )

