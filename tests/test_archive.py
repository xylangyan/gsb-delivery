"""Tests for the outbound/archive re-layout entry point."""
import unittest

from ebmlite import core, encoding
from ebmlite.archive import (PermissionTable, exportVolume, _readVolume,
                             ArchiveError, Locator)

SCHEMA_XML = '''<?xml version="1.0"?>
<Schema name="samp" version="1">
  <MasterElement name="Volume" id="0x80">
    <MasterElement name="P" id="0x81">
      <MasterElement name="A" id="0x82">
        <BinaryElement name="AV" id="0x84"/>
      </MasterElement>
      <MasterElement name="B" id="0x83">
        <BinaryElement name="BV" id="0x85"/>
      </MasterElement>
    </MasterElement>
    <BinaryElement name="Void" global="1" id="0xEC" multiple="1"/>
  </MasterElement>
</Schema>'''


def es(value, width=1):
    return encoding.encodeSize(value, width)


def buildVolume():
    av = bytes([0x84]) + es(6) + b'a' * 6
    a = bytes([0x82]) + b'\xff' + av
    bv = bytes([0x85]) + es(6) + b'b' * 6
    b = bytes([0x83]) + es(8, 3) + bv
    ppayload = a + b
    p = bytes([0x81]) + es(len(ppayload)) + ppayload
    volume = bytes([0x80]) + es(len(p)) + p
    assert len(ppayload) == 22 and len(a) == 10 and len(b) == 12
    tree = {0: {'name': 'Volume', 'id': 0x80, 'children': [
        {'offset': 2, 'name': 'P', 'id': 0x81, 'children': [
            {'offset': 4, 'name': 'A', 'id': 0x82, 'children': [
                {'offset': 6, 'name': 'AV', 'id': 0x84, 'children': []}]},
            {'offset': 14, 'name': 'B', 'id': 0x83, 'children': [
                {'offset': 18, 'name': 'BV', 'id': 0x85, 'children': []}]},
        ]}]}}
    return volume, tree


class testArchiveAB(unittest.TestCase):

    def setUp(self):
        self.schema = core.parseSchema(SCHEMA_XML, name='samp')
        self.volume, self.tree = buildVolume()

    def permissions(self, offset0=True, explicitA=True):
        windows = [{'start': 14, 'end': 16, 'cost': 5,
                    'container': 0x81, 'label': 'off:+10'}]
        if offset0:
            windows.append({'start': 4, 'end': 6, 'cost': 100,
                            'container': 0x81, 'label': 'off:0'})
        return PermissionTable({
            'containers': {
                0x80: {'widths': [1], 'cost': 1},
                0x81: {'widths': [1], 'cost': 1},
                0x82: {'widths': [1], 'unknown': True,
                       'explicit': explicitA, 'cost': 2},
                0x83: {'widths': [1], 'cost': 7},
            },
            'windows': windows,
            'volumeEnd': 26,
        })

    def testCheapestValidIsOffsetZero(self):
        r = exportVolume(self.volume, self.tree, self.schema, self.schema,
                         self.permissions(True, False), locks={14: 16})
        self.assertTrue(r.ok, r.diagnostics)
        self.assertEqual(len(r.volume), 26)
        self.assertEqual(r.locator[4].header_start, 6)
        self.assertEqual(r.locator[4].size_value, None)
        self.assertEqual(r.locator[14].header_start, 16)
        self.assertEqual(r.locator[14].size_width, 1)
        self.assertNotEqual(r.locator[14].size_value, 3)

    def testExplicitAAllowsOffsetTen(self):
        r = exportVolume(self.volume, self.tree, self.schema, self.schema,
                         self.permissions(False, True), locks={14: 16})
        self.assertTrue(r.ok, r.diagnostics)
        self.assertEqual(r.locator[4].header_start, 4)
        self.assertEqual(r.locator[4].size_value, 8)
        self.assertEqual(r.locator[14].header_start, 16)

    def testUnknownAMisreadsVoid(self):
        r = exportVolume(self.volume, self.tree, self.schema, self.schema,
                         self.permissions(False, False), locks={14: 16})
        self.assertFalse(r.ok)
        self.assertEqual(r.failure, 'layout')
        self.assertIsNone(r.volume)
        self.assertTrue(r.firstErrors)
        err = r.firstErrors[0]
        self.assertIn(err['reader'], ('old', 'new'))
        self.assertEqual(err['actualParentName'], 'A')
        self.assertEqual(err['newOffset'], 14)

    def testAddressFailure(self):
        perms = self.permissions(True, True)
        perms.volumeEnd = 27
        r = exportVolume(self.volume, self.tree, self.schema, self.schema,
                         perms, locks={14: 16})
        self.assertFalse(r.ok)
        self.assertEqual(r.failure, 'address')
        self.assertIsNone(r.volume)
        self.assertTrue(r.affectedRanges)

    def testLocatorRoundTrip(self):
        r = exportVolume(self.volume, self.tree, self.schema, self.schema,
                         self.permissions(True, False), locks={14: 16})
        self.assertTrue(r.ok, r.diagnostics)
        self.assertIsInstance(r.locator, Locator)
        for entry in r.locator.entries:
            self.assertEqual(r.volume[entry.header_start:entry.header_start + 1],
                             self.volume[entry.original_offset:entry.original_offset + 1])
            self.assertEqual(entry.payload_end - entry.payload_start + 0,
                             entry.payload_end - entry.payload_start)
        av = r.locator[6]
        self.assertEqual(r.volume[av.payload_start:av.payload_end], b'a' * 6)
        bv = r.locator[18]
        self.assertEqual(r.volume[bv.payload_start:bv.payload_end], b'b' * 6)


    def testFlatTreeByHeaderOffset(self):
        flat = {0: {'name': 'Volume', 'id': 0x80},
                2: {'name': 'P', 'id': 0x81},
                4: {'name': 'A', 'id': 0x82},
                6: {'name': 'AV', 'id': 0x84},
                14: {'name': 'B', 'id': 0x83},
                18: {'name': 'BV', 'id': 0x85}}
        r = exportVolume(self.volume, flat, self.schema, self.schema,
                         self.permissions(True, False), locks={14: 16})
        self.assertTrue(r.ok, r.diagnostics)
        self.assertEqual(r.locator[14].parent, 2)
        self.assertEqual(r.locator[6].parent, 4)


    def testAdjacencyTreeByHeaderOffset(self):
        tree = {0: [2], 2: [4, 14], 4: [6], 6: [], 14: [18], 18: []}
        r = exportVolume(self.volume, tree, self.schema, self.schema,
                         self.permissions(True, False), locks={14: 16})
        self.assertTrue(r.ok, r.diagnostics)
        self.assertEqual(r.locator[6].parent, 4)
        self.assertEqual(r.locator[18].parent, 14)


