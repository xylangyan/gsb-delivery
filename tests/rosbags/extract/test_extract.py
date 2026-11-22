# Copyright 2020-2026 Ternaris
# SPDX-License-Identifier: Apache-2.0
"""Time window extraction tests."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

from rosbags.extract import MissingDefinitionError, extract
from rosbags.highlevel import AnyReader, AnyReaderError
from rosbags.interfaces import MessageDefinitionFormat
from rosbags.rosbag2 import Reader, Writer
from rosbags.typesys import Stores, get_types_from_msg, get_typestore

if TYPE_CHECKING:
    from pathlib import Path

    from rosbags.typesys.store import Typestore

STATUS = 'pkg/msg/Status'
WRAP = 'pkg/msg/Wrap'
STATE = 'pkg/msg/State'
COMMON = 'pkg/msg/Common'
INT32_STATE = 'int32 mode'
STRING_STATE = 'string mode'


def make_store(state: str) -> Typestore:
    """Create a store with two nesting levels above a State variant."""
    store = get_typestore(Stores.EMPTY)
    types = get_types_from_msg(f'{WRAP} wrap\n{COMMON} common\nstring note', STATUS)
    types.update(get_types_from_msg(f'{STATE} state\nint32 seq', WRAP))
    types.update(get_types_from_msg(state, STATE))
    types.update(get_types_from_msg('string tag', COMMON))
    store.register(types)
    return store


def build(path: Path, state: str, *, start: int = 0, count: int = 3) -> None:
    """Write a bag with status and two direct State connections."""
    store = make_store(state)
    statuscls = store.types[STATUS]
    wrapcls = store.types[WRAP]
    statecls = store.types[STATE]
    commoncls = store.types[COMMON]

    with Writer(path, version=9) as writer:
        status = writer.add_connection('/status', STATUS, typestore=store)
        left = writer.add_connection('/left', STATE, typestore=store)
        right = writer.add_connection('/right', STATE, typestore=store)
        for index in range(count):
            timestamp = start + index
            if state == INT32_STATE:
                stateval = statecls(index)
                leftmsg = statecls(index * 10)
                rightmsg = statecls(index * 100)
            else:
                stateval = statecls(f's{index}')
                leftmsg = statecls(f'l{index}')
                rightmsg = statecls(f'r{index}')
            msg = statuscls(wrapcls(stateval, index), commoncls('shared'), f's{index}')
            writer.write(status, timestamp, store.serialize_cdr(msg, STATUS))
            writer.write(left, timestamp + 10, store.serialize_cdr(leftmsg, STATE))
            writer.write(right, timestamp + 20, store.serialize_cdr(rightmsg, STATE))


@pytest.fixture
def bags(tmp_path: Path) -> tuple[Path, Path]:
    """Two bags whose State definition differs."""
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    build(first, INT32_STATE)
    build(second, STRING_STATE)
    return first, second


def reader_store(reader: Reader) -> Typestore:
    """Create a store from all definitions in an open bag."""
    store = get_typestore(Stores.EMPTY)
    for connection in reader.connections:
        assert connection.msgdef.format != MessageDefinitionFormat.NONE
        store.register(get_types_from_msg(connection.msgdef.data, connection.msgtype))
    return store


def test_extract_keeps_numeric_and_text_variants(tmp_path: Path, bags: tuple[Path, Path]) -> None:
    """Both State variants remain readable under their own connections."""
    first, second = bags
    dst = tmp_path / 'dst'
    result = extract([first, second], dst, start=0, stop=100)

    assert result.written == 6
    assert result.message_count == 18
    assert sorted(result.renamed) == [STATE, STATUS]

    with Reader(dst) as reader:
        store = reader_store(reader)
        seen: dict[tuple[str, str], list[object]] = {}
        for connection, _, data in reader.messages():
            msg = store.deserialize_cdr(data, connection.msgtype)
            value = msg.wrap.state.mode if connection.topic == '/status' else msg.mode
            seen.setdefault((connection.topic, connection.msgtype), []).append(value)

        assert 0 in seen[('/status', STATUS)]
        assert 's0' in seen[('/status', f'{STATUS}_1')]
        assert 0 in seen[('/left', STATE)]
        assert 'l0' in seen[('/left', f'{STATE}_1')]


def test_same_source_connections_share_variant(tmp_path: Path, bags: tuple[Path, Path]) -> None:
    """Connections from one bag keep the same State variant."""
    first, second = bags
    dst = tmp_path / 'dst'
    _ = extract([first, second], dst, start=0, stop=100)

    with Reader(dst) as reader:
        by_topic = {}
        for connection in reader.connections:
            by_topic.setdefault(connection.topic, set()).add(connection.msgtype)
        assert by_topic['/left'] == {STATE, f'{STATE}_1'}
        assert by_topic['/right'] == {STATE, f'{STATE}_1'}

        # Both direct State connections of one source use the same variant.
        left = [x for x in reader.connections if x.topic == '/left']
        right = [x for x in reader.connections if x.topic == '/right']
        assert left[0].msgtype == right[0].msgtype
        assert left[1].msgtype == right[1].msgtype


def test_identical_nested_types_are_emitted_once(
    tmp_path: Path,
    bags: tuple[Path, Path],
) -> None:
    """Identical nested definitions are shared across both variants."""
    first, second = bags
    dst = tmp_path / 'dst'
    _ = extract([first, second], dst, start=0, stop=100)

    definitions: dict[str, object] = {}
    with Reader(dst) as reader:
        for connection in reader.connections:
            types = get_types_from_msg(connection.msgdef.data, connection.msgtype)
            for name, entry in types.items():
                if name == COMMON:
                    definitions.setdefault(name, entry)
                else:
                    assert name not in definitions or definitions[name] == entry
    assert list(definitions) == [COMMON]
    assert f'{COMMON}_1' not in definitions


def test_extract_only_selected_connection(tmp_path: Path, bags: tuple[Path, Path]) -> None:
    """Only the selected connection's messages are copied."""
    first, second = bags
    with Reader(first) as reader:
        selected = [x for x in reader.connections if x.topic == '/status']

    dst = tmp_path / 'dst'
    result = extract([first, second], dst, connections=selected, start=0, stop=100)
    assert result.written == 1
    assert result.message_count == 3

    with Reader(dst) as reader:
        assert [x.topic for x in reader.connections] == ['/status']
        assert len(list(reader.messages())) == 3


