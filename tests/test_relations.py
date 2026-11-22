# -*- coding: utf-8 -*-
from __future__ import division
from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals

import pytest
from tableschema import Table, Schema, VersionedRelations
from tableschema import exceptions


# Constants

SCHEMA = {
    'fields': [
        {'name': 'order_id', 'type': 'integer'},
        {'name': 'warehouse', 'type': 'string'},
        {'name': 'item', 'type': 'string'},
        {'name': 'ext_code', 'type': 'string'},
    ],
    'foreignKeys': [
        {'fields': ['warehouse', 'item'],
         'reference': {'resource': 'inventory',
                       'fields': ['warehouse', 'item']}},
        {'fields': ['warehouse', 'ext_code'],
         'reference': {'resource': 'inventory',
                       'fields': ['warehouse', 'code']}},
    ],
}

INVENTORY = [
    {'warehouse': 'A', 'item': '007', 'code': 'P', 'note': 'first'},
    {'warehouse': 'B', 'item': '009', 'code': 'Q', 'note': 'second'},
    {'warehouse': 'C', 'item': '003', 'code': 'R', 'note': 'third'},
]


# Helpers

def make_store(trim=True):
    consumed = []

    def rows(resource):
        for row in INVENTORY:
            consumed.append(row['item'])
            yield dict(row)
    store = VersionedRelations(
        Schema(SCHEMA), {'inventory': rows('inventory')}, trim=trim)
    return store, consumed


def make_table(rows):
    source = [['order_id', 'warehouse', 'item', 'ext_code']] + rows
    return Table(source, schema=SCHEMA)


def apply_full_patch(store, revision_id='r2'):
    revision = store.begin(revision_id=revision_id)
    revision.update(
        'inventory', {'warehouse': 'A', 'item': '007'}, {'code': 'Q'})
    revision.delete('inventory', {'warehouse': 'B', 'item': '009'})
    revision.insert(
        'inventory', {'warehouse': 'B', 'item': '009', 'code': 'P'})
    return revision


# Tests: loading

def test_load_reads_source_only_once():
    store, consumed = make_store()
    # two foreign keys point to the same resource but the original
    # table is consumed with a single sequential pass
    assert consumed == ['007', '009', '003']
    assert store.version == 1
    assert store.versions == [1]


def test_load_twice_is_rejected():
    store, _ = make_store()
    with pytest.raises(exceptions.RevisionError):
        store.load({'inventory': []})


def test_begin_without_data_is_rejected():
    store = VersionedRelations(Schema(SCHEMA))
    with pytest.raises(exceptions.RevisionError):
        store.begin()


def test_only_read_back_fields_are_stored():
    store, _ = make_store()
    rows = store['inventory']
    assert rows[0] == {'warehouse': 'A', 'item': '007', 'code': 'P'}
    assert 'note' not in rows[0]


def test_trimming_can_be_disabled():
    store, _ = make_store(trim=False)
    assert store['inventory'][0]['note'] == 'first'


# Tests: readers pinned to versions (the main scenario)

def test_readers_validate_against_their_own_version():
    store, _ = make_store()

    # the old order iteration looks up A/007 and pauses
    snapshot_old = store.snapshot()
    table_old = make_table([[1, 'A', '007', 'P'], [2, 'B', '009', 'Q']])
    iterator_old = table_old.iter(
        keyed=True, foreign_keys_values=snapshot_old.foreign_keys_values)
    row = next(iterator_old)
    assert row['warehouse']['code'] == 'P'
    assert row['ext_code']['item'] == '007'

    # a patch is half applied: A/007 has code Q and B/009 is deleted
    # (the intermediate state has a duplicate code Q)
    revision = store.begin(revision_id='r2')
    revision.update(
        'inventory', {'warehouse': 'A', 'item': '007'}, {'code': 'Q'})
    revision.delete('inventory', {'warehouse': 'B', 'item': '009'})

    # a new reader starting in the middle of the patch sees the old version
    snapshot_mid = store.snapshot()
    assert snapshot_mid.version == snapshot_old.version == 1
    index_mid = snapshot_mid.foreign_keys_values
    assert index_mid['inventory'][('warehouse', 'code')][('A', 'P')]['code'] == 'P'
    assert index_mid['inventory'][('warehouse', 'code')][('B', 'Q')]['item'] == '009'

    # the patch is completed and published at once
    revision.insert(
        'inventory', {'warehouse': 'B', 'item': '009', 'code': 'P'})
    assert store.publish(revision) == 2

    # a reader starting after the publication sees the new version
    snapshot_new = store.snapshot()
    assert snapshot_new.version == 2
    index_new = snapshot_new.foreign_keys_values
    assert index_new['inventory'][('warehouse', 'code')][('A', 'Q')]['item'] == '007'
    assert index_new['inventory'][('warehouse', 'code')][('B', 'P')]['item'] == '009'
    assert ('B', 'Q') not in index_new['inventory'][('warehouse', 'code')]

    # the old reader continues with the remaining items based on its
    # own version without re-reading the original inventory
    row = next(iterator_old)
    assert row['warehouse']['code'] == 'Q'
    assert row['ext_code']['item'] == '009'
    with pytest.raises(StopIteration):
        next(iterator_old)

    # a new order iteration validates against the new version
    table_new = make_table([[3, 'A', '007', 'Q'], [4, 'B', '009', 'P']])
    rows_new = table_new.read(
        keyed=True, foreign_keys_values=snapshot_new.foreign_keys_values)
    assert rows_new[0]['warehouse']['code'] == 'Q'
    assert rows_new[1]['ext_code']['item'] == '009'

    snapshot_old.close()
    snapshot_mid.close()
    snapshot_new.close()


