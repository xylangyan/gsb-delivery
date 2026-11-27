"""Handover data model (交接材料).

The old process exported psutil ``nowrap`` counters and, where
obtainable, also kept the raw counter, the reading process and the
original physical disk number.  Duty records either name a device
serial (clear) or only give the allowed disk numbers, whether a
restart was permitted and the counter field width (unclear).
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field


@dataclass(frozen=True)
class Sample:
    """One actual kernel reading (一次实际取数).

    ``sample_no`` is issued per read action: every disk read in the
    same sweep shares the number.  ``raw_value`` may be None when the
    old process could only keep the exported nowrap counter.
    """

    table: str
    sample_no: int
    timestamp: float
    disk_name: str
    nowrap_value: int
    raw_value: "int | None"
    reader: str
    phys_disk_no: "int | None"


@dataclass(frozen=True)
class Transcription:
    """A record copied by hand (转抄), not a second kernel reading.

    The log marks which actual readings it is allowed to correspond
    to (``allowed_sources`` are sample numbers).  The transcription is
    unified with exactly one of them; its ``value`` must equal the
    exported nowrap counter of that reading.
    """

    table: str
    disk_name: str
    value: int
    allowed_sources: tuple
    note: str = ""


@dataclass(frozen=True)
class DutyWindow:
    """Duty record (值班记录) for one disk name over a time window.

    Clear records carry ``serial``.  Unclear ones only carry the disk
    numbers allowed at the time, whether a restart was permitted and
    the original counter field width.
    """

    disk_name: str
    start: float
    end: float
    serial: "str | None" = None
    allowed_disk_nos: frozenset = frozenset()
    restart_allowed: bool = False
    counter_bits: int = 32


@dataclass(frozen=True)
class Capability:
    """Device capability record (设备能力记录): finite write-rate bound."""

    phys_disk_no: int
    max_write_rate: float  # counter units per second

    def bound(self, seconds: float) -> int:
        if seconds < 0:
            raise ValueError("negative window")
        return int(self.max_write_rate * seconds)


@dataclass(frozen=True)
class Coverage:
    """Physical coverage list (物理覆盖清单) for one view.

    ``extents`` names the physical extents the view actually covers,
    e.g. ``{"whole"}`` for a whole-disk view or ``{"p1", "p2"}`` for
    two partitions.  Reconciliation counts by actual coverage only.
    """

    table: str
    disk_name: str
    phys_disk_no: int
    extents: frozenset


@dataclass(frozen=True)
class SignedTotal:
    """An exact total already signed off (已签精确合计) for a window."""

    table: str
    disk_name: str
    start: float
    end: float
    amount: int
    label: str = ""


@dataclass(frozen=True)
class SwapClaim:
    """An unresolved disk swap (未核清的换盘).

    The same event may be referenced by several tables; every table
    must use the single shared interpretation of this event.
    """

    event_id: str
    disk_name: str
    start: float
    end: float


@dataclass
class Handover:
    """Everything handed over for warehouse-entry reconciliation."""

    samples: list = field(default_factory=list)
    transcriptions: list = field(default_factory=list)
    duty: list = field(default_factory=list)
    capabilities: list = field(default_factory=list)
    coverage: list = field(default_factory=list)
    signed_totals: list = field(default_factory=list)
    swap_claims: list = field(default_factory=list)
    archive: "object | None" = None  # ArchiveIndex for historical files

    def capability_for(self, phys_disk_no: int) -> "Capability | None":
        for cap in self.capabilities:
            if cap.phys_disk_no == phys_disk_no:
                return cap
        return None

    def duty_for(self, disk_name: str, t0: float, t1: float) -> "DutyWindow | None":
        """The tightest duty window covering ``[t0, t1]``."""
        best = None
        for win in self.duty:
            if win.disk_name != disk_name:
                continue
            if win.start <= t0 and t1 <= win.end:
                if best is None or (win.end - win.start) < (best.end - best.start):
                    best = win
        return best

    def coverage_for(self, table: str, disk_name: str) -> "Coverage | None":
        for cov in self.coverage:
            if cov.table == table and cov.disk_name == disk_name:
                return cov
        return None
