import copy
import json
import unittest

import jsonschema

from singer import address_publish as ap


def deep_order_record():
    return {
        'order_id': 'A-100',
        'updated_at': '2026-11-24T00:00:00Z',
        'billing_address': {
            'region': 'EU',
            'business_key': 'VAT-9',
            'supplier_note': 'internal',
            'next': {
                'region': 'FR',
                'business_key': 'SIREN-1',
                'next': None,
            },
        },
        'shipping_address': {
            'region': 'EU',
            'business_key': 'MUST-BE-DROPPED',
            'next': {
                'region': 'FR',
                'business_key': 'MUST-BE-DROPPED-TOO',
                'next': {'region': 'DE', 'business_key': 'X'},
            },
        },
        'lines': [
            {'line_id': 'L1', 'sku': 'SKU-1', 'quantity': 2,
             'unit_price': 9.5, 'internal_margin': 0.42},
        ],
    }


def deep_return_record():
    return {
        'return_id': 'R-200',
        'received_at': '2026-11-24T01:00:00Z',
        'billing_address': {
            'region': 'US',
            'business_key': 'EIN-7',
            'next': {'region': 'CA', 'business_key': 'BN-3'},
        },
        'shipping_address': {
            'region': 'US',
            'business_key': 'LEAK-CANDIDATE',
            'next': {'region': 'CA'},
        },
        'lines': [
            {'line_id': 'RL1', 'reason': 'damaged', 'quantity': 1,
             'condition': 'used', 'warehouse_code': 'WH-9'},
        ],
    }


class TestPermissionLoops(unittest.TestCase):

    def test_billing_loop_keeps_business_keys_shipping_loop_drops_them(self):
        catalog = ap.orders_catalog()
        schema = ap.build_filtered_schema(catalog)
        filtered = ap.filter_record(deep_order_record(), schema,
                                    schema['definitions'])
        self.assertEqual(
            {'region': 'EU', 'business_key': 'VAT-9',
             'next': {'region': 'FR', 'business_key': 'SIREN-1',
                      'next': None}},
            filtered['billing_address'])
        self.assertEqual(
            {'region': 'EU',
             'next': {'region': 'FR', 'next': {'region': 'DE'}}},
            filtered['shipping_address'])
        self.assertEqual(
            [{'line_id': 'L1', 'sku': 'SKU-1', 'quantity': 2,
              'unit_price': 9.5}],
            filtered['lines'])
        jsonschema.validate(filtered, schema)

    def test_return_lines_use_their_own_structure(self):
        catalog = ap.returns_catalog()
        schema = ap.build_filtered_schema(catalog)
        filtered = ap.filter_record(deep_return_record(), schema,
                                    schema['definitions'])
        self.assertEqual(
            [{'line_id': 'RL1', 'reason': 'damaged', 'quantity': 1,
              'condition': 'used'}],
            filtered['lines'])
        self.assertEqual({'region': 'US', 'next': {'region': 'CA'}},
                         filtered['shipping_address'])
        jsonschema.validate(filtered, schema)

    def test_receiver_validates_required_null_and_optional_next(self):
        schema = ap.build_filtered_schema(ap.returns_catalog())
        # region is required at any depth of both loops
        bad = {'return_id': 'R-1', 'received_at': '2026-11-24T02:00:00Z',
               'shipping_address': {'region': 'US',
                                    'next': {'business_key': 'no-region'}}}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(
                ap.filter_record(bad, schema, schema['definitions']), schema)
        # null region is allowed by type but null address and missing next
        # are both fine
        ok = {'return_id': 'R-2', 'received_at': '2026-11-24T03:00:00Z',
              'billing_address': None,
              'shipping_address': {'region': None}}
        jsonschema.validate(
            ap.filter_record(ok, schema, schema['definitions']), schema)


