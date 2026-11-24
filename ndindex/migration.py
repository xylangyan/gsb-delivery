"""
Symbolic verification of index-plan migrations.

This module implements :func:`verify_migration`, an entry point for checking
whether two multi-step indexing plans select the same *cells* of an array for
every allowed array shape.

The check never reads array data.  Instead, each indexing step is evaluated
symbolically: every output position is mapped back to the coordinate it
refers to in the original array.  Two plans are incompatible as soon as the
same output position refers to different original coordinates, even if the
stored values would happen to be equal.

The semantics follow ordinary NumPy basic/advanced indexing, including

- integers, slices, ellipses, ``np.newaxis`` and small integer array indices,
- broadcasting of integer array indices,
- and the rule that advanced indices separated from one another by slices,
  newaxes, or an ellipsis have their result axes moved to the front.

Because :class:`~ndindex.tuple.Tuple` itself does not support advanced indices
separated by slices or newaxes, the indexing engine here reuses ndindex's
leaf types (``Integer``, ``Slice``, ``IntegerArray``) but implements the full
NumPy tuple-index layout on its own.
"""

from itertools import product

from .ndindex import ndindex, NDIndexCommon
from .slice import Slice
from .integerarray import IntegerArray
from .booleanarray import BooleanArray
from .shapetools import broadcast_shapes

__all__ = ['IndexPlan', 'MigrationReport', 'verify_migration']

# Maximum number of concrete shapes evaluated by exhaustive enumeration.
# Above this, only critical-length representatives are evaluated.
_MAX_EXHAUSTIVE_SHAPES = 100_000

# Maximum number of output coordinates probed when comparing two maps with
# the same output shape.
_MAX_PROBE_COORDS = 4_096


class IndexPlan:
    """
    A sequence of indexing steps applied one after another.

    Each step is an ordinary NumPy index: an integer, slice, ellipsis,
    ``None`` (newaxis), integer array, or a tuple of these.  A step may also
    be an ndindex object, in which case its :attr:`~ndindex.ndindex.NDIndex.raw`
    form is used.

    >>> plan = IndexPlan([slice(None, None, -1), slice(0, None, 2)])
    >>> len(plan.steps)
    2

    The plan represents ``a[::-1][::2]``: every step indexes the result of the
    previous one.
    """

    def __init__(self, steps):
        if isinstance(steps, IndexPlan):
            self.steps = steps.steps
            return
        if not isinstance(steps, (tuple, list)):
            raise TypeError("IndexPlan expects a sequence of indexing steps")
        self.steps = tuple(_normalize_step(step) for step in steps)
        if not self.steps:
            raise ValueError("an IndexPlan must contain at least one step")

    def __repr__(self):
        return f"IndexPlan({list(self.steps)!r})"

    def __eq__(self, other):
        return isinstance(other, IndexPlan) and self.steps == other.steps

    def __hash__(self):
        return hash(self.steps)


class MigrationReport:
    """
    Result of :func:`verify_migration`.

    Attributes
    ----------
    compatible : bool
        ``True`` if both plans are valid and select the same original cells in
        the same output order for every allowed array shape.
    reason : str
        One of ``"compatible"``, ``"invalid"``, ``"shape_mismatch"`` or
        ``"different_cells"``.
    shape : tuple of ints or None
        The lexicographically earliest allowed shape producing a
        counterexample, or ``None`` when the plans are compatible.
    failing_plan : int or None
        Which plan fails (``0`` or ``1``) when ``reason == "invalid"``.
    failing_step : int or None
        Zero-based index of the step that raises ``IndexError`` when
        ``reason == "invalid"``.
    result_shapes : tuple of tuples or None
        The two result shapes on the counterexample shape.
    output_coordinate : tuple of ints or None
        An output position that refers to different original cells when
        ``reason == "different_cells"``.
    source_coordinates : tuple of tuples or None
        The original coordinates selected by each plan at
        ``output_coordinate`` when ``reason == "different_cells"``.
    """

    def __init__(self, compatible, reason, *, shape=None, failing_plan=None,
                 failing_step=None, result_shapes=None, output_coordinate=None,
                 source_coordinates=None):
        self.compatible = compatible
        self.reason = reason
        self.shape = shape
        self.failing_plan = failing_plan
        self.failing_step = failing_step
        self.result_shapes = result_shapes
        self.output_coordinate = output_coordinate
        self.source_coordinates = source_coordinates

    def __repr__(self):
        if self.compatible:
            return "MigrationReport(compatible=True, reason='compatible')"
        parts = ["compatible=False", f"reason={self.reason!r}"]
        if self.shape is not None:
            parts.append(f"shape={self.shape!r}")
        if self.failing_plan is not None:
            parts.append(f"failing_plan={self.failing_plan!r}")
            parts.append(f"failing_step={self.failing_step!r}")
        if self.result_shapes is not None:
            parts.append(f"result_shapes={self.result_shapes!r}")
        if self.output_coordinate is not None:
            parts.append(f"output_coordinate={self.output_coordinate!r}")
            parts.append(f"source_coordinates={self.source_coordinates!r}")
        return "MigrationReport(" + ", ".join(parts) + ")"

    def summary(self):
        """
        Return a human-readable description of the report.
        """
        if self.compatible:
            return ("The two plans select the same original cells in the same "
                    "order on every allowed shape.")
        if self.reason == "invalid":
            return (f"On shape {self.shape}, plan {self.failing_plan} fails at "
                    f"step {self.failing_step} with IndexError.")
        if self.reason == "shape_mismatch":
            return (f"On shape {self.shape}, the plans produce different result "
                    f"shapes {self.result_shapes[0]} and {self.result_shapes[1]}.")
        return (f"On shape {self.shape}, output coordinate "
                f"{self.output_coordinate} selects original cell "
                f"{self.source_coordinates[0]} under plan 0 but "
                f"{self.source_coordinates[1]} under plan 1.")


