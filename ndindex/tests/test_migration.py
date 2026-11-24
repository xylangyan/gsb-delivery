import itertools

import numpy as np
import pytest
from hypothesis import given, assume, settings
from hypothesis import strategies as st

from .. import IndexPlan, verify_migration, ndindex, Slice, Integer
from ..migration import _evaluate_plan, _probe_coordinates
from .._crt import prod


# ---------------------------------------------------------------------------
# Fixed scenarios from the migration requirements
# ---------------------------------------------------------------------------


def test_reverse_stride_vs_stride_reverse():
    reverse_then_stride = IndexPlan(
        [slice(None, None, -1), slice(0, None, 2)])
    stride_then_reverse = IndexPlan(
        [slice(0, None, 2), slice(None, None, -1)])

    for length in (1, 3, 5):
        report = verify_migration(reverse_then_stride, stride_then_reverse,
                                  [(length, length)])
        assert report.compatible

    report = verify_migration(reverse_then_stride, stride_then_reverse,
                              [(1, 8)])
    assert not report.compatible
    assert report.reason == "different_cells"
    # The earliest even length is the first counterexample.
    assert report.shape == (2,)
    assert report.output_coordinate == (0,)
    assert report.source_coordinates == ((1,), (0,))


def test_odd_even_odd_axis_ranges_keep_lexicographic_order():
    # Two odd sample lengths with an even length sandwiched between them.
    plan_a = IndexPlan([slice(None, None, -1), slice(0, None, 2)])
    plan_b = IndexPlan([slice(0, None, 2), slice(None, None, -1)])
    report = verify_migration(plan_a, plan_b, [(3, 5)])
    assert report.shape == (4,)
    assert not report.compatible


def test_separated_advanced_indices_two_by_two_block():
    indices = np.array([0, 1])
    separated = IndexPlan([(indices, slice(None), indices)])
    contiguous = IndexPlan([(indices, indices, slice(None))])

    report = verify_migration(separated, contiguous,
                              [(2, 3), (2, 3), (2, 3)],
                              equal_axes=[(0, 2)])
    assert not report.compatible
    assert report.reason == "different_cells"
    # Both result shapes are (2, 2): shape comparison alone cannot see this.
    assert report.result_shapes == ((2, 2), (2, 2))
    assert report.shape == (2, 2, 2)
    coordinate = report.output_coordinate
    array = np.arange(8).reshape(2, 2, 2)
    assert array[report.source_coordinates[0]] == \
        array[indices, :, indices][coordinate]
    assert array[report.source_coordinates[1]] == \
        array[indices, indices, :][coordinate]
    assert report.source_coordinates[0] != report.source_coordinates[1]


def test_split_steps_vs_merged_operation():
    indices = np.array([0, 1])
    split = IndexPlan([(indices, slice(None)), (slice(None), indices)])
    merged = IndexPlan([(indices, slice(None), indices)])
    report = verify_migration(split, merged, [(2, 3), (2, 3), (2, 3)],
                              equal_axes=[(0, 2)])
    assert not report.compatible
    # The merged operation moves the separated advanced block to the front,
    # which even changes the result shape here.
    assert report.reason == "shape_mismatch"


def test_split_steps_vs_merged_operation_detects_integer_separation():
    indices = np.array([0, 1])
    # a[0][:, I] keeps the block at the end, while the merged a[0, :, I]
    # moves it to the front because the integer and the array sit in
    # different advanced blocks.  The cells therefore differ position-wise.
    split = IndexPlan([0, (slice(None), indices)])
    merged = IndexPlan([(0, slice(None), indices)])
    report = verify_migration(split, merged, [(2, 3), (2, 3), (2, 3)],
                              equal_axes=[(1, 2)])
    assert not report.compatible
    assert report.reason == "different_cells"
    assert report.result_shapes[0] == report.result_shapes[1]


def test_basic_split_steps_match_merged_operation():
    # a[::-1][0] selects the same cell as a[-1].
    split = IndexPlan([slice(None, None, -1), 0])
    merged = IndexPlan([-1])
    report = verify_migration(split, merged, [(2, 5)])
    assert report.compatible
    # a[:, ::-1][:, 0] == a[:, -1]
    split = IndexPlan([(slice(None), slice(None, None, -1)),
                       (slice(None), 0)])
    merged = IndexPlan([(slice(None), -1)])
    report = verify_migration(split, merged, [(2, 5), (2, 5)])
    assert report.compatible


