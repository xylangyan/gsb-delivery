#! /usr/bin/env python

"""Regression tests for OData event identity.

An event key is a pair of (Source, Effective).  Source may contain commas
and quotes.  The tests in this module use only hand-written URI literals
for expectations; Pyslet's own key/URI formatters are never used to
produce an expected value.

Two backends are exercised from the same independent record list.  The
main scenario follows the links that the service actually returned, then
checks an independently written equivalent-UTC key and a narrow time
interval.  Two controls inject a single fault each:

* PrecisionLossServer corrupts only the links in a collection response.
* SourceManglingReader corrupts only the source it reads from a link.

Their first failing stages are deliberately different so that a failure
can be attributed to the writer, the service link generation or the
reader.
"""

import gc
import io
import logging
import unittest

from pyslet import iso8601 as iso
from pyslet import rfc2396 as uri
from pyslet.odata2 import core
from pyslet.odata2 import memds
from pyslet.odata2 import metadata as edmx
from pyslet.py2 import is_text
from pyslet.odata2 import server
from pyslet.odata2 import sqlds
from pyslet.vfs import OSFilePath as FilePath


SERVICE_ROOT = "http://events.example/service.svc"

SOURCE_NORTH = 'store, "north" O\'Neil'
SOURCE_SOUTH = 'store, "south" O\'Neil'

EFFECTIVE_A_LOCAL = "2026-11-19T09:00:00.1234567+09:00"
EFFECTIVE_A_UTC = "2026-11-19T00:00:00.1234567Z"
EFFECTIVE_B_LOCAL = "2026-11-19T09:00:00.1234568+09:00"
EFFECTIVE_C_UTC = "2026-11-19T00:00:00.1234567Z"

SENTINEL_A = "EV-A-LOCAL"
SENTINEL_B = "EV-B-ADJ"
SENTINEL_C = "EV-C-OTHER"

RECORDS = (
    {"source": SOURCE_NORTH, "effective": EFFECTIVE_A_LOCAL,
     "sentinel": SENTINEL_A},
    {"source": SOURCE_NORTH, "effective": EFFECTIVE_B_LOCAL,
     "sentinel": SENTINEL_B},
    {"source": SOURCE_SOUTH, "effective": EFFECTIVE_C_UTC,
     "sentinel": SENTINEL_C},
)

EXPECTED_STORED_KEYS = (
    (SOURCE_NORTH, iso.TimePoint.from_str(EFFECTIVE_A_LOCAL)),
    (SOURCE_NORTH, iso.TimePoint.from_str(EFFECTIVE_B_LOCAL)),
    (SOURCE_SOUTH, iso.TimePoint.from_str(EFFECTIVE_C_UTC)),
)

EXPECTED_IDENTITIES = {
    SENTINEL_A: (SOURCE_NORTH, EFFECTIVE_A_LOCAL),
    SENTINEL_B: (SOURCE_NORTH, EFFECTIVE_B_LOCAL),
    SENTINEL_C: (SOURCE_SOUTH, EFFECTIVE_C_UTC),
}

# Hand-written from the independent record list above.  In particular the
# source encoding is part of the expectation: a comma is %2C, a double
# quote is %22 and an apostrophe is doubled inside the quoted string.
EXPECTED_LINKS = {
    SENTINEL_A: SERVICE_ROOT + "/Events(Source%3D'store%2C%20%22north%22"
                "%20O''Neil'%2CEffective%3Ddatetimeoffset'2026-11-19T09%3A"
                "00%3A00.1234567%2B09%3A00')",
    SENTINEL_B: SERVICE_ROOT + "/Events(Source%3D'store%2C%20%22north%22"
                "%20O''Neil'%2CEffective%3Ddatetimeoffset'2026-11-19T09%3A"
                "00%3A00.1234568%2B09%3A00')",
    SENTINEL_C: SERVICE_ROOT + "/Events(Source%3D'store%2C%20%22south%22"
                "%20O''Neil'%2CEffective%3Ddatetimeoffset'2026-11-19T00%3A"
                "00%3A00.1234567%2B00%3A00')",
}

STORED_KEYS_URI = SERVICE_ROOT + \
                  "/Events?$select=Source,Effective,Sentinel"
