"""Deterministic Pyslet OData forensic harness.

No sockets are used: the real Pyslet WSGI service is invoked in-process and
the real Pyslet OData client/decoder consumes the raw response bytes.  This
keeps the service code untouched and makes every request/response auditable.
"""

import io
import os
import sys
import time
from collections import OrderedDict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pyslet import info
from pyslet import iso8601 as iso
from pyslet import rfc2396 as uri
from pyslet.http import client as http
from pyslet.odata2 import client as odata_client
from pyslet.odata2 import core
from pyslet.odata2 import memds
from pyslet.odata2 import metadata as edmx
from pyslet.odata2 import server


KEY_PROPERTY = 'ProductID'
DATA_PROPERTIES = [
    'ProductID', 'Name', 'Warehouse', 'BinSequence', 'Active', 'Region',
    'Note']
NAV_PROPERTIES = ['Category']
EXPECTED_ACTIVE_KEYS = ['P-0001', 'P-0002', 'P-0003', 'P-0005']
INACTIVE_KEY = 'P-0004'
OUT_OF_SCOPE_KEY = 'Q-0000'
REGION_NULL_KEY = 'P-0003'

SERVICE_ROOT = 'http://host/service/'
MODEL_PATH = os.path.join(os.path.dirname(__file__), 'model.xml')


class FixedTime(object):
    """Freeze Atom <updated> values so raw artifacts are byte-stable."""

    epoch = 1700000000

    @classmethod
    def time(cls):
        return cls.epoch

    @classmethod
    def gmtime(cls, *args):
        return time.gmtime(cls.epoch if not args else args[0])

    @classmethod
    def localtime(cls, *args):
        return time.localtime(cls.epoch if not args else args[0])


def _is_null_attribute(prop):
    for (ns, name), value in prop.get_attributes().items():
        if name == 'null' and str(value).lower() == 'true':
            return True
    return False


def _text_value(prop):
    if _is_null_attribute(prop):
        return None
    value = prop.get_value()
    if value is None:
        return None
    try:
        return str(value)
    except Exception:
        return repr(value)


def parse_properties(properties):
    result = OrderedDict()
    for prop in properties.Property:
        name = prop.xmlname if isinstance(prop.xmlname, str) \
            else str(prop.xmlname)
        result[name] = {
            'present': True,
            'null': _is_null_attribute(prop),
            'text': _text_value(prop),
        }
    return result


def parse_entry(entry):
    props = {}
    if entry.Content is not None and entry.Content.Properties is not None:
        props = parse_properties(entry.Content.Properties)
    navs = []
    for link in entry.Link:
        rel = link.rel or ''
        if not rel.startswith(core.ODATA_RELATED):
            continue
        nav_name = rel[len(core.ODATA_RELATED):]
        inline = None
        if link.Inline is not None:
            if link.Inline.Entry is not None:
                inline = {
                    'kind': 'entry',
                    'entry': parse_entry(link.Inline.Entry),
                }
            elif link.Inline.Feed is not None:
                inline = {
                    'kind': 'feed',
                    'entries': [parse_entry(e)
                                for e in link.Inline.Feed.Entry],
                }
        navs.append({
            'name': nav_name,
            'rel': rel,
            'href': str(link.href) if link.href is not None else None,
            'inline': inline,
        })
    return {
        'key': props.get(KEY_PROPERTY, {}).get('text'),
        'id': entry.AtomId.get_value() if entry.AtomId is not None else None,
        'properties': props,
        'field_presence': OrderedDict(
            (name, {
                'present': name in props,
                'null': props[name]['null'] if name in props else None,
            })
            for name in DATA_PROPERTIES),
        'navs': navs,
    }


