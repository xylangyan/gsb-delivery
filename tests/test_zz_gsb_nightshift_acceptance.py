import json
from pathlib import Path

import numpy as np
import pytest

from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.nightshift import (
    deliver_plan,
    plan_batch,
    plan_delivery,
    read_binary_seg,
    read_delivery_index,
    restore_check,
    restore_segment_sources,
)
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid


def type_a_artifact(index: int = 0) -> dict:
    return {
        "artifact_id": f"A{index}",
        "rows": 3,
        "columns": 3,
        "frame_numbers": [1, 2, 3],
        "source_sop_instance_uid": f"1.2.840.10008.test.{index}",
        "segments": {
            "A": {"frames": {1: [(1, 1)], 3: [(3, 2)]}},
            "B": {"frames": {1: [(1, 2)], 3: [(3, 1)]}},
            "C": {"frames": {1: [(1, 3)], 3: [(3, 2)]}},
            "D": {"frames": {1: [(1, 2)], 3: [(3, 3)]}},
        },
        "checks": [
            {"check_id": "AB", "segments": ["A", "B"], "score": 9},
            {"check_id": "BC", "segments": ["B", "C"], "score": 8},
            {"check_id": "AD", "segments": ["A", "D"], "score": 7},
            {"check_id": "CD", "segments": ["C", "D"], "score": 6},
            {
                "check_id": "AC",
                "segments": ["A", "C"],
                "score": 5,
                "allow_cross_images": True,
            },
        ],
    }


def type_b_artifact(index: int = 0) -> dict:
    return {
        "artifact_id": f"B{index}",
        "rows": 1,
        "columns": 1,
        "frame_numbers": [1],
        "segments": {"X": {"coordinates": [[1, 1, 1]]}},
        "checks": [{"check_id": "X1", "segments": ["X"], "score": 10}],
    }


def test_two_file_sample_delivers_22_points():
    result = plan_delivery({"quota": 2, "artifacts": [type_a_artifact()]})
    artifact = result["artifacts"][0]

    assert artifact["score"] == 22
    assert artifact["file_count"] == 2
    assert {check["check_id"] for check in artifact["checks"] if check["delivered"]} == {
        "AB",
        "BC",
        "AC",
    }
    ac = next(check for check in result["checks"] if check["check_id"] == "AC")
    assert len(ac["files"]) == 2
    assert ac["labels"] == {"A": 1, "C": 2}


def test_shared_batch_quota_prefers_30_type_a_artifacts():
    manifest = {
        "file_quota": 60,
        "artifacts": [type_a_artifact(i) for i in range(30)]
        + [type_b_artifact(i) for i in range(30)],
    }

    result = plan_batch(manifest)

    assert result["total_score"] == 660
    assert result["delivered_check_count"] == 90
    assert result["used_files"] == 60
    assert result["remaining_quota"] == 0
    assert len(result["quota_unallocated_artifacts"]) == 30
    assert {artifact["artifact_id"] for artifact in result["artifacts"][:30]} == {
        f"A{i}" for i in range(30)
    }
    assert all(
        artifact["allocation_status"] == "quota_unallocated"
        for artifact in result["artifacts"][30:]
    )


def test_overlap_blocker_reports_original_frame_and_pixel():
    artifact = type_a_artifact()
    artifact["checks"] = [
        {"check_id": "BD", "segments": ["B", "D"], "score": 100},
        {"check_id": "AC", "segments": ["A", "C"], "score": 50},
    ]

    result = plan_batch({"quota": 1, "artifacts": [artifact]})
    by_id = {check["check_id"]: check for check in result["checks"]}

    assert by_id["BD"]["status"] == "overlap_blocked"
    assert by_id["BD"]["overlaps"][0]["frame"] == 1
    assert (by_id["BD"]["overlaps"][0]["row"], by_id["BD"]["overlaps"][0]["column"]) == (1, 2)
    assert by_id["AC"]["overlaps"][0]["frame"] == 3
    assert (by_id["AC"]["overlaps"][0]["row"], by_id["AC"]["overlaps"][0]["column"]) == (3, 2)