class TestCacheIsolationAndSigning(unittest.TestCase):

    def test_cache_is_keyed_by_permission_context(self):
        builder = ap.FilteredSchemaBuilder(
            ap.orders_catalog()['definitions'], ap.PATH_PERMISSIONS)
        # Resolve the billing branch first, then the shipping branch: the
        # shipping definition must not inherit billing's business keys.
        billing = builder.filtered_definition('billing_address')
        shipping = builder.filtered_definition('shipping_address')
        self.assertIn('business_key', billing['properties'])
        self.assertNotIn('business_key', shipping['properties'])
        self.assertIs(builder.filtered_definition('billing_address'), billing)
        self.assertIsNot(billing, shipping)

    def test_leaked_business_key_in_shipping_loop_cannot_be_signed(self):
        catalog = ap.returns_catalog()
        schema = ap.build_filtered_schema(catalog)
        filtered = ap.filter_record(deep_return_record(), schema,
                                    schema['definitions'])
        # Correctly filtered record signs fine.
        self.assertTrue(ap.sign_record('returns', filtered,
                                       ap.PATH_PERMISSIONS))
        # Simulate a resolver cache keyed only by reference name: the billing
        # branch was cached first, so the shipping next level carries the
        # business key. The root of the record still looks correct.
        leaked = copy.deepcopy(filtered)
        leaked['shipping_address']['next']['business_key'] = 'EIN-7'
        self.assertNotIn('business_key', leaked['shipping_address'])
        with self.assertRaises(ap.SignatureError):
            ap.sign_record('returns', leaked, ap.PATH_PERMISSIONS)


class TestShallowInput(unittest.TestCase):

    def test_shallow_record_matches_current_transform_result(self):
        catalog = ap.orders_catalog()
        schema = ap.build_filtered_schema(catalog)
        shallow = {
            'order_id': 'A-9',
            'updated_at': '2026-11-24T04:00:00Z',
            'billing_address': {'region': 'EU', 'business_key': 'VAT-1',
                                'supplier_note': 'drop me'},
            'shipping_address': {'region': 'EU',
                                 'business_key': 'drop me too'},
            'lines': [],
        }
        expected = {
            'order_id': 'A-9',
            'updated_at': '2026-11-24T04:00:00Z',
            'billing_address': {'region': 'EU', 'business_key': 'VAT-1'},
            'shipping_address': {'region': 'EU'},
            'lines': [],
        }
        self.assertEqual(expected, ap.filter_record(shallow, schema,
                                                    schema['definitions']))


class TestVersionIncompatibility(unittest.TestCase):

    def test_later_version_reports_key_and_path_and_other_entry_continues(self):
        records = {
            'orders': [deep_order_record()],
            'returns': [deep_return_record()],
        }
        messages, problems = ap.publish(
            [ap.returns_catalog(version=2), ap.orders_catalog()], records)
        self.assertEqual(1, len(problems))
        problem = problems[0]
        self.assertEqual('received_at', problem.replication_key)
        self.assertEqual(['billing_address', 'shipping_address'],
                         problem.paths)
        self.assertIn('region', problem.missing)
        # The other entry was still processed with its existing catalog.
        streams = [m['stream'] for m in messages]
        self.assertNotIn('returns', streams)
        self.assertEqual(['orders', 'orders'], streams)
        record_msg = messages[1]
        self.assertEqual('orders', record_msg['stream'])
        self.assertTrue(record_msg['signature'])

    def test_compatible_catalogs_publish_both_entries(self):
        records = {
            'orders': [deep_order_record()],
            'returns': [deep_return_record()],
        }
        messages, problems = ap.publish(
            [ap.orders_catalog(), ap.returns_catalog()], records)
        self.assertEqual([], problems)
        self.assertEqual(['orders', 'orders', 'returns', 'returns'],
                         [m['stream'] for m in messages])


class TestProvenanceAndFinitePattern(unittest.TestCase):

    def test_messages_carry_source_catalog_and_ref_version(self):
        messages, _ = ap.publish([ap.orders_catalog()],
                                 {'orders': [deep_order_record()]})
        for message in messages:
            self.assertEqual(
                {'source_catalog': 'orders', 'catalog_version': 1,
                 'ref': 'address', 'ref_version': 1},
                message['provenance'])

    def test_recursion_stays_a_finite_ref_pattern(self):
        schema = ap.build_filtered_schema(ap.returns_catalog())
        definitions = schema['definitions']
        self.assertEqual(
            {'$ref': '#/definitions/address__billing_address'},
            definitions['address__billing_address']['properties']['next'])
        self.assertEqual(
            {'$ref': '#/definitions/address__shipping_address'},
            definitions['address__shipping_address']['properties']['next'])
        # The finite pattern must be smaller than expanding the recursion
        # even a few levels deep.
        expanded = copy.deepcopy(schema)
        for _ in range(4):
            for def_name, definition in list(expanded['definitions'].items()):
                nxt = definition['properties']['next']
                if '$ref' in nxt:
                    definition['properties']['next'] = copy.deepcopy(
                        expanded['definitions'][def_name])
        self.assertLess(len(json.dumps(schema)),
                        len(json.dumps(expanded)))


if __name__ == '__main__':
    unittest.main()
