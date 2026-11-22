# -*- coding: utf-8 -*-
from __future__ import division
from __future__ import print_function
from __future__ import absolute_import
from __future__ import unicode_literals

import pickle
import tempfile
import threading
from collections import OrderedDict, deque
from datetime import datetime, tzinfo

from dateutil.parser import parse as parse_datetime
from dateutil.tz import UTC, gettz

from .table import (
    Table,
    _check_relations,
    _check_unique,
    _create_unique_fields_cache,
)
from . import exceptions
from . import helpers


# Module API

class WindowParseError(exceptions.CastError):
    """A record whose window timestamp can't be interpreted.

    Such records never silently fall out of the windows: they are collected
    per window with the raw row and its source line number and exposed through
    `TimeWindow.parse_errors`, while the source stream keeps being consumed.

    # Arguments
        message (str): error message
        row_number (int): source row number (including the header line)
        row_data (OrderedDict): raw, uncast source row keyed by headers
        reason (Exception): the original parsing failure
    """

    def __init__(self, message, row_number, row_data, reason):
        super(WindowParseError, self).__init__(message)
        self.__row_number = row_number
        self.__row_data = row_data
        self.__reason = reason

    @property
    def row_number(self):
        return self.__row_number

    @property
    def row_data(self):
        return self.__row_data

    @property
    def reason(self):
        return self.__reason


