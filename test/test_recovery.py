"""Tests for the recovery and provenance layer."""

from __future__ import annotations

import struct

import numpy as np
import pytest
from xdf_builder import (
    assemble,
    chunk,
    clock_offset,
    file_header,
    samples_chunk,
    stream_header,
    string_samples_chunk,
)

from pyxdf.recovery import (
    ByteRange,
    ClockEpoch,
    ClockOffset,
    Observation,
    RecordingArchive,
    RecoveryJob,
    Replica,
    StreamSpec,
    scan_replica,
)

BOUNDARY = bytes(
    [
        0x43,
        0xA5,
        0x46,
        0xDC,
        0xCB,
        0xF5,
        0x41,
        0x0F,
        0xB3,
        0x0E,
        0xD5,
        0x46,
        0x73,
        0x83,
        0xCB,
        0xE4,
    ]
)


def boundary_chunk() -> bytes:
    return chunk(0, BOUNDARY)


def numeric_file(**kwargs):
    """A small single-stream numeric file with every explicit/omitted mix."""
    return assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32", nchannels=1, **kwargs),
        samples_chunk(
            1,
            [[1], [2], [3], [4], [5]],
            [10.0, 0.0, 0.0, 10.3, 0.0],
            [True, False, False, True, False],
        ),
    )


def string_file():
    return assemble(
        file_header(),
        stream_header(
            1, name="Marks", type="Markers", uid="markers", srate=10, fmt="string"
        ),
        string_samples_chunk(
            1, ["A", "B", "C"], [10.0, 0.0, 10.2], [True, False, True]
        ),
    )


def archive(**spec_kwargs):
    return RecordingArchive(
        [
            StreamSpec(
                "uid:eeg",
                tdiff=0.1,
                position_origin=100,
                device_origin=10.0,
                **spec_kwargs,
            )
        ]
    )


# ---------------------------------------------------------------------------
# Scanning and raw byte provenance


def test_scan_verifies_boundaries_timestamps_and_offsets():
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        clock_offset(1, 10.0, 0.5),
        clock_offset(1, 20.0, 0.5),
        samples_chunk(1, [[1], [2]], [10.0, 10.1], [True, True]),
    )
    scan = scan_replica(Replica.from_bytes("vol-a", raw))
    assert scan.complete
    assert scan.checks.all_verified
    summary = scan.checks.summary()
    assert summary["file_header"] == (1, 1)
    assert summary["boundary"][0] == summary["boundary"][1]
    assert summary["clock_offset"] == (2, 2)
    assert summary["explicit_timestamp"] == (2, 2)

    sample = scan.samples_for("uid:eeg")[0]
    assert sample.byte_range.volume_id == "vol-a"
    assert raw[sample.time_range.start : sample.time_range.end] == struct.pack(
        "<d", 10.0
    )
    assert raw[sample.value_range.start : sample.value_range.end] == struct.pack(
        "<f", 1.0
    )


def test_byte_ranges_are_half_open_and_volume_checked():
    left = ByteRange("a", 10, 20)
    right = ByteRange("a", 15, 25)
    other = ByteRange("b", 10, 20)
    assert left.length == 10
    assert left.offset == 10
    assert left.contains(right) is False
    assert left.overlaps(right)
    assert not left.overlaps(other)
    assert str(left) == "a:[10, 20)"
    with pytest.raises(ValueError):
        ByteRange("a", 5, 4)


