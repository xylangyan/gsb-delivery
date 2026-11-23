# Copyright 2016 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# A copy of the License is located at:
#
#    http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

from io import BytesIO

import pytest

from amazon.ion.core import IonEvent, IonEventType, IonType, ION_STREAM_END_EVENT
from amazon.ion.reader import NEXT_EVENT, blocking_reader
from amazon.ion.reader_binary import binary_reader
from amazon.ion.reader_managed import managed_reader
from amazon.ion.symbols import (
    ImportLocation,
    SymbolTableCatalog,
    SymbolToken,
    local_symbol_table,
    shared_symbol_table,
)
from amazon.ion.volumes import (
    IVM,
    IonVolumeError,
    audit_volumes,
    plan_volumes,
    read_values,
    split_ion_volumes,
    split_volumes,
)
from amazon.ion.writer import blocking_writer
from amazon.ion.writer_binary_raw import _raw_binary_writer
from amazon.ion.writer_buffer import BufferTree

_E = IonEvent
_ET = IonEventType
_IT = IonType


def _tok(sid):
    return SymbolToken(None, sid)


def _build(events):
    """Builds a binary Ion stream from raw events (no symbol interning)."""
    out = BytesIO()
    writer = blocking_writer(_raw_binary_writer(BufferTree()), out)
    for event in events:
        if event.event_type is IonEventType.VERSION_MARKER:
            writer.send(ION_STREAM_END_EVENT)
            out.write(IVM)
            continue
        writer.send(event)
    writer.send(ION_STREAM_END_EVENT)
    return IVM + out.getvalue()


def _lst_symbols(symbols):
    events = [_E(_ET.CONTAINER_START, _IT.STRUCT, annotations=[_tok(3)]),
              _E(_ET.CONTAINER_START, _IT.LIST, field_name=_tok(7))]
    events.extend(_E(_ET.SCALAR, _IT.STRING, symbol) for symbol in symbols)
    events.extend([_E(_ET.CONTAINER_END), _E(_ET.CONTAINER_END)])
    return events


def _lst_imports(imports, symbols=None):
    events = [_E(_ET.CONTAINER_START, _IT.STRUCT, annotations=[_tok(3)]),
              _E(_ET.CONTAINER_START, _IT.LIST, field_name=_tok(6))]
    for name, version, max_id in imports:
        events.extend([
            _E(_ET.CONTAINER_START, _IT.STRUCT),
            _E(_ET.SCALAR, _IT.STRING, name, field_name=_tok(4)),
            _E(_ET.SCALAR, _IT.INT, version, field_name=_tok(5)),
        ])
        if max_id is not None:
            events.append(_E(_ET.SCALAR, _IT.INT, max_id, field_name=_tok(8)))
        events.append(_E(_ET.CONTAINER_END))
    events.append(_E(_ET.CONTAINER_END))
    if symbols:
        events.append(_E(_ET.CONTAINER_START, _IT.LIST, field_name=_tok(7)))
        events.extend(_E(_ET.SCALAR, _IT.STRING, symbol) for symbol in symbols)
        events.append(_E(_ET.CONTAINER_END))
    events.append(_E(_ET.CONTAINER_END))
    return events


def _catalog(*tables):
    catalog = SymbolTableCatalog()
    for table in tables:
        catalog.register(table)
    return catalog


def _events(data, catalog=None):
    reader = blocking_reader(managed_reader(binary_reader(), catalog), BytesIO(data))
    out = []
    event = reader.send(NEXT_EVENT)
    while event.event_type is not IonEventType.STREAM_END:
        out.append(event)
        event = reader.send(NEXT_EVENT)
    return out


def test_basic_split_preserves_raw_values_and_order():
    events = [
        _E(_ET.SCALAR, _IT.INT, 1, field_name=_tok(4)),
        _E(_ET.SCALAR, _IT.STRING, 'two', annotations=[_tok(5)]),
        _E(_ET.SCALAR, _IT.SYMBOL, _tok(4)),
    ]
    source = _build(events)
    values = read_values(source)
    assert [value.raw for value in values] == [
        _build([events[0]])[4:],
        _build([events[1]])[4:],
        _build([events[2]])[4:],
    ]

    volumes = split_volumes(source)
    assert len(volumes) == 1
    assert values[0].raw in volumes[0]

    report = audit_volumes(source, volumes)
    assert report.ok
    assert report.minimal
    assert report.actual_opening_bytes == report.minimal_opening_bytes


