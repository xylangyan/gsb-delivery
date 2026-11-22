# -*- coding: utf-8 -*-
from __future__ import division
from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals

from collections import OrderedDict
from collections import namedtuple
from . import exceptions


# Module API

class Intake(object):
    """Coordinated intake of multiple tabular sources into a storage.

    The intake receives several sources (e.g. CSV files) at the same time and
    decides which rows are committed to the storage. A source is committed
    only if it passes its integrity check (declared size/hash) after being
    read completely. While reading, the intake emits a `pending` event for
    every accepted row so consumers can pre-check unique values (e.g. article
    and delivery numbers) before the final `committed`/`revoked`/`conflict`
    state follows - correlated by the same `(source, row_number)` key.

    Uniqueness of the fields given in `unique` is checked on cast values
    (e.g. the text "007" equals the integer 7) across all sources.

    Decisions are deterministic: they only depend on the receive order of the
    sources (the order of `add` calls) and the row order inside each source,
    never on which source happens to finish reading first. Each source is
    read once from beginning to end; waiting rows are reported as `waiting`
    events while the intake continues reading the other sources.

    # Arguments
        unique (str[]): field names checked for uniqueness on cast values

    # Example
        ```python
        intake = Intake(unique=['article', 'delivery'])
        intake.add('a', Table('a.csv', schema=schema), integrity={'hash': ...})
        intake.add('b', Table('b.csv', schema=schema), integrity={'hash': ...})
        for event in intake.run():
            print(event.type, event.key)
        ```

    """

    # Public

    def __init__(self, unique=()):
        self.__unique = list(unique)
        self.__sources = []
        self.__owners = {}
        self.__rows = OrderedDict()
        self.__events = []
        self.__history = []
        self.__started = False

    def add(self, name, table, integrity=None, relations=None):
        """Register a source. The order of calls defines the receive order.

        # Arguments
            name (str): source identifier used in every event key
            table (Table): table to read (must have a schema); it is read
                once from beginning to end
            integrity (dict): optional `{'size': bytes, 'hash': sha256}` the
                source is checked against when read completely; a source that
                fails the check is not committed at all
            relations (dict): optional foreign key references in the form
                `{resource: [{field: value, ...}, ...]}` used to validate the
                schema's foreign keys row by row

        # Raises
            TableSchemaException: if the intake is already running, if the
                table has no schema or if a unique field is unknown

        # Returns
            object: internal source handle (mostly for inspection)

        """
        if self.__started:
            message = 'Sources cannot be added after the intake has started'
            raise exceptions.TableSchemaException(message)
        if table.schema is None:
            message = 'Intake sources require a schema (source "%s")' % name
            raise exceptions.TableSchemaException(message)
        unknown = [field for field in self.__unique
                   if not table.schema.get_field(field)]
        if unknown:
            message = 'Unique field(s) %r not in schema of source "%s"' % (
                unknown, name)
            raise exceptions.TableSchemaException(message)
        source = _Source(name=name, table=table, integrity=integrity or {},
                         relations=relations, order=len(self.__sources))
        self.__sources.append(source)
        return source

    @property
    def results(self):
        """Current state of every seen row.

        # Returns
            OrderedDict: `{(source, row_number): state}` where state is one
                of `pending`, `waiting`, `committed`, `revoked`, `conflict`
                or `error`

        """
        return OrderedDict((key, row.state) for key, row in self.__rows.items())

    @property
    def committed(self):
        """Keys of all committed rows in receive/row order.

        # Returns
            tuple[]: list of `(source, row_number)` keys

        """
        return [key for key, row in self.__rows.items()
                if row.state == 'committed']

    @property
    def history(self):
        """All events emitted so far.

        # Returns
            Event[]: events in emission order

        """
        return list(self.__history)

    def run(self):
        """Read all sources once and yield intake events.

        Rows are pulled from the sources one by one in receive order rounds,
        so a row that has to wait is reported as a `waiting` event while the
        other sources keep being read. Row events are: `pending` (provisional
        hold on its unique values), `waiting` (blocked by an earlier row or
        withdrawn hold), `committed` (final, in the storage), `revoked`
        (terminal withdrawal of a hold), `conflict` (terminal uniqueness
        conflict with a committed row) and `error` (row could not be cast or
        violates a foreign key). Source events are `verified` (integrity
        check passed) and `integrity-error` (check failed; all its rows are
        revoked).

        # Raises
            TableSchemaException: if the intake is run more than once

        # Returns
            Iterator[Event]: yields events in deterministic order

        """
        if self.__started:
            message = 'Each source can be read only once'
            raise exceptions.TableSchemaException(message)
        self.__started = True
        active = []
        for source in self.__sources:
            source.start()
            active.append(source)
        while active:
            for source in list(active):
                try:
                    row_number, headers, row = next(source.iterator)
                except StopIteration:
                    active.remove(source)
                    self.__finish_source(source)
                else:
                    self.__accept_row(source, row_number, row)
                while self.__events:
                    yield self.__events.pop(0)

    # Private

    def __accept_row(self, source, row_number, raw_values):
        schema = source.table.schema
        fields = schema.fields

        # Cast field by field keeping positions, original values and
        # the successfully cast fields queryable; a missing column (the row
        # is too short) is reported distinctly from an empty value
        raw = OrderedDict()
        cast = OrderedDict()
        errors = []
        for index, field in enumerate(fields):
            position = index + 1
            if index < len(raw_values):
                value = raw_values[index]
                raw[field.name] = value
                try:
                    cast[field.name] = field.cast_value(value)
                except exceptions.CastError as exception:
                    errors.append(FieldError(
                        field=field.name, position=position, value=value,
                        reason='cast', message=str(exception)))
            else:
                raw[field.name] = MISSING
                errors.append(FieldError(
                    field=field.name, position=position, value=None,
                    reason='missing-column',
                    message='Row is missing column "%s"' % field.name))
        for index in range(len(fields), len(raw_values)):
            position = index + 1
            raw['column%s' % position] = raw_values[index]
            errors.append(FieldError(
                field=None, position=position, value=raw_values[index],
                reason='extra-column',
                message='Row has extra column %s' % position))

        # Check foreign keys on the cast values
        if not errors:
            errors = self.__check_relations(source, cast)

        key = (source.name, row_number)
        if errors:
            row = _Row(source=source, number=row_number, cast=cast, values={})
            row.state = 'error'
            self.__rows[key] = row
            self.__emit(Event(
                'error', source=source.name, row_number=row_number,
                row=cast, raw=raw, errors=errors))
            return

        values = OrderedDict(
            (name, cast[name]) for name in self.__unique
            if cast.get(name) is not None)
        row = _Row(source=source, number=row_number, cast=cast, values=values)
        self.__rows[key] = row
        source.rows.append(row)

        hard, blocked, preempt = self.__classify(row)
        if hard is not None:
            self.__conflict(row, hard)
        elif blocked:
            row.state = 'waiting'
            row.blocked_by = [owner.key for owner in blocked]
            self.__emit(Event(
                'waiting', source=source.name, row_number=row_number,
                row=row.cast, blocked_by=row.blocked_by))
        else:
            for owner in preempt:
                self.__withdraw(owner, winner=row, certain=False)
            self.__claim(row)
        self.__resolve()

    def __finish_source(self, source):
        source.finished = True
        table = source.table
        violations = []
        size = source.integrity.get('size')
        hash = source.integrity.get('hash')
        if size and size != table.size:
            violations.append('size "%s"' % table.size)
        if hash and hash != table.hash:
            violations.append('hash "%s"' % table.hash)
        if violations:
            source.failed = True
            error = exceptions.IntegrityError(
                'Calculated %s differ(s) from declared value(s)' %
                ' and '.join(violations))
            self.__emit(Event('integrity-error', source=source.name,
                              error=error))
            for row in source.rows:
                if row.state in ('pending', 'waiting'):
                    self.__release(row)
                    row.state = 'revoked'
                    self.__emit(Event(
                        'revoked', source=source.name, row_number=row.number,
                        row=row.cast, reason='integrity', error=error))
        else:
            source.verified = True
            self.__emit(Event('verified', source=source.name,
                              size=table.size, hash=table.hash))
        self.__resolve()

    def __resolve(self):
        # Commit frontier: rows are resolved for good in receive/row order
        # and only after their source has passed the integrity check. A
        # waiting row either takes over its values (revoking later
        # provisional holders) or conflicts with a committed row.
        for source in self.__sources:
            if not source.finished:
                break
            if source.failed:
                continue
            for row in source.rows:
                if row.state == 'pending':
                    self.__commit(row)
                elif row.state == 'waiting':
                    hard, blocked, preempt = self.__classify(row)
                    if hard is not None:
                        self.__conflict(row, hard)
                    else:
                        for owner in preempt:
                            self.__withdraw(owner, winner=row, certain=True)
                        self.__claim(row)
                        self.__commit(row)

    def __classify(self, row):
        hard = None
        blocked = []
        preempt = []
        for field, value in row.values.items():
            owner = self.__owners.get(field, {}).get(value)
            if owner is None or owner is row:
                continue
            if owner.state == 'committed':
                hard = owner
                break
            if owner.priority < row.priority:
                blocked.append(owner)
            else:
                preempt.append(owner)
        return hard, blocked, preempt

    def __claim(self, row):
        for field, value in row.values.items():
            self.__owners.setdefault(field, {})[value] = row
        row.state = 'pending'
        row.blocked_by = []
        self.__emit(Event('pending', source=row.source.name,
                          row_number=row.number, row=row.cast))

    def __withdraw(self, row, winner, certain):
        self.__release(row)
        if certain:
            row.state = 'revoked'
            self.__emit(Event('revoked', source=row.source.name,
                              row_number=row.number, row=row.cast,
                              reason='conflict', winner=winner.key))
        else:
            row.state = 'waiting'
            row.blocked_by = [winner.key]
            self.__emit(Event('waiting', source=row.source.name,
                              row_number=row.number, row=row.cast,
                              blocked_by=row.blocked_by))

    def __conflict(self, row, owner):
        self.__release(row)
        row.state = 'conflict'
        self.__emit(Event('conflict', source=row.source.name,
                          row_number=row.number, row=row.cast,
                          winner=owner.key))

    def __commit(self, row):
        row.state = 'committed'
        self.__emit(Event('committed', source=row.source.name,
                          row_number=row.number, row=row.cast))

    def __release(self, row):
        for field, value in row.values.items():
            owners = self.__owners.get(field, {})
            if owners.get(value) is row:
                del owners[value]

    def __check_relations(self, source, cast):
        errors = []
        if not source.relations:
            return errors
        schema = source.table.schema
        if source.fk_index is None:
            source.fk_index = source.table.index_foreign_keys_values(
                source.relations)
        positions = dict(
            (name, index + 1)
            for index, name in enumerate(schema.field_names))
        for foreign_key in schema.foreign_keys:
            local_values = tuple(
                cast.get(field) for field in foreign_key['fields'])
            if set(local_values) == {None}:
                continue
            relation = foreign_key['reference']['resource']
            keys = tuple(foreign_key['reference']['fields'])
            if local_values in source.fk_index[relation][keys]:
                continue
            for field, value in zip(foreign_key['fields'], local_values):
                errors.append(FieldError(
                    field=field, position=positions.get(field), value=value,
                    reason='foreign-key',
                    message=(
                        'Foreign key "%s" violation: %s not found in %s' % (
                            foreign_key['fields'], local_values, relation))))
        return errors

    def __emit(self, event):
        self.__events.append(event)
        self.__history.append(event)


