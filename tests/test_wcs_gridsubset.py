from datetime import datetime

from owslib.coverage.gridsubset import ClipRange, GridSubsetError, IndexRange
from owslib.coverage.gridsubset import plan_grid_batches
from owslib.coverage.gridsubset import plan_grid_subset, plan_subset_from_subsets
from owslib.coverage.wcs201 import Grid, ReferenceableGridByVectors
from owslib.etree import etree


def make_grid(**overrides):
    values = {
        "axislabels": ["Lat", "Long", "ansi"],
        "lowlimits": ["0", "0", "0"],
        "highlimits": ["1799", "3599", "184"],
        "origin": ["90", "-180", "2000-01-01T00:00:00Z"],
        "offsetvectors": [["-0.1", "0", "0"], ["0", "0.1", "0"], ["0", "0", "1"]],
    }
    values.update(overrides)

    class _Grid:
        pass

    grid = _Grid()
    for key, value in values.items():
        setattr(grid, key, value)
    return grid


def test_storage_axis_order_differs_from_request_order():
    grid = make_grid()
    plan = plan_subset_from_subsets(
        grid,
        [("Long", -10, 0), ("Lat", 40, 50), ("ansi", "2000-02-01T00:00:00Z")],
    )
    assert tuple(plan.axes) == ("Lat", "Long", "ansi")
    assert plan.request_axes == ("Long", "Lat", "ansi")
    assert plan.index_ranges == {
        "Lat": IndexRange(400, 499),
        "Long": IndexRange(1700, 1799),
        "ansi": IndexRange(30, 30),
    }
    assert plan.request_index_ranges == (
        IndexRange(1700, 1799),
        IndexRange(400, 499),
        IndexRange(30, 30),
    )


def test_negative_step_boxes_cover_requested_edges():
    grid = make_grid()
    plan = plan_grid_subset(grid, bbox=(-10, 40, 0, 50), times=None)
    assert plan.index_ranges["Long"] == (1700, 1799)
    # Latitude runs from +90 downward (negative offset), so 50..40 maps to the
    # low end of the native index range.
    assert plan.index_ranges["Lat"] == (400, 499)
    # Nudging the request inside the cell edges produces partial corner cells;
    # they must be reported as unproven rather than dropped or zero-filled.
    plan = plan_grid_subset(grid, bbox=(-9.95, 40.05, -0.05, 49.95), times=None)
    mask = plan.coverage_mask
    assert mask[(1700, 400)][1] == "partial"
    assert abs(mask[(1700, 400)][0] - 0.25) < 1e-6
    assert mask[(1700, 499)][1] == "partial"
    corner = plan.cells[0]
    assert corner.coverage == "partial"
    assert corner.proven is False
    assert corner.value is None


def test_true_cell_positions_follow_full_grid_transform():
    grid = make_grid()
    plan = plan_subset_from_subsets(
        grid,
        [("Long", -10, 0), ("Lat", 40, 50), ("ansi", "2000-02-01T00:00:00Z")],
    )
    cell = plan.cells[0]
    assert cell.storage_index == (400, 1700, 30)
    assert cell.xy == (-9.949999999999989, 49.949999999999996)
    assert cell.time == datetime(2000, 1, 31, 12, 0)
    assert cell.center["Long"] == cell.xy[0]
    assert cell.center["Lat"] == cell.xy[1]


def test_rotated_grid_uses_both_offsets_and_keeps_positions():
    grid = make_grid(
        axislabels=["Long", "Lat"],
        lowlimits=["0", "0"],
        highlimits=["3", "3"],
        origin=["0", "0"],
        offsetvectors=[["1", "0.5", "0"], ["-0.5", "1", "0"]],
    )
    plan = plan_grid_subset(grid, bbox=(0, 0, 4, 3))
    assert plan.index_ranges["Long"] == (0, 3)
    assert plan.index_ranges["Lat"] == (0, 2)
    first = plan.cells[0]
    # Rotated cells move in both coordinates; the position must reflect the
    # full transform, not a single-axis trace.
    assert first.xy == (0.25, 0.75)
    assert first.coverage == "partial"
    assert plan.cells[1].xy == (-0.25, 1.75)


def test_map_of_request_indices_to_storage_indices_roundtrip():
    grid = make_grid()
    plan = plan_subset_from_subsets(
        grid,
        [("Long", -10, 0), ("Lat", 40, 50), ("ansi", "2000-02-01T00:00:00Z")],
    )
    storage = plan.storage_index_of((1700, 400, 30))
    assert storage == (400, 1700, 30)
    assert plan.request_index_of(storage) == (1700, 400, 30)
    assert plan.storage_index_of({"Long": 1700, "Lat": 400, "ansi": 30}) == (
        400,
        1700,
        30,
    )
    cell = plan.cell_at((1700, 400, 30))
    assert cell.request_index == (1700, 400, 30)


