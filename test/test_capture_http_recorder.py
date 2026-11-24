"""
Socket-free tests for the capture_http recording machinery.

The environment these run in forbids binding real sockets, so the tests drive
the real ``http.client`` request/response state machine against in-memory fake
sockets. That still exercises response_class selection, RecordingStream,
framing detection, truncation marking and the CaptureGroup coordination.
"""
import threading

from io import BytesIO

import pytest

from warcio.capture_http import (
    capture_http,
    CaptureGroup,
    RecordingHTTPConnection,
)
from warcio.archiveiterator import ArchiveIterator
from warcio.warcwriter import BufferWARCWriter


# ============================================================================
class SharedFP(object):
    """
    A per-makefile() view over one shared byte stream, mirroring a real socket
    where closing one makefile object does not close the underlying socket.
    """
    def __init__(self, shared):
        self._shared = shared
        self._closed = False

    def _check(self):
        if self._closed:
            raise ValueError('I/O operation on closed file.')

    def read(self, amt=-1):
        self._check()
        if amt is None or amt < 0:
            return self._shared.read()
        return self._shared.read(amt)

    def read1(self, amt=-1):
        return self.read(amt)

    def readinto(self, buff):
        data = self.read(len(buff))
        count = len(data)
        buff[:count] = data
        return count

    def readline(self, maxlen=-1):
        self._check()
        if maxlen is None or maxlen < 0:
            return self._shared.readline()
        return self._shared.readline(maxlen)

    def peek(self, amt=-1):
        self._check()
        pos = self._shared.tell()
        data = self._shared.read(amt if amt and amt > 0 else -1)
        self._shared.seek(pos)
        return data

    def tell(self):
        return self._shared.tell()

    def seek(self, pos):
        return self._shared.seek(pos)

    def flush(self):
        pass

    def close(self):
        self._closed = True


class FakeSocket(object):
    def __init__(self, response_bytes, fp=None, peer=('127.0.0.1', 80)):
        self.response_bytes = response_bytes
        self._shared = fp if fp is not None else BytesIO(response_bytes)
        self.peer = peer
        self.sent = []
        self.closed = False

    def makefile(self, mode='rb', buffering=None, *args, **kwargs):
        return SharedFP(self._shared)

    def sendall(self, data):
        self.sent.append(data)

    def send(self, data):
        self.sendall(data)

    def setsockopt(self, *args, **kwargs):
        pass

    def settimeout(self, *args, **kwargs):
        pass

    def getpeername(self):
        return self.peer

    def shutdown(self, *args, **kwargs):
        pass

    def close(self):
        self.closed = True


class ShortReadFP(BytesIO):
    """
    A response body stream that hands out at most ``chunk`` bytes per read and
    then signals EOF, emulating a connection cut mid-body. readinto mirrors the
    short behaviour so the recorder's partial-buffer handling is exercised.
    """
    def __init__(self, data, chunk=3):
        super(ShortReadFP, self).__init__(data)
        self.chunk = chunk

    def read(self, amt=-1):
        if amt is None or amt < 0:
            amt = self.chunk
        return super(ShortReadFP, self).read(min(amt, self.chunk))

    def read1(self, amt=-1):
        return self.read(amt)

    def readinto(self, buff):
        data = self.read(len(buff))
        count = len(data)
        buff[:count] = data
        return count


def http_response(body, content_length=None, chunked=False,
                  status='200 OK', extra_headers=''):
    if content_length is None:
        content_length = len(body)
    head = 'HTTP/1.1 {status}\r\n'.format(status=status)
    if chunked:
        head += 'Transfer-Encoding: chunked\r\n'
    else:
        head += 'Content-Length: {0}\r\n'.format(content_length)
    head += extra_headers
    head += '\r\n'
    return head.encode('latin-1') + body


def make_connection(response_bytes=None, fp=None, host='example.com'):
    conn = RecordingHTTPConnection(host)
    conn.sock = FakeSocket(response_bytes or b'', fp=fp)
    return conn


def make_group():
    writer = BufferWARCWriter(gzip=False)
    group = CaptureGroup(writer, None, True, None, owns_out=False)
    return group, writer


def use_group(group):
    RecordingHTTPConnection.local.group = group


def clear_group():
    RecordingHTTPConnection.local.group = None


