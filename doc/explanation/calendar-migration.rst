.. _`calendar-migration`:

Timezone-table migration
========================

The :mod:`ics.migration` toolkit reconciles two iCalendar feeds that spell a
``TZID`` identically but ship different ``VTIMEZONE`` rules.  This happens when
a venue's timezone table is upgraded and the supplier re-issues the next two
months while already-confirmed sessions must keep their absolute instant.

The old feed is the source of truth for every already-defined session and its
occurrence identity; the new feed supplies the upgraded ``VTIMEZONE`` rules.
Operational changes (notifications, cancellations, moves, approved new
administrative slots and the legal split boundaries) arrive explicitly through
the :class:`~ics.migration.MigrationManifest` rather than being inferred.

Design principles
-----------------

* Every session is keyed by its **occurrence identity**: the master ``UID``
  plus the original ``RECURRENCE-ID`` (reduced to UTC).  Reschedules and
  cancellations are therefore traced back to the component they came from,
  never matched on the current wall-clock time.
* Each feed's own ``VTIMEZONE`` object is attached to the datetimes it
  produced, so a wall-clock time is always interpreted with the rules of the
  feed that carried it.  The two distinct rules are emitted as two
  ``VTIMEZONE`` blocks; the legacy one is renamed with a ``@legacy`` suffix.
* Already-notified occurrences keep their absolute instant and stay on the
  legacy zone.  Unnotified occurrences are published against the new zone,
  with their civil time shifted so the absolute instant is unchanged.
* The output only uses forms both supported reader classes understand.
  When a required split cannot be expressed, the affected occurrences are
  returned in :attr:`MigrationResult.unresolved` with the blocking reader and
  the exact limitation instead of being silently downgraded.
* Components outside the two-month window are preserved verbatim, with their
  ``TZID`` references moved onto the legacy ``VTIMEZONE`` block.

Inputs
------

The instructions are bundled in a :class:`~ics.migration.MigrationManifest`:

``notified``
    identities that were already communicated and must not move,
``cancelled``
    identities whose original occurrence is withdrawn,
``moves``
    ``(uid, recurrence-id, new start)`` triples for reschedules,
``allowed_boundaries``
    instants where a recurrence definition may be split,
``new_slots``
    approved administrative sessions to add,
``frozen_uids``
    whole component chains that must stay on the legacy zone.

The two reader profiles are :data:`~ics.migration.RFC5545_READER` (the full
specification) and :data:`~ics.migration.BASIC_READER` (daily/weekly/monthly
rules with ``INTERVAL=1`` and single-instance exceptions).  Planning works
against the intersection of both.

Minimum-cost segmentation
--------------------------

A series may be split at an allowed boundary into several ``VEVENT``
definitions.  The planner runs a dynamic program over every legal cut and
scores each candidate against a :class:`~ics.migration.CostSchedule`
(per component, ``EXDATE``, override, tombstone, ``VTIMEZONE`` and new-slot
record), so the cheapest publishable segmentation is selected.  A target-zone
change is a mandatory cut; if no allowed boundary covers it, the pair of
occurrences is reported as unrepresentable.

Example
-------

.. code-block:: python

    from datetime import datetime
    from ics.migration import (
        MigrationManifest,
        NewSlot,
        migrate_calendar,
    )
    from ics.timezone import UTC

    manifest = MigrationManifest(
        notified=[("weekly@venue", dec8_recurrence_id)],
        cancelled=[("weekly@venue", jan5_recurrence_id)],
        allowed_boundaries=[datetime(2026, 12, 18, tzinfo=UTC)],
        new_slots=[NewSlot(start, end, summary="Maintenance", uid="admin@venue")],
    )
    result = migrate_calendar(
        old_ics_text, new_ics_text, manifest, window_start, window_end
    )

    # re-readable by both supported reader classes
    publishable_ics = result.ics_text
    # per-identity audit: "kept", "moved", "deleted" or "added"
    for entry in result.audit:
        print(entry.kind, entry.uid, entry.rid)
    # anything that could not be expressed for every reader
    for blocked in result.unresolved:
        print(blocked.blocking_reader, blocked.limitation)

When two rescheduled sessions land on the same absolute instant they remain
two separate components with distinct ``UID`` values, so each still occupies
its own booking slot.

Reference
---------

.. autoclass:: ics.migration.MigrationManifest
    :members:

.. autoclass:: ics.migration.MigrationResult
    :members:

.. autoclass:: ics.migration.Unrepresentable
    :members:

.. autoclass:: ics.migration.NewSlot
    :members:

.. autoclass:: ics.migration.CostSchedule
    :members:

.. autoclass:: ics.migration.ReaderProfile
    :members:

.. autofunction:: ics.migration.migrate_calendar
