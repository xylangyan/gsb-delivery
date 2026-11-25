"""Tests for the reconciliation of R measurement tables."""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pytest

import rdata
from rdata.missing import R_INT_NA
from rdata.parser import parse_file
from rdata.parser._parser import (
    RObject,
    RObjectInfo,
    RObjectType,
    compact_intseq_constructor,
    compact_realseq_constructor,
)
from rdata.reconcile import (
    ArrayVectorView,
    CompactIntSeqView,
    CompactRealSeqView,
    DimensionWindow,
    SideLayout,
    column_major_strides,
    reconcile,
    vector_view,
)

if TYPE_CHECKING:
    import pathlib


def _seq_state(n: int, start: float, step: float) -> RObject:
    """Build the ALTREP state object of a compact sequence."""
    info = RObjectInfo(
        type=RObjectType.REAL,
        object=False,
        attributes=False,
        tag=False,
        gp=0,
        reference=0,
    )
    return RObject(
        info=info,
        value=np.array([n, start, step], dtype=np.float64),
        attributes=None,
    )


def test_compact_realseq_view_matches_expansion() -> None:
    """The view reads the same bits as the rdata expansion."""
    rng = np.random.default_rng(20261126)
    n_trials = 100
    for trial in range(n_trials):
        n = int(rng.integers(1, 30000))
        start = float(rng.normal(0.0, 1e4))
        step = float(rng.normal(0.0, 10.0))
        if trial % 5 == 0:
            step = 0.1
        if trial % 13 == 0:
            start, step = 0.0, 1e-3

        _, expanded = compact_realseq_constructor(_seq_state(n, start, step))
        view = CompactRealSeqView(n=n, start=start, step=step)
        assert view.length == len(expanded)

        count = min(n, 1000)
        positions = np.sort(
            rng.choice(n, size=count, replace=False),
        ).astype(np.int64)
        # Include the last position, set to stop exactly by linspace
        positions[-1] = n - 1

        values = view.values_at(positions)
        assert values.dtype == np.float64
        np.testing.assert_array_equal(
            values.view(np.uint64),
            expanded[positions].view(np.uint64),
        )


def test_compact_intseq_view_matches_expansion() -> None:
    """The view reads the same values as the rdata expansion."""
    cases = [
        (1000, 0, 1),
        (11, -5, 1),
        (8, 3, 4),
        (1, 7, 1),
        # Negative step: the rdata expansion is shorter than n
        (5, 5, -1),
    ]
    for n, start, step in cases:
        _, expanded = compact_intseq_constructor(_seq_state(n, start, step))
        view = CompactIntSeqView(n=n, start=start, step=step)
        assert view.length == len(expanded)

        positions = np.arange(view.length, dtype=np.int64)
        values = view.values_at(positions)
        assert values.dtype == np.int32
        np.testing.assert_array_equal(values, expanded)


def test_column_major_strides() -> None:
    """Strides are the cumulative products of the dimension sizes."""
    assert column_major_strides([4, 100, 7]) == (1, 4, 400)


def test_reconcile_swapped_dimensions() -> None:
    """A logical row is contiguous on one side and strided on the other."""
    # Sheet dimensions: (sensor, time); old table stored as (sensor, time),
    # new table stored as (time, sensor)
    n_sensors, n_times = 4, 3
    old = np.arange(n_sensors * n_times, dtype=np.float64)
    new = np.empty(n_sensors * n_times, dtype=np.float64)
    for sensor in range(n_sensors):
        for time in range(n_times):
            new[time + n_times * sensor] = old[sensor + n_sensors * time]

    dimensions = [
        DimensionWindow("sensor", start=1, size=2),
        DimensionWindow("time", start=0, size=3),
    ]
    old_layout = SideLayout(strides=(1, n_sensors))
    new_layout = SideLayout(strides=(n_times, 1))

    report = reconcile(
        old,
        new,
        dimensions=dimensions,
        old_layout=old_layout,
        new_layout=new_layout,
    )
    expected_cells = 6
    assert report.compared_cells == expected_cells
    assert report.different_cells == 0
    assert report.reusable
    assert report.first_difference is None

    # Perturb two logical cells: (sensor 2, time 1) and (sensor 1, time 2)
    new[1 + n_times * 2] += 1.0
    new[2 + n_times * 1] += 2.0

    report = reconcile(
        old,
        new,
        dimensions=dimensions,
        old_layout=old_layout,
        new_layout=new_layout,
    )
    expected_different = 2
    assert report.compared_cells == expected_cells
    assert report.different_cells == expected_different
    assert not report.reusable

    # The first dimension of the sheet varies fastest
    first = report.first_difference
    assert first is not None
    assert first.coordinates == (2, 1)
    assert first.old_position == 2 + n_sensors * 1
    assert first.new_position == 1 + n_times * 2
    assert first.old_value == float(old[first.old_position])
    assert first.new_value == float(old[first.old_position]) + 1.0


