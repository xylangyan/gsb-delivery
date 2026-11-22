# -*- coding: utf-8 -*-
from __future__ import division
from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals

from copy import deepcopy
import pytest
from tableschema import Schema, Table, exceptions
from tableschema import SchemaRevision
from tableschema import merge_revisions


# Fixtures

BASE_DESCRIPTOR = {
    'fields': [
        {'name': 'id', 'type': 'integer'},
        {'name': 'owner', 'type': 'string'},
        {'name': 'reviewer', 'type': 'string'},
        {'name': 'store_id', 'type': 'integer'},
    ],
    'primaryKey': ['id', 'store_id'],
    'foreignKeys': [
        # external target whose field happens to be called store_id:
        # renaming the local column must not touch the external name
        {
            'fields': ['store_id'],
            'reference': {'resource': 'stores', 'fields': ['store_id']},
        },
        # self reference back into this same table
        {
            'fields': ['reviewer'],
            'reference': {'resource': '', 'fields': ['owner']},
        },
    ],
}


def draft_a(descriptor=None):
    revision = SchemaRevision(descriptor or BASE_DESCRIPTOR)
    revision.rename_field('owner', 'reviewer')
    revision.rename_field('reviewer', 'owner')
    revision.rename_field('store_id', 'shop_id')
    return revision


def draft_b(descriptor=None):
    revision = SchemaRevision(descriptor or BASE_DESCRIPTOR)
    revision.update_field(
        'owner', {'constraints': {'enum': ['alice', 'bob']}})
    revision.move_field('owner', 2)
    # new self foreign key referencing the current reviewer: its source is
    # the field B chose (owner) and its target is reviewer
    revision.add_foreign_key({
        'fields': ['owner'],
        'reference': {'resource': '', 'fields': ['reviewer']},
    })
    return revision


def field(descriptor, name):
    return next(
        item for item in descriptor['fields'] if item['name'] == name)


# Renames follow field identity

def test_rename_follows_identity_through_pk_and_foreign_keys():
    merged = merge_revisions(BASE_DESCRIPTOR, draft_a())
    assert merged['primaryKey'] == ['id', 'shop_id']

    external, self_reference = merged['foreignKeys']
    assert external['fields'] == ['shop_id']
    assert external['reference']['resource'] == 'stores'
    assert external['reference']['fields'] == ['store_id']
    assert self_reference['fields'] == ['owner']
    assert self_reference['reference']['fields'] == ['reviewer']


def test_swap_keeps_each_field_identity():
    merged = merge_revisions(BASE_DESCRIPTOR, draft_a())
    names = [item['name'] for item in merged['fields']]
    assert names == ['id', 'reviewer', 'owner', 'shop_id']
    # identity (base position 1 = original owner) now carries name reviewer
    assert merged['fields'][1]['type'] == 'string'
    assert merged['fields'][2]['name'] == 'owner'


# Merging both maintainers

def test_merge_a_then_b_lands_changes_on_logical_identities():
    schema = Schema(deepcopy(BASE_DESCRIPTOR))
    schema.merge(draft_a())
    schema.merge(draft_b())
    merged = schema.descriptor

    # the enum was added on the original owner, now called reviewer
    assert field(merged, 'reviewer')['constraints']['enum'] == \
        ['alice', 'bob']
    assert 'constraints' not in field(merged, 'owner')

    # original owner was moved to position 2 (identity based)
    assert [item['name'] for item in merged['fields']] == [
        'id', 'owner', 'reviewer', 'shop_id']

    # B's new FK keeps the source B originally chose (the original owner
    # identity), now called 'reviewer', pointing at the original reviewer
    added_fk = merged['foreignKeys'][-1]
    assert added_fk['fields'] == ['reviewer']
    assert added_fk['reference'] == {
        'resource': '', 'fields': ['owner']}

    # the external target name remains untouched
    assert merged['foreignKeys'][0]['reference']['fields'] == ['store_id']
    assert schema.valid


def test_simultaneous_and_reversed_merges_are_identical():
    sequential_schema = Schema(deepcopy(BASE_DESCRIPTOR))
    sequential_schema.merge(draft_a())
    sequential_schema.merge(draft_b())

    simultaneous = merge_revisions(
        BASE_DESCRIPTOR, draft_a(), draft_b())
    reversed_order = merge_revisions(
        BASE_DESCRIPTOR, draft_b(), draft_a())

    assert sequential_schema.descriptor == simultaneous
    assert simultaneous == reversed_order


def test_merge_rejects_drafts_from_other_bases():
    schema = Schema(deepcopy(BASE_DESCRIPTOR))
    schema.merge(draft_a())
    foreign = SchemaRevision({
        'fields': [{'name': 'id'}, {'name': 'other'}],
    })
    with pytest.raises(exceptions.RevisionError):
        schema.merge(foreign)


# Conflicts

def test_attribute_conflict_reports_position_and_current_names():
    left = SchemaRevision(BASE_DESCRIPTOR)
    left.rename_field('owner', 'reviewer')
    left.rename_field('reviewer', 'owner')
    # identity of the original owner, now called 'reviewer' in this draft
    left.update_field('reviewer', {'type': 'integer'})

    right = SchemaRevision(BASE_DESCRIPTOR)
    right.update_field('owner', {'type': 'number'})

    with pytest.raises(exceptions.RevisionConflictError) as excinfo:
        merge_revisions(BASE_DESCRIPTOR, left, right)

    conflict = excinfo.value
    assert conflict.position == 1
    # current names of the same identity as seen by each revision
    assert conflict.names == ('reviewer', 'owner')