def parse_feed(body, base_url):
    doc = core.Document(base_uri=base_url)
    doc.read(body)
    if not isinstance(doc.root, core.Feed):
        raise ValueError('expected Atom feed, got %s' % type(doc.root))
    entries = [parse_entry(e) for e in doc.root.Entry]
    next_link = None
    feed_links = []
    for link in doc.root.Link:
        item = {
            'rel': link.rel,
            'href': str(link.href) if link.href is not None else None,
        }
        if link.rel == 'next':
            item['resolved'] = str(link.resolve_uri(link.href))
            next_link = item['resolved']
        feed_links.append(item)
    return {
        'entry_keys': [e['key'] for e in entries],
        'entries': entries,
        'next_link': next_link,
        'feed_links': feed_links,
    }


def parse_entry_document(body, base_url):
    doc = core.Document(base_uri=base_url)
    doc.read(body)
    if not isinstance(doc.root, core.Entry):
        raise ValueError('expected Atom entry, got %s' % type(doc.root))
    return parse_entry(doc.root)


def query_options(url):
    parsed = core.ODataURI(uri.URI.from_octets(url), '')
    result = OrderedDict()
    for name, value in parsed.sys_query_options.items():
        label = core.SystemQueryOption.to_str(name)
        if label == 'orderby':
            result[label] = core.CommonExpression.orderby_to_str(value)
        elif label in ('expand', 'select'):
            result[label] = core.format_expand(value)
        elif label == 'filter':
            result[label] = str(value)
        else:
            result[label] = str(value)
    return result


def request_text(record):
    lines = ['%s %s%s HTTP/1.1' % (
        record['method'], record['path'],
        ('?' + record['query']) if record['query'] else ''),
        'Host: %s' % record.get('host', 'host')]
    for name, value in record['headers'].items():
        lines.append('%s: %s' % (name, value))
    return '\r\n'.join(lines) + '\r\n\r\n'


class WsgiTransport(http.Client):
    """HTTP client replacement that invokes a WSGI app in-process."""

    def __init__(self, app):
        super(WsgiTransport, self).__init__()
        self.app = app
        self.log = []

    def process_request(self, request, timeout=60):
        url = request.url
        path = url.abs_path or '/'
        query = url.query or ''
        env = {
            'REQUEST_METHOD': request.method,
            'SCRIPT_NAME': '',
            'PATH_INFO': path,
            'QUERY_STRING': query,
            'SERVER_NAME': url.host or 'host',
            'SERVER_PORT': str(url.port or 80),
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'wsgi.version': (1, 0),
            'wsgi.input': io.BytesIO(b''),
            'wsgi.errors': sys.stderr,
            'wsgi.multithread': False,
            'wsgi.multiprocess': False,
            'wsgi.run_once': False,
            'wsgi.url_scheme': url.scheme or 'http',
        }
        if request.entity_body is not None:
            body = request.entity_body
            body.seek(0)
            env['wsgi.input'] = io.BytesIO(body.read())
        headers = OrderedDict()
        for name in request.get_headerlist():
            if name.lower() in (b'host',):
                continue
            value = request.get_header(name)
            if value is None:
                continue
            text_name = name.decode('latin-1')
            text_value = value.decode('latin-1')
            headers[text_name] = text_value
            env['HTTP_' + text_name.upper().replace('-', '_')] = text_value
        if 'Content-Type' in headers:
            env['CONTENT_TYPE'] = headers['Content-Type']
        if 'Content-Length' in headers:
            env['CONTENT_LENGTH'] = headers['Content-Length']

        captured = {}

        def start_response(status, response_headers, exc_info=None):
            captured['status'] = status
            captured['headers'] = list(response_headers)
            return lambda data: None

        chunks = self.app(env, start_response)
        body = b''.join(
            chunk if isinstance(chunk, bytes) else chunk.encode('utf-8')
            for chunk in chunks)
        status_text = captured['status']
        status = int(status_text.split(' ', 1)[0])
        request.status = status
        request.res_body = body
        response = request.response
        response.set_status(
            status, status_text.split(' ', 1)[1]
            if ' ' in status_text else None)
        for name, value in captured['headers']:
            response.set_header(name, value)
        record = {
            'method': request.method,
            'url': str(url),
            'scheme': url.scheme,
            'host': url.host or 'host',
            'path': path,
            'query': query,
            'query_options': query_options(str(url)),
            'headers': headers,
            'status': status,
            'reason': status_text.split(' ', 1)[1]
            if ' ' in status_text else '',
            'response_headers': OrderedDict(captured['headers']),
            'response_body': body,
        }
        self.log.append(record)
        return request