def test_reconcile_subwindow_against_short_compact_realseq() -> None:
    """A subwindow of a long sequence vs. a short sequence starting there."""
    n_long, start_long, step = 1_000_001, 0.0, 0.1
    offset, count = 500_000, 10_001
    start_short = offset * step

    _, expanded_long = compact_realseq_constructor(
        _seq_state(n_long, start_long, step),
    )
    _, expanded_short = compact_realseq_constructor(
        _seq_state(count, start_short, step),
    )

    old = CompactRealSeqView(n=n_long, start=start_long, step=step)
    new = CompactRealSeqView(n=count, start=start_short, step=step)

    report = reconcile(
        old,
        new,
        dimensions=[DimensionWindow("time", start=0, size=count)],
        old_layout=SideLayout(strides=(1,), offset=offset),
        new_layout=SideLayout(strides=(1,)),
    )

    expected_mismatch = (
        expanded_long[offset:offset + count].view(np.uint64)
        != expanded_short.view(np.uint64)
    )
    # The two expansions round differently somewhere in the middle,
    # even though the paper step and both ends coincide
    assert expected_mismatch.sum() > 0
    assert expanded_long[offset] == expanded_short[0]
    assert expanded_long[offset + count - 1] == expanded_short[-1]

    assert report.compared_cells == count
    assert report.different_cells == int(expected_mismatch.sum())

    first = report.first_difference
    assert first is not None
    expected_first = int(np.flatnonzero(expected_mismatch)[0])
    assert first.coordinates == (expected_first,)
    assert first.old_position == offset + expected_first
    assert first.new_position == expected_first
    assert first.old_value == float(expanded_long[first.old_position])
    assert first.new_value == float(expanded_short[expected_first])


def test_reconcile_huge_compact_segment_stays_lazy() -> None:
    """Segments with hundreds of millions of points are not expanded."""
    n = 300_000_000
    old = CompactRealSeqView(n=n, start=-1.5, step=0.25)
    assert old.length == n

    # Exact binary step: the subwindow and the short sequence coincide
    offset, count = 150_000_000, 1_000
    new = CompactRealSeqView(n=count, start=-1.5 + offset * 0.25, step=0.25)
    report = reconcile(
        old,
        new,
        dimensions=[DimensionWindow("time", start=0, size=count)],
        old_layout=SideLayout(strides=(1,), offset=offset),
        new_layout=SideLayout(strides=(1,)),
    )
    assert report.compared_cells == count
    assert report.different_cells == 0
    assert report.reusable

    # Inexact step: the evidence must point to readable positions
    old = CompactRealSeqView(n=n, start=-1.5, step=0.1)
    new = CompactRealSeqView(n=count, start=-1.5 + offset * 0.1, step=0.1)
    report = reconcile(
        old,
        new,
        dimensions=[DimensionWindow("time", start=0, size=count)],
        old_layout=SideLayout(strides=(1,), offset=offset),
        new_layout=SideLayout(strides=(1,)),
    )
    assert report.compared_cells == count
    first = report.first_difference
    if report.different_cells:
        assert first is not None
        assert 0 <= first.new_position < count
        assert first.old_position == offset + first.new_position
        old_value = old.values_at(np.array([first.old_position]))[0]
        new_value = new.values_at(np.array([first.new_position]))[0]
        assert old_value.view(np.uint64) != new_value.view(np.uint64)
        assert first.old_value == float(old_value)
        assert first.new_value == float(new_value)


