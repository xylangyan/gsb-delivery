# SPDX-FileCopyrightText: 2026 spdx contributors
#
# SPDX-License-Identifier: Apache-2.0
"""JSON to TagValue handoff for archived SPDX-2 catalogs.

This package turns one or more JSON catalogs (as read by
``spdx_tools.spdx.parser.json``) into a TagValue package that reads back to the
same relationship multiset, using a shared, pre-determined package order and
the smallest achievable explicit-relationship byte count per revision.
"""

from spdx_tools.spdx.handoff.encoding import (
    UnwritableRelationship,
    classify_comment,
    classify_relationship,
    describe_relationship,
    render_relationship,
    relationship_byte_cost,
    relationship_key,
)

__all__ = [
    "UnwritableRelationship",
    "classify_comment",
    "classify_relationship",
    "describe_relationship",
    "render_relationship",
    "relationship_byte_cost",
    "relationship_key",
]