def test_scan_does_not_trust_an_unproven_chunk():
    good = samples_chunk(1, [[1], [2]], [10.0, 10.1], [True, True])
    bad = samples_chunk(1, [[3], [4]], [10.2, 10.3], [True, True])
    after = samples_chunk(1, [[5], [6]], [10.4, 10.5], [True, True])
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        good,
        bad,
        after,
    )
    start = raw.index(bad)
    # Turn the second chunk's declared length into garbage and restore a
    # boundary some bytes later, so the scanner has to resynchronize.
    corrupted = (
        raw[:start]
        + b"\xde\xad\xbe\xef" * 4
        + boundary_chunk()
        + raw[raw.index(after) :]
    )
    scan = scan_replica(Replica.from_bytes("vol-a", corrupted))
    assert not scan.complete
    assert any(issue.kind == "resync" for issue in scan.issues)
    # Samples read from the damaged region keep their byte range for review but
    # are never marked as a proven value.
    unresolved = [
        sample for sample in scan.samples_for("uid:eeg") if not sample.value_complete
    ]
    assert all(sample.byte_range.volume_id == "vol-a" for sample in unresolved)


# ---------------------------------------------------------------------------
# Explicit time, trial reads, and omitted times


def test_trial_read_time_is_kept_but_never_used():
    """A time next to a truncated value is recorded, never used as predecessor."""
    raw = string_file()
    # Cut the last sample's value: its explicit time is intact but its value is
    # not complete.  Everything after this in the file is unreadable.
    truncated = raw[:-1]
    scan = scan_replica(Replica.from_bytes("vol-a", truncated))
    trial = scan.samples_for("uid:markers")[-1]
    assert trial.time_kind == "explicit"
    assert trial.device_time is not None
    assert trial.time_verified is False
    assert trial.value_complete is False

    view = RecoveryJob(
        [Replica.from_bytes("vol-a", truncated)],
        RecordingArchive(
            [
                StreamSpec(
                    "uid:markers", tdiff=0.1, position_origin=100, device_origin=10.0
                )
            ]
        ),
    ).resolve()
    stream = view.stream("uid:markers")
    # The damaged sample still has a value gap and a pending time, but the
    # trial-read device time is preserved for review.
    observation = next(o for o in stream if o.position == 102)
    assert observation.value is None
    assert observation.device_time is None
    assert observation.device_time_status == "pending"
    assert observation.unverified_device_time == pytest.approx(10.2)


def test_omitted_times_are_deduced_from_the_actual_predecessor():
    view = RecoveryJob(
        [Replica.from_bytes("vol-a", numeric_file())], archive()
    ).resolve()
    stream = view.stream("uid:eeg")
    times = {
        observation.position: (observation.device_time, observation.device_time_status)
        for observation in stream
    }
    assert times[100] == (10.0, "explicit")
    assert times[101] == (10.1, "deduced")
    assert times[102] == (10.2, "deduced")
    assert times[103] == (10.3, "explicit")
    assert times[104] == (10.4, "deduced")
    for observation in stream:
        if observation.device_time_status == "deduced":
            assert observation.predecessor_position == observation.position - 1


def test_leading_omitted_times_without_predecessor_stay_pending():
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        samples_chunk(1, [[1], [2], [3]], [0, 0, 10.2], [False, False, True]),
    )
    view = RecoveryJob([Replica.from_bytes("vol-a", raw)], archive()).resolve()
    stream = view.stream("uid:eeg")
    by_position = {observation.position: observation for observation in stream}
    # Positions 100 and 101 are before the first verified anchor.  Their values
    # are delivered, but no time is invented for them.
    assert by_position[100].value_complete
    assert by_position[100].device_time is None
    assert by_position[100].device_time_status == "pending"
    assert by_position[101].device_time_status == "pending"
    assert by_position[102].device_time == pytest.approx(10.2)
    assert by_position[102].device_time_status == "explicit"


def test_later_anchor_does_not_backfill_device_times():
    """An anchor found after omitted samples never back-fills those times."""
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        samples_chunk(1, [[1], [2], [3]], [0, 0, 10.5], [False, False, True]),
    )
    view = RecoveryJob([Replica.from_bytes("vol-a", raw)], archive()).resolve()
    statuses = [
        observation.device_time_status for observation in view.stream("uid:eeg")
    ]
    assert statuses == ["pending", "pending", "explicit"]
    assert all(
        observation.device_time is None for observation in view.stream("uid:eeg")[:2]
    )


