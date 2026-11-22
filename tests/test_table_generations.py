"""
Tests for table generations, merging and reference resolution.

These tests cover the scenario in which two registration tables (``north``
and ``south``) are merged into one ``units`` table while old work orders only
stored a table name and a document ID.
"""

import json
import os

import pytest

from tinydb import TinyDB, TableRef, where
from tinydb.references import (
    AmbiguousReference,
    TableRetiredError,
    UnresolvableReference,
)
from tinydb.storages import MemoryStorage
from tinydb.table import Document, Table

META_KEY = '__tinydb_meta__'


def make_database(tmp_path=None):
    if tmp_path is None:
        return TinyDB(storage=MemoryStorage)
    return TinyDB(os.path.join(str(tmp_path), 'db.json'))


def populate(db):
    """Create north/south tables that both own a document with ID 7."""
    north = db.table('north')
    for index in range(6):
        north.insert({'device': f'north-{index}'})
    assert north.insert({'device': 'north-device-7'}) == 7

    south = db.table('south')
    assert south.insert(Document({'device': 'south-device-7'}, 7)) == 7

    return north, south


@pytest.mark.parametrize('persist', ['memory', 'json'])
def test_merge_moves_documents_and_keeps_duplicate_ids_apart(
    persist, tmp_path
):
    db = make_database(tmp_path if persist == 'json' else None)
    populate(db)

    id_maps = db.merge_tables('units', ['north', 'south'])
    units = db.table('units')

    # Both source tables had an ID 7, but they describe different devices.
    new_north_seven = id_maps['north'][7]
    new_south_seven = id_maps['south'][7]
    assert new_north_seven != new_south_seven

    assert units.get(doc_id=new_north_seven[0]) == {'device': 'north-device-7'}
    assert units.get(doc_id=new_south_seven[0]) == {'device': 'south-device-7'}
    assert len(units) == 8


@pytest.mark.parametrize('persist', ['memory', 'json'])
def test_merged_names_are_retired_and_hidden_from_tables(persist, tmp_path):
    db = make_database(tmp_path if persist == 'json' else None)
    populate(db)
    db.merge_tables('units', ['north', 'south'])

    assert 'north' not in db.tables()
    assert 'south' not in db.tables()
    assert 'units' in db.tables()
    assert META_KEY not in db.tables()


def test_legacy_database_keeps_original_file_layout(tmp_path):
    # Ordinary CRUD must not introduce the generation metadata key.
    path = os.path.join(str(tmp_path), 'legacy.json')
    db = TinyDB(path)
    db.table('north').insert({'device': 'x'})
    db.close()

    with open(path) as handle:
        raw = json.load(handle)
    assert META_KEY not in raw
    assert set(raw) == {'north'}


def test_old_handle_stays_readable_through_alias(tmp_path=None):
    db = make_database(tmp_path)
    populate(db)
    old_north = db.table('north')

    db.merge_tables('units', ['north', 'south'])

    assert old_north.is_retired()
    assert old_north.get(doc_id=7) == {'device': 'north-device-7'}
    assert {doc['device'] for doc in old_north.all()} == {
        f'north-{index}' for index in range(6)
    } | {'north-device-7'}


def test_old_handle_rejects_every_write():
    db = make_database()
    populate(db)
    old_north = db.table('north')
    db.merge_tables('units', ['north'])

    with pytest.raises(TableRetiredError):
        old_north.insert({'device': 'late'})
    with pytest.raises(TableRetiredError):
        old_north.update({'device': 'changed'}, doc_ids=[7])
    with pytest.raises(TableRetiredError):
        old_north.remove(doc_ids=[7])
    with pytest.raises(TableRetiredError):
        old_north.truncate()


def test_snapshot_pins_the_version_for_a_partial_iteration():
    db = make_database()
    north, _ = populate(db)
    first_half = list(north)[:3]

    # The caller has only consumed half of the results before the merge.
    pinned = north.snapshot()
    db.merge_tables('units', ['north', 'south'])

    remaining = pinned.all()[3:]
    assert [doc.doc_id for doc in first_half] == [1, 2, 3]
    assert [doc.doc_id for doc in remaining] == [4, 5, 6, 7]
    assert remaining[-1] == {'device': 'north-device-7'}

    # The snapshot is itself read-only.
    with pytest.raises(TableRetiredError):
        pinned.insert({'device': 'nope'})