class ForensicClient(odata_client.Client):
    """OData client using the in-process transport and normal client headers."""

    def __init__(self, app, service_root=SERVICE_ROOT):
        self.transport = WsgiTransport(app)
        super(ForensicClient, self).__init__(service_root)

    def process_request(self, request, timeout=60):
        if not request.has_header('Accept'):
            request.set_accept(self.ACCEPT_LIST)
        if not request.has_header('DataServiceVersion'):
            request.set_header(
                'DataServiceVersion', '2.0; pyslet %s' % info.version)
            request.set_header(
                'MaxDataServiceVersion', '2.0; pyslet %s' % info.version)
        return self.transport.process_request(request, timeout)


def build_service(topmax=1):
    iso.pytime = FixedTime
    doc = edmx.Document()
    with open(MODEL_PATH, 'rb') as f:
        doc.read(f)
    svc = server.Server(SERVICE_ROOT)
    svc.set_model(doc)
    svc.topmax = topmax
    ds = doc.root.DataServices
    container = memds.InMemoryEntityContainer(
        ds['Inventory.InventoryContainer'])
    _load_records(container)
    return svc, container, doc


def _load_records(container):
    rows = [
        # key, name, warehouse, bin, active, region, note, category
        ('P-0001', 'Bolt A', 'W1', 100, True, 'R1', 'n1', 'C1'),
        ('P-0002', 'Bolt B', 'W1', 100, True, 'R1', 'n2', 'C1'),
        ('P-0003', 'Gasket', 'W1', 110, True, None, 'n3', 'C2'),
        ('P-0004', 'Washer', 'W1', 120, False, 'R2', 'n4', 'C1'),
        ('P-0005', 'Nut', 'W1', 130, True, 'R3', 'n5', 'C2'),
        ('Q-0000', 'Other', 'W2', 90, True, 'R9', 'n0', 'C1'),
    ]
    products = container.entityStorage['Products']
    for key, name, wh, seq, active, region, note, category in rows:
        products.data[key] = (key, name, wh, seq, active, region, note)
    categories = container.entityStorage['Categories']
    categories.data['C1'] = ('C1', 'Fasteners', 'R1')
    categories.data['C2'] = ('C2', 'Seals', None)
    with products.entity_set.open() as collection:
        for key, name, wh, seq, active, region, note, category in rows:
            entity = collection[key]
            entity['Category'].bind_entity(category)
            collection.update_entity(entity)


def warehouse_filter():
    return core.CommonExpression.from_str("Warehouse eq 'W1'")


def orderby():
    return core.CommonExpression.orderby_from_str(
        'BinSequence asc,ProductID asc')


def configure_collection(client, projection):
    collection = client.feeds['Products'].open()
    if projection == 'full':
        collection.set_expand({'Category': None}, None)
    elif projection == 'partial':
        collection.set_expand(
            {'Category': None},
            {'ProductID': None, 'Name': None, 'Warehouse': None,
             'BinSequence': None, 'Region': None})
    else:
        raise ValueError(projection)
    collection.set_orderby(orderby())
    collection.set_filter(warehouse_filter())
    collection.set_page(2, 0)
    return collection


