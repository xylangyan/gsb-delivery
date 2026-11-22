"""
This module contains the main component of TinyDB: the database.
"""

from collections.abc import Iterator

from . import JSONStorage, registry
from .references import (
    AmbiguousReference,
    ResolvedLocation,
    TableRef,
    UnresolvableReference,
)
from .storages import Storage
from .table import Table, Document
from .utils import with_typehint

# The table's base class. This is used to add type hinting from the Table
# class to TinyDB. Currently, this supports PyCharm, Pyright/VS Code and MyPy.
TableBase: type[Table] = with_typehint(Table)


class TinyDB(TableBase):
    """
    The main class of TinyDB.

    The ``TinyDB`` class is responsible for creating the storage class instance
    that will store this database's documents, managing the database
    tables as well as providing access to the default table.

    For table management, a simple ``dict`` is used that stores the table class
    instances accessible using their table name.

    Default table access is provided by forwarding all unknown method calls
    and property access operations to the default table by implementing
    ``__getattr__``.

    When creating a new instance, all arguments and keyword arguments (except
    for ``storage``) will be passed to the storage class that is provided. If
    no storage class is specified, :class:`~tinydb.storages.JSONStorage` will be
    used.

    .. admonition:: Customization

        For customization, the following class variables can be set:

        - ``table_class`` defines the class that is used to create tables,
        - ``default_table_name`` defines the name of the default table, and
        - ``default_storage_class`` will define the class that will be used to
          create storage instances if no other storage is passed.

        .. versionadded:: 4.0

    .. admonition:: Data Storage Model

        Data is stored using a storage class that provides persistence for a
        ``dict`` instance. This ``dict`` contains all tables and their data.
        The data is modelled like this::

            {
                'table1': {
                    0: {document...},
                    1: {document...},
                },
                'table2': {
                    ...
                }
            }

        Each entry in this ``dict`` uses the table name as its key and a
        ``dict`` of documents as its value. The document ``dict`` contains
        document IDs as keys and the documents themselves as values.

    :param storage: The class of the storage to use. Will be initialized
                    with ``args`` and ``kwargs``.
    """

    #: The class that will be used to create table instances
    #:
    #: .. versionadded:: 4.0
    table_class = Table

    #: The name of the default table
    #:
    #: .. versionadded:: 4.0
    default_table_name = '_default'

    #: The class that will be used by default to create storage instances
    #:
    #: .. versionadded:: 4.0
    default_storage_class = JSONStorage

    def __init__(self, *args, **kwargs) -> None:
        """
        Create a new instance of TinyDB.
        """

        storage = kwargs.pop('storage', self.default_storage_class)

        # Prepare the storage
        self._storage: Storage = storage(*args, **kwargs)

        self._opened = True
        self._tables: dict[str, Table] = {}

    def __repr__(self):

        args = [
            f'tables={list(self.tables())}',
            f'tables_count={len(self.tables())}',
            f'default_table_documents_count={self.__len__()}',
            f'all_tables_documents_count={[f"{table}={len(self.table(table))}" for table in self.tables()]}',
        ]

        return '<{} {}>'.format(type(self).__name__, ', '.join(args))

    def table(self, name: str, **kwargs) -> Table:
        """
        Get access to a specific table.

        If the table hasn't been accessed yet, a new table instance will be
        created using the :attr:`~tinydb.database.TinyDB.table_class` class.
        Otherwise, the previously created table instance will be returned.

        All further options besides the name are passed to the table class which
        by default is :class:`~tinydb.table.Table`. Check its documentation
        for further parameters you can pass.

        :param name: The name of the table.
        :param kwargs: Keyword arguments to pass to the table class constructor
        """

        if name in self._tables:
            return self._tables[name]

        table = self.table_class(self.storage, name, **kwargs)
        self._tables[name] = table

        return table

    def tables(self) -> set[str]:
        """
        Get the names of all live tables in the database.

        Only currently existing, non-retired tables are returned. The
        generation metadata entry is never part of the result.

        :returns: a set of table names
        """

        data = self.storage.read()

        return registry.physical_table_names(data)

    def drop_tables(self) -> None:
        """
        Drop all tables from the database. **CANNOT BE REVERSED!**

        This also clears the table generation metadata, so the database is
        returned to a fully empty state.
        """

        # We drop all tables from this database by writing an empty dict
        # to the storage thereby returning to the initial state with no tables.
        self.storage.write({})

        # After that we need to remember to empty the ``_tables`` dict, so we'll
        # create new table instances when a table is accessed again.
        self._tables.clear()

    def drop_table(self, name: str) -> None:
        """
        Drop a specific table from the database. **CANNOT BE REVERSED!**

        Both the physical table and every recorded generation/alias entry of
        the name are removed, so the name can later be reused as a fresh
        first-generation table.

        :param name: The name of the table to drop.
        """

        # If the table is currently opened, we need to forget the table class
        # instance
        if name in self._tables:
            del self._tables[name]

        data = self.storage.read()

        # The database is uninitialized, there's nothing to do
        if data is None:
            return

        had_meta = bool(registry.get_meta(data))

        # The table does not exist, there's nothing to do
        if name not in registry.physical_table_names(data):
            # Still clean up stale metadata for a previously retired name,
            # but don't introduce metadata into a legacy database.
            if had_meta:
                registry.forget_name(data, name)
                self.storage.write(data)
            return

        # Remove the table from the data dict
        del data[name]
        if had_meta:
            registry.forget_name(data, name)

        # Store the updated data back to the storage
        self.storage.write(data)

    @property
    def storage(self) -> Storage:
        """
        Get the storage instance used for this TinyDB instance.

        :return: This instance's storage
        :rtype: Storage
        """
        return self._storage

    def merge_tables(
        self, target_name: str, source_names: list[str]
    ) -> dict[str, dict[int, list[int]]]:
        """
        Merge one or more tables into another table and retire their names.

        All documents of every table in ``source_names`` are moved into the
        table ``target_name`` (which is created on demand). Duplicate document
        IDs across the sources and the target do not collide: every moved
        document receives a fresh ID in the target. The returned mapping
        reports the old-ID -> new-ID(s) translation per source table.

        Every source name is retired afterwards: an alias records that its
        documents now live in ``target_name``, so references captured before
        the merge (see :meth:`tinydb.table.Table.current_ref`) still resolve
        to the moved data. Already opened source handles stay readable but
        reject further writes.

        :param target_name: The table that receives all documents.
        :param source_names: The tables that are merged away.
        """

        if target_name in source_names:
            raise ValueError('A table cannot be merged into itself')

        data = self.storage.read()
        if registry.get_meta(data) and not registry.is_live(
            data, target_name
        ) and registry.generations_for(data, target_name):
            raise ValueError(
                f'Table {target_name!r} has been retired; call '
                f'reuse_table_name() before merging into it again'
            )

        target = self.table(target_name)
        id_maps: dict[str, dict[int, list[int]]] = {}

        for source_name in source_names:
            source = self.table(source_name)
            id_maps[source_name] = source.merge_table(target)

            # Forget the retired handle; a later ``table()`` call opens a
            # fresh, generation-aware handle instead.
            self._tables.pop(source_name, None)

        return id_maps

    def reuse_table_name(self, name: str) -> Table:
        """
        Create a new, empty table under a name that was merged or dropped.

        The reused table receives a brand-new generation number, one higher
        than any generation the name previously had. Documents and references
        of the old generation keep pointing at the merge target, so a late
        write addressed to the old generation can never reach this table.

        :param name: The table name to reuse.
        :returns: The fresh, empty table handle.
        """

        data = self.storage.read()
        if data is None:
            data = {}

        registry.ensure_meta(data)

        if registry.is_live(data, name):
            raise ValueError(
                f'Table {name!r} still exists; drop or merge it before '
                f'reusing the name'
            )

        new_version = registry.bump_version(data, name)
        data[name] = {}
        self.storage.write(data)

        # Replace any cached handle of the retired generation.
        self._tables.pop(name, None)
        table = self.table_class(
            self.storage, name, version=new_version
        )
        self._tables[name] = table

        return table

    def resolve_candidates(self, ref: TableRef) -> list[ResolvedLocation]:
        """
        Return every location a :class:`~tinydb.references.TableRef` may mean.

        - A reference carrying a generation returns at most one location: the
          live table when the generation is current, or the merge target
          recorded for that generation.
        - A reference that only stores a table name returns one location per
          generation known for that name. When the name was reused, this
          naturally contains both the old generation's merge target and the
          new table.
        - An unknown name/generation returns an empty list -- the caller then
          knows the evidence is insufficient instead of guessing.

        :param ref: The reference captured by a work order.
        """

        data = self.storage.read()

        # Databases without any generation metadata only ever contain
        # first-generation tables. A reference to an existing physical table
        # therefore resolves directly, including references that only stored
        # the bare table name.
        if not registry.get_meta(data):
            if (
                ref.name in registry.physical_table_names(data)
                and ref.version in (None, 1)
            ):
                return [ResolvedLocation(table=ref.name, doc_id=ref.doc_id)]
            return []

        generations = registry.generations_for(data, ref.name)

        if ref.version is not None:
            generations = [
                (version, table)
                for version, table in generations
                if version == ref.version
            ]

        return [
            ResolvedLocation(table=table, doc_id=ref.doc_id)
            for _, table in generations
        ]

    def resolve_ref(self, ref: TableRef) -> ResolvedLocation:
        """
        Resolve a reference to exactly one current location.

        :raises AmbiguousReference: if the reference carries no generation and
            several generations match. The exception exposes the candidates.
        :raises UnresolvableReference: if no generation matches and there is
            not enough evidence to pick a location. The new table is never
            assumed automatically.
        """

        candidates = self.resolve_candidates(ref)

        if len(candidates) == 1:
            return candidates[0]

        if len(candidates) > 1:
            raise AmbiguousReference(ref, candidates)

        raise UnresolvableReference(ref)

    def close(self) -> None:
        """
        Close the database.

        This may be needed if the storage instance used for this database
        needs to perform cleanup operations like closing file handles.

        To ensure this method is called, the TinyDB instance can be used as a
        context manager::

            with TinyDB('data.json') as db:
                db.insert({'foo': 'bar'})

        Upon leaving this context, the ``close`` method will be called.
        """
        self._opened = False
        self.storage.close()

    def __enter__(self):
        """
        Use the database as a context manager.

        Using the database as a context manager ensures that the
        :meth:`~tinydb.database.TinyDB.close` method is called upon leaving
        the context.

        :return: The current instance
        """
        return self

    def __exit__(self, *args):
        """
        Close the storage instance when leaving a context.
        """
        if self._opened:
            self.close()

    def __getattr__(self, name):
        """
        Forward all unknown attribute calls to the default table instance.
        """
        return getattr(self.table(self.default_table_name), name)

    # Here we forward magic methods to the default table instance. These are
    # not handled by __getattr__ so we need to forward them manually here

    def __len__(self):
        """
        Get the total number of documents in the default table.

        >>> db = TinyDB('db.json')
        >>> len(db)
        0
        """
        return len(self.table(self.default_table_name))

    def __iter__(self) -> Iterator[Document]:
        """
        Return an iterator for the default table's documents.
        """
        return iter(self.table(self.default_table_name))
