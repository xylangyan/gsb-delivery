'''
Streaming, auditable JSON archive correction engine.

The engine applies reviewer correction sheets to a JSON archive without ever
loading the archive into memory.  It is built on top of ijson's pure-Python
parsing entry points (``ijson.backends.python.Lexer`` and ``parse_value``):
the byte stream is decoded and lexed one chunk at a time, every parsed event
is annotated with the exact character *and* byte span of its originating
lexeme, and unselected byte ranges are copied to the output verbatim.
Unselected objects are therefore never materialized as whole Python objects
-- their original bytes are simply passed through.

Correction sheets
=================

Two sheet flavours are supported, matching the two kinds of reviewer forms:

1. Literal locator sheets ("array position + literal key")::

       {
         "sheet_id": "S1",
         "requests": [
           {
             "request_id": "S1-1",
             "array": "records",
             "index": 2,
             "key": "supplier",
             "old_value": "Acme Co",
             "new_value": "Globex GmbH"
           }
         ]
       }

   ``index`` is the 0-based position inside ``array`` (the root record array
   by default).  ``key`` is matched against the *literal* JSON key first, so
   a plain ``"supplier"`` column and a decode-equal ``"\\u0073upplier"``
   column are two distinct actual occurrences: a literal request can only
   ever touch the column whose raw key literal it names, never the twin.
   A sheet may pin the exact raw spelling explicitly with ``key_literal``.

2. Path locator sheets ("old supplier path + actual occurrence number")::

       {
         "sheet_id": "S2",
         "requests": [
           {
             "request_id": "S2-1",
             "path": "records.item.supplier.tax_id",
             "occurrence": 2,
             "old_value": "X-001",
             "new_value": "Y-002"
           }
         ]
       }

   ``occurrence`` is the 1-based actual occurrence number of a value at
   ``path`` (ijson dotted prefix notation, array elements collapse to
   ``item``).

Every request must carry the original value (``old_value``) it expects to
find.  A request whose target cannot be found, whose occurrence does not
exist, or whose ``old_value`` does not match the actual record is rejected
and reported instead of being guessed at.  The source file is never
overwritten: output goes to a new file, committed atomically, and a corrupt
source aborts the run with the actual error position and no output file.
Every accepted correction is re-verified by re-reading the new file.
'''
import json
import os
import tempfile
from decimal import Decimal

from ijson.backends.python import Lexer, parse_value, EOF
from ijson.common import ObjectBuilder, JSONError, IncompleteJSONError
from ijson.utils import coroutine


# Rejection reasons ---------------------------------------------------------

TARGET_NOT_FOUND = 'target_not_found'
OCCURRENCE_OUT_OF_RANGE = 'occurrence_out_of_range'
OLD_VALUE_MISMATCH = 'old_value_mismatch'
CONFLICT = 'conflict'
SCOPE_NOT_VALID = 'scope_not_valid_after_replacement'

_VALUE_EVENTS = frozenset(('null', 'boolean', 'number', 'string',
                           'start_map', 'start_array'))
_CONTAINER_STARTS = frozenset(('start_map', 'start_array'))
_CONTAINER_ENDS = frozenset(('end_map', 'end_array'))
_TRUNCATED_UTF8 = ('unexpected end of data',)


class RedactionError(Exception):
    '''Fatal error that prevents any corrected output from being written.'''


# Position mapping ----------------------------------------------------------

class _ByteLocator:
    '''Computes exact global UTF-8 byte spans in O(1) streaming state.

    The ijson Lexer reports character positions.  Between two consecutive
    lexemes the document only contains JSON whitespace, which is plain ASCII
    and therefore one byte per character; a lexeme itself contributes
    ``len(symbol.encode('utf-8'))`` bytes.  So each symbol's byte span follows
    directly from the previous symbol's end plus the character gap -- no
    per-character table is retained, keeping memory independent of file size.
    '''

    def __init__(self):
        self.bytes_consumed = 0
        self.prev_char_end = 0

    def locate(self, char_start, char_end, symbol):
        gap = char_start - self.prev_char_end
        byte_start = self.bytes_consumed + gap
        byte_end = byte_start + len(symbol.encode('utf-8'))
        self.prev_char_end = char_end
        self.bytes_consumed = byte_end
        return byte_start, byte_end