def test_invalid_step_is_reported_with_step_number():
    bad_plan = IndexPlan([0, (slice(None), 0)])
    good_plan = IndexPlan([(0, 0)])
    report = verify_migration(bad_plan, good_plan, [(2, 4), (2, 4)])
    assert not report.compatible
    assert report.reason == "invalid"
    assert report.failing_plan == 0
    assert report.failing_step == 1
    assert report.shape == (2, 2)


def test_result_shape_mismatch():
    indices = np.array([0, 1])
    report = verify_migration(IndexPlan([indices]),
                              IndexPlan([(indices, indices)]),
                              [(2, 4), (2, 4)], equal_axes=[(0, 1)])
    assert report.reason == "shape_mismatch"
    assert report.result_shapes == ((2, 2), (2,))


def test_identical_plans_are_compatible_on_unbounded_axis():
    plan = IndexPlan([slice(None, None, -1), (slice(1, None),)])
    report = verify_migration(plan, plan, [(1, None)])
    assert report.compatible
    assert report.reason == "compatible"
    assert report.shape is None


def test_equal_axes_constraint_is_respected():
    plan_a = IndexPlan([(slice(None), 0)])
    plan_b = IndexPlan([(0, slice(None))])
    report = verify_migration(plan_a, plan_b, [(2, 4), (2, 4)],
                              equal_axes=[(0, 1)])
    assert not report.compatible
    assert report.shape[0] == report.shape[1]


def test_report_summary_is_readable():
    report = verify_migration(
        IndexPlan([0]), IndexPlan([1]), [(2, 3)])
    assert not report.compatible
    assert "shape" in report.summary()


def test_ndindex_leaf_objects_are_accepted():
    # Reversing then taking the first element equals indexing with -1.
    plan = IndexPlan([Slice(None, None, -1), Integer(0)])
    report = verify_migration(plan, IndexPlan([-1]), [(3, 5)])
    assert report.compatible
    report = verify_migration(plan, IndexPlan([1]), [(3, 5)])
    assert not report.compatible


def test_boolean_indices_are_rejected():
    with pytest.raises(TypeError):
        IndexPlan([np.array([True, False])])
    with pytest.raises(TypeError):
        IndexPlan([(np.array([0, 1]), True)])


def test_empty_plan_is_rejected():
    with pytest.raises(ValueError):
        IndexPlan([])


def test_bad_shape_specification():
    with pytest.raises(ValueError):
        verify_migration(IndexPlan([0]), IndexPlan([0]), [(3, 2)])
    with pytest.raises(ValueError):
        verify_migration(IndexPlan([0]), IndexPlan([0]), [(2, 3)],
                         equal_axes=[(0, 5)])


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------


small_ints = st.integers(-4, 6)
slices = st.builds(
    slice,
    st.one_of(st.none(), small_ints),
    st.one_of(st.none(), small_ints),
    st.one_of(st.none(), st.integers(-3, -1), st.integers(1, 3)))
shapes = st.lists(st.integers(0, 5), min_size=1, max_size=3).map(tuple)


@st.composite
def integer_arrays(draw):
    array_shape = tuple(draw(st.integers(0, 3))
                        for _ in range(draw(st.integers(0, 2))))
    size = prod(array_shape) if array_shape else 1
    flat = draw(st.lists(st.integers(-4, 6), min_size=size, max_size=size))
    if array_shape:
        return np.array(flat, dtype=np.intp).reshape(array_shape)
    return np.array(flat[0], dtype=np.intp)


@st.composite
def index_steps(draw, max_entries=4):
    entry = st.one_of(small_ints, slices, st.just(None), st.just(...),
                      integer_arrays())
    entries = [draw(entry) for _ in range(draw(st.integers(1, max_entries)))]
    cleaned = []
    ellipsis_seen = False
    for item in entries:
        if item is ...:
            if ellipsis_seen:
                continue
            ellipsis_seen = True
        cleaned.append(item)
    if draw(st.booleans()):
        return tuple(cleaned)
    return cleaned[0]