def test_anchor_does_not_cross_a_possible_sample_loss():
    """After a resync, a later anchor may not be pushed back over the gap."""
    first = samples_chunk(1, [[1], [2]], [10.0, 0.0], [True, False])
    lost = samples_chunk(1, [[3], [4]], [10.4, 0.0], [True, False])
    after = samples_chunk(1, [[5], [6]], [10.6, 0.0], [True, False])
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        first,
        lost,
        after,
    )
    start = raw.index(lost)
    corrupted = (
        raw[:start]
        + b"\xde\xad\xbe\xef" * 4
        + boundary_chunk()
        + raw[raw.index(after) :]
    )
    view = RecoveryJob([Replica.from_bytes("vol-a", corrupted)], archive()).resolve()
    stream = view.stream("uid:eeg")
    after_gap = [observation for observation in stream if observation.position >= 106]
    for observation in after_gap:
        # No deduction may reach back before the anchor at 10.6.
        if observation.device_time_status == "deduced":
            assert observation.predecessor_position is not None
            assert observation.predecessor_position >= 106


def test_streams_never_share_a_predecessor():
    """Two streams with different clocks do not borrow each other's times."""
    raw = assemble(
        file_header(),
        stream_header(
            1, uid="text", name="text", type="Markers", srate=10, fmt="string"
        ),
        stream_header(2, uid="eeg", name="eeg", type="EEG", srate=10, fmt="float32"),
        string_samples_chunk(1, ["x", "y"], [20.0, 0.0], [True, False]),
        samples_chunk(2, [[1], [2]], [0.0, 0.0], [False, False]),
    )
    view = RecoveryJob(
        [Replica.from_bytes("vol-a", raw)],
        RecordingArchive(
            [
                StreamSpec(
                    "uid:text", tdiff=0.1, position_origin=100, device_origin=10.0
                ),
                StreamSpec(
                    "uid:eeg", tdiff=0.1, position_origin=100, device_origin=10.0
                ),
            ]
        ),
    ).resolve()
    # The numeric stream has no anchor at all, so none of its times may come
    # from the marker stream's explicit time.
    for observation in view.stream("uid:eeg"):
        assert observation.device_time is None
        assert observation.device_time_status == "pending"


def test_epochs_never_share_a_predecessor():
    """An omitted time after a device restart is not deduced across the restart."""
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        samples_chunk(
            1, [[1], [2], [3], [4]], [10.0, 0.0, 50.0, 0.0], [True, False, True, False]
        ),
    )
    spec = StreamSpec(
        "uid:eeg",
        tdiff=0.1,
        position_origin=100,
        epochs=(
            ClockEpoch(
                0, start_device_time=0.0, end_device_time=40.0, device_origin=10.0
            ),
            ClockEpoch(
                1,
                start_device_time=40.0,
                end_device_time=100.0,
                first_sample_index=2,
                position_offset=200,
                device_origin=50.0,
            ),
        ),
    )
    view = RecoveryJob(
        [Replica.from_bytes("vol-a", raw)], RecordingArchive([spec])
    ).resolve()
    stream = view.stream("uid:eeg")
    assert [observation.epoch_id for observation in stream] == [0, 0, 1, 1]
    assert stream[1].device_time == pytest.approx(10.1)
    # The omitted sample after the restart has a valid predecessor in its own
    # epoch (50.0), so it may be deduced from that.
    assert stream[3].device_time == pytest.approx(50.1)


# ---------------------------------------------------------------------------
# Volume-local damage does not contaminate another stream


