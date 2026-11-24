"""DICOM Binary SEG input and Label Map output entry points."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from .geometry import MaskCatalog, make_catalog

PRIVATE_GROUP = 0x0041
PRIVATE_CREATOR = "GSB_NIGHTSHIFT"
PRIVATE_TAG_OFFSET = 0x10
METADATA_SCHEMA = "pydicom.nightshift.labelmap/v1"


def _numpy():
    import numpy as np

    return np


def _pydicom():
    import pydicom

    return pydicom


def catalog_from_artifact(
    artifact: dict[str, Any], *, base_dir: str | Path | None = None
) -> MaskCatalog:
    """Build a catalog from explicit masks and any referenced Binary SEG files."""
    catalog = make_catalog(artifact)
    references = (
        artifact.get("seg_instances")
        or artifact.get("vendor_seg_instances")
        or artifact.get("seg_files")
        or []
    )
    if isinstance(references, (str, Path, dict)):
        references = [references]

    base = Path(base_dir or artifact.get("base_dir") or ".")
    for reference in references:
        if isinstance(reference, (str, Path)):
            reference = {"path": reference}
        if not isinstance(reference, dict):
            continue
        path_value = reference.get("path") or reference.get("file") or reference.get("filename")
        if path_value is None:
            dataset = reference.get("dataset")
            if dataset is None:
                continue
            path: str | Path = dataset
        else:
            path = Path(path_value)
            if not path.is_absolute() and base_dir is not None:
                path = base / path

        mapping = (
            reference.get("segment_map")
            or reference.get("mapping")
            or reference.get("segment_number_to_business_segment")
            or {}
        )
        frame_map = (
            reference.get("frame_map")
            or reference.get("encoded_frame_to_original_frame")
            or reference.get("frame_order")
            or artifact.get("frame_map")
            or artifact.get("encoded_frame_to_original_frame")
            or artifact.get("frame_order")
        )
        explicit_frame_numbers = artifact.get("frame_numbers")
        seg_catalog = read_binary_seg(
            path,
            segment_map=mapping,
            rows=catalog.rows or artifact.get("rows"),
            columns=catalog.columns or artifact.get("columns"),
            frame_numbers=explicit_frame_numbers,
            source_sop_instance_uid=catalog.source_uid(0),
            frame_map=frame_map,
        )
        _merge_catalog(catalog, seg_catalog, reference)
    return catalog


def _merge_catalog(
    catalog: MaskCatalog, incoming: MaskCatalog, reference: dict[str, Any]
) -> None:
    if not catalog.rows:
        catalog.rows = incoming.rows
    if not catalog.columns:
        catalog.columns = incoming.columns
    if not catalog.frame_numbers:
        catalog.frame_numbers = incoming.frame_numbers
    catalog.source_sop_instance_uids.update(incoming.source_sop_instance_uids)
    for segment, frames in incoming.point_sets.items():
        target = catalog.point_sets.setdefault(segment, {})
        for frame, pixels in frames.items():
            target.setdefault(frame, set()).update(pixels)
        catalog.provenance.setdefault(segment, []).append(
            {
                "seg_path": str(reference.get("path") or reference.get("file") or "<dataset>"),
                "segment_map": reference.get("segment_map") or reference.get("mapping"),
            }
        )
    for segment, frames in incoming.frame_arrays.items():
        catalog.frame_arrays.setdefault(segment, {}).update(frames)


def read_binary_seg(
    source: Any,
    *,
    segment_map: dict[str | int, str] | None = None,
    rows: int | None = None,
    columns: int | None = None,
    frame_numbers: list[int] | None = None,
    source_sop_instance_uid: str | None = None,
    frame_map: list[int] | dict[int, int] | None = None,
) -> MaskCatalog:
    """Read an uncompressed DICOM BINARY SEG into canonical source coordinates."""
    pydicom = _pydicom()
    np = _numpy()

    if isinstance(source, (str, Path)):
        dataset = pydicom.dcmread(str(source))
        pixel_source: Any = str(source)
    else:
        dataset = source
        pixel_source = source

    rows = int(rows or dataset.Rows)
    columns = int(columns or dataset.Columns)
    number_of_frames = int(getattr(dataset, "NumberOfFrames", 1) or 1)
    canonical_frames = [int(value) for value in (frame_numbers or range(1, number_of_frames + 1))]

    catalog = MaskCatalog(
        rows=rows,
        columns=columns,
        frame_numbers=canonical_frames,
        source_sop_instance_uids={0: source_sop_instance_uid}
        if source_sop_instance_uid
        else {},
    )

    mapping = {str(key): str(value) for key, value in (segment_map or {}).items()}
    explicit_map = _normalize_frame_map(frame_map)

    try:
        frame_iterator = pydicom.pixels.iter_pixels(pixel_source)
    except Exception:
        frame_iterator = iter(dataset.pixel_array)

    for encoded_index, array in enumerate(frame_iterator):
        if encoded_index >= number_of_frames:
            break
        vendor_number = _frame_segment_number(dataset, encoded_index)
        business_segment = mapping.get(str(vendor_number), f"SEGMENT_{vendor_number}")
        original_frame, source_uid = _frame_source(
            dataset,
            encoded_index,
            encoded_index,
            number_of_frames,
            canonical_frames,
            explicit_map,
        )
        if source_uid:
            catalog.source_sop_instance_uids[int(original_frame)] = source_uid
            catalog.source_sop_instance_uids.setdefault(0, source_uid)

        mask = np.asarray(array)
        if mask.ndim == 3:
            mask = mask[..., 0]
        row_indices, column_indices = np.nonzero(mask)
        for row0, column0 in zip(
            row_indices.tolist(), column_indices.tolist(), strict=False
        ):
            catalog.add_point(
                business_segment,
                int(original_frame),
                int(row0) + 1,
                int(column0) + 1,
            )
        catalog.provenance.setdefault(business_segment, []).append(
            {
                "vendor_segment_number": vendor_number,
                "encoded_seg_frame": encoded_index + 1,
                "original_frame_number": int(original_frame),
                "source_sop_instance_uid": source_uid,
            }
        )

    observed_frames = [
        int(frame)
        for frames in catalog.point_sets.values()
        for frame in frames
    ]
    observed_frames.extend(
        int(frame)
        for frames in catalog.frame_arrays.values()
        for frame in frames
    )
    maximum_original_frame = max(
        [len(catalog.frame_numbers), *observed_frames]
    )
    if maximum_original_frame > len(catalog.frame_numbers):
        catalog.frame_numbers = list(range(1, maximum_original_frame + 1))

    return catalog


def _normalize_frame_map(frame_map: Any) -> dict[int, int]:
    if frame_map is None:
        return {}
    if isinstance(frame_map, dict):
        return {int(key): int(value) for key, value in frame_map.items()}
    return {index: int(value) for index, value in enumerate(frame_map)}


def _frame_functional_item(dataset: Any, index: int) -> Any | None:
    sequence = getattr(dataset, "PerFrameFunctionalGroupsSequence", None)
    if not sequence or index >= len(sequence):
        return None
    return sequence[index]


def _frame_segment_number(dataset: Any, index: int) -> int:
    item = _frame_functional_item(dataset, index)
    if item is not None:
        identification = getattr(item, "SegmentIdentificationSequence", None)
        if identification:
            return int(identification[0].ReferencedSegmentNumber)
    if hasattr(dataset, "SegmentSequence") and len(dataset.SegmentSequence) == 1:
        return int(dataset.SegmentSequence[0].SegmentNumber)
    return index + 1


def _frame_source(
    dataset: Any,
    functional_index: int,
    encoded_index: int,
    number_of_frames: int,
    canonical_frames: list[int],
    explicit_map: dict[int, int],
) -> tuple[int, str | None]:
    if encoded_index in explicit_map:
        frame = explicit_map[encoded_index]
        return frame, None

    item = _frame_functional_item(dataset, functional_index)
    if item is not None:
        derivation = getattr(item, "DerivationImageSequence", None)
        if derivation:
            source_sequence = getattr(derivation[0], "SourceImageSequence", None)
            if source_sequence:
                source_item = source_sequence[0]
                uid = getattr(source_item, "ReferencedSOPInstanceUID", None)
                referenced_frame = getattr(source_item, "ReferencedFrameNumber", None)
                if referenced_frame is not None:
                    if hasattr(referenced_frame, "__iter__") and not isinstance(
                        referenced_frame, (str, bytes)
                    ):
                        referenced_frame = referenced_frame[0]
                    return int(referenced_frame), str(uid) if uid else None
                if uid:
                    return _default_frame(
                        encoded_index, number_of_frames, canonical_frames
                    ), str(uid)

        referenced_frame = _find_named_value(item, "ReferencedFrameNumber")
        referenced_uid = _find_named_value(item, "ReferencedSOPInstanceUID")
        if referenced_frame is not None:
            return int(referenced_frame), str(referenced_uid) if referenced_uid else None

    return _default_frame(encoded_index, number_of_frames, canonical_frames), None


def _default_frame(
    encoded_index: int, number_of_frames: int, canonical_frames: list[int]
) -> int:
    if len(canonical_frames) == number_of_frames:
        return int(canonical_frames[encoded_index])
    if encoded_index < len(canonical_frames):
        return int(canonical_frames[encoded_index])
    return encoded_index + 1


def _find_named_value(dataset: Any, keyword: str) -> Any:
    try:
        if keyword in dataset:
            return dataset[keyword].value
    except Exception:
        pass
    for element in getattr(dataset, "iterall", lambda: [])():
        value = getattr(element, "value", None)
        if hasattr(value, "__iter__") and not isinstance(value, (str, bytes)):
            for item in value:
                found = _find_named_value(item, keyword)
                if found is not None:
                    return found
    return None


def write_labelmap(
    output_path: str | Path,
    *,
    artifact: dict[str, Any],
    file_plan: dict[str, Any],
    catalog: MaskCatalog | None = None,
    check_records: list[dict[str, Any]] | None = None,
    plan_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write one directly readable DICOM Label Map file."""
    pydicom = _pydicom()
    np = _numpy()
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.sequence import Sequence
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid

    catalog = catalog or catalog_from_artifact(artifact)
    segments = list(file_plan["segments"])
    label_values = dict(file_plan.get("label_values") or {})
    if not label_values:
        label_values = {
            segment: label for label, segment in enumerate(sorted(segments), start=1)
        }
    maximum_label = max(int(value) for value in label_values.values())
    dtype = np.uint8 if maximum_label <= 255 else np.dtype("<u2")
    frame_count = len(catalog.frame_numbers) or int(artifact.get("number_of_frames", 1))
    rows = catalog.rows or int(artifact["rows"])
    columns = catalog.columns or int(artifact["columns"])
    shape = (frame_count, rows, columns)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_raw = tempfile.NamedTemporaryFile(
        prefix="pydicom-labelmap-", suffix=".raw", delete=False
    )
    temporary_raw.close()
    volume = np.memmap(temporary_raw.name, dtype=dtype, mode="w+", shape=shape)
    volume[:] = 0
    actual_labels: list[dict[str, Any]] = []
    for segment in segments:
        label = int(label_values[segment])
        painted = catalog.paint(segment, volume, label)
        actual_labels.append(
            {
                "label_value": label,
                "business_segment": segment,
                "nonzero_voxels": int(painted),
                "description": f"Label {label} is business segment {segment}",
                "provenance": catalog.provenance.get(segment, []),
            }
        )
    volume.flush()
    del volume
    pixel_bytes = Path(temporary_raw.name).read_bytes()
    try:
        Path(temporary_raw.name).unlink(missing_ok=True)
    except OSError:
        pass

    sop_uid = generate_uid()
    class_uid = pydicom.uid.LabelMapSegmentationStorage
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = class_uid
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = pydicom.uid.PYDICOM_IMPLEMENTATION_UID

    dataset = FileDataset(
        str(output_path),
        {},
        file_meta=file_meta,
        preamble=b"\0" * 128,
    )
    dataset.is_little_endian = True
    dataset.is_implicit_VR = False

    dataset.SOPClassUID = class_uid
    dataset.SOPInstanceUID = sop_uid
    dataset.StudyInstanceUID = str(
        artifact.get("study_instance_uid") or generate_uid()
    )
    dataset.SeriesInstanceUID = str(
        artifact.get("series_instance_uid") or generate_uid()
    )
    dataset.Modality = "SEG"
    dataset.ConversionType = "DI"
    dataset.PatientID = str(artifact.get("patient_id", artifact.get("artifact_id", "ARTIFACT")))
    dataset.PatientName = str(artifact.get("patient_name", "NIGHTSHIFT"))

    dataset.SegmentationType = "LABELMAP"
    dataset.SegmentsOverlap = "NO"
    dataset.Rows = rows
    dataset.Columns = columns
    dataset.NumberOfFrames = frame_count
    dataset.SamplesPerPixel = 1
    dataset.PhotometricInterpretation = "MONOCHROME2"
    dataset.BitsAllocated = 8 if dtype is np.uint8 else 16
    dataset.BitsStored = dataset.BitsAllocated
    dataset.HighBit = dataset.BitsStored - 1
    dataset.PixelRepresentation = 0
    dataset.SmallestImagePixelValue = 0
    dataset.LargestImagePixelValue = maximum_label
    dataset.PixelData = pixel_bytes

    segment_sequence = Sequence()
    for label_info in actual_labels:
        item = Dataset()
        label = label_info["label_value"]
        segment = label_info["business_segment"]
        item.SegmentNumber = label
        item.SegmentLabel = segment
        item.SegmentDescription = label_info["description"]
        item.RecommendedDisplayGrayscaleValue = label
        category = Dataset()
        category.CodeValue = "91723000"
        category.CodingSchemeDesignator = "SCT"
        category.CodeMeaning = "Anatomical structure"
        item.SegmentedPropertyCategoryCodeSequence = Sequence([category])
        property_type = Dataset()
        property_type.CodeValue = "85756007"
        property_type.CodingSchemeDesignator = "SCT"
        property_type.CodeMeaning = "Tissue"
        item.SegmentedPropertyTypeCodeSequence = Sequence([property_type])
        algorithm = Dataset()
        algorithm.AlgorithmType = "AUTOMATIC"
        algorithm.AlgorithmName = "PYDICOM_NIGHTSHIFT_LABELMAP"
        item.SegmentationAlgorithmIdentificationSequence = Sequence([algorithm])
        segment_sequence.append(item)
    dataset.SegmentSequence = segment_sequence

    frame_sequence = Sequence()
    frame_provenance: dict[str, Any] = {}
    default_source_uid = catalog.source_uid(0) or artifact.get(
        "source_sop_instance_uid", sop_uid
    )
    source_class_uid = artifact.get(
        "source_sop_class_uid", "1.2.840.10008.5.1.4.1.1.7"
    )
    for output_index, original_frame in enumerate(catalog.frame_numbers, start=1):
        source_uid = catalog.source_uid(int(original_frame)) or default_source_uid
        frame_provenance[str(output_index)] = {
            "labelmap_frame": output_index,
            "original_frame_number": int(original_frame),
            "source_sop_instance_uid": source_uid,
            "source_sop_class_uid": source_class_uid,
        }
        functional = Dataset()
        derivation = Dataset()
        source_item = Dataset()
        source_item.ReferencedSOPClassUID = source_class_uid
        source_item.ReferencedSOPInstanceUID = source_uid
        source_item.ReferencedFrameNumber = int(original_frame)
        derivation.SourceImageSequence = Sequence([source_item])
        functional.DerivationImageSequence = Sequence([derivation])
        frame_sequence.append(functional)
    dataset.PerFrameFunctionalGroupsSequence = frame_sequence

    mapping_sequence = Sequence()
    background = Dataset()
    background.MappedPixelValue = 0
    background.PixelValueMappingExplanation = "Background; no business segment"
    mapping_sequence.append(background)
    for label_info in actual_labels:
        item = Dataset()
        item.MappedPixelValue = label_info["label_value"]
        item.PixelValueMappingExplanation = label_info["description"]
        mapping_sequence.append(item)
    dataset.PixelValueMappingToCodedConceptSequence = mapping_sequence

    label_table = [
        {
            "label_value": 0,
            "role": "background",
            "business_segment": None,
            "description": "Background; zero is the only background value",
        },
        *actual_labels,
    ]
    metadata = {
        "schema": METADATA_SCHEMA,
        "artifact_id": artifact.get("artifact_id") or artifact.get("id"),
        "file_index": file_plan.get("file_index"),
        "file_id": file_plan.get("file_id"),
        "background_label_value": 0,
        "sop_instance_uid": sop_uid,
        "label_table": label_table,
        "frame_provenance": frame_provenance,
        "checks": check_records or [],
        "plan": plan_metadata or {},
    }
    dataset.ImageComments = (
        "Background label=0; "
        + "; ".join(f"{item['label_value']}={item['business_segment']}" for item in actual_labels)
    )
    block = dataset.private_block(PRIVATE_GROUP, PRIVATE_CREATOR, create=True)
    block.add_new(PRIVATE_TAG_OFFSET, "UT", json.dumps(metadata, ensure_ascii=False))

    dataset.save_as(output_path, enforce_file_format=True)
    metadata["path"] = str(output_path)
    return metadata


