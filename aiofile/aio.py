import asyncio
import os
from collections import namedtuple
from concurrent.futures import Executor
from functools import partial, wraps
from os import strerror
from pathlib import Path
from typing import (
    Any, Awaitable, BinaryIO, Callable, Dict, Generator, Optional, TextIO,
    TypeVar, Union, cast,
)
from weakref import finalize

import caio
from caio.asyncio_base import AsyncioContextBase


_T = TypeVar("_T")

AIO_FILE_NOT_OPENED = -1
AIO_FILE_CLOSED = -2

FileIOType = Union[TextIO, BinaryIO]

FileMode = namedtuple(
    "FileMode", (
        "readable",
        "writable",
        "plus",
        "appending",
        "created",
        "flags",
        "binary",
    ),
)


def parse_mode(mode: str) -> FileMode:    # noqa: C901
    """ Rewritten from `cpython fileno`_

    .. _cpython fileio: https://bit.ly/2JY2cnp
    """

    flags = os.O_RDONLY

    rwa = False
    writable = False
    readable = False
    plus = False
    appending = False
    created = False
    binary = False

    for m in mode:
        if m == "x":
            rwa = True
            created = True
            writable = True
            flags |= os.O_EXCL | os.O_CREAT

        if m == "r":
            if rwa:
                raise Exception("Bad mode")

            rwa = True
            readable = True

        if m == "w":
            if rwa:
                raise Exception("Bad mode")

            rwa = True
            writable = True

            flags |= os.O_CREAT | os.O_TRUNC

        if m == "a":
            if rwa:
                raise Exception("Bad mode")
            rwa = True
            writable = True
            appending = True
            flags |= os.O_CREAT | os.O_APPEND

        if m == "+":
            if plus:
                raise Exception("Bad mode")
            readable = True
            writable = True
            plus = True

        if m == "b":
            binary = True
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY

    if readable and writable:
        flags |= os.O_RDWR

    elif readable:
        flags |= os.O_RDONLY
    else:
        flags |= os.O_WRONLY

    return FileMode(
        readable=readable,
        writable=writable,
        plus=plus,
        appending=appending,
        created=created,
        flags=flags,
        binary=binary,
    )


