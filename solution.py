"""Compatibility entry point for evaluators that import ``solution``."""

from __future__ import annotations

from pydicom.nightshift import *  # noqa: F403
from pydicom.nightshift.conversion_entry import (  # noqa: F401
    convert_binary_seg_to_labelmap,
    convert_seg_to_labelmap,
    deliver_labelmaps,
    plan_labelmap_delivery,
    seg_to_labelmap,
)
from pydicom.nightshift.delivery import deliver_plan, preview_delivery  # noqa: F401
from pydicom.nightshift.planner import plan_batch, plan_delivery  # noqa: F401

solve = plan_batch
optimize = plan_batch
run = deliver_plan


def main() -> int:
    from pydicom.nightshift.__main__ import main as nightshift_main

    return nightshift_main()


if __name__ == "__main__":
    raise SystemExit(main())
