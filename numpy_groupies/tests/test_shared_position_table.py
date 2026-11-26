import numpy as np
import pytest

from numpy_groupies.shared_position_table import (
    Archive,
    RegularSegment,
    WindowBatch,
    SharedPositionTable,
    analyze_window,
    build_batches,
    build_position_table,
)
from numpy_groupies.temporal import NAT_TICK


def test_zero_and_one_nanoseconds_share_neighbours_but_not_slots():
    archive = Archive.from_arrays(
        np.array([0, 1], dtype="timedelta64[ns]"),
        group_idx=np.array([0, 0]),
        row_id=np.array([10, 11]),
        group_count=1,
    )
    evidence = analyze_window(archive, 10, 2, 0)
    assert evidence.sorted_positions == (0, 1)
    assert evidence.evidence_rows == (10, 11)
    assert evidence.median_tick == 0
    assert evidence.quantile_tick == 1
    assert not evidence.shared

    table = build_position_table(archive, lengths=2, start_range=(10, 10))
    read = table.read(start=10, length=2, group=0)
    assert int(read.median.value.astype(np.int64)) == 0
    assert int(read.quantile.value.astype(np.int64)) == 1
    assert read.median.slot != read.quantile.slot
    assert read.median.sorted_positions == (0, 1)


def test_zero_and_two_nanoseconds_use_one_shared_slot():
    archive = Archive.from_arrays(np.array([0, 2], "m8[ns]"), group_count=1)
    table = build_position_table(archive, lengths=2, start_range=(0, 0))
    read = table.read(start=0, length=2, group=0)
    assert int(read.median.value.astype(np.int64)) == 1
    assert int(read.quantile.value.astype(np.int64)) == 1
    assert read.median.slot == read.quantile.slot
    assert read.shared


def test_odd_count_uses_one_rank_and_one_slot():
    values = np.array([30, 10, 20], "m8[ns]")
    archive = Archive.from_arrays(values, group_count=1)
    table = build_position_table(archive, lengths=3, start_range=(0, 0))
    read = table.read(start=0, length=3, group=0)
    assert read.median.sorted_positions == (1,)
    assert read.median.evidence_rows == (2,)
    assert read.median.slot == read.quantile.slot
    assert int(read.median.value.astype(np.int64)) == 20


def test_nat_is_skipped_and_empty_group_keeps_backend_fill():
    values = np.array([0, 1, "NaT", 4], "m8[ns]")
    groups = np.array([0, 0, 0, 1])
    archive = Archive.from_arrays(values, groups, group_count=3)
    table = build_position_table(archive, lengths=4, start_range=(0, 0))

    group_zero = table.read(start=0, length=4, group=0)
    assert group_zero.median.sorted_positions == (0, 1)
    assert int(group_zero.median.value.astype(np.int64)) == 0
    assert int(group_zero.quantile.value.astype(np.int64)) == 1

    empty = table.read(start=0, length=4, group=2)
    assert np.isnat(empty.median.value)
    assert np.isnat(empty.quantile.value)
    assert empty.median.slot == empty.quantile.slot == 0
    assert empty.median.sorted_positions == ()


def test_removed_missing_reading_changes_evidence_without_stale_neighbours():
    values = np.array([100, 0, 1, 2, 3], "m8[ns]")
    groups = np.zeros(5, dtype=np.int64)
    archive = Archive.from_arrays(values, groups, row_id=np.array([0, 2, 3, 4, 5]), group_count=1)

    full_window = analyze_window(archive, 0, 6, 0)
    assert full_window.sorted_positions == (2,)
    assert full_window.evidence_rows == (4,)

    after_leading_drop = analyze_window(archive, 2, 4, 0)
    assert after_leading_drop.sorted_positions == (1, 2)
    assert after_leading_drop.evidence_rows == (3, 4)

    sliding_values = np.array([0, 1, 2, 100, 3, 4], "m8[ns]")
    sliding_archive = Archive.from_arrays(sliding_values, group_count=1)
    first = analyze_window(sliding_archive, 0, 4, 0)
    second = analyze_window(sliding_archive, 1, 4, 0)
    assert first.evidence_rows == (1, 2)
    assert second.evidence_rows == (2, 4)

    table = build_position_table(sliding_archive, lengths=4, start_range=(0, 2))
    assert table.read(start=0, length=4, group=0).median.evidence_rows == (1, 2)
    assert table.read(start=1, length=4, group=0).median.evidence_rows == (2, 4)