def test_snapshot_is_independent_of_later_writes():
    db = make_database()
    north, _ = populate(db)
    pinned = north.snapshot()

    north.update({'device': 'mutated'}, doc_ids=[7])
    assert pinned.get(doc_id=7) == {'device': 'north-device-7'}


def test_reused_name_gets_a_new_empty_generation():
    db = make_database()
    populate(db)
    db.merge_tables('units', ['north', 'south'])

    new_north = db.reuse_table_name('north')

    assert new_north.version == 2
    assert new_north.all() == []
    assert 'north' in db.tables()

    for _ in range(6):
        new_north.insert({'device': 'placeholder'})
    assert new_north.insert({'device': 'brand-new-north-7'}) == 7
    assert new_north.get(doc_id=7) == {'device': 'brand-new-north-7'}


def test_reuse_is_rejected_while_the_table_is_live():
    db = make_database()
    populate(db)
    with pytest.raises(ValueError):
        db.reuse_table_name('north')


def test_old_generation_resolves_to_merge_target_not_the_new_table():
    db = make_database()
    populate(db)
    old_ref = db.table('north').current_ref(doc_id=7)
    assert old_ref == TableRef('north', 1, 7)

    db.merge_tables('units', ['north', 'south'])
    new_north = db.reuse_table_name('north')
    for _ in range(6):
        new_north.insert({'device': 'placeholder'})
    new_north.insert({'device': 'brand-new-north-7'})

    assert db.resolve_ref(TableRef('north', 1, 7)).table == 'units'
    assert db.resolve_ref(TableRef('north', 2, 7)).table == 'north'

    # A late write through the old generation cannot reach the new table.
    old_handle = Table(db.storage, 'north', version=1)
    with pytest.raises(TableRetiredError):
        old_handle.update({'late': True}, doc_ids=[7])
    assert new_north.get(doc_id=7) == {'device': 'brand-new-north-7'}


def test_bare_reference_reports_candidates_instead_of_guessing():
    db = make_database()
    populate(db)
    db.merge_tables('units', ['north', 'south'])
    db.reuse_table_name('north')

    bare = TableRef('north', None, 7)
    candidates = {item.table for item in db.resolve_candidates(bare)}
    assert candidates == {'units', 'north'}

    with pytest.raises(AmbiguousReference) as exc_info:
        db.resolve_ref(bare)
    assert {item.table for item in exc_info.value.candidates} == {
        'units',
        'north',
    }


def test_unknown_generation_is_insufficient_evidence():
    db = make_database()
    populate(db)
    db.merge_tables('units', ['north'])
    db.reuse_table_name('north')

    with pytest.raises(UnresolvableReference):
        db.resolve_ref(TableRef('north', 99, 7))
    assert db.resolve_candidates(TableRef('north', 99, 7)) == []


def test_resolution_is_stable_across_reopen(tmp_path):
    path = os.path.join(str(tmp_path), 'reopen.json')
    db = TinyDB(path)
    populate(db)
    db.merge_tables('units', ['north', 'south'])
    new_north = db.reuse_table_name('north')
    for _ in range(6):
        new_north.insert({'device': 'placeholder'})
    new_north.insert({'device': 'brand-new-north-7'})
    db.close()

    reopened = TinyDB(path)
    assert reopened.resolve_ref(TableRef('north', 1, 7)).table == 'units'
    assert reopened.resolve_ref(TableRef('north', 2, 7)).table == 'north'

    candidates = {
        item.table
        for item in reopened.resolve_candidates(TableRef('north', None, 7))
    }
    assert candidates == {'units', 'north'}

    # Ordinary lookups read the new north generation as usual.
    assert reopened.table('north').version == 2
    assert reopened.table('north').get(doc_id=7) == {
        'device': 'brand-new-north-7'
    }
    reopened.close()


