"""Tests for incremental, receipted cross-database delivery."""

import os
import shutil
import tempfile
import unittest

import transaction
from persistent import Persistent
from persistent.mapping import PersistentMapping
from persistent.wref import WeakRef

import ZODB
import ZODB.FileStorage
from ZODB.batch import Delivery
from ZODB.batch import Group
from ZODB.batch import MissingReference
from ZODB.batch import destination_oid
from ZODB.utils import p64


class Record(Persistent):
    pass


class Material(Persistent):
    pass


class DeliveryTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='ZODB-batch-')
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.sources = {}
        self.database_names = ('records', 'materials')

    def tearDown(self):
        for database in self.sources.values():
            database.close()
        self.sources.clear()

    def source(self, name):
        if name not in self.sources:
            path = os.path.join(self.directory, 'legacy-%s.fs' % name)
            self.sources[name] = ZODB.DB(
                ZODB.FileStorage.FileStorage(path, create=True),
                database_name=name,
                databases=self.sources,
            )
        return self.sources[name]

    def delivery(self, **kwargs):
        sources = {
            name: os.path.join(self.directory, 'legacy-%s.fs' % name)
            for name in self.database_names
        }
        kwargs.setdefault('group_dir', os.path.join(self.directory, 'out'))
        delivery = Delivery(sources, **kwargs)
        self.addCleanup(delivery.close)
        return delivery

    def build_legacy(self):
        records = self.source('records')
        self.source('materials')
        conn = records.open()
        materials = conn.get_connection('materials')

        material_17 = Material()
        material_17.label = 'material-17'
        materials.root()['17'] = material_17

        # The records database has an unrelated object created before the
        # material, so both sources hold small numeric oids of their own.
        same_oid_record = Record()
        same_oid_record.kind = 'record-17'
        conn.add(same_oid_record)
        self.assertEqual(len(same_oid_record._p_oid), 8)

        material_22 = Material()
        material_22.label = 'material-22'
        materials.root()['22'] = material_22
        transaction.commit()

        record_a = Record()
        record_a.text = '物料库/17'
        record_a.material = material_17
        record_a.weak = WeakRef(material_22)
        conn.root()['A'] = record_a
        transaction.commit()

        # The material points back at the record, creating the cycle.
        material_17.record = record_a
        transaction.commit()

        record_b = Record()
        record_b.text = 'B'
        record_b.material = material_22
        conn.root()['B'] = record_b
        transaction.commit()

        record_u = Record()
        record_u.text = 'U'
        conn.root()['U'] = record_u
        transaction.commit()
        return record_a, material_17, material_22, record_b, record_u

    def delete_record(self, database, oid):
        """Delete one current record from the legacy source."""
        database_object = self.source(database)
        database_object.close()
        self.sources.pop(database)
        path = os.path.join(self.directory, 'legacy-%s.fs' % database)
        storage = ZODB.FileStorage.FileStorage(path)
        transaction = _StorageTransaction()
        try:
            storage.tpc_begin(transaction)
            _data, serial = storage.load(oid)
            storage.deleteObject(oid, serial, transaction)
            storage.tpc_vote(transaction)
            storage.tpc_finish(transaction)
        finally:
            storage.close()

    # -- first slip ---------------------------------------------------------

    def test_first_batch_delivers_cycle_and_keeps_weakref_empty(self):
        record_a, material_17, material_22, _b, _u = self.build_legacy()
        delivery = self.delivery()
        receipt = delivery.release('slip-1', [
            ('records', record_a._p_oid),
            ('materials', material_17._p_oid),
        ])

        self.assertTrue(receipt['complete'])
        self.assertIn('records/%s' % record_a._p_oid.hex(),
                      receipt['roots'])
        self.assertIn('materials/%s' % material_17._p_oid.hex(),
                      receipt['roots'])

        with delivery.open_group() as group:
            a = group.source('records', record_a._p_oid)
            m17 = group.source('materials', material_17._p_oid)
            self.assertEqual(a.text, '物料库/17')
            self.assertIsInstance(a.text, str)
            self.assertIs(a.material, m17)
            self.assertIs(m17.record, a)
            self.assertIsInstance(a.weak, WeakRef)
            self.assertIsNone(a.weak())
            self.assertIsNone(group.source('materials', material_22._p_oid))

    def test_same_numeric_oid_in_two_databases_has_two_identities(self):
        record_a, material_17, _m22, _b, _u = self.build_legacy()
        delivery = self.delivery()
        receipt = delivery.release('slip-1', [
            ('records', record_a._p_oid),
            ('materials', material_17._p_oid),
        ])
        materials_target = receipt['roots'][
            'materials/%s' % material_17._p_oid.hex()]['target']
        records_target = receipt['roots'][
            'records/%s' % record_a._p_oid.hex()]['target']
        self.assertNotEqual(materials_target[0], records_target[0])
        self.assertEqual(materials_target[1],
                         destination_oid('materials',
                                         material_17._p_oid).hex())

    # -- later slip ---------------------------------------------------------

    def test_later_batch_makes_earlier_weakref_resolve(self):
        record_a, material_17, material_22, record_b, _u = self.build_legacy()
        delivery = self.delivery()
        first = delivery.release('slip-1', [
            ('records', record_a._p_oid),
            ('materials', material_17._p_oid),
        ])
        second = delivery.release('slip-2', [
            ('records', record_b._p_oid),
            ('materials', material_22._p_oid),
        ])
        self.assertTrue(second['complete'])

        with delivery.open_group() as group:
            client = group.client()
            records = client.get_connection('records')
            materials = client.get_connection('materials')
            a = records.get(
                group.target('records', record_a._p_oid)[1])
            b = records.get(
                group.target('records', record_b._p_oid)[1])
            m22 = materials.get(
                group.target('materials', material_22._p_oid)[1])
            self.assertIsInstance(a.weak, WeakRef)
            self.assertIs(a.weak(), m22)
            self.assertIs(a.weak(), b.material)
            self.assertEqual(m22.label, 'material-22')
            self.assertEqual(
                first['roots']['records/%s' % record_a._p_oid.hex()]
                ['target'],
                [delivery.destinations['records'],
                 destination_oid('records', record_a._p_oid).hex()])

    def test_redelivery_returns_recorded_identities(self):
        record_a, material_17, _m22, _b, _u = self.build_legacy()
        delivery = self.delivery()
        first = delivery.release('slip-1', [
            ('records', record_a._p_oid),
            ('materials', material_17._p_oid),
        ])
        again = delivery.release('slip-1', [
            ('records', record_a._p_oid),
            ('materials', material_17._p_oid),
        ])
        self.assertEqual(first, again)

        reopened = self.delivery().receipt('slip-1')
        self.assertEqual(first, reopened)

        with delivery.open_group() as group:
            self.assertEqual(
                group.target('records', record_a._p_oid)[1],
                destination_oid('records', record_a._p_oid))
            self.assertEqual(
                group.target('materials', material_17._p_oid)[1],
                destination_oid('materials', material_17._p_oid))

    # -- missing strong dependencies ---------------------------------------

    def test_unreleased_strong_dependency_is_reported(self):
        record_a, material_17, _m22, record_b, _u = self.build_legacy()
        conn = self.source('records').open()
        conn.root()['A'].other = conn.root()['B']
        transaction.commit()
        conn.close()
        self.delete_record('records', record_b._p_oid)

        delivery = self.delivery()
        receipt = delivery.release('slip-1', [
            ('records', record_a._p_oid),
            ('materials', material_17._p_oid),
        ])
        key = 'records/%s' % record_a._p_oid.hex()
        self.assertIn(key, receipt['missing'])
        self.assertEqual(receipt['missing'][key][0]['source'],
                         ['records', record_b._p_oid.hex()])

        with delivery.open_group() as group:
            delivered = group.source('records', record_a._p_oid)
            self.assertIsNone(delivered.other)

    def test_other_batches_continue_after_missing_dependency(self):
        record_a, material_17, _m22, record_b, record_u = self.build_legacy()
        conn = self.source('records').open()
        conn.root()['A'].other = conn.root()['B']
        transaction.commit()
        conn.close()
        self.delete_record('records', record_b._p_oid)

        delivery = self.delivery()
        delivery.release('slip-1', [
            ('records', record_a._p_oid),
            ('materials', material_17._p_oid),
        ])
        receipt_u = delivery.release('slip-u', [
            ('records', record_u._p_oid),
        ])
        self.assertTrue(receipt_u['complete'])

        with delivery.open_group() as group:
            self.assertEqual(
                group.source('records', record_u._p_oid).text, 'U')
            self.assertIsNone(group.source('records', record_b._p_oid))

    def test_unknown_root_fails(self):
        self.source('records')
        delivery = self.delivery()
        with self.assertRaises(MissingReference):
            delivery.release('slip-x', [('records', p64(987654))])

    # -- crash and recovery -------------------------------------------------

    def test_crash_before_publish_leaves_previous_group(self):
        record_a, material_17, _m22, _b, _u = self.build_legacy()
        delivery = self.delivery()

        def fail(stage, *details):
            if stage == 'before-publish':
                raise RuntimeError('crash')

        delivery.inject = fail
        with self.assertRaises(RuntimeError):
            delivery.release('slip-1', [('records', record_a._p_oid)])

        delivery.inject = None
        self.assertIsNone(delivery.receipt('slip-1'))
        with delivery.open_group() as group:
            self.assertIsNone(group.source('records', record_a._p_oid))
        receipt = delivery.release('slip-1', [('records', record_a._p_oid)])
        self.assertTrue(receipt['complete'])

    def test_crash_after_publish_keeps_receipt(self):
        record_a, material_17, _m22, _b, _u = self.build_legacy()
        delivery = self.delivery()

        def fail(stage, *details):
            if stage == 'after-publish':
                raise RuntimeError('response lost')

        delivery.inject = fail
        with self.assertRaises(RuntimeError):
            delivery.release('slip-1', [('records', record_a._p_oid)])
        delivery.inject = None

        with delivery.open_group() as group:
            self.assertIsNotNone(group.source('records', record_a._p_oid))
            self.assertIsNotNone(group.receipt('slip-1'))
        receipt = delivery.release('slip-1', [('records', record_a._p_oid)])
        self.assertTrue(receipt['complete'])
        self.assertEqual(len(receipt['roots']), 1)

    def test_partial_material_write_is_invisible_until_publish(self):
        record_a, material_17, material_22, record_b, record_u = \
            self.build_legacy()
        delivery = self.delivery()
        delivery.release('slip-1', [
            ('records', record_a._p_oid),
            ('materials', material_17._p_oid),
        ])

        def fail(stage, *details):
            # The materials database of slip-2 is fully written, but the
            # records database is not; then the process stops.
            if stage == 'after-write' and details[0] == 'materials':
                raise RuntimeError('power loss')

        delivery.inject = fail
        with self.assertRaises(RuntimeError):
            delivery.release('slip-2', [
                ('records', record_b._p_oid),
                ('materials', material_22._p_oid),
            ])
        delivery.inject = None

        # Reopening the delivered group shows the previous generation:
        # the weak reference is still empty and B is absent.
        with delivery.open_group() as group:
            a = group.source('records', record_a._p_oid)
            self.assertIsInstance(a.weak, WeakRef)
            self.assertIsNone(a.weak())
            self.assertIsNone(group.source('records', record_b._p_oid))
            self.assertIsNone(
                group.source('materials', material_22._p_oid))

        # Before B is resent, an unrelated slip is signed in.
        receipt_u = delivery.release('slip-u', [
            ('records', record_u._p_oid),
        ])
        self.assertTrue(receipt_u['complete'])

        # Resending B produces the same identities and the weak reference
        # now resolves to the same object as B's strong reference.
        second = delivery.release('slip-2', [
            ('records', record_b._p_oid),
            ('materials', material_22._p_oid),
        ])
        self.assertTrue(second['complete'])

        with delivery.open_group() as group:
            client = group.client()
            records = client.get_connection('records')
            materials = client.get_connection('materials')
            a = records.get(group.target('records', record_a._p_oid)[1])
            b = records.get(group.target('records', record_b._p_oid)[1])
            m22 = materials.get(
                group.target('materials', material_22._p_oid)[1])
            u = records.get(group.target('records', record_u._p_oid)[1])
            self.assertIsInstance(a.weak, WeakRef)
            self.assertIs(a.weak(), m22)
            self.assertIs(a.weak(), b.material)
            self.assertEqual(u.text, 'U')

    def test_group_reopens_with_ordinary_connections(self):
        record_a, material_17, _m22, _b, _u = self.build_legacy()
        delivery = self.delivery()
        delivery.release('slip-1', [
            ('records', record_a._p_oid),
            ('materials', material_17._p_oid),
        ])

        group = Group(os.path.join(self.directory, 'out'))
        self.addCleanup(group.close)
        databases = group.databases
        self.assertEqual(sorted(databases),
                         ['__delivery__', 'materials', 'records'])
        for database in databases.values():
            self.assertTrue(database.storage.isReadOnly())
        connection = group.connection('records')
        root = connection.root()
        entry = root['records/%s' % record_a._p_oid.hex()]
        self.assertIsInstance(entry, Record)
        self.assertIs(entry.material._p_jar.db(), databases['materials'])
        self.assertIsInstance(entry.weak, WeakRef)

    def test_identity_from_first_receipt_is_stable_after_later_slips(self):
        record_a, material_17, material_22, record_b, _u = self.build_legacy()
        delivery = self.delivery()
        first = delivery.release('slip-1', [
            ('records', record_a._p_oid),
            ('materials', material_17._p_oid),
        ])
        delivery.release('slip-2', [
            ('records', record_b._p_oid),
            ('materials', material_22._p_oid),
        ])
        again = delivery.release('slip-1', [
            ('records', record_a._p_oid),
            ('materials', material_17._p_oid),
        ])
        self.assertEqual(first, again)
        with delivery.open_group() as group:
            self.assertEqual(
                group.target('records', record_a._p_oid),
                (delivery.destinations['records'],
                 destination_oid('records', record_a._p_oid)))

    def test_redelivered_published_batch_keeps_single_identity(self):
        record_a, _m17, material_22, record_b, _u = self.build_legacy()
        delivery = self.delivery()

        def fail(stage, *details):
            if stage == 'after-publish':
                raise RuntimeError('response lost')

        delivery.inject = fail
        with self.assertRaises(RuntimeError):
            delivery.release('slip-2', [
                ('records', record_b._p_oid),
                ('materials', material_22._p_oid),
            ])
        delivery.inject = None

        published = delivery.receipt('slip-2')
        again = delivery.release('slip-2', [
            ('records', record_b._p_oid),
            ('materials', material_22._p_oid),
        ])
        self.assertEqual(published, again)
        with delivery.open_group() as group:
            self.assertEqual(
                len([key for key in group.ledger()['identities']
                     if key.startswith('records/%s' %
                                       record_b._p_oid.hex())]), 1)
            records = group.connection('records')
            b = records.get(group.target('records', record_b._p_oid)[1])
            m22 = records.get_connection('materials').get(
                group.target('materials', material_22._p_oid)[1])
            self.assertIs(b.material, m22)


