import threading

from io import BytesIO

from six.moves import http_client as httplib

from contextlib import contextmanager

from array import array

from warcio.utils import to_native_str, BUFF_SIZE, open
from warcio.warcwriter import WARCWriter, BufferWARCWriter

from tempfile import SpooledTemporaryFile


# ============================================================================
orig_connection = httplib.HTTPConnection


# ============================================================================
class RecordingStream(object):
    """
    Wraps the raw socket file object of an HTTPResponse.

    Every byte that the caller actually pulls off the wire is forwarded to
    the recorder that owns this particular request. Recording therefore
    follows the real interaction on a connection, rather than the thread
    that happened to open the pooled connection or the one that eventually
    reads the body.
    """
    def __init__(self, fp, recorder):
        self.fp = fp
        self.recorder = recorder

        self.recorder.set_remote_ip(self._get_remote_ip())

    def _get_remote_ip(self):
        try:
            fp = self.fp
            # for python 3, need to get 'raw' fp
            if hasattr(fp, 'raw'):  #pragma: no cover
                fp = fp.raw

            socket = fp._sock

            # wrapped ssl socket
            if hasattr(socket, 'socket'):
                socket = socket.socket

            return socket.getpeername()[0]

        except Exception:  #pragma: no cover
            return None

    # Used in PY2 Only
    def read(self, amt=None):  #pragma: no cover
        buff = self.fp.read(amt)
        self.recorder.write_response(buff)
        return buff

    # Used in PY3 Only
    def readinto(self, buff):  #pragma: no cover
        res = self.fp.readinto(buff)
        # readinto may fill only part of the supplied buffer, so record only
        # the bytes that were actually received
        if res:
            self.recorder.write_response(buff[:res])
        return res

    def readline(self, maxlen=-1):
        line = self.fp.readline(maxlen)
        self.recorder.write_response(line)
        return line

    def close(self):
        # idempotent: http.client may close the response more than once,
        # the record must only be written a single time
        self.recorder.done()
        if self.fp:
            return self.fp.close()

    def flush(self):
        return self.fp.flush()


# ============================================================================
class RecordingHTTPResponse(httplib.HTTPResponse):
    def __init__(self, recorder, *args, **kwargs):
        httplib.HTTPResponse.__init__(self, *args, **kwargs)
        self.fp = RecordingStream(self.fp, recorder)
        self._warcio_recorder = recorder

    def begin(self):
        httplib.HTTPResponse.begin(self)
        # response headers have now arrived, remember how the framing says the
        # body should terminate so that truncation can be detected later
        self._warcio_recorder.note_response_framing(self)

    def _read_next_chunk_size(self):
        size = httplib.HTTPResponse._read_next_chunk_size(self)
        if size == 0:
            # the terminating zero chunk means the chunked body completed
            # normally; an early EOF raises before this can run
            self._warcio_recorder.note_chunked_complete()
        return size


