"""Lossless re-packing of legacy survey samples into SEG-Y Rev 1 reels.

The package implements a handover-driven archive packer:

* ``time_codes`` - exact (no rounding) SEG-Y Rev 1 time-scalar arithmetic.
* ``model``      - handover, segment and manifest data structures.
* ``planner``    - legal segment enumeration and minimum-reel planning.
* ``writer``     - SEG-Y Rev 1 output via Segpy plus a provenance manifest.
* ``verify``     - ordinary-reader read-back and one-shot coverage checks.
"""

from archive_pack.model import (
    HandoverItem,
    SourceTrace,
    Segment,
    ReelPlan,
    PackPlan,
    LosslessDeliveryError,
    UnrepresentableTimeField,
)
from archive_pack.planner import plan_packing
from archive_pack.writer import write_plan
from archive_pack.verify import verify_plan

__all__ = [
    'HandoverItem',
    'SourceTrace',
    'Segment',
    'ReelPlan',
    'PackPlan',
    'LosslessDeliveryError',
    'UnrepresentableTimeField',
    'plan_packing',
    'write_plan',
    'verify_plan',
]
