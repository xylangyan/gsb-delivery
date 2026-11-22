# Copyright 2020-2026 Ternaris
# SPDX-License-Identifier: Apache-2.0
"""Extract a time window from one or more rosbag2 recordings."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from heapq import merge
from typing import TYPE_CHECKING

from rosbags.interfaces import MessageDefinitionFormat
from rosbags.rosbag2 import Reader, StoragePlugin, Writer

from .definitions import (
    DefinitionRegistry,
    MissingDefinitionError,
    definition_for,
    parse_definition,
    resolve_from_candidates,
)

if TYPE_CHECKING:
    from pathlib import Path

    from rosbags.interfaces import Connection, Qos
    from rosbags.interfaces.typing import Typesdict
    from rosbags.typesys.store import Typestore

    Fallback = (
        Typestore | Mapping[str, object] | Sequence[Typestore | Mapping[str, object] | str] | str
    )


class ExtractError(Exception):
    """Extraction failed."""


@dataclass
class Origin:
    """Provenance of a written connection."""

    source: str
    connection: int
    topic: str
    msgtype: str
    output: int


@dataclass
class ExtractResult:
    """Result of an extraction run."""

    connections: list[Connection] = field(default_factory=list)
    renamed: dict[str, list[str]] = field(default_factory=dict)
    missing: list[Connection] = field(default_factory=list)
    origins: list[Origin] = field(default_factory=list)
    renames: dict[str, str] = field(default_factory=dict)
    message_count: int = 0

    @property
    def written(self) -> int:
        """Number of written connections."""
        return len(self.connections)


@dataclass
class _Resolved:
    connection: Connection
    source: str
    types: Typesdict
    root_key: str = ''


def extract(
    srcs: Sequence[Path],
    dst: Path,
    *,
    start: int | None = None,
    stop: int | None = None,
    start_time: int | None = None,
    stop_time: int | None = None,
    connections: Sequence[Connection] | None = None,
    topics: Sequence[str] | None = None,
    fallback: Fallback | None = None,
    include_missing: bool = True,
    scope: str | None = None,
    storage: str = 'sqlite3',
) -> ExtractResult:
    """Extract a time window from rosbag2 recordings.

    The destination is a regular rosbag2 recording that existing readers can
    reopen. Payloads are copied byte for byte; only type names and definitions
    are adjusted where same-named types differ between sources. Every
    connection is resolved against its own definition, never against a
    definition that merely shares its type name.

    Connections from one source that use the same type definition share their
    output variant, as do connections from different sources whose referenced
    definitions are identical. Other types are emitted once.

    Args:
        srcs: Rosbag2 directories.
        dst: Destination directory.
        start: First timestamp to include (inclusive, ns).
        stop: First timestamp to exclude (exclusive, ns).
        start_time: Alias for start.
        stop_time: Alias for stop.
        connections: Connections to copy. Defaults to all connections.
        topics: Only copy connections on these topics.
        fallback: Definition source consulted for connections whose embedded
            definition is incomplete. Accepts a typestore, a mapping of type
            names to definition text or type descriptors, definition text, or
            a sequence of these. A candidate is only accepted when its digest
            matches the connection.
        include_missing: Write valid connections and report missing ones
            instead of raising before writing. Invalid connections are never
            written using another connection's definition.
        scope: Package (or package/msg) prefix for conflicting type variants.
            The first variant of a name keeps the original type name.
        storage: Destination storage plugin, ``sqlite3`` or ``mcap``.

    Returns:
        Extraction result with written connections and conflicts.

    Raises:
        ExtractError: No connections were selected.
        MissingDefinitionError: Definitions are missing and include_missing is
            False.

    """
    if start is None:
        start = start_time
    if stop is None:
        stop = stop_time

    readers: list[Reader] = []
    try:
        for src in srcs:
            reader = Reader(src)
            reader.open()
            readers.append(reader)

        available = [x for reader in readers for x in reader.connections]
        wanted = list(connections) if connections is not None else available
        if topics:
            wanted = [x for x in wanted if x.topic in topics]
        if not wanted:
            msg = 'No connections selected for extraction.'
            raise ExtractError(msg)

        index_of = {id(reader.storage): index for index, reader in enumerate(readers)}
        source_of = list(srcs)
        available_by_key = {_connection_key(x): x for x in available}

        resolved: list[_Resolved] = []
        missing: list[Connection] = []
        candidates = _candidates(fallback)
        for requested in wanted:
            connection = available_by_key.get(_connection_key(requested), requested)
            owner = index_of.get(id(connection.owner))
            if owner is None:
                missing.append(requested)
                continue
            try:
                types, _ = resolve_from_candidates(connection, candidates)
            except MissingDefinitionError:
                missing.append(connection)
                continue
            resolved.append(
                _Resolved(connection, str(source_of[owner]), types),
            )

        if missing and not include_missing:
            raise MissingDefinitionError(missing)
        if not resolved:
            raise MissingDefinitionError(missing)

        registry = DefinitionRegistry()
        for item in resolved:
            item.root_key = registry.add(item.types, item.connection.msgtype)
        registry.assign_names(scope)
        store = registry.store()
        renames: dict[str, str] = {}
        for name, keys in registry.keys_by_name.items():
            for key in keys:
                output = registry.nodes[key].output
                if output != name:
                    renames[output] = name

        result = ExtractResult(missing=missing, renames=renames)
        connmap: dict[int, Connection] = {}
        seen: dict[tuple[str, str, object], Connection] = {}

        plugin = StoragePlugin.MCAP if storage == 'mcap' else StoragePlugin.SQLITE3
        with Writer(dst, version=9, storage_plugin=plugin) as writer:
            for item in resolved:
                msgtype = registry.nodes[item.root_key].output
                msgdef = definition_for(store, msgtype)
                key = (item.connection.topic, msgtype, repr(_ext(item.connection)))
                target = seen.get(key)
                if target is None:
                    target = writer.add_connection(
                        item.connection.topic,
                        msgtype,
                        msgdef=msgdef,
                        rihs01=store.hash_rihs01(msgtype),
                        serialization_format='cdr',
                        offered_qos_profiles=_qos(item.connection),
                    )
                    seen[key] = target
                    result.connections.append(target)
                    connmap[id(item.connection)] = target
                else:
                    connmap[id(item.connection)] = target

                if msgtype != item.connection.msgtype:
                    names = result.renamed.setdefault(item.connection.msgtype, [])
                    if msgtype not in names:
                        names.append(msgtype)
                result.origins.append(
                    Origin(
                        item.source,
                        item.connection.id,
                        item.connection.topic,
                        item.connection.msgtype,
                        target.id,
                    ),
                )

            writer.set_custom_data(
                'rosbags.extract.origins',
                json.dumps(
                    [
                        {
                            'source': x.source,
                            'connection': x.connection,
                            'topic': x.topic,
                            'type': x.msgtype,
                            'output': x.output,
                        }
                        for x in result.origins
                    ],
                    separators=(',', ':'),
                ),
            )

            groups = []
            for reader in readers:
                selected = [
                    x
                    for x in reader.connections
                    if id(x) in connmap and connmap[id(x)].topic == x.topic
                ]
                if selected:
                    groups.append(
                        reader.messages(connections=selected, start=start, stop=stop),
                    )

            for connection, timestamp, data in merge(*groups, key=lambda x: x[1]):
                writer.write(connmap[id(connection)], timestamp, data)
                result.message_count += 1
    finally:
        for reader in readers:
            reader.close()

    return result


def _candidates(fallback: object) -> list[dict[str, object]]:
    """Normalize fallback definitions into candidate type dictionaries."""
    if fallback is None:
        return []
    items = (
        list(fallback)
        if isinstance(fallback, Sequence) and not isinstance(fallback, str)
        else [fallback]
    )
    result: list[dict[str, object]] = []
    for item in items:
        if isinstance(item, Mapping):
            result.append(_mapping_types(item))
        elif isinstance(item, str):
            result.append(
                parse_definition(MessageDefinitionFormat.MSG, item, '__fallback__'),
            )
        elif fielddefs := getattr(item, 'fielddefs', None):
            result.append(
                {
                    name: (list(consts), list(fields))
                    for name, (consts, fields) in fielddefs.items()
                },
            )
        else:
            msg = f'Unsupported fallback definition {item!r}.'
            raise ExtractError(msg)
    return result


def _mapping_types(mapping: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in mapping.items():
        if isinstance(value, str):
            result.update(parse_definition(MessageDefinitionFormat.MSG, value, name))
        else:
            result[name] = value
    return result


def _connection_key(connection: Connection) -> tuple[object, ...]:
    return (
        connection.topic,
        connection.msgtype,
        connection.digest,
        connection.msgdef.format,
        connection.msgdef.data,
    )


def _ext(connection: Connection) -> object:
    from rosbags.interfaces import ConnectionExtRosbag2  # noqa: PLC0415

    return ConnectionExtRosbag2('cdr', list(_qos(connection)))


def _qos(connection: Connection) -> Sequence[Qos]:
    profiles = getattr(connection.ext, 'offered_qos_profiles', None)
    return profiles or []