class _EventEngine:
    '''Single-forward-pass driver for ijson's pure-Python Lexer/parse_value.

    Raw byte chunks are pushed in; incomplete trailing UTF-8 sequences are
    carried to the next chunk.  Every event is emitted as
    ``(event, value, char_start, char_end, byte_start, byte_end)`` with exact
    character and byte spans, plus the originating raw lexeme.  All position
    state is O(1) regardless of document size.
    '''

    def __init__(self):
        self.locator = _ByteLocator()
        self._pending = b''
        self._collected = []
        self._latest = {'span': None, 'byte_span': None, 'raw': None}

        class _Sink:
            @staticmethod
            def send(item):
                event, value = item
                cstart, cend = self._latest['span']
                bstart, bend = self._latest['byte_span']
                self._collected.append(
                    (event, value, cstart, cend, bstart, bend,
                     self._latest['raw']))

        parser = parse_value(_Sink(), False, False)
        self._lexer = Lexer(_span_tracker(parser, self._latest, self.locator))

    def push(self, chunk):
        raw = self._pending + chunk
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as error:
            if error.reason in _TRUNCATED_UTF8:
                text = raw[:error.start].decode('utf-8')
                self._pending = raw[error.start:]
            else:
                raise RedactionError(
                    'invalid UTF-8 at byte %d: %s'
                    % (error.start, error.reason))
        else:
            self._pending = b''
        if text:
            try:
                self._lexer.send(text)
            except (JSONError, IncompleteJSONError) as error:
                raise self._parse_failure(error)
        events = self._collected[:]
        del self._collected[:]
        return events

    def finish(self):
        if self._pending:
            raise RedactionError(
                'truncated UTF-8 multibyte sequence at end of stream')
        try:
            self._lexer.close()
        except (JSONError, IncompleteJSONError) as error:
            raise self._parse_failure(error)
        events = self._collected[:]
        del self._collected[:]
        return events

    def _parse_failure(self, error):
        if self._latest['byte_span'] is not None:
            byte_pos = self._latest['byte_span'][0]
        else:
            byte_pos = self.locator.bytes_consumed
        kind = ('incomplete or corrupt JSON content'
                if isinstance(error, IncompleteJSONError)
                else 'invalid JSON')
        return RedactionError('%s at byte %d: %s'
                              % (kind, byte_pos, error))


@coroutine
def _span_tracker(target, latest, locator):
    try:
        while True:
            pos, symbol = yield
            if (pos, symbol) == EOF:
                target.send(EOF)
                continue
            char_span = (pos, pos + len(symbol))
            byte_span = locator.locate(char_span[0], char_span[1], symbol)
            latest['span'] = char_span
            latest['byte_span'] = byte_span
            latest['raw'] = symbol
            target.send((pos, symbol))
    except StopIteration:
        return


# Path / occurrence tracking ------------------------------------------------

class _PathTracker:
    '''Mirrors ijson's prefix logic while additionally tracking array
    indices and per-path actual occurrence numbers.'''

    def __init__(self):
        self._path = []
        self._frames = []
        self.path_counts = {}

    @property
    def prefix(self):
        return '.'.join(self._path)

    @property
    def depth(self):
        return len(self._frames)

    def feed(self, event, value):
        if event == 'map_key':
            self._path[-1] = value
            return None
        is_value = event in _VALUE_EVENTS
        opened_prefix = None
        # The owning array frame is the top frame *before* the value's own
        # container frame gets pushed.
        if is_value and self._frames \
                and self._frames[-1]['type'] == 'array':
            array_frame = self._frames[-1]
            array_frame['current_index'] = array_frame['next_index']
            array_frame['next_index'] += 1
        if event == 'start_map':
            opened_prefix = self.prefix
            self._path.append(None)
            self._frames.append({'type': 'map', 'prefix': opened_prefix})
        elif event == 'start_array':
            opened_prefix = self.prefix
            self._path.append('item')
            self._frames.append({'type': 'array', 'prefix': opened_prefix,
                                 'next_index': 0, 'current_index': None})
        elif event == 'end_map':
            self._frames.pop()
            self._path.pop()
        elif event == 'end_array':
            self._frames.pop()
            self._path.pop()
        if not is_value:
            return None
        prefix = opened_prefix if opened_prefix is not None else self.prefix
        occurrence = self.path_counts.get(prefix, 0) + 1
        self.path_counts[prefix] = occurrence
        return prefix, occurrence

    def nearest_array(self, array_path):
        for frame in reversed(self._frames):
            if frame['type'] == 'array' and frame['prefix'] == array_path:
                return frame
        return None