PROJECTION_URI = SERVICE_ROOT + "/Events?$select=Sentinel"

# Independently written key URI: the same instant as event A but in UTC.
EVENT_A_UTC_URI = SERVICE_ROOT + "/Events(Source='store%2C%20%22north%22" \
                  "%20O''Neil',Effective=datetimeoffset'2026-11-19T00%3A" \
                  "00%3A00.1234567Z')"
# Independently written key URI for the third event, same instant as A but
# a different source.
EVENT_C_UTC_URI = SERVICE_ROOT + "/Events(Source='store%2C%20%22south%22" \
                  "%20O''Neil',Effective=datetimeoffset'2026-11-19T00%3A" \
                  "00%3A00.1234567Z')"
# Narrow interval: strictly after A and up to B.  Only event B is adjacent.
ADJACENT_URI = SERVICE_ROOT + "/Events?$filter=Effective%20gt%20" \
               "datetimeoffset'2026-11-19T00%3A00%3A00.1234567Z'%20and" \
               "%20Effective%20le%20datetimeoffset'2026-11-19T00%3A00%3A" \
               "00.1234568Z'"

STAGE_WRITE = "1-write-keys"
STAGE_PROJECTION = "2-projection"
STAGE_LINK_READ = "3-read-service-links"
STAGE_UTC = "4-independent-utc"
STAGE_ADJACENT = "5-narrow-adjacent"
STAGE_OTHER = "6-independent-other-source"
STAGE_ORDER = (STAGE_WRITE, STAGE_PROJECTION, STAGE_LINK_READ,
               STAGE_UTC, STAGE_ADJACENT, STAGE_OTHER)

expectedFailure = getattr(unittest, 'expectedFailure', lambda f: f)

TEST_DATA_DIR = FilePath(
    FilePath(__file__).abspath().split()[0], 'data_odatav2')


def suite(prefix='test'):
    loader = unittest.TestLoader()
    loader.testMethodPrefix = prefix
    return unittest.TestSuite((
        loader.loadTestsFromTestCase(EventIdentityTests),
    ))


def load_tests(loader, tests, pattern):
    return suite()


def load_model():
    doc = edmx.Document()
    md_path = TEST_DATA_DIR.join('sample_server', 'event_identity.xml')
    with md_path.open('rb') as f:
        doc.read(f)
    return doc


def same_instant(actual, expected):
    if actual is None:
        return False
    try:
        expected = iso.TimePoint.from_str(expected)
    except iso.DateTimeError:
        return False
    try:
        return actual == expected
    except iso.DateTimeError:
        return False


def display_time_key(key):
    source, effective = key
    if effective is None:
        text = None
    else:
        text = effective.get_calendar_string(ndp=7, dp=".")
    return display_key(source, text)


def display_key(source, effective):
    if source is None:
        return None
    return "%s|%s" % (source, effective)


def stored_key(entity):
    value = entity['Effective'].value
    if value is None:
        effective = None
    else:
        effective = value.get_calendar_string(ndp=7, dp=".")
    return entity['Source'].value, effective


def property_values(entry):
    values = {}
    if entry.Content is None or entry.Content.Properties is None:
        return values
    for p in entry.Content.Properties.Property:
        values[p.xmlname] = p.get_value()
    return values


def entity_evidence(entry):
    values = property_values(entry)
    source = values.get('Source')
    effective = values.get('Effective')
    sentinel = values.get('Sentinel')
    effective_value = None
    effective_text = None
    if effective is not None:
        effective_value = effective.value
        if is_text(effective_value):
            try:
                effective_value = iso.TimePoint.from_str(effective_value)
            except iso.DateTimeError:
                effective_value = None
        if isinstance(effective_value, iso.TimePoint):
            effective_text = effective_value.get_calendar_string(
                ndp=7, dp=".")
        else:
            effective_text = str(effective_value)
    return {
        "source": source.value if source is not None else None,
        "effective": effective_text,
        "effective_value": effective_value,
        "sentinel": sentinel.value if sentinel is not None else None,
    }


class StageFailure(object):

    def __init__(self, stage, kind, message):
        self.stage = stage
        self.kind = kind
        self.message = message

    def __str__(self):
        return "[%s/%s] %s" % (self.stage, self.kind, self.message)