# ============================================================================
class RecordingHTTPConnection(httplib.HTTPConnection):
    local = threading.local()

    def __init__(self, *args, **kwargs):
        orig_connection.__init__(self, *args, **kwargs)
        # the recorder for the request currently using this connection,
        # bound when the request is started
        self._warcio_recorder = None
        self._refresh_response_class()

    @classmethod
    def _current_group(cls):
        return getattr(cls.local, 'group', None)

    def _refresh_response_class(self):
        group = self._current_group()

        def make_recording_response(*resp_args, **resp_kwargs):
            # no recorder bound (e.g. the connection is reused outside any
            # capture, or a proxy CONNECT): behave as a plain httplib
            # response so pooled connections keep working unrecorded
            if self._warcio_recorder is None:
                return httplib.HTTPResponse(*resp_args, **resp_kwargs)
            return RecordingHTTPResponse(self._warcio_recorder,
                                         *resp_args, **resp_kwargs)

        # when not recording, leave the standard response class in place so
        # that pooled connections keep behaving exactly as plain httplib
        if group:
            self.response_class = make_recording_response
        else:
            self.response_class = httplib.HTTPResponse

    def _get_recorder(self):
        # a recorder is bound in putrequest() for every recorded request;
        # never create one implicitly here, e.g. for a raw send() such as the
        # CONNECT exchange, which must not be archived on its own
        return self._warcio_recorder

    def send(self, data):
        recorder = self._get_recorder()
        if not recorder:
            return orig_connection.send(self, data)

        def send_request(buff):
            recorder.extract_url(buff, self.host, self.port, self.default_port)

            orig_connection.send(self, buff)
            recorder.write_request(buff)

        # if sending request body as stream
        # (supported via httplib but seems unused via higher-level apis)
        if hasattr(data, 'read') and not isinstance(data, array):  #pragma: no cover
            try:
                while True:
                    buff = data.read(BUFF_SIZE)
                    if not buff:
                        break

                    send_request(buff)
            except Exception:
                # the request never reaches getresponse(), so there will be
                # no response close() to finalize this recorder
                recorder.force_done()
                raise
        else:
            try:
                send_request(data)
            except Exception:
                recorder.force_done()
                raise

    def _tunnel(self, *args, **kwargs):
        # stdlib/urllib3 issue the proxy CONNECT directly via send(); give it
        # a dedicated, non-archived recorder so it can never be confused with
        # the real request that travels through the tunnel
        group = self._current_group()
        if not group:
            return orig_connection._tunnel(self, *args, **kwargs)

        real_recorder = self._warcio_recorder
        tunnel_recorder = group.get_recorder()
        tunnel_recorder.start()
        tunnel_recorder.tunnel = True
        self._warcio_recorder = tunnel_recorder
        self._refresh_response_class()

        try:
            try:
                result = orig_connection._tunnel(self, *args, **kwargs)
            except Exception:
                tunnel_recorder.force_done()
                raise
        finally:
            # restore the request recorder (or leave it unbound if the tunnel
            # was opened before any request); the tunnel recorder already
            # unregistered itself in its non-archived close()
            self._warcio_recorder = real_recorder
            self._refresh_response_class()

        tunnel_meta = (
            tunnel_recorder.connect_host,
            tunnel_recorder.connect_port,
            tunnel_recorder.warc_headers.get('WARC-Proxy-Host'),
        )
        if real_recorder is not None:
            self._apply_tunnel_meta(real_recorder, tunnel_meta)
        else:
            # tunnel opened before any putrequest(): hand the metadata to the
            # next request recorder created on this connection
            self._warcio_pending_tunnel = tunnel_meta

        return result

    @staticmethod
    def _apply_tunnel_meta(recorder, tunnel_meta):
        connect_host, connect_port, proxy_header = tunnel_meta
        if connect_host:
            recorder.connect_host = connect_host
        if connect_port:
            recorder.connect_port = connect_port
        if proxy_header:
            recorder.warc_headers['WARC-Proxy-Host'] = proxy_header

    def putrequest(self, *args, **kwargs):
        # this is the first call of every new request: bind the archive that
        # is active *now*, independent of where the pooled connection came
        # from or which thread will later read the response
        group = self._current_group()
        if group:
            recorder = group.get_recorder()
            recorder.start()
            pending_tunnel = getattr(self, '_warcio_pending_tunnel', None)
            if pending_tunnel:
                self._apply_tunnel_meta(recorder, pending_tunnel)
                self._warcio_pending_tunnel = None
            self._warcio_recorder = recorder
        else:
            self._warcio_recorder = None
        self._refresh_response_class()

        return orig_connection.putrequest(self, *args, **kwargs)

    def getresponse(self, *args, **kwargs):
        recorder = self._warcio_recorder
        try:
            return orig_connection.getresponse(self, *args, **kwargs)
        except Exception:
            # no response object (and therefore no close()) will be produced;
            # flush whatever request bytes were already captured so that a
            # failure here does not swallow this or later interactions
            if recorder:
                recorder.force_done()
            raise

    def close(self):
        # Do not force the recorder to finish here: http.client calls
        # connection.close() for will_close responses *before* the body is
        # read, and urllib3 also discards pooled connections this way. The
        # response holds its own recorder reference and finalizes the archive
        # pair in its own close(); any genuinely orphaned recorders are
        # flushed by the capture scope's exception teardown instead.
        self._warcio_recorder = None
        orig_connection.close(self)