def test_duplicate_fields_are_not_deduplicated_by_a_dict():
    source = _build([
        _E(_ET.CONTAINER_START, _IT.STRUCT),
        _E(_ET.SCALAR, _IT.INT, 1, field_name=_tok(4)),
        _E(_ET.SCALAR, _IT.INT, 2, field_name=_tok(4)),
        _E(_ET.SCALAR, _IT.INT, 3, field_name=_tok(4)),
        _E(_ET.CONTAINER_END),
    ])
    volumes = split_volumes(source)
    report = audit_volumes(source, volumes)
    assert report.ok
    assert volumes[0].count(b'\x84\x21\x01') == 1
    assert b'\x84\x21\x02' in volumes[0]
    assert b'\x84\x21\x03' in volumes[0]


def test_fields_annotations_and_symbol_values_keep_sid_meaning():
    source = _build([
        _E(_ET.CONTAINER_START, _IT.STRUCT, annotations=[_tok(3)]),
        _E(_ET.CONTAINER_START, _IT.LIST, field_name=_tok(7)),
        _E(_ET.SCALAR, _IT.STRING, 'field'),
        _E(_ET.SCALAR, _IT.STRING, 'anno'),
        _E(_ET.CONTAINER_END),
        _E(_ET.CONTAINER_END),
        _E(_ET.CONTAINER_START, _IT.STRUCT),
        _E(_ET.SCALAR, _IT.SYMBOL, _tok(10), field_name=_tok(11),
           annotations=[_tok(11), _tok(10)]),
        _E(_ET.CONTAINER_END),
    ])
    volumes = split_volumes(source)
    assert len(volumes) == 1
    assert audit_volumes(source, volumes).ok
    event = _events(volumes[0])[-2]
    assert event.field_name.text == 'anno'
    assert [a.text for a in event.annotations] == ['anno', 'field']
    assert event.value.text == 'field'


def test_append_local_symbol_table_changes_the_meaning_of_sid_12():
    source = _build(
        _lst_symbols(['a', 'b']) + [
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(10)),
            _E(_ET.CONTAINER_START, _IT.STRUCT, annotations=[_tok(3)]),
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(3), field_name=_tok(6)),
            _E(_ET.CONTAINER_START, _IT.LIST, field_name=_tok(7)),
            _E(_ET.SCALAR, _IT.STRING, 'c'),
            _E(_ET.SCALAR, _IT.STRING, 'd'),
            _E(_ET.CONTAINER_END),
            _E(_ET.CONTAINER_END),
            _E(_ET.SCALAR, _IT.INT, 7),
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(12)),
        ]
    )
    values = read_values(source)
    assert [need.text for need in values[0].needs] == ['a']
    assert [need.text for need in values[2].needs] == ['c']

    plan = plan_volumes(source, max_opening_bytes=14)
    assert plan.ok, plan.problems
    assert [volume.value_indices for volume in plan.volumes] == [(0, 1), (2,)]
    report = audit_volumes(source, plan.volumes, max_opening_bytes=14)
    assert report.ok
    symbols = [event.value.text for event in _events(plan.volumes[0])
               if event.event_type is IonEventType.SCALAR and event.ion_type is IonType.SYMBOL]
    assert symbols == ['a']
    symbols = [event.value.text for event in _events(plan.volumes[1])
               if event.event_type is IonEventType.SCALAR and event.ion_type is IonType.SYMBOL]
    assert symbols == ['c']


def test_ivm_resets_and_a_later_local_table_can_redefine_sid_12():
    source = _build(
        _lst_symbols(['a', 'b']) + [
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(11)),
            _E(_ET.VERSION_MARKER),
        ]
        + _lst_symbols(['x', 'y']) + [
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(11)),
        ]
    )
    values = read_values(source)
    assert [need.text for need in values[0].needs] == ['b']
    assert [need.text for need in values[1].needs] == ['y']
    volumes = split_volumes(source)
    report = audit_volumes(source, volumes)
    assert report.ok
    symbols = [event.value.text for volume in volumes for event in _events(volume)
               if event.event_type is IonEventType.SCALAR and event.ion_type is IonType.SYMBOL]
    assert symbols == ['b', 'y']


