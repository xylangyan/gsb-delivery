#! /usr/bin/env python

import unittest

import pyslet.odata2.csdl as edm
import pyslet.odata2.edmx as edmx

from pyslet.odata2 import memds
from pyslet.py2 import ul
from pyslet.vfs import OSFilePath as FilePath

from test_odata2_core import DataServiceRegressionTests


def suite():
    loader = unittest.TestLoader()
    loader.testMethodPrefix = 'test'
    return unittest.TestSuite((
        loader.loadTestsFromTestCase(MemDSTests),
        loader.loadTestsFromTestCase(BatchImportTests),
        loader.loadTestsFromTestCase(RegressionTests)
    ))


def load_tests(loader, tests, pattern):
    return suite()


TEST_DATA_DIR = FilePath(
    FilePath(__file__).abspath().split()[0], 'data_odatav2')


class MemDSTests(unittest.TestCase):

    def setUp(self):        # noqa
        self.doc = edmx.Document()
        mdpath = TEST_DATA_DIR.join('sample_server', 'metadata.xml')
        with mdpath.open('rb') as f:
            self.doc.read(f)
        self.schema = self.doc.root.DataServices['SampleModel']
        self.containerDef = self.doc.root.DataServices[
            "SampleModel.SampleEntities"]
        self.container = memds.InMemoryEntityContainer(self.containerDef)
        self.employees = self.container.entityStorage['Employees']

    def tearDown(self):     # noqa
        pass

    def test_constructors(self):
        es = self.schema['SampleEntities.Employees']
        self.assertTrue(isinstance(es.open(), memds.EntityCollection))

    def test_length(self):
        es = self.schema['SampleEntities.Employees']
        self.assertTrue(isinstance(es, edm.EntitySet))
        with es.open() as collection:
            self.assertTrue(len(collection) == 0, "Length on load")
            self.employees.data["ABCDE"] = (
                ul("ABCDE"), ul("John Smith"), None, None)
            self.assertTrue(len(collection) == 1, "Length after insert")
            self.employees.data["FGHIJ"] = (
                ul("FGHIJ"), ul("Jane Smith"), None, None)
            self.assertTrue(len(collection) == 2, "Length after 2xinsert")
            del collection["ABCDE"]
            self.assertTrue(len(collection) == 1, "Length after delete")

    def test_entity_set_data(self):
        self.employees.data["ABCDE"] = (ul("ABCDE"), ul("John Smith"), None,
                                        None)
        self.employees.data["FGHIJ"] = (ul("FGHIJ"), ul("Jane Smith"), None,
                                        None)


