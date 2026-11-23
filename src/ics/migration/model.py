"""Data model for the venue-scheduling calendar migration.

The migration reconciles two iCalendar feeds that share the same ``TZID``
strings but carry different VTIMEZONE rules.  Everything is keyed on the
*occurrence identity* (``UID`` plus the original ``RECURRENCE-ID``), never on
the current wall-clock time, so rescheduled or cancelled sessions can still be
traced back to the source component that defined them.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from ics.timezone import Timezone

# A recurrence identity: the UID of the (master) event and the original
# start of the occurrence.  ``rid`` is ``None`` for one-off events and for
# the anchor of a series without an explicit RECURRENCE-ID.
Identity = tuple


@dataclass(frozen=True)
class Occurrence:
    """One concrete session, bound to an absolute instant.

    ``start``/``end`` are timezone-aware datetimes and therefore pin the
    absolute instant independently of any later TZID redefinition.
    ``civil_start``/``civil_end`` keep the wall-clock time as published by
    the source, together with the exact VTIMEZONE object the source carried.
    """

    uid: str
    rid: Optional[datetime]
    start: datetime
    end: datetime
    civil_start: datetime
    civil_end: datetime
    source_tz: Optional[Timezone]
    summary: Optional[str] = None
    status: Optional[str] = None
    cancelled: bool = False
    sequence: Optional[int] = None
    # True when the occurrence came from a RECURRENCE-ID override component
    # (its start may diverge from the master rule's slot).
    source_override: bool = False
    # identity of the source component (calendar name, component index)
    origin: Optional[str] = None

    def key(self) -> Identity:
        """Stable identity key with the recurrence-id normalised to UTC.

        Two occurrences describe the same original session iff their UIDs
        match and their original recurrence instants coincide; reducing the
        recurrence-id to UTC makes the comparison independent of the
        source's VTIMEZONE spelling.
        """
        return self.uid, _utc_key(self.rid)


def _utc_key(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    from ics.timezone import UTC

    return value.astimezone(UTC)


@dataclass(frozen=True)
class NewSlot:
    """A newly approved administrative session the supplier may publish."""

    start: datetime
    end: datetime
    summary: Optional[str] = None
    uid: Optional[str] = None


@dataclass
class MigrationManifest:
    """All externally supplied migration instructions.

    Attributes:
        notified: identities (uid, recurrence-id) that were already
            communicated and must keep their absolute instant.
        cancelled: identities whose original occurrence is withdrawn.
        moves: triples (uid, recurrence-id, new absolute start) for
            rescheduled occurrences; their duration is preserved.
        allowed_boundaries: instants at which recurrence definitions may be
            split into new components.
        new_slots: approved administrative sessions to add.
        frozen_uids: uids whose whole component chain must stay byte-stable.
    """

    notified: List[Identity] = field(default_factory=list)
    cancelled: List[Identity] = field(default_factory=list)
    moves: List[tuple] = field(default_factory=list)
    allowed_boundaries: List[datetime] = field(default_factory=list)
    new_slots: List[NewSlot] = field(default_factory=list)
    frozen_uids: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class AuditEntry:
    uid: str
    rid: Optional[datetime]
    kind: str  # "kept" | "moved" | "deleted" | "added"
    old_start: Optional[datetime] = None
    new_start: Optional[datetime] = None
    detail: str = ""


@dataclass(frozen=True)
class Unrepresentable:
    """An occurrence that cannot be published for every supported reader."""

    uid: str
    rid: Optional[datetime]
    start: datetime
    blocking_reader: str
    limitation: str


@dataclass
class MigrationResult:
    ics_text: str
    audit: List[AuditEntry] = field(default_factory=list)
    unresolved: List[Unrepresentable] = field(default_factory=list)
    cost: int = 0
    component_count: int = 0
