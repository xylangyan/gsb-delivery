"""Night-shift Binary SEG to DICOM Label Map planning and delivery."""

from .planner import plan_artifact, plan_batch, plan_delivery
from .delivery import (
    deliver_plan,
    find_check,
    preview_delivery,
    read_delivery_index,
    restore_check,
)
from .dicom_io import (
    catalog_from_artifact,
    read_binary_seg,
    read_labelmap,
    restore_segment_sources,
    segment_mask,
    write_labelmap,
)
from .conversion_entry import (
    convert_binary_seg_to_labelmap,
    convert_seg_to_labelmap,
    deliver_labelmaps,
    labelmap_from_seg,
    plan_labelmap_delivery,
    seg_to_labelmap,
)

plan_nightshift = plan_batch
preview_nightshift = preview_delivery
deliver_nightshift = deliver_plan
plan_manifest = plan_batch
optimize_delivery = plan_batch
optimize_batch = plan_batch
schedule_delivery = plan_batch
build_labelmaps = deliver_plan
write_labelmaps = deliver_plan
convert_manifest = deliver_plan
run_delivery = deliver_plan
plan = plan_batch
preview = preview_delivery
deliver = deliver_plan
optimize = plan_batch
convert = convert_seg_to_labelmap


class NightShiftPlanner:
    """Small object-style facade around the functional API."""

    def __init__(self, manifest: dict | str | None = None, **kwargs):
        self.manifest = manifest
        self.kwargs = kwargs

    def plan(self, quota: int | None = None, **kwargs):
        return plan_batch(self.manifest, quota=quota, **{**self.kwargs, **kwargs})

    preview = plan
    optimize = plan

    def deliver(self, output_dir=None, quota: int | None = None, **kwargs):
        return deliver_plan(
            self.manifest,
            output_dir=output_dir,
            quota=quota,
            **{**self.kwargs, **kwargs},
        )


class DeliveryPlanner(NightShiftPlanner):
    pass

__all__ = [
    "DeliveryPlanner",
    "NightShiftPlanner",
    "build_labelmaps",
    "catalog_from_artifact",
    "convert_binary_seg_to_labelmap",
    "convert_manifest",
    "convert_seg_to_labelmap",
    "convert",
    "deliver",
    "deliver_labelmaps",
    "deliver_nightshift",
    "deliver_plan",
    "find_check",
    "optimize",
    "optimize_batch",
    "optimize_delivery",
    "plan",
    "plan_artifact",
    "plan_batch",
    "plan_delivery",
    "plan_labelmap_delivery",
    "plan_manifest",
    "plan_nightshift",
    "preview",
    "preview_delivery",
    "preview_nightshift",
    "read_binary_seg",
    "read_delivery_index",
    "read_labelmap",
    "restore_check",
    "restore_segment_sources",
    "run_delivery",
    "schedule_delivery",
    "segment_mask",
    "write_labelmap",
    "write_labelmaps",
]
