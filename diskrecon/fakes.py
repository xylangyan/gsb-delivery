"""Fake reader data (假读表数据) for full reproduction.

The reproduction never touches real disks outside the process: a
:class:`FakeKernel` simulates physical disks with fixed-width wrapping
counters and same-name swaps, and :class:`OldExporter` replays the old
process — it reads the fake kernel and exports psutil ``nowrap``
counters through the genuine :func:`psutil._common.wrap_numbers` code
path, keeping the raw counter, the reader name and the physical disk
number exactly like the old process did.
"""

from __future__ import annotations

from .model import Sample
from .nowrap import NowrapTracker


class FakeKernel:
    """Simulated disks behind one cabinet of possibly duplicated names."""

    def __init__(self, counter_bits: int = 8):
        self.counter_bits = counter_bits
        self.modulus = 1 << counter_bits
        self._installed = {}  # disk name -> [phys_disk_no, raw value]
        self.written = {}  # phys_disk_no -> total units written (truth)

    def install(self, disk_name: str, phys_disk_no: int, start: int = 0):
        self._installed[disk_name] = [phys_disk_no, start % self.modulus]
        self.written.setdefault(phys_disk_no, 0)

    def write(self, disk_name: str, amount: int):
        phys, raw = self._installed[disk_name]
        self._installed[disk_name][1] = (raw + amount) % self.modulus
        self.written[phys] += amount

    def swap(self, disk_name: str, phys_disk_no: int):
        """Physically replace the disk; the new counter restarts at 0."""
        self.install(disk_name, phys_disk_no, start=0)

    def read_raw(self, disk_name: str) -> int:
        return self._installed[disk_name][1]

    def phys_of(self, disk_name: str) -> int:
        return self._installed[disk_name][0]


class OldExporter:
    """The old process: one read action issues one sample number."""

    def __init__(self, kernel: FakeKernel, reader: str = "exporter-0"):
        self.kernel = kernel
        self.reader = reader
        self._nowrap = {}  # (table, disk_name) -> NowrapTracker

    def sweep(self, table: str, sample_no: int, timestamp: float) -> list:
        """Read every installed disk once and export the handover rows."""
        out = []
        for disk_name in sorted(self.kernel._installed):
            raw = self.kernel.read_raw(disk_name)
            tracker = self._nowrap.setdefault((table, disk_name), NowrapTracker())
            out.append(
                Sample(
                    table=table,
                    sample_no=sample_no,
                    timestamp=timestamp,
                    disk_name=disk_name,
                    nowrap_value=tracker.update(raw),
                    raw_value=raw,
                    reader=self.reader,
                    phys_disk_no=self.kernel.phys_of(disk_name),
                )
            )
        return out
