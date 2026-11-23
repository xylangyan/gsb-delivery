'''Publish permission-filtered schemas and records for catalogs that share a
recursive ``address`` reference.

Two catalogs (``orders`` and ``returns``) each carry a recursive ``address``
definition.  The supplier permits the same address to be reached through a
``billing_address`` path (region and business keys allowed) and a
``shipping_address`` path (region only).  This module filters each catalog's
schema and records per authorized path, keeps the recursion as a finite
``$ref`` pattern instead of expanding it, and emits Singer messages annotated
with the source catalog and the actual reference version used.
'''
import copy
import hashlib
import json

import jsonschema

REF_KEY = '$ref'
DEFINITIONS_KEY = 'definitions'
ADDRESS_REF = 'address'
RECURSE_FIELD = 'next'

# Keys the receiver requires to be present (and required) on every level of
# an authorized address branch.
REQUIRED_ADDRESS_KEYS = frozenset(['region'])

# Field permissions per authorized path: billing keeps region and business
# keys, shipping keeps region only.
PATH_PERMISSIONS = {
    'billing_address': frozenset(['region', 'business_key']),
    'shipping_address': frozenset(['region']),
}


class CatalogIncompatibility(Exception):
    '''Raised when a catalog version can no longer serve an authorized path.

    Carries the actual replication key of the stream and the authorized
    paths that are missing required keys.
    '''

    def __init__(self, catalog, version, replication_key, paths, missing):
        self.catalog = catalog
        self.version = version
        self.replication_key = replication_key
        self.paths = list(paths)
        self.missing = sorted(missing)
        super().__init__(
            "catalog '{}' v{}: replication key '{}' is incompatible with "
            "authorized path(s) {}: required key(s) {} are not guaranteed "
            "on the address branch".format(
                catalog, version, replication_key,
                ', '.join(self.paths), ', '.join(self.missing)))


class SignatureError(Exception):
    '''Raised when a record carries fields outside the authorized path set.'''


def address_definition_v1():
    '''The shared recursive address definition, reference version 1.'''
    return {
        'version': 1,
        'schema': {
            'type': ['object', 'null'],
            'properties': {
                'region': {'type': ['string', 'null']},
                'business_key': {'type': ['string', 'null']},
                RECURSE_FIELD: {REF_KEY: '#/definitions/' + ADDRESS_REF},
            },
            'required': ['region'],
        },
    }


def address_definition_v2():
    '''A later reference version that no longer guarantees ``region``.'''
    definition = address_definition_v1()
    definition['version'] = 2
    definition['schema'] = copy.deepcopy(definition['schema'])
    definition['schema']['required'] = []
    return definition


def orders_catalog():
    '''Catalog A: order lines, recursive address via billing and shipping.'''
    return {
        'name': 'orders',
        'version': 1,
        'definitions': {ADDRESS_REF: address_definition_v1()},
        'stream': {
            'tap_stream_id': 'orders',
            'key_properties': ['order_id'],
            'replication_key': 'updated_at',
            'permissions': PATH_PERMISSIONS,
            'schema': {
                'type': 'object',
                'properties': {
                    'order_id': {'type': 'string'},
                    'updated_at': {'type': 'string', 'format': 'date-time'},
                    'billing_address': {REF_KEY: '#/definitions/' + ADDRESS_REF},
                    'shipping_address': {REF_KEY: '#/definitions/' + ADDRESS_REF},
                    'lines': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'line_id': {'type': 'string'},
                                'sku': {'type': 'string'},
                                'quantity': {'type': 'integer'},
                                'unit_price': {'type': 'number'},
                            },
                        },
                    },
                },
                'required': ['order_id', 'updated_at'],
            },
        },
    }


def returns_catalog(version=1):
    '''Catalog B: return lines, same recursive address, different lines.'''
    definitions = {ADDRESS_REF: address_definition_v1()}
    if version >= 2:
        definitions = {ADDRESS_REF: address_definition_v2()}
    return {
        'name': 'returns',
        'version': version,
        'definitions': definitions,
        'stream': {
            'tap_stream_id': 'returns',
            'key_properties': ['return_id'],
            'replication_key': 'received_at',
            'permissions': PATH_PERMISSIONS,
            'schema': {
                'type': 'object',
                'properties': {
                    'return_id': {'type': 'string'},
                    'received_at': {'type': 'string', 'format': 'date-time'},
                    'billing_address': {REF_KEY: '#/definitions/' + ADDRESS_REF},
                    'shipping_address': {REF_KEY: '#/definitions/' + ADDRESS_REF},
                    'lines': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'line_id': {'type': 'string'},
                                'reason': {'type': 'string'},
                                'quantity': {'type': 'integer'},
                                'condition': {'type': 'string'},
                            },
                        },
                    },
                },
                'required': ['return_id', 'received_at'],
            },
        },
    }


def _ref_name(ref):
    return ref.split('/')[-1]


class FilteredSchemaBuilder:
    '''Builds filtered definitions for the recursive address reference.

    The cache is keyed by (reference, authorized path) so that resolving the
    billing branch first can never leak business keys into the shipping
    branch of the same reference.
    '''

    def __init__(self, definitions, permissions):
        self.definitions = definitions
        self.permissions = permissions
        self._cache = {}

    def filtered_definition(self, context):
        key = (ADDRESS_REF, context)
        if key in self._cache:
            return self._cache[key]
        allowed = self.permissions[context]
        source = self.definitions[ADDRESS_REF]['schema']
        filtered = {
            'type': source['type'],
            'properties': {},
            'required': [k for k in source.get('required', []) if k in allowed],
        }
        # Register before wiring the recursion so the cycle stays a finite
        # $ref pattern instead of an unbounded expansion.
        self._cache[key] = filtered
        for field in sorted(allowed):
            if field in source['properties']:
                filtered['properties'][field] = copy.deepcopy(
                    source['properties'][field])
        filtered['properties'][RECURSE_FIELD] = {
            REF_KEY: '#/definitions/{}__{}'.format(ADDRESS_REF, context),
        }
        return filtered


