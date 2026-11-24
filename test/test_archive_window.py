from __future__ import annotations

import io
import struct

import numpy as np
import pytest

from pyxdf import XdfArchive, load_xdf
from pyxdf.archive import ByteRange, _value_range


def _varlen(value: int) -> bytes:
    if value < 256:
        return b"\x01" + struct.pack("<B", value)
    return b"\x04" + struct.pack("<I", value)


def _chunk(tag: int, content: bytes, stream_id: int | None = None) -> bytes:
    body = struct.pack("<H", tag)
    if stream_id is not None:
        body += struct.pack("<I", stream_id)
    body += content
    return _varlen(len(body)) + body


def _header(*, srate: float = 4.0, can_drop: bool = False) -> bytes:
    desc = ""
    if can_drop:
        desc = (
            "<desc><synchronization><can_drop_samples>true"
            "</can_drop_samples></synchronization></desc>"
        )
    return (
        "<?xml version='1.0'?><info><name>Test</name><type>T</type>"
        "<channel_count>1</channel_count>"
        f"<nominal_srate>{srate}</nominal_srate>"
        "<channel_format>double64</channel_format>"
        f"{desc}</info>"
    ).encode()


def _canonical_xdf() -> bytes:
    stream_id = 7
    stamps = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.125]
    values = np.arange(100, 109, dtype=np.float64)
    sample = [_varlen(len(stamps))]
    for stamp, value in zip(stamps, values, strict=True):
        sample.append(b"\x08" + struct.pack("<d", stamp))
        sample.append(struct.pack("<d", value))
    file_header = _chunk(1, b"<?xml version='1.0'?><info><version>1.0</version></info>")
    return b"XDF:" + b"".join(
        [
            file_header,
            _chunk(2, _header(), stream_id),
            _chunk(4, struct.pack("<dd", 0.0, 0.125), stream_id),
            _chunk(3, b"".join(sample), stream_id),
        ]
    )


class RangeTracker:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.ranges: list[ByteRange] = []

    def read_ranges(self, ranges):
        normalized = []
        for item in ranges:
            if isinstance(item, ByteRange):
                rng = item
            else:
                start, length_or_end = item
                rng = ByteRange(start, start + length_or_end)
            normalized.append(rng)
        self.ranges.extend(normalized)
        return [self.data[r.start : r.end] for r in normalized]


def test_canonical_exclusion_clock_and_dejitter_example():
    archive = XdfArchive(_canonical_xdf())
    options = {
        "jitter_break_threshold_seconds": 1,
        "jitter_break_threshold_samples": 4,
    }

    result = archive.read_window(
        7,
        1.60,
        1.64,
        exclusions={7: [(2, 6)]},
        synchronize_clocks=True,
        **options,
    )
    np.testing.assert_array_equal(result.sample_indices, [6])
    assert result.time_stamps[0] == pytest.approx(1.6041666666666667, abs=1e-9)
    np.testing.assert_array_equal(result.time_series[:, 0], [106.0])

    clock_only = archive.read_window(7, 1.60, 1.64, synchronize_clocks=True, **options)
    exclusion_only = archive.read_window(
        7,
        1.60,
        1.64,
        exclusions={7: [(2, 6)]},
        synchronize_clocks=False,
        **options,
    )
    assert len(clock_only) == 0
    assert len(exclusion_only) == 0


def test_repeated_plans_on_same_registration_are_identical():
    archive = XdfArchive(_canonical_xdf())
    options = dict(
        jitter_break_threshold_seconds=1,
        jitter_break_threshold_samples=4,
    )
    calls = []
    for excluded in (True, False, True):
        result = archive.read_window(
            7,
            1.60,
            1.64,
            exclusions={7: [(2, 6)]} if excluded else None,
            synchronize_clocks=True,
            **options,
        )
        calls.append(
            (
                result.sample_indices.copy(),
                result.time_stamps.copy(),
                result.time_series.copy(),
            )
        )
    np.testing.assert_array_equal(calls[0][0], calls[2][0])
    np.testing.assert_allclose(calls[0][1], calls[2][1], atol=0, rtol=0)
    np.testing.assert_array_equal(calls[0][2], calls[2][2])
    assert len(calls[1][0]) == 0


def _random_xdf(mode: str, seed: int, n: int = 73, jitter: float = 2e-5):
    rng = np.random.default_rng(seed)
    stream_id = 3
    chunk_size = 11
    nominal = 100.0
    true_stamps = np.arange(n) / nominal
    true_stamps += rng.normal(0.0, jitter, n) if jitter else 0.0
    has_stamp = {
        "all": np.ones(n, dtype=bool),
        "none": np.zeros(n, dtype=bool),
        "first": np.arange(n) % chunk_size == 0,
        "mixed": rng.random(n) < 0.6,
    }[mode]
    values = rng.integers(-1000, 1000, size=(n, 2), dtype=np.int32)

    parts = [
        b"XDF:",
        _chunk(1, b"<?xml version='1.0'?><info><version>1.0</version></info>"),
        _chunk(2, _header(srate=nominal), stream_id),
    ]
    for first in range(0, n, chunk_size):
        last = min(n, first + chunk_size)
        body = [_varlen(last - first)]
        for i in range(first, last):
            if has_stamp[i]:
                body.append(b"\x08" + struct.pack("<d", true_stamps[i]))
            else:
                body.append(b"\x00")
            body.append(values[i].astype("<i4").tobytes())
        parts.append(_chunk(3, b"".join(body), stream_id))
    return b"".join(parts), stream_id, values


