from __future__ import annotations

import fcntl
import hashlib
import json
import os
import struct
import zipfile
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal, NamedTuple

if TYPE_CHECKING:
    # TODO: import from typing (needs Python >=3.10)
    from typing_extensions import TypeAlias

FileMode: TypeAlias = Literal["r", "w", "x", "a"]

# ZIP format constants (fixed by APPNOTE.TXT; the zipfile module does not
# export all of them on every Python version)
_STRUCT_CENTRAL_DIR = "<4s4B4HL2L5H2L"
_SIZE_CENTRAL_DIR = struct.calcsize(_STRUCT_CENTRAL_DIR)
_STRING_CENTRAL_DIR = b"PK\x01\x02"
_STRUCT_END_ARCHIVE = "<4s4H2LH"
_SIZE_END_ARCHIVE = struct.calcsize(_STRUCT_END_ARCHIVE)
_STRING_END_ARCHIVE = b"PK\x05\x06"


class Zip(MutableMapping[str, bytes]):
    """Mutable Mapping interface to a Zip file

    Keys must be strings, values must be bytes

    Parameters
    ----------
    filename: string
    mode: string, ('r', 'w', 'a'), defaults to 'a'

    Notes
    -----
    None of this class is thread-safe - not even normally trivial methods such as
    ``__len__ `` or ``__contains__``.

    Examples
    --------
    >>> z = Zip('myfile.zip')  # doctest: +SKIP
    >>> z['x'] = b'123'  # doctest: +SKIP
    >>> z['x']  # doctest: +SKIP
    b'123'
    >>> z.flush()  # flush and write metadata to disk  # doctest: +SKIP
    """

    filename: str
    mode: FileMode | Literal["closed"]
    _file: zipfile.ZipFile | None

    def __init__(self, filename: str, mode: FileMode = "a"):
        super().__init__()
        self.filename = filename
        self.mode = mode
        self._file = None

    @property
    def file(self) -> zipfile.ZipFile:
        if self.mode == "closed":
            raise OSError("File closed")
        if not self._file or not self._file.fp:
            self._file = zipfile.ZipFile(self.filename, mode=self.mode)
        return self._file

    def __getitem__(self, key: str) -> bytes:
        if not isinstance(key, str):
            raise KeyError(key)
        return self.file.read(key)

    def __setitem__(self, key: str, value: bytes | bytearray | memoryview) -> None:
        if not isinstance(key, str):
            raise TypeError(key)
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError(value)
        if key in self:
            raise NotImplementedError("Not supported by stdlib zipfile")
        self.file.writestr(key, value)

    def __iter__(self) -> Iterator[str]:
        return (zi.filename for zi in self.file.filelist)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        try:
            self.file.getinfo(key)
            return True
        except KeyError:
            return False

    def __delitem__(self, key: str) -> None:  # pragma: nocover
        raise NotImplementedError("Not supported by stdlib zipfile")

    def __len__(self) -> int:
        return len(self.file.filelist)

    def flush(self) -> None:
        if self._file:
            if self._file.fp:
                self._file.fp.flush()
            self._file.close()
            self._file = None

    def close(self) -> None:
        self.flush()
        self.mode = "closed"

    def __enter__(self) -> Zip:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class ConflictError(Exception):
    """A commit was rejected because one or more members no longer match the
    revisions the writer based its changes on.

    The ``conflicts`` attribute maps each conflicting key to
    ``{"expected": ..., "current": ...}``; ``None`` means the member is absent
    from the current handoff. No part of a rejected commit is applied.
    """

    conflicts: dict[str, dict[str, int | None]]

    def __init__(self, conflicts: dict[str, dict[str, int | None]]):
        self.conflicts = conflicts
        keys = ", ".join(sorted(conflicts))
        super().__init__(f"Revision conflict on member(s): {keys}")


class SimulatedCrash(BaseException):
    """Raised in-process at a commit checkpoint named in
    ``TransactionalZip(crash_at=...)`` to simulate abrupt process termination.
    """


class CommitReceipt(NamedTuple):
    """Acknowledgement returned by :meth:`TransactionalZip.commit`.

    ``applied`` is False when the commit was recognized as a retry of an
    application that had already taken effect (same ``app_id`` and identical
    request); in that case the commit is a no-op.
    """

    app_id: str
    generation: int
    applied: bool


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _parse_cd_entries(cd: bytes) -> list[tuple[str, bytes]]:
    """Split raw central directory bytes into ``(filename, raw_record)`` pairs"""
    entries = []
    pos = 0
    while pos < len(cd):
        if cd[pos : pos + 4] != _STRING_CENTRAL_DIR:
            raise zipfile.BadZipFile("Corrupt central directory")
        fields = struct.unpack(
            _STRUCT_CENTRAL_DIR, cd[pos : pos + _SIZE_CENTRAL_DIR]
        )
        flag_bits = fields[5]
        fname_len, extra_len, comment_len = fields[12], fields[13], fields[14]
        end = pos + _SIZE_CENTRAL_DIR + fname_len + extra_len + comment_len
        name_start = pos + _SIZE_CENTRAL_DIR
        raw_name = cd[name_start : name_start + fname_len]
        name = raw_name.decode("utf-8" if flag_bits & 0x800 else "cp437")
        entries.append((name, cd[pos:end]))
        pos = end
    return entries