def read_records(writer):
    # ArchiveIterator records share one underlying stream, so drain each
    # record's payload while iterating and return light, fully-read snapshots
    stream = writer.get_stream()
    snapshots = []
    for record in ArchiveIterator(stream):
        snapshots.append(RecordSnapshot(record))
    return snapshots


class RecordSnapshot(object):
    def __init__(self, record):
        self.rec_type = record.rec_type
        self.headers = record.rec_headers
        self.http_headers = record.http_headers
        self.payload = record.content_stream().read()

    @property
    def rec_headers(self):
        return self.headers

    def content_stream(self):
        return BytesIO(self.payload)


def pairs(writer):
    return [
        (
            record.rec_type,
            record.rec_headers.get_header('WARC-Target-URI'),
            record.rec_headers.get_header('WARC-Truncated'),
            record.payload if record.rec_type == 'response' else None,
        )
        for record in read_records(writer)
    ]


# ============================================================================
class TestBasicCapture(object):
    def test_full_request_response_pair(self):
        group, writer = make_group()
        body = b'{"ok": true}'
        use_group(group)
        try:
            conn = make_connection(http_response(body))
            conn.request('GET', '/get?foo=bar')
            resp = conn.getresponse()
            assert resp.read() == body
            resp.close()
        finally:
            clear_group()

        records = read_records(writer)
        assert len(records) == 2
        response, request = records
        assert response.rec_type == 'response'
        assert response.rec_headers['WARC-Target-URI'] == 'http://example.com/get?foo=bar'
        assert 'WARC-Truncated' not in response.rec_headers
        assert response.content_stream().read() == body
        assert request.rec_type == 'request'
        assert request.rec_headers['WARC-Target-URI'] == 'http://example.com/get?foo=bar'
        assert request.rec_headers['WARC-Concurrent-To'] == response.rec_headers['WARC-Record-ID']

    def test_disabled_recording_leaves_pool_connection_plain(self):
        # connection created before any capture starts
        conn = make_connection(http_response(b'plain'))
        clear_group()

        conn.request('GET', '/not-recorded')
        resp = conn.getresponse()
        assert resp.read() == b'plain'
        resp.close()

        # no archive involved at all
        group, writer = make_group()
        assert read_records(writer) == []

    def test_pool_connection_created_before_capture_records_later(self):
        # mirrors a connection pool that outlives the capture scopes
        conn = make_connection(http_response(b'early'))
        clear_group()

        # an unrecorded interaction before any capture
        conn.request('GET', '/before')
        conn.getresponse().read()

        group, writer = make_group()
        use_group(group)
        try:
            # same pooled connection, now inside a capture: putrequest must
            # switch it to recording and bind the active archive
            conn.sock = FakeSocket(http_response(b'after'))
            conn.request('GET', '/after')
            resp = conn.getresponse()
            assert resp.read() == b'after'
            resp.close()
        finally:
            clear_group()

        captured = pairs(writer)
        assert len(captured) == 2
        assert all(uri == 'http://example.com/after' for _, uri, _, _ in captured)
        assert captured[0][3] == b'after'

    def test_pool_connection_used_after_capture_is_not_recorded(self):
        group, writer = make_group()
        conn = make_connection(http_response(b'first'))

        use_group(group)
        conn.request('GET', '/in-a')
        conn.getresponse().read()
        clear_group()

        # same pooled connection, new fake response, no active capture
        conn.sock = FakeSocket(http_response(b'second'))
        conn.request('GET', '/outside')
        resp = conn.getresponse()
        assert type(resp).__name__ == 'HTTPResponse'
        assert resp.read() == b'second'
        resp.close()

        captured = pairs(writer)
        assert len(captured) == 2
        assert all(uri == 'http://example.com/in-a' for _, uri, _, _ in captured)