def test_resolution_follows_connection_not_first_loaded(tmp_path: Path) -> None:
    """Loading the other declaration first cannot change interpretation."""
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    build(first, INT32_STATE)
    build(second, STRING_STATE)

    with AnyReader([first]) as reader:
        first_types = get_types_from_msg(
            reader.connections[0].msgdef.data,
            reader.connections[0].msgtype,
        )
        assert first_types[STATE][1][0][1][1][0] == 'int32'

    with AnyReader([first, second]) as reader:
        seen: dict[str, list[object]] = {}
        for connection, _, data in reader.messages():
            msg = reader.deserialize(data, connection.msgtype, connection)
            value = msg.wrap.state.mode if connection.topic == '/status' else msg.mode
            seen.setdefault(connection.topic, []).append(value)
        assert 0 in seen['/status']
        assert 's0' in seen['/status']
        assert 0 in seen['/left']
        assert 'l0' in seen['/left']


def test_anyreader_rejects_ambiguous_implicit_lookup(tmp_path: Path) -> None:
    """Implicit lookup by conflicting type name is rejected."""
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    build(first, INT32_STATE)
    build(second, STRING_STATE)

    with Reader(second) as rawreader, AnyReader([first, second]) as reader:
        connection, _, data = next(rawreader.messages())
        with pytest.raises(AnyReaderError, match='pass the delivering connection'):
            _ = reader.deserialize(data, connection.msgtype)


def test_extract_window(tmp_path: Path, bags: tuple[Path, Path]) -> None:
    """The time window is inclusive of start and exclusive of stop."""
    first, second = bags
    dst = tmp_path / 'dst'
    result = extract([first, second], dst, start=1, stop=2)
    assert result.message_count == 2

    with Reader(dst) as reader:
        assert [t for _, t, _ in reader.messages()] == [1, 1]