class TimeWindow(object):
    """One left-closed/right-open time window over a shared record stream.

    A window is an independent reader of a `WindowedTable`: it owns its read
    position, unique-key/foreign-key caches and parse error list, so the same
    device number may be valid in several windows without leaking state
    between them.

    Windows are single-pass iterators over the non-rewindable source: every
    record is delivered to a window at most once. Consume `window.iter(...)`
    from the ingestion job and call `window.close()` when the job finishes
    early; closing one window never stops the others. The window can also be
    used as a context manager.
    """

    # Public

    def __init__(self, table, start, end, name, spool):
        self.__table = table
        self.__start = start
        self.__end = end
        self.__name = name
        self.__spool = spool
        self.__condition = threading.Condition()
        self.__finished = False
        self.__closed = False
        self.__parse_errors = []
        self.__naive_rows = []

    @property
    def name(self):
        return self.__name

    @property
    def start(self):
        return self.__start

    @property
    def end(self):
        return self.__end

    @property
    def parse_errors(self):
        """WindowParseError list for records this window can't classify."""
        with self.__condition:
            return list(self.__parse_errors)

    @property
    def naive_rows(self):
        """Timezone-less records as `(row_number, raw_keyed_row)` tuples.

        Populated only when the table has no default timezone to interpret
        naive timestamps; the records are still routed by naive comparison so
        they are never silently treated as out of window, but stay separately
        traceable by their raw text and source line number.
        """
        with self.__condition:
            return list(self.__naive_rows)

    def contains(self, timestamp):
        """Left-closed/right-open membership test for a parsed timestamp."""
        start = self.__start
        end = self.__end
        # Mixed naive/aware operands cannot be compared directly. Attach the
        # table default timezone (UTC as a last resort) to the naive side so
        # routing never crashes; naive records stay tracked via naive_rows.
        aware_mismatch = (start is not None and
                          (start.tzinfo is None) != (timestamp.tzinfo is None))
        if aware_mismatch:
            start, timestamp = self.__table.normalize_for_comparison(
                start, timestamp)
        if end is not None and \
                (end.tzinfo is None) != (timestamp.tzinfo is None):
            end, timestamp = self.__table.normalize_for_comparison(
                end, timestamp)
        if start is not None and timestamp < start:
            return False
        if end is not None and not timestamp < end:
            return False
        return True

    def iter(self, keyed=False, extended=False, cast=True,
             relations=False, foreign_keys_values=False, exc_handler=None):
        """Yield the window rows, pulling lazily from the shared stream.

        Mirrors `Table.iter` except integrity checking, which is not available
        as a window only sees part of the source. Unique and foreign key state
        is private to this window and to this iteration.
        """
        if self.__closed:
            raise exceptions.TableSchemaException(
                'Window "%s" has been closed' % self.__name)
        self.__table._ensure_producer_started()
        handler = helpers.default_exc_handler if exc_handler is None \
            else exc_handler
        unique_cache = _create_unique_fields_cache(self.__table.schema) \
            if cast and self.__table.schema else {}
        if relations and not foreign_keys_values:
            foreign_keys_values = \
                self.__table.index_foreign_keys_values(relations)

        raw_iterator = self.__read_rows()
        cast_iterator = self.__table._apply_processors(
            raw_iterator, cast=cast, exc_handler=handler)

        for row_number, headers, row in cast_iterator:
            schema = self.__table.schema
            if cast and schema and self.__table.headers and \
                    self.__table.headers != schema.field_names:
                message = (
                    'Table headers (%r) don\'t match schema field names (%r) '
                    'in row %s' % (
                        self.__table.headers, schema.field_names,
                        row_number))
                keyed_row = OrderedDict(zip(headers, row))
                handler(
                    exceptions.CastError(message),
                    row_number=row_number, row_data=keyed_row,
                    error_data=keyed_row)
                continue

            if cast:
                _check_unique(
                    schema, headers, row, row_number, unique_cache, handler)

            if relations:
                row = _check_relations(
                    schema, headers, row, row_number,
                    foreign_keys_values, handler)

            if extended:
                yield (row_number, headers, row)
            elif keyed:
                yield dict(zip(headers, row))
            else:
                yield row

        if not self.closed:
            producer_error = self.__table.producer_error
            if producer_error is not None:
                raise producer_error

    def read(self, keyed=False, extended=False, cast=True,
             relations=False, foreign_keys_values=False, exc_handler=None):
        """Read all window rows as a list; same options as `iter`."""
        return list(self.iter(
            keyed=keyed, extended=extended, cast=cast,
            relations=relations, foreign_keys_values=foreign_keys_values,
            exc_handler=exc_handler))

    def close(self):
        """Stop consuming this window without touching the other windows.

        Buffered rows (memory and spool file) are discarded and the shared
        producer stops routing records here, so remaining windows can finish.
        """
        with self.__condition:
            if self.__closed:
                return
            self.__closed = True
            self.__spool.clear()
            self.__condition.notify_all()

    @property
    def closed(self):
        with self.__condition:
            return self.__closed

    @property
    def buffered_rows(self):
        """Rows currently waiting for this window (memory plus spool)."""
        with self.__condition:
            return len(self.__spool)

    @property
    def spooled_rows(self):
        """Buffered rows currently held on the spillover file."""
        with self.__condition:
            return self.__spool.on_disk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    # Internal -- producer side (driven by WindowedTable)

    def _accept_row(self, extended_row):
        with self.__condition:
            if self.__closed or self.__finished:
                return False
            self.__spool.put(extended_row)
            self.__condition.notify_all()
            return True

    def _accept_parse_error(self, error):
        with self.__condition:
            if self.__closed or self.__finished:
                return False
            self.__parse_errors.append(error)
            self.__condition.notify_all()
            return True

    def _accept_naive_row(self, row_number, keyed_row):
        with self.__condition:
            if self.__closed or self.__finished:
                return False
            self.__naive_rows.append((row_number, keyed_row))
            self.__condition.notify_all()
            return True

    def _mark_finished(self):
        with self.__condition:
            self.__finished = True
            self.__condition.notify_all()

    # Internal -- consumer side

    def __read_rows(self):
        while True:
            with self.__condition:
                while not self.__spool and not self.__finished \
                        and not self.__closed:
                    self.__condition.wait()
                if self.__closed:
                    return
                if self.__spool:
                    extended_row = self.__spool.get()
                    self.__condition.notify_all()
                else:
                    return
            yield extended_row