def test_reconcile_shared_compact_vector_multiple_windows() -> None:
    """Independent windows can share one original compact vector."""
    n = 1000
    _, expanded = compact_intseq_constructor(_seq_state(n, 0, 1))
    shared = CompactIntSeqView(n=n, start=0, step=1)

    windows = [
        (10, 5, 1),
        (100, 4, 3),
        (500, 3, 7),
    ]
    for offset, count, stride in windows:
        positions = offset + stride * np.arange(count)
        plain = expanded[positions]
        report = reconcile(
            shared,
            ArrayVectorView(plain),
            dimensions=[DimensionWindow("cell", start=0, size=count)],
            old_layout=SideLayout(strides=(stride,), offset=offset),
            new_layout=SideLayout(strides=(1,)),
        )
        assert report.reusable

    # A wrong value in one window is detected with its original position
    offset, count, stride = windows[1]
    changed_cell = 2
    plain = expanded[offset + stride * np.arange(count)].copy()
    plain[changed_cell] += 1
    report = reconcile(
        shared,
        ArrayVectorView(plain),
        dimensions=[DimensionWindow("cell", start=0, size=count)],
        old_layout=SideLayout(strides=(stride,), offset=offset),
        new_layout=SideLayout(strides=(1,)),
    )
    assert report.different_cells == 1
    first = report.first_difference
    assert first is not None
    assert first.old_position == offset + stride * changed_cell
    assert first.new_position == changed_cell
    assert first.old_value == int(expanded[first.old_position])
    assert first.new_value == int(expanded[first.old_position]) + 1


def test_reconcile_from_rds_files(tmp_path: pathlib.Path) -> None:
    """RDS files are parsed lazily: compact sequences are not expanded."""
    old_path = tmp_path / "old.rds"
    new_path = tmp_path / "new.rds"

    rdata.write_rds(old_path, pd.RangeIndex(0, 1000))
    rdata.write_rds(new_path, np.arange(10, 20, dtype=np.int32))

    view = vector_view(old_path)
    assert isinstance(view, CompactIntSeqView)
    expected_length = 1000
    assert view.length == expected_length

    report = reconcile(
        old_path,
        new_path,
        dimensions=[DimensionWindow("cell", start=0, size=10)],
        old_layout=SideLayout(strides=(1,), offset=10),
        new_layout=SideLayout(strides=(1,)),
    )
    assert report.reusable

    # Plain vectors are read natively, cell by cell
    rdata.write_rds(old_path, np.arange(10, 20, dtype=np.float64))
    rdata.write_rds(new_path, np.arange(10, 20, dtype=np.float64) + 0.5)
    report = reconcile(
        old_path,
        new_path,
        dimensions=[DimensionWindow("cell", start=0, size=10)],
        old_layout=SideLayout(strides=(1,)),
        new_layout=SideLayout(strides=(1,)),
    )
    expected_different = 10
    assert report.different_cells == expected_different
    first = report.first_difference
    assert first is not None
    assert first.old_value == float(10)
    assert first.new_value == float(10) + 0.5


def test_reconcile_collects_all_differences() -> None:
    """max_differences=None collects the evidence of every cell."""
    old = np.zeros(5, dtype=np.float64)
    new = np.array([1.0, 0.0, 2.0, 0.0, 3.0])
    dimensions = [DimensionWindow("cell", start=0, size=5)]
    layout = SideLayout(strides=(1,))

    report = reconcile(
        old,
        new,
        dimensions=dimensions,
        old_layout=layout,
        new_layout=layout,
        max_differences=None,
    )
    expected_different = 3
    assert report.different_cells == expected_different
    assert len(report.differences) == expected_different
    assert [d.new_position for d in report.differences] == [0, 2, 4]

    report = reconcile(
        old,
        new,
        dimensions=dimensions,
        old_layout=layout,
        new_layout=layout,
        max_differences=2,
    )
    expected_collected = 2
    assert report.different_cells == expected_different
    assert len(report.differences) == expected_collected


def test_reconcile_real_bits_signed_zero() -> None:
    """Reals are compared by their bits: 0.0 and -0.0 differ."""
    report = reconcile(
        np.array([0.0]),
        np.array([-0.0]),
        dimensions=[DimensionWindow("cell", start=0, size=1)],
        old_layout=SideLayout(strides=(1,)),
        new_layout=SideLayout(strides=(1,)),
    )
    assert report.different_cells == 1
    first = report.first_difference
    assert first is not None
    assert first.old_value == 0.0
    assert first.new_value == -0.0


