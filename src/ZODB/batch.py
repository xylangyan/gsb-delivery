"""Incremental, receipted delivery of ZODB object graphs.

This module implements the storage-level side of an audit hand-off where
objects are released from legacy ZODB databases into a new group of
ordinary ``FileStorage`` databases one *batch* (release slip) at a time.

Rules
=====

* A batch has a stable identifier.  Its roots are only read when the batch
  is released, and each batch can be released independently of the others.
* Only objects reachable from the released roots through **strong**
  references are copied.  Persistent weak references are preserved but are
  neither followed during the reachability walk nor used to authorize an
  object.
* A strong reference that cannot be satisfied is replaced by ``None`` in
  the delivered object and the relation is listed under the release root in
  the receipt.  Other batches are not blocked.
* Sources are addressed by ``(database name, oid)``.  The destination oid
  is derived deterministically from that pair.  This is what allows a weak
  reference written by an early batch to resolve to an object delivered by
  a later batch, while keeping one identity per source object forever.
* A new generation of the affected databases is prepared off to the side
  and published by atomically replacing ``MANIFEST.json``.  A crash leaves
  either the previous complete group or the next complete group, never a
  half-written one.
* Identities and receipts live inside a real database of the group, so a
  normal ``ZODB`` connection can reopen and audit the result.

Example::

    delivery = Delivery(
        sources={'records': 'legacy/records.fs',
                 'materials': 'legacy/materials.fs'},
        group_dir='delivered',
    )
    receipt = delivery.release('slip-1', [('records', oid_a)])
    with delivery.open_group() as group:
        group.source('records', oid_a)
"""

import hashlib
import io
import json
import os
import shutil

from persistent.mapping import PersistentMapping

from ZODB import broken
from ZODB._compat import PersistentPickler
from ZODB._compat import PersistentUnpickler
from ZODB._compat import _protocol
from ZODB.FileStorage import FileStorage
from ZODB.POSException import POSKeyError
from ZODB.utils import p64
from ZODB.utils import z64

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


#: Root oid shared by every FileStorage database.
ROOT_OID = z64

#: Name of the database holding the identity ledger and receipts.
LEDGER_NAME = '__delivery__'

#: Manifest format written into the group directory.
FORMAT = 'ZODB.batch/1'

#: Database name used by references without an explicit database name.
DEFAULT_DATABASE = 'default'


class BatchError(Exception):
    """Base class for delivery errors."""


class MissingReference(BatchError):
    """A release root is not present in a known source database."""


class DeliveryError(BatchError):
    """The delivered group is inconsistent."""


# ---------------------------------------------------------------------------
# Persistent references inside a record
# ---------------------------------------------------------------------------

class _Reference:
    """A persistent reference discovered while decoding a record."""

    __slots__ = ('oid', 'database', 'klass', 'weak')

    def __init__(self, oid, database=None, klass=None, weak=False):
        self.oid = oid
        self.database = database
        self.klass = klass
        self.weak = weak

    def __repr__(self):  # pragma: no cover - debugging aid
        return '_Reference(%r, database=%r, klass=%r, weak=%r)' % (
            self.oid, self.database, self.klass, self.weak)


def _as_bytes(oid):
    if isinstance(oid, str):
        return oid.encode('ascii')
    return bytes(oid)


def _batch_id(batch):
    if isinstance(batch, bytes):
        return batch.decode('ascii')
    return str(batch)


def _identity_key(database, oid):
    return '%s/%s' % (database, oid.hex())


