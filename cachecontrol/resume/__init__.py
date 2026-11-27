# SPDX-FileCopyrightText: 2026
#
# SPDX-License-Identifier: Apache-2.0

"""Resumable, range-aware, Vary/ETag-correct partial caching.

This subpackage is additive: the classic whole-response CacheControl code path
(:mod:`cachecontrol.adapter`, :mod:`cachecontrol.controller`) is unchanged. It
provides a separate state machine that can assemble one complete representation
from independently delivered byte segments, persisting over any
:class:`~cachecontrol.cache.BaseCache` (including the header/body-splitting
:class:`~cachecontrol.cache.SeparateBodyBaseCache`).

Public surface::

    from cachecontrol.resume import (
        ResumableCache, ResumeStore, InProcessTransport, RequestsTransport,
        GenerationView, AuditReport, ByteRange,
    )
"""

from __future__ import annotations

from cachecontrol.resume.engine import (
    AuditReport,
    GenerationView,
    RangeProvenance,
    ResumableCache,
)
from cachecontrol.resume.http import (
    ByteRange,
    HttpResponse,
    is_strong_etag,
    parse_content_range,
)
from cachecontrol.resume.store import (
    ByteConflict,
    Generation,
    GenerationIdentity,
    IncompleteGeneration,
    Receipt,
    ResumeStore,
    Segment,
    ValidationRecord,
)
from cachecontrol.resume.transport import (
    InProcessTransport,
    IncompleteTransfer,
    Interrupt,
    RequestsTransport,
    Transport,
)

__all__ = [
    "AuditReport",
    "ByteConflict",
    "ByteRange",
    "Generation",
    "GenerationIdentity",
    "GenerationView",
    "InProcessTransport",
    "IncompleteTransfer",
    "IncompleteGeneration",
    "Interrupt",
    "RangeProvenance",
    "Receipt",
    "RequestsTransport",
    "ResumableCache",
    "ResumeStore",
    "Segment",
    "Transport",
    "ValidationRecord",
    "HttpResponse",
    "is_strong_etag",
    "parse_content_range",
]