class _StorageTransaction:

    user = 'tests'
    description = 'delete record'
    extension_bytes = b''


if __name__ == '__main__':
    unittest.main()


class RootPersistenceTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='ZODB-batch-root-')
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_earlier_root_stays_a_persistent_reference(self):
        source = os.path.join(self.directory, 'legacy.fs')
        db = ZODB.DB(ZODB.FileStorage.FileStorage(source, create=True),
                     database_name='records', databases={})
        connection = db.open()
        first = Record()
        first.text = 'first'
        connection.root()['first'] = first
        second = Record()
        second.text = 'second'
        connection.root()['second'] = second
        transaction.commit()
        first_oid, second_oid = first._p_oid, second._p_oid
        db.close()

        delivery = Delivery({'records': source},
                            group_dir=os.path.join(self.directory, 'out'))
        self.addCleanup(delivery.close)
        delivery.release('slip-1', [('records', first_oid)])
        delivery.release('slip-2', [('records', second_oid)])

        group = Group.open(os.path.join(self.directory, 'out'))
        self.addCleanup(group.close)
        connection = group.connection('records')
        root = connection.root()
        self.assertEqual(root['records/%s' % first_oid.hex()].text,
                         'first')
        self.assertEqual(root['records/%s' % second_oid.hex()].text,
                         'second')

    def test_generation_files_are_swept(self):
        source = os.path.join(self.directory, 'legacy.fs')
        db = ZODB.DB(ZODB.FileStorage.FileStorage(source, create=True),
                     database_name='records', databases={})
        connection = db.open()
        record = Record()
        connection.root()['r'] = record
        transaction.commit()
        oid = record._p_oid
        db.close()

        delivery = Delivery({'records': source},
                            group_dir=os.path.join(self.directory, 'out'))
        self.addCleanup(delivery.close)
        delivery.release('slip-1', [('records', oid)])
        delivery.release('slip-2', [('records', oid)])
        files = sorted(name for name in
                       os.listdir(os.path.join(self.directory, 'out'))
                       if name.endswith('.fs'))
        self.assertEqual(files,
                         ['__delivery__-2.fs', 'records-2.fs'])