def _decode(data):
    """Return ``(class, state, references)`` for a stored record.

    ``references`` contains every persistent reference in both pickles,
    including weak references, in the order they are encountered.
    """
    references = []

    def load(reference):
        if isinstance(reference, bytes):
            result = _Reference(reference)
        elif isinstance(reference, tuple):
            oid, klass = reference
            result = _Reference(_as_bytes(oid), klass=klass)
        elif isinstance(reference, list):
            if len(reference) == 2 and reference[0] == 'w':
                args = reference[1]
                if isinstance(args, tuple) and len(args) == 1:
                    result = _Reference(_as_bytes(args[0]), weak=True)
                else:
                    result = _Reference(_as_bytes(args[0]),
                                        database=args[1], weak=True)
            elif len(reference) == 2 and reference[0] == 'm':
                database, oid, klass = reference[1]
                result = _Reference(_as_bytes(oid), database, klass)
            elif len(reference) == 2 and reference[0] == 'n':
                database, oid = reference[1]
                result = _Reference(_as_bytes(oid), database)
            else:
                # Legacy weak reference format: [oid]
                result = _Reference(_as_bytes(reference[0]), weak=True)
        else:
            raise DeliveryError(
                'unsupported persistent reference %r' % (reference,))
        references.append(result)
        return result

    unpickler = PersistentUnpickler(broken.find_global, load, io.BytesIO(data))
    klass = unpickler.load()
    state = unpickler.load()
    return klass, state, references


def _encode(klass, state, database, resolve):
    """Re-pickle a record, asking ``resolve`` about every reference."""
    output = io.BytesIO()

    def persistent_id(obj):
        if not isinstance(obj, _Reference):
            return None
        target = resolve(obj)
        if target is None:
            if obj.weak:
                return None
            raise DeliveryError(
                'cannot encode unsatisfied strong reference %r' % (obj,))
        target_database, oid, target_klass = target
        if obj.weak:
            if target_database == database:
                return ['w', (oid,)]
            return ['w', (oid, target_database)]
        if target_database == database:
            if target_klass is None:
                return oid
            return oid, target_klass
        if target_klass is None:
            return ['n', (target_database, oid)]
        return ['m', (target_database, oid, target_klass)]

    pickler = PersistentPickler(persistent_id, output, _protocol)
    pickler.dump(klass)
    pickler.dump(state)
    return output.getvalue()


def _rewrite(data, database, resolve):
    klass, state, references = _decode(data)
    state = _substitute(state, resolve)
    return _encode(klass, state, database, resolve), references


def _substitute(value, resolve):
    """Replace unsatisfiable strong references in ``state`` with ``None``.

    Weak references and satisfiable strong references are kept as
    :class:`_Reference` markers for the pickler.  The traversal understands
    the container types and object states that can occur in a ZODB record.
    """
    if isinstance(value, _Reference):
        if value.weak:
            return value
        return value if resolve(value) is not None else None
    if isinstance(value, dict):
        return {key: _substitute(item, resolve)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_substitute(item, resolve) for item in value]
    if isinstance(value, tuple):
        return tuple(_substitute(item, resolve) for item in value)
    if isinstance(value, set):
        return {_substitute(item, resolve) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_substitute(item, resolve) for item in value)
    return value


def _mapping_state(mapping):
    return {'data': mapping}


def _mapping_record(mapping, database=DEFAULT_DATABASE, references=None):
    """Encode a ``PersistentMapping`` for ``database``.

    Byte-string values listed in ``references`` are destination oids and
    are written as persistent references in ``database``.  Pass
    ``references=True`` to treat every byte-string value that way.
    """
    if references is True:
        references = {value.oid if isinstance(value, _Reference) else value
                      for value in mapping.values()
                      if isinstance(value, (bytes, _Reference))}
    else:
        references = set(references or ())

    def resolve(reference):
        if reference.oid in references:
            return database, reference.oid, None
        return None

    state = {}
    for key, value in mapping.items():
        if isinstance(value, _Reference):
            state[key] = value
        elif isinstance(value, bytes) and value in references:
            state[key] = _Reference(value)
        else:
            state[key] = value
    return _encode(PersistentMapping, _mapping_state(state), database,
                   resolve)


def _mapping_data(state):
    if 'data' in state:
        return state['data']
    # Very old PersistentMapping pickles used ``_container``.
    if '_container' in state:
        return state['_container']
    return state


