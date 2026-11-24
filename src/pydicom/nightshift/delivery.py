"""Write planned Label Maps and maintain a re-readable delivery index."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .dicom_io import catalog_from_artifact, read_labelmap, write_labelmap
from .planner import plan_batch


def preview_delivery(
    manifest: dict[str, Any] | str | Path,
    quota: int | None = None,
    *positional: Any,
    file_quota: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Return the duty-officer arrangement without creating pixel volumes."""
    if quota is not None and not isinstance(quota, int):
        file_quota = quota if file_quota is None else file_quota
        quota = positional[0] if positional else None
    effective_quota = quota if quota is not None else file_quota
    if isinstance(manifest, (str, Path)):
        source_path = Path(manifest)
        manifest_data = json.loads(source_path.read_text(encoding="utf-8"))
        base_dir = str(source_path.parent)
    else:
        manifest_data = dict(manifest)
        base_dir = None
    return plan_batch(
        manifest_data,
        quota=effective_quota,
        base_dir=base_dir,
        file_quota=file_quota if quota is None else None,
        **kwargs,
    )


def deliver_plan(
    manifest: dict[str, Any] | str | Path,
    output_dir: str | Path | int | None = None,
    quota: int | None = None,
    *positional: Any,
    file_quota: int | None = None,
    plan: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Plan if needed and write all selected DICOM Label Map instances."""
    if isinstance(output_dir, int) and quota is None:
        quota = output_dir
        output_dir = kwargs.pop("output_dir", None)
    elif quota is None and positional and isinstance(positional[0], int):
        quota = positional[0]
    effective_quota = quota if quota is not None else file_quota
    base_dir: str | None = None
    if isinstance(manifest, list):
        manifest_data = {"artifacts": manifest}
    elif isinstance(manifest, (str, Path)):
        source_path = Path(manifest)
        base_dir = str(source_path.parent)
        manifest_data = json.loads(source_path.read_text(encoding="utf-8"))
    else:
        manifest_data = dict(manifest or {})

    output = Path(
        output_dir
        or manifest_data.get("output_dir")
        or (Path(base_dir) / "labelmaps" if base_dir else "labelmaps")
    )
    output.mkdir(parents=True, exist_ok=True)

    delivery_plan = plan or plan_batch(
        manifest_data,
        quota=effective_quota,
        base_dir=base_dir,
        output_dir=str(output),
        **kwargs,
    )
    artifacts_by_id = {
        str(artifact.get("artifact_id") or artifact.get("id")): artifact
        for artifact in manifest_data.get("artifacts", [])
    }

    written_files: list[dict[str, Any]] = []
    for file_record in delivery_plan["files"]:
        artifact_id = file_record["artifact_id"]
        artifact = artifacts_by_id[artifact_id]
        catalog = catalog_from_artifact(artifact, base_dir=base_dir)
        file_path = output / f"{file_record['file_id']}.dcm"
        file_plan = {
            "file_index": file_record["file_index"],
            "segments": file_record["segments"],
            "label_values": file_record["label_values"],
            "file_id": file_record["file_id"],
        }
        check_records = [
            _embedded_check(record, file_record["file_id"])
            for record in delivery_plan["checks"]
            if record["artifact_id"] == artifact_id
            and file_record["file_id"] in record.get("files", [])
        ]
        metadata = write_labelmap(
            file_path,
            artifact=artifact,
            file_plan=file_plan,
            catalog=catalog,
            check_records=check_records,
            plan_metadata={
                "total_score": delivery_plan["total_score"],
                "quota": delivery_plan["quota"],
                "delivery_index": "delivery_index.json",
            },
        )
        file_record = {
            **file_record,
            "path": str(file_path),
            "sop_instance_uid": metadata.get("sop_instance_uid"),
        }
        written_files.append(file_record)

    delivery_plan["output_dir"] = str(output)
    delivery_plan["files"] = written_files
    index_path = output / "delivery_index.json"
    index_path.write_text(
        json.dumps(_json_safe(delivery_plan), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    preview_path = output / "delivery_preview.json"
    preview_path.write_text(
        json.dumps(_json_safe(delivery_plan), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    delivery_plan["index_path"] = str(index_path)
    return delivery_plan


def _sop_uid(path: Path) -> str | None:
    try:
        return read_labelmap(path)["dataset"].SOPInstanceUID
    except Exception:
        return None


def _embedded_check(record: dict[str, Any], file_id: str) -> dict[str, Any]:
    labels_in_file = {
        segment: label
        for segment, label in record.get("labels", {}).items()
        if record.get("segment_files", {}).get(segment) == file_id
    }
    segments_in_file = [
        segment
        for segment, assigned_file in record.get("segment_files", {}).items()
        if assigned_file == file_id
    ]
    return {
        "check_id": record["check_id"],
        "artifact_id": record["artifact_id"],
        "status": record["status"],
        "score": record.get("score", 0),
        "file_id": file_id,
        "segments_in_this_file": segments_in_file,
        "label_values": labels_in_file,
        "all_files": record.get("files", []),
        "all_label_values": record.get("labels", {}),
        "segment_files": record.get("segment_files", {}),
    }


def read_delivery_index(
    output_dir_or_index: str | Path,
) -> dict[str, Any]:
    path = Path(output_dir_or_index)
    if path.is_dir():
        path = path / "delivery_index.json"
    return json.loads(path.read_text(encoding="utf-8"))


def find_check(
    index: dict[str, Any],
    check_id: str,
    *,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    matches = [
        record
        for record in index.get("checks", [])
        if record.get("check_id") == check_id
        and (artifact_id is None or record.get("artifact_id") == artifact_id)
    ]
    if not matches:
        raise KeyError(f"check {check_id!r} was not delivered")
    if artifact_id is None and len(matches) > 1:
        raise KeyError(
            f"check {check_id!r} is ambiguous; pass artifact_id; "
            f"found {[item['artifact_id'] for item in matches]}"
        )
    return matches[0]


def restore_check(
    output_dir_or_index: str | Path | dict[str, Any],
    check_id: str,
    *,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """Reread all files for a check and restore masks in source coordinates."""
    if isinstance(output_dir_or_index, dict):
        index = output_dir_or_index
        base = Path(index.get("output_dir", "."))
    else:
        source = Path(output_dir_or_index)
        if source.is_file():
            index = read_delivery_index(source)
            base = source.parent
        else:
            index = read_delivery_index(source)
            base = source
    record = find_check(index, check_id, artifact_id=artifact_id)
    restored_files = []
    for file_id in record.get("files", []):
        file_record = next(
            item for item in index["files"] if item["file_id"] == file_id
        )
        path = Path(file_record.get("path") or base / f"{file_id}.dcm")
        if not path.is_absolute() and not path.exists():
            path = base / path
        labelmap = read_labelmap(path)
        segments = [
            segment
            for segment, assigned_file in record.get("segment_files", {}).items()
            if assigned_file == file_id
        ]
        if not segments:
            segments = [
                segment
                for segment, label in record.get("labels", {}).items()
                if label in labelmap["label_values"]
            ]
        restored_files.append(
            {
                "file_id": file_id,
                "path": str(path),
                "labelmap": labelmap,
                "segments": segments,
            }
        )
    return {"check": record, "files": restored_files}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