# Targets -------------------------------------------------------------------

_MISSING = object()


class _Target:
    def __init__(self, request, sheet_id):
        self.request = request
        self.sheet_id = sheet_id
        self.kind = 'path' if 'path' in request else 'array_literal'
        self.prefix = None
        self.occurrence = None
        self.char_start = None
        self.char_end = None
        self.byte_start = None
        self.byte_end = None
        self.actual_value = _MISSING
        self.raw_literal = None
        self.builder = None
        self.builder_depth = None
        self.status = 'accepted'
        self.rejection_reason = None
        self.rejection_detail = None
        self.composed_into = None
        self.verified = False
        self.sources = [{'sheet_id': sheet_id,
                         'request_id': request.get('request_id')}]

    @property
    def span(self):
        return self.char_start, self.char_end

    def reject(self, reason, detail):
        self.status = 'rejected'
        self.rejection_reason = reason
        self.rejection_detail = detail

    def as_result(self):
        entry = {
            'sheet_id': self.sheet_id,
            'request_id': self.request.get('request_id'),
            'locator': self.kind,
            'path': self.prefix,
            'occurrence': self.occurrence,
            'char_span': [self.char_start, self.char_end],
            'byte_span': [self.byte_start, self.byte_end],
            'status': self.status,
            'old_value_expected': self.request.get('old_value'),
            'new_value': self.request.get('new_value'),
            'sources': list(self.sources),
            'verified': self.verified,
        }
        if self.status == 'accepted':
            entry['old_value_actual'] = self.actual_value
            entry['old_raw_literal'] = self.raw_literal
        else:
            entry['rejection_reason'] = self.rejection_reason
            entry['rejection_detail'] = self.rejection_detail
        if self.composed_into is not None:
            entry['composed_into'] = self.composed_into
        return entry


def _values_equal(actual, expected):
    # bool is a subclass of int: keep true/false distinct from 1/0.
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, dict) or isinstance(expected, dict):
        return (isinstance(actual, dict) and isinstance(expected, dict)
                and actual.keys() == expected.keys()
                and all(_values_equal(actual[key], expected[key])
                        for key in actual))
    if isinstance(actual, (list, tuple)) or isinstance(expected, (list, tuple)):
        return (isinstance(actual, (list, tuple))
                and isinstance(expected, (list, tuple))
                and len(actual) == len(expected)
                and all(_values_equal(a, b)
                        for a, b in zip(actual, expected)))
    if isinstance(actual, (int, float, Decimal)) \
            and isinstance(expected, (int, float, Decimal)):
        # ijson yields Decimal for fractions while sheets carry JSON floats;
        # compare on the decimal spelling so 0.1 matches Decimal('0.1').
        return Decimal(str(actual)) == Decimal(str(expected))
    return actual == expected


# Sheet loading -------------------------------------------------------------