# ============================================================================
class RequestRecorder(object):
    """
    Records a single request/response interaction.

    A recorder is created when a request is started and is owned by that
    interaction until its response stream is closed. It is independent of
    thread-local state, so the response can finish being read -- and the
    pair written to the archive -- on a background thread after the
    capturing scope has been exited.
    """
    def __init__(self, group, record_ip=True):
        self.group = group
        self.filter_func = group.filter_func
        self.writer = group.writer
        self.request_out = None
        self.response_out = None
        self.url = None
        self.connect_host = self.connect_port = None
        self.started_req = False
        self.first_line_read = False
        # True while the in-flight request is a proxy CONNECT: the CONNECT
        # exchange itself is not archived, only the tunneled requests are
        self.tunnel = False
        self.warc_headers = {}
        self.record_ip = record_ip
        # guarantees a single write even if close()/force_done() arrive
        # concurrently from different threads
        self._finalize_lock = threading.Lock()

        # response framing, captured once the headers arrive
        self.response = None
        self.body_start = 0
        self.chunked = False
        self.chunked_complete = False
        self.expected_length = None

        # how the interaction ended
        self.complete = False
        self.truncated = False

    def start(self):
        # may be called on a recorder that already held a CONNECT exchange;
        # discard those bytes and start fresh for the real request
        self._reset_buffers()
        self.url = None
        self.started_req = True
        self.first_line_read = False
        self.tunnel = False
        self.response = None
        self.body_start = 0
        self.chunked = False
        self.chunked_complete = False
        self.expected_length = None
        self.complete = False
        self.truncated = False
        self.group.register(self)

    def note_chunked_complete(self):
        self.chunked_complete = True

    def _create_buffer(self):
        return SpooledTemporaryFile(BUFF_SIZE)

    def _reset_buffers(self):
        if self.request_out is not None:
            self.request_out.close()
            self.response_out.close()
        self.request_out = self._create_buffer()
        self.response_out = self._create_buffer()

    def set_remote_ip(self, remote_ip):
        if self.record_ip and remote_ip:  #pragma: no cover
            self.warc_headers['WARC-IP-Address'] = remote_ip

    def note_response_framing(self, response):
        self.response = response
        self.chunked = bool(response.chunked)
        self.expected_length = response.length
        # everything recorded so far is status line + headers; the body only
        # begins after this offset
        self.body_start = self.response_out.tell()

    def write_request(self, buff):
        if self.started_req:
            self.request_out.write(buff)

    def write_response(self, buff):
        if self.started_req and buff:
            self.response_out.write(buff)

    def _create_record(self, out, record_type):
        length = out.tell()
        out.seek(0)

        warc_headers = self.warc_headers
        if record_type == 'response' and self.truncated:
            # WARC-Truncated marks a response whose payload was not fully
            # received; the original HTTP headers and the bytes that did
            # arrive are left untouched
            warc_headers = dict(warc_headers)
            warc_headers['WARC-Truncated'] = 'unspecified'

        return self.writer.create_warc_record(
                warc_headers_dict=warc_headers,
                uri=self.url,
                record_type=record_type,
                payload=out,
                length=length)

    def _body_bytes(self):
        return self.response_out.tell() - self.body_start

    def _mark_complete(self):
        """
        Decide whether the response body was read in full, based on the
        framing advertised in the response headers.
        """
        if self.expected_length is not None:
            # Content-Length, or HEAD / 1xx / 204 / 304 with length 0:
            # compare against the body bytes actually pulled off the wire,
            # excluding the recorded status line and headers
            self.complete = (self._body_bytes() >= self.expected_length)
        elif self.chunked:
            # chunked encoding: completion is signalled explicitly when the
            # terminating zero chunk is read; an early EOF raises instead
            self.complete = self.chunked_complete
        else:
            # no framing information: the body runs until the connection
            # closes, so an observed close is the natural end of the body
            self.complete = True

        if not self.complete:
            self.truncated = True

    def done(self):
        with self._finalize_lock:
            if not self.started_req:
                return

            # the proxy CONNECT exchange is not archived; close its buffers
            # and drop it from the pending set so it cannot block teardown
            if self.tunnel:
                self.started_req = False
                self.request_out.close()
                self.response_out.close()
                self.group.unregister(self)
                return

            self._mark_complete()

            try:
                request = self._create_record(self.request_out, 'request')
                response = self._create_record(self.response_out, 'response')

                if self.filter_func:
                    request, response = self.filter_func(request, response, self)
                    if not request or not response:
                        return

                with self.group.write_lock:
                    self.writer.write_request_response_pair(request, response)
            finally:
                self.started_req = False
                self.request_out.close()
                self.response_out.close()
                # release the response (and its socket file object) now that
                # framing and bytes have been consumed
                self.response = None
                self.group.unregister(self)

    def force_done(self):
        """
        Finalize the current interaction if it is still in flight.

        Used when no normal response close will happen (request/response
        errors, connection teardown, scope exit while a body is still open).
        Recorded bytes, if any, are still written and flagged as truncated
        when the body was not completed.
        """
        if not self.started_req:
            return
        self.done()

    def extract_url(self, data, host, port, default_port):
        if self.first_line_read:
            return

        self.first_line_read = True
        buff = BytesIO(data)
        line = to_native_str(buff.readline(), 'latin-1')

        parts = line.split(' ', 2)
        verb = parts[0]
        path = parts[1]

        if verb == "CONNECT":
            parts = path.split(":", 1)
            self.connect_host = parts[0]
            self.connect_port = int(parts[1]) if len(parts) > 1 else default_port
            self.warc_headers['WARC-Proxy-Host'] = "https://{0}:{1}".format(host, port)
            return

        if self.connect_host:
            host = self.connect_host

        if self.connect_port:
            port = self.connect_port

        if path.startswith(('http:', 'https:')):
            self.warc_headers['WARC-Proxy-Host'] = "http://{0}:{1}".format(host, port)
            self.url = path
            return

        scheme = 'https' if default_port == 443 else 'http'
        self.url = scheme + '://' + host
        if port != default_port:
            self.url += ':' + str(port)

        self.url += path