class TestArchiveBinding(object):
    def test_recording_follows_request_not_thread_or_connection(self):
        group_a, writer_a = make_group()
        group_b, writer_b = make_group()

        # one long-lived pooled connection object, reused across both archives
        conn = make_connection(http_response(b'aaaa'))

        # A starts a request and only reads part of the body
        use_group(group_a)
        conn.sock = FakeSocket(http_response(b'aaaa', content_length=4))
        conn.request('GET', '/a1')
        resp_a = conn.getresponse()
        assert resp_a.read(2) == b'aa'

        # the same thread enters B, using a different pooled connection
        conn_b = make_connection(http_response(b'bbb'))
        use_group(group_b)
        conn_b.request('GET', '/b')
        resp_b = conn_b.getresponse()
        assert resp_b.read() == b'bbb'
        resp_b.close()

        # back to A: finish the first response on the now-idle connection
        use_group(group_a)
        assert resp_a.read() == b'aa'
        resp_a.close()
        clear_group()

        assert pairs(writer_a) == [
            ('response', 'http://example.com/a1', None, b'aaaa'),
            ('request', 'http://example.com/a1', None, None),
        ]
        assert pairs(writer_b) == [
            ('response', 'http://example.com/b', None, b'bbb'),
            ('request', 'http://example.com/b', None, None),
        ]

    def test_reused_connection_binds_each_request_to_active_archive(self):
        group_a, writer_a = make_group()
        group_b, writer_b = make_group()
        conn = make_connection()

        use_group(group_a)
        conn.sock = FakeSocket(http_response(b'one'))
        conn.request('GET', '/a/1')
        conn.getresponse().read()

        use_group(group_b)
        conn.sock = FakeSocket(http_response(b'two'))
        conn.request('GET', '/b/1')
        conn.getresponse().read()

        use_group(group_a)
        conn.sock = FakeSocket(http_response(b'three'))
        conn.request('GET', '/a/2')
        conn.getresponse().read()
        clear_group()

        uris_a = [uri for _, uri, _, _ in pairs(writer_a)]
        uris_b = [uri for _, uri, _, _ in pairs(writer_b)]
        assert uris_a == [
            'http://example.com/a/1', 'http://example.com/a/1',
            'http://example.com/a/2', 'http://example.com/a/2',
        ]
        assert uris_b == [
            'http://example.com/b/1', 'http://example.com/b/1',
        ]


class TestTruncation(object):
    def test_short_read_records_only_received_bytes_and_marks_truncated(self):
        group, writer = make_group()
        full_body = b'abcdefghij'
        # claim 100 bytes but the stream dies after the real body
        raw = http_response(full_body, content_length=100)
        fp = ShortReadFP(raw, chunk=3)

        use_group(group)
        try:
            conn = make_connection(fp=fp)
            conn.request('GET', '/partial')
            resp = conn.getresponse()
            # caller hands over a 64 byte buffer, the wire only has 3 bytes
            chunk = resp.read(64)
            assert chunk == b'abc'
            # caller gives up early and closes the response
            resp.close()
        finally:
            clear_group()

        records = read_records(writer)
        response = records[0]
        assert response.rec_headers['WARC-Truncated'] == 'unspecified'
        # original HTTP headers are retained, including the advertised length
        assert response.http_headers.get_header('Content-Length') == '100'
        # only the bytes actually received can be read back
        assert response.content_stream().read() == b'abc'

    def test_completed_content_length_is_not_truncated(self):
        group, writer = make_group()
        body = b'xyz'
        use_group(group)
        try:
            conn = make_connection(http_response(body))
            conn.request('GET', '/full')
            resp = conn.getresponse()
            resp.read()
            resp.close()
        finally:
            clear_group()

        response = read_records(writer)[0]
        assert 'WARC-Truncated' not in response.rec_headers
        assert response.content_stream().read() == body

    def test_chunked_complete(self):
        group, writer = make_group()
        body = b'5\r\nhello\r\n0\r\n\r\n'
        use_group(group)
        try:
            conn = make_connection(http_response(body, chunked=True))
            conn.request('GET', '/chunked')
            resp = conn.getresponse()
            assert resp.read() == b'hello'
            resp.close()
        finally:
            clear_group()

        response = read_records(writer)[0]
        assert 'WARC-Truncated' not in response.rec_headers
        assert response.content_stream().read() == b'hello'

    def test_chunked_cut_mid_body_is_truncated(self):
        group, writer = make_group()
        # announces a 5 byte chunk but the stream dies after 3 bytes
        body = b'5\r\nhel'
        use_group(group)
        try:
            conn = make_connection(http_response(body, chunked=True))
            conn.request('GET', '/chunked-cut')
            resp = conn.getresponse()
            with pytest.raises(Exception):
                resp.read()
            # urllib3's error handling closes the original response on failure
            resp.close()
        finally:
            clear_group()

        records = read_records(writer)
        response = records[0]
        assert response.rec_headers['WARC-Truncated'] == 'unspecified'
        assert response.content_stream().read() == b'hel'

    def test_head_response_with_zero_length_not_truncated(self):
        group, writer = make_group()
        use_group(group)
        try:
            conn = make_connection(http_response(b'', content_length=0))
            conn.request('HEAD', '/head')
            resp = conn.getresponse()
            assert resp.read() == b''
            resp.close()
        finally:
            clear_group()

        response = read_records(writer)[0]
        assert 'WARC-Truncated' not in response.rec_headers

    def test_connection_close_response_still_recorded_in_full(self):
        # will_close responses make http.client close the *connection* inside
        # getresponse(), before the body is read; that must not finalize (and
        # truncate) the recording prematurely
        group, writer = make_group()
        body = b'close-delimited-body'
        raw = http_response(
            body, extra_headers='Connection: close\r\n')
        use_group(group)
        try:
            conn = make_connection(raw)
            conn.request('GET', '/will-close')
            resp = conn.getresponse()
            # the connection has already been detached/closed
            assert conn._warcio_recorder is None
            assert resp.read() == body
            resp.close()
        finally:
            clear_group()

        response = read_records(writer)[0]
        assert 'WARC-Truncated' not in response.rec_headers
        assert response.payload == body