def test_unknown_shared_import_keeps_same_unknown_identity():
    source = _build(_lst_imports([('unknown', 1, 5)]) + [
        _E(_ET.SCALAR, _IT.SYMBOL, _tok(12)),
    ])
    catalog = SymbolTableCatalog()
    values = read_values(source, catalog)
    assert values[0].needs[0].text is None
    assert values[0].needs[0].location == ImportLocation('unknown', 3)

    volumes = split_volumes(source, catalog=catalog)
    report = audit_volumes(source, volumes, catalog=catalog)
    assert report.ok
    reopened = read_values(volumes[0], catalog)[0]
    assert reopened.needs[0].text is None
    assert reopened.needs[0].location == ImportLocation('unknown', 3)


def test_substitute_text_is_not_treated_as_recovered_provenance():
    source = _build(_lst_imports([('foo', 2, 4)]) + [
        _E(_ET.SCALAR, _IT.SYMBOL, _tok(12)),
    ])
    # Only version 1 is known; its text must not be applied to version 2.
    catalog = _catalog(shared_symbol_table('foo', 1, ['a', 'b', 'c', 'd']))
    values = read_values(source, catalog)
    assert values[0].needs[0].text is None
    assert values[0].needs[0].location == ImportLocation('foo', 3)

    volumes = split_volumes(source, catalog=catalog)
    report = audit_volumes(source, volumes, catalog=catalog)
    assert report.ok

    exact = _catalog(shared_symbol_table('foo', 2, ['w', 'x', 'y', 'z']))
    values = read_values(source, exact)
    assert values[0].needs[0].text == 'y'


def test_same_name_different_versions_are_distinct_import_identities():
    source = _build(
        _lst_imports([('foo', 1, 2)]) + [_E(_ET.SCALAR, _IT.SYMBOL, _tok(10))]
        + _lst_imports([('foo', 2, 2)]) + [_E(_ET.SCALAR, _IT.SYMBOL, _tok(10))]
    )
    catalog = _catalog(
        shared_symbol_table('foo', 1, ['a', 'b']),
        shared_symbol_table('foo', 2, ['x', 'y']),
    )
    values = read_values(source, catalog)
    assert [(need.text, need.version) for need in values[0].needs] == [('a', 1)]
    assert [(need.text, need.version) for need in values[1].needs] == [('x', 2)]

    plan = plan_volumes(source, catalog=catalog)
    assert plan.ok
    assert [volume.value_indices for volume in plan.volumes] == [(0,), (1,)]
    assert audit_volumes(source, plan.volumes, catalog=catalog).ok

    plan = plan_volumes(source, catalog=catalog, groups=[[0, 1]])
    assert not plan.ok
    assert any(problem.kind == 'group_conflict' for problem in plan.problems)


def test_undefined_sid_is_not_movable():
    source = _build(_lst_symbols(['only']) + [
        _E(_ET.SCALAR, _IT.SYMBOL, _tok(11)),
    ])
    plan = plan_volumes(source)
    assert not plan.ok
    assert plan.problems[0].kind == 'unresolved_symbol'
    assert plan.problems[0].value_indices == (0,)
    with pytest.raises(IonVolumeError):
        split_volumes(source)


def test_symbol_conflict_forces_separate_volumes_and_group_reports_conflict():
    source = _build(
        _lst_symbols(['a']) + [_E(_ET.SCALAR, _IT.SYMBOL, _tok(10))]
        + _lst_symbols(['b']) + [_E(_ET.SCALAR, _IT.SYMBOL, _tok(10))]
    )
    plan = plan_volumes(source)
    assert plan.ok
    assert [volume.value_indices for volume in plan.volumes] == [(0,), (1,)]
    assert audit_volumes(source, plan.volumes).ok

    plan = plan_volumes(source, groups=[[0, 1]])
    assert not plan.ok
    assert any(problem.kind == 'group_conflict' for problem in plan.problems)


