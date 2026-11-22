"""
Cross-table transactions for TinyDB.

TinyDB's storage interface reads and writes the *whole* database as a single
``dict`` of tables. This module builds on that property to provide atomic
transactions that span multiple tables (for example a ``devices`` table and
a ``parts`` table whose documents reference device IDs):

- A transaction stages operations (inserts, updates, removals) against any
  number of tables. Nothing it stages is visible to other readers of the
  database file until :meth:`Transaction.commit` succeeds.
- Committing applies all staged operations to the *current* committed state
  and writes the result as a single storage write, so after reopening the
  database you either see all of a transaction's changes or none of them.
- Document IDs for new documents are handed out by a shared, persisted
  allocator, so two transactions that are open at the same time never
  receive the same ID. Issued IDs are recorded for auditing.
- Every commit bumps a persisted revision counter and records the
  transaction in a journal. A :class:`Receipt` captures everything needed
  to check or re-apply a transaction later: if a commit's outcome was
  uncertain (e.g. the write was never confirmed),
  :meth:`TransactionManager.recommit` either confirms the earlier commit,
  applies it now, or raises
  :class:`ConflictError` when an ID has since been taken by different
  content -- it never re-attaches staged documents to someone else's ID.

Transaction metadata lives in a reserved table named ``_txmeta`` inside the
same database file, so it travels with the data and survives reopens.

The manager works with any storage, including middlewares such as
:class:`~tinydb.middlewares.CachingMiddleware`: pending cached writes are
flushed and cached reads invalidated around its critical sections, and every
commit is flushed so a confirmed commit is durable.
"""

import copy
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from typing import Optional

from .table import Document

__all__ = ('ConflictError', 'Receipt', 'Transaction', 'TransactionManager')

#: Name of the reserved table that stores transaction metadata.
META_TABLE = '_txmeta'


class ConflictError(RuntimeError):
    """
    Raised when a transaction cannot be applied because the database has
    changed in a conflicting way since the transaction was staged -- most
    notably when a document ID the transaction uses has been taken by
    different content in the meantime.
    """