class MissingReportTests(unittest.TestCase):

    def test_missing_link_is_readable_under_the_root(self):
        directory = tempfile.mkdtemp(prefix='ZODB-batch-missing-')
        self.addCleanup(shutil.rmtree, directory, True)
        source = os.path.join(directory, 'legacy.fs')
        db = ZODB.DB(ZODB.FileStorage.FileStorage(source, create=True),
                     database_name='records', databases={})
        connection = db.open()
        keep = Record()
        keep.text = 'keep'
        gone = Record()
        gone.text = 'gone'
        connection.root()['keep'] = keep
        connection.root()['gone'] = gone
        transaction.commit()
        keep_oid, gone_oid = keep._p_oid, gone._p_oid
        keep.other = gone
        transaction.commit()
        db.close()

        storage = ZODB.FileStorage.FileStorage(source)
        storage_transaction = _StorageTransaction()
        try:
            storage.tpc_begin(storage_transaction)
            _data, serial = storage.load(gone_oid)
            storage.deleteObject(gone_oid, serial, storage_transaction)
            storage.tpc_vote(storage_transaction)
            storage.tpc_finish(storage_transaction)
        finally:
            storage.close()

        delivery = Delivery({'records': source},
                            group_dir=os.path.join(directory, 'out'))
        self.addCleanup(delivery.close)
        receipt = delivery.release('slip-1', [('records', keep_oid)])
        root_key = 'records/%s' % keep_oid.hex()
        self.assertEqual(receipt['missing'][root_key][0]['source'],
                         ['records', gone_oid.hex()])

        group = Group.open(os.path.join(directory, 'out'))
        self.addCleanup(group.close)
        root = group.connection('records').root()
        report = root['missing/%s' % root_key]
        self.assertEqual(report['root'], root_key)
        self.assertEqual(report['links'][0]['source'],
                         ['records', gone_oid.hex()])


