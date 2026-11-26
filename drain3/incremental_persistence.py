# SPDX-License-Identifier: MIT

"""
Lightweight (incremental) checkpoint persistence for local log collectors.

The classic :class:`~drain3.file_persistence.FilePersistence` rewrites the whole
miner state on every save.  For a large, mostly-stable template library a batch
of new messages typically changes only a handful of clusters, so rewriting the
complete snapshot is wasteful.

This module stores the state as

* one full *base snapshot* (the classic jsonpickle-encoded Drain snapshot, which
  may already exist from a previous run in full-snapshot mode), and
* an append-only *journal* where each line is one checkpoint: only the clusters
  that changed since the previous checkpoint, plus the log position (watermark)
  that has been fully processed.

A checkpoint therefore only serializes the clusters that actually changed and
its size grows with the amount of change instead of the size of the library.
The watermark of the last durable checkpoint tells the collector after which
log line it may continue reading after a restart.  The base snapshot records
its own watermark in a sidecar metadata file, so the resume position survives
even after the journal has been folded away by compaction.

Old checkpoints can be folded into a fresh full snapshot by
:meth:`begin_compaction` / :meth:`commit_compaction` (also exposed through
:meth:`TemplateMiner.compact_checkpoint`).  After compaction the journal is
reset and a restart no longer has to replay the historical checkpoints.

All writes are crash safe: a new snapshot or journal file is written to a
temporary file, fsynced and atomically renamed, and the pointer that names the
durable journal length is only switched once the new data is durable.  A
checkpoint that was only partially written when a process died is therefore
ignored and the previous complete checkpoint is used instead.
"""

import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional

from drain3.persistence_handler import PersistenceHandler


logger = logging.getLogger(__name__)

BASE_SNAPSHOT_FILE = "base_snapshot.json"
BASE_META_FILE = "base_snapshot.meta"
JOURNAL_FILE = "journal.jsonl"
JOURNAL_POINTER_FILE = "journal.pointer"
COMPACTING_SNAPSHOT_SUFFIX = ".compacting"
TMP_SUFFIX = ".tmp"
JOURNAL_FORMAT_VERSION = 1

