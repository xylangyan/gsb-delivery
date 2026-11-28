import h5py
import numpy as np
import pytest

import h5netcdf
from h5netcdf.scan import ScanWindowError


def _archive(path):
    with h5py.File(path, "w") as f:
        f.create_dataset("scan_angle", data=np.array([30.0, 30.0, 32.0, 29.0]))
        f.create_dataset(
            "reflectivity",
            data=np.array([101, 102, 103, 104], dtype=np.int32),
            chunks=(2,),
        )
        f.create_dataset(
            "qc", data=np.array([5.0, 6.0, 7.0, -9999.0]), chunks=(2,)
        )


@pytest.fixture
def archive(tmp_path):
    path = str(tmp_path / "radar.nc")
    _archive(path)
    with h5netcdf.File(path, "r+") as f:
        f.create_scan_index()
    return path


def _scans(page):
    return [record.scan for record in page]


def test_window_orders_by_corrected_angle_then_scan(archive):
    with h5netcdf.File(archive, "r") as f:
        window = f.open_scan_window(29, 32, page_size=10)
        page = window.page()
        assert _scans(page) == [0, 1]
        assert [r.angle for r in page.records] == [30.0, 30.0]
        assert [r.reflectivity for r in page.records] == [101, 102]
        # right bound is exclusive
        assert f.open_scan_window(29, 30).page().records == ()


def test_left_closed_window(archive):
    with h5netcdf.File(archive, "r") as f:
        page = f.open_scan_window(30, 33, page_size=10).page()
        assert _scans(page) == [0, 1, 2]


def test_token_resumes_old_release_after_new_publish(archive):
    with h5netcdf.File(archive, "r") as f:
        page = f.open_scan_window(29, 32, page_size=1).page()
        assert _scans(page) == [0]
        token = page.next_token
        assert token is not None

    with h5netcdf.File(archive, "r+") as f:
        release = f.publish_scan_release(
            scans=[0, 2, 1, 3],
            angles=[31.0, 30.0, None, None],
            qcs=[None, None, 8.0, -9999.0],
        )
        assert release == 1

    with h5netcdf.File(archive, "r") as f:
        resumed = f.open_scan_window(token=token).page()
        assert _scans(resumed) == [1]
        record = resumed.records[0]
        assert record.angle == 30.0
        assert record.reflectivity == 102
        assert record.qc == 6.0
        assert record.release == 0
        assert resumed.next_token is None

        fresh = f.open_scan_window(29, 32, page_size=10).page()
        assert _scans(fresh) == [3, 1, 2, 0]
        by_scan = {record.scan: record for record in fresh.records}
        assert by_scan[3].missing
        assert by_scan[3].has_receipt
        assert by_scan[3].qc == -9999.0
        assert by_scan[1].qc == 8.0
        assert by_scan[0].angle == 31.0
        assert by_scan[2].angle == 30.0


def test_old_release_remains_readable_after_publish(archive):
    with h5netcdf.File(archive, "r+") as f:
        f.publish_scan_release(
            scans=[0, 2], angles=[31.0, 30.0], qcs=[None, None]
        )
    with h5netcdf.File(archive, "r") as f:
        old = f.open_scan_window(29, 32, page_size=10, release=0).page()
        assert _scans(old) == [0, 1]
        assert old.records[0].angle == 30.0


def test_publish_reuses_untouched_index_nodes_and_reflectivity(archive):
    with h5netcdf.File(archive, "r") as f:
        index = f.scan_index()
        before = index.stats()
        reflect_before = f._h5file["reflectivity"].id.get_offset()
        chunk_before = f._h5file["reflectivity"].chunks
    with h5netcdf.File(archive, "r+") as f:
        f.publish_scan_release(
            scans=[1],
            qcs=[8.0],
        )
    with h5netcdf.File(archive, "r") as f:
        index = f.scan_index()
        after = index.stats()
        assert after["node_start"] == before["node_end"]
        assert after["new_nodes"] < before["total_nodes"]
        assert f._h5file["reflectivity"].id.get_offset() == reflect_before
        assert f._h5file["reflectivity"].chunks == chunk_before


def test_reflectivity_lookup_by_scan(archive):
    with h5netcdf.File(archive, "r+") as f:
        f.publish_scan_release(scans=[0, 2], angles=[31.0, 30.0])
    with h5netcdf.File(archive, "r") as f:
        assert f.read_reflectivity(0) == 101
        assert f.read_reflectivity(3) == 104
        assert f.read_scan(2).angle == 30.0


def test_reflectivity_is_read_per_needed_block(archive):
    with h5netcdf.File(archive, "r") as f:
        index = f.scan_index()
        calls = []
        original = index._read_reflectivity

        def spy(positions):
            calls.append(list(positions))
            return original(positions)

        index._read_reflectivity = spy
        page = index.window(29, 32, page_size=1).page()
        assert _scans(page) == [0]
        assert calls == [[0]]


def test_publish_requires_receipt_to_enter_window(archive):
    with h5netcdf.File(archive, "r+") as f:
        f.publish_scan_release(scans=[3], qcs=[-9999.0])
    with h5netcdf.File(archive, "r") as f:
        page = f.open_scan_window(29, 33, page_size=10).page()
        assert _scans(page) == [3, 0, 1, 2]