class Event(object):
    """Intake event.

    Row events are correlated by `key` - the same `(source, row_number)`
    identity is used for the early `pending` event and the later `revoked`,
    `conflict` or `committed` state.

    # Attributes
        type (str): one of `pending`, `waiting`, `committed`, `revoked`,
            `conflict`, `error`, `verified`, `integrity-error`
        source (str): source identifier as given to `Intake.add`
        row_number (int): row number inside the source (row events only)
        row (OrderedDict): cast row; for `error` events only the
            successfully cast fields
        raw (OrderedDict): original values (`error` events); missing columns
            are marked with `intake.MISSING`, empty values stay empty
        errors (FieldError[]): per-field errors (`error` events)
        error (Exception): the integrity error (`integrity-error` events)
        blocked_by (tuple[]): keys of the rows this row waits on
        winner (tuple): key of the committed row a `conflict`/`revoked`
            row lost against
        reason (str): `integrity` or `conflict` for `revoked` events
        size (int): actual size in bytes (`verified` events)
        hash (str): actual SHA256 hash (`verified` events)

    """

    # Public

    def __init__(self, type, source=None, row_number=None, row=None,
                 raw=None, errors=None, error=None, blocked_by=None,
                 winner=None, reason=None, size=None, hash=None):
        self.type = type
        self.source = source
        self.row_number = row_number
        self.row = row
        self.raw = raw
        self.errors = errors or []
        self.error = error
        self.blocked_by = blocked_by or []
        self.winner = winner
        self.reason = reason
        self.size = size
        self.hash = hash

    @property
    def key(self):
        """Row identity `(source, row_number)` or `None` for source events.

        # Returns
            tuple/None: row key

        """
        if self.source is not None and self.row_number is not None:
            return (self.source, self.row_number)
        return None

    def __repr__(self):
        parts = [self.type]
        if self.key:
            parts.append('%s:%s' % self.key)
        return '<Event %s>' % ' '.join(parts)


# Field error of an `error` event:
# - field: schema field name (None for extra columns)
# - position: 1-based column position inside the row
# - value: original value (None for missing columns)
# - reason: one of `cast`, `missing-column`, `extra-column`, `foreign-key`
# - message: human readable description
FieldError = namedtuple(
    'FieldError', ['field', 'position', 'value', 'reason', 'message'])


class _Missing(object):
    """Sentinel marking a missing column in an `error` event's raw row."""

    def __repr__(self):
        return 'MISSING'


MISSING = _Missing()


# Internal

class _Source(object):

    def __init__(self, name, table, integrity, relations, order):
        self.name = name
        self.table = table
        self.integrity = integrity
        self.relations = relations
        self.order = order
        self.rows = []
        self.finished = False
        self.verified = False
        self.failed = False
        self.iterator = None
        self.fk_index = None

    def start(self):
        self.iterator = self.table.iter(extended=True, cast=False)


class _Row(object):

    def __init__(self, source, number, cast, values):
        self.source = source
        self.number = number
        self.cast = cast
        self.values = values
        self.state = 'new'
        self.blocked_by = []

    @property
    def key(self):
        return (self.source.name, self.number)

    @property
    def priority(self):
        return (self.source.order, self.number)
