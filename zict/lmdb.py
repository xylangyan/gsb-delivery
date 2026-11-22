from __future__ import annotations

import os
import pathlib
import struct
import sys
import tempfile
import types
from collections.abc import Callable, ItemsView, Iterable, Iterator, Mapping, ValuesView
from typing import Any

from zict.common import ZictBase

try:
    import lmdb
except ImportError:  # pragma: no cover - lmdb is an optional dependency
    # The package is only needed when an LMDB mapping is actually opened. The
    # shim below keeps the module (and its exception hierarchy) importable and
    # gives static type checkers a single, unconditional base class.
    lmdb = types.ModuleType("lmdb")
    lmdb.MapFullError = Exception  # type: ignore
    lmdb.Transaction = object  # type: ignore


def _encode_key(key: str) -> bytes:
    return key.encode("utf-8")


def _decode_key(key: bytes) -> str:
    return key.decode("utf-8")


class VersionConflictError(Exception):
    """Raised by a conditional batch commit when the condition key no longer holds.

    The batch is never published; its staged journal is left on disk so that the
    caller can re-read the current value and retry without re-reading the source
    manifest.
    """

    def __init__(self, key: str, expected: bytes | None, actual: bytes | None):
        self.key = key
        self.expected = expected
        self.actual = actual
        self.journal_path: pathlib.Path | None = None
        super().__init__(
            f"version conflict on key {key!r}: expected {expected!r}, found {actual!r}"
        )


class BatchItemError(TypeError):
    """Raised when an item of a batch has a non-str key or a non-bytes value.

    The error reports the zero-based position of the offending item in the input
    stream, so format errors are reported at their original location even when the
    manifest can only be read once.
    """

    def __init__(self, index: int, *, bad_key: bool, value: object):
        self.index = index
        self.bad_key = bad_key
        self.value = value
        kind = "key" if bad_key else "value"
        super().__init__(
            f"invalid {kind} at batch position {index}: {value!r} is not a "
            f"{'str' if bad_key else 'bytes'}"
        )


class MapSizeExceededError(lmdb.MapFullError):
    """Raised when a batch does not fit and the map cannot grow any further.

    Subclasses :class:`lmdb.MapFullError` (plain :class:`Exception` when the
    optional lmdb package is missing), so callers catching the raw LMDB error
    keep working. The staged journal is left on disk for a later retry with a
    compliant (smaller) batch.
    """

    map_size: int
    max_map_size: int
    journal_path: pathlib.Path | None

    def __init__(self, map_size: int, max_map_size: int):
        self.map_size = map_size
        self.max_map_size = max_map_size
        self.journal_path = None
        super().__init__(
            f"map full at size {map_size}; configured maximum is {max_map_size}"
        )


class JournalError(Exception):
    """Raised when a staged batch journal is missing, truncated, or not sealed."""


# On-disk journal layout:
#   magic (8 bytes)
#   repeated frames: uint32 little-endian key length, uint32 value length,
#                    key bytes, value bytes
#   sentinel frame: key length == _SENTINEL, value length == 0
# A journal without the trailing sentinel was never sealed (e.g. the writer died
# halfway) and is rejected on replay instead of publishing a partial batch.
_JOURNAL_MAGIC = b"ZICTJNL1"
_SENTINEL = 0xFFFFFFFF
_FRAME_HEADER = struct.Struct("<II")
_JOURNAL_DIRNAME = "zict-journal"
_REPLAY_CHUNK = 256
# Safety net against an endless grow/replay loop when max_map_size is unbounded
# and a batch can never fit (e.g. the filesystem is exhausted).
_MAX_GROW_ATTEMPTS = 32