# ============================================================================
class CaptureGroup(object):
    """
    Coordinates every interaction captured within one capture_http() scope.

    Responsibilities:
      - hand out a fresh per-request recorder
      - keep track of recorders whose interaction is still in flight so that a
        response finished on a background thread after the scope was exited
        still gets written to this archive
      - only close a file that the library itself opened once every accepted
        interaction has somewhere to go
      - flush recorders whose response never closes (exceptions, teardown)
    """
    def __init__(self, writer, filter_func, record_ip, out, owns_out):
        self.writer = writer
        self.filter_func = filter_func
        self.record_ip = record_ip
        self.out = out
        self.owns_out = owns_out
        self._pending = set()
        self._lock = threading.Lock()
        # serializes writes to the archive; shared across every capture that
        # uses the same writer (a writer may be passed to several captures,
        # including from background reader threads)
        self.write_lock = getattr(writer, '_warcio_write_lock', None)
        if self.write_lock is None:
            self.write_lock = threading.Lock()
            writer._warcio_write_lock = self.write_lock
        self._scope_closed = False

    def get_recorder(self):
        recorder = RequestRecorder(self, record_ip=self.record_ip)
        self.register(recorder)
        return recorder

    def register(self, recorder):
        with self._lock:
            self._pending.add(recorder)

    def unregister(self, recorder):
        with self._lock:
            self._pending.discard(recorder)
        self._maybe_close_out()

    def _maybe_close_out(self):
        # a file the library opened is closed only once every accepted
        # interaction has been written; this may run on a background reader
        # thread after the capturing scope has been exited
        if not self.owns_out:
            return

        with self._lock:
            if not self._scope_closed or self._pending or not self.out:
                return
            out = self.out
            self.out = None

        out.close()

    def finish_all(self):
        # normal scope exit: return without blocking on responses that were
        # handed to background threads. Their recorders keep this group (and
        # an internally opened file) alive and write the pair when the
        # response finishes; the file is closed in unregister() at that point.
        self._scope_closed = True
        self._maybe_close_out()

    def abort_all(self):
        # exception path: interactions whose response never got a normal
        # close are flushed now, preserving whatever bytes were received and
        # marking unfinished bodies as truncated
        self._scope_closed = True
        while True:
            with self._lock:
                pending = list(self._pending)
            if not pending:
                break
            for recorder in pending:
                recorder.force_done()
        self._maybe_close_out()


# ============================================================================
httplib.HTTPConnection = RecordingHTTPConnection
# ============================================================================

@contextmanager
def capture_http(warc_writer=None, filter_func=None, append=True,
                record_ip=True, **kwargs):
    out = None
    if warc_writer == None:
        if 'gzip' not in kwargs:
            kwargs['gzip'] = False

        warc_writer = BufferWARCWriter(**kwargs)

    if isinstance(warc_writer, str):
        out = open(warc_writer, 'ab' if append else 'xb')
        warc_writer = WARCWriter(out, **kwargs)

    group = CaptureGroup(warc_writer, filter_func, record_ip,
                         out, owns_out=bool(out))

    # restore any outer (nested) capture's group on exit
    previous_group = getattr(RecordingHTTPConnection.local, 'group', None)
    RecordingHTTPConnection.local.group = group
    try:
        yield warc_writer
    except BaseException:
        # the capturing scope failed with an interaction possibly still open:
        # flush whatever was received (marked truncated) instead of hanging,
        # without touching recorders owned by other archives/threads
        RecordingHTTPConnection.local.group = previous_group
        group.abort_all()
        if group.owns_out and group.out:
            group.out.close()
        raise
    else:
        RecordingHTTPConnection.local.group = previous_group
        group.finish_all()