def test_missing_definition_reported(tmp_path: Path, bags: tuple[Path, Path]) -> None:
    """Connections without a State declaration are reported, not guessed."""
    first, second = bags
    strip_state(second)

    dst = tmp_path / 'dst'
    result = extract([first, second], dst, start=0, stop=100)
    assert sorted((x.topic, x.msgtype) for x in result.missing) == [
        ('/left', STATE),
        ('/right', STATE),
        ('/status', STATUS),
    ]
    assert result.written == 3

    with Reader(dst) as reader:
        assert {x.topic for x in reader.connections} == {'/left', '/right', '/status'}


def test_missing_nested_declaration_only_hits_that_connection(
    tmp_path: Path,
    bags: tuple[Path, Path],
) -> None:
    """One broken connection does not affect valid connections of the same bag."""
    first, second = bags
    truncate(tmp_path, second, STATUS, f'{STATE} state\nstring note\n')

    dst = tmp_path / 'dst'
    result = extract([first, second], dst, start=0, stop=100)
    assert [(x.topic, x.msgtype) for x in result.missing] == [('/status', STATUS)]
    assert result.written == 5

    with Reader(dst) as reader:
        assert sorted(x.topic for x in reader.connections) == [
            '/left',
            '/left',
            '/right',
            '/right',
            '/status',
        ]


def test_missing_nested_declaration_accepts_matching_fallback(
    tmp_path: Path,
    bags: tuple[Path, Path],
) -> None:
    """A fallback restores only the connection with the matching digest."""
    first, second = bags
    truncate(tmp_path, second, STATUS, f'{STATE} state\nstring note\n')

    dst = tmp_path / 'dst'
    result = extract(
        [first, second],
        dst,
        start=0,
        stop=100,
        fallback=make_store(STRING_STATE),
    )
    assert not result.missing
    assert result.written == 6


def test_missing_definition_can_be_supplied(tmp_path: Path, bags: tuple[Path, Path]) -> None:
    """A fallback matching each connection digest restores the connections."""
    first, second = bags
    strip_state(second)

    dst = tmp_path / 'dst'
    fallback = make_store(STRING_STATE)
    result = extract([first, second], dst, start=0, stop=100, fallback=fallback)
    assert not result.missing
    assert result.written == 6


def test_mismatched_fallback_is_rejected(tmp_path: Path, bags: tuple[Path, Path]) -> None:
    """A fallback with another State definition is never borrowed."""
    first, second = bags
    strip_state(second)

    dst = tmp_path / 'dst'
    fallback = make_store(INT32_STATE)
    with pytest.raises(MissingDefinitionError) as caught:
        extract([first, second], dst, start=0, stop=100, fallback=fallback, include_missing=False)
    assert sorted(x.topic for x in caught.value.connections) == ['/left', '/right', '/status']


def truncate(tmp_path: Path, path: Path, msgtype: str, body: str) -> None:
    """Replace one connection definition body while keeping its digest."""
    del tmp_path
    db = next(path.glob('*.db3'))
    connection = sqlite3.connect(db)
    try:
        connection.execute(
            'UPDATE message_definitions SET encoded_message_definition = ? WHERE topic_type = ?',
            (body, msgtype),
        )
        connection.commit()
    finally:
        connection.close()


def strip_state(path: Path) -> None:
    """Remove the State declaration from a bag's type definitions."""
    db = next(path.glob('*.db3'))
    connection = sqlite3.connect(db)
    try:
        connection.execute('DELETE FROM message_definitions WHERE topic_type = ?', (STATE,))
        for msgtype, body in (
            (STATUS, f'{WRAP} wrap\n{COMMON} common\nstring note\n'),
            (WRAP, f'{STATE} state\nint32 seq\n'),
        ):
            connection.execute(
                'UPDATE message_definitions SET encoded_message_definition = ? '
                'WHERE topic_type = ?',
                (body, msgtype),
            )
        connection.commit()
    finally:
        connection.close()