@pytest.mark.parametrize("mode", ["all", "none", "first", "mixed"])
def test_archive_matches_on_chunk_oracle_and_reads_only_hit_values(mode):
    raw, stream_id, values = _random_xdf(mode, seed=4)
    archive = XdfArchive(raw)
    exclusions = {stream_id: [(5, 9), (31, 34)]}
    options = dict(
        synchronize_clocks=False,
        dejitter_timestamps=False,
    )

    seen = 0
    kept: list[int] = []

    def on_chunk(chunk_values, chunk_stamps, info, this_id):
        nonlocal seen
        indices = np.arange(seen, seen + len(chunk_stamps))
        seen += len(chunk_stamps)
        remove = np.zeros(len(indices), dtype=bool)
        for a, b in exclusions[stream_id]:
            remove |= (indices >= a) & (indices < b)
        keep = ~remove
        kept.extend(indices[keep].tolist())
        return chunk_values[keep], chunk_stamps[keep], info

    streams, _ = load_xdf(io.BytesIO(raw), on_chunk=on_chunk, **options)
    reference = next(
        stream for stream in streams if stream["info"]["stream_id"] == stream_id
    )
    kept_array = np.asarray(kept, dtype=np.int64)

    windows = [(0.02, 0.20), (0.30, 0.55), (0.0, 1.0)]
    tracker = RangeTracker(raw)
    si = archive.get(stream_id)
    for lo, hi in windows:
        tracker.ranges.clear()
        result = archive.read_window(
            stream_id,
            lo,
            hi,
            exclusions=exclusions,
            reader=tracker,
            **options,
        )
        mask = (reference["time_stamps"] >= lo) & (reference["time_stamps"] < hi)
        np.testing.assert_array_equal(result.sample_indices, kept_array[mask])
        np.testing.assert_allclose(
            result.time_stamps,
            reference["time_stamps"][mask],
            atol=1e-9,
            rtol=0,
        )
        np.testing.assert_array_equal(
            result.time_series, reference["time_series"][mask]
        )

        hits = set(result.sample_indices.tolist())
        for original_index in range(si.n_samples):
            if original_index in hits:
                continue
            value_range = _value_range(si, original_index)
            for actual_range in tracker.ranges:
                overlap = actual_range.start < value_range.end and (
                    value_range.start < actual_range.end
                )
                assert not overlap


def test_boundary_samples_follow_half_open_windows():
    raw, stream_id, _values = _random_xdf("all", seed=2, n=31, jitter=0.0)
    archive = XdfArchive(raw)
    result = archive.read_window(
        stream_id,
        0.10,
        0.21,
        synchronize_clocks=False,
        dejitter_timestamps=False,
    )
    np.testing.assert_array_equal(result.sample_indices, np.arange(10, 21))
    np.testing.assert_allclose(result.time_stamps[0], 0.10, atol=1e-12)
    np.testing.assert_allclose(result.time_stamps[-1], 0.20, atol=1e-12)


def test_can_drop_samples_preserves_gaps():
    n = 30
    srate = 10.0
    stamps = np.arange(n) / srate
    stamps[5::5] += 0.5
    body = [_varlen(n)]
    for i, stamp in enumerate(stamps):
        body.append(b"\x08" + struct.pack("<d", stamp))
        body.append(struct.pack("<d", float(i)))
    raw = b"XDF:" + b"".join(
        [
            _chunk(1, b"<?xml version='1.0'?><info><version>1.0</version></info>"),
            _chunk(2, _header(srate=srate, can_drop=True), 1),
            _chunk(3, b"".join(body), 1),
        ]
    )
    archive = XdfArchive(raw)
    result = archive.read_window(
        1,
        0.45,
        0.75,
        synchronize_clocks=False,
        jitter_break_threshold_seconds=1,
        jitter_break_threshold_samples=500,
    )
    np.testing.assert_array_equal(result.time_stamps, stamps[6:8])


def test_index_does_not_store_numeric_sample_values():
    archive = XdfArchive(_canonical_xdf())
    si = archive.get(7)
    assert not hasattr(si, "time_series") or si.stream_data.time_series == []
    assert len(si.chunks) == 1
    assert all(not hasattr(chunk, "values") for chunk in si.chunks)


def test_stable_giant_chunk_query_reads_only_hit_value_range():
    n = 20_000
    stream_id = 11
    body = [_varlen(n)]
    for i in range(n):
        body.append(b"\x08" + struct.pack("<d", i / 4.0))
        body.append(struct.pack("<d", float(i)))
    raw = b"XDF:" + b"".join(
        [
            _chunk(1, b"<?xml version='1.0'?><info><version>1.0</version></info>"),
            _chunk(2, _header(srate=4.0), stream_id),
            _chunk(4, struct.pack("<dd", 0.0, 0.125), stream_id),
            _chunk(3, b"".join(body), stream_id),
        ]
    )
    archive = XdfArchive(raw)
    tracker = RangeTracker(raw)
    result = archive.read_window(
        stream_id,
        3750.10,
        3750.30,
        exclusions={stream_id: [(123, 4567)]},
        reader=tracker,
        jitter_break_threshold_seconds=1,
        jitter_break_threshold_samples=4,
    )
    np.testing.assert_array_equal(result.sample_indices, [15_000])
    assert result.time_stamps[0] == pytest.approx(3750.125, abs=1e-9)
    assert len(tracker.ranges) == 1
    assert tracker.ranges[0].length == 8