def test_damage_in_one_stream_leaves_the_other_stream_anchored():
    text = string_samples_chunk(
        1, ["a", "b", "c"], [10.0, 0.0, 0.0], [True, False, False]
    )
    numeric = samples_chunk(2, [[1], [2], [3]], [10.0, 0.0, 0.0], [True, False, False])
    raw = assemble(
        file_header(),
        stream_header(1, uid="text", type="Markers", srate=10, fmt="string"),
        stream_header(2, uid="eeg", type="EEG", srate=10, fmt="float32"),
        text,
        numeric,
    )
    # Damage only the text chunk's last value, leaving the numeric chunk intact.
    corrupted = bytearray(raw)
    text_start = raw.index(text)
    corrupted[text_start + 20] ^= 0xFF
    view = RecoveryJob(
        [Replica.from_bytes("vol-a", bytes(corrupted))],
        RecordingArchive(
            [
                StreamSpec(
                    "uid:text", tdiff=0.1, position_origin=100, device_origin=10.0
                ),
                StreamSpec(
                    "uid:eeg", tdiff=0.1, position_origin=100, device_origin=10.0
                ),
            ]
        ),
    ).resolve()
    eeg = view.stream("uid:eeg")
    assert [observation.device_time_status for observation in eeg] == [
        "explicit",
        "deduced",
        "deduced",
    ]


# ---------------------------------------------------------------------------
# Replica merge: which layer changed?


def test_second_replica_value_bytes_change_only_the_value_layer():
    header = stream_header(
        1, uid="markers", name="Marks", type="Markers", srate=10, fmt="string"
    )
    # Sample 1 has an omitted time, so its device time is deduced from sample 0
    # in every replica.  Only its value bytes are missing from replica A, which
    # isolates the value layer from the device-time layer.
    full = assemble(
        file_header(),
        header,
        string_samples_chunk(1, ["A", "B"], [10.0, 0.0], [True, False]),
    )
    scan = scan_replica(Replica.from_bytes("probe", full))
    target = scan.samples_for("uid:markers")[1]
    replica_a = full[: target.value_range.end - 1]

    markers = RecordingArchive(
        [StreamSpec("uid:markers", tdiff=0.1, position_origin=100, device_origin=10.0)]
    )
    job = RecoveryJob([Replica.from_bytes("vol-a", replica_a)], markers, dejitter=False)
    first = job.resolve()
    assert first.at("uid:markers", 101).value_complete is False
    assert first.at("uid:markers", 101).device_time_status == "deduced"

    report = job.merge(Replica.from_bytes("vol-b", full))
    assert report.added_volumes == ("vol-b",)
    # Only the value layer changes: the device time was already deduced from
    # the actual predecessor and does not move.
    assert "value" in report.changed_layers()
    assert report.value_changes
    assert not report.device_time_changes
    assert not report.position_changes
    # The same original samples are counted once each.  They are reported as
    # duplicates (bytes that arrived twice) rather than as new device records.
    assert [position for _, position in report.duplicate_samples] == [100, 101]
    stream = job.resolve().stream("uid:markers")
    positions = [observation.position for observation in stream]
    assert positions == sorted(set(positions))
    assert [observation.value for observation in stream] == [("A",), ("B",)]


def test_second_replica_explicit_time_can_change_the_device_time_layer():
    """When the late copy proves an explicit time, that layer is reported."""
    header = stream_header(
        1, uid="markers", name="Marks", type="Markers", srate=10, fmt="string"
    )
    full = assemble(
        file_header(),
        header,
        string_samples_chunk(1, ["A", "B"], [10.0, 10.2], [True, True]),
    )
    scan = scan_replica(Replica.from_bytes("probe", full))
    target = scan.samples_for("uid:markers")[1]
    replica_a = full[: target.value_range.end - 1]
    markers = RecordingArchive(
        [StreamSpec("uid:markers", tdiff=0.1, position_origin=100, device_origin=10.0)]
    )
    job = RecoveryJob([Replica.from_bytes("vol-a", replica_a)], markers, dejitter=False)
    first = job.resolve()
    # Without the late copy, sample B's time is only a trial read: it is placed
    # one sample after its predecessor but no time is claimed.
    provisional = first.at("uid:markers", 101)
    assert provisional.value_complete is False
    assert provisional.device_time_status == "pending"
    assert provisional.position_status == "tentative"

    report = job.merge(Replica.from_bytes("vol-b", full))
    # The late copy proves sample B's explicit time.  Both the value and the
    # device-time layer therefore change, and the position is corrected to the
    # verified anchor's position.
    assert {"value", "device_time"} <= report.changed_layers()
    after = job.resolve().at("uid:markers", 102)
    assert after.device_time == pytest.approx(10.2)
    assert after.device_time_status == "explicit"
    assert after.value == ("B",)
    # The same original sample is not counted a second time.
    assert job.resolve().at("uid:markers", 101) is None