class TestLifecycle(object):
    def test_double_close_writes_pair_once(self):
        group, writer = make_group()
        use_group(group)
        try:
            conn = make_connection(http_response(b'data'))
            conn.request('GET', '/once')
            resp = conn.getresponse()
            resp.read()
            resp.close()
            resp.close()
        finally:
            clear_group()

        assert len(read_records(writer)) == 2

    def test_background_thread_finishes_after_scope_exits(self, tmp_path):
        # the capturing thread leaves the context while the response is still
        # open; a background thread finishes reading it afterwards and the pair
        # must still land in the archive
        path = str(tmp_path / 'bg.warc')
        release = threading.Event()
        holder = {}

        with capture_http(path, gzip=False) as warc_writer:
            holder['out'] = warc_writer.out
            conn = make_connection(http_response(b'late-body'))
            conn.request('GET', '/late')
            resp = conn.getresponse()

        # scope already exited, but the response was not fully read
        assert not holder['out'].closed

        def finish_read():
            release.wait(5)
            assert resp.read() == b'late-body'
            resp.close()

        thread = threading.Thread(target=finish_read)
        thread.start()
        release.set()
        thread.join(5)
        assert not thread.is_alive()

        # the background completion closed the library-owned file
        assert holder['out'].closed
        with open(path, 'rb') as stream:
            records = [RecordSnapshot(record)
                       for record in ArchiveIterator(stream)]
        assert len(records) == 2
        response = records[0]
        assert response.rec_headers['WARC-Target-URI'] == 'http://example.com/late'
        assert response.payload == b'late-body'

    def test_library_opened_file_closes_only_after_last_interaction(self, tmp_path):
        path = str(tmp_path / 'deferred.warc')
        release = threading.Event()
        holder = {}

        with capture_http(path, gzip=False) as warc_writer:
            holder['out'] = warc_writer.out
            # one fully read interaction, one still open
            conn_done = make_connection(http_response(b'done-body'))
            conn_done.request('GET', '/done')
            conn_done.getresponse().read()

            conn_open = make_connection(http_response(b'bg'))
            conn_open.request('GET', '/bg')
            resp = conn_open.getresponse()

        # the open interaction keeps the file alive past scope exit
        assert not holder['out'].closed

        def finish_read():
            release.wait(5)
            resp.read()
            resp.close()

        thread = threading.Thread(target=finish_read)
        thread.start()
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert holder['out'].closed

        with open(path, 'rb') as stream:
            uris = [record.rec_headers['WARC-Target-URI']
                    for record in ArchiveIterator(stream)
                    if record.rec_type == 'response']
        assert uris == ['http://example.com/done', 'http://example.com/bg']

    def test_caller_supplied_writer_is_not_closed(self):
        writer = BufferWARCWriter(gzip=False)
        ready = threading.Event()
        release = threading.Event()

        def worker():
            with capture_http(writer):
                conn = make_connection(http_response(b'kept'))
                conn.request('GET', '/kept')
                resp = conn.getresponse()
                ready.set()
                release.wait(5)
                resp.read()
                resp.close()

        thread = threading.Thread(target=worker)
        thread.start()
        assert ready.wait(5)
        release.set()
        thread.join(5)
        assert not thread.is_alive()

        # caller still owns the stream and can read everything back
        assert not writer.out.closed
        records = read_records(writer)
        assert records[0].content_stream().read() == b'kept'

    def test_exception_flushes_inflight_and_keeps_other_archive_intact(self):
        group_a, writer_a = make_group()
        group_b, writer_b = make_group()

        # A has a healthy completed interaction
        use_group(group_a)
        conn_a = make_connection(http_response(b'a-ok'))
        conn_a.request('GET', '/a/ok')
        conn_a.getresponse().read()

        # B starts an interaction and blows up before reading the body
        use_group(group_b)
        conn_b = make_connection(http_response(b'b-partial', content_length=100))
        conn_b.request('GET', '/b/broken')
        conn_b.getresponse()

        with pytest.raises(RuntimeError):
            try:
                raise RuntimeError('boom in B')
            except BaseException:
                RecordingHTTPConnection.local.group = None
                group_b.abort_all()
                raise

        # A must still be able to capture subsequent interactions
        use_group(group_a)
        conn_a.sock = FakeSocket(http_response(b'a-next'))
        conn_a.request('GET', '/a/next')
        conn_a.getresponse().read()
        clear_group()

        uris_a = [uri for _, uri, _, _ in pairs(writer_a)]
        assert uris_a == [
            'http://example.com/a/ok', 'http://example.com/a/ok',
            'http://example.com/a/next', 'http://example.com/a/next',
        ]

        b_records = pairs(writer_b)
        assert b_records[0][0] == 'response'
        assert b_records[0][1] == 'http://example.com/b/broken'
        assert b_records[0][2] == 'unspecified'