def test_rename_into_occupied_name_conflicts_and_leaves_no_half_descriptor():
    schema = Schema(deepcopy(BASE_DESCRIPTOR))
    schema.merge(draft_a())
    before = deepcopy(schema.descriptor)

    bad = schema.draft()
    bad.rename_field('reviewer', 'owner')
    with pytest.raises(exceptions.RevisionConflictError):
        schema.merge(bad)

    assert schema.descriptor == before
    assert schema.field_names == ['id', 'reviewer', 'owner', 'shop_id']


def test_unrelated_edit_merges_after_a_conflict():
    schema = Schema(deepcopy(BASE_DESCRIPTOR))
    schema.merge(draft_a())

    bad = schema.draft()
    bad.rename_field('reviewer', 'owner')
    with pytest.raises(exceptions.RevisionConflictError):
        schema.merge(bad)

    title = schema.draft()
    title.update_descriptor({'title': 'shipping table'})
    schema.merge(title)

    assert schema.descriptor['title'] == 'shipping table'
    assert schema.field_names == ['id', 'reviewer', 'owner', 'shop_id']


def test_invalid_merged_descriptor_is_not_committed():
    schema = Schema(deepcopy(BASE_DESCRIPTOR))
    before = deepcopy(schema.descriptor)
    revision = schema.draft()
    # enum values must be strings for a string field profile
    revision.update_field('owner', {'constraints': {'enum': [1, 2]}})
    with pytest.raises(exceptions.RevisionError):
        schema.merge(revision)
    assert schema.descriptor == before


def test_repeated_edit_within_one_draft_keeps_only_last_value():
    revision = SchemaRevision(BASE_DESCRIPTOR)
    revision.update_field('owner', {'type': 'integer'})
    revision.update_field('owner', {'type': 'number'})
    merged = merge_revisions(BASE_DESCRIPTOR, revision)
    assert field(merged, 'owner')['type'] == 'number'


def test_distinct_leaf_edits_on_one_field_do_not_conflict():
    left = SchemaRevision(BASE_DESCRIPTOR)
    left.update_field('owner', {'constraints': {'required': True}})
    right = SchemaRevision(BASE_DESCRIPTOR)
    right.update_field(
        'owner', {'constraints': {'enum': ['alice']}})
    merged = merge_revisions(BASE_DESCRIPTOR, left, right)
    owner = field(merged, 'owner')
    assert owner['constraints'] == {
        'required': True, 'enum': ['alice']}


def test_field_added_by_one_draft_stays_editable_in_later_drafts():
    schema = Schema({'fields': [
        {'name': 'id', 'type': 'integer'}, {'name': 'owner'}]})

    first = schema.draft()
    first.add_field({'name': 'note', 'type': 'string'}, position=1)
    schema.merge(first)

    second = schema.draft()
    second.rename_field('note', 'remark')
    second.move_field('remark', 0)
    second.update_field('remark', {'constraints': {'required': True}})
    second.add_foreign_key({
        'fields': ['owner'],
        'reference': {'resource': '', 'fields': ['remark']},
    })
    schema.merge(second)

    names = [item['name'] for item in schema.descriptor['fields']]
    assert names == ['remark', 'id', 'owner']
    assert schema.descriptor['fields'][0]['constraints'] == \
        {'required': True}
    assert schema.descriptor['foreignKeys'] == [{
        'fields': ['owner'],
        'reference': {'resource': '', 'fields': ['remark']},
    }]
    assert schema.valid


# Table reads across versions

OLD_TABLE = [
    ['id', 'owner', 'reviewer', 'store_id'],
    [1, 'alice', 'bob', 10],
    [2, 'carol', 'dave', 20],
    [3, 'erin', 'frank', 30],
]


def test_running_iteration_keeps_old_headers_until_the_end():
    schema = Schema(deepcopy(BASE_DESCRIPTOR))
    table = Table(deepcopy(OLD_TABLE), schema=schema)

    iterator = table.iter()
    assert next(iterator) == [1, 'alice', 'bob', 10]

    schema.merge(draft_a())
    assert schema.field_names == ['id', 'reviewer', 'owner', 'shop_id']

    assert next(iterator) == [2, 'carol', 'dave', 20]
    assert list(iterator) == [[3, 'erin', 'frank', 30]]


def test_new_read_validates_against_merged_version():
    schema = Schema(deepcopy(BASE_DESCRIPTOR))
    schema.merge(draft_a())
    schema.merge(draft_b())

    # old headers no longer match the committed field names
    with pytest.raises(exceptions.CastError) as excinfo:
        Table(deepcopy(OLD_TABLE), schema=schema).read()
    assert 'match schema field names' in str(excinfo.value)

    new_version = [
        ['id', 'owner', 'reviewer', 'shop_id'],
        [1, 'bob', 'alice', 10],
    ]
    rows = Table(new_version, schema=schema).read(keyed=True)
    assert rows == [{
        'id': 1, 'owner': 'bob', 'reviewer': 'alice', 'shop_id': 10}]


def test_new_read_enforces_the_merged_enum():
    schema = Schema(deepcopy(BASE_DESCRIPTOR))
    schema.merge(draft_a())
    schema.merge(draft_b())

    invalid = [
        ['id', 'owner', 'reviewer', 'shop_id'],
        [1, 'bob', 'nobody', 10],
    ]
    with pytest.raises(exceptions.CastError):
        Table(invalid, schema=schema).read()