def test_legacy_bare_reference_resolves_to_the_physical_table(tmp_path):
    path = os.path.join(str(tmp_path), 'legacy-ref.json')
    db = TinyDB(path)
    db.table('north').insert(Document({'device': 'old'}, 7))
    db.close()

    reopened = TinyDB(path)
    location = reopened.resolve_ref(TableRef('north', None, 7))
    assert location.table == 'north'
    assert location.doc_id == 7
    assert reopened.table('north').get(doc_id=7) == {'device': 'old'}
    reopened.close()


def test_chain_merge_keeps_oldest_references_resolvable():
    db = make_database()
    populate(db)
    db.merge_tables('units', ['north'])
    db.merge_tables('archive', ['units', 'south'])

    assert db.resolve_ref(TableRef('north', 1, 7)).table == 'archive'
    assert db.resolve_ref(TableRef('south', 1, 7)).table == 'archive'
    assert db.table('north').get(doc_id=7) == {'device': 'north-device-7'}
    assert db.table('south').get(doc_id=7) == {'device': 'south-device-7'}


def test_drop_table_removes_generation_metadata():
    db = make_database()
    populate(db)
    db.merge_tables('units', ['north'])
    db.reuse_table_name('north')

    db.drop_table('north')
    assert 'north' not in db.tables()
    assert db.resolve_candidates(TableRef('north', 2, 7)) == []

    # After dropping every trace, the name starts over as generation one.
    fresh = db.reuse_table_name('north')
    assert fresh.version == 1


def test_retired_handle_sees_later_target_changes():
    db = make_database()
    populate(db)
    old_north = db.table('north')
    db.merge_tables('units', ['north'])

    db.table('units').update({'seen': True}, doc_ids=[7])
    assert old_north.get(doc_id=7) == {
        'device': 'north-device-7',
        'seen': True,
    }
    # Redirected searches are not served from a stale query cache.
    assert old_north.search(where('seen') == True)  # noqa: E712


def test_merge_into_a_retired_name_is_rejected():
    db = make_database()
    populate(db)
    db.merge_tables('units', ['north'])

    with pytest.raises(ValueError):
        db.merge_tables('north', ['south'])

    # Reusing the name first makes it a valid target again.
    reused = db.reuse_table_name('north')
    reused.insert({'device': 'new-generation'})
    db.merge_tables('north', ['south'])
    assert db.table('north').get(doc_id=1) == {'device': 'new-generation'}
    assert db.resolve_ref(TableRef('south', 1, 7)).table == 'north'


def test_snapshot_keeps_old_generation_after_name_reuse():
    db = make_database()
    north, _ = populate(db)
    pinned = north.snapshot()

    db.merge_tables('units', ['north'])
    new_north = db.reuse_table_name('north')
    new_north.insert({'device': 'new-generation'})

    # The snapshot keeps serving the pre-merge documents under old IDs.
    assert pinned.get(doc_id=7) == {'device': 'north-device-7'}
    assert len(pinned.all()) == 7
    assert new_north.get(doc_id=1) == {'device': 'new-generation'}


def test_merge_tables_rejects_self_merge():
    db = make_database()
    populate(db)
    with pytest.raises(ValueError):
        db.merge_tables('north', ['north'])


def test_fresh_handle_binds_to_the_new_generation_after_reuse():
    db = make_database()
    populate(db)
    # A handle used before the merge binds to the first generation.
    old = Table(db.storage, 'north')
    assert old.version == 1

    db.merge_tables('units', ['north'])
    db.reuse_table_name('north')
    db.table('north').insert({'device': 'new-generation'})

    # A handle opened afterwards deterministically binds to the current
    # generation and reads the new table. Ambiguity is only possible for
    # serialized TableRefs through the resolve_* API, not live handles.
    fresh = Table(db.storage, 'north')
    assert fresh.version == 2
    assert fresh.get(doc_id=1) == {'device': 'new-generation'}
    assert old.get(doc_id=7) == {'device': 'north-device-7'}


def test_pinned_handle_to_a_dropped_generation_is_unresolvable():
    db = make_database()
    populate(db)
    db.merge_tables('units', ['north'])
    db.drop_table('north')

    # Generation 1's alias was removed together with the name.
    stale = Table(db.storage, 'north', version=1)
    with pytest.raises(UnresolvableReference):
        stale.all()