def build_url(collection, skip=None, skiptoken=None, include_top=True,
              filter_override=None):
    options = OrderedDict()
    active_filter = (filter_override if filter_override is not None
                     else collection.filter)
    if active_filter is not None:
        options[core.SystemQueryOption.filter] = str(active_filter)
    if collection.expand is not None:
        options[core.SystemQueryOption.expand] = core.format_expand(
            collection.expand)
    if collection.select is not None:
        options[core.SystemQueryOption.select] = core.format_select(
            collection.select)
    if collection.orderby is not None:
        options[core.SystemQueryOption.orderby] = \
            core.CommonExpression.orderby_to_str(collection.orderby)
    if include_top and collection.top is not None:
        options[core.SystemQueryOption.top] = str(collection.top)
    if skip is not None:
        options[core.SystemQueryOption.skip] = str(skip)
    if skiptoken is not None:
        options[core.SystemQueryOption.skiptoken] = skiptoken
    if options:
        return str(uri.URI.from_octets(
            str(collection.base_uri) + '?' +
            core.ODataURI.format_sys_query_options(options)))
    return str(collection.base_uri)


def fetch(client, url, accept='application/atom+xml'):
    request = http.ClientRequest(url)
    request.set_header('Accept', accept)
    client.process_request(request)
    record = client.transport.log[-1]
    if request.status != 200:
        raise RuntimeError('HTTP %s for %s' % (request.status, url))
    return request, record


def decode_feed(collection, body, url):
    doc = core.Document(base_uri=url)
    doc.read(body)
    decoded = []
    for entry in doc.root.Entry:
        entity = core.Entity(collection.entity_set)
        entity.exists = True
        entry.get_value(entity)
        values = OrderedDict()
        for name in DATA_PROPERTIES:
            value = entity[name]
            values[name] = {
                'selected': entity.is_selected(name),
                'value': value.value if hasattr(value, 'value') else None,
            }
        navs = OrderedDict()
        for name in NAV_PROPERTIES:
            deferred = entity[name]
            info = {
                'expanded': deferred.isExpanded,
                'is_collection': deferred.isCollection,
            }
            if deferred.isExpanded:
                with deferred.open() as expanded:
                    items = []
                    for child in expanded.itervalues():
                        child_values = OrderedDict()
                        for pname, pvalue in child.data_items():
                            child_values[pname] = {
                                'selected': child.is_selected(pname),
                                'value': pvalue.value
                                if hasattr(pvalue, 'value') else None,
                            }
                        items.append({
                            'key': child.key(),
                            'selected': sorted(child.selected)
                            if child.selected is not None else None,
                            'values': child_values,
                        })
                    info['items'] = items
            navs[name] = info
        decoded.append({
            'key': entity.key(),
            'selected': sorted(entity.selected)
            if entity.selected is not None else None,
            'values': values,
            'navs': navs,
        })
    return decoded


def refetch_active(client, collection, key):
    key_dict = collection.entity_set.get_key_dict(key)
    url = str(collection.base_uri) + core.ODataURI.format_key_dict(key_dict)
    options = OrderedDict()
    options[core.SystemQueryOption.select] = 'ProductID,Active'
    url = str(uri.URI.from_octets(
        url + '?' + core.ODataURI.format_sys_query_options(options)))
    request, record = fetch(client, url, 'application/atom+xml;type=entry')
    doc = core.Document(base_uri=url)
    doc.read(request.res_body)
    if not isinstance(doc.root, core.Entry):
        raise ValueError('expected entry from %s' % url)
    entity = core.Entity(collection.entity_set)
    entity.exists = True
    doc.root.get_value(entity)
    parsed = parse_entry(doc.root)
    return {
        'key': key,
        'url': url,
        'request': record,
        'raw': request.res_body,
        'parsed': parsed,
        'decoded': {
            'Active': {
                'selected': entity.is_selected('Active'),
                'value': entity['Active'].value,
            },
        },
    }


def classify(entity, client_filter, client, collection, refetch_log):
    active = entity['Active']
    if client_filter == 'naive':
        keep = active.value is True
        reason = 'naive: Active.value=%r (selected=%s)' % (
            active.value, entity.is_selected('Active'))
        return keep, reason
    elif client_filter == 'selection_aware':
        if entity.is_selected('Active'):
            keep = active.value is True
            reason = 'selection-aware: selected Active=%r' % (
                active.value,)
            return keep, reason
        refetched = refetch_active(client, collection, entity.key())
        refetch_log.append(refetched)
        value = refetched['decoded']['Active']['value']
        keep = value is True
        reason = 'selection-aware: Active absent in page, refetched=%r' % (
            value,)
        return keep, reason
    else:
        raise ValueError(client_filter)