def _load_sheet(raw_sheet, sheet_id):
    if isinstance(raw_sheet, str):
        with open(raw_sheet, 'r', encoding='utf-8') as handle:
            raw_sheet = json.load(handle)
    elif isinstance(raw_sheet, bytes):
        raw_sheet = json.loads(raw_sheet.decode('utf-8'))
    if not isinstance(raw_sheet, dict):
        raise RedactionError('sheet %s must be a JSON object' % sheet_id)
    requests = raw_sheet.get('requests')
    if not isinstance(requests, list) or not requests:
        raise RedactionError(
            'sheet %s must contain a non-empty "requests" list' % sheet_id)
    sheet_id = raw_sheet.get('sheet_id', sheet_id)
    normalized = []
    for number, request in enumerate(requests):
        if not isinstance(request, dict):
            raise RedactionError(
                'sheet %s request #%d must be an object'
                % (sheet_id, number))
        if 'old_value' not in request or 'new_value' not in request:
            raise RedactionError(
                'sheet %s request #%d needs old_value and new_value'
                % (sheet_id, number))
        is_path = 'path' in request
        is_literal = 'index' in request and 'key' in request
        if not (is_path or is_literal):
            raise RedactionError(
                'sheet %s request #%d needs either "path"+"occurrence" or '
                '"index"+"key" locators' % (sheet_id, number))
        request = dict(request)
        request.setdefault('request_id', '%s-%d' % (sheet_id, number + 1))
        request.setdefault('array', 'records')
        if is_path:
            request.setdefault('occurrence', 1)
        else:
            request.setdefault(
                'key_literal',
                json.dumps(request['key'], ensure_ascii=False))
        normalized.append(request)
    return sheet_id, normalized


# Pass 1: resolution --------------------------------------------------------

def _resolve(source_path, loaded_sheets, buf_size):
    literal_requests = [(sid, req) for sid, req in loaded_sheets
                        if 'index' in req]
    path_requests = [(sid, req) for sid, req in loaded_sheets
                     if 'path' in req]

    engine = _EventEngine()
    tracker = _PathTracker()
    targets = []
    awaiting = []
    open_targets = []

    def process(event, value, cstart, cend, bstart, bend, raw):
        nonlocal awaiting
        info = tracker.feed(event, value)
        for target in open_targets:
            target.builder.event(event, value)

        if event == 'map_key':
            for sheet_id, request in literal_requests:
                if value != request['key']:
                    continue
                array_frame = tracker.nearest_array(request['array'])
                if array_frame is None:
                    continue
                if array_frame['current_index'] != request['index']:
                    continue
                if raw != request['key_literal']:
                    continue
                awaiting.append(_Target(request, sheet_id))
            return

        if event not in _VALUE_EVENTS:
            if event in _CONTAINER_ENDS:
                due = [target for target in open_targets
                       if target.builder_depth == tracker.depth + 1]
                for target in due:
                    open_targets.remove(target)
                    target.char_end = cend
                    target.byte_end = bend
                    _finalize_value(target, target.builder.value, targets)
            return

        prefix, occurrence = info
        due = awaiting
        awaiting = []
        for sheet_id, request in path_requests:
            if (prefix == request['path']
                    and occurrence == request['occurrence']):
                due.append(_Target(request, sheet_id))

        for target in due:
            target.prefix = prefix
            target.occurrence = occurrence
            target.char_start = cstart
            target.char_end = cend
            target.byte_start, target.byte_end = bstart, bend
            target.raw_literal = None if event in _CONTAINER_STARTS else raw
            if event in _CONTAINER_STARTS:
                target.builder = ObjectBuilder()
                target.builder.event(event, value)
                target.builder_depth = tracker.depth
                open_targets.append(target)
            else:
                _finalize_value(target, value, targets)

    with open(source_path, 'rb') as handle:
        while True:
            chunk = handle.read(buf_size)
            if not chunk:
                break
            for event_tuple in engine.push(chunk):
                process(*event_tuple)
        for event_tuple in engine.finish():
            process(*event_tuple)

    _check_missing(targets, loaded_sheets, tracker)
    return targets


def _finalize_value(target, actual_value, targets):
    expected = target.request['old_value']
    if not _values_equal(actual_value, expected):
        target.reject(OLD_VALUE_MISMATCH,
                      'expected %r, found %r' % (expected, actual_value))
    else:
        target.actual_value = actual_value
    targets.append(target)