def verify_migration(plan_a, plan_b, axis_ranges, equal_axes=()):
    """
    Verify that two multi-step indexing plans are equivalent on every allowed
    array shape.

    Both plans must produce a result (not raise ``IndexError``) on each allowed
    shape.  Equivalence is defined cell-wise: for every output position, the
    two plans must refer to the same coordinate of the original array.  The
    values stored in the array are never inspected, so coincidentally equal
    values do not count as a match.

    Parameters
    ----------
    plan_a, plan_b : IndexPlan or sequence of indices
        The two plans.  Each plan is a sequence of indexing steps, applied in
        order, each step indexing the result of the previous one.  A raw index
        may be an integer, slice, ellipsis, ``None``, integer array, or a
        tuple of these.
    axis_ranges : sequence of (int, int or None) pairs
        Inclusive finite ranges ``(low, high)`` of the possible length of each
        original array axis.  ``high=None`` means the axis is unbounded above.
    equal_axes : sequence of pairs of ints, optional
        Axis indices whose lengths must be equal on every allowed shape.
        Transitive grouping is supported.

    Returns
    -------
    MigrationReport
        A compatible report, or the lexicographically earliest counterexample.
        When the shape space is small enough it is enumerated exhaustively;
        otherwise a finite set of critical-length representatives is used,
        covering every clipping boundary induced by the plans.

    Examples
    --------
    Reversing an axis and then taking every other element differs from taking
    every other element first and then reversing once the axis is even:

    >>> from ndindex import verify_migration, IndexPlan
    >>> reverse_then_stride = IndexPlan([slice(None, None, -1), slice(0, None, 2)])
    >>> stride_then_reverse = IndexPlan([slice(0, None, 2), slice(None, None, -1)])
    >>> report = verify_migration(reverse_then_stride, stride_then_reverse,
    ...                           [(1, 6)])
    >>> report.compatible
    False
    >>> report.shape
    (2,)
    """
    plan_a = IndexPlan(plan_a)
    plan_b = IndexPlan(plan_b)
    spec = _ShapeSpec(axis_ranges, equal_axes)

    critical = _critical_lengths(plan_a, plan_b, spec)
    exhaustive = spec.total_combinations is not None

    for shape in spec.iter_shapes(critical, exhaustive=exhaustive):
        report = _compare_on_shape(plan_a, plan_b, shape)
        if report is not None:
            return report

    return MigrationReport(True, "compatible")


# ---------------------------------------------------------------------------
# Shape constraints
# ---------------------------------------------------------------------------