class WindowedTable(Table):
    """A table whose single non-rewindable stream is fanned out into windows.

    The source record stream is opened and read exactly once by a single
    background producer. Every record is routed by its timestamp into the
    windows whose left-closed/right-open bounds contain it; windows then pull
    their own rows independently through per-window buffers that stay bounded
    in memory (overflow is spooled to a temporary file), so interleaved
    ingestion jobs never rescan the source and a slow window never piles the
    whole stream up in memory nor blocks a faster window.

    A table without windows keeps using `Table.iter`/`Table.read` unchanged.

    # Arguments
        source (str/list[]): data source accepted by `Table`
        time_field (str): name of the field carrying the record timestamp
        windows (list[dict]): window descriptors as `{'name', 'start', 'end'}`;
            bounds are datetimes, ISO strings or `None` (unbounded)
        schema (any): data schema in all forms supported by `Schema`
        strict (bool): strictness option to pass to `Schema` constructor
        timezone (str/datetime.tzinfo/None): timezone assumed for timestamp
            strings without timezone information; aware timestamps are kept
        buffer_size (int): max rows held in memory per window; extra rows are
            spooled to a temporary file instead of growing memory
        post_cast (function[]): list of post cast processors
        storage (None): storage name like `sql` or `bigquery`
        options (dict): `tabulator` or storage's options
    """

    # Public

    def __init__(self, source, time_field, windows, schema=None,
                 strict=False, timezone=None, buffer_size=1000,
                 post_cast=[], storage=None, **options):
        super(WindowedTable, self).__init__(
            source, schema=schema, strict=strict,
            post_cast=post_cast, storage=storage, **options)
        self.__time_field = time_field
        self.__timezone = self.__parse_timezone(timezone)
        self.__buffer_size = buffer_size
        self.__windows = OrderedDict()
        for index, descriptor in enumerate(windows):
            name = descriptor.get('name') or 'window%s' % (index + 1)
            window = TimeWindow(
                self,
                start=self.__parse_bound(descriptor.get('start')),
                end=self.__parse_bound(descriptor.get('end')),
                name=name,
                spool=_SpooledQueue(buffer_size))
            self.__windows[window.name] = window
        self.__producer_lock = threading.Lock()
        self.__producer_started = False
        self.__producer_error = None
        self.__producer_done = threading.Event()

    @property
    def time_field(self):
        return self.__time_field

    @property
    def buffer_size(self):
        return self.__buffer_size

    @property
    def windows(self):
        """OrderedDict of window name to TimeWindow."""
        return OrderedDict(self.__windows)

    @property
    def producer_error(self):
        """Exception raised by the shared producer, if any."""
        return self.__producer_error

    def window(self, name):
        """Get a window by name."""
        return self.__windows[name]

    def normalize_for_comparison(self, bound, timestamp):
        """Make a bound and a timestamp comparable when tz-awareness differs.

        The naive operand gets the configured default timezone, or UTC as a
        last resort, attached. Aware datetimes are returned untouched.
        """
        default_tz = self.__timezone or UTC
        if bound.tzinfo is None:
            bound = bound.replace(tzinfo=default_tz)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=default_tz)
        return bound, timestamp

    def iter(self, **options):
        # Without windows the table behaves exactly like a plain Table.
        if not self.__windows:
            return super(WindowedTable, self).iter(**options)
        raise exceptions.TableSchemaException(
            'WindowedTable reads through its windows; use table.window(name)')

    def read(self, **options):
        if not self.__windows:
            return super(WindowedTable, self).read(**options)
        raise exceptions.TableSchemaException(
            'WindowedTable reads through its windows; use table.window(name)')

    def run(self):
        """Start the single shared stream producer explicitly.

        The producer also starts lazily on the first window pull, so calling
        this is optional. Returns the windows for convenient consumption.
        """
        self._ensure_producer_started()
        return self.windows

    def close(self):
        """Close every window and release per-window spool files."""
        for window in self.__windows.values():
            window.close()

    def join(self, timeout=None):
        """Wait for the shared producer thread to finish.

        Returns True if the producer finished within `timeout`.
        """
        if not self.__producer_started:
            return True
        return self.__producer_done.wait(timeout)

    # Internal

    def _ensure_producer_started(self):
        with self.__producer_lock:
            if self.__producer_started:
                return
            self.__producer_started = True
            thread = threading.Thread(
                target=self.__produce, name='tableschema-window-producer')
            thread.daemon = True
            thread.start()

    def __produce(self):
        try:
            self.__consume_stream()
        except Exception as exception:
            # Surface to every window once its buffered rows are drained.
            self.__producer_error = exception
        finally:
            for window in self.__windows.values():
                window._mark_finished()
            self.__producer_done.set()

    def __consume_stream(self):
        windows = list(self.__windows.values())
        with self._stream as stream:
            headers = None
            for row_number, row_headers, raw_row in stream.iter(extended=True):
                # Once every ingestion job has closed its window there is no
                # point in pulling more of the non-rewindable source.
                if windows and all(window.closed for window in windows):
                    break
                if headers is None:
                    headers = row_headers
                    self._set_headers(headers)
                self.__route_row(row_number, headers, raw_row, windows)

    def __route_row(self, row_number, headers, raw_row, windows):
        keyed_row = OrderedDict(zip(headers, raw_row))
        timestamp, reason, is_naive = self.__parse_timestamp(keyed_row)
        extended_row = (row_number, headers, list(raw_row))
        if reason is not None:
            error = WindowParseError(
                'Window timestamp field "%s" can\'t parse value "%s" '
                'in row "%s"' % (
                    self.__time_field,
                    keyed_row.get(self.__time_field), row_number),
                row_number=row_number, row_data=keyed_row, reason=reason)
            # Membership is unknowable, so every open window tracks the raw
            # record with its line number instead of silently dropping it.
            for window in windows:
                window._accept_parse_error(error)
            return
        for window in windows:
            if window.contains(timestamp):
                window._accept_row(extended_row)
                # No default timezone is configured: keep the timezone-less
                # raw record separately traceable for the windows it hits.
                if is_naive:
                    window._accept_naive_row(row_number, keyed_row)

    def __parse_timestamp(self, keyed_row):
        value = keyed_row.get(self.__time_field)
        if value is None or value == '':
            return None, ValueError(
                'empty timestamp field "%s"' % self.__time_field), False
        if isinstance(value, datetime):
            timestamp = value
        else:
            try:
                timestamp = parse_datetime(value)
            except Exception as exception:
                return None, exception, False
        is_naive = timestamp.tzinfo is None
        if is_naive and self.__timezone is not None:
            timestamp = timestamp.replace(tzinfo=self.__timezone)
            is_naive = False
        return timestamp, None, is_naive

    def __parse_bound(self, bound):
        if bound is None:
            return None
        if isinstance(bound, datetime):
            timestamp = bound
        else:
            timestamp = parse_datetime(bound)
        if timestamp.tzinfo is None and self.__timezone is not None:
            timestamp = timestamp.replace(tzinfo=self.__timezone)
        return timestamp

    @staticmethod
    def __parse_timezone(timezone):
        if timezone is None or isinstance(timezone, tzinfo):
            return timezone
        parsed = gettz(timezone)
        if parsed is None:
            raise exceptions.TableSchemaException(
                'Unknown timezone "%s"' % timezone)
        return parsed