def _write_missing(records, database, links):
    """Persist unsatisfied strong references below the releasing roots."""
    for root_key, entries in links.items():
        state = {
            'root': root_key,
            'links': [
                {'owner': entry.get('owner'),
                 'source': entry['source']}
                for entry in entries
            ],
        }
        # This is an additional, directly loadable report of the
        # unsatisfied links for one releasing root.
        records[_missing_oid(root_key)] = _encode(
            PersistentMapping, _mapping_state(state), database,
            lambda reference: None)


def _missing_oid(root_key):
    digest = hashlib.sha256(
        b'ZODB.batch/missing\0' + root_key.encode('utf-8')).digest()
    value = int.from_bytes(digest[:8], 'big') & ((1 << 63) - 1)
    return p64(value or 2)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

class _Transaction:
    """The minimal transaction object accepted by ``BaseStorage``."""

    user = 'ZODB.batch'
    description = 'batch delivery'
    extension_bytes = b''


def _apply(path, records):
    """Store ``{oid: data}`` in ``path`` in a single transaction."""
    storage = FileStorage(path, create=not os.path.exists(path))
    transaction = _Transaction()
    try:
        storage.tpc_begin(transaction)
        for oid, data in records.items():
            try:
                _old, serial = storage.load(oid)
            except POSKeyError:
                serial = None
            storage.store(oid, serial, data, '', transaction)
        storage.tpc_vote(transaction)
        storage.tpc_finish(transaction)
    except BaseException:
        try:
            storage.tpc_abort(transaction)
        except Exception:
            pass
        raise
    finally:
        storage.close()


def _read_root(path):
    storage = FileStorage(path, read_only=True)
    try:
        data = storage.load(ROOT_OID)[0]
    except POSKeyError:
        return {}
    finally:
        storage.close()
    klass, state, _references = _decode(data)
    if not issubclass(klass, PersistentMapping):
        raise DeliveryError('%s does not have a mapping root' % path)
    return _mapping_data(state)


def _read_ledger(path):
    root = _read_root(path)
    root.setdefault('format', FORMAT)
    root.setdefault('identities', {})
    root.setdefault('batches', {})
    root.setdefault('missing', {})
    return root


