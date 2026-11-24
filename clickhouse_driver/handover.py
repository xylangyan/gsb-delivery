"""
Public entry point for LowCardinality column handover.

The implementation lives next to the column it belongs to:
``clickhouse_driver.columns.lowcardinalityhandover``.  This module only
re-exports it so callers do not have to reach into ``columns``.
"""

from .columns.lowcardinalityhandover import (  # noqa: F401
    HandoverError,
    HandoverFormatError,
    HandoverPermissionError,
    HandoverVerificationError,
    HandoverPlan,
    LowCardinalityFragment,
    SegmentPlan,
    analyze_fragments,
    driver_key_width,
    encode_fragment,
    fragment_from_parts,
    fragment_from_rows,
    parse_fragment,
    plan_handover,
)


__all__ = [
    'HandoverError',
    'HandoverFormatError',
    'HandoverPermissionError',
    'HandoverVerificationError',
    'HandoverPlan',
    'LowCardinalityFragment',
    'SegmentPlan',
    'analyze_fragments',
    'driver_key_width',
    'encode_fragment',
    'fragment_from_parts',
    'fragment_from_rows',
    'parse_fragment',
    'plan_handover',
]