def read_labelmap(path: str | Path | Any) -> dict[str, Any]:
    """Re-read a label map and expose labels, metadata and source restoration."""
    pydicom = _pydicom()
    np = _numpy()
    if not hasattr(path, "private_block"):
        dataset = pydicom.dcmread(str(path))
        path_value = str(path)
    else:
        dataset = path
        path_value = getattr(path, "filename", "<dataset>")

    block = dataset.private_block(PRIVATE_GROUP, PRIVATE_CREATOR)
    metadata_tag = block.get_tag(PRIVATE_TAG_OFFSET)
    raw_metadata = dataset[metadata_tag].value
    if isinstance(raw_metadata, bytes):
        raw_metadata = raw_metadata.decode("utf-8")
    metadata = json.loads(raw_metadata)
    array = np.asarray(dataset.pixel_array)
    metadata["path"] = path_value
    metadata["metadata"] = metadata
    metadata["dataset"] = dataset
    metadata["pixel_array"] = array
    metadata["label_values"] = [item["label_value"] for item in metadata["label_table"]]
    return metadata


def segment_mask(labelmap: dict[str, Any], segment: str | None = None, label: int | None = None):
    array = labelmap["pixel_array"]
    if label is None:
        for item in labelmap["label_table"]:
            if item.get("business_segment") == segment:
                label = int(item["label_value"])
                break
    if label is None:
        raise KeyError(f"segment {segment!r} is not present in {labelmap.get('path')}")
    return array == label


def restore_segment_sources(
    labelmap: dict[str, Any], segment: str
) -> dict[tuple[str, int], list[tuple[int, int]]]:
    """Return 1-based coordinates grouped by original SOP UID and frame."""
    np = _numpy()
    mask = segment_mask(labelmap, segment=segment)
    result: dict[tuple[str, int], list[tuple[int, int]]] = {}
    for frame_index in range(mask.shape[0]):
        rows, columns = np.nonzero(mask[frame_index])
        if not len(rows):
            continue
        provenance = labelmap["frame_provenance"][str(frame_index + 1)]
        key = (
            provenance["source_sop_instance_uid"],
            int(provenance["original_frame_number"]),
        )
        result[key] = [
            (int(row) + 1, int(column) + 1)
            for row, column in zip(rows.tolist(), columns.tolist(), strict=False)
        ]
    return result