def test_unsupported_axis_reports_read_and_clip_ranges():
    grid = make_grid()
    plan = plan_subset_from_subsets(
        grid,
        [("Long", -10, 0), ("Lat", 40, 50), ("ansi", "2000-02-01T00:00:00Z")],
        supported_axes=["Long", "Lat"],
    )
    assert plan.unsupported_axes == ("ansi",)
    time_axis = plan.axis("ansi")
    assert time_axis.read_range == IndexRange(0, 184)
    assert time_axis.index_range == IndexRange(30, 30)
    assert time_axis.clip_range == ClipRange(30, 31)
    assert plan.needs_local_clip is True
    assert plan.read_ranges["Lat"] == plan.index_ranges["Lat"]


def test_irregular_time_axis_selects_listed_instants():
    grid = make_grid(
        axislabels=["Lat", "Long", "ansi"],
        lowlimits=["0", "0", "0"],
        highlimits=["2", "2", "4"],
        origin=["2", "0", "2015-01-01T00:00:00Z"],
        offsetvectors=[["-1", "0", "0"], ["0", "1", "0"], ["0", "0", "0"]],
        axis_coefficients={
            "ansi": [
                '"2015-01-01T00:00:00Z"',
                '"2015-02-01T00:00:00Z"',
                '"2015-04-01T00:00:00Z"',
                '"2015-07-01T00:00:00Z"',
                '"2016-01-01T00:00:00Z"',
            ]
        },
    )
    plan = plan_grid_subset(grid, times=["2015-04-01T00:00:00Z", "2016-01-01T00:00:00Z"])
    time_axis = plan.axis("ansi")
    assert tuple(time_axis) == (2, 4)
    assert time_axis.irregular is True
    assert time_axis.positions == (
        datetime(2015, 4, 1),
        datetime(2016, 1, 1),
    )
    assert plan.cell_at((0, 0, 2)).time == datetime(2015, 4, 1)


def test_missing_time_is_not_invented():
    grid = make_grid(
        axislabels=["Lat", "Long", "ansi"],
        lowlimits=["0", "0", "0"],
        highlimits=["1", "1", "2"],
        origin=["1", "0", "2000-01-01T00:00:00Z"],
        offsetvectors=[["-1", "0", "0"], ["0", "1", "0"], ["0", "0", "0"]],
        axis_coefficients={
            "ansi": [
                '"2000-01-01T00:00:00Z"',
                '"2000-03-01T00:00:00Z"',
                '"2000-06-01T00:00:00Z"',
            ]
        },
    )
    try:
        plan_grid_subset(grid, times=["2000-02-01T00:00:00Z"])
    except GridSubsetError as err:
        assert "not present" in str(err)
    else:
        raise AssertionError("a missing sampling time must not be invented")

    plan = plan_grid_subset(
        grid, times=["2000-02-01T00:00:00Z"], on_missing_time="skip"
    )
    assert plan.unmatched_times == (datetime(2000, 2, 1),)
    assert plan.index_ranges["ansi"].count == 0
    assert len(plan.cells) == 0
    assert plan.fill_value is None


def test_partial_coverage_is_not_proven():
    grid = make_grid()
    plan = plan_grid_subset(grid, bbox=(-10.05, 40, 0, 50))
    mask = plan.coverage_mask
    assert any(kind == "partial" for _, kind in mask.values())
    assert plan.proven is False
    assert plan.fill_value is None
    assert any(not cell.proven for cell in plan.cells if cell.coverage != "full")


def test_plain_grid_without_transform_rejects_crs_bbox():
    xml = """
    <gml:Grid xmlns:gml="http://www.opengis.net/gml/3.2" dimension="2">
      <gml:limits><gml:GridEnvelope>
        <gml:low>0 0</gml:low><gml:high>3 4</gml:high>
      </gml:GridEnvelope></gml:limits>
      <gml:axisLabels>Long Lat</gml:axisLabels>
    </gml:Grid>
    """
    grid = Grid(etree.fromstring(xml))
    plan = grid.plan_subset()
    assert plan.index_ranges["Long"] == (0, 3)
    assert plan.index_ranges["Lat"] == (0, 4)
    assert plan.grid is grid
    try:
        grid.plan_subset(bbox=(0, 0, 10, 10))
    except GridSubsetError as err:
        assert "no origin/offset vectors" in str(err)
    else:
        raise AssertionError("a CRS bbox needs a real georeference")


