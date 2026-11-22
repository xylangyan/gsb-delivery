import pytest

from tinydb import TinyDB, JSONStorage
from tinydb.middlewares import CachingMiddleware
from tinydb.table import Document
from tinydb.transactions import (
    META_TABLE,
    ConflictError,
    Receipt,
    TransactionManager,
)


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / 'repo.json')


def open_repo(path, cached=False):
    storage = CachingMiddleware(JSONStorage) if cached else JSONStorage
    return TinyDB(path, storage=storage)


def seed_device_12(db):
    db.table('devices').insert(Document({'kind': 'valve', 'desc': ''}, 12))


class TestAtomicBackfill:
    def test_commit_is_atomic_and_visible_after_reopen(self, path):
        db = open_repo(path)
        mgr = TransactionManager(db)

        tx = mgr.begin()
        device_id = tx.insert('devices', {'kind': 'pump'})
        tx.insert('parts', {'device_id': device_id, 'name': 'seal'})
        tx.insert('parts', {'device_id': device_id, 'name': 'rotor'})

        # External readers only see committed data while the tx is open
        reader = open_repo(path)
        assert reader.table('devices').all() == []
        assert reader.table('parts').all() == []
        reader.close()

        receipt = tx.commit()
        assert receipt.status == 'committed'
        assert receipt.revision == 1
        db.close()

        # After reopening, both tables and their association are intact
        reopened = open_repo(path)
        devices = reopened.table('devices').all()
        parts = reopened.table('parts').all()
        assert [doc.doc_id for doc in devices] == [device_id]
        assert {part['device_id'] for part in parts} == {device_id}
        assert {part['name'] for part in parts} == {'seal', 'rotor'}
        reopened.close()

    def test_callback_exception_leaves_nothing_committed(self, path):
        db = open_repo(path)
        mgr = TransactionManager(db)

        def backfill(tx):
            device_id = tx.insert('devices', {'kind': 'pump'})
            tx.insert('parts', {'device_id': device_id, 'name': 'seal'})
            raise RuntimeError('callback failed')

        with pytest.raises(RuntimeError, match='callback failed'):
            mgr.run(backfill)
        db.close()

        reopened = open_repo(path)
        assert reopened.table('devices').all() == []
        assert reopened.table('parts').all() == []
        assert TransactionManager(reopened).revision() == 0
        reopened.close()

    def test_context_manager_rolls_back_on_exception(self, path):
        db = open_repo(path)
        mgr = TransactionManager(db)

        with pytest.raises(ValueError):
            with mgr.transaction() as tx:
                tx.insert('devices', {'kind': 'pump'})
                raise ValueError('boom')

        assert db.table('devices').all() == []
        db.close()

    def test_unconfirmed_write_then_receipt_recommit(self, path):
        db = open_repo(path)
        mgr = TransactionManager(db)

        tx = mgr.begin()
        device_id = tx.insert('devices', {'kind': 'pump'})
        tx.insert('parts', {'device_id': device_id, 'name': 'seal'})
        receipt = tx.receipt
        assert receipt.status == 'pending'

        # Simulate a commit whose write is never confirmed
        original_write = db.storage.write

        def failing_write(data):
            raise IOError('write not confirmed')

        db.storage.write = failing_write
        with pytest.raises(IOError):
            tx.commit()
        db.storage.write = original_write
        db.close()

        # Reopening shows the old state: nothing was committed
        reopened = open_repo(path)
        assert reopened.table('devices').all() == []
        assert reopened.table('parts').all() == []

        # The receipt can still be used to commit the very same transaction
        mgr2 = TransactionManager(reopened)
        assert mgr2.status(receipt) == 'pending'
        final = mgr2.recommit(receipt)
        assert final.status == 'committed'
        reopened.close()

        check = open_repo(path)
        devices = check.table('devices').all()
        assert [doc.doc_id for doc in devices] == [device_id]
        parts = check.table('parts').all()
        assert {part['device_id'] for part in parts} == {device_id}

        # Recommitting again is idempotent: same revision, no duplicates
        mgr3 = TransactionManager(check)
        again = mgr3.recommit(receipt)
        assert again.revision == final.revision
        assert mgr3.status(receipt) == 'committed'
        assert len(check.table('devices').all()) == 1
        assert len(check.table('parts').all()) == 1
        check.close()