class StageReport(object):

    def __init__(self, name):
        self.name = name
        self.records = []
        self.failures = []

    def record(self, action, request_uri, status, key_evidence, sentinel):
        item = {
            "action": action,
            "request": request_uri,
            "status": status,
            "key": key_evidence,
            "sentinel": sentinel,
        }
        self.records.append(item)
        logging.info("[%s] %s %s -> %s key=%s sentinel=%s",
                     self.name, action, request_uri, status,
                     key_evidence, sentinel)

    def fail(self, kind, message):
        failure = StageFailure(self.name, kind, message)
        self.failures.append(failure)
        logging.error("%s", failure)

    def format(self):
        lines = ["stage %s:" % self.name]
        for r in self.records:
            lines.append("  %s %s -> %s key=%s sentinel=%s" % (
                r["action"], r["request"], r["status"], r["key"],
                r["sentinel"]))
        for f in self.failures:
            lines.append("  FAIL %s" % f)
        return "\n".join(lines)


class Scenario(object):

    def __init__(self):
        self.stages = {}
        for name in STAGE_ORDER:
            self.stages[name] = StageReport(name)

    def stage(self, name):
        return self.stages[name]

    def first_failure(self):
        for name in STAGE_ORDER:
            if self.stages[name].failures:
                return self.stages[name].failures[0]
        return None

    def first_failed_stage(self):
        failure = self.first_failure()
        return failure.stage if failure is not None else None

    def report(self):
        return "\n".join(self.stages[name].format()
                         for name in STAGE_ORDER)


class WSGIResponse(object):

    def __init__(self, request_uri, status_line, headers, body):
        self.request_uri = request_uri
        self.status_line = status_line
        self.headers = headers
        self.body = body
        self.status = int(status_line.split()[0])

    def feed_entries(self):
        doc = core.Document(base_uri=self.request_uri)
        doc.read(self.body)
        if not isinstance(doc.root, core.Feed):
            raise ValueError("Expected a feed, found %s" %
                             doc.root.__class__.__name__)
        return doc.root.Entry

    def entity(self):
        doc = core.Document(base_uri=self.request_uri)
        doc.read(self.body)
        if not isinstance(doc.root, core.Entry):
            raise ValueError("Expected an entry, found %s" %
                             doc.root.__class__.__name__)
        return doc.root


class WSGITransport(object):

    """A minimal, fresh WSGI client for each event identity check."""

    def __init__(self, app):
        self.app = app

    def get(self, request_uri, accept="application/atom+xml"):
        url = uri.URI.from_octets(request_uri)
        path = uri.unescape_data(url.abs_path or '/').decode('utf-8')
        environ = {
            'REQUEST_METHOD': 'GET',
            'SCRIPT_NAME': '',
            'PATH_INFO': path,
            'QUERY_STRING': url.query or '',
            'SERVER_NAME': url.host or 'events.example',
            'SERVER_PORT': str(url.port or 80),
            'SERVER_PROTOCOL': 'HTTP/1.1',
            'REMOTE_ADDR': '127.0.0.1',
            'wsgi.version': (1, 0),
            'wsgi.url_scheme': 'http',
            'wsgi.input': io.BytesIO(b''),
            'wsgi.errors': io.BytesIO(),
            'wsgi.multithread': True,
            'wsgi.multiprocess': False,
            'wsgi.run_once': False,
            'CONTENT_LENGTH': '0',
            'HTTP_ACCEPT': accept,
            'HTTP_DATASERVICEVERSION': '2.0',
            'HTTP_MAXDATASERVICEVERSION': '2.0',
        }
        state = {}

        def start_response(status, headers, exc_info=None):
            state['status'] = status
            state['headers'] = headers

        body = b''.join(self.app(environ, start_response))
        return WSGIResponse(request_uri, state['status'],
                            state['headers'], body)