class TransactionalZip(Zip):
    """Zip store with atomic, revision-checked, crash-safe commits

    Multiple writers may open the same archive and prepare changes against the
    member revisions they observed. A commit atomically applies a set of
    overwrites and deletions if - and only if - every touched member still
    matches the revision the writer based it on; otherwise it raises
    :class:`ConflictError` and applies nothing.

    Commits never rewrite existing archive bytes: changed members and a fresh
    central directory (with deleted paths and superseded duplicates removed)
    are appended at the end of the file. Unchanged members therefore keep
    their disk location and original compressed bytes, readers that already
    opened a member stream keep reading valid data, and the staging
    requirements are limited to the changed members, the new directory, and
    recovery metadata - the archive is never copied in full.

    The staging area lives in a *handoff directory* (``<filename>.handoff`` by
    default) and holds at most:

    - ``tail.bin``: the staged changed members plus the new central directory
    - ``journal.json``: recovery information for an in-progress commit
    - ``meta.json``: member revisions and the last committed application id

    If the process dies mid-commit, the next open of the formal path (in any
    process) rolls the journalled commit forward deterministically, so an
    independent ZIP reader always sees either the complete previous handoff or
    the complete new one. Retrying a committed application with the same
    ``app_id`` succeeds without taking effect a second time.

    Parameters
    ----------
    filename: string
    mode: string, ('r', 'w', 'a'), defaults to 'a'
    handoff_dir: string, optional
        Staging directory for commits; defaults to ``<filename>.handoff``
    crash_at: iterable of string, optional
        Fault-injection hook for testing: checkpoint names
        ("staged", "journaled", "members_written", "directory_written",
        "meta_written") at which to simulate an abrupt process crash
    crash_exit: bool, optional
        If True, a simulated crash calls ``os._exit(1)`` (for use in a child
        process); otherwise it raises :class:`SimulatedCrash`

    Notes
    -----
    Zip64 archives are not supported. Commits are serialized across processes
    with an advisory lock, but this class is not thread-safe.
    """

    handoff_dir: str
    crash_at: frozenset[str]
    crash_exit: bool
    _meta: dict[str, Any]

    def __init__(
        self,
        filename: str,
        mode: FileMode = "a",
        handoff_dir: str | None = None,
        *,
        crash_at: Iterable[str] = (),
        crash_exit: bool = False,
    ):
        super().__init__(filename, mode)
        self.handoff_dir = handoff_dir or (filename + ".handoff")
        self.crash_at = frozenset(crash_at)
        self.crash_exit = crash_exit
        os.makedirs(self.handoff_dir, exist_ok=True)
        with self._locked():
            self._recover()
            self._meta = self._load_meta()

    @property
    def _journal_path(self) -> str:
        return os.path.join(self.handoff_dir, "journal.json")

    @property
    def _meta_path(self) -> str:
        return os.path.join(self.handoff_dir, "meta.json")

    @property
    def _tail_path(self) -> str:
        return os.path.join(self.handoff_dir, "tail.bin")

    @property
    def _staging_path(self) -> str:
        return os.path.join(self.handoff_dir, "staging.zip")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        fd = os.open(
            os.path.join(self.handoff_dir, "lock"), os.O_CREAT | os.O_RDWR, 0o666
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _checkpoint(self, name: str) -> None:
        if name in self.crash_at:
            if self.crash_exit:
                os._exit(1)
            raise SimulatedCrash(name)

    @staticmethod
    def _write_atomic(path: str, data: bytes) -> None:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_dir(os.path.dirname(path) or ".")

    def _write_json(self, path: str, obj: Any) -> None:
        self._write_atomic(path, json.dumps(obj).encode())

    def _load_meta(self) -> dict[str, Any]:
        try:
            with open(self._meta_path, "rb") as f:
                return json.load(f)
        except FileNotFoundError:
            pass
        # Bootstrap from the archive as found on disk
        members = []
        revisions = {}
        try:
            with zipfile.ZipFile(self.filename) as zf:
                for name in zf.NameToInfo:  # unique names; latest entry wins
                    members.append(name)
                    revisions[name] = 1
        except (FileNotFoundError, zipfile.BadZipFile):
            pass
        meta = {
            "generation": 0,
            "members": members,
            "revisions": revisions,
            "last_app_id": None,
            "last_request_hash": None,
        }
        self._write_json(self._meta_path, meta)
        return meta

    def _read_directory(self) -> tuple[int, list[tuple[str, bytes]]]:
        """Return ``(file_size, [(name, raw_central_record), ...])`` of the
        formal archive, tolerating trailing bytes left by interrupted commits
        """
        try:
            f = open(self.filename, "rb")
        except FileNotFoundError:
            return 0, []
        with f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return 0, []
            window = min(size, _SIZE_END_ARCHIVE + 0xFFFF)
            f.seek(size - window)
            data = f.read(window)
        idx = data.rfind(_STRING_END_ARCHIVE)
        if idx < 0:
            raise zipfile.BadZipFile("No end of central directory record")
        eocd = struct.unpack(
            _STRUCT_END_ARCHIVE, data[idx : idx + _SIZE_END_ARCHIVE]
        )
        _, _, _, _, n_total, cd_size, cd_offset, _ = eocd
        if n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
            raise NotImplementedError("Zip64 archives are not supported")
        with open(self.filename, "rb") as f:
            f.seek(cd_offset)
            cd = f.read(cd_size)
        return size, _parse_cd_entries(cd)

    def _apply(self, journal: dict[str, Any], tail: bytes) -> None:
        """Append the staged tail to the formal archive. Deterministic and
        idempotent: the file is first truncated to its pre-commit size.
        """
        old_size = journal["old_size"]
        member_len = journal["member_len"]
        try:
            f = open(self.filename, "r+b")
        except FileNotFoundError:
            f = open(self.filename, "w+b")
        with f:
            f.truncate(old_size)
            f.seek(old_size)
            f.write(tail[:member_len])
            f.flush()
            os.fsync(f.fileno())
            self._checkpoint("members_written")
            f.write(tail[member_len:])
            f.flush()
            os.fsync(f.fileno())
            self._checkpoint("directory_written")
        _fsync_dir(os.path.dirname(os.path.abspath(self.filename)))

    def _recover(self) -> None:
        """Roll an interrupted commit forward, if one was journalled"""
        try:
            with open(self._journal_path, "rb") as f:
                journal = json.load(f)
        except FileNotFoundError:
            for stale in (self._staging_path, self._tail_path):
                try:
                    os.remove(stale)
                except FileNotFoundError:
                    pass
            return
        with open(self._tail_path, "rb") as f:
            tail = f.read()
        if len(tail) != journal["tail_len"] or (
            hashlib.sha256(tail).hexdigest() != journal["tail_sha256"]
        ):
            raise OSError(f"Corrupt handoff staging in {self.handoff_dir}")
        self._apply(journal, tail)
        self._write_json(self._meta_path, journal["new_meta"])
        os.remove(self._journal_path)
        os.remove(self._tail_path)
        _fsync_dir(self.handoff_dir)

    @staticmethod
    def _request_hash(
        changes: dict[str, bytes], deletions: list[str], base: dict[str, int | None]
    ) -> str:
        payload = {
            "changes": {
                k: hashlib.sha256(v).hexdigest() for k, v in sorted(changes.items())
            },
            "deletions": sorted(deletions),
            "base": sorted(base.items()),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @property
    def generation(self) -> int:
        """Number of commits applied so far"""
        return self._meta["generation"]

    def revisions(self) -> dict[str, int]:
        """Current revision of every member present in the formal handoff"""
        return {k: self._meta["revisions"][k] for k in self._meta["members"]}

    def commit(
        self,
        changes: Mapping[str, bytes | bytearray | memoryview] | None = None,
        deletions: Iterable[str] = (),
        base: Mapping[str, int | None] | None = None,
        app_id: str | None = None,
    ) -> CommitReceipt:
        """Atomically apply overwrites and deletions, checked against revisions

        Parameters
        ----------
        changes: mapping of member name to new contents, optional
        deletions: iterable of member names to remove, optional
        base: mapping of member name to the revision the writer based the
            change on; use None for members expected to be absent. Must cover
            every name in ``changes`` and ``deletions``.
        app_id: string
            Application identifier. Retrying a committed application with the
            same id and an identical request succeeds as a no-op, so an
            application interrupted after the commit became durable but before
            the receipt returned takes effect exactly once.

        Returns
        -------
        CommitReceipt

        Raises
        ------
        ConflictError
            If any touched member no longer matches ``base``; nothing is
            applied in that case.
        """
        if self.mode in ("r", "closed"):
            raise OSError(f"Cannot commit in mode {self.mode!r}")
        if app_id is None:
            raise ValueError("app_id is required")
        changes_b = {k: bytes(v) for k, v in (changes or {}).items()}
        deletions_l = list(deletions)
        base_d = dict(base or {})
        if set(changes_b) & set(deletions_l):
            raise ValueError("Same member in changes and deletions")
        touched = list(changes_b) + deletions_l
        missing_base = [k for k in touched if k not in base_d]
        if missing_base:
            raise ValueError(f"Missing base revision for: {missing_base}")
        request_hash = self._request_hash(changes_b, deletions_l, base_d)

        with self._locked():
            self._recover()
            self._meta = meta = self._load_meta()

            if app_id == meta["last_app_id"]:
                if request_hash == meta["last_request_hash"]:
                    return CommitReceipt(app_id, meta["generation"], applied=False)
                raise ValueError(
                    f"app_id {app_id!r} was already used for a different request"
                )

            members = set(meta["members"])
            conflicts = {}
            for key in touched:
                current = meta["revisions"].get(key) if key in members else None
                if base_d[key] != current:
                    conflicts[key] = {"expected": base_d[key], "current": current}
            if conflicts:
                raise ConflictError(conflicts)

            old_size, old_entries = self._read_directory()

            # Stage the changed members in the handoff directory
            with zipfile.ZipFile(
                self._staging_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as zf:
                for key, value in changes_b.items():
                    zf.writestr(key, value)
            with open(self._staging_path, "rb") as f:
                staged = f.read()
            idx = staged.rfind(_STRING_END_ARCHIVE)
            _, _, _, _, _, s_cd_size, s_cd_offset, _ = struct.unpack(
                _STRUCT_END_ARCHIVE, staged[idx : idx + _SIZE_END_ARCHIVE]
            )
            member_bytes = staged[:s_cd_offset]
            staged_entries = _parse_cd_entries(
                staged[s_cd_offset : s_cd_offset + s_cd_size]
            )

            # New central directory: unchanged entries keep their raw records
            # (and therefore their disk location and compressed bytes); deleted
            # and overwritten paths - including stale duplicates - are dropped.
            changed = set(changes_b) | set(deletions_l)
            latest = {}
            for name, raw in old_entries:
                latest[name] = raw  # on duplicate paths the latest entry wins
            new_records = [raw for name, raw in latest.items() if name not in changed]
            for name, raw in staged_entries:
                (old_local_offset,) = struct.unpack("<L", raw[42:46])
                new_records.append(
                    raw[:42] + struct.pack("<L", old_local_offset + old_size) + raw[46:]
                )
            new_cd = b"".join(new_records)
            cd_offset = old_size + len(member_bytes)
            n = len(new_records)
            if n >= 0xFFFF or cd_offset + len(new_cd) > 0xFFFFFFFF:
                raise NotImplementedError("Zip64 archives are not supported")
            eocd = struct.pack(
                _STRUCT_END_ARCHIVE,
                _STRING_END_ARCHIVE,
                0,
                0,
                n,
                n,
                len(new_cd),
                cd_offset,
                0,
            )
            tail = member_bytes + new_cd + eocd

            revisions = dict(meta["revisions"])
            for key in touched:
                revisions[key] = revisions.get(key, 0) + 1
            new_members = [k for k in meta["members"] if k not in deletions_l]
            new_members += [k for k in changes_b if k not in new_members]
            new_meta = {
                "generation": meta["generation"] + 1,
                "members": new_members,
                "revisions": revisions,
                "last_app_id": app_id,
                "last_request_hash": request_hash,
            }

            self._write_atomic(self._tail_path, tail)
            self._checkpoint("staged")
            journal = {
                "app_id": app_id,
                "request_hash": request_hash,
                "old_size": old_size,
                "member_len": len(member_bytes),
                "tail_len": len(tail),
                "tail_sha256": hashlib.sha256(tail).hexdigest(),
                "new_meta": new_meta,
            }
            self._write_json(self._journal_path, journal)
            self._checkpoint("journaled")

            self._apply(journal, tail)
            self._write_json(self._meta_path, new_meta)
            self._checkpoint("meta_written")

            os.remove(self._journal_path)
            os.remove(self._tail_path)
            os.remove(self._staging_path)
            _fsync_dir(self.handoff_dir)
            self._meta = new_meta

        self.flush()  # drop any cached zipfile handle on the pre-commit file
        return CommitReceipt(app_id, new_meta["generation"], applied=True)

    def __iter__(self) -> Iterator[str]:
        seen = set()
        for zi in self.file.filelist:
            if zi.filename not in seen:
                seen.add(zi.filename)
                yield zi.filename

    def __len__(self) -> int:
        return sum(1 for _ in self)
