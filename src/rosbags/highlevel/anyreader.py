# Copyright 2020-2026 Ternaris
# SPDX-License-Identifier: Apache-2.0
"""Tools for reading all rosbag versions with unified api."""

from __future__ import annotations

import functools
import operator
from collections import OrderedDict
from contextlib import ExitStack, suppress
from heapq import merge
from itertools import groupby
from typing import TYPE_CHECKING, cast

from rosbags.interfaces import (
    MessageDefinition,
    MessageDefinitionFormat,
    Nodetype,
    TopicInfo,
)
from rosbags.rosbag1 import (
    Reader as Reader1,
    ReaderError as ReaderError1,
)
from rosbags.rosbag2 import (
    Reader as Reader2,
    ReaderError as ReaderError2,
)
from rosbags.serde import SerdeError
from rosbags.typesys import (
    Stores,
    TypesysError,
    get_types_from_idl,
    get_types_from_msg,
    get_typestore,
)

if TYPE_CHECKING:
    import sys
    from collections.abc import Collection, Generator, Iterable, Sequence
    from types import TracebackType
    from typing import Literal

    if sys.version_info >= (3, 11):
        from typing import Self
    else:
        from typing_extensions import Self

    from rosbags.interfaces import Connection
    from rosbags.interfaces.typing import RPath, Typesdict
    from rosbags.typesys.store import Typestore


class AnyReaderError(Exception):
    """Reader error."""


CONFLICT_HINT = 'pass the delivering connection to deserialize()'


def _closure_names(
    name: str, defs: dict[tuple[int, object], Typesdict], conflicting: set[str]
) -> set[str]:
    """Return conflicting types reachable from a type across all connections."""
    found: set[str] = set()
    for types in defs.values():
        if name not in types:
            continue
        pending = [name]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen or current not in types:
                continue
            if current in conflicting:
                found.add(current)
            seen.add(current)
            _, fields = types[current]
            pending.extend(
                cast('str', base[1])
                for _, desc in fields
                if (base := desc if desc[0] in {Nodetype.BASE, Nodetype.NAME} else desc[1][0])[0]
                == Nodetype.NAME
            )
    return found


def _merge_types(target: Typesdict, source: Typesdict) -> None:
    """Merge definitions, keeping the first of conflicting same-named types."""
    for name, entry in source.items():
        target.setdefault(name, entry)


def _closure_signatures(types: Typesdict, msgtype: str) -> dict[str, str]:
    """Return the canonical definition of every type reachable from msgtype."""
    signatures: dict[str, str] = {}
    pending = [msgtype]
    while pending:
        name = pending.pop()
        if name in signatures or name not in types:
            continue
        consts, fields = types[name]
        pending.extend(
            cast('str', base[1])
            for _, desc in fields
            if (base := desc if desc[0] in {Nodetype.BASE, Nodetype.NAME} else desc[1][0])[0]
            == Nodetype.NAME
        )
        signatures[name] = repr((consts, fields))
    return signatures


ReaderErrors = (ReaderError1, ReaderError2)