class AIOFile:
    _file_obj: Optional[FileIOType]
    _file_obj_owner: bool
    _encoding: str
    _executor: Optional[Executor]
    mode: FileMode
    __open_result: "Optional[asyncio.Future[FileIOType]]"

    def __init__(
        self, filename: Union[str, Path],
        mode: str = "r", encoding: str = "utf-8",
        context: Optional[AsyncioContextBase] = None,
        executor: Optional[Executor] = None,
    ):
        self.__context = context or get_default_context()
        self.__open_result = None

        self._fname = str(filename)
        self._open_mode = mode

        self.mode = parse_mode(mode)

        self._file_obj = None
        self._file_obj_owner = True
        self._encoding = encoding
        self._executor = executor
        self._clone_lock = asyncio.Lock()
        self._clones = 0
        # Low-level I/O operations currently running against our descriptor.
        # close() drains this set so an in-flight write can never land on a
        # recycled descriptor belonging to a later-generation file.
        self._inflight: "set[asyncio.Future[Any]]" = set()
        # Set once the final release has started: no new operation may start.
        self._closing = False
        self._close_task: "Optional[asyncio.Future[None]]" = None
        # Orders the durability flush against close: the last fdsync must
        # complete before the descriptor is closed, even when a segment seal
        # and the holder's close run concurrently.
        self._finalize_lock = asyncio.Lock()

    def _track(
        self, awaitable: "Awaitable[_T]",
    ) -> "asyncio.Future[_T]":
        future = asyncio.ensure_future(awaitable)
        self._inflight.add(future)
        future.add_done_callback(self._inflight.discard)
        return future

    async def _drain_inflight(self) -> None:
        pending = [
            future for future in tuple(self._inflight)
            if not future.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @classmethod
    def from_fp(cls, fp: FileIOType, **kwargs: Any) -> "AIOFile":
        afp = cls(fp.name, fp.mode, **kwargs)
        afp._file_obj = fp
        afp._open_mode = fp.mode
        afp._file_obj_owner = False
        return afp

    def _run_in_thread(
            self, func: "Callable[..., _T]", *args: Any, **kwargs: Any,
    ) -> "asyncio.Future[_T]":
        return self.__context.loop.run_in_executor(
            self._executor, partial(func, *args, **kwargs),
        )

    @property
    def name(self) -> str:
        return self._fname

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self.__context.loop

    @property
    def encoding(self) -> str:
        return self._encoding

    @property
    def closed(self) -> bool:
        return self._file_obj is None or self._file_obj.closed

    def __on_open_done(
        self, future: "asyncio.Future[FileIOType]",
    ) -> None:
        # Adopt the opened file even when the task that started the open()
        # was cancelled while the background open was still running, and
        # reset the shared future on failure so a later open() can retry
        # instead of replaying a dead result to every subsequent waiter.
        if self.__open_result is not future:
            return

        self.__open_result = None

        if future.cancelled() or future.exception() is not None:
            return

        if self._file_obj is None:
            self._file_obj = future.result()

    async def open(self) -> Optional[int]:
        if self._file_obj is not None:
            if self._file_obj.closed:
                raise asyncio.InvalidStateError("AIOFile closed")
            return None

        is_opener = False
        if self.__open_result is None:
            future = cast(
                "asyncio.Future[FileIOType]",
                self._run_in_thread(open, self._fname, self._open_mode),
            )
            self.__open_result = future
            future.add_done_callback(self.__on_open_done)
            is_opener = True
        else:
            # Share the in-flight open. Each waiter shields the shared
            # future: cancelling one waiter's task cancels only its shield,
            # never the background open the other waiters rely on.
            future = self.__open_result

        await asyncio.shield(future)

        if self._file_obj is None:
            # Defensive: the done callback normally adopts the result first.
            self._file_obj = future.result()
            if self.__open_result is future:
                self.__open_result = None

        if is_opener:
            return self._file_obj.fileno()
        return None

    def __repr__(self) -> str:
        return "<AIOFile: %r>" % self._fname

    async def clone(self) -> "AIOFile":
        """Returns self with a ref-count bump; close() is deferred until all
        clones are released."""
        if self._closing:
            raise asyncio.InvalidStateError(
                "AIOFile is closing and can no longer be cloned",
            )
        async with self._clone_lock:
            self._clones += 1
            return self

    async def close(self) -> None:
        # Cheap, per-call release paths keep their original semantics: every
        # close() by a holder releases exactly one reference. Only the actual
        # final descriptor release (drain + flush + close) is coalesced.
        if self._file_obj is None or self._file_obj.closed:
            return

        if not self._file_obj_owner:
            # A borrowed file object is only released, never closed by us.
            return

        async with self._clone_lock:
            if self._clones > 0:
                self._clones -= 1
                return

            if self._close_task is None:
                self._closing = True
                self._close_task = asyncio.ensure_future(
                    self._final_release(),
                )
            release = self._close_task

        await release

    async def _final_release(self) -> None:
        # Wait for in-flight operations so a write can never land on a
        # descriptor closed and recycled for a later generation.
        await self._drain_inflight()

        # Hold the lock across flush and close so no concurrent fdsync (e.g.
        # a segment seal) can observe a half-closed descriptor, and durability
        # is always established before the descriptor goes away.
        async with self._finalize_lock:
            if self._file_obj is None or self._file_obj.closed:
                return

            if self.mode.writable:
                await self.__context.fdsync(self.fileno())

            await self._run_in_thread(self._file_obj.close)

    def fileno(self) -> int:
        if self._file_obj is None:
            raise asyncio.InvalidStateError("AIOFile closed")
        return self._file_obj.fileno()

    def __await__(self) -> Generator[None, Any, "AIOFile"]:
        yield from self.open().__await__()
        return self

    async def __aenter__(self) -> "AIOFile":
        await self.open()
        return self

    def __aexit__(self, *args: Any) -> Awaitable[Any]:
        return asyncio.get_event_loop().create_task(self.close())

    async def read(self, size: int = -1, offset: int = 0) -> Union[bytes, str]:
        data = await self.read_bytes(size, offset)
        return data if self.mode.binary else self.decode_bytes(data)

    async def read_bytes(self, size: int = -1, offset: int = 0) -> bytes:
        if size < -1:
            raise ValueError("Unsupported value %d for size" % size)

        if self._closing:
            raise asyncio.InvalidStateError(
                "AIOFile is closing and no longer starts operations",
            )

        return await self._track(self._read_bytes(size, offset))

    async def _read_bytes(self, size: int, offset: int) -> bytes:
        if size == -1:
            size = (
                await self._run_in_thread(
                    os.stat,
                    self.fileno(),
                )
            ).st_size

        return await self.__context.read(size, self.fileno(), offset)

    async def write(self, data: Union[str, bytes], offset: int = 0) -> int:
        if self.mode.binary:
            if not isinstance(data, bytes):
                raise ValueError("Data must be bytes in binary mode")
            bytes_data = data
        else:
            if not isinstance(data, str):
                raise ValueError("Data must be str in text mode")
            bytes_data = self.encode_bytes(data)

        return await self.write_bytes(bytes_data, offset)

    def encode_bytes(self, data: str) -> bytes:
        return data.encode(self._encoding)

    def decode_bytes(self, data: bytes) -> str:
        return data.decode(self._encoding)

    async def write_bytes(self, data: bytes, offset: int = 0) -> int:
        data_size = len(data)
        if data_size == 0:
            return 0

        if self._closing:
            raise asyncio.InvalidStateError(
                "AIOFile is closing and no longer starts operations",
            )

        return await self._track(self._write_bytes(data, offset))

    async def _write_bytes(self, data: bytes, offset: int) -> int:
        data_size = len(data)

        # data can be written partially, see write(2)
        # (https://www.man7.org/linux/man-pages/man2/write.2.html)
        # for example, it can happen when a disk quota or a resource limit
        # is exceeded (in that case subsequent call will return a
        # corresponding error) or write has been interrupted by
        # an incoming signal

        # behaviour here in regard to continue trying to write remaining data
        # corresponds to the behaviour of io.BufferedIOBase
        # (https://docs.python.org/3/library/io.html#io.BufferedIOBase.write)
        # which used by object returned open() with `buffering` argument >= 1
        # (effectively the default)

        written = 0
        while written < data_size:
            res = await self.__context.write(
                data[written:], self.fileno(), offset + written,
            )
            if res == 0:
                raise RuntimeError(
                    "Write operation returned 0", self, offset, written,
                )
            elif res < 0:
                # fix for linux_aio implementation bug in caio<=0.6.1
                # (https://github.com/mosquito/caio/pull/7)
                # and safeguard against future similar issues
                errno = -res
                raise OSError(errno, strerror(errno), self._fname)

            written += res

        return written

    async def fsync(self) -> None:
        async with self._finalize_lock:
            return await self.__context.fsync(self.fileno())

    async def fdsync(self) -> None:
        async with self._finalize_lock:
            if self._file_obj is not None and self._file_obj.closed:
                return None
            return await self.__context.fdsync(self.fileno())

    def truncate(self, length: int = 0) -> Awaitable[None]:
        return self._run_in_thread(
            os.ftruncate, self.fileno(), length,
        )


# Keyed by id(loop) so the keys themselves do not retain event loops. Values
# still retain their loops through caio contexts, so create_context() also
# installs synchronous cleanup into loop.close(). This is important for
# short-lived loops: waiting for garbage collection would keep native AIO
# resources (and io_uring SQPOLL threads) alive between loop instances.
ContextStoreType = Dict[int, caio.AsyncioContext]
DEFAULT_CONTEXT_STORE: ContextStoreType = {}


def _release_context(loop_id: int) -> None:
    context = DEFAULT_CONTEXT_STORE.pop(loop_id, None)
    if context is not None:
        context.close()


def _install_context_cleanup(
    loop: asyncio.AbstractEventLoop,
) -> None:
    original_close = loop.close
    loop_id = id(loop)

    @wraps(original_close)
    def close() -> None:
        if loop.is_running():
            original_close()
            return

        try:
            _release_context(loop_id)
        finally:
            original_close()

    try:
        setattr(loop, "close", close)
    except (AttributeError, TypeError):
        # Some third-party event loops do not allow instance attributes.
        # The finalizer below still provides interpreter-shutdown cleanup.
        pass


def create_context(
    max_requests: int = caio.AsyncioContext.MAX_REQUESTS_DEFAULT,
) -> caio.AsyncioContext:
    loop = asyncio.get_event_loop()
    context = caio.AsyncioContext(max_requests, loop=loop)

    _install_context_cleanup(loop)
    finalize(loop, _release_context, id(loop))
    DEFAULT_CONTEXT_STORE[id(loop)] = context
    return context


def get_default_context() -> caio.AsyncioContext:
    loop = asyncio.get_event_loop()
    context = DEFAULT_CONTEXT_STORE.get(id(loop))

    if context is not None:
        return context

    return create_context()
