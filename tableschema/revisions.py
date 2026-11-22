# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from copy import deepcopy
from .profile import Profile
from . import exceptions
from . import helpers


# Module API

class SchemaRevision(object):
    """A draft of descriptor changes based on a committed descriptor.

    Every edit is tracked against the *logical identity* of a field, so a
    rename follows the field instead of being treated as a removal and an
    addition. Primary keys, self-referencing foreign keys and other field
    references are rewritten through these identities when revisions are
    merged.

    # Arguments
        descriptor (dict): the base schema descriptor
        strict (bool): raise validation errors immediately instead of
            collecting them

    """

    # Public

    def __init__(self, descriptor, strict=False):
        descriptor = helpers.expand_schema_descriptor(deepcopy(descriptor))
        if not isinstance(descriptor, dict):
            raise exceptions.RevisionError('A schema descriptor must be a dict')
        self.__strict = strict
        self.__base = descriptor
        self.__base_names = [
            field['name'] for field in descriptor.get('fields', [])]
        self.__names = dict(enumerate(self.__base_names))
        self.__changes = []
        self.__add_tokens = []

    @property
    def base_descriptor(self):
        """Expanded deepcopy of the descriptor the revision was drafted from.
        """
        return deepcopy(self.__base)

    @property
    def descriptor(self):
        """Materialized descriptor including all drafted changes.
        """
        descriptor, _ = build_descriptor(
            self.__base, self.__changes, strict=self.__strict)
        return descriptor

    @property
    def changes(self):
        """Structured change records carried by the revision.
        """
        return deepcopy(self.__changes)

    @property
    def field_names(self):
        """Current field names as seen by the revision.
        """
        return list(self.__names.values()) + [
            change['field']['name']
            for change in self.__changes if change['kind'] == 'add']

    def name_for_identity(self, identity):
        """Current name of a base field identity in this revision.
        """
        return self.__names.get(identity)

    def rename_field(self, old_name, new_name):
        """Rename a field keeping its logical identity.

        Name swaps are supported: while the draft is open, two fields may
        temporarily carry the same name. The swap only becomes valid once
        the counterpart rename is recorded; name uniqueness is enforced when
        the descriptor is built or merged.

        # Raises
            RevisionError: if the field is unknown

        """
        identity = self.__identity_of(old_name)
        self.__names[identity] = new_name
        self.__record({
            'kind': 'rename',
            'identity': identity,
            'name': new_name,
        })
        return True

    def update_field(self, name, update):
        """Merge a partial update into the field of the given name.

        Nested mappings (e.g. ``constraints``) are flattened into leaf
        attributes so two revisions can edit different constraints of the
        same field without conflicting.

        """
        identity = self.__identity_of(name)
        if not isinstance(update, dict):
            raise exceptions.RevisionError(
                'A field update must be a dict, got %r' % type(update))
        recorded = 0
        for path, value in _flatten(update):
            self.__changes = [
                change for change in self.__changes
                if not (change['kind'] == 'attr'
                        and change['identity'] == identity
                        and change['path'] == path)]
            self.__changes.append({
                'kind': 'attr',
                'identity': identity,
                'path': path,
                'value': deepcopy(value),
            })
            recorded += 1
        return bool(recorded)

    def move_field(self, name, position):
        """Move the field (by its current name) to another field position.
        """
        identity = self.__identity_of(name)
        if not isinstance(position, int) or isinstance(position, bool):
            raise exceptions.RevisionError(
                'Field position must be an integer, got %r' % type(position))
        if not 0 <= position < len(self.field_names):
            raise exceptions.RevisionError(
                'Field position "%s" is out of range' % position)
        self.__record({
            'kind': 'reorder',
            'identity': identity,
            'position': position,
        })
        return True

    def add_field(self, descriptor, position=None):
        """Add a brand new field (without base identity).
        """
        if not isinstance(descriptor, dict) or 'name' not in descriptor:
            raise exceptions.RevisionError(
                'A field descriptor must be a dict with a "name" key')
        name = descriptor['name']
        if name in self.field_names:
            raise exceptions.RevisionError(
                'Can\'t add field with occupied name "%s"' % name)
        if position is not None and (
                not isinstance(position, int) or isinstance(position, bool)
                or not 0 <= position <= len(self.field_names)):
            raise exceptions.RevisionError(
                'Field position "%s" is out of range' % position)
        token = ('add', id(self), len(self.__add_tokens))
        self.__add_tokens.append(token)
        self.__changes.append({
            'kind': 'add',
            'token': token,
            'field': helpers.expand_field_descriptor(deepcopy(descriptor)),
            'position': position,
        })
        return token

    def add_foreign_key(self, foreign_key):
        """Add a foreign key; local fields must name current fields.

        Reference fields of external resources are kept verbatim. For
        self references (an empty ``resource``) they follow field
        identities.

        """
        foreign_key = deepcopy(foreign_key)
        fields = _as_list(foreign_key.get('fields'))
        reference = foreign_key.get('reference', {})
        resource = reference.get('resource', '')
        for field_name in fields:
            if field_name not in self.field_names:
                raise exceptions.RevisionError(
                    'Foreign key field "%s" does not exist' % field_name)
        if resource == '':
            for field_name in _as_list(reference.get('fields')):
                if field_name not in self.field_names:
                    raise exceptions.RevisionError(
                        'Self reference field "%s" does not exist' % field_name)
        self.__changes.append({
            'kind': 'fk',
            'foreign_key': foreign_key,
            'name_map': self.__name_map(include_adds=True),
        })
        return True

    def update_descriptor(self, update):
        """Update top-level descriptor attributes (e.g. ``title``).
        """
        if not isinstance(update, dict):
            raise exceptions.RevisionError(
                'A descriptor update must be a dict')
        if 'fields' in update or 'primaryKey' in update or \
                'foreignKeys' in update:
            raise exceptions.RevisionError(
                'Managed keys must be changed through their dedicated methods')
        recorded = 0
        for path, value in _flatten(update):
            self.__changes = [
                change for change in self.__changes
                if not (change['kind'] == 'top'
                        and change['path'] == path)]
            self.__changes.append({
                'kind': 'top',
                'path': path,
                'value': deepcopy(value),
            })
            recorded += 1
        return bool(recorded)

    # Internal

    def __identity_of(self, name):
        matches = [
            identity for identity, current_name in self.__names.items()
            if current_name == name]
        if len(matches) == 1:
            return matches[0]
        # During a name swap two identities can briefly share a name: prefer
        # the field that originally carried it.
        for identity in matches:
            if self.__base_names[identity] == name:
                return identity
        raise exceptions.RevisionError('Unknown field "%s"' % name)

    def __name_map(self, include_adds=False):
        mapping = dict(
            (name, identity) for identity, name in self.__names.items())
        if include_adds:
            for change in self.__changes:
                if change['kind'] == 'add':
                    mapping[change['field']['name']] = change['token']
        return mapping

    def __record(self, change):
        key = (change['kind'], change['identity'])
        self.__changes = [
            existing for existing in self.__changes
            if not (existing['kind'] == change['kind']
                    and existing.get('identity') == change.get('identity')
                    and key[0] in ('rename', 'reorder'))]
        self.__changes.append(change)