class _BatchJournal:
    """Disk-backed, single-pass staging area for a batch.

    The manifest is streamed to a temporary file instead of being held in memory,
    so arbitrarily large batches are supported as long as they fit on disk. The
    sealed journal can be replayed into a write transaction as many times as
    needed, which makes retries after ``MDB_MAP_FULL`` or version conflicts
    possible without re-reading the (single-pass) source.
    """

    def __init__(self, journal_dir: pathlib.Path):
        self._journal_dir = journal_dir
        self._file: Any = None
        self._tmp_path: pathlib.Path | None = None
        self.path: pathlib.Path | None = None
        self.count = 0

    def __enter__(self) -> _BatchJournal:
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=f"batch-{os.getpid()}-", suffix=".tmp", dir=self._journal_dir
        )
        self._file = os.fdopen(fd, "wb")
        self._tmp_path = pathlib.Path(tmp)
        self._file.write(_JOURNAL_MAGIC)
        return self

    def add(self, key: bytes, value: bytes) -> None:
        assert self._file is not None
        self._file.write(_FRAME_HEADER.pack(len(key), len(value)))
        self._file.write(key)
        self._file.write(value)
        self.count += 1

    def seal(self) -> pathlib.Path:
        """Flush the journal and publish it atomically.

        Returns the path of the sealed journal.
        """
        assert self._file is not None and self._tmp_path is not None
        self._file.write(_FRAME_HEADER.pack(_SENTINEL, 0))
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._file = None
        final = self._tmp_path.with_suffix(".journal")
        os.replace(self._tmp_path, final)
        self._tmp_path = None
        self.path = final
        _fsync_directory(self._journal_dir)
        return final

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
        if self._tmp_path is not None:
            self._tmp_path.unlink(missing_ok=True)

    @staticmethod
    def iter_chunks(
        path: pathlib.Path, chunk_size: int = _REPLAY_CHUNK
    ) -> Iterator[list[tuple[bytes, bytes]]]:
        """Yield the journal contents in bounded-size chunks, without loading the
        whole batch in memory."""
        with open(path, "rb") as fh:
            magic = fh.read(len(_JOURNAL_MAGIC))
            if magic != _JOURNAL_MAGIC:
                raise JournalError(f"{path} is not a zict batch journal")
            chunk: list[tuple[bytes, bytes]] = []
            sealed = False
            while True:
                header = fh.read(_FRAME_HEADER.size)
                if not header:
                    break
                if len(header) < _FRAME_HEADER.size:
                    raise JournalError(f"{path} is truncated mid-frame header")
                key_len, value_len = _FRAME_HEADER.unpack(header)
                if key_len == _SENTINEL:
                    sealed = True
                    break
                key = fh.read(key_len)
                value = fh.read(value_len)
                if len(key) < key_len or len(value) < value_len:
                    raise JournalError(f"{path} is truncated mid-frame payload")
                chunk.append((key, value))
                if len(chunk) == chunk_size:
                    yield chunk
                    chunk = []
            if not sealed:
                raise JournalError(f"{path} was never sealed; refusing to replay it")
            if chunk:
                yield chunk