class _ShapeSpec:
    """
    Allowed array shapes expressed as per-axis ranges with equality groups.
    """

    def __init__(self, axis_ranges, equal_axes):
        axis_ranges = tuple(axis_ranges)
        if not axis_ranges:
            raise ValueError("at least one array axis is required")
        self.ndim = len(axis_ranges)
        self.ranges = []
        for low, high in axis_ranges:
            low = _as_int(low, "axis range lower bound")
            if low < 0:
                raise ValueError("axis lengths must be nonnegative")
            if high is not None:
                high = _as_int(high, "axis range upper bound")
                if high < low:
                    raise ValueError("axis range upper bound is below lower bound")
            self.ranges.append((low, high))

        groups = {i: i for i in range(self.ndim)}

        def find(x):
            while groups[x] != x:
                groups[x] = groups[groups[x]]
                x = groups[x]
            return x

        for pair in equal_axes:
            if len(pair) != 2:
                raise ValueError("equal_axes entries must be pairs of axis indices")
            x = _as_int(pair[0], "equal_axes entry")
            y = _as_int(pair[1], "equal_axes entry")
            if not (0 <= x < self.ndim and 0 <= y < self.ndim):
                raise ValueError("equal_axes refers to an out-of-bounds axis")
            root_x, root_y = find(x), find(y)
            if root_x != root_y:
                groups[root_y] = root_x

        members = {}
        for axis in range(self.ndim):
            members.setdefault(find(axis), []).append(axis)

        # Order groups by their smallest axis so that enumeration follows the
        # lexicographic order of the full shape tuples.
        self.group_axes = [tuple(sorted(axes))
                           for axes in sorted(members.values())]
        self.group_ranges = []
        for axes in self.group_axes:
            low = max(self.ranges[i][0] for i in axes)
            highs = [self.ranges[i][1] for i in axes
                     if self.ranges[i][1] is not None]
            high = min(highs) if highs else None
            if high is not None and high < low:
                raise ValueError(
                    "equal_axes constraints have an empty intersection")
            self.group_ranges.append((low, high))

        self.total_combinations = 1
        for low, high in self.group_ranges:
            if high is None:
                self.total_combinations = None
                break
            self.total_combinations *= high - low + 1
            if self.total_combinations > _MAX_EXHAUSTIVE_SHAPES:
                self.total_combinations = None
                break

    def iter_shapes(self, critical_lengths, *, exhaustive):
        """
        Yield allowed shape tuples in lexicographic order.

        When ``exhaustive`` is false, only a finite set of representative
        lengths per equality group is used: the group bounds, the critical
        lengths induced by the plans (and their neighbors), and a prefix of
        small lengths.  Normalized NumPy indices only change their clipping
        behavior at those critical lengths, while modular/parity behavior is
        fixed in between, so the representatives retain the earliest
        counterexample.
        """
        value_lists = []
        for low, high in self.group_ranges:
            if exhaustive:
                values = range(low, high + 1)
            else:
                values = self._representative_lengths(low, high,
                                                       critical_lengths)
            value_lists.append(list(values))

        for combo in product(*value_lists):
            shape = [0] * self.ndim
            for axes, value in zip(self.group_axes, combo):
                for axis in axes:
                    shape[axis] = value
            yield tuple(shape)

    @staticmethod
    def _representative_lengths(low, high, critical_lengths):
        values = {low}
        if high is not None:
            values.update({high, max(low, high - 1)})
        # Prefix of small lengths covers parity and short-axis behavior.
        for delta in range(1, 8):
            candidate = low + delta
            if high is None or candidate <= high:
                values.add(candidate)
        for point in critical_lengths:
            for candidate in (point - 1, point, point + 1):
                if candidate >= low and (high is None or candidate <= high):
                    values.add(candidate)
        if high is None:
            tail = max(critical_lengths, default=8) + 4
            values.update({max(low, tail), max(low, tail + 1)})
        return sorted(values)


def _as_int(value, what):
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an integer")
    return value


# ---------------------------------------------------------------------------
# Raw index normalization and critical lengths
# ---------------------------------------------------------------------------


def _normalize_step(step):
    if isinstance(step, NDIndexCommon):
        step = step.raw
    elif isinstance(step, tuple) and any(
            isinstance(item, NDIndexCommon) for item in step):
        step = tuple(item.raw if isinstance(item, NDIndexCommon) else item
                     for item in step)
    if isinstance(step, bool):
        raise TypeError("boolean scalar indices are not supported")
    if isinstance(step, tuple):
        ellipsis_count = sum(1 for item in step if item is ...)
        if ellipsis_count > 1:
            raise IndexError("an index can only have a single ellipsis ('...')")
        for item in step:
            _check_supported(item)
        return step
    _check_supported(step)
    return (step,)


