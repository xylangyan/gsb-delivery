# -*- coding: utf-8 -*-
from __future__ import division
from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals

import threading
from collections import OrderedDict
from collections import defaultdict
from .schema import Schema
from . import exceptions


# Module API

class VersionedRelations(object):
    """Versioned store of foreign key relations (referenced rows).

    The store ingests the original relations data with a single sequential
    pass per resource (the source iterables are never re-read) and afterwards
    evolves only through atomically published revisions (patches). Readers
    pin a snapshot of one published version and use it to validate foreign
    keys - concurrently with other readers pinned to other versions.

    ```python
    store = VersionedRelations(schema, relations)
    snapshot = store.snapshot()
    table.read(foreign_keys_values=snapshot.foreign_keys_values)
    snapshot.close()

    revision = store.begin(revision_id='r2')
    revision.update('inventory', {'warehouse': 'A', 'item': '007'}, {'code': 'Q'})
    revision.delete('inventory', {'warehouse': 'B', 'item': '009'})
    revision.insert('inventory', {'warehouse': 'B', 'item': '009', 'code': 'P'})
    store.publish(revision)
    ```

    # Arguments
        schema (Schema/dict): schema of the referencing table providing
            the foreign keys (reference resources and fields) to index
        relations (dict): original relations data in a form of
            `{resource1: [{field1: value1, ...}, ...], ...}`; every resource
            is consumed exactly once so one-shot iterables are supported
        trim (bool): if true and a schema is provided, only the fields
            needed for read-back (the foreign key reference fields) are
            stored for every resource

    # Raises
        TableSchemaException: raises on any error

    """

    # Public

    def __init__(self, schema=None, relations=None, trim=True):

        # Schema
        if schema is not None and not isinstance(schema, Schema):
            schema = Schema(schema)
        self.__schema = schema
        self.__trim = trim

        # Reference fields per resource (ordered and deduplicated)
        self.__reference_fields = OrderedDict()
        self.__kept_fields = {}
        if self.__schema:
            for foreign_key in self.__schema.foreign_keys:
                resource = foreign_key['reference']['resource']
                fields = tuple(foreign_key['reference']['fields'])
                self.__reference_fields.setdefault(resource, [])
                if fields not in self.__reference_fields[resource]:
                    self.__reference_fields[resource].append(fields)
                kept_fields = self.__kept_fields.setdefault(resource, set())
                kept_fields.update(fields)

        # Versions
        self.__lock = threading.RLock()
        self.__versions = OrderedDict()
        self.__current = None
        self.__pins = defaultdict(int)

        # Initial load
        if relations is not None:
            self.load(relations)

    @property
    def version(self):
        """Current published version number

        # Returns
            int/None: current version number (None if not loaded)

        """
        return self.__current

    @property
    def versions(self):
        """Live (retained) version numbers

        Versions are retained while they are current or pinned by
        an existing snapshot; unreferenced old versions are collected.

        # Returns
            int[]: sorted list of retained version numbers

        """
        return sorted(self.__versions)

    def load(self, relations):
        """Load the original relations data.

        Every resource iterable is consumed exactly once (a single
        sequential pass) so generators and other one-shot iterables
        are supported. Afterwards the data is only changed by revisions.

        # Arguments
            relations (dict): `{resource1: [{field1: value1, ...}, ...], ...}`

        # Raises
            RevisionError: if relations data is already loaded

        # Returns
            int: number of the published initial version

        """
        with self.__lock:
            if self.__versions:
                message = 'Relations data is already loaded'
                raise exceptions.RevisionError(message)
            resources = {}
            for resource, rows in relations.items():
                resources[resource] = tuple(
                    self.__trim_row(resource, row) for row in rows)
            return self.__add_version(resources)

    def begin(self, revision_id=None):
        """Begin a new revision (patch) based on the current version.

        Operations applied to the revision are private until published:
        concurrent readers never observe a partially applied patch.

        # Arguments
            revision_id (any): optional identifier carried by the revision
                (e.g. to allow resubmission with the original number)

        # Raises
            RevisionError: if no relations data is loaded

        # Returns
            Revision: revision to apply operations to

        """
        with self.__lock:
            if self.__current is None:
                message = 'No relations data is loaded'
                raise exceptions.RevisionError(message)
            return Revision(self, revision_id=revision_id)

    def publish(self, revision):
        """Atomically publish a revision as a new version.

        The whole patch becomes visible to new snapshots at once; if the
        revision has failed, nothing is published. A revision based on a
        version which is not current anymore conflicts and is rejected
        without overwriting the newer relations data.

        # Arguments
            revision (Revision): revision to publish

        # Raises
            RevisionError: if the revision has failed or is foreign
            RevisionConflict: if the revision base is not the current version

        # Returns
            int: number of the published version

        """
        with self.__lock:
            if revision.store is not self:
                message = 'Revision belongs to another relations store'
                raise exceptions.RevisionError(message)
            if revision.failed is not None:
                message = 'Can\'t publish a failed revision: %s' % revision.failed
                raise exceptions.RevisionError(message)
            if revision.base != self.__current:
                message = (
                    'Revision "%s" is based on version "%s" '
                    'but the current version is "%s"' % (
                        revision.id, revision.base, self.__current))
                raise exceptions.RevisionConflict(message)
            resources = dict(self.__versions[self.__current].resources)
            for resource, rows in revision.resources.items():
                resources[resource] = tuple(rows)
            return self.__add_version(resources)

    def snapshot(self):
        """Pin the current version and return a snapshot for reading.

        The snapshot always reflects one valid published version - the one
        current at its start - even while newer revisions are published.
        Close the snapshot (or use it as a context manager) to allow the
        store to collect the pinned version once it gets old.

        # Raises
            RevisionError: if no relations data is loaded

        # Returns
            RelationsSnapshot: snapshot of the current version

        """
        with self.__lock:
            if self.__current is None:
                message = 'No relations data is loaded'
                raise exceptions.RevisionError(message)
            version = self.__versions[self.__current]
            self.__pins[version.number] += 1
            return RelationsSnapshot(self, version)

    def __getitem__(self, resource):
        """Rows of a resource in the current version (read-only)."""
        with self.__lock:
            return self.__versions[self.__current].resources[resource]

    # Internal

    def _rows(self, resource, version):
        with self.__lock:
            return self.__versions[version].resources.get(resource, ())

    def _trim_row(self, resource, row):
        return self.__trim_row(resource, row)

    def _identity(self, resource):
        fields_list = self.__reference_fields.get(resource)
        if fields_list:
            return fields_list[0]
        return None

    def _index(self, version):
        with self.__lock:
            if version.index is None:
                version.index = self.__build_index(version)
            return version.index

    def _release(self, number):
        with self.__lock:
            self.__pins[number] -= 1
            if self.__pins[number] <= 0:
                del self.__pins[number]
            self.__collect_garbage()

    def __trim_row(self, resource, row):
        kept_fields = self.__kept_fields.get(resource)
        if not self.__trim or kept_fields is None:
            return dict(row)
        return dict(
            (field, value) for field, value in row.items()
            if field in kept_fields)

    def __add_version(self, resources):
        number = (self.__current or 0) + 1
        self.__versions[number] = _Version(number, resources)
        self.__current = number
        self.__collect_garbage()
        return number

    def __build_index(self, version):
        index = defaultdict(dict)
        for resource, fields_list in self.__reference_fields.items():
            for fields in fields_list:
                index[resource][fields] = {}
            for row in version.resources.get(resource, ()):
                for fields in fields_list:
                    key = tuple(row.get(field) for field in fields)
                    if key not in index[resource][fields]:
                        index[resource][fields][key] = row
        return index

    def __collect_garbage(self):
        for number in list(self.__versions):
            if number != self.__current and not self.__pins.get(number):
                del self.__versions[number]