def build_filtered_schema(catalog):
    '''Filter a catalog stream schema down to the authorized paths.

    The returned schema keeps the recursive address as per-path definitions
    referenced by ``$ref``; it never expands the recursion.
    '''
    entry = catalog['stream']
    permissions = entry['permissions']
    builder = FilteredSchemaBuilder(catalog['definitions'], permissions)
    schema = entry['schema']
    properties = {}
    definitions = {}
    for name, subschema in schema['properties'].items():
        if (subschema.get(REF_KEY) == '#/definitions/' + ADDRESS_REF
                and name in permissions):
            def_name = '{}__{}'.format(ADDRESS_REF, name)
            definitions[def_name] = builder.filtered_definition(name)
            properties[name] = {REF_KEY: '#/definitions/' + def_name}
        else:
            properties[name] = copy.deepcopy(subschema)
    return {
        'type': schema['type'],
        'properties': properties,
        'required': list(schema.get('required', [])),
        DEFINITIONS_KEY: definitions,
    }


def filter_record(node, schema, definitions):
    '''Project a record onto the filtered schema, at any recursion depth.'''
    if node is None:
        return None
    if REF_KEY in schema:
        schema = definitions[_ref_name(schema[REF_KEY])]
    if isinstance(node, dict):
        return {
            key: filter_record(node[key], subschema, definitions)
            for key, subschema in schema.get('properties', {}).items()
            if key in node
        }
    if isinstance(node, list):
        return [filter_record(item, schema['items'], definitions)
                for item in node]
    return node


def _check_branch(node, allowed, path):
    while isinstance(node, dict):
        extras = set(node) - set(allowed) - {RECURSE_FIELD}
        if extras:
            raise SignatureError(
                'unauthorized field(s) {} at {}'.format(sorted(extras), path))
        node = node.get(RECURSE_FIELD)
        path = path + '.' + RECURSE_FIELD


def sign_record(stream, record, permissions):
    '''Sign a filtered record after checking every authorized branch.

    The check walks each permission path down the whole recursion, so a
    business key cached from the billing branch and leaked into any shipping
    level is rejected even when the root of the record looks correct.
    '''
    for field in sorted(permissions):
        _check_branch(record.get(field), permissions[field],
                      '{}.{}'.format(stream, field))
    payload = json.dumps(record, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def check_catalog_compatibility(catalog):
    '''Verify a catalog version still guarantees the required keys.

    Raises CatalogIncompatibility naming the actual replication key and the
    authorized paths that lost a required key.
    '''
    entry = catalog['stream']
    address = catalog['definitions'][ADDRESS_REF]['schema']
    replication_key = entry['replication_key']
    if replication_key not in entry['schema']['properties']:
        raise CatalogIncompatibility(
            catalog['name'], catalog['version'], replication_key,
            ['<stream>'], [replication_key])
    declared = set(address.get('properties', {}))
    guaranteed = set(address.get('required', []))
    broken_paths = []
    missing = set()
    for path, allowed in entry['permissions'].items():
        lacking = {k for k in REQUIRED_ADDRESS_KEYS
                   if k not in declared or k not in guaranteed
                   or k not in allowed}
        if lacking:
            broken_paths.append(path)
            missing |= lacking
    if broken_paths:
        raise CatalogIncompatibility(
            catalog['name'], catalog['version'], replication_key,
            sorted(broken_paths), missing)


def _provenance(catalog):
    return {
        'source_catalog': catalog['name'],
        'catalog_version': catalog['version'],
        'ref': ADDRESS_REF,
        'ref_version': catalog['definitions'][ADDRESS_REF]['version'],
    }


def publish(catalogs, records_by_stream):
    '''Emit filtered SCHEMA/RECORD messages for every compatible catalog.

    Returns (messages, incompatibilities).  A catalog whose version breaks
    an authorized path is reported and skipped; the remaining entries
    continue with their existing correct catalogs.
    '''
    messages = []
    incompatibilities = []
    for catalog in catalogs:
        entry = catalog['stream']
        stream = entry['tap_stream_id']
        try:
            check_catalog_compatibility(catalog)
        except CatalogIncompatibility as exc:
            incompatibilities.append(exc)
            continue
        filtered_schema = build_filtered_schema(catalog)
        provenance = _provenance(catalog)
        messages.append({
            'type': 'SCHEMA',
            'stream': stream,
            'schema': filtered_schema,
            'key_properties': list(entry['key_properties']),
            'provenance': provenance,
        })
        for record in records_by_stream.get(stream, []):
            filtered = filter_record(record, filtered_schema,
                                     filtered_schema[DEFINITIONS_KEY])
            jsonschema.validate(filtered, filtered_schema)
            messages.append({
                'type': 'RECORD',
                'stream': stream,
                'record': filtered,
                'signature': sign_record(stream, filtered,
                                         entry['permissions']),
                'provenance': provenance,
            })
    return messages, incompatibilities
