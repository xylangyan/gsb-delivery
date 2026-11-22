# -*- coding: utf-8 -*-
from __future__ import division
from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals

import hashlib
import pytest
from tableschema import Table, Intake
from tableschema.intake import MISSING
from tableschema import exceptions


# Helpers

SCHEMA = {
    'fields': [
        {'name': 'article', 'type': 'integer'},
        {'name': 'delivery', 'type': 'string'},
    ],
}

SCHEMA_FK = {
    'fields': [
        {'name': 'article', 'type': 'integer'},
        {'name': 'delivery', 'type': 'string'},
    ],
    'foreignKeys': [
        {'fields': ['article'],
         'reference': {'resource': 'articles', 'fields': ['id']}},
    ],
}


def make_csv(tmpdir, name, rows):
    lines = ['article,delivery']
    lines += [','.join(map(str, row)) for row in rows]
    contents = '\n'.join(lines) + '\n'
    path = tmpdir.join(name)
    path.write(contents)
    return str(path), hashlib.sha256(contents.encode('utf-8')).hexdigest()


def make_intake(tmpdir, a_hash_ok, relations=None):
    files = {}
    files['a'] = make_csv(tmpdir, 'a.csv', [('007', 'A1'), ('9', 'A2')])
    files['b'] = make_csv(tmpdir, 'b.csv', [('7', 'X'), ('8', 'X')])
    files['c'] = make_csv(tmpdir, 'c.csv', [('8', 'C1')])
    intake = Intake(unique=['article', 'delivery'])
    a_path, a_hash = files['a']
    intake.add('a', Table(a_path, schema=SCHEMA_FK if relations else SCHEMA),
               integrity={'hash': a_hash if a_hash_ok else '0' * 64},
               relations=relations)
    for name in ['b', 'c']:
        path, hash = files[name]
        intake.add(name, Table(path, schema=SCHEMA),
                   integrity={'hash': hash})
    return intake


def events_of(intake, type, source=None):
    return [event for event in intake.history
            if event.type == type
            and (source is None or event.source == source)]


# Tests

def test_integrity_failure_releases_values(tmpdir):
    # A pauses after its first row and finally fails the integrity check:
    # B's first row takes over article 7 and delivery X, B's second row is
    # revoked and C's row is committed
    intake = make_intake(tmpdir, a_hash_ok=False)
    events = list(intake.run())

    # Final states follow receive order and row order
    assert intake.results == {
        ('a', 2): 'revoked',
        ('a', 3): 'revoked',
        ('b', 2): 'committed',
        ('b', 3): 'revoked',
        ('c', 2): 'committed',
    }
    assert intake.committed == [('b', 2), ('c', 2)]

    # A reported the integrity error at its end
    error_events = events_of(intake, 'integrity-error', 'a')
    assert len(error_events) == 1
    assert isinstance(error_events[0].error, exceptions.IntegrityError)
    assert events_of(intake, 'verified', 'b')
    assert events_of(intake, 'verified', 'c')

    # B's first row waited on A's first row (cast "007" == 7)
    waiting = events_of(intake, 'waiting', 'b')[0]
    assert waiting.key == ('b', 2)
    assert waiting.blocked_by == [('a', 2)]

    # B's second row was pending and got revoked by B's first row
    pending = events_of(intake, 'pending', 'b')
    assert set(event.key for event in pending) == {('b', 2), ('b', 3)}
    revoked = events_of(intake, 'revoked', 'b')[0]
    assert revoked.key == ('b', 3)
    assert revoked.reason == 'conflict'
    assert revoked.winner == ('b', 2)

    # Consumers correlate pending and final states by the same key
    by_key = {}
    for event in events:
        if event.key:
            by_key.setdefault(event.key, []).append(event.type)
    assert by_key[('c', 2)][-1] == 'committed'
    assert by_key[('b', 3)] == ['pending', 'revoked']
    assert by_key[('b', 2)][-2:] == ['pending', 'committed']


def test_integrity_success_keeps_holders(tmpdir):
    # A passes the integrity check; its trailing row has a foreign key
    # error but castable values: B's second row survives, C's row
    # reports a conflict with B
    relations = {'articles': [{'id': 7}, {'id': 8}]}
    intake = make_intake(tmpdir, a_hash_ok=True, relations=relations)
    list(intake.run())

    assert intake.results == {
        ('a', 2): 'committed',
        ('a', 3): 'error',
        ('b', 2): 'conflict',
        ('b', 3): 'committed',
        ('c', 2): 'conflict',
    }
    assert intake.committed == [('a', 2), ('b', 3)]

    # B's first row conflicts with A's committed first row
    conflict_b = events_of(intake, 'conflict', 'b')[0]
    assert conflict_b.key == ('b', 2)
    assert conflict_b.winner == ('a', 2)

    # C's row reports the conflict with B's second row
    conflict_c = events_of(intake, 'conflict', 'c')[0]
    assert conflict_c.key == ('c', 2)
    assert conflict_c.winner == ('b', 3)

    # A's trailing row reports the foreign key error with cast values
    error = events_of(intake, 'error', 'a')[0]
    assert error.key == ('a', 3)
    assert error.row == {'article': 9, 'delivery': 'A2'}
    assert len(error.errors) == 1
    assert error.errors[0].reason == 'foreign-key'
    assert error.errors[0].field == 'article'
    assert error.errors[0].value == 9


def test_cast_error_details(tmpdir):
    # Multiple cast errors in one row keep positions, original values and
    # the successful fields queryable; missing columns differ from empties
    path, hash = make_csv(tmpdir, 'd.csv', [('abc', ''), ('1',)])
    intake = Intake(unique=['article', 'delivery'])
    intake.add('d', Table(path, schema=SCHEMA), integrity={'hash': hash})
    list(intake.run())

    bad, short = events_of(intake, 'error', 'd')

    # First row: bad article cast, empty delivery is a valid missing value
    assert bad.key == ('d', 2)
    assert len(bad.errors) == 1
    assert bad.errors[0].reason == 'cast'
    assert bad.errors[0].field == 'article'
    assert bad.errors[0].position == 1
    assert bad.errors[0].value == 'abc'
    assert bad.row == {'delivery': None}
    assert bad.raw == {'article': 'abc', 'delivery': ''}

    # Second row: missing column is distinct from an empty value
    assert short.key == ('d', 3)
    assert len(short.errors) == 1
    assert short.errors[0].reason == 'missing-column'
    assert short.errors[0].field == 'delivery'
    assert short.errors[0].position == 2
    assert short.raw['delivery'] is MISSING
    assert short.row == {'article': 1}


def test_each_source_is_read_only_once(tmpdir):
    intake = make_intake(tmpdir, a_hash_ok=True)
    list(intake.run())
    with pytest.raises(exceptions.TableSchemaException):
        list(intake.run())


def test_waiting_does_not_stop_other_sources(tmpdir):
    # While B's first row waits on A, rows of B and C keep flowing
    intake = make_intake(tmpdir, a_hash_ok=False)
    events = list(intake.run())
    waiting_index = next(
        i for i, event in enumerate(events)
        if event.type == 'waiting' and event.key == ('b', 2))
    later = [event.key for event in events[waiting_index:]
             if event.type == 'pending']
    assert ('b', 3) in later or ('c', 2) in later