def combine_revisions(revisions):
    """Combine revision change records and collect attribute conflicts.

    # Returns
        tuple: (combined changes, RevisionConflictError list)

    """
    revisions = list(revisions)
    return _combine_changes(
        revisions,
        resolver_for=lambda revision: revision.name_for_identity)


def names_from_changes(base, changes):
    """Map every field identity (base fields and added fields) to its name.
    """
    names = dict(enumerate(
        field['name'] for field in base.get('fields', [])))
    for change in changes:
        if change['kind'] == 'rename':
            names[change['identity']] = change['name']
        elif change['kind'] == 'add':
            names[change['token']] = change['field']['name']
    return names


class _Changeset(object):
    """Minimal revision-like adapter over already combined change records.
    """

    def __init__(self, changes, names_by_identity):
        self.__changes = changes
        self.__names = names_by_identity

    @property
    def changes(self):
        return deepcopy(self.__changes)

    def name_for_identity(self, identity):
        return self.__names.get(identity)


def normalize_revision(revision, original_base, current_base, names_by_identity):
    """Rebase a revision's change records onto the original field identities.

    A revision may be drafted either from the original base descriptor or
    from the currently committed one. In the latter case, every field
    identity is translated back through the lineage name map so sequential
    and simultaneous merges stay comparable.

    # Arguments
        revision (SchemaRevision): the draft to normalize
        original_base (dict): expanded original base descriptor
        current_base (dict): expanded currently committed descriptor
        names_by_identity (dict): original identity -> current field name

    # Returns
        _Changeset: changes expressed with original base identities

    """
    if _same_descriptor(revision.base_descriptor, original_base):
        names = dict(
            (identity, revision.name_for_identity(identity))
            for identity in range(len(original_base.get('fields', []))))
        return _Changeset(revision.changes, names)

    if not _same_descriptor(revision.base_descriptor, current_base):
        raise exceptions.RevisionError(
            'Revisions must be drafted from the original base or the '
            'currently committed descriptor')

    name_to_identity = dict(
        (name, identity)
        for identity, name in names_by_identity.items())
    current_fields = current_base.get('fields', [])
    translated = []
    revision_names = dict(names_by_identity)
    # The draft's current (possibly renamed) field names mapped back onto
    # the original field identities carried by the lineage.
    current_name_to_identity = {}
    for revision_index, current_field in enumerate(current_fields):
        committed_name = current_field['name']
        original_identity = name_to_identity[committed_name]
        current_name = revision.name_for_identity(revision_index)
        current_name_to_identity[current_name] = original_identity

    for change in revision.changes:
        kind = change['kind']
        if kind in ('rename', 'attr', 'reorder'):
            base_name = current_fields[change['identity']]['name']
            identity = name_to_identity[base_name]
            change = dict(change, identity=identity)
            if kind == 'rename':
                revision_names[identity] = change['name']
        elif kind == 'fk':
            change = dict(change)
            rewritten_map = {}
            for name in change['name_map']:
                if name in current_name_to_identity:
                    rewritten_map[name] = current_name_to_identity[name]
                elif name in name_to_identity:
                    rewritten_map[name] = name_to_identity[name]
                else:
                    raise exceptions.RevisionError(
                        'Foreign key field "%s" is unknown' % name)
            change['name_map'] = rewritten_map
        translated.append(change)

    return _Changeset(translated, revision_names)


