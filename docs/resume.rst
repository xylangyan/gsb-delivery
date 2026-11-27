..
  SPDX-FileCopyrightText: 2026

  SPDX-License-Identifier: Apache-2.0

=====================
Resumable Downloads
=====================

The classic CacheControl adapter caches whole responses only; a request that
carries a ``Range`` header bypasses the cache entirely. The
:mod:`cachecontrol.resume` subpackage adds an opt-in, range-aware cache that
can assemble one complete representation from independently delivered byte
segments. It is additive, so existing whole-response behaviour is unchanged.

A separate :class:`~cachecontrol.resume.transport.Transport` boundary keeps
the engine independent of :mod:`http.client`. :class:`InProcessTransport` is a
socket-free stand-in (handy for tests and local simulation), while
:class:`RequestsTransport` bridges onto a real :mod:`requests` session.

Concepts
========

* **Generation** -- one specific representation, identified by the tuple
  ``(ETag or Last-Modified, total length, Content-Encoding, Vary selection)``
  and keyed by a digest of that tuple. Bytes are only ever shared between
  segments of the same generation.
* **Segment** -- a cleanly terminated ``206`` response. Its range is taken
  from ``Content-Range`` and its bytes are checksummed. A half-closed transfer
  never produces a segment: the trailing, unterminated bytes are discarded.
* **Receipt** -- a sign-off written only when every byte of a generation is
  present and verified. The warehouse can hand over a generation only when it
  has a receipt.
* **Validation ledger** -- every conditional request records which generation
  it targeted, the validator sent, and whether the resulting confirmation was
  applied or rejected.

Joining rules
=============

Two segments may be joined only when they share one *strong* ETag, one total
length and one ``Content-Encoding``. Overlapping bytes must agree; a real
mismatch revokes any receipt already written for that generation.

``Vary`` decides which request header values distinguish entries. A header the
request omits (``None``) and a header present but empty (``""``) are distinct
keys. A fresh ``200`` or an *accepted* ``304`` may update the generation's
``Vary`` declaration.

Revalidation is generation-scoped. A ``304`` is applied only while it still
names the current generation for that Vary selection; if a newer generation
was established while the request was in flight, the stale ``304`` is rejected
even when its short ETag string happens to recur in another distribution zone.

With only a weak validator (``W/"..."``) or a date, a *whole* stored item
follows the ordinary ``If-None-Match`` / ``If-Modified-Since`` protocol. An
incomplete fragment has no joinable evidence under those validators and is
re-fetched from scratch.

Persistence and crashes
=======================

The store works over any :class:`~cachecontrol.cache.BaseCache`. With a
:class:`~cachecontrol.cache.SeparateBodyBaseCache` the manifest and the body
file are written separately. On every load the manifest is reconciled against
the bytes actually on disk (length plus per-segment digest), so an interrupted
write can never be mistaken for a complete generation: the warehouse observes
either a signed whole generation or an explicit list of missing ranges.

Basic usage
===========

.. code-block:: python

  from cachecontrol.cache import DictCache
  from cachecontrol.resume import InProcessTransport, ResumableCache, ResumeStore

  transport = InProcessTransport()
  transport.handle("/report", my_handler)

  cache = ResumableCache(transport, ResumeStore(DictCache()))

  # Claim the first segment, then backfill the remaining gaps.
  generation = cache.claim(
      "/report", {"X-Zone": "north"}, range_header="bytes=0-99"
  )
  generation = cache.backfill(generation, {"X-Zone": "north"})

  # handover() raises IncompleteGeneration if ranges are still missing.
  view = cache.handover(generation)
  assert view.is_signed and view.body

  # Where did each byte range come from, what is still valid, and what is the
  # minimum set of ranges left to fetch?
  for report in cache.audit("/report", {"X-Zone": "north"}):
      print(report.covered, report.validations, report.missing)

A reader that has opened a generation keeps working with that exact snapshot;
publishing a newer generation for the same URL does not change the bytes the
reader already holds.