class Revision(object):
    """Revision (patch) of a `VersionedRelations` store.

    A revision is a sequence of operations (insert/update/delete) applied
    to a private working copy of the version it was based on. Intermediate
    states of the working copy are never visible to readers and are
    discarded entirely if any operation fails.

    """

    # Public

    def __init__(self, store, revision_id=None):
        self.__store = store
        self.__id = revision_id
        self.__base = store.version
        self.__failed = None
        self.__resources = {}

    @property
    def id(self):
        """Revision identifier

        # Returns
            any: identifier given to `VersionedRelations.begin`

        """
        return self.__id

    @property
    def store(self):
        """Store the revision belongs to

        # Returns
            VersionedRelations: store

        """
        return self.__store

    @property
    def base(self):
        """Version number the revision is based on

        # Returns
            int: base version number

        """
        return self.__base

    @property
    def failed(self):
        """Failure message if an operation of the revision has failed

        # Returns
            str/None: failure message

        """
        return self.__failed

    @property
    def resources(self):
        """Touched resources of the working copy (resource to rows)

        # Returns
            dict: working copy rows per touched resource

        """
        return self.__resources

    def insert(self, resource, row):
        """Insert a row into a resource.

        # Arguments
            resource (str): resource name
            row (dict): keyed row to insert

        # Raises
            RevisionError: if a row with the same identity already exists

        """
        self.__apply(resource, 'insert', row=row)

    def update(self, resource, key, changes):
        """Update the row of a resource identified by a key.

        # Arguments
            resource (str): resource name
            key (dict): keyed fields identifying the row to update
            changes (dict): keyed changes to merge into the row

        # Raises
            RevisionError: if the key matches no or multiple rows

        """
        self.__apply(resource, 'update', key=key, changes=changes)

    def delete(self, resource, key):
        """Delete the row of a resource identified by a key.

        # Arguments
            resource (str): resource name
            key (dict): keyed fields identifying the row to delete

        # Raises
            RevisionError: if the key matches no or multiple rows

        """
        self.__apply(resource, 'delete', key=key)

    # Internal

    def __apply(self, resource, operation, row=None, key=None, changes=None):
        if self.__failed is not None:
            message = 'Revision has already failed: %s' % self.__failed
            raise exceptions.RevisionError(message)
        try:
            rows = self.__working_rows(resource)
            if operation == 'insert':
                row = self.__store._trim_row(resource, row)
                identity = self.__store._identity(resource)
                if identity is not None:
                    identity_key = dict(
                        (field, row.get(field)) for field in identity)
                    if any(_match(item, identity_key) for item in rows):
                        message = (
                            'Duplicate row for key %r in resource "%s"' % (
                                identity_key, resource))
                        raise exceptions.RevisionError(message)
                rows.append(row)
            elif operation == 'update':
                position = self.__find(rows, resource, key)
                updated = dict(rows[position])
                updated.update(changes)
                rows[position] = self.__store._trim_row(resource, updated)
            elif operation == 'delete':
                position = self.__find(rows, resource, key)
                del rows[position]
        except exceptions.RevisionError as exception:
            self.__failed = str(exception)
            raise

    def __working_rows(self, resource):
        if resource not in self.__resources:
            self.__resources[resource] = list(
                self.__store._rows(resource, self.__base))
        return self.__resources[resource]

    def __find(self, rows, resource, key):
        positions = [
            position for position, row in enumerate(rows)
            if _match(row, key)]
        if not positions:
            message = (
                'No row matches key %r in resource "%s"' % (key, resource))
            raise exceptions.RevisionError(message)
        if len(positions) > 1:
            message = (
                'Multiple rows match key %r in resource "%s"' % (key, resource))
            raise exceptions.RevisionError(message)
        return positions[0]