class PrecisionLossServer(server.Server):

    """Test double: lose the last declared digit only in response links.

    Storage and entity reads are untouched, so only the projection stage
    sees the injected fault.  This mirrors a service whose write path and
    storage preserve the key but whose link writer rounds the timestamp."""

    LINK_CORRUPTIONS = (
        (".1234567%2B09%3A00", ".1234560%2B09%3A00"),
        (".1234567%2B00%3A00", ".1234560%2B00%3A00"),
    )

    def return_entity_collection(self, entities, request, environ,
                                 start_response, response_headers):
        response_type = self.content_negotiation(
            request, environ, self.FeedTypes)
        if response_type is None:
            return self.odata_error(
                request, environ, start_response, "Not Acceptable",
                'xml, json or plain text formats supported', 406)
        if response_type == "application/json":
            return super(PrecisionLossServer, self).return_entity_collection(
                entities, request, environ, start_response, response_headers)
        entities.set_topmax(self.topmax)
        feed = core.Feed(None, entities)
        doc = core.Document(root=feed)
        feed.collection = entities
        feed.set_base(str(self.service_root))
        data = str(doc)
        for old, new in self.LINK_CORRUPTIONS:
            data = data.replace(old, new)
        data = data.encode('utf-8')
        response_headers.append(("Content-Type", str(response_type)))
        response_headers.append(("Content-Length", str(len(data))))
        start_response("%i %s" % (200, "Success"), response_headers)
        return [data]


class NormalReader(object):

    def __init__(self, app):
        self.app = app

    def reference(self, service_link, sentinel):
        return service_link

    def new_client(self):
        return WSGITransport(self.app)


class SourceManglingReader(NormalReader):

    """Test double: mis-parse the quoted source read from a service link.

    The service's projection links are correct; the reader changes only
    the source token it sends when following them.  This mirrors a client
    that tokenises the composite key on a comma inside the quoted source."""

    def reference(self, service_link, sentinel):
        if '%22north%22' in service_link:
            return service_link.replace('%22north%22', '%22south%22')
        return service_link