def test_qc_only_update_keeps_angle_and_reflectivity(archive):
    with h5netcdf.File(archive, "r+") as f:
        f.publish_scan_release(scans=[1], qcs=[8.0])
    with h5netcdf.File(archive, "r") as f:
        record = f.read_scan(1)
        assert record.angle == 30.0
        assert record.reflectivity == 102
        assert record.qc == 8.0


def test_angle_update_moves_scan_between_windows(archive):
    with h5netcdf.File(archive, "r+") as f:
        f.publish_scan_release(scans=[2], angles=[30.0])
    with h5netcdf.File(archive, "r") as f:
        inside = f.open_scan_window(29, 32, page_size=10).page()
        assert _scans(inside) == [0, 1, 2]
        outside = f.open_scan_window(32, 40, page_size=10).page()
        assert outside.records == ()


def test_bad_token_rejected(archive):
    with h5netcdf.File(archive, "r") as f:
        with pytest.raises(ScanWindowError):
            f.open_scan_window(token="not-a-token")
        page = f.open_scan_window(29, 32, page_size=1).page()
        with pytest.raises(ScanWindowError):
            f.open_scan_window(0, 1, token=page.next_token)


def test_incremental_append_matches_bulk(tmp_path):
    path = str(tmp_path / "radar.nc")
    with h5py.File(path, "w") as f:
        f.create_dataset("scan_angle", data=np.array([30.0, 30.0, 32.0, 29.0]))
        f.create_dataset(
            "reflectivity",
            data=np.array([101, 102, 103, 104], dtype=np.int32),
            maxshape=(None,),
            chunks=(2,),
        )
        f.create_dataset(
            "qc", data=np.array([5.0, 6.0, 7.0, -9999.0]), chunks=(2,)
        )
    with h5netcdf.File(path, "r+") as f:
        f.create_scan_index()
        dataset = f._h5file["reflectivity"]
        dataset.resize((6,))
        dataset[4:6] = [105, 106]
        f.publish_scan_release(scans=[4], angles=[30.0], qcs=[9.0])
        f.publish_scan_release(scans=[5], angles=[29.0], qcs=[10.0])
    with h5netcdf.File(path, "r") as f:
        page = f.open_scan_window(29, 32, page_size=10).page()
        assert _scans(page) == [5, 0, 1, 4]
        assert {r.scan: r.reflectivity for r in page.records}[5] == 106


def test_mapping_style_publish(archive):
    with h5netcdf.File(archive, "r+") as f:
        f.publish_scan_release(
            angle_calibrations={0: 31.0, 2: 30.0},
            qc_receipts={1: 8.0, 3: -9999.0},
        )
    with h5netcdf.File(archive, "r") as f:
        page = f.open_scan_window(29, 32, page_size=10).page()
        assert _scans(page) == [3, 1, 2, 0]


def test_large_window_pages_directly(archive):
    with h5netcdf.File(archive, "r") as f:
        window = f.open_scan_window(29, 32, page_size=1, release=0)
        seen = []
        while True:
            page = window.page()
            if not page.records:
                break
            seen.extend(_scans(page))
            if page.next_token is None:
                break
        assert seen == [0, 1]


def test_empty_window(archive):
    with h5netcdf.File(archive, "r") as f:
        page = f.open_scan_window(100, 200, page_size=10).page()
        assert page.records == ()
        assert page.next_token is None
        assert not page.has_more


def test_publish_read_only_fails(archive):
    with h5netcdf.File(archive, "r") as f:
        with pytest.raises(ScanWindowError):
            f.publish_scan_release(scans=[0], qcs=[1.0])


def test_missing_index_is_reported(tmp_path):
    path = str(tmp_path / "plain.nc")
    _archive(path)
    with h5netcdf.File(path, "r") as f:
        with pytest.raises(ScanWindowError, match="create_scan_index"):
            f.open_scan_window(29, 32)


def test_page_sizes_agree(archive):
    with h5netcdf.File(archive, "r+") as f:
        f.publish_scan_release(
            angle_calibrations={0: 31.0, 2: 30.0},
            qc_receipts={1: 8.0, 3: -9999.0},
        )
    with h5netcdf.File(archive, "r") as f:
        whole = f.open_scan_window(29, 32, page_size=100).page()
        expected = [(r.scan, r.angle, r.qc) for r in whole.records]
        for size in (1, 2, 3, 5):
            chunks = [
                [(r.scan, r.angle, r.qc) for r in page.records]
                for page in f.open_scan_window(29, 32, page_size=size)
            ]
            assert [item for chunk in chunks for item in chunk] == expected


def test_token_from_deleted_release_still_reads_snapshot(archive):
    with h5netcdf.File(archive, "r") as f:
        token = f.open_scan_window(29, 32, page_size=1).page().next_token
    for _ in range(3):
        with h5netcdf.File(archive, "r+") as f:
            f.publish_scan_release(
                angle_calibrations={0: 31.0}, qc_receipts={3: -9999.0}
            )
    with h5netcdf.File(archive, "r") as f:
        record = f.open_scan_window(token=token).page().records[0]
        assert record.scan == 1
        assert record.release == 0
        assert record.qc == 6.0


def test_token_from_rebuilt_index_is_rejected(archive):
    with h5netcdf.File(archive, "r") as f:
        token = f.open_scan_window(29, 32, page_size=1).page().next_token
    with h5netcdf.File(archive, "r+") as f:
        f.create_scan_index(replace=True)
    with h5netcdf.File(archive, "r") as f:
        with pytest.raises(ScanWindowError, match="rebuilt"):
            f.open_scan_window(token=token)