class RelationsSnapshot(object):
    """Snapshot of one published version of a `VersionedRelations` store.

    A snapshot pins the version which was current at its start. All foreign
    keys validated against it resolve to the same relations result,
    independently of revisions published afterwards.

    """

    # Public

    def __init__(self, store, version):
        self.__store = store
        self.__version = version
        self.__closed = False

    @property
    def version(self):
        """Version number the snapshot is pinned to

        # Returns
            int: version number

        """
        return self.__version.number

    @property
    def foreign_keys_values(self):
        """Three-level dictionary of foreign key references for this version

        The index is created once per version and shared by all the foreign
        keys and snapshots based on this version. It can be passed to
        `table.iter`/`table.read` as `foreign_keys_values` and has a form of
        `{resource1: {(fk_field1, fk_field2): {(value1, value2): {row}, ...}}}`.

        # Returns
            dict: index of foreign key references

        """
        self.__ensure_open()
        return self.__store._index(self.__version)

    def __getitem__(self, resource):
        """Rows of a resource in this version (read-only)."""
        self.__ensure_open()
        return self.__version.resources[resource]

    def close(self):
        """Release the pinned version so the store can collect it."""
        if not self.__closed:
            self.__closed = True
            self.__store._release(self.__version.number)

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.close()

    # Internal

    def __ensure_open(self):
        if self.__closed:
            message = 'Snapshot of version "%s" is closed' % self.__version.number
            raise exceptions.RevisionError(message)


# Internal

class _Version(object):

    __slots__ = ('number', 'resources', 'index')

    def __init__(self, number, resources):
        self.number = number
        self.resources = resources
        self.index = None


def _match(row, key):
    return all(row.get(field) == value for field, value in key.items())