class _SpooledQueue(object):
    """FIFO queue with a hard in-memory row cap and disk spillover.

    Up to `max_memory` rows stay in a deque; further rows are pickled into a
    temporary file so a slow consumer bounds its memory footprint instead of
    accumulating the whole stream.
    """

    def __init__(self, max_memory):
        self.__max_memory = max_memory
        self.__memory = deque()
        self.__file = None
        self.__on_disk = 0
        self.__read_position = 0

    def __bool__(self):
        return bool(self.__memory) or self.__on_disk > 0

    __nonzero__ = __bool__

    def __len__(self):
        return len(self.__memory) + self.__on_disk

    @property
    def on_disk(self):
        return self.__on_disk

    @property
    def spool_file_open(self):
        return self.__file is not None

    def put(self, item):
        if self.__file is not None:
            self.__file.seek(0, 2)
            pickle.dump(item, self.__file)
            self.__file.flush()
            self.__on_disk += 1
        elif len(self.__memory) >= self.__max_memory:
            self.__file = tempfile.TemporaryFile()
            pickle.dump(item, self.__file)
            self.__file.flush()
            self.__read_position = 0
            self.__on_disk = 1
        else:
            self.__memory.append(item)

    def get(self):
        if self.__memory:
            return self.__memory.popleft()
        if not self.__on_disk:
            raise IndexError('pop from an empty spooled queue')
        self.__file.seek(self.__read_position)
        item = pickle.load(self.__file)
        self.__read_position = self.__file.tell()
        self.__on_disk -= 1
        if self.__on_disk == 0:
            self.__file.close()
            self.__file = None
            self.__read_position = 0
        return item

    def clear(self):
        self.__memory.clear()
        if self.__file is not None:
            self.__file.close()
            self.__file = None
        self.__on_disk = 0
        self.__read_position = 0