def test_delivered_labelmaps_reread_check_labels_and_complete_b_mask(tmp_path: Path):
    manifest = {"file_quota": 2, "artifacts": [type_a_artifact()]}

    result = deliver_plan(manifest, output_dir=tmp_path)
    index = read_delivery_index(tmp_path)

    assert result["total_score"] == 22
    assert (tmp_path / "delivery_index.json").exists()
    ab = next(check for check in index["checks"] if check["check_id"] == "AB")
    bc = next(check for check in index["checks"] if check["check_id"] == "BC")
    assert ab["labels"] == {"A": 1, "B": 2}
    assert bc["labels"] == {"B": 1, "C": 2}

    ab_restored = restore_check(tmp_path, "AB")
    bc_restored = restore_check(tmp_path, "BC")
    b_masks = []
    for restored in (ab_restored, bc_restored):
        labelmap = restored["files"][0]["labelmap"]
        sources = restore_segment_sources(labelmap, "B")
        b_masks.append(sources)
        assert (
            "Background label=0" in labelmap["dataset"].ImageComments
            or "Background" in labelmap["dataset"].PixelValueMappingToCodedConceptSequence[0].PixelValueMappingExplanation
        )

    expected_b = {
        ("1.2.840.10008.test.0", 1): [(1, 2)],
        ("1.2.840.10008.test.0", 3): [(3, 1)],
    }
    assert b_masks[0] == expected_b
    assert b_masks[1] == expected_b


def test_read_binary_seg_maps_reordered_frames_and_repeated_segment_number(tmp_path: Path):
    seg_path = tmp_path / "vendor.dcm"
    frames = []
    for row, column in ((3, 2), (1, 1)):
        frame = np.zeros((3, 3), dtype=np.uint8)
        frame[row - 1, column - 1] = 1
        frames.append(frame)

    file_meta = FileMetaDataset()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.66.4"
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    dataset = FileDataset(str(seg_path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    dataset.is_little_endian = True
    dataset.is_implicit_VR = False
    dataset.SOPClassUID = file_meta.MediaStorageSOPClassUID
    dataset.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    dataset.Rows = 3
    dataset.Columns = 3
    dataset.NumberOfFrames = 2
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8
    dataset.BitsStored = 8
    dataset.HighBit = 7
    dataset.PixelRepresentation = 0
    dataset.PixelData = np.stack(frames).tobytes()

    segment = Dataset()
    segment.SegmentNumber = 1
    segment.SegmentLabel = "vendor-first"
    dataset.SegmentSequence = Sequence([segment])

    per_frame = Sequence()
    for original_frame in (3, 1):
        item = Dataset()
        identification = Dataset()
        identification.ReferencedSegmentNumber = 1
        item.SegmentIdentificationSequence = Sequence([identification])
        derivation = Dataset()
        source = Dataset()
        source.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
        source.ReferencedSOPInstanceUID = "1.2.840.10008.source"
        source.ReferencedFrameNumber = original_frame
        derivation.SourceImageSequence = Sequence([source])
        item.DerivationImageSequence = Sequence([derivation])
        per_frame.append(item)
    dataset.PerFrameFunctionalGroupsSequence = per_frame
    dataset.save_as(seg_path, enforce_file_format=True)

    catalog = read_binary_seg(
        seg_path,
        segment_map={"1": "A"},
        frame_numbers=[1, 2, 3],
    )

    assert catalog.point_sets["A"][1] == {0}
    assert catalog.point_sets["A"][3] == {7}
    assert catalog.provenance["A"][0]["vendor_segment_number"] == 1
    assert catalog.provenance["A"][1]["original_frame_number"] == 1


def test_preview_json_is_serializable_without_writing_dicoms(tmp_path: Path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"file_quota": 2, "artifacts": [type_a_artifact()]}),
        encoding="utf-8",
    )

    preview = plan_delivery(manifest_path)

    assert preview["total_score"] == 22
    assert list(tmp_path.glob("*.dcm")) == []
    json.dumps(preview, ensure_ascii=False)