def test_group_keeps_non_adjacent_values_together_and_names_the_budget():
    source = _build(
        _lst_symbols(['a', 'b']) + [
            _E(_ET.SCALAR, _IT.INT, 1),
            _E(_ET.SCALAR, _IT.INT, 2),
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(10)),
        ]
    )
    plan = plan_volumes(source, groups=[[0, 2]], max_opening_bytes=13)
    assert plan.ok, plan.problems
    assert [volume.value_indices for volume in plan.volumes] == [(0, 1, 2)]
    report = audit_volumes(source, plan.volumes, groups=[[0, 2]], max_opening_bytes=13)
    assert report.ok
    assert report.group_results[0]['ok']

    plan = plan_volumes(source, groups=[[0, 2]], max_opening_bytes=11)
    assert not plan.ok
    assert any(problem.kind == 'group_budget' for problem in plan.problems)


def test_opening_budget_splits_and_audit_checks_minimum():
    source = _build(
        _lst_symbols(['a', 'b', 'c']) + [
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(10)),
            _E(_ET.SCALAR, _IT.INT, 2),
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(12)),
        ]
    )
    # The three values together need a 15-byte opening; {0,1} needs 12 and
    # {2} needs 14, so a 14-byte budget forces a split.
    plan = plan_volumes(source, max_opening_bytes=14)
    assert plan.ok
    assert [volume.value_indices for volume in plan.volumes] == [(0, 1), (2,)]
    assert [volume.opening_bytes for volume in plan.volumes] == [12, 14]
    report = audit_volumes(source, plan.volumes, max_opening_bytes=14)
    assert report.ok and report.minimal


def test_audit_detects_byte_changes_swaps_and_redundant_preambles():
    source = _build(
        _lst_symbols(['a', 'b']) + [
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(10)),
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(11)),
        ]
    )
    plan = plan_volumes(source, max_opening_bytes=13)
    assert plan.ok
    first = bytes(plan.volumes[0])
    second = bytes(plan.volumes[1])

    corrupted = bytearray(first)
    corrupted[-1] ^= 0xff
    report = audit_volumes(source, [bytes(corrupted), second], max_opening_bytes=13)
    assert not report.ok
    assert not report.values[0].raw_equal

    report = audit_volumes(source, [second, first], max_opening_bytes=13)
    assert not report.ok

    # Both values reopened under one full symbol table: readable but not cheapest.
    combined = plan_volumes(source).volumes[0]
    report = audit_volumes(source, [combined], max_opening_bytes=13)
    assert not report.ok
    assert any('opening' in problem for problem in report.problems)


def test_shared_tables_must_be_shared_and_may_fill_referenced_slots():
    foo = shared_symbol_table('foo', 1, ['a', 'b'])
    source = _build(_lst_imports([('foo', 1, 2)]) + [
        _E(_ET.SCALAR, _IT.SYMBOL, _tok(11)),
    ])
    catalog = _catalog(foo)
    volumes = split_volumes(source, catalog=catalog, shared_tables=[foo])
    assert audit_volumes(source, volumes, catalog=catalog, shared_tables=[foo]).ok

    with pytest.raises(IonVolumeError):
        split_volumes(source, catalog=catalog,
                      shared_tables=[local_symbol_table()])


def test_group_failure_indices_are_reported():
    source = _build(
        _lst_symbols(['a']) + [_E(_ET.SCALAR, _IT.SYMBOL, _tok(10))]
        + _lst_symbols(['b']) + [_E(_ET.SCALAR, _IT.SYMBOL, _tok(10))]
        + [_E(_ET.SCALAR, _IT.INT, 1)]
    )
    plan = plan_volumes(source, groups=[[0, 1]])
    assert not plan.ok
    assert plan.group_failures == (0,)
    assert plan.problems[0].group_index == 0


def test_zero_values_and_alias_entry_points():
    empty = IVM
    plan = plan_volumes(empty)
    assert plan.ok and plan.volumes == []

    source = _build([_E(_ET.SCALAR, _IT.INT, 1)])
    assert split_ion_volumes(source)[0] == split_volumes(source)[0]