def _check_missing(targets, loaded_sheets, tracker):
    seen = {(t.sheet_id, t.request['request_id']) for t in targets}
    for sheet_id, request in loaded_sheets:
        if (sheet_id, request['request_id']) in seen:
            continue
        target = _Target(request, sheet_id)
        if 'path' in request:
            actual = tracker.path_counts.get(request['path'], 0)
            if actual == 0:
                target.reject(TARGET_NOT_FOUND,
                              'no value at path %r' % request['path'])
            else:
                target.reject(
                    OCCURRENCE_OUT_OF_RANGE,
                    'path %r has %d actual occurrence(s), requested %d'
                    % (request['path'], actual, request['occurrence']))
        else:
            target.reject(
                TARGET_NOT_FOUND,
                'no literal key %s at %s[%d]'
                % (request['key_literal'], request['array'],
                   request['index']))
        targets.append(target)


# Pass 2: compatibility composition ----------------------------------------

def _relative_tokens(owner_prefix, child_prefix):
    if not child_prefix.startswith(owner_prefix + '.'):
        return None
    return child_prefix[len(owner_prefix) + 1:].split('.')


def _navigate(value, tokens):
    current = value
    for token in tokens:
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list):
            # Prefixes collapse indices to "item"; a single-element list is
            # unambiguous, anything else cannot be addressed safely.
            if token != 'item' or len(current) != 1:
                return _MISSING
            current = current[0]
        else:
            return _MISSING
    return current


def _set_path(value, tokens, new_value):
    current = value
    for token in tokens[:-1]:
        if isinstance(current, dict):
            current = current[token]
        elif isinstance(current, list) and token == 'item' \
                and len(current) == 1:
            current = current[0]
        else:
            return False
    last = tokens[-1]
    if isinstance(current, dict) and last in current:
        current[last] = new_value
        return True
    if isinstance(current, list) and last == 'item' and len(current) == 1:
        current[0] = new_value
        return True
    return False


def _compose(targets):
    accepted = [target for target in targets if target.status == 'accepted']

    # Requests hitting the exact same original occurrence first.
    span_groups = {}
    for target in accepted:
        span_groups.setdefault(target.span, []).append(target)

    owners = []
    for group in span_groups.values():
        new_values = {json.dumps(target.request['new_value'],
                                 sort_keys=True, ensure_ascii=False)
                      for target in group}
        if len(new_values) > 1:
            for target in group:
                target.reject(
                    CONFLICT,
                    'requests replace the same occurrence %r with '
                    'different new values' % (group[0].span,))
            continue
        owner = group[0]
        for target in group[1:]:
            owner.sources.extend(target.sources)
            target.composed_into = owner.request['request_id']
        owners.append(owner)

    # Fold nested scopes into their broader replacement while the approved
    # scope stays valid inside the replacement object.
    owners.sort(key=lambda target: target.char_start)
    plans = {}
    for target in owners:
        broader = [owner for owner in plans.values()
                   if owner.char_start < target.char_start
                   and target.char_end < owner.char_end]
        if not broader:
            plans[target.span] = target
            continue
        owner = max(broader, key=lambda candidate: candidate.char_start)
        tokens = _relative_tokens(owner.prefix, target.prefix)
        # Both old values were already proven against the original document.
        # Compatibility therefore hinges on the narrower approved scope still
        # existing inside the broader replacement object.
        if (_navigate(owner.request['new_value'], tokens or [])
                is not _MISSING
                and _set_path(owner.request['new_value'], tokens,
                              target.request['new_value'])):
            owner.sources.extend(target.sources)
            target.composed_into = owner.request['request_id']
            continue
        target.reject(
            SCOPE_NOT_VALID,
            'replacement by %s no longer contains the approved scope %s'
            % (owner.request['request_id'], '.'.join(tokens or [])))
    return plans


# Pass 3: streaming output --------------------------------------------------