def _check_supported(item):
    if item is None or item is ... or isinstance(item, slice) or isinstance(item, int):
        if isinstance(item, bool):
            raise TypeError("boolean scalar indices are not supported")
        return
    if isinstance(item, IntegerArray):
        return
    if isinstance(item, BooleanArray):
        raise TypeError("boolean array indices are not supported")
    try:
        converted = ndindex(item)
    except IndexError:
        raise
    if isinstance(converted, BooleanArray):
        raise TypeError("boolean array indices are not supported")
    if not isinstance(converted, IntegerArray):
        raise TypeError(
            f"unsupported index element of type {type(item).__name__}")


def _critical_lengths(plan_a, plan_b, spec):
    """
    Lengths at which any negative-index, stop-clipping or stride behavior
    changes, gathered across every step of both plans.
    """
    points = set()
    for plan in (plan_a, plan_b):
        for step in plan.steps:
            for item in step:
                points.update(_item_critical_points(item))
    lows = [range_[0] for range_ in spec.ranges]
    return {point for point in points if any(low <= point for low in lows)}


def _item_critical_points(item):
    if isinstance(item, int) and not isinstance(item, bool):
        return {abs(item), abs(item) + 1}
    if isinstance(item, slice):
        points = set()
        for bound in (item.start, item.stop):
            if bound is not None:
                points.update({abs(bound), abs(bound) + 1})
        if item.step is not None:
            step = abs(item.step)
            points.update({step, step + 1, 2 * step, 2 * step + 1})
        return points
    array = _as_integer_array(item)
    if array is None:
        return set()
    flat = array.array.flat
    return ({abs(int(value)) for value in flat}
            | {abs(int(value)) + 1 for value in array.array.flat})


def _as_integer_array(item):
    if isinstance(item, IntegerArray):
        return item
    if isinstance(item, (list, tuple)):
        converted = ndindex(item)
        if isinstance(converted, IntegerArray):
            return converted
    return None


# ---------------------------------------------------------------------------
# Symbolic indexing engine
# ---------------------------------------------------------------------------


class _StepMap:
    """
    Lazy mapping from output coordinates to input coordinates for one indexing
    step on an array of a concrete shape.

    ``selectors`` holds one entry per input axis, in input axis order:

    - ``('int', value)``: the axis is removed, selecting ``value``;
    - ``('array', ndarray)``: advanced index, broadcast to the block shape;
    - ``('slice', start, step, out_position)``: affine selection read from the
      output axis at ``out_position``.

    ``block_axes`` lists the output axes belonging to the advanced-index block.
    """

    def __init__(self, in_shape, out_shape, selectors, block_axes):
        self.in_shape = tuple(in_shape)
        self.out_shape = tuple(out_shape)
        self.selectors = tuple(selectors)
        self.block_axes = tuple(block_axes)

    def forward(self, out_coord):
        if len(out_coord) != len(self.out_shape):
            raise IndexError("output coordinate has the wrong number of axes")
        block_coord = tuple(out_coord[pos] for pos in self.block_axes)
        source = []
        for selector in self.selectors:
            kind = selector[0]
            if kind == 'int':
                source.append(selector[1])
            elif kind == 'array':
                source.append(int(selector[1][block_coord]))
            else:
                _, start, step_value, out_position = selector
                source.append(start + step_value * out_coord[out_position])
        return tuple(source)


class _PlanMap:
    """
    Lazy composition of the step maps of one plan.
    """

    def __init__(self, in_shape, step_maps, out_shape):
        self.in_shape = tuple(in_shape)
        self.step_maps = tuple(step_maps)
        self.out_shape = tuple(out_shape)
        self.block_axes = step_maps[-1].block_axes

    def forward(self, out_coord):
        coord = tuple(out_coord)
        for step_map in reversed(self.step_maps):
            coord = step_map.forward(coord)
        return coord


def _evaluate_plan(plan, shape):
    """
    Apply every step of ``plan`` symbolically.

    Raises ``IndexError`` whose second argument is the failing step number if
    a step would be invalid for the current array shape.
    """
    current_shape = tuple(shape)
    step_maps = []
    for step_number, step in enumerate(plan.steps):
        try:
            step_map = _evaluate_step(step, current_shape)
        except IndexError as exc:
            raise IndexError(str(exc), step_number) from exc
        step_maps.append(step_map)
        current_shape = step_map.out_shape
    return _PlanMap(shape, step_maps, current_shape)