def test_nop_padding_is_not_a_value_and_is_dropped():
    # IVM, extended NOP pad of 5 bytes, then int 1.
    source = bytes.fromhex('e00100ea0e8500000000002101')
    values = read_values(source)
    assert [value.raw for value in values] == [b'!\x01']
    volumes = split_volumes(source)
    assert bytes(volumes[0]) == IVM + b'!\x01'
    assert audit_volumes(source, volumes).ok


def test_nop_padding_inside_a_struct_has_no_field_name():
    # LST with symbols ['a'], then {<nop pad> a: 1}.
    lst_inner = bytes.fromhex('87b28161')
    lst_struct = bytes([0xD0 | len(lst_inner)]) + lst_inner
    wrap_body = bytes.fromhex('8183') + lst_struct
    struct_body = b'\x00' + bytes.fromhex('8a') + bytes.fromhex('2101')
    source = (IVM + bytes([0xE0 | len(wrap_body)]) + wrap_body
              + bytes([0xD0 | len(struct_body)]) + struct_body)
    values = read_values(source)
    assert [need.text for need in values[0].needs] == ['a']
    volumes = split_volumes(source)
    assert audit_volumes(source, volumes).ok


def test_non_binary_input_is_rejected():
    with pytest.raises(IonVolumeError):
        read_values(b'1 2 3')
    plan = plan_volumes(b'1 2 3')
    assert not plan.ok
    assert plan.problems[0].kind == 'invalid_stream'


def test_null_annotated_struct_is_a_value_not_a_symbol_table():
    source = _build([
        _E(_ET.SCALAR, _IT.NULL, None, annotations=[_tok(3)]),
        _E(_ET.SCALAR, _IT.INT, 1),
    ])
    values = read_values(source)
    assert len(values) == 2
    volumes = split_volumes(source)
    assert [volume.value_indices for volume in volumes] == [(0, 1)]
    assert audit_volumes(source, volumes).ok


def test_annotated_symbols_list_is_still_a_local_table():
    source = _build([
        _E(_ET.CONTAINER_START, _IT.STRUCT, annotations=[_tok(3)]),
        _E(_ET.CONTAINER_START, _IT.LIST, field_name=_tok(7), annotations=[_tok(4)]),
        _E(_ET.SCALAR, _IT.STRING, 'x'),
        _E(_ET.CONTAINER_END),
        _E(_ET.CONTAINER_END),
        _E(_ET.SCALAR, _IT.SYMBOL, _tok(10)),
    ])
    values = read_values(source)
    assert [need.text for need in values[0].needs] == ['x']
    volumes = split_volumes(source)
    assert audit_volumes(source, volumes).ok


def test_faux_ivm_is_a_noop_like_the_managed_reader():
    source = _build([
        _E(_ET.SCALAR, _IT.SYMBOL, _tok(2)),
        _E(_ET.SCALAR, _IT.INT, 1),
    ])
    values = read_values(source)
    assert len(values) == 1
    assert audit_volumes(source, split_volumes(source)).ok


def test_volume_drops_unused_text_but_keeps_sids_and_raw_bytes():
    source = _build(
        _lst_symbols(['unused0', 'unused1', 'used']) + [
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(12)),
        ]
    )
    volumes = split_volumes(source)
    # The source used SID 12.  The volume preserves two earlier null slots so
    # the raw TLV (and thus the SID) does not move; only unused text is
    # dropped from the rebuilt symbol table.
    assert b'\x71\x0c' in source
    assert b'\x71\x0c' in volumes[0]
    assert b'unused0' not in volumes[0]
    assert b'unused1' not in volumes[0]
    assert read_values(source)[0].raw == read_values(volumes[0])[0].raw
    assert audit_volumes(source, volumes).ok


def test_group_names_field_annotation_and_value_sids_together():
    source = _build(
        _lst_symbols(['f', 'g']) + [
            _E(_ET.SCALAR, _IT.SYMBOL, _tok(10), field_name=_tok(10),
               annotations=[_tok(11)]),
        ]
    )
    plan = plan_volumes(source)
    assert plan.ok
    value = plan.values[0]
    assert [need.text for need in value.needs] == ['g', 'f']
    assert audit_volumes(source, plan.volumes).ok
