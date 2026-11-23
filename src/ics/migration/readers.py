"""Capabilities of the two reader classes that must re-read the output.

Every emitted recurrence definition and every exception form has to be
understood by *both* readers, so the planner works against the intersection
of the two profiles.  Anything that cannot be expressed inside that
intersection (and cannot be split at an allowed boundary) is reported back as
an :class:`~ics.migration.model.Unrepresentable` entry instead of being
silently downgraded.
"""
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Tuple

# Rule feature flags, named after the RFC 5545 RRULE parts they gate.
FREQ_SECONDLY = "SECONDLY"
FREQ_MINUTELY = "MINUTELY"
FREQ_HOURLY = "HOURLY"
FREQ_DAILY = "DAILY"
FREQ_WEEKLY = "WEEKLY"
FREQ_MONTHLY = "MONTHLY"
FREQ_YEARLY = "YEARLY"

# Exception / instance forms.
FORM_EXDATE = "EXDATE"
FORM_RDATE = "RDATE"
FORM_EXRULE = "EXRULE"
FORM_RECURRENCE_ID = "RECURRENCE-ID"
FORM_RECURRENCE_ID_TIME_SHIFT = "RECURRENCE-ID-TIME-SHIFT"
FORM_MULTI_RRULE_COMPONENT = "MULTI-RRULE-COMPONENT"
FORM_OVERLAPPING_SEGMENTS = "OVERLAPPING-SEGMENTS"


@dataclass(frozen=True)
class ReaderProfile:
    name: str
    supported_freq: FrozenSet[str]
    max_interval: int
    supports_byday: bool
    supports_bymonthday: bool
    supports_count: bool
    supports_until: bool
    exception_forms: FrozenSet[str]
    # maximum number of RRULE lines inside one VEVENT (1 = one rule only)
    max_rules_per_component: int = 1
    notes: str = ""

    def rule_supported(
        self,
        freq: str,
        interval: int,
        byday: Tuple[str, ...] = (),
        bymonthday: Tuple[int, ...] = (),
        count: Optional[int] = None,
        until: Optional[object] = None,
    ) -> Optional[str]:
        """Return ``None`` if the rule is readable, else the limitation text."""
        if freq not in self.supported_freq:
            return f"{self.name} does not support FREQ={freq}"
        if interval > self.max_interval:
            return f"{self.name} only supports INTERVAL<={self.max_interval}"
        if byday and not self.supports_byday:
            return f"{self.name} does not support BYDAY"
        if bymonthday and not self.supports_bymonthday:
            return f"{self.name} does not support BYMONTHDAY"
        if count is not None and not self.supports_count:
            return f"{self.name} does not support COUNT"
        if until is not None and not self.supports_until:
            return f"{self.name} does not support UNTIL"
        return None

    def form_supported(self, form: str) -> bool:
        return form in self.exception_forms


# A standards-compliant desktop / server reader (full RFC 5545 feature set).
RFC5545_READER = ReaderProfile(
    name="rfc5545-reader",
    supported_freq=frozenset(
        {
            FREQ_SECONDLY,
            FREQ_MINUTELY,
            FREQ_HOURLY,
            FREQ_DAILY,
            FREQ_WEEKLY,
            FREQ_MONTHLY,
            FREQ_YEARLY,
        }
    ),
    max_interval=10**6,
    supports_byday=True,
    supports_bymonthday=True,
    supports_count=True,
    supports_until=True,
    exception_forms=frozenset(
        {
            FORM_EXDATE,
            FORM_RDATE,
            FORM_EXRULE,
            FORM_RECURRENCE_ID,
            FORM_RECURRENCE_ID_TIME_SHIFT,
            FORM_MULTI_RRULE_COMPONENT,
            FORM_OVERLAPPING_SEGMENTS,
        }
    ),
    notes="full RFC 5545 recurrence support",
)

# A constrained mobile/web reader: plain weekly/daily/monthly rules only,
# INTERVAL must be 1, one RRULE per VEVENT and no EXRULE.  It handles single
# instances (EXDATE/RDATE and time-shifting RECURRENCE-ID overrides) but
# refuses overlapping segments that carry the same UID.
BASIC_READER = ReaderProfile(
    name="basic-reader",
    supported_freq=frozenset({FREQ_DAILY, FREQ_WEEKLY, FREQ_MONTHLY}),
    max_interval=1,
    supports_byday=True,
    supports_bymonthday=True,
    supports_count=True,
    supports_until=True,
    exception_forms=frozenset(
        {
            FORM_EXDATE,
            FORM_RDATE,
            FORM_RECURRENCE_ID,
            FORM_RECURRENCE_ID_TIME_SHIFT,
        }
    ),
    notes="no EXRULE, INTERVAL=1, one RRULE/VEVENT, overlapping same-UID segments rejected",
)

SUPPORTED_READERS: Tuple[ReaderProfile, ...] = (RFC5545_READER, BASIC_READER)


@dataclass
class ReaderIntersection:
    """Combined gate used while planning."""

    readers: Tuple[ReaderProfile, ...] = field(
        default_factory=lambda: SUPPORTED_READERS
    )

    def rule_supported(self, **kwargs) -> Optional[str]:
        for reader in self.readers:
            limitation = reader.rule_supported(**kwargs)
            if limitation is not None:
                return limitation
        return None

    def form_supported(self, form: str) -> Optional[str]:
        for reader in self.readers:
            if not reader.form_supported(form):
                return f"{reader.name} does not support {form}"
        return None