def test_two_reads_validate_different_versions_concurrently():
    store, _ = make_store()
    snapshot_old = store.snapshot()
    store.publish(apply_full_patch(store))
    snapshot_new = store.snapshot()

    iterator_old = make_table([[1, 'A', '007', 'P']]).iter(
        keyed=True, foreign_keys_values=snapshot_old.foreign_keys_values)
    iterator_new = make_table([[2, 'A', '007', 'Q']]).iter(
        keyed=True, foreign_keys_values=snapshot_new.foreign_keys_values)

    # interleave the two reads: each one keeps its own version
    assert next(iterator_old)['ext_code']['code'] == 'P'
    assert next(iterator_new)['ext_code']['code'] == 'Q'

    snapshot_old.close()
    snapshot_new.close()


def test_same_version_shares_one_index():
    store, _ = make_store()
    snapshot_one = store.snapshot()
    snapshot_two = store.snapshot()
    assert snapshot_one.foreign_keys_values is snapshot_two.foreign_keys_values
    assert snapshot_one.foreign_keys_values is snapshot_one.foreign_keys_values
    snapshot_one.close()
    snapshot_two.close()


# Tests: revisions

def test_failed_revision_publishes_nothing():
    store, _ = make_store()
    revision = store.begin(revision_id='r3')
    revision.update(
        'inventory', {'warehouse': 'C', 'item': '003'}, {'code': 'S'})
    # the last step of the change errors
    with pytest.raises(exceptions.RevisionError):
        revision.delete('inventory', {'warehouse': 'Z', 'item': '000'})
    # the revision is marked as failed and can't be published
    assert revision.failed is not None
    with pytest.raises(exceptions.RevisionError):
        store.publish(revision)
    # no half of the reference result got published
    assert store.version == 1
    assert store['inventory'][2]['code'] == 'R'


def test_conflict_resubmit_does_not_overwrite_newer_relations():
    store, _ = make_store()
    store.publish(apply_full_patch(store))

    # a change fails on its last step
    revision = store.begin(revision_id='r3')
    revision.update(
        'inventory', {'warehouse': 'C', 'item': '003'}, {'code': 'S'})
    with pytest.raises(exceptions.RevisionError):
        revision.delete('inventory', {'warehouse': 'Z', 'item': '000'})

    # the change is resubmitted with the original number
    resubmitted = store.begin(revision_id='r3')
    resubmitted.update(
        'inventory', {'warehouse': 'C', 'item': '003'}, {'code': 'S'})

    # meanwhile another valid revision changing the same foreign key
    # target gets committed
    other = store.begin(revision_id='r4')
    other.update(
        'inventory', {'warehouse': 'C', 'item': '003'}, {'code': 'T'})
    assert store.publish(other) == 3

    # the stale resubmission conflicts and doesn't overwrite the newer data
    assert resubmitted.id == 'r3'
    assert resubmitted.base == 2
    with pytest.raises(exceptions.RevisionConflict):
        store.publish(resubmitted)
    assert store.version == 3
    row_c = [row for row in store['inventory'] if row['warehouse'] == 'C'][0]
    assert row_c['code'] == 'T'


def test_revision_conflict_is_a_revision_error():
    assert issubclass(exceptions.RevisionConflict, exceptions.RevisionError)
    assert issubclass(exceptions.RevisionError, exceptions.RelationError)


def test_insert_duplicate_identity_is_rejected():
    store, _ = make_store()
    revision = store.begin()
    with pytest.raises(exceptions.RevisionError):
        revision.insert(
            'inventory', {'warehouse': 'A', 'item': '007', 'code': 'X'})


def test_update_ambiguous_key_is_rejected():
    store, _ = make_store()
    revision = store.begin()
    with pytest.raises(exceptions.RevisionError):
        revision.update('inventory', {}, {'code': 'X'})


def test_intermediate_duplicate_state_is_never_published():
    store, _ = make_store()
    # the intermediate state of the full patch has a duplicate code Q
    # but readers only ever see consistent published versions
    revision = store.begin()
    revision.update(
        'inventory', {'warehouse': 'A', 'item': '007'}, {'code': 'Q'})
    revision.delete('inventory', {'warehouse': 'B', 'item': '009'})
    assert store['inventory'][0]['code'] == 'P'
    assert store['inventory'][1]['code'] == 'Q'
    revision.insert(
        'inventory', {'warehouse': 'B', 'item': '009', 'code': 'P'})
    store.publish(revision)
    codes = [row['code'] for row in store['inventory']]
    assert sorted(codes) == ['P', 'Q', 'R']


