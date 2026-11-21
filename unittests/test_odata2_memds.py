#! /usr/bin/env python

import unittest
import threading
import time

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


class RegressionTests(DataServiceRegressionTests):

    def setUp(self):        # noqa
        DataServiceRegressionTests.setUp(self)
        self.container = memds.InMemoryEntityContainer(
            self.ds['RegressionModel.RegressionContainer'])

    def test_all_tests(self):
        self.run_combined()

    def test_replace_if_current_atomicity(self):
        """Concurrent readers must never observe a partially updated
        association: at all times the product has exactly one
        responsible warehouse (never none, never two)."""
        container = self.ds['RegressionModel.RegressionContainer']
        products = container['Products']
        warehouses = container['Warehouses']
        with products.open() as pcoll:
            with warehouses.open() as wcoll:
                for wid in ['A', 'B']:
                    w = wcoll.new_entity()
                    w['WarehouseID'].set_from_value(wid)
                    wcoll.insert_entity(w)
                p = pcoll.new_entity()
                p['ProductID'].set_from_value(1)
                p['Name'].set_from_value('Widget')
                p['ResponsibleWarehouse'].bind_entity('A')
                pcoll.insert_entity(p)
        errors = []
        stop = []

        def reader():
            try:
                while not stop:
                    with products.open() as pcoll:
                        p = pcoll[1]
                        with p['ResponsibleWarehouse'].open() as nav:
                            count = len(nav)
                    if count != 1:
                        errors.append(count)
            except Exception as err:
                errors.append(err)

        def writer():
            for i in range(100):
                source, target = ('A', 'B') if i % 2 == 0 else ('B', 'A')
                with products.open() as pcoll:
                    p = pcoll[1]
                    with p['ResponsibleWarehouse'].open() as nav:
                        nav.replace_if_current(target, expected=source)

        # Widen the window between adding the new link and removing the
        # old one: if the check-and-replace is not executed atomically a
        # reader is very likely to observe both links (or neither).
        add_link = memds.InMemoryAssociationIndex.add_link

        def slow_add_link(self, from_key, to_key):
            add_link(self, from_key, to_key)
            time.sleep(0.001)

        memds.InMemoryAssociationIndex.add_link = slow_add_link
        try:
            readers = [threading.Thread(target=reader) for i in range(3)]
            for t in readers:
                t.start()
            writer()
            stop.append(True)
            for t in readers:
                t.join()
        finally:
            memds.InMemoryAssociationIndex.add_link = add_link
        self.assertTrue(
            not errors,
            "Readers observed intermediate states: %s" % repr(errors))


if __name__ == "__main__":
    unittest.main()