class ProcessKillTests(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='ZODB-batch-kill-')
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.source_path = os.path.join(self.directory, 'legacy.fs')
        self.group_dir = os.path.join(self.directory, 'out')

    def _source(self):
        db = ZODB.DB(
            ZODB.FileStorage.FileStorage(self.source_path, create=True),
            database_name='records', databases={})
        connection = db.open()
        return db, connection

    def test_process_kill_during_second_slip(self):
        import subprocess
        import sys
        import textwrap

        db, connection = self._source()
        first = Record()
        first.text = 'A'
        second = Record()
        second.text = 'B'
        connection.root()['A'] = first
        connection.root()['B'] = second
        transaction.commit()
        first_oid, second_oid = first._p_oid, second._p_oid
        db.close()

        delivery = Delivery({'records': self.source_path},
                            group_dir=self.group_dir)
        self.addCleanup(delivery.close)
        delivery.release('slip-1', [('records', first_oid)])

        script = textwrap.dedent('''
            import os, sys
            sys.path.insert(0, %r)
            from ZODB.batch import Delivery

            def hard_stop(stage, *details):
                if stage == "after-write":
                    os._exit(17)

            delivery = Delivery({"records": %r}, group_dir=%r,
                                inject=hard_stop)
            delivery.release("slip-2", [("records", bytes.fromhex(%r))])
            os._exit(0)
        ''') % (os.path.abspath('src'), self.source_path, self.group_dir,
                second_oid.hex())
        result = subprocess.run([sys.executable, '-c', script])
        self.assertEqual(result.returncode, 17)

        # The previous generation is still the published one: slip-2 has no
        # receipt and its object is invisible.
        reopened = Delivery({'records': self.source_path},
                            group_dir=self.group_dir)
        self.addCleanup(reopened.close)
        self.assertIsNone(reopened.receipt('slip-2'))
        with reopened.open_group() as group:
            self.assertEqual(
                group.source('records', first_oid).text, 'A')
            self.assertIsNone(group.source('records', second_oid))

        receipt = reopened.release('slip-2', [('records', second_oid)])
        self.assertTrue(receipt['complete'])
        with reopened.open_group() as group:
            self.assertEqual(
                group.source('records', second_oid).text, 'B')