def merge_revisions(base_descriptor, revision_a, revision_b=None,
                    strict=False):
    """Merge one or two revisions drafted from the same base descriptor.

    # Returns
        dict: the merged descriptor

    # Raises
        RevisionConflictError: when both revisions change the same field
            attribute to different values
        RevisionError: when the merged result cannot form a valid descriptor

    """
    revisions = [revision_a] + ([revision_b] if revision_b is not None else [])
    bases = [revision.base_descriptor for revision in revisions]
    if not _same_descriptor(bases[0], helpers.expand_schema_descriptor(
            deepcopy(base_descriptor))):
        raise exceptions.RevisionError(
            'Revisions must be drafted from the same base descriptor')
    for base in bases[1:]:
        if not _same_descriptor(base, bases[0]):
            raise exceptions.RevisionError(
                'Revisions must be drafted from the same base descriptor')

    combined, conflicts = _combine_changes(
        revisions, resolver_for=lambda revision: revision.name_for_identity)
    if conflicts:
        raise conflicts[0]
    descriptor, _ = build_descriptor(bases[0], combined, strict=strict)
    return descriptor


def build_descriptor(base, changes, strict=False):
    """Build a descriptor from a base and a list of change records.

    # Returns
        tuple: (descriptor, identity_order)

    """
    base = helpers.expand_schema_descriptor(deepcopy(base))
    changes = deepcopy(changes)

    # Fields keyed by base identity, plus token-keyed additions
    fields = OrderedList(
        (identity, deepcopy(field))
        for identity, field in enumerate(base.get('fields', [])))
    final_names = {}
    for identity, field in fields:
        final_names[identity] = field['name']

    # Renames and attribute edits
    field_identities = dict(fields.items())
    for change in changes:
        if change['kind'] == 'rename':
            identity = change['identity']
            final_names[identity] = change['name']
            if identity in field_identities:
                field_identities[identity]['name'] = change['name']
            else:
                # rename of a field previously added by another revision
                for added in changes:
                    if added['kind'] == 'add' \
                            and added['token'] == identity:
                        added['field']['name'] = change['name']
        elif change['kind'] == 'attr':
            if change['identity'] in field_identities:
                _set_leaf(field_identities[change['identity']],
                          change['path'], change['value'])
            else:
                # attribute edit of a field previously added by a revision
                for added in changes:
                    if added['kind'] == 'add' \
                            and added['token'] == change['identity']:
                        _set_leaf(added['field'],
                                  change['path'], change['value'])
        elif change['kind'] == 'add':
            final_names[change['token']] = change['field']['name']

    # Name uniqueness on the merged field set
    seen = {}
    for identity, name in final_names.items():
        if name in seen:
            first = seen[name]
            position = first if isinstance(first, int) else None
            message = (
                'Field name "%s" is occupied: fields at position %s and %s '
                'both claim it' % (name, first, identity))
            raise exceptions.RevisionConflictError(
                message, position=position, names=(name, name))
        seen[name] = identity

    # Reordering of base fields (identity based, order-independent)
    base_identities = set(identity for identity, _ in fields.items())
    for change in changes:
        if change['kind'] == 'reorder' \
                and change['identity'] in base_identities:
            fields.move(change['identity'], change['position'])

    # New fields are inserted at their recorded positions relative to the
    # current ordering of base fields.
    entries = list(fields.items())
    for change in changes:
        if change['kind'] != 'add':
            continue
        position = change['position']
        if position is None:
            entries.append((change['token'], change['field']))
        else:
            entries.insert(min(position, len(entries)),
                           (change['token'], change['field']))

    # Reordering of previously added (token identity) fields
    for change in changes:
        if change['kind'] != 'reorder' \
                or change['identity'] in base_identities:
            continue
        index = next(
            (i for i, item in enumerate(entries)
             if item[0] == change['identity']), None)
        if index is not None:
            item = entries.pop(index)
            entries.insert(min(change['position'], len(entries)), item)

    descriptor = deepcopy(base)
    descriptor['fields'] = [field for _, field in entries]
    identity_order = [identity for identity, _ in entries]

    # Primary key follows identities
    base_names = [field['name'] for field in base.get('fields', [])]
    base_to_final = dict(zip(base_names, (
        final_names[identity] for identity in range(len(base_names)))))
    if 'primaryKey' in descriptor:
        descriptor['primaryKey'] = [
            base_to_final[name] for name in _as_list(descriptor['primaryKey'])]

    # Base foreign keys: local fields and self references follow identities,
    # external references keep their original field names.
    if 'foreignKeys' in descriptor:
        descriptor['foreignKeys'] = [
            _rewrite_base_foreign_key(foreign_key, base_to_final)
            for foreign_key in descriptor['foreignKeys']]

    # Foreign keys added by revisions
    for change in changes:
        if change['kind'] != 'fk':
            continue
        descriptor.setdefault('foreignKeys', []).append(
            _rewrite_added_foreign_key(
                change['foreign_key'], change['name_map'], final_names))

    # Top-level attribute edits
    for change in changes:
        if change['kind'] == 'top':
            _set_leaf(descriptor, change['path'], change['value'])

    # Whole-descriptor validation; failure means nothing is returned
    profile = Profile('table-schema')
    try:
        profile.validate(descriptor)
    except exceptions.ValidationError as exception:
        if strict:
            raise
        raise exceptions.RevisionError(
            'Merged descriptor is not valid', errors=exception.errors)

    return descriptor, identity_order