class AnyReader:
    """Unified rosbag1 and rosbag2 reader."""

    readers: Sequence[Reader1] | Sequence[Reader2]
    typestore: Typestore

    def __init__(
        self,
        paths: Sequence[RPath],
        *,
        default_typestore: Typestore | None = None,
    ) -> None:
        """Initialize RosbagReader.

        Opens one or multiple rosbag1 recordings or a single rosbag2 recording.

        Args:
            paths: Paths to multiple rosbag1 files or single rosbag2 directory.
            default_typestore: Typestore to deserialize messages if bag has
                no embedded message definions.

        Raises:
            AnyReaderError: If paths do not exist.

        """
        if not paths:
            msg = 'Must call with at least one path.'
            raise AnyReaderError(msg)

        if missing := [x for x in paths if not x.exists()]:
            msg = f'The following paths are missing: {missing!r}'
            raise FileNotFoundError(msg)

        self.paths = paths
        self.is2 = any(x.suffix != '.bag' for x in paths)
        self.stack: ExitStack | None = None
        self.connections: list[Connection] = []
        self.default_typestore = default_typestore
        self.typestore = get_typestore(Stores.EMPTY)
        self._connection_defs: dict[tuple[int, object], Typesdict] = {}
        self._connection_stores: dict[tuple[int, object], Typestore] = {}
        self._conflicts: set[str] = set()
        self._signatures: dict[str, set[str]] = {}
        self._roots: set[str] = set()
        self._rawdata: OrderedDict[
            int,
            tuple[bytes | memoryview, Connection],
        ] = OrderedDict()

        try:
            if self.is2:
                self.readers = [Reader2(x) for x in paths]
            else:
                self.readers = [Reader1(x) for x in paths]
        except ReaderErrors as err:
            raise AnyReaderError(*err.args) from None

    def _check_open(self) -> None:
        """Check if is open."""
        if not self.stack:
            msg = 'AnyReader is not open.'
            raise AnyReaderError(msg)

    def _deser_ros1(self, rawdata: bytes, typ: str) -> object:
        """Deserialize ROS1 message."""
        return self.typestore.deserialize_ros1(rawdata, typ)

    def _deser_ros2(self, rawdata: bytes, typ: str) -> object:
        """Deserialize CDR message."""
        return self.typestore.deserialize_cdr(rawdata, typ)

    def deserialize(
        self,
        rawdata: bytes,
        typ: str,
        connection: Connection | None = None,
    ) -> object:
        """Deserialize message with appropriate helper.

        Definitions are resolved against the delivering connection when one is
        given. This matters for bags whose connections share a type name but
        carry different definitions.

        Args:
            rawdata: Serialized message.
            typ: Message type name.
            connection: Connection that delivered the message.

        Returns:
            Deserialized message.

        Raises:
            AnyReaderError: The message cannot be deserialized.

        """
        try:
            if connection is not None:
                store = self._connection_store(connection)
                if store is not None:
                    return (
                        store.deserialize_cdr(rawdata, typ)
                        if self.is2
                        else store.deserialize_ros1(rawdata, typ)
                    )
            remembered = self._rawdata.get(id(rawdata))
            if remembered is not None and remembered[0] is rawdata:
                store = self._connection_store(remembered[1])
                if store is not None:
                    return (
                        store.deserialize_cdr(rawdata, typ)
                        if self.is2
                        else store.deserialize_ros1(rawdata, typ)
                    )
            if typ in self._conflicts:
                msg = f'Message type {typ!r} has conflicting definitions; {CONFLICT_HINT}.'
                raise AnyReaderError(msg)
            return self._deser_ros2(rawdata, typ) if self.is2 else self._deser_ros1(rawdata, typ)
        except SerdeError as err:
            raise AnyReaderError(*err.args) from None

    def _connection_store(self, connection: Connection) -> Typestore | None:
        """Return the store holding the definition of a connection."""
        key = (connection.id, connection.owner)
        if key in self._connection_stores:
            return self._connection_stores[key]

        types = self._connection_defs.get(key)
        if types is None:
            return None

        store = get_typestore(Stores.EMPTY)
        try:
            store.register(types)
        except TypesysError:
            return None
        self._connection_stores[key] = store
        return store

    def _open(self) -> None:
        """Open subreaders and populate typestore."""
        assert self.stack
        for reader in self.readers:
            self.stack.enter_context(reader)

        typs: Typesdict = {}
        connections = [y for x in self.readers for y in x.connections]
        if connections:
            definitions = [
                x for x in connections if x.msgdef.format != MessageDefinitionFormat.NONE
            ]
            if definitions:
                for connection in definitions:
                    if connection.msgdef.format == MessageDefinitionFormat.MSG:
                        current = get_types_from_msg(connection.msgdef.data, connection.msgtype)
                    else:
                        assert connection.msgdef.format == MessageDefinitionFormat.IDL
                        sep = '=' * 80 + '\n'
                        current = {}
                        if connection.msgdef.data.startswith(f'{sep}IDL: '):
                            for msgdef in connection.msgdef.data.split(sep)[1:]:
                                hdr, idl = msgdef.split('\n', 1)
                                assert hdr.startswith('IDL: ')
                                current.update(get_types_from_idl(idl))
                        else:
                            current.update(get_types_from_idl(connection.msgdef.data))

                    key = (connection.id, connection.owner)
                    self._connection_defs[key] = current
                    _merge_types(typs, current)
                    for name, signature in _closure_signatures(current, connection.msgtype).items():
                        self._signatures.setdefault(name, set()).add(signature)
                    self._roots.add(connection.msgtype)

                    if connection.digest:
                        self._check_digest(connection, current)
            elif self.default_typestore:
                typs.update(self.default_typestore.fielddefs)
            else:
                msg = (
                    'Bag contains no type definitions. '
                    'Instantiate AnyReader with a default_typestore argument.'
                )
                raise AnyReaderError(msg)
        typestore = get_typestore(Stores.EMPTY)
        typestore.register(typs)

        conflicting = {name for name, sigs in self._signatures.items() if len(sigs) > 1}
        self._conflicts = set(conflicting)
        for name in self._roots:
            if conflicts := _closure_names(name, self._connection_defs, conflicting):
                self._conflicts.update({name} | conflicts)
        self.connections = connections
        self.typestore = typestore

    def _check_digest(self, connection: Connection, types: Typesdict) -> None:
        """Validate a connection's definition against its digest."""
        if not connection.digest:
            return
        store = get_typestore(Stores.EMPTY)
        store.register(types)
        func = store.hash_rihs01 if self.is2 else lambda x: store.generate_msgdef(x)[1]
        if (have := func(connection.msgtype)) != connection.digest:
            msg = f'Message type {connection.msgtype!r} hash mismatch {have} != {connection.digest}'
            raise AnyReaderError(msg)

    def open(self) -> None:
        """Open reader."""
        if self.stack:
            msg = 'AnyReader is already open.'
            raise AnyReaderError(msg)

        self.stack = ExitStack()
        try:
            self._open()
        except BaseException as err:
            with suppress(Exception):
                self.close()
            if isinstance(err, (ReaderErrors, TypesysError)):
                raise AnyReaderError(*err.args) from None
            raise

    def close(self) -> None:
        """Close reader."""
        self._check_open()
        stack, self.stack = self.stack, None
        assert stack

        self.connections.clear()
        self._connection_defs.clear()
        self._connection_stores.clear()
        self._conflicts.clear()
        self._signatures.clear()
        self._roots.clear()
        self._rawdata.clear()
        self.typestore = get_typestore(Stores.EMPTY)
        stack.close()

    def __enter__(self) -> Self:
        """Open reader when entering contextmanager."""
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> Literal[False]:
        """Close reader when exiting contextmanager."""
        self.close()
        return False

    @property
    def duration(self) -> int:
        """Duration in nanoseconds between earliest and latest messages."""
        self._check_open()
        return self.end_time - self.start_time

    @property
    def start_time(self) -> int:
        """Timestamp in nanoseconds of the earliest message."""
        self._check_open()
        return min(x.start_time for x in self.readers)

    @property
    def end_time(self) -> int:
        """Timestamp in nanoseconds after the latest message."""
        self._check_open()
        return max(x.end_time for x in self.readers)

    @property
    def message_count(self) -> int:
        """Total message count."""
        self._check_open()
        return sum(x.message_count for x in self.readers)

    @property
    def topics(self) -> dict[str, TopicInfo]:
        """Topics stored in the rosbags."""
        self._check_open()

        def summarize(names_infos: Iterable[tuple[str, TopicInfo]]) -> TopicInfo:
            """Summarize topic infos."""
            infos = [x[1] for x in names_infos]
            return TopicInfo(
                msgtypes.pop() if len(msgtypes := {x.msgtype for x in infos}) == 1 else None,
                msgdefs.pop()
                if len(msgdefs := {x.msgdef for x in infos}) == 1
                else MessageDefinition(MessageDefinitionFormat.NONE, ''),
                sum(x.msgcount for x in infos),
                functools.reduce(operator.iadd, (x.connections for x in infos), []),
            )

        return {
            name: summarize(infos)
            for name, infos in groupby(
                sorted(
                    (x for reader in self.readers for x in reader.topics.items()),
                    key=lambda x: x[0],
                ),
                key=lambda x: x[0],
            )
        }

    def messages(
        self,
        connections: Collection[Connection] = (),
        start: int | None = None,
        stop: int | None = None,
    ) -> Generator[tuple[Connection, int, bytes], None, None]:
        """Read messages from bags.

        Args:
            connections: Collection with connections to filter for. An empty
                iterable disables filtering on connections.
            start: Yield only messages at or after this timestamp (ns).
            stop: Yield only messages before this timestamp (ns).

        Yields:
            Tuples of connection, timestamp (ns), and rawdata.

        """
        self._check_open()

        def get_owner(connection: Connection) -> Reader1 | Reader2:
            return cast('Reader1 | Reader2', connection.owner)

        if connections:
            generators = [
                reader.messages(connections=list(conns), start=start, stop=stop)
                for reader, conns in groupby(
                    sorted(connections, key=lambda x: id(get_owner(x))), key=get_owner
                )
            ]
        else:
            generators = [reader.messages(start=start, stop=stop) for reader in self.readers]
        for connection, timestamp, rawdata in merge(*generators, key=lambda x: x[1]):
            self._rawdata[id(rawdata)] = (rawdata, connection)
            while len(self._rawdata) > 64:
                self._rawdata.popitem(last=False)
            yield connection, timestamp, rawdata