def test_referenceable_grid_reads_irregular_coefficients():
    xml = """
    <gml:ReferenceableGridByVectors xmlns:gml="http://www.opengis.net/gml/3.2"
        xmlns:rgrid="http://www.opengis.net/gml/3.3/rgrid" dimension="2">
      <gml:limits><gml:GridEnvelope>
        <gml:low>0 0</gml:low><gml:high>1 2</gml:high>
      </gml:GridEnvelope></gml:limits>
      <gml:axisLabels>Lat ansi</gml:axisLabels>
      <rgrid:origin><gml:Point><gml:pos>10 2000-01-01T00:00:00Z</gml:pos></gml:Point></rgrid:origin>
      <rgrid:generalGridAxis>
        <rgrid:GeneralGridAxis>
          <rgrid:offsetVector>1 0</rgrid:offsetVector>
          <rgrid:coefficients>0 1</rgrid:coefficients>
          <rgrid:gridAxesSpanned>Lat</rgrid:gridAxesSpanned>
        </rgrid:GeneralGridAxis>
      </rgrid:generalGridAxis>
      <rgrid:generalGridAxis>
        <rgrid:GeneralGridAxis>
          <rgrid:offsetVector>0 0</rgrid:offsetVector>
          <rgrid:coefficients>"2000-01-01T00:00:00Z" "2000-03-01T00:00:00Z" "2000-06-01T00:00:00Z"</rgrid:coefficients>
          <rgrid:gridAxesSpanned>ansi</rgrid:gridAxesSpanned>
        </rgrid:GeneralGridAxis>
      </rgrid:generalGridAxis>
    </gml:ReferenceableGridByVectors>
    """
    grid = ReferenceableGridByVectors(etree.fromstring(xml))
    assert grid.axis_coefficients["ansi"] == [
        "2000-01-01T00:00:00Z",
        "2000-03-01T00:00:00Z",
        "2000-06-01T00:00:00Z",
    ]


def test_non_contiguous_time_selection_marks_clip_gaps():
    grid = make_grid(
        axislabels=["Lat", "Long", "ansi"],
        lowlimits=["0", "0", "0"],
        highlimits=["1", "1", "4"],
        origin=["1", "0", "2000-01-01T00:00:00Z"],
        offsetvectors=[["-1", "0", "0"], ["0", "1", "0"], ["0", "0", "0"]],
        axis_coefficients={
            "ansi": [
                '"2000-01-01T00:00:00Z"',
                '"2000-03-01T00:00:00Z"',
                '"2000-06-01T00:00:00Z"',
                '"2000-09-01T00:00:00Z"',
                '"2001-01-01T00:00:00Z"',
            ]
        },
    )
    plan = plan_grid_subset(
        grid,
        times=["2000-03-01T00:00:00Z", "2000-09-01T00:00:00Z"],
        supported_axes=["Lat", "Long"],
    )
    time_axis = plan.axis("ansi")
    assert tuple(time_axis) == (1, 3)
    assert time_axis.clip_indices == (1, 3)
    assert time_axis.clip_range == (1, 4)
    assert time_axis.clip_range.indices == (1, 3)
    assert time_axis.clip_range.gaps is True
    assert plan.clip_ranges["ansi"] == (1, 4)


def test_batches_one_plan_per_sampling_instant():
    grid = make_grid(
        axislabels=["Lat", "Long", "ansi"],
        lowlimits=["0", "0", "0"],
        highlimits=["1", "1", "4"],
        origin=["1", "0", "2000-01-01T00:00:00Z"],
        offsetvectors=[["-1", "0", "0"], ["0", "1", "0"], ["0", "0", "0"]],
        axis_coefficients={
            "ansi": [
                '"2000-01-01T00:00:00Z"',
                '"2000-03-01T00:00:00Z"',
                '"2000-06-01T00:00:00Z"',
                '"2000-09-01T00:00:00Z"',
                '"2001-01-01T00:00:00Z"',
            ]
        },
    )
    plans = plan_grid_batches(grid, bbox=(0, 0, 2, 2), batch_times=True)
    assert len(plans) == 5
    assert [p.index_ranges["ansi"] for p in plans] == [
        (0, 0),
        (1, 1),
        (2, 2),
        (3, 3),
        (4, 4),
    ]
    assert [p.cells[0].time for p in plans] == [
        datetime(2000, 1, 1),
        datetime(2000, 3, 1),
        datetime(2000, 6, 1),
        datetime(2000, 9, 1),
        datetime(2001, 1, 1),
    ]