# Internal

class OrderedList(object):

    def __init__(self, iterable=None):
        self.__items = list(iterable or [])

    def __iter__(self):
        return iter(self.__items)

    def append(self, item):
        self.__items.append(item)

    def items(self):
        return list(self.__items)

    def get(self, key):
        for identity, value in self.__items:
            if identity == key:
                return value
        raise KeyError(key)

    def move(self, key, position):
        index = next(
            i for i, item in enumerate(self.__items) if item[0] == key)
        item = self.__items.pop(index)
        self.__items.insert(min(position, len(self.__items)), item)


def _combine_changes(revisions, resolver_for):
    combined = []
    conflicts = []
    index = {}

    def add_conflict(kind, identity, path, left, right):
        position = None
        names = ()
        if identity is not None:
            position = identity if isinstance(identity, int) else None
            names = tuple(
                resolver_for(rev)(identity) or '' for rev in revisions)
        message = (
            'Conflicting %s changes on field at position %s (%s): '
            '%r vs %r' % (kind, position, ' / '.join(names), left, right))
        conflicts.append(exceptions.RevisionConflictError(
            message, position=position, names=names))

    for revision in revisions:
        for change in revision.changes:
            kind = change['kind']
            if kind == 'attr':
                key = ('attr', change['identity'], change['path'])
                if key in index:
                    previous = index[key]
                    if previous['value'] != change['value']:
                        add_conflict(
                            'attribute "%s"' % '.'.join(change['path']),
                            change['identity'], change['path'],
                            previous['value'], change['value'])
                    continue
                index[key] = change
                combined.append(change)
            elif kind == 'rename':
                key = ('rename', change['identity'])
                if key in index:
                    previous = index[key]
                    if previous['name'] != change['name']:
                        add_conflict(
                            'name', change['identity'], ('name',),
                            previous['name'], change['name'])
                    continue
                index[key] = change
                combined.append(change)
            elif kind == 'reorder':
                key = ('reorder', change['identity'])
                if key in index:
                    previous = index[key]
                    if previous['position'] != change['position']:
                        add_conflict(
                            'position', change['identity'], ('position',),
                            previous['position'], change['position'])
                    continue
                index[key] = change
                combined.append(change)
            elif kind == 'top':
                key = ('top', change['path'])
                if key in index:
                    previous = index[key]
                    if previous['value'] != change['value']:
                        message = (
                            'Conflicting descriptor changes on "%s": '
                            '%r vs %r' % (
                                '.'.join(change['path']),
                                previous['value'], change['value']))
                        conflicts.append(exceptions.RevisionConflictError(
                            message, position=None, names=()))
                    continue
                index[key] = change
                combined.append(change)
            elif kind == 'add':
                existing = next((
                    other for other in combined
                    if other['kind'] == 'add'
                    and other['field']['name'] == change['field']['name']), None)
                if existing is not None:
                    if existing['field'] != change['field']:
                        message = (
                            'Conflicting added fields named "%s"'
                            % change['field']['name'])
                        conflicts.append(exceptions.RevisionConflictError(
                            message, position=None, names=(
                                change['field']['name'],)))
                    continue
                combined.append(change)
            elif kind == 'fk':
                duplicate = next((
                    other for other in combined
                    if other['kind'] == 'fk'
                    and other['foreign_key'] == change['foreign_key']), None)
                if duplicate is not None:
                    continue
                combined.append(change)
    return combined, conflicts