def test_reconcile_masked_int_vector() -> None:
    """Integer NA sentinels compare equal through the raw values."""
    old = np.ma.masked_array(
        np.array([1, R_INT_NA, 3], dtype=np.int32),
        mask=[False, True, False],
    )
    new = np.array([1, R_INT_NA, 4], dtype=np.int32)
    report = reconcile(
        old,
        new,
        dimensions=[DimensionWindow("cell", start=0, size=3)],
        old_layout=SideLayout(strides=(1,)),
        new_layout=SideLayout(strides=(1,)),
    )
    assert report.different_cells == 1
    first = report.first_difference
    assert first is not None
    expected_position = 2
    assert first.new_position == expected_position


def test_reconcile_kind_mismatch() -> None:
    """Integer and real vectors cannot be reconciled."""
    with pytest.raises(ValueError, match="Cannot reconcile"):
        reconcile(
            np.array([1], dtype=np.int32),
            np.array([1.0]),
            dimensions=[DimensionWindow("cell", start=0, size=1)],
            old_layout=SideLayout(strides=(1,)),
            new_layout=SideLayout(strides=(1,)),
        )


def test_reconcile_non_finite_raises() -> None:
    """Only finite real values are accepted in the window."""
    old = np.array([1.0, np.inf])
    new = np.array([1.0, 2.0])
    with pytest.raises(ValueError, match="Non-finite"):
        reconcile(
            old,
            new,
            dimensions=[DimensionWindow("cell", start=0, size=2)],
            old_layout=SideLayout(strides=(1,)),
            new_layout=SideLayout(strides=(1,)),
        )

    # Non-finite values outside the window are never read
    report = reconcile(
        old,
        new,
        dimensions=[DimensionWindow("cell", start=0, size=1)],
        old_layout=SideLayout(strides=(1,)),
        new_layout=SideLayout(strides=(1,)),
    )
    assert report.reusable


def test_reconcile_out_of_bounds() -> None:
    """Windows outside a vector are rejected before comparing."""
    with pytest.raises(ValueError, match="length"):
        reconcile(
            np.arange(5, dtype=np.float64),
            np.arange(5, dtype=np.float64),
            dimensions=[DimensionWindow("cell", start=0, size=6)],
            old_layout=SideLayout(strides=(1,)),
            new_layout=SideLayout(strides=(1,)),
        )


def test_reconcile_first_dimension_varies_fastest() -> None:
    """The first difference follows the sheet order, first dim fastest."""
    old = np.zeros(6, dtype=np.float64)
    new = np.zeros(6, dtype=np.float64)
    # Cells (a, b) with a in {0, 1}, b in {0, 1, 2}, column-major storage
    new[0 + 2 * 1] = 1.0  # (a=0, b=1)
    new[1 + 2 * 0] = 1.0  # (a=1, b=0), encountered first

    report = reconcile(
        old,
        new,
        dimensions=[
            DimensionWindow("a", start=0, size=2),
            DimensionWindow("b", start=0, size=3),
        ],
        old_layout=SideLayout(strides=(1, 2)),
        new_layout=SideLayout(strides=(1, 2)),
    )
    expected_different = 2
    assert report.different_cells == expected_different
    first = report.first_difference
    assert first is not None
    assert first.coordinates == (1, 0)
    assert first.old_position == 1
    assert first.new_position == 1


def test_vector_view_from_parsed_altrep() -> None:
    """Parsed ALTREP objects keep their compact representation."""
    parsed = parse_file(
        rdata.TESTDATA_PATH
        / "generated/test_altrep_compact_intseq__xdr__version_3.rds",
        expand_altrep=False,
    )
    view = vector_view(parsed)
    assert isinstance(view, CompactIntSeqView)
    expected_length = 1000
    assert view.length == expected_length
    positions = np.array([0, 500, 999], dtype=np.int64)
    np.testing.assert_array_equal(
        view.values_at(positions),
        np.array([0, 500, 999], dtype=np.int32),
    )

    # Already expanded objects are viewed as plain vectors
    parsed = parse_file(
        rdata.TESTDATA_PATH
        / "generated/test_altrep_compact_intseq__xdr__version_3.rds",
    )
    view = vector_view(parsed)
    assert isinstance(view, ArrayVectorView)
    np.testing.assert_array_equal(
        view.values_at(positions),
        np.array([0, 500, 999], dtype=np.int32),
    )