class Receipt:
    """
    A durable record of a transaction.

    A receipt is available as soon as a transaction has staged operations
    (see :attr:`Transaction.receipt`) and can be serialized with
    :meth:`to_dict` to survive the process that created it. After an
    uncertain commit, :meth:`TransactionManager.recommit` uses the receipt
    to decide whether the transaction may still be applied.

    :ivar tx_id: The transaction's unique ID.
    :ivar operations: The staged operations (JSON-serializable).
    :ivar reserved_ids: Mapping of table name to the document IDs that the
        shared allocator issued to this transaction.
    :ivar status: ``'pending'``, ``'committed'`` or ``'aborted'``.
    :ivar revision: The database revision at which the transaction took
        effect, or ``None`` while it has not been committed.
    """

    def __init__(
        self,
        tx_id: str,
        operations: list[dict],
        reserved_ids: dict[str, list[int]],
        status: str = 'pending',
        revision: Optional[int] = None,
    ):
        self.tx_id = tx_id
        self.operations = copy.deepcopy(operations)
        self.reserved_ids = {
            table: list(ids) for table, ids in reserved_ids.items()
        }
        self.status = status
        self.revision = revision

    def __repr__(self):
        return (
            f'<{type(self).__name__} tx_id={self.tx_id!r} '
            f'status={self.status!r} revision={self.revision!r}>'
        )

    def to_dict(self) -> dict:
        """
        Serialize the receipt to a JSON-compatible ``dict``.
        """
        return {
            'tx_id': self.tx_id,
            'status': self.status,
            'revision': self.revision,
            'reserved_ids': copy.deepcopy(self.reserved_ids),
            'operations': copy.deepcopy(self.operations),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> 'Receipt':
        """
        Rebuild a receipt from :meth:`to_dict` output.
        """
        return cls(
            tx_id=data['tx_id'],
            operations=data['operations'],
            reserved_ids=data.get('reserved_ids', {}),
            status=data.get('status', 'pending'),
            revision=data.get('revision'),
        )


class Transaction:
    """
    A single cross-table transaction.

    Transactions are created with :meth:`TransactionManager.begin` or via
    :meth:`TransactionManager.transaction` / :meth:`TransactionManager.run`.
    Staged changes are private to the transaction until committed.
    """

    def __init__(self, manager: 'TransactionManager'):
        self._manager = manager
        self.tx_id = uuid.uuid4().hex
        self._operations: list[dict] = []
        self._reserved: dict[str, list[int]] = {}
        self._staged: dict[str, dict[int, dict]] = {}
        self._state = 'active'

    def _ensure_active(self):
        if self._state != 'active':
            raise RuntimeError(
                f'Transaction is {self._state}, no further operations '
                f'are allowed'
            )

    @property
    def receipt(self) -> Receipt:
        """
        A receipt capturing the operations staged so far.
        """
        status = {
            'active': 'pending',
            'committed': 'committed',
            'aborted': 'aborted',
        }[self._state]
        return Receipt(self.tx_id, self._operations, self._reserved, status)

    def reserve_id(self, table: str) -> int:
        """
        Reserve the next document ID for ``table`` without inserting yet.

        The ID is handed out by a shared, persisted allocator, so concurrent
        transactions never receive the same ID. Reserved IDs are burned even
        if the transaction is later rolled back; they remain visible in
        :meth:`TransactionManager.issued_references` for auditing.
        """
        self._ensure_active()
        doc_id = self._manager._reserve_id(table, self.tx_id)
        self._reserved.setdefault(table, []).append(doc_id)
        return doc_id

    def insert(
        self,
        table: str,
        document: Mapping,
        doc_id: Optional[int] = None,
    ) -> int:
        """
        Stage the insertion of ``document`` into ``table``.

        If ``doc_id`` is not given, a new ID is reserved from the shared
        allocator and returned immediately, so other staged documents (e.g.
        repair parts) can reference it right away.

        :returns: the document ID the document will be stored under
        """
        self._ensure_active()

        if not isinstance(document, Mapping):
            raise ValueError('Document is not a Mapping')

        if doc_id is None:
            doc_id = self.reserve_id(table)

        document = dict(document)
        self._operations.append({
            'op': 'insert',
            'table': table,
            'doc_id': doc_id,
            'document': document,
        })
        self._staged.setdefault(table, {})[doc_id] = dict(document)

        return doc_id

    def update(
        self,
        table: str,
        fields: Mapping,
        doc_ids: Iterable[int],
    ) -> list[int]:
        """
        Stage an update of the documents with the given IDs in ``table``.

        ``fields`` must be a ``Mapping`` (callables cannot be recorded on a
        receipt). Updating a document that does not exist when the
        transaction is committed raises :class:`ConflictError`.

        :returns: the list of targeted document IDs
        """
        self._ensure_active()

        if not isinstance(fields, Mapping):
            raise ValueError(
                'Transactional update requires a Mapping of fields so the '
                'operation can be recorded on a receipt'
            )

        doc_ids = [int(doc_id) for doc_id in doc_ids]
        fields = dict(fields)
        self._operations.append({
            'op': 'update',
            'table': table,
            'doc_ids': doc_ids,
            'fields': fields,
        })

        # Keep the read-your-writes view up to date
        staged = self._staged.setdefault(table, {})
        for doc_id in doc_ids:
            current = staged.get(doc_id)
            if current is None:
                existing = self.get(table, doc_id)
                current = dict(existing) if existing is not None else None
            if current is not None:
                current.update(fields)
                staged[doc_id] = current

        return doc_ids

    def remove(self, table: str, doc_ids: Iterable[int]) -> list[int]:
        """
        Stage the removal of the documents with the given IDs in ``table``.

        IDs that do not exist when the transaction is committed are silently
        skipped, mirroring :meth:`tinydb.table.Table.remove`.

        :returns: the list of targeted document IDs
        """
        self._ensure_active()

        doc_ids = [int(doc_id) for doc_id in doc_ids]
        self._operations.append({
            'op': 'remove',
            'table': table,
            'doc_ids': doc_ids,
        })

        staged = self._staged.get(table, {})
        for doc_id in doc_ids:
            staged.pop(doc_id, None)

        return doc_ids

    def get(self, table: str, doc_id: int) -> Optional[Document]:
        """
        Read a document as this transaction sees it (read-your-writes):
        staged changes are visible, everything else reflects the latest
        committed state.
        """
        staged = self._staged.get(table, {})
        if doc_id in staged:
            return Document(copy.deepcopy(staged[doc_id]), doc_id)

        data = self._manager._read_data()
        raw = data.get(table, {}).get(str(doc_id))
        if raw is None:
            return None

        return Document(copy.deepcopy(raw), doc_id)

    def commit(self) -> Receipt:
        """
        Atomically commit all staged operations.

        The operations are applied on top of the latest committed state, so
        changes committed by others in the meantime are preserved. The whole
        result is persisted with a single storage write (and flushed, if the
        storage supports it): after reopening the database, either all of
        this transaction's changes are visible or none are.

        :returns: a committed :class:`Receipt` recording the revision at
            which the transaction took effect
        """
        self._ensure_active()
        receipt = self._manager._commit(self.tx_id, self._operations,
                                        self._reserved)
        self._state = 'committed'
        return receipt

    def rollback(self) -> None:
        """
        Discard all staged operations. Reserved IDs stay burned and remain
        visible in the audit log.
        """
        self._ensure_active()
        self._state = 'aborted'


class TransactionManager:
    """
    Coordinates cross-table transactions for one :class:`~tinydb.TinyDB`
    instance.

    Multiple managers (each with its own ``TinyDB`` instance) may share the
    same database file: ID allocation and commits always operate on the
    latest committed state, so concurrently committed work by others is
    preserved. Note that -- like TinyDB itself -- this provides no
    inter-process locking; coordination is best-effort and conflicts are
    reported via :class:`ConflictError` instead of being silently resolved.

    :param db: The ``TinyDB`` instance to manage.
    """

    #: Name of the reserved table that stores transaction metadata
    meta_table = META_TABLE

    def __init__(self, db):
        self._db = db
        self._storage = db.storage

    # -- transaction lifecycle -------------------------------------------

    def begin(self) -> Transaction:
        """
        Start a new transaction.
        """
        return Transaction(self)

    @contextmanager
    def transaction(self):
        """
        Context manager for a transaction::

            with manager.transaction() as tx:
                device_id = tx.insert('devices', {'kind': 'pump'})
                tx.insert('parts', {'device_id': device_id})

        The transaction is committed when the block exits normally and
        rolled back when it raises.
        """
        tx = self.begin()
        try:
            yield tx
        except BaseException:
            tx.rollback()
            raise
        tx.commit()

    def run(self, callback) -> Receipt:
        """
        Run ``callback(tx)`` in a transaction and commit it.

        If the callback raises, the transaction is rolled back -- nothing it
        staged becomes visible -- and the exception propagates.

        :returns: the committed :class:`Receipt`
        """
        tx = self.begin()
        try:
            callback(tx)
        except BaseException:
            tx.rollback()
            raise
        return tx.commit()

    # -- receipts ---------------------------------------------------------

    def recommit(self, receipt: Receipt) -> Receipt:
        """
        Confirm or re-apply a transaction from its receipt.

        This is idempotent:

        - If the transaction is already recorded in the commit journal, the
          recorded revision is returned and nothing is changed.
        - Otherwise the receipt's operations are validated against the
          current state and applied atomically.
        - If a document ID used by the receipt has since been taken by
          *different* content, :class:`ConflictError` is raised and nothing
          is written -- staged documents are never re-attached to an ID that
          now belongs to someone else.
        """
        revision = self._commit_operations(receipt.tx_id, receipt.operations)
        return Receipt(receipt.tx_id, receipt.operations,
                       receipt.reserved_ids, 'committed', revision)

    def status(self, receipt) -> str:
        """
        Return ``'committed'`` if the receipt's transaction is recorded in
        the commit journal, else ``'pending'``.
        """
        tx_id = receipt.tx_id if isinstance(receipt, Receipt) else receipt
        meta = self._read_meta()
        return 'committed' if tx_id in meta.get('committed', {}) else 'pending'

    # -- auditing ---------------------------------------------------------

    def revision(self) -> int:
        """
        The revision of the latest committed transaction (0 if none).
        """
        return self._read_meta().get('revision', 0)

    def issued_references(self) -> list[dict]:
        """
        Audit log of all document IDs the shared allocator has handed out,
        as ``{'table': ..., 'doc_id': ..., 'tx_id': ...}`` entries.
        """
        return [dict(entry) for entry in self._read_meta().get('issued', [])]

    def committed_transactions(self) -> dict[str, dict]:
        """
        The commit journal: transaction ID -> ``{'revision': ...,
        'operations': ...}``.
        """
        return copy.deepcopy(self._read_meta().get('committed', {}))

    # -- internals --------------------------------------------------------

    def _commit(self, tx_id, operations, reserved) -> Receipt:
        revision = self._commit_operations(tx_id, operations)
        return Receipt(tx_id, operations, reserved, 'committed', revision)

    def _commit_operations(self, tx_id: str, operations: list[dict]) -> int:
        data = self._read_data()
        meta = data.setdefault(self.meta_table, {})
        committed = meta.setdefault('committed', {})

        if tx_id in committed:
            # Already committed: confirm the recorded revision
            return committed[tx_id]['revision']

        self._apply_operations(data, operations)

        revision = meta.get('revision', 0) + 1
        meta['revision'] = revision
        committed[tx_id] = {
            'revision': revision,
            'operations': copy.deepcopy(operations),
        }

        self._write_data(data)
        return revision

    def _reserve_id(self, table: str, tx_id: str) -> int:
        data = self._read_data()
        meta = data.setdefault(self.meta_table, {})
        next_ids = meta.setdefault('next_ids', {})

        existing_ids = []
        for key in data.get(table, {}):
            try:
                existing_ids.append(int(key))
            except (TypeError, ValueError):
                continue

        candidate = next_ids.get(table, 0)
        candidate = max(candidate, max(existing_ids, default=0) + 1)

        next_ids[table] = candidate + 1
        meta.setdefault('issued', []).append({
            'table': table,
            'doc_id': candidate,
            'tx_id': tx_id,
        })

        self._write_data(data)
        return candidate

    def _apply_operations(self, data: dict, operations: list[dict]) -> None:
        # Validate everything first, so a conflict leaves the database
        # untouched.
        for op in operations:
            table = data.get(op['table'], {})

            if op['op'] == 'insert':
                key = str(op['doc_id'])
                if key in table and table[key] != op['document']:
                    raise ConflictError(
                        f"Document ID {op['doc_id']} in table "
                        f"'{op['table']}' is already taken by different "
                        f"content; refusing to re-attach the transaction's "
                        f"staged documents to it"
                    )

            elif op['op'] == 'update':
                missing = [doc_id for doc_id in op['doc_ids']
                           if str(doc_id) not in table]
                if missing:
                    raise ConflictError(
                        f"Cannot update missing document IDs {missing} in "
                        f"table '{op['table']}'"
                    )

            elif op['op'] != 'remove':
                raise ValueError(f"Unknown operation: {op['op']!r}")

        for op in operations:
            table = data.setdefault(op['table'], {})

            if op['op'] == 'insert':
                table[str(op['doc_id'])] = copy.deepcopy(op['document'])

            elif op['op'] == 'update':
                for doc_id in op['doc_ids']:
                    table[str(doc_id)].update(copy.deepcopy(op['fields']))

            elif op['op'] == 'remove':
                for doc_id in op['doc_ids']:
                    table.pop(str(doc_id), None)

    # -- storage access ---------------------------------------------------

    def _read_meta(self) -> dict:
        return self._read_data().get(self.meta_table, {})

    def _read_data(self) -> dict:
        self._refresh_storage()
        return self._storage.read() or {}

    def _write_data(self, data: dict) -> None:
        self._storage.write(data)
        # Make the write durable even when a write cache (e.g.
        # CachingMiddleware) wraps the storage.
        flush = getattr(self._storage, 'flush', None)
        if callable(flush):
            flush()

    def _refresh_storage(self) -> None:
        # If the storage is wrapped in a write cache, persist its pending
        # writes and drop its read cache so we see what other instances have
        # committed to the underlying storage.
        flush = getattr(self._storage, 'flush', None)
        if callable(flush):
            flush()
        if hasattr(self._storage, 'cache'):
            self._storage.cache = None