class ReceiptMirrorTests(unittest.TestCase):

    def test_receipt_mirror_and_ledger_agree(self):
        import json
        directory = tempfile.mkdtemp(prefix='ZODB-batch-mirror-')
        self.addCleanup(shutil.rmtree, directory, True)
        source = os.path.join(directory, 'legacy.fs')
        db = ZODB.DB(ZODB.FileStorage.FileStorage(source, create=True),
                     database_name='records', databases={})
        connection = db.open()
        record = Record()
        connection.root()['r'] = record
        transaction.commit()
        oid = record._p_oid
        db.close()

        group_dir = os.path.join(directory, 'out')
        delivery = Delivery({'records': source}, group_dir=group_dir)
        self.addCleanup(delivery.close)
        receipt = delivery.release('slip-1', [('records', oid)])

        with open(os.path.join(group_dir, 'RECEIPTS.json')) as file:
            mirror = json.load(file)
        self.assertEqual(mirror['slip-1'], receipt)

        group = Group.open(group_dir)
        self.addCleanup(group.close)
        self.assertEqual(group.receipt('slip-1'), receipt)
        self.assertEqual(group.receipts(), mirror)
        with open(os.path.join(group_dir, 'IDENTITIES.json')) as file:
            identities = json.load(file)
        self.assertEqual(group.identities(), identities)