def _fsync_directory(directory: pathlib.Path) -> None:
    if sys.platform == "win32":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class LMDBSnapshot(Mapping[str, bytes]):
    """Read-only, immutable snapshot of an :class:`LMDB` database.

    Wraps a single LMDB read transaction. Iteration and lookups always observe the
    database revision that was current when the snapshot was taken, even while
    other processes publish new commits. Close it (or use it as a context
    manager) to release the read transaction.
    """

    def __init__(self, txn: lmdb.Transaction):
        self._txn = txn
        self._closed = False

    def __getitem__(self, key: str) -> bytes:
        if not isinstance(key, str):
            raise KeyError(key)
        value = self._txn.get(_encode_key(key))
        if value is None:
            raise KeyError(key)
        return value

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return self._txn.cursor().set_key(_encode_key(key))

    def __iter__(self) -> Iterator[str]:
        cursor = self._txn.cursor()
        return (_decode_key(k) for k in cursor.iternext(keys=True, values=False))

    def __len__(self) -> int:
        return self._txn.stat()["entries"]

    def close(self) -> None:
        if not self._closed:
            self._txn.abort()
            self._closed = True

    def __enter__(self) -> LMDBSnapshot:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class LMDB(ZictBase[str, bytes]):
    """Mutable Mapping interface to a LMDB database.

    Keys must be strings, values must be bytes

    Parameters
    ----------
    directory: str
    map_size: int
        Initial maximum size of the database file on disk.
        Defaults to 1 TiB on 64 bit systems and 1 GiB on 32 bit ones (10 MiB on
        Windows).
    max_map_size: int, optional
        Hard upper bound for automatic growth. If a batch does not fit once the
        map has grown to this size, :class:`MapSizeExceededError` is raised and
        the batch is left staged on disk. Defaults to ``None``, i.e. unbounded
        growth.
    grow_factor: float, optional
        Multiplier applied to the current map size on every automatic growth
        attempt. Defaults to 2.

    Notes
    -----
    None of this class is thread-safe - not even normally trivial methods such as
    ``__len__ `` or ``__contains__``.

    Examples
    --------
    >>> z = LMDB('/tmp/somedir/')  # doctest: +SKIP
    >>> z['x'] = b'123'  # doctest: +SKIP
    >>> z['x']  # doctest: +SKIP
    b'123'
    """

    def __init__(
        self,
        directory: str | pathlib.Path,
        map_size: int | None = None,
        max_map_size: int | None = None,
        grow_factor: float = 2.0,
        readonly: bool = False,
    ):
        import lmdb

        super().__init__()
        if map_size is None:
            if sys.platform != "win32":
                map_size = min(2**40, sys.maxsize // 4)
            else:
                map_size = 10 * 2**20
        if grow_factor <= 1.0:
            raise ValueError("grow_factor must be strictly greater than 1")
        if max_map_size is not None and max_map_size < map_size:
            raise ValueError("max_map_size must be >= map_size")

        self.directory = pathlib.Path(directory)
        self.readonly = readonly
        self.max_map_size = max_map_size
        self.grow_factor = grow_factor
        self._map_size = map_size
        # Test/observability hook: invoked with no write transaction active after
        # a MDB_MAP_FULL abort and before the map is grown and the batch replayed.
        self.on_map_full: Callable[[], None] | None = None

        self.db = lmdb.open(
            str(directory),
            subdir=True,
            map_size=map_size,
            sync=False,
            writemap=True,
            readonly=readonly,
        )

    @property
    def map_size(self) -> int:
        """Map size currently configured on this handle (bytes)."""
        return self._map_size

    def set_map_size(self, map_size: int) -> int:
        """Explicitly resize the map.

        This is visible to other processes opening the database later. Use
        :attr:`max_map_size` to bound the automatic growth performed by batch
        commits; this method bypasses that bound.

        Returns the map size actually in effect, which another process may have
        raised above the requested value.
        """
        if map_size <= 0:
            raise ValueError("map_size must be positive")
        self.db.set_mapsize(map_size)
        # Another process may grow the map concurrently; LMDB then silently adopts
        # the larger of the two sizes, so re-read the effective value.
        self._map_size = self.db.info()["map_size"]
        return self._map_size

    @property
    def _journal_dir(self) -> pathlib.Path:
        return self.directory / _JOURNAL_DIRNAME

    def __getitem__(self, key: str) -> bytes:
        if not isinstance(key, str):
            raise KeyError(key)
        with self.db.begin() as txn:
            value = txn.get(_encode_key(key))
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: bytes) -> None:
        if not isinstance(key, str):
            raise TypeError(key)
        if not isinstance(value, bytes):
            raise TypeError(value)
        with self.db.begin(write=True) as txn:
            txn.put(_encode_key(key), value)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        with self.db.begin() as txn:
            return txn.cursor().set_key(_encode_key(key))

    def __iter__(self) -> Iterator[str]:
        cursor = self.db.begin().cursor()
        return (_decode_key(k) for k in cursor.iternext(keys=True, values=False))

    def items(self) -> ItemsView[str, bytes]:
        return LMDBItemsView(self)

    def values(self) -> ValuesView[bytes]:
        return LMDBValuesView(self)

    def snapshot(self) -> LMDBSnapshot:
        """Take an immutable read-only snapshot.

        The snapshot pins one database revision until it is closed; commits from
        other processes or later batches are invisible to it. New snapshots, by
        contrast, observe the latest fully committed revision.
        """
        return LMDBSnapshot(self.db.begin())

    def stage_batch(
        self, items: Iterable[tuple[str, bytes]]
    ) -> pathlib.Path:
        """Validate a batch and stream it to a sealed on-disk journal.

        The input iterable is consumed exactly once. Items with a non-str key or
        a non-bytes value raise :class:`BatchItemError` carrying their original
        (zero-based) position; nothing is staged in that case.

        Returns the path of the sealed journal, to be passed to
        :meth:`commit_staged` (possibly several times).
        """
        with _BatchJournal(self._journal_dir) as journal:
            for index, (key, value) in enumerate(items):
                if not isinstance(key, str):
                    raise BatchItemError(index, bad_key=True, value=key)
                if not isinstance(value, bytes):
                    raise BatchItemError(index, bad_key=False, value=value)
                journal.add(_encode_key(key), value)
            return journal.seal()

    def discard_staged(self, journal_path: str | pathlib.Path) -> None:
        """Delete a staged batch journal."""
        pathlib.Path(journal_path).unlink(missing_ok=True)

    def commit_items(
        self,
        items: Iterable[tuple[str, bytes]],
        condition: tuple[str, bytes | None] | None = None,
    ) -> None:
        """Stage and atomically commit a batch.

        Duplicate keys inside the batch resolve to the last write. On success the
        staged journal is deleted; on failure it is kept on disk and its path is
        attached to the raised exception.

        Parameters
        ----------
        items:
            Iterable of ``(key, value)`` pairs, read exactly once.
        condition:
            Optional optimistic-concurrency check ``(key, expected_value)``.
            The batch is published only if, inside the write transaction, the
            condition key holds ``expected_value``; ``None`` means the key must
            be absent. A mismatch raises :class:`VersionConflictError`.
        """
        journal_path = self.stage_batch(items)
        self.commit_staged(journal_path, condition=condition)

    def commit_staged(
        self,
        journal_path: str | pathlib.Path,
        condition: tuple[str, bytes | None] | None = None,
    ) -> None:
        """Commit a previously staged batch journal.

        The journal is replayed from disk inside a single write transaction, so
        retries never require re-reading the source manifest and a commit is
        always all-or-nothing. ``MDB_MAP_FULL`` triggers a bounded automatic
        growth followed by a full replay; a failed condition raises
        :class:`VersionConflictError` without publishing anything.
        """
        import lmdb

        path = pathlib.Path(journal_path)
        if not path.is_file():
            raise JournalError(f"staged journal not found: {path}")
        if condition is not None:
            condition_key, expected_value = condition
            if not isinstance(condition_key, str):
                raise TypeError(condition_key)
            if expected_value is not None and not isinstance(expected_value, bytes):
                raise TypeError(expected_value)
            encoded_condition_key = _encode_key(condition_key)
        else:
            condition_key = ""
            expected_value = b""
            encoded_condition_key = b""

        attempts = 0
        while True:
            txn = self.db.begin(write=True)
            try:
                if condition is not None:
                    actual_value = txn.get(encoded_condition_key)
                    if actual_value != expected_value:
                        raise VersionConflictError(
                            condition_key, expected_value, actual_value
                        )
                cursor = txn.cursor()
                for chunk in _BatchJournal.iter_chunks(path):
                    cursor.putmulti(chunk)
                txn.commit()
                break
            except lmdb.MapFullError:
                # The failed transaction is already aborted by LMDB; abort()
                # again is harmless and frees any state held by the wrapper.
                txn.abort()
                attempts += 1
                if attempts > _MAX_GROW_ATTEMPTS:
                    error = MapSizeExceededError(
                        self._map_size, self.max_map_size or self._map_size
                    )
                    error.journal_path = path
                    raise error
                if self.on_map_full is not None:
                    self.on_map_full()
                try:
                    self._grow_map()
                except MapSizeExceededError as exc:
                    exc.journal_path = path
                    raise
            except VersionConflictError as exc:
                txn.abort()
                exc.journal_path = path
                raise
            except MapSizeExceededError as exc:
                txn.abort()
                exc.journal_path = path
                raise
            except BaseException:
                txn.abort()
                raise

        path.unlink(missing_ok=True)

    def _grow_map(self) -> int:
        """Grow the map once, honouring :attr:`max_map_size`.

        Returns the new map size. Raises :class:`MapSizeExceededError` if the
        configured cap does not allow any further growth.
        """
        current = self._map_size
        if self.max_map_size is not None and current >= self.max_map_size:
            raise MapSizeExceededError(current, self.max_map_size)
        target = int(current * self.grow_factor)
        if self.max_map_size is not None:
            target = min(target, self.max_map_size)
        if target <= current:
            if self.max_map_size is None:
                raise MapSizeExceededError(current, current)
            raise MapSizeExceededError(current, self.max_map_size)
        self.db.set_mapsize(target)
        # Re-read the effective size: another process may have grown the map in
        # the meantime and LMDB adopts the largest requested value.
        self._map_size = self.db.info()["map_size"]
        return self._map_size

    def _do_update(self, items: Iterable[tuple[str, bytes]]) -> None:
        # Stream to disk first so single-pass iterables survive MapFull retries.
        journal_path = self.stage_batch(items)
        self.commit_staged(journal_path)

    def __delitem__(self, key: str) -> None:
        if not isinstance(key, str):
            raise KeyError(key)
        with self.db.begin(write=True) as txn:
            if not txn.delete(_encode_key(key)):
                raise KeyError(key)

    def __len__(self) -> int:
        return self.db.stat()["entries"]

    def close(self) -> None:
        self.db.close()


class LMDBItemsView(ItemsView[str, bytes]):
    _mapping: LMDB  # FIXME CPython implementation detail
    __slots__ = ()

    def __contains__(self, item: object) -> bool:
        key: str
        value: object
        key, value = item  # type: ignore
        try:
            v = self._mapping[key]
        except KeyError:
            return False
        else:
            return v == value

    def __iter__(self) -> Iterator[tuple[str, bytes]]:
        cursor = self._mapping.db.begin().cursor()
        return ((_decode_key(k), v) for k, v in cursor.iternext(keys=True, values=True))


class LMDBValuesView(ValuesView[bytes]):
    _mapping: LMDB  # FIXME CPython implementation detail
    __slots__ = ()

    def __contains__(self, value: object) -> bool:
        return any(value == v for v in self)

    def __iter__(self) -> Iterator[bytes]:
        cursor = self._mapping.db.begin().cursor()
        return cursor.iternext(keys=False, values=True)
