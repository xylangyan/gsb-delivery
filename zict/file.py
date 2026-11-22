from __future__ import annotations

import mmap
import os
import pathlib
from collections.abc import Iterator
from urllib.parse import quote, unquote

from zict.common import ZictBase, locked


class File(ZictBase[str, bytes]):
    """Mutable Mapping interface to a directory

    Keys must be strings, values must be buffers

    Values are published crash-safely and are visible to other processes that
    open the same directory afterwards. Each write goes to a temporary
    ``<key>#<n>.part`` file which is atomically renamed to ``<key>#<n>`` only
    after it has been fully written and closed. The rename is the commit
    point: if the writer is interrupted beforehand, the orphan ``.part`` file
    is discarded on the next open and never becomes visible; if the writer is
    interrupted afterwards, the new version has already been published and
    remains the current one, even if the cleanup of the old version (the
    "receipt") never happened.

    Deletions are committed with a ``<key>#tomb`` tombstone before any data
    file is unlinked, so a deleted key never reappears when the directory is
    reopened - even if the operating system refuses to unlink its files, e.g.
    on Windows or whenever a file is still memory-mapped by this or another
    process. Files that cannot be removed immediately are retried on the next
    open, write, or :meth:`close`.

    Successive versions of a key are stored as separate files and old versions
    are unlinked only after the new version has been published, so a reader
    holding a memory map (or a slice of one) of a previous version keeps
    reading consistent data, and existing mappings never block other processes
    from publishing newer versions.

    Parameters
    ----------
    directory: str
        Directory to write to. If it already exists, existing files will be
        imported as mapping elements. If it doesn't exist, it will be created.
    memmap: bool (optional)
        If True, use `mmap` for reading. Defaults to False.

    Notes
    -----
    If you call methods of this class from multiple threads, access will be
    fast as long as atomic disk access such as ``open``, ``os.fstat``, and
    ``os.remove`` is fast. This is not always the case, e.g. in case of slow
    network mounts or spun-down magnetic drives.

    Keys are loaded from disk when the object is created; writes performed
    through other File instances (or other processes) after that are not
    visible to this instance. Concurrent writes to the same key from multiple
    processes are not supported.

    Examples
    --------
    >>> z = File('myfile')  # doctest: +SKIP
    >>> z['x'] = b'123'  # doctest: +SKIP
    >>> z['x']  # doctest: +SKIP
    b'123'

    Also supports writing lists of bytes objects

    >>> z['y'] = [b'123', b'4567']  # doctest: +SKIP
    >>> z['y']  # doctest: +SKIP
    b'1234567'

    Or anything that can be used with file.write, like a memoryview

    >>> z['data'] = np.ones(5).data  # doctest: +SKIP
    """

    directory: str
    memmap: bool
    filenames: dict[str, str]
    _inc: int
    _tombs: set[str]  # quoted keys with a tombstone on disk
    _pending: set[str]  # file names whose deletion was refused by the OS

    def __init__(self, directory: str | pathlib.Path, memmap: bool = False):
        super().__init__()
        self.directory = str(directory)
        self.memmap = memmap
        self.filenames = {}
        self._inc = 0
        self._tombs = set()
        self._pending = set()

        if not os.path.exists(self.directory):
            os.makedirs(self.directory, exist_ok=True)
        else:
            self._import_directory()

    @staticmethod
    def _parse_filename(fn: str) -> tuple[str, str, int] | None:
        """Split a file name into (quoted key, kind, version).

        kind is one of "data" (committed), "part" (unpublished temporary
        file), or "tomb" (tombstone). Return None for foreign files that
        don't belong to the protocol.
        """
        if fn.endswith(".part"):
            q, sep, ver = fn[: -len(".part")].rpartition("#")
            if sep and ver.isdigit():
                return q, "part", int(ver)
            return None
        q, sep, ver = fn.rpartition("#")
        if not sep:
            # Legacy file written by zict < 3.0, without a version suffix
            return fn, "data", -1
        if ver == "tomb":
            return q, "tomb", -1
        if ver.isdigit():
            return q, "data", int(ver)
        # Legacy/foreign file; everything before the last '#' is the key
        return q, "data", -1

    def _import_directory(self) -> None:
        """Rebuild the in-memory key index from the on-disk state and collect
        garbage left behind by interrupted processes:

        - ``.part`` files of writers that died before publishing
        - old versions whose replacement was published but whose receipt
          (deletion) never happened
        - data files of deleted keys that could not be unlinked, and their
          tombstones once nothing is left
        """
        versions: dict[str, dict[int, list[str]]] = {}
        parts = []
        max_ver = -1
        for fn in os.listdir(self.directory):
            parsed = self._parse_filename(fn)
            if parsed is None:
                continue
            q, kind, ver = parsed
            max_ver = max(max_ver, ver)
            if kind == "part":
                parts.append(fn)
            elif kind == "tomb":
                self._tombs.add(q)
            else:
                versions.setdefault(q, {}).setdefault(ver, []).append(fn)

        for q, vers in versions.items():
            if q in self._tombs:
                continue  # deleted key; collected by _sweep_tombstoned below
            cur = max(vers)
            current_fn = versions[q][cur][0]
            self.filenames[unquote(q)] = current_fn
            for ver, fns in vers.items():
                for fn in fns:
                    if fn != current_fn:
                        self._remove_quiet(fn)

        self._inc = max_ver + 1

        for fn in parts:
            self._remove_quiet(fn)
        for q in list(self._tombs):
            self._sweep_tombstoned(q)

    def _safe_key(self, key: str) -> str:
        """Escape key so that it is usable on all filesystems.

        Append to the filenames a unique suffix that changes every time this
        method is called. This prevents race conditions when another thread
        accesses the same key, e.g. ``__setitem__`` on one thread and
        ``__getitem__`` on another, and makes sure that a file name is never
        reused for a different value.
        """
        # `#` is escaped by quote and is supported by most file systems
        key = quote(key, safe="") + f"#{self._inc}"
        self._inc += 1
        return key

    @staticmethod
    def _unsafe_key(key: str) -> str:
        """Undo the escaping done by _safe_key()"""
        key = key.split("#")[0]
        return unquote(key)

    def __str__(self) -> str:
        return f"<File: {self.directory}, {len(self)} elements>"

    __repr__ = __str__

    def _remove_quiet(self, fn: str) -> bool:
        """Best-effort deletion of a garbage file. Return True if the file is
        gone.

        If the OS refuses to delete the file - e.g. on Windows, or anywhere a
        file is still memory-mapped by this or another process - remember it
        and retry on the next open, write, or close().
        """
        try:
            os.remove(os.path.join(self.directory, fn))
        except FileNotFoundError:
            # Another process or thread already removed it
            self._pending.discard(fn)
            return True
        except OSError:
            self._pending.add(fn)
            return False
        self._pending.discard(fn)
        return True

    def _sweep_tombstoned(self, qkey: str) -> None:
        """Best-effort deletion of all committed versions of a deleted key,
        followed by its tombstone once nothing is left. Unpublished ``.part``
        files are left alone: they may belong to a live writer and are
        collected on open instead.
        """
        prefix = qkey + "#"
        tomb = prefix + "tomb"
        try:
            fns = os.listdir(self.directory)
        except OSError:
            return
        remaining = False
        for fn in fns:
            if (
                fn.startswith(prefix)
                and fn != tomb
                and not fn.endswith(".part")
                and not self._remove_quiet(fn)
            ):
                remaining = True
        if not remaining and self._remove_quiet(tomb):
            self._tombs.discard(qkey)

    def _retry_pending(self) -> None:
        """Retry deletions that the OS previously refused, e.g. because a file
        was still memory-mapped. Called on open, write, and close(), so that
        versions abandoned by readers that released their views or exited are
        eventually discovered and removed.
        """
        for fn in list(self._pending):
            self._remove_quiet(fn)
        # A tombstone can only be removed once its data files are gone
        for qkey in list(self._tombs):
            self._sweep_tombstoned(qkey)

    def close(self) -> None:
        """Release any system resources held by this object.

        Retry the deletion of files that the OS previously refused to remove.
        """
        if getattr(self, "_pending", None):
            self._retry_pending()

    @locked
    def __getitem__(self, key: str) -> bytearray | memoryview:
        fn = os.path.join(self.directory, self.filenames[key])

        # distributed.protocol.numpy.deserialize_numpy_ndarray makes sure that, if the
        # numpy array was writeable before serialization, remains writeable afterwards.
        # If it receives a read-only buffer (e.g. from fh.read() or from a mmap to a
        # read-only file descriptor), it performs an expensive memcpy.
        # Note that this is a dask-specific feature; vanilla pickle.loads will instead
        # return an array with flags.writeable=False.

        if self.memmap:
            with open(fn, "r+b") as fh:
                return memoryview(mmap.mmap(fh.fileno(), 0))
        else:
            with open(fn, "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                buf = bytearray(size)
                with self.unlock():
                    nread = fh.readinto(buf)
                assert nread == size
                return buf

    @locked
    def __setitem__(
        self,
        key: str,
        value: bytes
        | bytearray
        | memoryview
        | list[bytes | bytearray | memoryview]
        | tuple[bytes | bytearray | memoryview, ...],
    ) -> None:
        fn = self._safe_key(key)
        tmp_fn = fn + ".part"
        tmp_path = os.path.join(self.directory, tmp_fn)
        try:
            with open(tmp_path, "wb") as fh, self.unlock():
                if isinstance(value, (tuple, list)):
                    fh.writelines(value)
                else:
                    fh.write(value)
        except BaseException:
            self._remove_quiet(tmp_fn)
            raise

        # Atomic publish: the rename is the commit point. A reader only ever
        # sees fully written files; if this process is interrupted right after
        # this line, the new version is the current one on the next open.
        os.replace(tmp_path, os.path.join(self.directory, fn))

        old_fn = self.filenames.get(key)
        self.filenames[key] = fn
        if old_fn is not None:
            # Receipt: the old version is now unreachable. If the process is
            # interrupted before this line, the next open finds both versions
            # on disk and collects the old one. If the OS refuses to unlink it
            # because it is still memory-mapped, the deletion is retried later;
            # either way, publishing the new version is not blocked.
            self._remove_quiet(old_fn)

        qkey = quote(key, safe="")
        if qkey in self._tombs:
            # A deletion of this key was previously committed; the new value
            # wins over the tombstone.
            if self._remove_quiet(qkey + "#tomb"):
                self._tombs.discard(qkey)

        self._retry_pending()

    def __contains__(self, key: object) -> bool:
        return key in self.filenames

    def __iter__(self) -> Iterator[str]:
        return iter(self.filenames)

    @locked
    def __delitem__(self, key: str) -> None:
        self.filenames.pop(key)
        qkey = quote(key, safe="")
        # Commit the deletion before touching any data file, so that the key
        # cannot resurrect on reopen even if unlinking its files is refused
        # (e.g. still memory-mapped by another process) or this process is
        # interrupted halfway through.
        with open(os.path.join(self.directory, qkey + "#tomb"), "wb"):
            pass
        self._tombs.add(qkey)
        self._sweep_tombstoned(qkey)

    def __len__(self) -> int:
        return len(self.filenames)