def run_export(projection, continuation, client_filter, topmax=1, top=2,
               max_pages=12):
    svc, container, model = build_service(topmax)
    client = ForensicClient(svc)
    collection = configure_collection(client, projection)
    url = build_url(collection, skip=0)
    pages = []
    refetches = []
    kept = []
    dropped = []
    previous_next = None

    for page_number in range(1, max_pages + 1):
        request, record = fetch(client, url)
        parsed = parse_feed(request.res_body, url)
        decoded = decode_feed(collection, request.res_body, url)
        decisions = []
        for entity, dec in zip(
                [None] * len(decoded), decoded):
            # Re-create an entity for classification from the same raw entry
            # is unnecessary: dec already carries selected/value evidence, but
            # the client filter is defined on the real Entity API.  We decode
            # again cheaply to obtain the Entity object.
            pass
        doc = core.Document(base_uri=url)
        doc.read(request.res_body)
        entities = []
        for entry in doc.root.Entry:
            entity = core.Entity(collection.entity_set)
            entity.exists = True
            entry.get_value(entity)
            entities.append(entity)
        for entity in entities:
            keep, reason = classify(
                entity, client_filter, client, collection, refetches)
            decision = {
                'key': entity.key(),
                'kept': keep,
                'reason': reason,
            }
            decisions.append(decision)
            if keep:
                kept.append(entity.key())
            else:
                dropped.append({'key': entity.key(), 'reason': reason})
        page = {
            'page': page_number,
            'request_url': url,
            'request': record,
            'request_text': request_text(record),
            'response_body': request.res_body,
            'parsed': parsed,
            'decoded': decoded,
            'decisions': decisions,
            'previous_next_link': previous_next,
            'request_equals_previous_next':
                (previous_next == record['url'])
                if previous_next is not None else None,
            'next_link': parsed['next_link'],
        }
        pages.append(page)
        previous_next = parsed['next_link']
        if not parsed['entry_keys']:
            break
        if continuation == 'next':
            if parsed['next_link'] is None:
                break
            url = parsed['next_link']
        elif continuation == 'rebuilt':
            skip = page_number * top
            url = build_url(collection, skip=skip)
        else:
            raise ValueError(continuation)

    return {
        'projection': projection,
        'continuation': continuation,
        'client_filter': client_filter,
        'server_topmax': topmax,
        'requested_top': top,
        'initial_url': pages[0]['request_url'] if pages else None,
        'pages': pages,
        'refetches': refetches,
        'kept': kept,
        'dropped': dropped,
    }


def write_run(run, out_dir):
    run_dir = os.path.join(out_dir, 'raw', run['name'])
    os.makedirs(run_dir, exist_ok=True)
    for page in run['pages']:
        stem = os.path.join(run_dir, 'page-%02d' % page['page'])
        with open(stem + '.request.txt', 'wb') as f:
            f.write(page['request_text'].encode('utf-8'))
        with open(stem + '.response.xml', 'wb') as f:
            f.write(page['response_body'])
        summary = {
            'page': page['page'],
            'request_url': page['request_url'],
            'query_options': page['request']['query_options'],
            'request_equals_previous_next':
                page['request_equals_previous_next'],
            'previous_next_link': page['previous_next_link'],
            'response_status': page['request']['status'],
            'entry_keys': page['parsed']['entry_keys'],
            'field_presence': OrderedDict(
                (entry['key'], entry['field_presence'])
                for entry in page['parsed']['entries']),
            'next_link': page['next_link'],
            'client_decisions': page['decisions'],
        }
        with open(stem + '.json', 'w') as f:
            import json
            json.dump(summary, f, indent=2, sort_keys=False)
            f.write('\n')
    for index, refetch in enumerate(run['refetches'], 1):
        stem = os.path.join(run_dir, 'refetch-%02d' % index)
        with open(stem + '.request.txt', 'wb') as f:
            f.write(request_text(refetch['request']).encode('utf-8'))
        with open(stem + '.response.xml', 'wb') as f:
            f.write(refetch['raw'])
        with open(stem + '.json', 'w') as f:
            import json
            json.dump({
                'key': refetch['key'],
                'url': refetch['url'],
                'parsed': refetch['parsed'],
                'decoded': refetch['decoded'],
            }, f, indent=2, sort_keys=False)
            f.write('\n')