def test_late_clock_offset_changes_only_the_unified_layer():
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        clock_offset(1, 0.0, 1.0),
        samples_chunk(1, [[1], [2]], [10.0, 0.0], [True, False]),
    )
    job = RecoveryJob([Replica.from_bytes("vol-a", raw)], archive(), dejitter=False)
    before = job.resolve()
    assert before.stream("uid:eeg")[0].unified_status == "synced"

    offset = ClockOffset(
        time=100.0,
        value=2.0,
        volume_id="vol-a",
        byte_range=ByteRange("vol-a", 0, 16),
    )
    report = job.merge_clock_offsets("vol-a", "uid:eeg", [offset])
    assert "unified_time" in report.changed_layers()
    assert not report.value_changes
    assert not report.device_time_changes
    after = job.resolve().stream("uid:eeg")
    # Values and device times are untouched; only the unified time moves.
    assert np.allclose(
        [o.value[0] for o in after], [o.value[0] for o in before.stream("uid:eeg")]
    )
    assert np.allclose(
        [o.device_time for o in after],
        [o.device_time for o in before.stream("uid:eeg")],
    )
    assert not np.allclose(
        [o.unified_time for o in after],
        [o.unified_time for o in before.stream("uid:eeg")],
    )


# ---------------------------------------------------------------------------
# Committed windows are immutable


def test_committed_window_can_be_requeried_after_a_merge():
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        samples_chunk(1, [[1], [2]], [10.0, 10.1], [True, True]),
    )
    job = RecoveryJob([Replica.from_bytes("vol-a", raw)], archive(), dejitter=False)
    job.resolve()
    window = job.commit_window("uid:eeg", 0, 200)
    frozen_positions = window.positions
    frozen_values = [sample.value.copy() for sample in window]

    late = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        samples_chunk(1, [[3], [4]], [10.2, 10.3], [True, True]),
    )
    job.merge(Replica.from_bytes("vol-b", late))
    requery = window.requery()
    assert requery.revision == window.revision == 0
    assert requery.positions == frozen_positions
    for sample, value in zip(requery, frozen_values):
        np.testing.assert_array_equal(sample.value, value)
    assert isinstance(requery.samples[0], Observation)


def test_window_by_unified_time_selects_the_same_frozen_samples():
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        clock_offset(1, 0.0, 1.0),
        clock_offset(1, 100.0, 1.0),
        samples_chunk(1, [[1], [2], [3]], [10.0, 10.1, 10.2], [True, True, True]),
    )
    job = RecoveryJob([Replica.from_bytes("vol-a", raw)], archive(), dejitter=False)
    job.resolve()
    window = job.commit_window("uid:eeg", 11.0, 11.15, by="unified_time")
    assert window.positions
    assert window.requery().positions == window.positions


# ---------------------------------------------------------------------------
# Cross-check against pyxdf and layer separation


def test_complete_input_matches_pyxdf_sync_and_dejitter():
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        clock_offset(1, 0.0, 1.0),
        clock_offset(1, 100.0, 1.0),
        samples_chunk(
            1,
            [[1], [2], [3], [4]],
            [10.0, 0.0, 0.0, 10.3],
            [True, False, False, True],
        ),
    )
    view = RecoveryJob([Replica.from_bytes("vol-a", raw)], archive()).resolve()
    assert len(view.pyxdf_checks) == 1
    check = view.pyxdf_checks[0]
    assert check.complete_input
    assert check.values_match
    assert check.sync_time_matches
    assert check.unified_time_matches
    assert check.mismatched_positions == ()