class TestTunnel(object):
    def _tunnel_connection(self, combined_stream):
        conn = make_connection(host='proxy.example')
        # emulate an HTTPS urllib3 connection behind a CONNECT proxy
        conn.default_port = 443
        conn._tunnel_host = 'target.example'
        conn._tunnel_port = 443
        conn._tunnel_headers = {}
        conn.sock = FakeSocket(None, fp=combined_stream)
        return conn

    def test_connect_exchange_not_archived_tunneled_request_is(self):
        group, writer = make_group()
        combined = BytesIO(
            b'HTTP/1.1 200 Connection established\r\n\r\n'
            + http_response(b'secure')
        )
        use_group(group)
        try:
            conn = self._tunnel_connection(combined)
            # realistic order: putrequest() binds the real request recorder,
            # then the first send would trigger connect()/_tunnel(); emulate
            # that ordering explicitly since the fake socket is pre-attached
            conn.putrequest('GET', '/secret')
            conn._tunnel()
            conn.endheaders()
            resp = conn.getresponse()
            assert resp.read() == b'secure'
            resp.close()
        finally:
            clear_group()

        records = read_records(writer)
        # only one request/response pair, no standalone CONNECT record
        assert len(records) == 2
        response, request = records
        assert response.rec_headers['WARC-Target-URI'] == \
            'https://target.example/secret'
        assert response.rec_headers['WARC-Proxy-Host'] == \
            'https://proxy.example:80'
        assert response.payload == b'secure'
        assert request.rec_headers['WARC-Target-URI'] == \
            'https://target.example/secret'

    def test_tunnel_before_request_uses_pending_metadata(self):
        group, writer = make_group()
        use_group(group)
        try:
            conn = make_connection(host='proxy.example')
            conn.default_port = 443
            conn._tunnel_host = 'target.example'
            conn._tunnel_port = 443
            conn._tunnel_headers = {}
            conn.sock = FakeSocket(
                None,
                fp=BytesIO(b'HTTP/1.1 200 Connection established\r\n\r\n'
                           + http_response(b'later'))
            )
            # unusual but supported: establish the tunnel before any request
            conn._tunnel()
            conn.request('GET', '/later')
            resp = conn.getresponse()
            assert resp.read() == b'later'
            resp.close()
        finally:
            clear_group()

        records = read_records(writer)
        assert len(records) == 2
        assert records[0].rec_headers['WARC-Target-URI'] == \
            'https://target.example/later'
        assert records[0].payload == b'later'