class IncrementalFilePersistence(PersistenceHandler):
    """Directory based, journaling persistence handler.

    The directory layout is::

        <dir>/base_snapshot.json        full Drain snapshot (classic format)
        <dir>/base_snapshot.meta        watermark covered by the base snapshot
        <dir>/journal.jsonl             one JSON checkpoint record per line
        <dir>/journal.pointer           durable byte length of the journal
        <dir>/*.compacting              staged snapshot while compacting

    The handler keeps the *encoded* base snapshot bytes, so compression stays
    owned by :class:`~drain3.template_miner.TemplateMiner` exactly as for the
    classic persistence handlers.
    """

    def __init__(self, dir_path: str) -> None:
        self.dir_path = dir_path
        self._base_snapshot: Optional[bytes] = None
        self._base_watermark: Optional[int] = None
        self._journal_records: List[Dict[str, Any]] = []
        self._journal_file = pathlib.Path(self.dir_path) / JOURNAL_FILE
        self._pointer_file = pathlib.Path(self.dir_path) / JOURNAL_POINTER_FILE
        self._base_snapshot_file = pathlib.Path(self.dir_path) / BASE_SNAPSHOT_FILE
        self._base_meta_file = pathlib.Path(self.dir_path) / BASE_META_FILE
        self._compacting_snapshot_file = (
            pathlib.Path(self.dir_path) / (BASE_SNAPSHOT_FILE + COMPACTING_SNAPSHOT_SUFFIX)
        )
        self._compacting_meta_file = (
            pathlib.Path(self.dir_path) / (BASE_META_FILE + COMPACTING_SNAPSHOT_SUFFIX)
        )
        self._load()

    # ------------------------------------------------------------------ load

    def _load(self) -> None:
        pathlib.Path(self.dir_path).mkdir(parents=True, exist_ok=True)

        # A staged compaction left behind by a process that died before the
        # switch can never be the live state: drop it so the previous base
        # snapshot and journal remain the source of truth.
        for stale_file in (self._compacting_snapshot_file, self._compacting_meta_file):
            if stale_file.exists():
                logger.info("Discarding incomplete compaction artifact %s", stale_file)
                stale_file.unlink()

        self._base_snapshot = None
        if self._base_snapshot_file.exists():
            self._base_snapshot = self._base_snapshot_file.read_bytes()

        self._base_watermark = None
        if self._base_meta_file.exists():
            try:
                self._base_watermark = json.loads(self._base_meta_file.read_text("utf-8")).get("watermark")
            except (ValueError, OSError):
                self._base_watermark = None

        self._journal_records = []
        if self._journal_file.exists():
            durable_length = self._read_durable_length()
            if durable_length:
                with self._journal_file.open("rb") as journal_fh:
                    raw = journal_fh.read(durable_length)
                for line in raw.splitlines():
                    if line.strip():
                        self._journal_records.append(json.loads(line.decode("utf-8")))

        if self._base_snapshot is None and not self._journal_records:
            logger.info("No saved incremental state found in %s", self.dir_path)
        else:
            logger.info(
                "Restored incremental state from %s: base snapshot %s, %d checkpoint(s)",
                self.dir_path,
                "present" if self._base_snapshot is not None else "absent",
                len(self._journal_records),
            )

    def _read_durable_length(self) -> Optional[int]:
        if not self._pointer_file.exists():
            # No pointer means the journal was never committed (e.g. the process
            # died while the first checkpoint was written): treat it as absent.
            return None
        try:
            return int(self._pointer_file.read_text().strip())
        except (ValueError, OSError):
            return None

    def load_state(self) -> Optional[bytes]:
        """Return the persisted base snapshot, if any.

        Checkpoint records are delivered separately through
        :attr:`journal_records` and replayed by the miner, because a base
        snapshot written by the classic full-snapshot mode must remain loadable
        unchanged.
        """
        return self._base_snapshot

    @property
    def journal_records(self) -> List[Dict[str, Any]]:
        return list(self._journal_records)

    @property
    def base_watermark(self) -> Optional[int]:
        """Watermark covered by the base snapshot itself (before journal replay)."""
        return self._base_watermark

    def has_state(self) -> bool:
        return self._base_snapshot is not None or bool(self._journal_records)

    # ----------------------------------------------------------------- saves

    def save_state(self, state: bytes) -> None:
        """Persist a full snapshot as the new base and drop the journal.

        Kept for :class:`PersistenceHandler` compatibility; the incremental
        miner normally calls :meth:`save_base_snapshot` /
        :meth:`append_checkpoint` directly.
        """
        self.save_base_snapshot(state)

    def _atomic_write(self, path: pathlib.Path, data: bytes) -> None:
        tmp_path = path.with_name(path.name + TMP_SUFFIX)
        with tmp_path.open("wb") as tmp_fh:
            tmp_fh.write(data)
            tmp_fh.flush()
            os.fsync(tmp_fh.fileno())
        os.replace(tmp_path, path)
        self._fsync_dir()

    def _fsync_dir(self) -> None:
        dir_fd = os.open(self.dir_path, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _write_meta(self, path: pathlib.Path, watermark: Optional[int]) -> None:
        payload = json.dumps({"version": JOURNAL_FORMAT_VERSION, "watermark": watermark}).encode("utf-8")
        self._atomic_write(path, payload)

    def save_base_snapshot(self, state: bytes, watermark: Optional[int] = None) -> None:
        """Crash-safely replace the base snapshot and reset the journal."""
        self._atomic_write(self._base_snapshot_file, state)
        self._write_meta(self._base_meta_file, watermark)
        # The new base already contains every cluster, so the old journal is
        # obsolete.  Reset the journal, then publish the empty pointer.
        self._atomic_write(self._journal_file, b"")
        self._atomic_write(self._pointer_file, b"0")
        self._base_snapshot = state
        self._base_watermark = watermark
        self._journal_records = []

    def append_checkpoint(self, record: Dict[str, Any]) -> None:
        """Append one checkpoint record as a crash-safe journal line."""
        record = dict(record)
        record["version"] = JOURNAL_FORMAT_VERSION
        encoded = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")

        # Any garbage past the durable pointer (left by a crash mid-write) is
        # truncated before appending the new, complete checkpoint.
        durable_length = self._read_durable_length() or 0
        if self._journal_file.exists():
            with self._journal_file.open("r+b") as journal_fh:
                journal_fh.truncate(durable_length)
                journal_fh.seek(0, os.SEEK_END)
                journal_fh.write(encoded)
                journal_fh.flush()
                os.fsync(journal_fh.fileno())
        else:
            # First checkpoint: write through a temp file so that readers never
            # observe a partially created journal without a committed pointer.
            self._atomic_write(self._journal_file, encoded)

        self._atomic_write(self._pointer_file, str(durable_length + len(encoded)).encode("utf-8"))
        self._journal_records.append(record)

    def begin_compaction(self, state: bytes, watermark: int) -> None:
        """Stage a compacted full snapshot without touching the live state.

        A crash between :meth:`begin_compaction` and :meth:`commit_compaction`
        leaves the previous base snapshot and journal fully usable.
        """
        self._atomic_write(self._compacting_snapshot_file, state)
        self._write_meta(self._compacting_meta_file, watermark)

    def commit_compaction(self, watermark: int) -> List[Dict[str, Any]]:
        """Promote the staged compacted snapshot and discard folded checkpoints.

        Checkpoints newer than ``watermark`` (saved while compaction was in
        flight) are retained on top of the new base and returned so the miner
        can continue numbering from them.
        """
        if not self._compacting_snapshot_file.exists():
            raise RuntimeError("compaction was not started")

        retained_records = [
            record for record in self._journal_records if record["watermark"] > watermark
        ]

        os.replace(self._compacting_snapshot_file, self._base_snapshot_file)
        if self._compacting_meta_file.exists():
            os.replace(self._compacting_meta_file, self._base_meta_file)
        self._fsync_dir()
        self._atomic_write(self._journal_file, b"")
        self._atomic_write(self._pointer_file, b"0")
        self._base_snapshot = self._base_snapshot_file.read_bytes()
        self._base_watermark = watermark
        self._journal_records = []

        for record in retained_records:
            self._append_loaded_record(record)

        return retained_records

    def _append_loaded_record(self, record: Dict[str, Any]) -> None:
        encoded = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        durable_length = self._read_durable_length() or 0
        if self._journal_file.exists():
            with self._journal_file.open("r+b") as journal_fh:
                journal_fh.truncate(durable_length)
                journal_fh.seek(0, os.SEEK_END)
                journal_fh.write(encoded)
                journal_fh.flush()
                os.fsync(journal_fh.fileno())
        else:
            self._atomic_write(self._journal_file, encoded)
        self._atomic_write(self._pointer_file, str(durable_length + len(encoded)).encode("utf-8"))
        self._journal_records.append(record)

    def abort_compaction(self) -> None:
        for staged_file in (self._compacting_snapshot_file, self._compacting_meta_file):
            if staged_file.exists():
                staged_file.unlink()

    def compacting_in_progress(self) -> bool:
        return self._compacting_snapshot_file.exists()

    # ------------------------------------------------------------- inspection

    def journal_size_bytes(self) -> int:
        """Bytes currently occupied by the durable journal (0 after compaction)."""
        durable_length = self._read_durable_length()
        if not durable_length or not self._journal_file.exists():
            return 0
        # Never report past the actual file: a crash between truncating the
        # journal and refreshing the pointer must not look like durable data.
        return min(durable_length, self._journal_file.stat().st_size)

    def checkpoint_count(self) -> int:
        """Number of durable checkpoints stored after the base snapshot."""
        return len(self._journal_records)

    def last_watermark(self) -> Optional[int]:
        """Watermark of the last durable checkpoint, falling back to the base."""
        if self._journal_records:
            return self._journal_records[-1]["watermark"]
        return self._base_watermark

    # -------------------------------------------------------- test utilities

    def simulate_partial_last_checkpoint_write(self) -> None:
        """Simulate a process killed while the newest checkpoint was written.

        The journal pointer is only published after the journal write completes,
        so it is rolled back to the byte offset at which the last complete
        checkpoint ended, and only a partial line remains beyond it.  A restart
        therefore restores the previous complete checkpoint and re-reads the
        message whose checkpoint never finished.
        """
        durable_length = self._read_durable_length() or 0
        if not self._journal_file.exists() or durable_length == 0:
            return

        with self._journal_file.open("rb") as journal_fh:
            journal_fh.seek(0)
            durable_bytes = journal_fh.read(durable_length)

        last_line_start = durable_bytes.rfind(b"\n", 0, durable_length - 1)
        previous_length = last_line_start + 1 if last_line_start != -1 else 0

        with self._journal_file.open("r+b") as journal_fh:
            journal_fh.truncate(previous_length)
            journal_fh.seek(0, os.SEEK_END)
            journal_fh.write(b'{"seq": 99, "incomplete": true,\n')
            journal_fh.flush()
            os.fsync(journal_fh.fileno())

        self._atomic_write(self._pointer_file, str(previous_length).encode("utf-8"))