class testArchivePreservation(unittest.TestCase):
    """Byte preservation and two-schema replay."""

    OLD = '''<Schema name="old" version="1">
      <MasterElement name="Vol" id="0x80">
        <MasterElement name="P" id="0x81">
          <BinaryElement name="Scalar" id="0x86"/>
          <MasterElement name="B" id="0x83"/>
        </MasterElement>
        <BinaryElement name="Void" global="1" id="0xEC" multiple="1"/>
      </MasterElement>
    </Schema>'''
    NEW = '''<Schema name="new" version="1">
      <MasterElement name="Vol" id="0x80">
        <MasterElement name="P" id="0x81">
          <BinaryElement name="Scalar" id="0x86"/>
          <MasterElement name="B" id="0x83"/>
        </MasterElement>
        <BinaryElement name="Void" global="1" id="0xEC" multiple="1"/>
      </MasterElement>
    </Schema>'''

    def testScalarBytesAndTwoSchemas(self):
        old = core.parseSchema(self.OLD, name='old-archive-test')
        new = core.parseSchema(self.NEW, name='new-archive-test')
        scalarPayload = bytes(range(1, 5))
        scalar = bytes([0x86]) + es(len(scalarPayload)) + scalarPayload
        b = bytes([0x83]) + es(8, 3) + b'B' * 8
        p = bytes([0x81]) + es(len(scalar) + len(b), 2) + scalar + b
        volume = bytes([0x80]) + es(len(p)) + p
        scalarOffset = 2 + 2 + 1
        bOffset = scalarOffset + len(scalar)
        tree = {0: {'name': 'Vol', 'id': 0x80, 'children': [
            {'offset': 2, 'name': 'P', 'id': 0x81, 'children': [
                {'offset': scalarOffset, 'name': 'Scalar', 'id': 0x86,
                 'children': []},
                {'offset': bOffset, 'name': 'B', 'id': 0x83, 'children': []},
            ]}]}}
        perms = PermissionTable({
            'containers': {0x81: {'widths': [1, 2]}, 0x83: {'widths': [1]}},
            'windows': [{'start': bOffset, 'end': len(volume) + 2,
                         'cost': 3, 'container': 0x81}],
            'volumeEnd': len(volume),
        })
        r = exportVolume(volume, tree, old, new, perms)
        self.assertTrue(r.ok, r.diagnostics)
        entry = r.locator[scalarOffset]
        self.assertEqual(r.volume[entry.header_start:entry.payload_end],
                         volume[scalarOffset:scalarOffset + len(scalar)])
        # B shrank from width 3 to width 1; the difference became a Void.
        bEntry = r.locator[bOffset]
        self.assertEqual(bEntry.size_width, 1)
        self.assertEqual(len(r.volume), len(volume))
        for schema in (old, new):
            reads = _readVolume(r.volume, schema)
            self.assertTrue(any(el.offset == bEntry.header_start for el in reads))


if __name__ == '__main__':
    unittest.main()