# Tests: memory management

def test_unchanged_relations_are_not_copied_between_versions():
    store, _ = make_store()
    snapshot_old = store.snapshot()
    revision = store.begin()
    revision.update(
        'inventory', {'warehouse': 'A', 'item': '007'}, {'code': 'Q'})
    store.publish(revision)
    snapshot_new = store.snapshot()
    # untouched rows are shared between versions
    assert snapshot_old['inventory'][1] is snapshot_new['inventory'][1]
    assert snapshot_old['inventory'][2] is snapshot_new['inventory'][2]
    # changed rows are new objects
    assert snapshot_old['inventory'][0] is not snapshot_new['inventory'][0]
    snapshot_old.close()
    snapshot_new.close()


def test_old_versions_are_kept_while_readers_use_them():
    store, _ = make_store()
    snapshot_old = store.snapshot()
    store.publish(apply_full_patch(store))
    assert store.versions == [1, 2]
    # the old version is collected once no reader can use it anymore
    snapshot_old.close()
    assert store.versions == [2]


def test_closed_snapshot_is_not_usable():
    store, _ = make_store()
    snapshot = store.snapshot()
    snapshot.close()
    with pytest.raises(exceptions.RevisionError):
        snapshot.foreign_keys_values


def test_snapshot_as_context_manager():
    store, _ = make_store()
    with store.snapshot() as snapshot:
        assert snapshot.version == 1
    assert store.versions == [1]


# Tests: integration with Table

def test_snapshot_can_be_passed_as_relations():
    store, _ = make_store()
    with store.snapshot() as snapshot:
        rows = make_table([[1, 'A', '007', 'P']]).read(
            keyed=True, relations=snapshot)
    assert rows[0]['warehouse']['code'] == 'P'


def test_store_can_be_passed_as_relations():
    store, _ = make_store()
    rows = make_table([[1, 'A', '007', 'P']]).read(
        keyed=True, relations=store)
    assert rows[0]['warehouse']['code'] == 'P'


def test_unresolved_foreign_key_receipt():
    store, _ = make_store()
    store.publish(apply_full_patch(store))
    with store.snapshot() as snapshot:
        # under the new version the pair (B, Q) doesn't exist anymore
        table = make_table([[9, 'B', '009', 'Q']])
        with pytest.raises(exceptions.UnresolvedFKError):
            table.read(foreign_keys_values=snapshot.foreign_keys_values)
        # the receipt points out which foreign key and which local values
        errors = []

        def handler(exc, row_number=None, row_data=None, error_data=None):
            errors.append((exc, error_data))
        table.read(
            foreign_keys_values=snapshot.foreign_keys_values,
            exc_handler=handler)
        assert len(errors) == 1
        assert isinstance(errors[0][0], exceptions.UnresolvedFKError)
        assert "['warehouse', 'ext_code']" in str(errors[0][0])
        assert errors[0][1] == {'warehouse': 'B', 'ext_code': 'Q'}


def test_composite_keys_can_point_to_different_rows():
    store, _ = make_store()
    store.publish(apply_full_patch(store))
    with store.snapshot() as snapshot:
        index = snapshot.foreign_keys_values['inventory']
        by_item = index[('warehouse', 'item')]
        by_code = index[('warehouse', 'code')]
        assert by_item[('A', '007')]['code'] == 'Q'
        assert by_code[('B', 'P')]['item'] == '009'
        assert by_code[('B', 'P')] is by_item[('B', '009')]


# Tests: Table.index_foreign_keys_values

def test_index_foreign_keys_values_reads_relations_once():
    consumed = []

    def rows():
        for row in INVENTORY:
            consumed.append(row['item'])
            yield row
    table = make_table([])
    index = table.index_foreign_keys_values({'inventory': rows()})
    # the resource is indexed for both foreign keys in a single pass
    assert consumed == ['007', '009', '003']
    assert ('A', '007') in index['inventory'][('warehouse', 'item')]
    assert ('A', 'P') in index['inventory'][('warehouse', 'code')]


def test_index_foreign_keys_values_shares_subindex():
    schema = {
        'fields': [
            {'name': 'warehouse', 'type': 'string'},
            {'name': 'item', 'type': 'string'},
            {'name': 'again', 'type': 'string'},
        ],
        'foreignKeys': [
            {'fields': ['warehouse', 'item'],
             'reference': {'resource': 'inventory',
                           'fields': ['warehouse', 'item']}},
            {'fields': ['warehouse', 'again'],
             'reference': {'resource': 'inventory',
                           'fields': ['warehouse', 'item']}},
        ],
    }
    table = Table([['warehouse', 'item', 'again']], schema=schema)
    index = table.index_foreign_keys_values({'inventory': iter(INVENTORY)})
    subindex = index['inventory'][('warehouse', 'item')]
    assert ('A', '007') in subindex