def test_uncalibrated_epoch_leaves_a_unified_time_gap():
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        samples_chunk(1, [[1], [2]], [10.0, 0.0], [True, False]),
    )
    spec = StreamSpec(
        "uid:eeg",
        tdiff=0.1,
        position_origin=100,
        device_origin=10.0,
        epochs=(ClockEpoch(0, calibration_complete=False),),
    )
    view = RecoveryJob(
        [Replica.from_bytes("vol-a", raw)], RecordingArchive([spec]), dejitter=False
    ).resolve()
    stream = view.stream("uid:eeg")
    assert all(observation.unified_time is None for observation in stream)
    assert all(observation.unified_status == "uncalibrated" for observation in stream)
    assert all(gap.kind == "unified_time" for gap in stream.gaps)


def test_divergent_replica_values_are_reported_as_a_conflict():
    raw_a = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        samples_chunk(1, [[1], [2]], [10.0, 10.1], [True, True]),
    )
    raw_b = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        samples_chunk(1, [[1], [9]], [10.0, 10.1], [True, True]),
    )
    job = RecoveryJob(
        [Replica.from_bytes("vol-a", raw_a), Replica.from_bytes("vol-b", raw_b)],
        archive(),
        dejitter=False,
    )
    view = job.resolve()
    conflicts = view.stream("uid:eeg").conflicts
    assert conflicts
    assert conflicts[0].layer == "value"
    assert set(conflicts[0].volumes) == {"vol-a", "vol-b"}


def test_recovery_view_exposes_value_device_time_and_unified_time_separately():
    view = RecoveryJob(
        [Replica.from_bytes("vol-a", numeric_file())], archive()
    ).resolve()
    observation = view.at("uid:eeg", 101)
    assert observation.value_complete
    assert observation.device_time == pytest.approx(10.1)
    assert observation.unified_time is None
    assert observation.unified_status == "uncalibrated"
    # The device time is not silently mixed into the unified timeline.
    assert observation.device_time_status == "deduced"
    assert observation.position_status == "deduced"


def test_loss_gap_is_reported_and_deduction_starts_after_it():
    """A resync leaves a sample gap; the right side deduces from its own anchor."""
    first = samples_chunk(1, [[1], [2], [3]], [10.0, 0, 0], [True, False, False])
    second = samples_chunk(1, [[4], [5]], [30.0, 0], [True, False])
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        first,
        second,
    )
    start = raw.index(second)
    corrupted = raw[:start] + b"\x04\xff\xff\xff\xff" + boundary_chunk() + raw[start:]
    view = RecoveryJob(
        [Replica.from_bytes("vol-a", corrupted)], archive(), dejitter=False
    ).resolve()
    stream = view.stream("uid:eeg")
    sample_gaps = [gap for gap in stream.gaps if gap.kind == "sample"]
    assert sample_gaps
    assert sample_gaps[0].start_position == 103
    assert stream.at_position(300).device_time_status == "explicit"
    # The sample after the right anchor deduces from 300, not from 102 or from
    # the lost time between the two anchors.
    assert stream.at_position(301).predecessor_position == 300
    assert stream.at_position(301).device_time == pytest.approx(30.1)


def test_unverified_clock_offset_never_enters_calibration():
    bad_offset = clock_offset(1, 100.0, 5.0)
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        clock_offset(1, 0.0, 1.0),
        bad_offset[:-4],
        samples_chunk(1, [[1], [2]], [10.0, 0], [True, False]),
    )
    scan = scan_replica(Replica.from_bytes("vol-a", raw))
    offsets = scan.clock_offsets.get("uid:eeg", [])
    assert len(offsets) == 2
    assert [offset.verified for offset in offsets] == [True, False]
    view = RecoveryJob(
        [Replica.from_bytes("vol-a", raw)], archive(), dejitter=False
    ).resolve()
    # Only the verified offset is used; the incomplete one is reported as an
    # unverified check, not folded into the fit silently.
    assert view.checks.unverified("clock_offset")
    assert view.stream("uid:eeg").clock_offsets
    assert [offset.verified for offset in view.stream("uid:eeg").clock_offsets] == [
        True,
        False,
    ]