def test_equal_values_choose_earliest_original_row_as_evidence():
    values = np.array([10, 20, 10, 20, 10, 20], "m8[ns]")
    archive = Archive.from_arrays(values, group_count=1)
    evidence = analyze_window(Archive.from_arrays(values, group_count=1), 0, 6, 0)
    assert evidence.sorted_positions == (2, 3)
    assert evidence.evidence_rows == (4, 1)

    table = build_position_table(archive, lengths=6, start_range=(0, 0))
    read = table.read(start=0, length=6, group=0)
    assert read.median.evidence_rows == (4, 1)


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_every_small_window_is_bitwise_backend_equal(seed):
    rng = np.random.default_rng(seed)
    n = 90
    group_count = 3
    ticks = rng.integers(-(2**63) + 1, 2**63 - 1, n, dtype=np.int64)
    ticks[rng.random(n) < 0.12] = NAT_TICK
    groups = rng.integers(0, group_count, n)
    archive = Archive(np.arange(n), groups, ticks, group_count=group_count)

    first_start = 0
    last_start = 35
    length = 9
    table = build_position_table(
        archive,
        lengths=length,
        start_range=(first_start, last_start),
        verify_small=False,
    )
    report = table.verify_small_batches(10**9)
    assert report.ok, report.mismatches[:3]

    for start in range(first_start, last_start + 1):
        for group in range(group_count):
            evidence = analyze_window(archive, start, length, group)
            read = table.read(start=start, length=length, group=group)
            assert read.median.slot == read.quantile.slot if evidence.shared else read.median.slot != read.quantile.slot
            assert read.median.sorted_positions == evidence.sorted_positions
            assert read.median.evidence_rows == evidence.evidence_rows


def test_regular_segments_map_interruptions_and_nat_back_to_global_rows():
    segment = RegularSegment(
        start_row=40,
        stride=5,
        group=1,
        ticks=np.array([7, 9, NAT_TICK, 11, 13], dtype=np.int64),
        missing_slots=np.array([1]),
    )
    archive = Archive.from_segments([segment], group_count=2)
    assert archive.rows.tolist() == [40, 50, 55, 60]
    assert archive.ticks.tolist() == [7, NAT_TICK, 11, 13]
    assert archive.source_of_row(55)[0].slot == 3
    assert archive.source_of_row(45) == ()

    table = build_position_table(archive, lengths=11, start_range=(50, 50))
    read = table.read(start=50, length=11, group=1)
    assert read.median.evidence_rows == (55, 60)


def test_long_odd_region_is_one_compressed_run():
    n = 10_000
    archive = Archive.from_arrays(
        np.arange(n, dtype=np.int64).astype("timedelta64[ns]"),
        group_idx=np.zeros(n, dtype=np.int64),
        group_count=1,
    )
    table = build_position_table(archive, lengths=101, start_range=(0, n - 101), verify_small=False)
    runs = table.layout_runs(101, 0)
    assert runs.shape == (1,)
    assert bool(runs[0]["shared"])
    assert int(runs[0]["first_start"]) == 0
    assert int(runs[0]["last_start"]) == n - 101


def test_extreme_gap_can_reverse_the_usual_order():
    lower = -3_630_414_616_439_744_187
    upper = -857_812_531_637_777_362
    archive = Archive.from_arrays(np.array([lower, upper], "m8[ns]"), group_count=1)
    table = build_position_table(archive, lengths=2, start_range=(0, 0))
    read = table.read(start=0, length=2, group=0)
    median_tick = int(read.median.value.astype(np.int64))
    quantile_tick = int(read.quantile.value.astype(np.int64))
    assert median_tick == -2_244_113_574_038_760_774
    assert quantile_tick == -2_244_113_574_038_760_658
    assert quantile_tick > median_tick
    assert table.verify_window(0, 2).ok


def test_npz_table_roundtrip_keeps_old_reader_evidence(tmp_path):
    archive = Archive.from_arrays(np.array([-100, 0, 1, 100], "m8[ns]"), group_count=1)
    table = build_position_table(archive, lengths=4, start_range=(0, 0))
    path = tmp_path / "shared-position-table.npz"
    table.save_npz(str(path))

    loaded = SharedPositionTable.load_npz(str(path))
    read = loaded.read(start=0, length=4, group=0)
    assert read.median.evidence_rows == (1, 2)
    assert read.median.slot != read.quantile.slot
    assert int(read.median.value.astype(np.int64)) == 0
    assert int(read.quantile.value.astype(np.int64)) == 1


def test_multiple_batches_and_legacy_reader():
    values = np.arange(20, dtype=np.int64).astype("timedelta64[ns]")
    archive = Archive.from_arrays(values, group_count=1)
    batches = [
        WindowBatch.make(lengths=(2, 3), start_range=(0, 4), name="small"),
        WindowBatch.make(lengths=4, start_range=(10, 12), name="longish"),
    ]
    table = build_batches(archive, batches)
    value, positions, slot = table.legacy_read("nanmedian", start=0, length=3, group=0)
    assert positions == (1,)
    assert slot == table.read_column("nanquantile", start=0, length=3, group=0).slot
    assert int(value.astype(np.int64)) == 1

    disjoint = build_batches(
        archive,
        [WindowBatch.make(lengths=3, start_range=(0, 1)), WindowBatch.make(lengths=3, start_range=(10, 11))],
    )
    runs = disjoint.layout_runs(3, 0)
    assert runs["first_start"].tolist() == [0, 10]
    with pytest.raises(KeyError):
        disjoint.locate_cell(5, 3, 0)