class TestConcurrency:
    def test_concurrent_commits_preserve_each_other(self, path):
        db1 = open_repo(path)
        seed_device_12(db1)
        mgr1 = TransactionManager(db1)

        db2 = open_repo(path)
        mgr2 = TransactionManager(db2)

        # First backfill: reserve a device ID, reference it from parts
        tx1 = mgr1.begin()
        device_id_1 = tx1.insert('devices', {'kind': 'pump'})
        tx1.insert('parts', {'device_id': device_id_1, 'name': 'seal'})

        # Meanwhile a second user patches device 12 and claims a new ID
        tx2 = mgr2.begin()
        tx2.update('devices', {'desc': 'patched'}, doc_ids=[12])
        device_id_2 = tx2.insert('devices', {'kind': 'motor'})
        assert device_id_2 != device_id_1
        receipt2 = tx2.commit()

        # The first backfill commits afterwards
        receipt1 = tx1.commit()
        assert receipt1.revision != receipt2.revision
        db1.close()
        db2.close()

        reopened = open_repo(path)
        devices = reopened.table('devices')

        # The second user's confirmed update is still there
        assert devices.get(doc_id=12)['desc'] == 'patched'

        # Both new devices exist under their distinct reserved IDs
        assert devices.get(doc_id=device_id_1)['kind'] == 'pump'
        assert devices.get(doc_id=device_id_2)['kind'] == 'motor'

        # The parts reference the first backfill's device
        parts = reopened.table('parts').all()
        assert {part['device_id'] for part in parts} == {device_id_1}
        reopened.close()

    def test_concurrent_id_reservation_with_write_cache(self, path):
        db1 = open_repo(path, cached=True)
        db2 = open_repo(path, cached=True)
        mgr1 = TransactionManager(db1)
        mgr2 = TransactionManager(db2)

        tx1 = mgr1.begin()
        tx2 = mgr2.begin()
        id1 = tx1.reserve_id('devices')
        id2 = tx2.reserve_id('devices')
        assert id1 != id2

        tx1.insert('devices', {'kind': 'pump'}, doc_id=id1)
        tx2.insert('devices', {'kind': 'motor'}, doc_id=id2)
        tx2.commit()
        tx1.commit()
        db1.close()
        db2.close()

        reopened = open_repo(path)
        assert reopened.table('devices').get(doc_id=id1)['kind'] == 'pump'
        assert reopened.table('devices').get(doc_id=id2)['kind'] == 'motor'
        reopened.close()


class TestConflicts:
    def test_recommit_conflict_when_id_taken(self, path):
        db = open_repo(path)
        mgr = TransactionManager(db)

        # Stage a backfill but never commit it (e.g. the process died)
        tx = mgr.begin()
        device_id = tx.insert('devices', {'kind': 'pump'})
        tx.insert('parts', {'device_id': device_id, 'name': 'seal'})
        receipt = tx.receipt
        tx.rollback()

        # Someone else takes the ID via plain per-table work
        db.table('devices').insert(Document({'kind': 'motor'}, device_id))

        # Replaying the receipt must fail loudly ...
        with pytest.raises(ConflictError, match='already taken'):
            mgr.recommit(receipt)
        db.close()

        # ... and must not re-attach the old parts to the new device
        reopened = open_repo(path)
        devices = reopened.table('devices').all()
        assert len(devices) == 1
        assert devices[0]['kind'] == 'motor'
        assert reopened.table('parts').all() == []
        reopened.close()

    def test_update_of_missing_document_conflicts(self, path):
        db = open_repo(path)
        mgr = TransactionManager(db)

        tx = mgr.begin()
        tx.update('devices', {'desc': 'x'}, doc_ids=[99])
        with pytest.raises(ConflictError, match='missing'):
            tx.commit()
        db.close()