def _fsync(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_json(path, value):
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as file:
        json.dump(value, file, sort_keys=True, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    _fsync_dir(os.path.dirname(path))


def _read_json(path):
    with open(path, encoding='utf-8') as file:
        return json.load(file)


def _copy_json(value):
    return json.loads(json.dumps(value))


def _remove(path):
    for suffix in ('', '.index', '.lock', '.tmp', '.old'):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Identity derivation
# ---------------------------------------------------------------------------

def destination_oid(database, oid):
    """Return the deterministic destination oid for a source identity.

    The value is stable across processes, releases and restarts and only
    depends on ``(database, oid)``.
    """
    digest = hashlib.sha256(
        b'ZODB.batch\0' + database.encode('utf-8') + b'\0' + _as_bytes(oid)
    ).digest()
    value = int.from_bytes(digest[:8], 'big') & ((1 << 63) - 1)
    return p64(value or 1)


# ---------------------------------------------------------------------------
# Delivered group
# ---------------------------------------------------------------------------

class Group:
    """A reopened set of delivered databases."""

    def __init__(self, directory, read_only=True):
        self.directory = os.path.abspath(directory)
        self.manifest = _read_json(os.path.join(directory, 'MANIFEST.json'))
        if self.manifest.get('format') != FORMAT:
            raise DeliveryError('unsupported group format')
        self.ledger_name = self.manifest['ledger']
        self.paths = {
            name: os.path.join(self.directory, filename)
            for name, filename in self.manifest['databases'].items()
        }
        self.read_only = read_only
        self._databases = None
        self._client = None

    @classmethod
    def open(cls, directory, read_only=True):
        """Open the group currently published in ``directory``."""
        return cls(directory, read_only=read_only)

    def __enter__(self):
        self._open()
        return self

    def __exit__(self, *exc_info):
        self.close()

    def _open(self):
        if self._databases is None:
            from ZODB.DB import DB
            databases = {}
            for name, path in self.paths.items():
                databases[name] = DB(
                    FileStorage(path, read_only=self.read_only),
                    database_name=name,
                    databases=databases,
                )
            self._databases = databases
        return self._databases

    @property
    def databases(self):
        return self._open()

    def db(self, name=None):
        return self._open()[name or self.ledger_name]

    def connection(self, name=None):
        """Return a connection to ``name`` in the shared connection group."""
        client = self.client()
        if name is None or name == client.db().database_name:
            return client
        return client.get_connection(name)

    def client(self, name=None):
        """Return one connection whose ``get_connection`` joins the group.

        Objects reached through cross-database references share this
        connection group, so ``a.material is m17`` holds when both are read
        through the same client.
        """
        if self._client is None:
            name = name or self.ledger_name
            self._client = self._open()[name].open()
        return self._client

    def source(self, source_database, source_oid):
        """Load the object delivered for a source identity."""
        target = self.target(source_database, source_oid)
        if target is None:
            return None
        return self.connection(target[0]).get(target[1])

    def target(self, source_database, source_oid):
        """Return the ``(database, oid)`` delivered for a source identity."""
        entry = self.ledger().get('identities', {}).get(
            _identity_key(source_database, _as_bytes(source_oid)))
        if entry is None:
            return None
        return entry[0], bytes.fromhex(entry[1])

    def ledger(self):
        return self.connection(self.ledger_name).root()

    def path(self, name):
        return self.paths[name]

    @property
    def names(self):
        return sorted(self.paths)

    def receipt(self, batch):
        value = self.ledger().get('batches', {}).get(str(batch))
        return None if value is None else _copy_json(value)

    def identities(self):
        """Return ``{'database/oid': [database, oid]}`` for the group."""
        return _copy_json(self.ledger().get('identities', {}))

    def receipts(self):
        """Return every receipt in the group, keyed by batch id."""
        return _copy_json(self.ledger().get('batches', {}))

    def close(self):
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._databases is not None:
            for database in self._databases.values():
                database.close()
            self._databases = None


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

class _Plan:
    """Everything a single release is going to add."""

    def __init__(self, batch):
        self.batch = batch
        self.identities = {}
        self.records = {}
        self.roots = {}
        self.receipt = {
            'batch': batch,
            'complete': True,
            'roots': {},
            'dependencies': {},
            'missing': {},
        }


class Delivery:
    """Deliver release slips into a group of ordinary FileStorage databases.

    :param sources: mapping of source database name to a ``FileStorage``
        path or an open storage.
    :param destinations: mapping of source database name to destination
        database name.  Defaults to keeping the name.
    :param group_dir: directory holding the delivered generations and the
        manifest.
    :param ledger: destination database name holding identities and
        receipts.
    :param inject: optional callable used by tests to simulate a crash.
        It is called as ``inject(stage, *details)`` and may raise.
    """

    def __init__(self, sources, destinations=None, group_dir='delivered',
                 ledger=LEDGER_NAME, inject=None):
        self.sources = dict(sources)
        if destinations is None:
            destinations = {name: name for name in sources}
        self.destinations = dict(destinations)
        for source_name in self.sources:
            self.destinations.setdefault(source_name, source_name)
        self.group_dir = os.path.abspath(group_dir)
        self.ledger = ledger
        self.inject = inject
        self._sources = {}

    # -- public API ---------------------------------------------------------

    def release(self, batch, roots):
        """Release ``roots`` for ``batch`` and return its receipt.

        ``roots`` is an iterable of ``(source database, oid)`` pairs.
        Releasing an already released batch returns the receipt recorded by
        the first successful release.
        """
        batch = _batch_id(batch)
        roots = [(name, _as_bytes(oid)) for name, oid in roots]
        os.makedirs(self.group_dir, exist_ok=True)
        with _GroupLock(self.group_dir):
            manifest = self._prepare()
            ledger = _read_ledger(self._path(manifest, self.ledger))
            recorded = ledger['batches'].get(batch)
            if recorded is not None:
                return _copy_json(recorded)
            plan = self._plan(batch, roots, ledger)
            self._publish(manifest, ledger, plan)
            return _copy_json(plan.receipt)

    def receipt(self, batch):
        """Return the receipt for ``batch`` or ``None``."""
        manifest = self._prepare()
        ledger = _read_ledger(self._path(manifest, self.ledger))
        recorded = ledger['batches'].get(_batch_id(batch))
        return None if recorded is None else _copy_json(recorded)

    def open_group(self, read_only=True):
        """Open the currently delivered group."""
        self._prepare()
        return Group(self.group_dir, read_only=read_only)

    def close(self):
        for storage in self._sources.values():
            storage.close()
        self._sources.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    # -- setup --------------------------------------------------------------

    def _batch_id(self, batch):
        if isinstance(batch, bytes):
            return batch.decode('ascii')
        return str(batch)

    def _manifest_path(self):
        return os.path.join(self.group_dir, 'MANIFEST.json')

    def _prepare(self):
        path = self._manifest_path()
        if os.path.exists(path):
            manifest = _read_json(path)
            if manifest.get('format') != FORMAT:
                raise DeliveryError('unsupported group format')
            return manifest

        databases = {}
        for destination in sorted(set(self.destinations.values())):
            filename = self._filename(destination, 0)
            _apply(os.path.join(self.group_dir, filename),
                   {ROOT_OID: _mapping_record({}, destination)})
            databases[destination] = filename
        if self.ledger not in databases:
            filename = self._filename(self.ledger, 0)
            _apply(os.path.join(self.group_dir, filename),
                   {ROOT_OID: _mapping_record({}, self.ledger)})
            databases[self.ledger] = filename
        manifest = {
            'format': FORMAT,
            'generation': 0,
            'ledger': self.ledger,
            'databases': databases,
        }
        _atomic_json(path, manifest)
        return manifest

    @staticmethod
    def _filename(database, generation):
        return '%s-%d.fs' % (database, generation)

    def _path(self, manifest, database):
        return os.path.join(self.group_dir,
                            manifest['databases'][database])

    def _source(self, database):
        if database not in self.sources:
            raise MissingReference(
                'database %r is not a known source' % database)
        if database not in self._sources:
            source = self.sources[database]
            if isinstance(source, str):
                source = FileStorage(source, read_only=True)
            self._sources[database] = source
        return self._sources[database]

    def _destination(self, database):
        return self.destinations.get(database, database)

    # -- planning -----------------------------------------------------------

    def _plan(self, batch, roots, ledger):
        plan = _Plan(batch)
        identities = ledger['identities']
        discovered = []
        seen = set()
        queue = []

        for database, oid in roots:
            key = (database, oid)
            root_key = _identity_key(database, oid)
            if key in seen:
                continue
            seen.add(key)
            if _identity_key(database, oid) in identities:
                self._record_existing(plan, ledger, database, oid, root_key,
                                      'root')
                continue
            if database not in self.sources:
                raise MissingReference(
                    'root %s is not in a known source database' % root_key)
            queue.append((database, oid, root_key, 'root'))

        # First pass: read every new record and discover its strong
        # references.  No object is rewritten or allocated yet, so a
        # reference may point forward at any other object of the slip.
        while queue:
            database, oid, root_key, kind = queue.pop(0)
            key = (database, oid)
            if _identity_key(database, oid) in identities:
                self._record_existing(plan, ledger, database, oid, root_key,
                                      kind)
                continue
            data = self._load_source(database, oid)
            if data is None:
                if kind == 'root':
                    raise MissingReference(
                        'root %s is not present in the source' %
                        _identity_key(database, oid))
                # The owner's resolver records the unsatisfied relation
                # under the releasing root.
                continue

            target_database = self._destination(database)
            target_oid = destination_oid(database, oid)
            identities[_identity_key(database, oid)] = [
                target_database, target_oid.hex()]
            plan.identities[key] = (target_database, target_oid)
            plan.receipt['roots' if kind == 'root' else 'dependencies'][
                _identity_key(database, oid)] = {
                    'source': [database, oid.hex()],
                    'target': [target_database, target_oid.hex()],
            }
            if kind == 'root':
                plan.roots.setdefault(target_database, {})[
                    _identity_key(database, oid)] = target_oid
            discovered.append((database, oid, root_key, data))

            _klass, _state, references = _decode(data)
            for reference in references:
                if reference.weak:
                    continue
                reference_database = reference.database or database
                reference_key = (reference_database, reference.oid)
                if reference_key in seen:
                    continue
                seen.add(reference_key)
                if _identity_key(reference_database,
                                 reference.oid) in identities:
                    self._record_existing(
                        plan, ledger, reference_database, reference.oid,
                        root_key, 'dependency')
                    continue
                if reference_database not in self.sources:
                    # The resolver records this relation when rewriting the
                    # owner's record.
                    continue
                queue.append((reference_database, reference.oid, root_key,
                              'dependency'))

        # Second pass: every identity in this slip now exists, so rewriting
        # can resolve forward and backward references uniformly.
        for database, oid, root_key, data in discovered:
            target_database, target_oid = plan.identities[(database, oid)]
            rewritten, _references = _rewrite(
                data, target_database,
                self._resolver(database, oid, identities, plan, root_key),
            )
            plan.records.setdefault(target_database, {})[target_oid] = \
                rewritten

        receipt = plan.receipt
        # Duplicate missing links can appear when several owners point at the
        # same absent object; report each relation once.
        for root_key, links in list(receipt['missing'].items()):
            unique = {}
            for link in links:
                unique[json.dumps(link, sort_keys=True)] = link
            receipt['missing'][root_key] = list(unique.values())
        return plan

    def _record_existing(self, plan, ledger, database, oid, root_key, kind):
        entry = ledger['identities'][_identity_key(database, oid)]
        plan.receipt['roots' if kind == 'root' else 'dependencies'][
            _identity_key(database, oid)] = {
                'source': [database, oid.hex()],
                'target': [entry[0], entry[1]],
        }
        if kind == 'root':
            plan.roots.setdefault(entry[0], {})[
                _identity_key(database, oid)] = bytes.fromhex(entry[1])

    def _resolver(self, owner_database, owner_oid, identities, plan,
                  root_key):
        def resolve(reference):
            reference_database = reference.database or owner_database
            entry = identities.get(
                _identity_key(reference_database, reference.oid))
            if entry is not None:
                return entry[0], bytes.fromhex(entry[1]), reference.klass
            if reference.weak:
                # A weak reference may name an object that a later batch will
                # deliver.  Point it at the deterministic destination so it
                # starts resolving as soon as that batch is accepted, while
                # reading as empty until then.
                if reference_database in self.destinations:
                    return (self._destination(reference_database),
                            destination_oid(reference_database,
                                            reference.oid),
                            reference.klass)
                return (reference_database, reference.oid, reference.klass)
            # Strong reference that is not released: note it under the
            # releasing root and keep the object loadable.  The reference
            # itself is replaced by ``None`` by ``_substitute``.
            plan.receipt['missing'].setdefault(root_key, []).append({
                'owner': [owner_database, owner_oid.hex()],
                'source': [reference_database, reference.oid.hex()],
            })
            return None
        return resolve

    def _load_source(self, database, oid):
        try:
            return self._source(database).load(oid)[0]
        except POSKeyError:
            return None

    # -- publishing ---------------------------------------------------------

    def _publish(self, manifest, ledger, plan):
        generation = manifest['generation'] + 1
        databases = dict(manifest['databases'])
        old_files = set(databases.values())
        new_files = set()

        ledger['batches'][plan.batch] = plan.receipt
        for root_key, links in plan.receipt['missing'].items():
            ledger.setdefault('missing', {})[root_key] = links
        changed = dict(plan.records)
        if self.ledger in changed:
            raise DeliveryError('the ledger database cannot hold records')
        changed[self.ledger] = {}
        for database in plan.roots:
            changed.setdefault(database, {})

        published = False
        try:
            for database in sorted(changed):
                old_path = self._path(manifest, database)
                filename = self._filename(database, generation)
                new_path = os.path.join(self.group_dir, filename)
                self._call('before-copy', database)
                if database == self.ledger:
                    shutil.copyfile(old_path, new_path)
                    _remove_index(new_path)
                    root = _read_ledger(new_path)
                    root.update(ledger)
                    records = dict(plan.records.get(database, {}))
                    records[ROOT_OID] = _mapping_record(root, database)
                    _apply(new_path, records)
                else:
                    shutil.copyfile(old_path, new_path)
                    _remove_index(new_path)
                    records = dict(plan.records.get(database, {}))
                    root = _read_root(new_path)
                    roots = dict(plan.roots.get(database, {}))
                    root.update(roots)
                    for root_key, entries in plan.receipt['missing'].items():
                        if root_key.split('/', 1)[0] != database:
                            continue
                        root['missing/%s' % root_key] = _missing_oid(root_key)
                    records[ROOT_OID] = _mapping_record(
                        root, database, references=True)
                    missing = {}
                    for root_key, entries in plan.receipt['missing'].items():
                        if root_key.split('/', 1)[0] == database:
                            missing[root_key] = entries
                    _write_missing(records, database, missing)
                    _apply(new_path, records)
                _fsync(new_path)
                self._call('after-write', database, new_path)
                databases[database] = filename
                new_files.add(filename)

            new_manifest = {
                'format': FORMAT,
                'generation': generation,
                'ledger': self.ledger,
                'databases': databases,
            }
            self._call('before-publish')
            _atomic_json(self._manifest_path(), new_manifest)
            published = True
            self._call('after-publish')
        except BaseException:
            if not published:
                for filename in new_files:
                    _remove(os.path.join(self.group_dir, filename))
            # If the manifest was already replaced, the new generation is
            # live: leave it in place so the redelivered slip finds its
            # receipt instead of allocating new identities.
            raise

        for filename in old_files - set(databases.values()):
            _remove(os.path.join(self.group_dir, filename))
        self._sweep(databases)
        self._write_mirrors(ledger)
        return new_manifest

    def _write_mirrors(self, ledger):
        """Write JSON mirrors of the ledger beside the group.

        These files are derived data.  The authoritative identities and
        receipts are the objects inside the ledger database.
        """
        try:
            _atomic_json(os.path.join(self.group_dir, 'IDENTITIES.json'),
                         ledger.get('identities', {}))
            _atomic_json(os.path.join(self.group_dir, 'RECEIPTS.json'),
                         ledger.get('batches', {}))
        except OSError:  # pragma: no cover - best effort
            pass

    def _sweep(self, databases):
        """Remove generation files that no manifest refers to.

        Only files named ``<database>-<generation>.fs`` are considered, so
        unrelated files in the group directory are left alone.
        """
        live = set(databases.values())
        for filename in os.listdir(self.group_dir):
            if filename in live or not filename.endswith('.fs'):
                continue
            stem = filename[:-3]
            database, separator, generation = stem.rpartition('-')
            if not separator or not generation.isdigit():
                continue
            if database not in databases:
                continue
            _remove(os.path.join(self.group_dir, filename))

    def _call(self, stage, *details):
        if self.inject is not None:
            self.inject(stage, *details)


def _remove_index(path):
    try:
        os.remove(path + '.index')
    except OSError:
        pass


class _GroupLock:

    def __init__(self, directory):
        self.path = os.path.join(directory, '.delivery.lock')
        self.file = None

    def __enter__(self):
        self.file = open(self.path, 'a+')
        if fcntl is not None:
            try:
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX)
            except OSError:  # pragma: no cover - non-POSIX platforms
                pass
        return self

    def __exit__(self, *exc_info):
        try:
            if self.file is not None and fcntl is not None:
                try:
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
                except OSError:  # pragma: no cover
                    pass
        finally:
            if self.file is not None:
                self.file.close()
                self.file = None


def open_group(directory, read_only=True):
    """Open the group currently delivered in ``directory``."""
    return Group.open(directory, read_only=read_only)