@st.composite
def plans(draw, max_steps=2):
    steps = [draw(index_steps())
             for _ in range(draw(st.integers(1, max_steps)))]
    return IndexPlan(steps)


@st.composite
def equal_axis_pairs(draw, ndim):
    count = draw(st.integers(0, 2))
    pairs = []
    for _ in range(count):
        x = draw(st.integers(0, ndim - 1))
        y = draw(st.integers(0, ndim - 1))
        if x != y:
            pairs.append((x, y))
    return pairs


bounded_ranges = st.lists(
    st.builds(lambda low, extra: (low, low + extra),
              st.integers(1, 3), st.integers(0, 2)),
    min_size=1, max_size=2)


def numpy_apply(plan, shape):
    array = np.arange(int(prod(shape)), dtype=np.int64).reshape(shape)
    current = array
    for step in plan.steps:
        current = current[step]
    return array, current


# ---------------------------------------------------------------------------
# Engine vs NumPy property tests
# ---------------------------------------------------------------------------


@given(plan=plans(), shape=shapes)
@settings(max_examples=400, deadline=None)
def test_engine_matches_numpy(plan, shape):
    if 0 in shape:
        assume(False)
    try:
        array, reference = numpy_apply(plan, shape)
    except (IndexError, ValueError):
        with pytest.raises((IndexError, ValueError)):
            _evaluate_plan(plan, shape)
        return

    plan_map = _evaluate_plan(plan, shape)
    assert plan_map.out_shape == reference.shape

    if reference.ndim == 0:
        coordinates = [()]
    elif reference.size == 0:
        coordinates = []
    elif reference.size <= 64:
        coordinates = list(itertools.product(
            *[range(size) for size in reference.shape]))
    else:
        coordinates = list(_probe_coordinates(
            reference.shape, set(plan_map.block_axes)))

    for coordinate in coordinates:
        source = plan_map.forward(coordinate)
        assert array[source] == reference[coordinate]


@given(plan=plans(), shape=shapes)
@settings(max_examples=200, deadline=None)
def test_failing_step_number_matches_numpy(plan, shape):
    if 0 in shape:
        assume(False)
    numpy_step = None
    current = np.zeros(shape, dtype=np.int64)
    for step_number, step in enumerate(plan.steps):
        try:
            current = current[step]
        except (IndexError, ValueError):
            numpy_step = step_number
            break

    if numpy_step is None:
        _evaluate_plan(plan, shape)
    else:
        with pytest.raises(IndexError) as exc_info:
            _evaluate_plan(plan, shape)
        assert exc_info.value.args[1] == numpy_step


# ---------------------------------------------------------------------------
# Brute-force cross-check of verify_migration over every allowed shape
# ---------------------------------------------------------------------------


def numpy_source_cells(plan, shape):
    # Values of this array are the flat index of their own cell, so reading a
    # result value tells us exactly which original cell was selected.
    array = np.arange(int(prod(shape)), dtype=np.int64).reshape(shape)
    current = array
    for step in plan.steps:
        current = current[step]
    if current.ndim == 0:
        return current.shape, {(): tuple(np.unravel_index(int(current), shape))}
    cells = {}
    for coordinate in itertools.product(*[range(size) for size in current.shape]):
        cells[coordinate] = tuple(
            np.unravel_index(int(current[coordinate]), shape))
    return current.shape, cells


def brute_force_verdict(plan_a, plan_b, allowed_shapes):
    for shape in allowed_shapes:
        outcomes = []
        for plan in (plan_a, plan_b):
            try:
                result_shape, cells = numpy_source_cells(plan, shape)
            except (IndexError, ValueError):
                outcomes.append(None)
            else:
                outcomes.append((result_shape, cells))

        if any(outcome is None for outcome in outcomes):
            plan_number = next(i for i, outcome in enumerate(outcomes)
                               if outcome is None)
            return ("invalid", shape, plan_number)
        if outcomes[0][0] != outcomes[1][0]:
            return ("shape_mismatch", shape, outcomes[0][0], outcomes[1][0])
        if outcomes[0][1] != outcomes[1][1]:
            coordinate = next(
                coord for coord in outcomes[0][1]
                if outcomes[0][1][coord] != outcomes[1][1][coord])
            return ("different_cells", shape, coordinate,
                    outcomes[0][1][coordinate], outcomes[1][1][coordinate])
    return ("compatible",)