def test_checks_summary_is_split_by_volume():
    view = RecoveryJob(
        [Replica.from_bytes("vol-a", numeric_file())], archive()
    ).resolve()
    by_volume = view.checks.by_volume()
    assert "vol-a" in by_volume
    assert by_volume["vol-a"]["explicit_timestamp"] == (2, 2)


def test_clock_reset_uses_per_epoch_offsets_and_keeps_distinct_positions():
    """A device restart uses its own clock offsets and its own positions."""
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        clock_offset(1, 0.0, 1.0),
        clock_offset(1, 10.0, 1.0),
        clock_offset(1, 100.0, -9.0),
        clock_offset(1, 110.0, -9.0),
        samples_chunk(
            1,
            [[1], [2], [3], [4], [5], [6]],
            [10.0, 10.1, 10.2, 100.0, 100.1, 100.2],
            [True] * 6,
        ),
    )
    spec = StreamSpec(
        "uid:eeg",
        tdiff=0.1,
        epochs=(
            ClockEpoch(
                0,
                start_device_time=0,
                end_device_time=50,
                device_origin=0,
                first_clock_index=0,
                last_clock_index=1,
            ),
            ClockEpoch(
                1,
                start_device_time=50,
                end_device_time=200,
                position_offset=900,
                device_origin=100,
                first_clock_index=2,
                last_clock_index=3,
            ),
        ),
    )
    view = RecoveryJob(
        [Replica.from_bytes("vol-a", raw)], RecordingArchive([spec]), dejitter=False
    ).resolve()
    stream = view.stream("uid:eeg")
    assert [observation.position for observation in stream] == [
        100,
        101,
        102,
        900,
        901,
        902,
    ]
    assert [observation.epoch_id for observation in stream] == [0, 0, 0, 1, 1, 1]
    # Each epoch is synchronized with its own offsets (the second clock has a
    # different offset), and the values stay the original ones.
    assert [observation.unified_time for observation in stream] == pytest.approx(
        [11.0, 11.1, 11.2, 91.0, 91.1, 91.2]
    )
    assert [observation.value[0] for observation in stream] == pytest.approx(
        [1, 2, 3, 4, 5, 6]
    )
    check = next(c for c in view.pyxdf_checks if c.stream_key == "uid:eeg")
    assert check.sync_time_matches
    assert check.values_match


def test_irregular_stream_keeps_verified_explicit_times():
    """A stream without a sampling period keeps its verified times."""
    raw = assemble(
        file_header(),
        stream_header(
            1, uid="text", name="text", type="Markers", srate=0, fmt="string"
        ),
        string_samples_chunk(1, ["on", "off"], [5.0, 5.4], [True, True]),
    )
    view = RecoveryJob(
        [Replica.from_bytes("vol-a", raw)],
        RecordingArchive([StreamSpec("uid:text", tdiff=0.0)]),
        dejitter=False,
    ).resolve()
    stream = view.stream("uid:text")
    assert [observation.value for observation in stream] == [("on",), ("off",)]
    assert [observation.device_time for observation in stream] == [5.0, 5.4]
    assert all(observation.device_time_status == "explicit" for observation in stream)
    # There is no sampling period, so no unified timeline can be claimed.
    assert all(observation.unified_time is None for observation in stream)
    assert all(observation.unified_status == "uncalibrated" for observation in stream)