def _evaluate_step(step, in_shape):
    entries = _expand_ellipsis(step, in_shape)
    entries = _pad_implicit_axes(entries, in_shape)

    axis_entries = []
    input_axis = 0
    for entry in entries:
        if entry is None:
            continue
        axis_entries.append((input_axis, entry))
        input_axis += 1
    if input_axis > len(in_shape):
        raise IndexError(
            f"too many indices for array: array is {len(in_shape)}-dimensional, "
            f"but {input_axis} were indexed")

    # First pass: integers, slices and 0-D integer arrays are always
    # bounds-checked (NumPy treats 0-D arrays like scalar integers).  Higher
    # dimensional raw arrays are collected so their broadcast shape is known
    # before their own bounds check, since NumPy skips that check when the
    # broadcast block is empty.
    selectors = []
    raw_arrays = []
    for axis, entry in axis_entries:
        length = in_shape[axis]
        if isinstance(entry, int) and not isinstance(entry, bool):
            selectors.append(('int', _normalize_integer(entry, length, axis)))
        elif isinstance(entry, slice):
            reduced = Slice(entry).reduce(length)
            start, _stop, step_value = reduced.args
            # len() on a shape-reduced slice handles the negative-step
            # sentinel stop correctly; start + step*k remains affine.
            selectors.append(('slice', start, step_value, len(reduced)))
        else:
            raw = entry.array if isinstance(entry, IntegerArray) \
                else ndindex(entry).array
            if raw.ndim == 0:
                selectors.append(('int', _normalize_integer(
                    int(raw), length, axis)))
            else:
                selectors.append(('array-raw', raw))
                raw_arrays.append(raw)

    block_shape = _broadcast_block_shape(raw_arrays)
    empty_block = 0 in block_shape
    advanced_arrays = []
    for index, selector in enumerate(selectors):
        if selector[0] != 'array-raw':
            continue
        array = _normalize_integer_array(
            selector[1], in_shape[axis_entries[index][0]],
            axis_entries[index][0], check_bounds=not empty_block)
        selectors[index] = ('array', array)
        advanced_arrays.append(array)

    separated = _advanced_is_separated(step)
    broadcasted = {id(array): _broadcast_array(array, block_shape)
                   for array in advanced_arrays}

    out_shape = []
    block_axes = []
    block_emitted = not advanced_arrays

    def emit_block():
        for axis_index, size in enumerate(block_shape):
            block_axes.append(len(out_shape))
            out_shape.append(size)

    if separated:
        emit_block()
        block_emitted = True

    selector_index = 0
    for entry in entries:
        if entry is None:
            out_shape.append(1)
            continue
        selector = selectors[selector_index]
        if selector[0] == 'slice':
            _, start, step_value, length = selector
            out_position = len(out_shape)
            selectors[selector_index] = ('slice', start, step_value,
                                         out_position)
            out_shape.append(length)
            selector_index += 1
        elif selector[0] == 'int':
            selector_index += 1
        else:
            selectors[selector_index] = ('array',
                                         broadcasted[id(selector[1])])
            selector_index += 1
            if not block_emitted:
                emit_block()
                block_emitted = True

    if not block_emitted:
        emit_block()

    return _StepMap(in_shape, tuple(out_shape), selectors, block_axes)


def _advanced_is_separated(step):
    """
    Return whether advanced entries (integers and integer arrays) are split
    into more than one block by slices, newaxes or an ellipsis, which moves
    their result axes to the front of the output.

    Adjacent advanced entries belong to one block; a new block only begins
    when a separator appears between two advanced entries.  The ellipsis is
    examined before expansion, so even one matching zero axes still counts.
    """
    advanced_seen = False
    separator_since = False
    for entry in step:
        if entry is None or isinstance(entry, slice) or entry is ...:
            if advanced_seen:
                separator_since = True
        elif separator_since:
            return True
        else:
            advanced_seen = True
    return False


def _expand_ellipsis(step, in_shape):
    ellipsis_position = next((i for i, item in enumerate(step) if item is ...),
                             None)
    if ellipsis_position is None:
        return tuple(step)
    consumed = sum(1 for item in step if item is not None and item is not ...)
    missing = len(in_shape) - consumed
    if missing < 0:
        raise IndexError(
            f"too many indices for array: array is {len(in_shape)}-dimensional")
    return (tuple(step[:ellipsis_position])
            + (slice(None),) * missing
            + tuple(step[ellipsis_position + 1:]))