class EventIdentityTests(unittest.TestCase):

    def setUp(self):        # noqa
        self.containers = []
        self.tempdirs = []

    def tearDown(self):     # noqa
        # The server's feed generator leaves its collection open until the
        # response object is collected; drain it before closing SQL
        # connections so the pool does not report a forced close.
        gc.collect()
        for container in self.containers:
            try:
                container.close()
            except Exception:
                pass
        for d in self.tempdirs:
            try:
                d.rmtree(True)
            except Exception:
                pass

    def make_backend(self, kind):
        doc = load_model()
        data_services = doc.root.DataServices
        container_def = data_services['EventModel.EventContainer']
        entity_set = data_services['EventModel.EventContainer.Events']
        if kind == 'memory':
            container = memds.InMemoryEntityContainer(container_def)
        elif kind == 'sqlite':
            d = FilePath.mkdtemp('.d', 'pyslet-event-identity-')
            self.tempdirs.append(d)
            container = sqlds.SQLiteEntityContainer(
                file_path=d.join('events.db'),
                container=container_def,
                streamstore=sqlds.SQLiteStreamStore(
                    file_path=d.join('streams.db')))
            container.create_all_tables()
        else:
            raise ValueError("Unknown backend: %s" % kind)
        self.containers.append(container)
        return doc, container, entity_set

    def write_records(self, scenario, entity_set):
        stage = scenario.stage(STAGE_WRITE)
        with entity_set.open() as collection:
            for record in RECORDS:
                entity = collection.new_entity()
                entity['Source'].set_from_value(record['source'])
                entity['Effective'].set_from_value(
                    iso.TimePoint.from_str(record['effective']))
                entity['Sentinel'].set_from_value(record['sentinel'])
                try:
                    collection.insert_entity(entity)
                except Exception as e:
                    message = str(e)
                    if "UNIQUE constraint failed" in message:
                        kind = "write-merged"
                        detail = "write merged event %s (%s)" % (
                            record['sentinel'], message)
                    else:
                        kind = "write-error"
                        detail = "write failed for %s: %s" % (
                            record['sentinel'], message)
                    stage.fail(kind, detail)
        actual = []
        with entity_set.open() as collection:
            for entity in collection.itervalues():
                text_key = stored_key(entity)
                actual.append((entity['Source'].value,
                               entity['Effective'].value))
                stage.record(
                    "backend-scan", "backend://Events", 200,
                    display_key(text_key[0], text_key[1]),
                    entity['Sentinel'].value)
        if sorted(actual) != sorted(EXPECTED_STORED_KEYS):
            missing = [k for k in EXPECTED_STORED_KEYS if k not in actual]
            extra = [k for k in actual if k not in EXPECTED_STORED_KEYS]
            stage.fail(
                "write-keys",
                "stored key set differs: missing=%s extra=%s actual=%s" % (
                    [display_time_key(k) for k in missing],
                    [display_time_key(k) for k in extra],
                    [display_time_key(k) for k in sorted(actual)]))

    def check_stored_keys(self, scenario, transport):
        stage = scenario.stage(STAGE_WRITE)
        response = transport.get(STORED_KEYS_URI)
        if response.status != 200:
            stage.record("stored-keys", STORED_KEYS_URI, response.status,
                         None, None)
            stage.fail("write-keys",
                       "stored key request returned %i" % response.status)
            return
        try:
            entries = response.feed_entries()
        except Exception as e:
            stage.record("stored-keys", STORED_KEYS_URI, response.status,
                         None, None)
            stage.fail("write-keys", "bad stored key response: %s" % e)
            return
        observed = []
        for entry in entries:
            evidence = entity_evidence(entry)
            stage.record(
                "stored-keys", STORED_KEYS_URI, response.status,
                display_key(evidence['source'], evidence['effective']),
                evidence['sentinel'])
            observed.append((evidence['source'], evidence['effective_value']))
        if sorted(observed) != sorted(EXPECTED_STORED_KEYS):
            stage.fail(
                "write-keys",
                "stored key response differs: got %s expected %s" % (
                    [display_time_key(k) for k in sorted(observed)],
                    [display_time_key(k) for k in sorted(
                        EXPECTED_STORED_KEYS)]))

    def check_projection(self, scenario, transport):
        stage = scenario.stage(STAGE_PROJECTION)
        response = transport.get(PROJECTION_URI)
        if response.status != 200:
            stage.record("projection-feed", PROJECTION_URI,
                         response.status, None, None)
            stage.fail("projection", "projection request failed: %i" %
                       response.status)
            return {}
        try:
            entries = response.feed_entries()
        except Exception as e:
            stage.record("projection-feed", PROJECTION_URI,
                         response.status, None, None)
            stage.fail("projection", "bad projection response: %s" % e)
            return {}
        if len(entries) != len(RECORDS):
            stage.fail(
                "projection-count",
                "projection returned %i entries, expected %i" %
                (len(entries), len(RECORDS)))
        observed = {}
        for entry in entries:
            values = property_values(entry)
            names = sorted(values.keys())
            sentinel = values.get('Sentinel')
            sentinel = sentinel.value if sentinel is not None else None
            atom_id = str(entry.AtomId.get_value())
            edit_link = None
            for link in entry.Link:
                if link.rel == 'edit':
                    edit_link = str(link.href)
                    break
            evidence = {
                "id": atom_id,
                "edit": edit_link,
                "properties": names,
            }
            stage.record("projection-entry", PROJECTION_URI,
                         response.status, evidence, sentinel)
            if names != ['Sentinel']:
                stage.fail(
                    "projection-leak",
                    "projection leaked key fields: got %s" % names)
            if sentinel not in EXPECTED_LINKS:
                stage.fail(
                    "projection-sentinel",
                    "unexpected sentinel in projection: %s" % sentinel)
                continue
            expected = EXPECTED_LINKS[sentinel]
            if atom_id != expected:
                stage.fail(
                    "service-link",
                    "service wrote a different id for %s: got %s expected %s"
                    % (sentinel, atom_id, expected))
            if edit_link != expected:
                stage.fail(
                    "service-link",
                    "service wrote a different edit link for %s: got %s "
                    "expected %s" % (sentinel, edit_link, expected))
            observed[sentinel] = edit_link or atom_id
        missing = [s for s in EXPECTED_LINKS if s not in observed]
        if missing:
            stage.fail("projection-count",
                       "projection missing events: %s" % missing)
        return observed

    def check_service_links(self, scenario, transport, observed, reader):
        stage = scenario.stage(STAGE_LINK_READ)
        for sentinel in sorted(observed):
            service_link = observed[sentinel]
            request_uri = reader.reference(service_link, sentinel)
            client = reader.new_client()
            response = client.get(request_uri)
            if response.status != 200:
                if request_uri != service_link:
                    kind = "reader-link"
                    detail = ("reader requested %s for %s (service link was "
                              "%s), got %i" % (request_uri, sentinel,
                                               service_link, response.status))
                else:
                    kind = "service-link"
                    detail = ("service link %s for %s returned %i" %
                              (service_link, sentinel, response.status))
                stage.record("read-service-link", request_uri,
                             response.status, None, None)
                stage.fail(kind, detail)
                continue
            try:
                entry = response.entity()
                evidence = entity_evidence(entry)
            except Exception as e:
                stage.record("read-service-link", request_uri,
                             response.status, None, None)
                stage.fail("reader-link",
                           "could not read entity from %s: %s" %
                           (request_uri, e))
                continue
            stage.record(
                "read-service-link", request_uri, response.status,
                display_key(evidence['source'], evidence['effective']),
                evidence['sentinel'])
            expected_source, expected_effective = EXPECTED_IDENTITIES[sentinel]
            if evidence['sentinel'] != sentinel:
                kind = ("reader-link" if request_uri != service_link
                        else "service-link")
                stage.fail(
                    kind,
                    "link for %s returned sentinel %s via %s" %
                    (sentinel, evidence['sentinel'], request_uri))
            if evidence['source'] != expected_source:
                kind = ("reader-link" if request_uri != service_link
                        else "service-link")
                stage.fail(
                    kind,
                    "link for %s returned source %r expected %r via %s" %
                    (sentinel, evidence['source'], expected_source,
                     request_uri))
            if not same_instant(evidence['effective_value'],
                                expected_effective):
                kind = ("reader-link" if request_uri != service_link
                        else "service-link")
                stage.fail(
                    kind,
                    "link for %s returned effective %s expected %s via %s" %
                    (sentinel, evidence['effective'], expected_effective,
                     request_uri))

    def check_one_key(self, scenario, transport, request_uri, stage_name,
                      expected_sentinel, expected_source, expected_effective,
                      kind):
        stage = scenario.stage(stage_name)
        response = transport.get(request_uri)
        if response.status != 200:
            stage.record(stage_name, request_uri, response.status, None, None)
            stage.fail(kind, "%s returned %i" % (request_uri,
                                                 response.status))
            return None
        try:
            evidence = entity_evidence(response.entity())
        except Exception as e:
            stage.record(stage_name, request_uri, response.status, None, None)
            stage.fail(kind, "could not read entity from %s: %s" %
                       (request_uri, e))
            return None
        stage.record(
            stage_name, request_uri, response.status,
            display_key(evidence['source'], evidence['effective']),
            evidence['sentinel'])
        if evidence['sentinel'] != expected_sentinel:
            stage.fail(kind, "%s returned sentinel %s expected %s" %
                       (request_uri, evidence['sentinel'],
                        expected_sentinel))
        if evidence['source'] != expected_source:
            stage.fail(kind, "%s returned source %r expected %r" %
                       (request_uri, evidence['source'], expected_source))
        if not same_instant(evidence['effective_value'],
                            expected_effective):
            stage.fail(kind, "%s returned effective %s expected %s" %
                       (request_uri, evidence['effective'],
                        expected_effective))
        return evidence

    def check_adjacent(self, scenario, transport):
        stage = scenario.stage(STAGE_ADJACENT)
        response = transport.get(ADJACENT_URI)
        if response.status != 200:
            stage.record("narrow-adjacent", ADJACENT_URI,
                         response.status, None, None)
            stage.fail("adjacent-merge",
                       "narrow interval returned %i" % response.status)
            return
        try:
            entries = response.feed_entries()
        except Exception as e:
            stage.record("narrow-adjacent", ADJACENT_URI,
                         response.status, None, None)
            stage.fail("adjacent-merge",
                       "bad narrow interval response: %s" % e)
            return
        sentinels = []
        key_evidence = []
        for entry in entries:
            values = property_values(entry)
            sentinel = values.get('Sentinel')
            sentinels.append(sentinel.value if sentinel is not None else None)
            atom_id = str(entry.AtomId.get_value())
            edit_link = None
            for link in entry.Link:
                if link.rel == 'edit':
                    edit_link = str(link.href)
                    break
            key_evidence.append({"id": atom_id, "edit": edit_link})
        stage.record("narrow-adjacent", ADJACENT_URI, response.status,
                     key_evidence, sentinels)
        if sentinels != [SENTINEL_B]:
            stage.fail(
                "adjacent-merge",
                "narrow interval returned %s, expected only %s" %
                (sentinels, SENTINEL_B))

    def run_scenario(self, kind, server_class=server.Server,
                     reader_class=NormalReader):
        doc, container, entity_set = self.make_backend(kind)
        scenario = Scenario()
        self.write_records(scenario, entity_set)
        app = server_class(SERVICE_ROOT)
        app.set_model(doc)
        transport = WSGITransport(app)
        self.check_stored_keys(scenario, transport)
        observed = self.check_projection(scenario, transport)
        reader = reader_class(app)
        self.check_service_links(scenario, transport, observed, reader)
        utc_evidence = self.check_one_key(
            scenario, WSGITransport(app), EVENT_A_UTC_URI, STAGE_UTC,
            SENTINEL_A, SOURCE_NORTH, EFFECTIVE_A_LOCAL, "independent-utc")
        if utc_evidence is not None and \
                utc_evidence['effective_value'] is not None:
            if utc_evidence['effective_value'] != iso.TimePoint.from_str(
                    EFFECTIVE_A_UTC):
                scenario.stage(STAGE_UTC).fail(
                    "independent-utc",
                    "UTC key did not resolve to the same instant as %s" %
                    EFFECTIVE_A_UTC)
        self.check_adjacent(scenario, transport)
        self.check_one_key(
            scenario, WSGITransport(app), EVENT_C_UTC_URI, STAGE_OTHER,
            SENTINEL_C, SOURCE_SOUTH, EFFECTIVE_C_UTC,
            "independent-other-source")
        return scenario

    def assert_stage_passed(self, scenario, name):
        stage = scenario.stage(name)
        self.assertFalse(
            stage.failures,
            "stage %s failed:\n%s" % (name, scenario.report()))

    def test_memory_backend_event_identity(self):
        scenario = self.run_scenario('memory')
        for name in STAGE_ORDER:
            self.assert_stage_passed(scenario, name)

    @expectedFailure
    def test_sqlite_backend_event_identity(self):
        scenario = self.run_scenario('sqlite')
        for name in STAGE_ORDER:
            self.assert_stage_passed(scenario, name)

    def test_sqlite_pinned_limitation_is_write_stage_merge(self):
        scenario = self.run_scenario('sqlite')
        first = scenario.first_failure()
        self.assertTrue(first is not None, scenario.report())
        self.assertTrue(
            first.stage == STAGE_WRITE,
            "first failure should be the write stage, got %s" % first)
        self.assertTrue(
            first.kind == "write-merged",
            "first failure should be a write merge, got %s" % first)

    def test_controls_have_distinct_first_failure_stages(self):
        service_run = self.run_scenario(
            'memory', server_class=PrecisionLossServer)
        reader_run = self.run_scenario(
            'memory', reader_class=SourceManglingReader)

        self.assert_stage_passed(service_run, STAGE_WRITE)
        self.assertTrue(
            service_run.stage(STAGE_PROJECTION).failures,
            service_run.report())
        self.assertTrue(
            service_run.first_failure().kind == "service-link",
            "service control should fail on link evidence, got %s" %
            service_run.first_failure())
        self.assert_stage_passed(service_run, STAGE_UTC)
        self.assert_stage_passed(service_run, STAGE_ADJACENT)

        self.assert_stage_passed(reader_run, STAGE_WRITE)
        self.assert_stage_passed(reader_run, STAGE_PROJECTION)
        self.assertTrue(
            reader_run.stage(STAGE_LINK_READ).failures,
            reader_run.report())
        self.assertTrue(
            reader_run.first_failure().kind == "reader-link",
            "reader control should fail on the link read, got %s" %
            reader_run.first_failure())
        self.assert_stage_passed(reader_run, STAGE_UTC)
        self.assert_stage_passed(reader_run, STAGE_ADJACENT)

        self.assertTrue(
            service_run.first_failed_stage() !=
            reader_run.first_failed_stage(),
            "controls must not share a first failure stage: %s" %
            service_run.first_failed_stage())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    unittest.main()