class TestCachingMiddleware:
    def test_commit_is_flushed_through_write_cache(self, path):
        db = open_repo(path, cached=True)
        mgr = TransactionManager(db)

        with mgr.transaction() as tx:
            device_id = tx.insert('devices', {'kind': 'pump'})
            tx.insert('parts', {'device_id': device_id, 'name': 'seal'})

        # A reader going straight to the underlying file sees the commit
        # even though the writer's cache has not reached its flush threshold
        reader = open_repo(path)
        assert reader.table('devices').get(doc_id=device_id) is not None
        assert len(reader.table('parts').all()) == 1
        reader.close()
        db.close()

    def test_unflushed_cache_crash_shows_nothing_committed(self, path):
        # If the process dies before a cached write is flushed, reopening
        # shows the state without the transaction -- never a partial one.
        db = open_repo(path, cached=True)
        mgr = TransactionManager(db)

        tx = mgr.begin()
        device_id = tx.insert('devices', {'kind': 'pump'})
        tx.insert('parts', {'device_id': device_id, 'name': 'seal'})
        receipt = tx.receipt

        # Commit writes only into the cache; simulate a crash before the
        # flush by failing the underlying storage write.
        def failing_flush():
            raise IOError('crash before flush')

        db.storage.flush = failing_flush
        with pytest.raises(IOError):
            tx.commit()

        reopened = open_repo(path)
        assert reopened.table('devices').all() == []
        assert reopened.table('parts').all() == []

        # The receipt still commits the whole transaction atomically
        final = TransactionManager(reopened).recommit(receipt)
        assert final.status == 'committed'
        assert reopened.table('devices').get(doc_id=device_id) is not None
        parts = reopened.table('parts').all()
        assert {p['device_id'] for p in parts} == {device_id}
        reopened.close()


class TestAuditAndInterop:
    def test_plain_table_api_works_with_actual_ids(self, path):
        db = open_repo(path)
        mgr = TransactionManager(db)

        receipt = mgr.run(lambda tx: tx.insert('devices', {'kind': 'pump'}))
        device_id = receipt.reserved_ids['devices'][0]

        # Existing per-table work keeps looking documents up by actual ID
        assert db.table('devices').get(doc_id=device_id)['kind'] == 'pump'
        assert db.table('devices').contains(doc_id=device_id)
        db.table('devices').insert({'kind': 'spare'})
        assert len(db.table('devices').all()) == 2
        db.close()

    def test_issued_references_revision_and_journal(self, path):
        db = open_repo(path)
        mgr = TransactionManager(db)

        assert mgr.revision() == 0
        assert mgr.issued_references() == []

        tx = mgr.begin()
        device_id = tx.insert('devices', {'kind': 'pump'})
        part_id = tx.insert('parts', {'device_id': device_id, 'name': 'seal'})
        receipt = tx.commit()

        # Which references were handed out, and to which transaction
        issued = mgr.issued_references()
        assert {(e['table'], e['doc_id']) for e in issued} == {
            ('devices', device_id), ('parts', part_id),
        }
        assert all(e['tx_id'] == receipt.tx_id for e in issued)

        # Which revision actually took effect
        assert mgr.revision() == receipt.revision == 1
        journal = mgr.committed_transactions()
        assert journal[receipt.tx_id]['revision'] == 1
        db.close()

        # The audit trail survives a reopen
        reopened = open_repo(path)
        mgr2 = TransactionManager(reopened)
        assert mgr2.revision() == 1
        assert len(mgr2.issued_references()) == 2
        assert mgr2.status(receipt) == 'committed'
        reopened.close()

    def test_receipt_serialization_roundtrip(self, path):
        db = open_repo(path)
        mgr = TransactionManager(db)

        tx = mgr.begin()
        device_id = tx.insert('devices', {'kind': 'pump'})
        tx.insert('parts', {'device_id': device_id, 'name': 'seal'})
        receipt = tx.receipt
        tx.rollback()

        restored = Receipt.from_dict(receipt.to_dict())
        assert restored.tx_id == receipt.tx_id
        assert restored.operations == receipt.operations
        assert restored.reserved_ids == receipt.reserved_ids

        final = mgr.recommit(restored)
        assert final.status == 'committed'
        assert db.table('devices').get(doc_id=device_id) is not None
        db.close()

    def test_meta_table_travels_with_the_data(self, path):
        db = open_repo(path)
        mgr = TransactionManager(db)
        mgr.run(lambda tx: tx.insert('devices', {'kind': 'pump'}))

        assert META_TABLE in db.tables()
        db.close()