class TestPublicContextManager(object):
    def test_default_buffer_writer_captures_pair(self):
        with capture_http() as warc_writer:
            conn = make_connection(http_response(b'buf'))
            conn.request('GET', '/buffer')
            resp = conn.getresponse()
            resp.read()
            resp.close()

        records = [RecordSnapshot(record)
                   for record in ArchiveIterator(warc_writer.get_stream())]
        assert len(records) == 2
        assert records[0].payload == b'buf'

    def test_filter_can_skip_pair(self):
        def skip_filter(request, response, recorder):
            return None, None

        with capture_http(filter_func=skip_filter) as warc_writer:
            conn = make_connection(http_response(b'skipped'))
            conn.request('GET', '/skip')
            conn.getresponse().read()

        assert warc_writer.get_contents() == b''

    def test_filter_receives_recorder_and_pair(self):
        seen = []

        def observe_filter(request, response, recorder):
            seen.append((request.rec_type, response.rec_type, recorder.url))
            return request, response

        with capture_http(filter_func=observe_filter) as warc_writer:
            conn = make_connection(http_response(b'f'))
            conn.request('GET', '/filtered')
            conn.getresponse().read()

        assert seen == [('request', 'response', 'http://example.com/filtered')]
        assert len(read_records(warc_writer)) == 2

    def test_exception_inside_context_flushes_inflight_as_truncated(self):
        with pytest.raises(RuntimeError):
            with capture_http() as warc_writer:
                conn = make_connection(
                    http_response(b'partial', content_length=50))
                conn.request('GET', '/boom')
                resp = conn.getresponse()
                assert resp.read(3) == b'par'
                raise RuntimeError('stop before reading the rest')

        records = read_records(warc_writer)
        response = records[0]
        assert response.rec_headers['WARC-Truncated'] == 'unspecified'
        # the bytes received before the exception are still retrievable
        assert response.payload == b'par'

    def test_exception_in_one_archive_does_not_touch_another(self):
        writer_a = BufferWARCWriter(gzip=False)

        with capture_http(writer_a):
            conn_a = make_connection(http_response(b'a1'))
            conn_a.request('GET', '/a/1')
            conn_a.getresponse().read()

            with pytest.raises(RuntimeError):
                with capture_http() as writer_b:
                    conn_b = make_connection(http_response(b'b1'))
                    conn_b.request('GET', '/b/1')
                    conn_b.getresponse()
                    raise RuntimeError('B fails')

            # A keeps recording after B's failure
            conn_a.sock = FakeSocket(http_response(b'a2'))
            conn_a.request('GET', '/a/2')
            conn_a.getresponse().read()

        uris_a = [uri for _, uri, _, _ in pairs(writer_a)]
        assert uris_a == [
            'http://example.com/a/1', 'http://example.com/a/1',
            'http://example.com/a/2', 'http://example.com/a/2',
        ]
        b_records = pairs(writer_b)
        assert b_records[0][1] == 'http://example.com/b/1'
        assert b_records[0][2] == 'unspecified'

    def test_partial_buffer_records_exact_returned_bytes(self):
        # mirrors the requirement: 64 byte buffer, only 3 bytes come back
        group, writer = make_group()
        raw = http_response(b'abcdefghij', content_length=100)
        use_group(group)
        try:
            conn = make_connection(fp=ShortReadFP(raw, chunk=3))
            conn.request('GET', '/exact')
            resp = conn.getresponse()
            returned = resp.read(64)
            assert returned == b'abc'
            resp.close()
        finally:
            clear_group()

        response = read_records(writer)[0]
        assert response.rec_headers['WARC-Truncated'] == 'unspecified'
        assert response.payload == b'abc'

    def test_truncation_round_trips_through_gzip_archive(self):
        with capture_http(gzip=True) as warc_writer:
            raw = http_response(b'abcdefghij', content_length=100)
            conn = make_connection(fp=ShortReadFP(raw, chunk=3))
            conn.request('GET', '/gz-trunc')
            resp = conn.getresponse()
            assert resp.read(64) == b'abc'
            resp.close()

        records = [RecordSnapshot(record)
                   for record in ArchiveIterator(warc_writer.get_stream())]
        response = records[0]
        assert response.rec_headers['WARC-Truncated'] == 'unspecified'
        assert response.payload == b'abc'