def _pad_implicit_axes(entries, in_shape):
    """
    Append implicit full slices when an index does not mention every axis,
    matching NumPy's implicit trailing ellipsis.
    """
    consumed = sum(1 for item in entries if item is not None)
    if consumed > len(in_shape):
        raise IndexError(
            f"too many indices for array: array is {len(in_shape)}-dimensional, "
            f"but {consumed} were indexed")
    return entries + (slice(None),) * (len(in_shape) - consumed)


def _normalize_integer(value, length, axis):
    if not (-length <= value < length):
        raise IndexError(
            f"index {value} is out of bounds for axis {axis} with size {length}")
    return value if value >= 0 else value + length


def _normalize_integer_array(entry, length, axis, check_bounds=True):
    if isinstance(entry, IntegerArray):
        raw = entry.array
    else:
        raw = ndindex(entry).array
    if check_bounds:
        out_of_bounds = (raw >= length) | ((raw < 0) & (raw < -length))
        if out_of_bounds.any():
            bad = int(raw[out_of_bounds][0])
            raise IndexError(
                f"index {bad} is out of bounds for axis {axis} "
                f"with size {length}")
    if (raw < 0).any():
        from numpy import where
        raw = where(raw < 0, raw + length, raw)
    return raw


def _broadcast_block_shape(advanced_arrays):
    if not advanced_arrays:
        return ()
    try:
        return broadcast_shapes(*[array.shape for array in advanced_arrays])
    except Exception as exc:
        raise IndexError(
            "shape mismatch: indexing arrays could not be broadcast together"
        ) from exc


def _broadcast_array(array, block_shape):
    if array.shape == block_shape:
        return array
    from numpy import broadcast_to
    return broadcast_to(array, block_shape)


# ---------------------------------------------------------------------------
# Plan comparison on a concrete shape
# ---------------------------------------------------------------------------


def _evaluate_both(plan_a, plan_b, shape):
    maps = []
    for plan_number, plan in enumerate((plan_a, plan_b)):
        try:
            maps.append(_evaluate_plan(plan, shape))
        except IndexError as exc:
            return None, (plan_number, exc.args[1])
    return maps, None


def _compare_on_shape(plan_a, plan_b, shape):
    maps, failure = _evaluate_both(plan_a, plan_b, shape)
    if failure is not None:
        plan_number, step_number = failure
        return MigrationReport(False, "invalid", shape=shape,
                               failing_plan=plan_number,
                               failing_step=step_number)

    map_a, map_b = maps
    if map_a.out_shape != map_b.out_shape:
        return MigrationReport(
            False, "shape_mismatch", shape=shape,
            result_shapes=(map_a.out_shape, map_b.out_shape))

    block_axes = set(map_a.block_axes) | set(map_b.block_axes)
    for coordinate in _probe_coordinates(map_a.out_shape, block_axes):
        source_a = map_a.forward(coordinate)
        source_b = map_b.forward(coordinate)
        if source_a != source_b:
            return MigrationReport(
                False, "different_cells", shape=shape,
                result_shapes=(map_a.out_shape, map_b.out_shape),
                output_coordinate=coordinate,
                source_coordinates=(source_a, source_b))
    return None


def _probe_coordinates(shape, block_axes):
    """
    Coordinates that expose any cell-wise difference.

    Block axes come from small integer index arrays and are swept in full.
    Slice axes are affine in their output coordinate at a fixed concrete
    shape, so their first two and last positions suffice; the Cartesian
    product of these per-axis sets is complete for affine differences.
    """
    if not shape:
        yield ()
        return
    if any(size == 0 for size in shape):
        # An empty result has no output positions, hence no cells to compare.
        return

    per_axis = []
    for axis, size in enumerate(shape):
        if axis in block_axes:
            per_axis.append(tuple(range(size)))
        else:
            values = {0, size - 1}
            if size > 1:
                values.add(1)
            per_axis.append(tuple(sorted(values)))

    total = 1
    for values in per_axis:
        total *= len(values)
    if total <= _MAX_PROBE_COORDS:
        for coordinate in product(*per_axis):
            yield coordinate
        return

    # Fallback for unusually large probe spaces: anchor on the all-zero and
    # all-maximum corners and vary one axis at a time.
    zero = tuple(0 for _ in shape)
    maximum = tuple(size - 1 for size in shape)
    yield zero
    if maximum != zero:
        yield maximum
    for axis, values in enumerate(per_axis):
        for value in values:
            base = list(zero)
            base[axis] = value
            yield tuple(base)
            base = list(maximum)
            base[axis] = value
            yield tuple(base)