def _flatten(update, prefix=()):
    for key, value in update.items():
        path = prefix + (key,)
        if isinstance(value, dict):
            for item in _flatten(value, path):
                yield item
        else:
            yield path, value


def _set_leaf(mapping, path, value):
    target = mapping
    for key in path[:-1]:
        target = target.setdefault(key, {})
    target[path[-1]] = value


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _rewrite_base_foreign_key(foreign_key, base_to_final):
    foreign_key = deepcopy(foreign_key)
    local_was_string = not isinstance(foreign_key.get('fields'), list)
    rewritten = [
        base_to_final[name] for name in _as_list(foreign_key['fields'])]
    foreign_key['fields'] = rewritten[0] if local_was_string else rewritten
    reference = foreign_key.setdefault('reference', {})
    if reference.get('resource', '') == '':
        ref_was_string = not isinstance(reference.get('fields'), list)
        rewritten = [
            base_to_final[name]
            for name in _as_list(reference.get('fields'))]
        reference['fields'] = rewritten[0] if ref_was_string else rewritten
    return foreign_key


def _rewrite_added_foreign_key(foreign_key, name_map, final_names):
    foreign_key = deepcopy(foreign_key)
    reference = foreign_key.setdefault('reference', {})

    def resolve(name):
        identity = name_map.get(name)
        if identity is None:
            raise exceptions.RevisionError(
                'Foreign key field "%s" is unknown' % name)
        return final_names[identity]

    local_was_string = not isinstance(foreign_key.get('fields'), list)
    rewritten = [
        resolve(name) for name in _as_list(foreign_key['fields'])]
    foreign_key['fields'] = rewritten[0] if local_was_string else rewritten
    if reference.get('resource', '') == '':
        ref_was_string = not isinstance(reference.get('fields'), list)
        rewritten = [
            resolve(name) for name in _as_list(reference.get('fields'))]
        reference['fields'] = rewritten[0] if ref_was_string else rewritten
    return foreign_key


def _same_descriptor(left, right):
    return left == right