def test_late_bytes_resolve_gaps_and_report_which_layers_changed():
    header = stream_header(
        1, uid="markers", name="Marks", type="Markers", srate=10, fmt="string"
    )
    full = assemble(
        file_header(),
        header,
        string_samples_chunk(1, ["A", "B", "C"], [10.0, 0, 10.2], [True, False, True]),
    )
    scan = scan_replica(Replica.from_bytes("probe", full))
    target = scan.samples_for("uid:markers")[1]
    # Remove sample B's value bytes; the following sample can no longer be
    # parsed either, so replica A leaves both as gaps.
    replica_a = full[: target.value_range.start] + full[target.value_range.end :]
    markers = RecordingArchive(
        [StreamSpec("uid:markers", tdiff=0.1, position_origin=100, device_origin=10.0)]
    )
    job = RecoveryJob([Replica.from_bytes("vol-a", replica_a)], markers, dejitter=False)
    first = job.resolve()
    assert first.at("uid:markers", 101).value_complete is False
    # Sample C cannot be placed from replica A at all.
    assert first.at("uid:markers", 102) is None

    report = job.merge(Replica.from_bytes("vol-b", full))
    # The late copy fills sample B's value and adds sample C (which replica A
    # could not place at all), so the value and position layers change.  Sample
    # B's device time was already deduced from its actual predecessor, so it
    # does not move.
    assert report.resolved_gaps
    assert {"value", "position"} <= report.changed_layers()
    assert not report.device_time_changes
    after = job.resolve().stream("uid:markers")
    assert [observation.value for observation in after] == [
        ("A",),
        ("B",),
        ("C",),
    ]
    assert [observation.device_time for observation in after] == [10.0, 10.1, 10.2]


def test_clock_offset_only_merge_leaves_values_and_device_times_frozen():
    """A replica that brings only a late clock offset changes one layer only."""
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        clock_offset(1, 0.0, 1.0),
        clock_offset(1, 100.0, 1.0),
        samples_chunk(1, [[1], [2], [3]], [10.0, 10.1, 10.2], [True] * 3),
    )
    job = RecoveryJob([Replica.from_bytes("vol-a", raw)], archive(), dejitter=False)
    before = job.resolve()
    values = [observation.value.copy() for observation in before.stream("uid:eeg")]
    device_times = [observation.device_time for observation in before.stream("uid:eeg")]
    unified_times = [
        observation.unified_time for observation in before.stream("uid:eeg")
    ]

    # A late clock correction only: no sample bytes at all.
    late = ClockOffset(
        time=50.0,
        value=1.05,
        volume_id="vol-a",
        byte_range=ByteRange("vol-a", 0, 16),
    )
    report = job.merge_clock_offsets("vol-a", "uid:eeg", [late])
    assert report.changed_layers() == {"unified_time"}
    assert report.notes == ["late clock correction only"]
    after = job.resolve().stream("uid:eeg")
    for observation, value, device_time, unified_time in zip(
        after, values, device_times, unified_times
    ):
        np.testing.assert_array_equal(observation.value, value)
        assert observation.device_time == device_time
        assert observation.unified_time != unified_time


def test_time_revision_keeps_the_same_original_value():
    """Clock synchronization revises time, not the original sample value."""
    raw = assemble(
        file_header(),
        stream_header(1, uid="eeg", srate=10, fmt="float32"),
        clock_offset(1, 0.0, 1.0),
        clock_offset(1, 100.0, 1.0),
        samples_chunk(1, [[1], [2], [3]], [10.0, 10.1, 10.2], [True] * 3),
    )
    view = RecoveryJob([Replica.from_bytes("vol-a", raw)], archive()).resolve()
    stream = view.stream("uid:eeg")
    # The unified time is revised by the clock offset...
    assert [observation.unified_time for observation in stream] == pytest.approx(
        [11.0, 11.1, 11.2]
    )
    # ...while the values remain the original device values.
    assert [observation.value[0] for observation in stream] == pytest.approx([1, 2, 3])
    # And the pyxdf cross-check still finds the same values at the revised times.
    check = next(c for c in view.pyxdf_checks if c.stream_key == "uid:eeg")
    assert check.values_match
    assert check.sync_time_matches
    assert check.mismatched_positions == ()