def _write(source_path, destination, plans, buf_size):
    plan_by_start = {
        target.char_start: (
            target.char_end,
            json.dumps(target.request['new_value'],
                       ensure_ascii=False, separators=(',', ':')
                       ).encode('utf-8'))
        for target in plans.values()}

    engine = _EventEngine()
    buffer = bytearray()
    buffer_start = 0
    skip = None
    bytes_in = 0
    bytes_out = 0

    class _CountingWriter:
        def __init__(self, handle):
            self._handle = handle

        def write(self, data):
            nonlocal bytes_out
            written = self._handle.write(data)
            bytes_out += written if written is not None else len(data)
            return written

    def flush_through(out, byte_pos):
        nonlocal buffer_start
        index = byte_pos - buffer_start
        out.write(bytes(buffer[:index]))
        del buffer[:index]
        buffer_start = byte_pos

    def discard_through(byte_pos):
        nonlocal buffer_start
        index = byte_pos - buffer_start
        del buffer[:index]
        buffer_start = byte_pos

    def process(out, event, value, cstart, cend, bstart, bend, _raw):
        nonlocal skip
        if skip is not None:
            if event in _CONTAINER_STARTS:
                skip['depth'] += 1
            elif event in _CONTAINER_ENDS:
                skip['depth'] -= 1
                if skip['depth'] == 0:
                    discard_through(bend)
                    out.write(skip['replacement'])
                    skip = None
                    return
            discard_through(bend)
            return
        if cstart in plan_by_start:
            end_char, replacement = plan_by_start[cstart]
            flush_through(out, bstart)
            discard_through(bend)
            if end_char == cend:
                out.write(replacement)
            else:
                # The replacement for a whole container is emitted once, at
                # its closing boundary; the original bytes are discarded.
                skip = {'depth': 1, 'replacement': replacement}
        else:
            # Keep the buffer bounded: ordinary bytes are passed through at
            # every event boundary, never materialized into objects.
            flush_through(out, bend)

    with open(source_path, 'rb') as src, \
            open(destination, 'wb') as raw_out:
        out = _CountingWriter(raw_out)
        while True:
            chunk = src.read(buf_size)
            if not chunk:
                break
            bytes_in += len(chunk)
            buffer.extend(chunk)
            for event_tuple in engine.push(chunk):
                process(out, *event_tuple)
        for event_tuple in engine.finish():
            process(out, *event_tuple)
        flush_through(out, buffer_start + len(buffer))
    return bytes_in, bytes_out


# Pass 4: re-read verification ---------------------------------------------

def _verify_plans(destination, plans, buf_size):
    engine = _EventEngine()
    tracker = _PathTracker()
    wanted = {(target.prefix, target.occurrence): target
              for target in plans.values()}
    found = {}
    active = []

    def process(event, value, cstart, cend, _bstart, _bend, _raw):
        info = tracker.feed(event, value)
        for entry in active:
            entry['builder'].event(event, value)
        if event in _VALUE_EVENTS:
            prefix, occurrence = info
            key = (prefix, occurrence)
            if event in _CONTAINER_STARTS:
                if key in wanted:
                    builder = ObjectBuilder()
                    builder.event(event, value)
                    active.append({'key': key, 'builder': builder,
                                   'depth': tracker.depth})
            elif key in wanted:
                found[key] = value
        elif event in _CONTAINER_ENDS:
            due = [entry for entry in active
                   if entry['depth'] == tracker.depth + 1]
            for entry in due:
                active.remove(entry)
                found[entry['key']] = entry['builder'].value

    with open(destination, 'rb') as handle:
        while True:
            chunk = handle.read(buf_size)
            if not chunk:
                break
            for event_tuple in engine.push(chunk):
                process(*event_tuple)
        for event_tuple in engine.finish():
            process(*event_tuple)

    ok = True
    for key, target in wanted.items():
        actual = found.get(key, _MISSING)
        matched = actual is not _MISSING and _values_equal(
            actual, target.request['new_value'])
        target.verified = matched
        ok = ok and matched
    return ok


# Public API ---------------------------------------------------------------