def test_chunked_plans_cover_exactly_the_full_block():
    grid = make_grid(
        axislabels=["Lat", "Long", "ansi"],
        lowlimits=["0", "0", "0"],
        highlimits=["99", "149", "9"],
        origin=["1", "0", "2000-01-01T00:00:00Z"],
        offsetvectors=[["-0.02", "0", "0"], ["0", "0.02", "0"], ["0", "0", "1"]],
    )
    full = plan_subset_from_subsets(grid, [("Long", -10, 0), ("Lat", 40, 50)])
    chunks = plan_grid_batches(
        grid, bbox=(-10, 40, 0, 50), times=["2000-01-02T00:00:00Z"], chunk=20
    )
    covered = [cell.storage_index for plan in chunks for cell in plan.cells]
    assert len(covered) == len(set(covered)) == len(full.cells)
    assert set(covered) == {cell.storage_index for cell in full.cells}


def test_boundary_cells_map_between_request_and_storage_orders():
    grid = make_grid(
        axislabels=["Lat", "Long", "ansi"],
        lowlimits=["0", "0", "0"],
        highlimits=["19", "29", "3"],
        origin=["2", "0", "2000-01-01T00:00:00Z"],
        offsetvectors=[["-0.1", "0", "0"], ["0", "0.1", "0"], ["0", "0", "1"]],
    )
    # Both edges land exactly on cell boundaries, so the selected block touches
    # the boundary in every direction and the corners must agree across orders.
    plan = plan_grid_subset(grid, bbox=(0.5, 0.5, 1.5, 1.5))
    assert plan.index_ranges["Lat"] == (5, 14)
    assert plan.index_ranges["Long"] == (5, 14)
    corners = [
        (5, 5, 0),
        (5, 14, 0),
        (14, 5, 0),
        (14, 14, 0),
    ]
    for storage in corners:
        request = plan.request_index_of(storage)
        assert plan.storage_index_of(request) == storage
        cell = plan.cell_at(request)
        assert cell.storage_index == storage
        assert cell.request_index == request
        assert (cell.index["Lat"], cell.index["Long"]) == (storage[0], storage[1])
        x_name, y_name = plan.spatial_axes
        assert cell.xy == plan.grid._subset_geometry.cell_center(
            cell.index[x_name], cell.index[y_name]
        )
    # A native index range is inclusive: the inverse mapping of the high edge
    # must not silently become the exclusive stop.
    assert plan.request_index_ranges[0] == IndexRange(5, 14)
    assert plan.request_index_ranges[1] == IndexRange(5, 14)


def test_cells_fall_inside_declared_read_ranges():
    grid = make_grid()
    plan = plan_subset_from_subsets(
        grid,
        [("Long", -10, 0), ("Lat", 40, 50), ("ansi", "2000-02-01T00:00:00Z")],
        supported_axes=["Long", "Lat"],
    )
    for axis in plan.axes.values():
        low, high = axis.read_range
        for cell in plan.cells[:: len(plan.axes["ansi"]) * 97]:
            index = cell.index[axis.name]
            assert low <= index <= high


def test_single_time_string_is_one_batch():
    grid = make_grid()
    plans = plan_grid_batches(
        grid,
        bbox=(-10, 40, 0, 50),
        times="2000-02-01T00:00:00Z",
        batch_times=True,
    )
    assert len(plans) == 1
    assert plans[0].index_ranges["ansi"] == (30, 30)


def test_cell_sequence_indexing_is_storage_ordered():
    grid = make_grid(
        axislabels=["Lat", "Long", "ansi"],
        lowlimits=["0", "0", "0"],
        highlimits=["1", "2", "1"],
        origin=["2", "0", "2000-01-01T00:00:00Z"],
        offsetvectors=[["-1", "0", "0"], ["0", "1", "0"], ["0", "0", "1"]],
    )
    plan = plan_grid_set_subset(grid)
    assert len(plan.cells) == 12
    # Row-major storage order: the last axis varies fastest.
    assert plan.cells[0].storage_index == (0, 0, 0)
    assert plan.cells[1].storage_index == (0, 0, 1)
    assert plan.cells[2].storage_index == (0, 1, 0)
    assert plan.cells[3].storage_index == (0, 1, 1)
    assert plan.cells[-1].storage_index == (1, 2, 1)
    assert [cell.storage_index for cell in plan.cells[0:2]] == [
        (0, 0, 0),
        (0, 0, 1),
    ]


def plan_grid_set_subset(grid):
    return plan_grid_subset(grid, bbox=(0, 0, 3, 2))


def test_range_and_clip_helpers_are_sequence_like():
    index_range = IndexRange(2, 4)
    assert index_range == (2, 4)
    assert list(index_range) == [2, 4]
    assert index_range.count == 3
    assert index_range.slice == slice(2, 5)
    assert IndexRange.empty().count == 0

    clip = ClipRange(1, 4, (1, 3))
    assert clip == (1, 4)
    assert clip.count == 3
    assert clip.gaps is True
    assert clip.slice == slice(1, 4)
    assert ClipRange(2, 2).count == 0