class BatchImportTests(unittest.TestCase):

    """Acceptance tests for whole-file, atomic batch imports.

    The model binds Products to Categories with a *-1 association
    (every product requires a category), categories carry a compound
    key and both navigation properties are declared.  Suppliers deliver
    products before categories so forward references inside one file
    must resolve at commit time."""

    def setUp(self):        # noqa
        self.doc = edmx.Document()
        mdpath = TEST_DATA_DIR.join('sample_server', 'batch_import.xml')
        with mdpath.open('rb') as f:
            self.doc.read(f)
        self.containerDef = self.doc.root.DataServices[
            'Shop.ShopContainer']
        self.products = self.containerDef['Products']
        self.categories = self.containerDef['Categories']
        self.container = memds.InMemoryEntityContainer(self.containerDef)
        # legacy database: an existing category with the display name
        # the new category will reuse, plus a product already linked to it
        with self.categories.open() as coll:
            old = coll.new_entity()
            old.set_key(('OLD', 'T1'))
            old['DisplayName'].set_from_value('Shared')
            coll.insert_entity(old)
        with self.products.open() as coll:
            legacy = coll.new_entity()
            legacy.set_key('LEGACY')
            legacy['Name'].set_from_value('Legacy product')
            legacy['Category'].bind_entity(('OLD', 'T1'))
            coll.insert_entity(legacy)

    def cat_uri(self, key):
        return "Categories(CatID='%s',Tenant='%s')" % key

    def product_uri(self, pid):
        return "Products('%s')" % pid

    def product_row(self, pid, cat_key, name='Widget'):
        return {
            '__metadata': {'uri': self.product_uri(pid)},
            'ProductID': pid,
            'Name': name,
            'Category': {'__metadata': {'uri': self.cat_uri(cat_key)}}
        }

    def category_row(self, key, display):
        return {
            '__metadata': {'uri': self.cat_uri(key)},
            'CatID': key[0],
            'Tenant': key[1],
            'DisplayName': display
        }

    def index_snapshot(self):
        aindex = self.container.associationStorage['ProductCategorySet']
        return (
            sorted(self.container.entityStorage['Products'].data),
            sorted(self.container.entityStorage['Categories'].data),
            dict((k, sorted(v)) for k, v in aindex.index.items()),
            dict((k, sorted(v)) for k, v in aindex.reverseIndex.items()))

    def assert_legacy_intact(self):
        # legacy values and relationships must never change
        with self.products.open() as coll:
            self.assertTrue('LEGACY' in coll)
            self.assertEqual(
                coll['LEGACY']['Category'].get_entity().key(),
                ('OLD', 'T1'))
        with self.categories.open() as coll:
            with coll[('OLD', 'T1')]['Products'].open() as prods:
                self.assertEqual(
                    sorted(e.key() for e in prods.values()), ['LEGACY'])

    def test_forward_reference_commits_as_one_batch(self):
        rows = [
            self.product_row('P', ('NEW', 'T1')),
            self.category_row(('NEW', 'T1'), 'Shared'),
        ]
        batch = memds.BatchImport(self.container)
        batch.import_feed(self.products, rows)
        # observer via independent collections sees nothing new while
        # the file is only staged, and no new row is paired with the
        # existing (same display-name) category
        with self.products.open() as obs:
            self.assertEqual(len(obs), 1)
            self.assertFalse('P' in obs)
        with self.categories.open() as obs:
            self.assertEqual(len(obs), 1)
            self.assertFalse(('NEW', 'T1') in obs)
        with self.categories.open() as coll:
            with coll[('OLD', 'T1')]['Products'].open() as prods:
                self.assertEqual(
                    sorted(e.key() for e in prods.values()), ['LEGACY'])
        batch.commit()
        # read back by product key: forward reference resolves to B
        with self.products.open() as coll:
            product = coll['P']
            category = product['Category'].get_entity()
            self.assertEqual(category.key(), ('NEW', 'T1'))
            self.assertEqual(category['DisplayName'].value, 'Shared')
        # read back by category compound key, both directions visible
        with self.categories.open() as coll:
            new_cat = coll[('NEW', 'T1')]
            with new_cat['Products'].open() as prods:
                self.assertEqual(
                    sorted(e.key() for e in prods.values()), ['P'])
            old_cat = coll[('OLD', 'T1')]
            with old_cat['Products'].open() as prods:
                self.assertEqual(
                    sorted(e.key() for e in prods.values()), ['LEGACY'])
        self.assert_legacy_intact()

    def test_missing_target_rolls_back_entire_file(self):
        rows = [
            self.product_row('P', ('NEW', 'T1')),
            self.category_row(('NEW', 'T1'), 'Shared'),
            self.product_row('Q', ('GHOST', 'T1')),
        ]
        before = self.index_snapshot()
        batch = memds.BatchImport(self.container)
        batch.import_feed(self.products, rows)
        try:
            batch.commit()
            self.fail("commit should fail on an unresolvable link")
        except memds.BatchImportError as err:
            # the offending input row is pinpointed
            self.assertEqual(err.line, 3)
        self.assertFalse(batch.active)
        # no rows and no associations from the batch survive
        self.assertEqual(self.index_snapshot(), before)
        self.assert_legacy_intact()

    def test_duplicate_key_rolls_back_entire_file(self):
        rows = [
            self.product_row('P', ('B3', 'T1')),
            self.category_row(('B3', 'T1'), 'Shared'),
            self.category_row(('B3', 'T1'), 'Different'),
        ]
        before = self.index_snapshot()
        batch = memds.BatchImport(self.container)
        try:
            batch.import_feed(self.products, rows)
            batch.commit()
            self.fail("import should fail on the conflicting record")
        except memds.BatchImportError as err:
            self.assertEqual(err.line, 3)
        self.assertFalse(batch.active)
        self.assertEqual(self.index_snapshot(), before)
        self.assert_legacy_intact()

    def test_retry_after_failure_is_not_blocked(self):
        conflict = [
            self.product_row('P', ('B3', 'T1')),
            self.category_row(('B3', 'T1'), 'Shared'),
            self.category_row(('B3', 'T1'), 'Different'),
        ]
        failed = memds.BatchImport(self.container)
        try:
            failed.import_feed(self.products, conflict)
            failed.commit()
        except memds.BatchImportError:
            pass
        # the keys staged by the failed attempt must not obstruct a
        # normal retry with a legal file using those same keys
        retry = memds.BatchImport(self.container)
        retry.import_feed(self.products, [
            self.product_row('P', ('B3', 'T1')),
            self.category_row(('B3', 'T1'), 'Shared'),
        ])
        retry.commit()
        with self.products.open() as coll:
            self.assertEqual(
                coll['P']['Category'].get_entity().key(), ('B3', 'T1'))

    def test_required_relationship_multiplicity_enforced(self):
        before = self.index_snapshot()
        batch = memds.BatchImport(self.container)
        batch.import_feed(self.products, [
            {'__metadata': {'uri': self.product_uri('ORPHAN')},
             'ProductID': 'ORPHAN', 'Name': 'No category'}
        ])
        try:
            batch.commit()
            self.fail("commit should reject a product with no category")
        except memds.BatchImportError as err:
            self.assertEqual(err.line, 1)
        self.assertFalse(batch.active)
        self.assertEqual(self.index_snapshot(), before)

    def test_observer_never_sees_unpublished_rows_or_links(self):
        rows = [
            self.product_row('P', ('NEW', 'T1')),
            self.category_row(('NEW', 'T1'), 'Shared'),
        ]
        batch = memds.BatchImport(self.container)
        batch.import_feed(self.products, rows)
        # a separate collection must not see a new relationship that
        # points at a row that has not been published, nor a new row
        # paired with an existing relationship
        aindex = self.container.associationStorage['ProductCategorySet']
        self.assertEqual(aindex.index, {'LEGACY': {('OLD', 'T1')}})
        self.assertEqual(
            aindex.reverseIndex, {('OLD', 'T1'): {'LEGACY'}})
        batch.commit()
        # only after commit do the row and both directions appear
        self.assertEqual(aindex.index['P'], {('NEW', 'T1')})
        self.assertEqual(aindex.reverseIndex[('NEW', 'T1')], {'P'})

    def test_programmatic_bind_by_key_to_later_entity(self):
        batch = memds.BatchImport(self.container)
        with self.products.open() as coll:
            product = coll.new_entity()
            product.set_key('PM')
            product['Name'].set_from_value('Manual product')
        batch.add_entity(product, line=1)
        with self.categories.open() as coll:
            category = coll.new_entity()
            category.set_key(('CM', 'T1'))
            category['DisplayName'].set_from_value('Manual category')
        batch.add_entity(category, line=2)
        # key-only forward reference, target added afterwards
        batch.add_link(self.products, 'Category', 'PM', ('CM', 'T1'),
                       line=1)
        batch.commit()
        with self.products.open() as coll:
            self.assertEqual(
                coll['PM']['Category'].get_entity().key(), ('CM', 'T1'))
        with self.categories.open() as coll:
            with coll[('CM', 'T1')]['Products'].open() as prods:
                self.assertEqual(
                    sorted(e.key() for e in prods.values()), ['PM'])


class RegressionTests(DataServiceRegressionTests):

    def setUp(self):        # noqa
        DataServiceRegressionTests.setUp(self)
        self.container = memds.InMemoryEntityContainer(
            self.ds['RegressionModel.RegressionContainer'])

    def test_all_tests(self):
        self.run_combined()


if __name__ == "__main__":
    unittest.main()