class Report:
    def __init__(self, source, destination, results, bytes_in, bytes_out,
                 verified):
        self.source = source
        self.destination = destination
        self.results = results
        self.bytes_in = bytes_in
        self.bytes_out = bytes_out
        self.verified = verified

    @property
    def applied(self):
        return [item for item in self.results if item['status'] == 'accepted']

    @property
    def rejected(self):
        return [item for item in self.results if item['status'] == 'rejected']

    def to_dict(self):
        return {
            'source': self.source,
            'destination': self.destination,
            'bytes_in': self.bytes_in,
            'bytes_out': self.bytes_out,
            'applied': len(self.applied),
            'rejected': len(self.rejected),
            'verified': self.verified,
            'results': self.results,
        }


def apply_corrections(source, destination, sheets, buf_size=64 * 1024):
    '''Apply reviewer correction ``sheets`` to ``source``, writing the
    corrected document to ``destination`` and returning a :class:`Report`.

    ``sheets`` is an iterable of sheet paths, raw JSON bytes or already
    parsed sheet dictionaries.  The archive is streamed in bounded-memory
    forward passes (resolve, write, verify); the destination is committed
    atomically and the source file is never rewritten.
    '''
    if os.path.abspath(source) == os.path.abspath(destination):
        raise RedactionError(
            'destination must be a new file; the source is never rewritten')

    loaded_sheets = []
    for index, sheet in enumerate(sheets):
        sheet_id, requests = _load_sheet(sheet, 'S%d' % (index + 1))
        loaded_sheets.extend((sheet_id, request) for request in requests)

    targets = _resolve(source, loaded_sheets, buf_size)
    plans = _compose(targets)

    destination_dir = os.path.dirname(os.path.abspath(destination)) or '.'
    tmp_handle, tmp_path = tempfile.mkstemp(
        prefix='.redact-', suffix='.tmp', dir=destination_dir)
    os.close(tmp_handle)
    try:
        bytes_in, bytes_out = _write(source, tmp_path, plans, buf_size)
        if not _verify_plans(tmp_path, plans, buf_size):
            raise RedactionError(
                'post-write verification failed; the source is untouched')
        os.replace(tmp_path, destination)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    results = [target.as_result() for target in targets]
    by_id = {target.request['request_id']: target for target in targets}
    for target in targets:
        if target.composed_into is not None:
            target.verified = by_id[target.composed_into].verified
    results = [target.as_result() for target in targets]
    results.sort(key=lambda item:
                 (item['char_span'][0]
                  if item['char_span'][0] is not None else 1 << 60,
                  item['sheet_id'], item['request_id']))
    verified = all(target.verified for target in plans.values())
    return Report(source, destination, results, bytes_in, bytes_out,
                  verified)


def _main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description='Apply reviewer correction sheets to a JSON archive.')
    parser.add_argument('source', help='source JSON archive (never modified)')
    parser.add_argument('destination', help='path of the corrected new file')
    parser.add_argument('--sheet', action='append', required=True,
                        help='correction sheet JSON (repeatable)')
    parser.add_argument('--report', help='write the audit report as JSON')
    parser.add_argument('--buf-size', type=int, default=64 * 1024)
    args = parser.parse_args(argv)
    try:
        report = apply_corrections(args.source, args.destination,
                                   args.sheet, buf_size=args.buf_size)
    except RedactionError as error:
        parser.exit(2, 'redaction failed: %s\n' % error)
    if args.report:
        with open(args.report, 'w', encoding='utf-8') as handle:
            json.dump(report.to_dict(), handle, ensure_ascii=False, indent=2)
    summary = report.to_dict()
    print('applied=%d rejected=%d verified=%s bytes_in=%d bytes_out=%d'
          % (summary['applied'], summary['rejected'],
             summary['verified'], summary['bytes_in'],
             summary['bytes_out']))
    for item in report.rejected:
        print('REJECTED %s/%s: %s (%s)'
              % (item['sheet_id'], item['request_id'],
                 item['rejection_reason'], item['rejection_detail']))
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