def allowed_shape_list(axis_ranges, equal_axes):
    ranges = [range(low, high + 1) for low, high in axis_ranges]
    enumerated = itertools.product(*ranges)
    return sorted(shape for shape in enumerated
                  if all(shape[x] == shape[y] for x, y in equal_axes))


@given(axis_ranges=bounded_ranges,
       equal_axes=st.data(),
       plan_a=plans(max_steps=2),
       plan_b=plans(max_steps=2))
@settings(max_examples=300, deadline=None)
def test_verify_migration_matches_brute_force(axis_ranges, equal_axes,
                                              plan_a, plan_b):
    ndim = len(axis_ranges)
    pairs = equal_axes.draw(equal_axis_pairs(ndim))
    feasible = all(
        max(axis_ranges[x][0], axis_ranges[y][0])
        <= min(axis_ranges[x][1], axis_ranges[y][1])
        for x, y in pairs)
    assume(feasible)
    allowed = allowed_shape_list(axis_ranges, pairs)

    expected = brute_force_verdict(plan_a, plan_b, allowed)
    report = verify_migration(plan_a, plan_b, axis_ranges, equal_axes=pairs)

    if expected[0] == "compatible":
        assert report.compatible
        return

    assert not report.compatible
    assert report.reason == expected[0]
    assert report.shape == expected[1]
    if expected[0] == "invalid":
        assert report.failing_plan == expected[2]
    elif expected[0] == "shape_mismatch":
        assert report.result_shapes == (expected[2], expected[3])
    else:
        assert report.output_coordinate == expected[2]
        assert report.source_coordinates == (expected[3], expected[4])


def test_representative_mode_keeps_early_counterexample(monkeypatch):
    import ndindex.migration as migration
    # Force the representative path even for small shape counts.
    monkeypatch.setattr(migration, "_MAX_EXHAUSTIVE_SHAPES", 4)

    plan_a = IndexPlan([slice(None, None, -1), slice(0, None, 2)])
    plan_b = IndexPlan([slice(0, None, 2), slice(None, None, -1)])
    axis_ranges = [(1, 40)]
    expected = brute_force_verdict(
        plan_a, plan_b, allowed_shape_list(axis_ranges, []))
    report = verify_migration(plan_a, plan_b, axis_ranges)
    assert report.reason == expected[0]
    assert report.shape == expected[1]


def test_representative_mode_uses_critical_lengths_beyond_prefix(monkeypatch):
    import ndindex.migration as migration
    monkeypatch.setattr(migration, "_MAX_EXHAUSTIVE_SHAPES", 4)

    # These plans agree for every N <= 100 and diverge first at N = 101,
    # which lies beyond the small-length prefix, so the critical-length
    # machinery (not exhaustive enumeration) must produce the answer.
    plan_a = IndexPlan([slice(0, 100)])
    plan_b = IndexPlan([slice(None)])
    axis_ranges = [(1, 150)]
    expected = brute_force_verdict(
        plan_a, plan_b, allowed_shape_list(axis_ranges, []))
    report = verify_migration(plan_a, plan_b, axis_ranges)
    assert expected[0] == "shape_mismatch"
    assert report.reason == "shape_mismatch"
    assert report.shape == (101,)


def test_probe_coordinates_sweep_block_axes_fully():
    coordinates = list(_probe_coordinates((2, 3), {0}))
    assert (0, 0) in coordinates and (1, 2) in coordinates
    assert len(coordinates) == 2 * 3
    assert list(_probe_coordinates((2, 0), {0})) == []
    assert list(_probe_coordinates((), set())) == [()]


def test_ndindex_constructor_roundtrip():
    from ndindex import Tuple
    plan = IndexPlan([ndindex[::-1], Tuple(ndindex[0:3])])
    assert all(isinstance(step, tuple) for step in plan.steps)
    report = verify_migration(plan, IndexPlan([slice(None, None, -1),
                                               slice(0, 3)]),
                              [(4, 6)])
    assert report.compatible