def region_evidence(client, collection):
    """Fetch P-0003 with and without explicit Category expansion."""
    key_url = str(collection.base_uri) + core.ODataURI.format_key_dict(
        collection.entity_set.get_key_dict(REGION_NULL_KEY))
    select = ('ProductID,Name,Warehouse,BinSequence,Active,Region')
    cases = []
    for name, expand in (('expanded', True), ('unexpanded', False)):
        options = OrderedDict()
        if expand:
            options[core.SystemQueryOption.expand] = 'Category'
        options[core.SystemQueryOption.select] = select
        url = str(uri.URI.from_octets(
            key_url + '?' +
            core.ODataURI.format_sys_query_options(options)))
        request, record = fetch(
            client, url, 'application/atom+xml;type=entry')
        parsed = parse_entry_document(request.res_body, url)
        doc = core.Document(base_uri=url)
        doc.read(request.res_body)
        entity = core.Entity(collection.entity_set)
        entity.exists = True
        doc.root.get_value(entity)
        category = entity['Category']
        decoded = {
            'Region': {
                'selected': entity.is_selected('Region'),
                'value': entity['Region'].value,
            },
            'Category': {
                'expanded': category.isExpanded,
            },
        }
        if category.isExpanded:
            with category.open() as expanded:
                child = expanded.values()[0]
                decoded['Category']['key'] = child.key()
                decoded['Category']['Region'] = {
                    'selected': child.is_selected('Region'),
                    'value': child['Region'].value,
                }
                decoded['Category']['CategoryName'] = {
                    'selected': child.is_selected('CategoryName'),
                    'value': child['CategoryName'].value,
                }
        cases.append({
            'name': name,
            'url': url,
            'request': record,
            'request_text': request_text(record),
            'response_body': request.res_body,
            'parsed': parsed,
            'decoded': decoded,
        })
    return cases


def count_evidence(client, collection):
    """Fetch the filtered $count so total-only comparisons can be audited."""
    url = str(collection.base_uri) + '/$count?' + \
        core.ODataURI.format_sys_query_options(OrderedDict([
            (core.SystemQueryOption.filter, str(collection.filter)),
        ]))
    request = http.ClientRequest(url)
    request.set_header('Accept', 'text/plain')
    client.process_request(request)
    record = client.transport.log[-1]
    if request.status != 200:
        raise RuntimeError('HTTP %s for %s' % (request.status, url))
    return {
        'url': url,
        'request': record,
        'request_text': request_text(record),
        'response_body': request.res_body,
        'count': int(request.res_body),
    }


def write_region(cases, out_dir):
    region_dir = os.path.join(out_dir, 'region')
    os.makedirs(region_dir, exist_ok=True)
    for case in cases:
        stem = os.path.join(region_dir, case['name'])
        with open(stem + '.request.txt', 'wb') as f:
            f.write(case['request_text'].encode('utf-8'))
        with open(stem + '.response.xml', 'wb') as f:
            f.write(case['response_body'])
        with open(stem + '.json', 'w') as f:
            import json
            json.dump({
                'url': case['url'],
                'parsed': case['parsed'],
                'decoded': case['decoded'],
            }, f, indent=2, sort_keys=False)
            f.write('\n')