class DestinationTests(unittest.TestCase):

    def test_destination_can_be_renamed(self):
        directory = tempfile.mkdtemp(prefix='ZODB-batch-dest-')
        self.addCleanup(shutil.rmtree, directory, True)
        source = os.path.join(directory, 'legacy.fs')
        db = ZODB.DB(ZODB.FileStorage.FileStorage(source, create=True),
                     database_name='records', databases={})
        connection = db.open()
        record = Record()
        connection.root()['r'] = record
        transaction.commit()
        oid = record._p_oid
        db.close()

        delivery = Delivery({'records': source},
                            destinations={'records': 'archive'},
                            group_dir=os.path.join(directory, 'out'))
        self.addCleanup(delivery.close)
        receipt = delivery.release('slip-1', [('records', oid)])
        self.assertEqual(receipt['roots']['records/%s' % oid.hex()]
                         ['target'][0], 'archive')

        with delivery.open_group() as group:
            self.assertEqual(sorted(group.names),
                             ['__delivery__', 'archive'])
            self.assertEqual(group.source('records', oid)._p_oid,
                             destination_oid('records', oid))


class RepeatedRootTests(unittest.TestCase):

    def test_later_slip_may_repeat_an_earlier_root(self):
        directory = tempfile.mkdtemp(prefix='ZODB-batch-repeat-')
        self.addCleanup(shutil.rmtree, directory, True)
        source = os.path.join(directory, 'legacy.fs')
        db = ZODB.DB(ZODB.FileStorage.FileStorage(source, create=True),
                     database_name='records', databases={})
        connection = db.open()
        first = Record()
        second = Record()
        connection.root()['first'] = first
        connection.root()['second'] = second
        transaction.commit()
        first_oid, second_oid = first._p_oid, second._p_oid
        db.close()

        delivery = Delivery({'records': source},
                            group_dir=os.path.join(directory, 'out'))
        self.addCleanup(delivery.close)
        first_receipt = delivery.release(
            'slip-1', [('records', first_oid)])
        second_receipt = delivery.release(
            'slip-2', [('records', first_oid), ('records', second_oid)])

        key = 'records/%s' % first_oid.hex()
        self.assertEqual(first_receipt['roots'][key],
                         second_receipt['roots'][key])
        self.assertEqual(
            second_receipt['roots'][key]['target'][1],
            destination_oid('records', first_oid).hex())

        with delivery.open_group() as group:
            self.assertEqual(
                len([name for name in group.identities()
                     if name == key]), 1)
