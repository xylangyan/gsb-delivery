"""
pyexcel.grouping
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Single pass grouping of a sheet source into a durable catalog.

The source is consumed once.  Each row is assigned a stable source row
number and appended to the group named by a key column.  The resulting
catalog is stored on disk so that downstream processes can open individual
groups, deliver rows one by one, acknowledge them, and resume later without
the original input.

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details
"""

import json
import os
import sqlite3
import uuid
from collections import OrderedDict
from hashlib import sha1
from itertools import islice

from pyexcel.exceptions import (
    CatalogIncomplete,
    GroupNotFound,
    UnknownGroup,
)
from pyexcel.internal.core import get_sheet_stream

DEFAULT_CATALOG_NAME = "catalog.sqlite3"
MANIFEST_NAME = "manifest.json"
FORMAT_VERSION = 1

COMPLETE = "complete"
INCOMPLETE = "incomplete"

ACKNOWLEDGED = "acknowledged"
PENDING = "pending"
UNREAD = "unread"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    catalog_uuid TEXT NOT NULL,
    status TEXT NOT NULL,
    format_version INTEGER NOT NULL,
    source TEXT,
    total_rows INTEGER NOT NULL DEFAULT 0,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS groups (
    position INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS rows (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_position INTEGER NOT NULL,
    source_row INTEGER NOT NULL,
    payload TEXT NOT NULL,
    fingerprint TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rows_in_group
    ON rows (group_position, row_id);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_position INTEGER NOT NULL,
    source_row INTEGER NOT NULL,
    payload TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (group_position, source_row)
);
CREATE TABLE IF NOT EXISTS acknowledgements (
    ack_id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_position INTEGER NOT NULL,
    source_row INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    UNIQUE (group_position, source_row)
);
CREATE TABLE IF NOT EXISTS readers (
    session_id TEXT NOT NULL,
    group_position INTEGER NOT NULL,
    reader_id TEXT NOT NULL,
    next_row_id INTEGER NOT NULL DEFAULT 0,
    start_ack_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, group_position, reader_id)
);
CREATE TABLE IF NOT EXISTS seen (
    session_id TEXT NOT NULL,
    group_position INTEGER NOT NULL,
    reader_id TEXT NOT NULL,
    source_row INTEGER NOT NULL,
    PRIMARY KEY (session_id, group_position, reader_id, source_row)
);
"""


def build_catalog(
    catalog_path,
    group_by,
    source=None,
    source_row_start=1,
    header_row=None,
    complete=True,
    **keywords
):
    """Consume a sheet source once and write a durable group catalog.

    :param str catalog_path: directory that will hold the catalog
    :param group_by: column index, column name, or callable returning a key
    :param source: any keyword accepted by :func:`pyexcel.iget_array`
    :param int source_row_start: line number of the first data row
    :param int header_row: row index holding column names when ``group_by``
                           is a name; that row is not added to any group
    :param bool complete: mark the catalog deliverable once the source ends
    :param keywords: extra keyword arguments for the sheet source
    :returns: a :class:`Catalog`
    """
    if source:
        keywords.update(source)
    if "on_demand" in keywords:
        keywords.pop("on_demand")

    catalog = Catalog(catalog_path)
    catalog.begin(source=_describe_source(keywords))
    try:
        sheet_stream = get_sheet_stream(on_demand=True, **keywords)
        _consume(
            sheet_stream,
            catalog,
            group_by,
            source_row_start,
            header_row=header_row,
        )
    except BaseException as exc:
        catalog.abandon(str(exc) or type(exc).__name__)
        raise
    catalog.finish(complete=complete)
    return catalog


def open_catalog(catalog_path):
    """Open a previously written catalog without the original input."""
    return Catalog(catalog_path)


def _describe_source(keywords):
    for key in ("file_name", "url", "file_type", "array", "adict"):
        if key in keywords:
            value = keywords[key]
            if key == "array":
                return "array"
            return f"{key}:{value}"
    return "memory"


def _consume(
    sheet_stream, catalog, group_by, source_row_start, header_row=None
):
    payload = sheet_stream.payload
    if isinstance(payload, (list, tuple)):
        payload = iter(payload)

    column_index = group_by
    names = list(getattr(sheet_stream, "colnames", None) or [])
    if isinstance(group_by, str) and header_row is None:
        if not names:
            raise ValueError(
                "group_by must be an integer, a callable, or a column name "
                "with header_row"
            )
        column_index = names.index(group_by)

    for offset, row in enumerate(payload):
        source_row = source_row_start + offset
        if header_row is not None and offset == header_row:
            names = [str(name) for name in row]
            if isinstance(group_by, str):
                column_index = names.index(group_by)
            continue
        if callable(group_by):
            key = group_by(row)
        else:
            try:
                key = row[column_index]
            except (IndexError, TypeError):
                raise ValueError(
                    f"source row {source_row} has no group column"
                )
        catalog.append(key, row, source_row)
    close = getattr(payload, "close", None)
    if close is not None:
        close()


class Catalog:
    """A durable, append-only grouping of a single sheet source."""

    def __init__(self, path):
        self.path = os.fspath(path)
        if self._is_database_file():
            self.directory = os.path.dirname(os.path.abspath(self.path))
            self.database = self.path
            self.manifest = self.path + ".manifest.json"
        else:
            self.directory = self.path
            self.database = os.path.join(self.path, DEFAULT_CATALOG_NAME)
            self.manifest = os.path.join(self.path, MANIFEST_NAME)
        os.makedirs(self.directory, exist_ok=True)
        self._connection = None
        self.session_id = uuid.uuid4().hex
        if os.path.exists(self.database):
            self._open_existing()
        else:
            self._create()

    def _is_database_file(self):
        if os.path.isdir(self.path):
            return False
        if os.path.exists(self.path):
            return True
        return os.path.splitext(self.path)[1].lower() in (
            ".sqlite",
            ".sqlite3",
            ".db",
        )

    def _connect(self):
        if self._connection is None:
            self._connection = sqlite3.connect(self.database)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
        return self._connection

    @property
    def connection(self):
        return self._connect()

    def _create(self):
        connection = self._connect()
        connection.executescript(_SCHEMA)
        connection.execute(
            "INSERT INTO catalog "
            "(id, catalog_uuid, status, format_version) "
            "VALUES (1, ?, ?, ?)",
            (uuid.uuid4().hex, INCOMPLETE, FORMAT_VERSION),
        )
        connection.commit()
        self._write_manifest()

    def _open_existing(self):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT status, format_version FROM catalog WHERE id = 1"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise CatalogIncomplete(
                f"{self.database} is not a group catalog: {exc}"
            )
        if row is None:
            raise CatalogIncomplete(
                f"{self.database} is not a group catalog"
            )
        if row["format_version"] != FORMAT_VERSION:
            raise CatalogIncomplete(
                f"{self.database} has unsupported catalog version "
                f"{row['format_version']}"
            )

    @property
    def uuid(self):
        return self.connection.execute(
            "SELECT catalog_uuid FROM catalog WHERE id = 1"
        ).fetchone()["catalog_uuid"]

    def _write_manifest(self):
        status, total_rows, detail = self.connection.execute(
            "SELECT status, total_rows, detail FROM catalog WHERE id = 1"
        ).fetchone()
        groups = [
            item["name"]
            for item in self.connection.execute(
                "SELECT name FROM groups ORDER BY position"
            )
        ]
        payload = {
            "format_version": FORMAT_VERSION,
            "catalog_uuid": self.uuid,
            "status": status,
            "rows": total_rows,
            "groups": groups,
        }
        if detail:
            payload["detail"] = detail
        temporary = self.manifest + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.manifest)

    def begin(self, source=None):
        """Reset delivery state for a new pass over the source."""
        connection = self.connection
        connection.execute("DELETE FROM acknowledgements")
        connection.execute("DELETE FROM deliveries")
        connection.execute("DELETE FROM readers")
        connection.execute("DELETE FROM seen")
        connection.execute("DELETE FROM rows")
        connection.execute("DELETE FROM groups")
        connection.execute(
            "UPDATE catalog SET status = ?, total_rows = 0, "
            "detail = NULL, source = ?, catalog_uuid = ? WHERE id = 1",
            (INCOMPLETE, source, uuid.uuid4().hex),
        )
        connection.commit()
        self._write_manifest()
        return self

    def append(self, group_name, row, source_row):
        """Append one source row to its group.  Source row is the identity."""
        if not isinstance(group_name, (str, int, float, bool)):
            group_name = str(group_name)
        name = str(group_name)
        connection = self.connection
        found = connection.execute(
            "SELECT position FROM groups WHERE name = ?", (name,)
        ).fetchone()
        if found is None:
            position = connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM groups"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO groups (position, name) VALUES (?, ?)",
                (position, name),
            )
        else:
            position = found["position"]
        _insert_row(connection, position, row, source_row)
        connection.execute(
            "UPDATE catalog SET total_rows = total_rows + 1 WHERE id = 1"
        )
        connection.commit()

    def finish(self, complete=True):
        """Seal the catalog; an incomplete seal is not deliverable."""
        connection = self.connection
        status = COMPLETE if complete else INCOMPLETE
        detail = None if complete else "source did not finish"
        connection.execute(
            "UPDATE catalog SET status = ?, detail = ? WHERE id = 1",
            (status, detail),
        )
        connection.commit()
        self._write_manifest()
        return self

    def abandon(self, detail):
        """Record that the source ended before it was fully scanned."""
        connection = self.connection
        connection.execute(
            "UPDATE catalog SET status = ?, detail = ? WHERE id = 1",
            (INCOMPLETE, detail),
        )
        connection.commit()
        self._write_manifest()

    @property
    def status(self):
        return self.connection.execute(
            "SELECT status FROM catalog WHERE id = 1"
        ).fetchone()["status"]

    @property
    def complete(self):
        return self.status == COMPLETE

    def group_names(self):
        """Group names in first seen order."""
        return [
            item["name"]
            for item in self.connection.execute(
                "SELECT name FROM groups ORDER BY position"
            )
        ]

    @property
    def groups(self):
        """Group names in first seen order, as a list."""
        return _NameList(self.group_names())

    def __iter__(self):
        return iter(self.group_names())

    def __len__(self):
        return len(self.group_names())

    def __getitem__(self, name):
        return self.group(name)

    def group(self, name, reader_id=None):
        """Open a reader for a single group."""
        return GroupReader(self, name, reader_id=reader_id)

    def reader(self, name, reader_id=None):
        """Open a reader with its own progress within a group."""
        return GroupReader(self, name, reader_id=reader_id)

    def iget_group(self, name, reader_id=None):
        """Alias of :meth:`reader` for the on-demand reading style."""
        return self.reader(name, reader_id=reader_id)

    def recover(self, require_complete=True):
        """Report groups in first seen order with per group states.

        :param bool require_complete: raise
            :class:`~pyexcel.exceptions.CatalogIncomplete` when the source
            was not completely scanned
        """
        report = RecoveryReport(self)
        if require_complete and not self.complete:
            raise CatalogIncomplete(
                f"{self.database} is not deliverable because the source "
                f"was not completely scanned: {self._detail()}"
            )
        return report

    def recovery(self, require_complete=True):
        """Alias of :meth:`recover`."""
        return self.recover(require_complete=require_complete)

    def report(self):
        """Report per group states without requiring a complete catalog."""
        return RecoveryReport(self)

    def states(self):
        """Per group counts without loading row payloads."""
        return self.report().counts()

    @property
    def deliverable(self):
        """True only when the source was completely scanned."""
        return self.complete

    def _detail(self):
        row = self.connection.execute(
            "SELECT detail FROM catalog WHERE id = 1"
        ).fetchone()
        return row["detail"] or "unknown reason"

    def summary(self):
        names = self.group_names()
        return OrderedDict(
            (name, self.report().group(name).counts()) for name in names
        )

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


class GroupReader:
    """Deliver the rows of one group, one at a time."""

    def __init__(self, catalog, name, reader_id=None):
        if isinstance(catalog, (str, bytes, os.PathLike)):
            catalog = Catalog(catalog)
        if not catalog.complete:
            raise CatalogIncomplete(
                f"{catalog.database} was not completely scanned: "
                f"{catalog._detail()}"
            )
        row = catalog.connection.execute(
            "SELECT position FROM groups WHERE name = ?", (str(name),)
        ).fetchone()
        if row is None:
            raise UnknownGroup(f"{name!r} is not a group in this catalog")
        self.catalog = catalog
        self.name = str(name)
        self.position = row["position"]
        self.reader_id = (
            str(reader_id) if reader_id is not None else uuid.uuid4().hex
        )
        self.session_id = catalog.session_id
        self._cursor = None
        self._load_cursor()

    def _load_cursor(self):
        if self._cursor is not None:
            return self._cursor
        connection = self.catalog.connection
        row = connection.execute(
            "SELECT next_row_id, start_ack_id FROM readers "
            "WHERE session_id = ? AND group_position = ? "
            "AND reader_id = ?",
            (self.session_id, self.position, self.reader_id),
        ).fetchone()
        if row is not None:
            self._cursor = {
                "next_row_id": row["next_row_id"],
                "start_ack_id": row["start_ack_id"],
            }
            return self._cursor
        self._cursor = {
            "next_row_id": 0,
            "start_ack_id": connection.execute(
                "SELECT COALESCE(MAX(ack_id), 0) FROM acknowledgements "
                "WHERE group_position = ?",
                (self.position,),
            ).fetchone()[0],
        }
        self._save_cursor()
        return self._cursor

    def _save_cursor(self):
        self.catalog.connection.execute(
            "INSERT INTO readers (session_id, group_position, reader_id, "
            "next_row_id, start_ack_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (session_id, group_position, reader_id) DO UPDATE "
            "SET next_row_id = excluded.next_row_id, "
            "start_ack_id = excluded.start_ack_id",
            (
                self.session_id,
                self.position,
                self.reader_id,
                self._cursor["next_row_id"],
                self._cursor["start_ack_id"],
            ),
        )

    def _mark_seen(self, source_row):
        self.catalog.connection.execute(
            "INSERT OR IGNORE INTO seen "
            "(session_id, group_position, reader_id, source_row) "
            "VALUES (?, ?, ?, ?)",
            (self.session_id, self.position, self.reader_id, source_row),
        )

    def next(self):
        """Deliver the next unread row, or None when the group is drained."""
        connection = self.catalog.connection
        cursor = self._load_cursor()
        replayed = connection.execute(
            "SELECT d.delivery_id, d.source_row, d.payload FROM deliveries d "
            "WHERE d.group_position = ? AND NOT EXISTS ("
            "SELECT 1 FROM acknowledgements a "
            "WHERE a.group_position = d.group_position "
            "AND a.source_row = d.source_row) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM seen s "
            "WHERE s.session_id = ? AND s.group_position = d.group_position "
            "AND s.reader_id = ? AND s.source_row = d.source_row) "
            "ORDER BY d.delivery_id LIMIT 1",
            (self.position, self.session_id, self.reader_id),
        ).fetchone()
        if replayed is not None:
            self._mark_seen(replayed["source_row"])
            connection.commit()
            return DeliveredRow(
                self,
                replayed["source_row"],
                replayed["payload"],
                catalog_uuid=self.catalog.uuid,
            )
        row = connection.execute(
            "SELECT row_id, source_row, payload FROM rows "
            "WHERE group_position = ? AND row_id > ? "
            "AND NOT EXISTS ("
            "SELECT 1 FROM acknowledgements a "
            "WHERE a.group_position = rows.group_position "
            "AND a.source_row = rows.source_row "
            "AND a.ack_id <= ?) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM seen s "
            "WHERE s.session_id = ? "
            "AND s.group_position = rows.group_position "
            "AND s.reader_id = ? AND s.source_row = rows.source_row) "
            "ORDER BY row_id LIMIT 1",
            (
                self.position,
                cursor["next_row_id"],
                cursor["start_ack_id"],
                self.session_id,
                self.reader_id,
            ),
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            "INSERT INTO deliveries "
            "(group_position, source_row, payload, fingerprint) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (group_position, source_row) DO UPDATE SET "
            "payload = excluded.payload, fingerprint = excluded.fingerprint",
            (
                self.position,
                row["source_row"],
                row["payload"],
                _fingerprint(row["payload"]),
            ),
        )
        cursor["next_row_id"] = row["row_id"]
        self._mark_seen(row["source_row"])
        self._save_cursor()
        connection.commit()
        return DeliveredRow(
            self,
            row["source_row"],
            row["payload"],
            catalog_uuid=self.catalog.uuid,
        )

    def __iter__(self):
        return self

    def __next__(self):
        record = self.next()
        if record is None:
            raise StopIteration
        return record

    def _pending_cursor(self):
        connection = self.catalog.connection
        return connection.execute(
            "SELECT d.source_row, d.payload FROM deliveries d "
            "WHERE d.group_position = ? AND NOT EXISTS ("
            "SELECT 1 FROM acknowledgements a "
            "WHERE a.group_position = d.group_position "
            "AND a.source_row = d.source_row) "
            "ORDER BY d.delivery_id",
            (self.position,),
        )

    def pending(self):
        """Rows delivered and not yet acknowledged, in delivery order."""
        return PendingRows(self)

    def acknowledge(self, record):
        """Acknowledge one delivered row without consuming any other group."""
        source_row = _source_row(record)
        connection = self.catalog.connection
        identity = getattr(record, "catalog_uuid", None)
        if identity is not None and identity != self.catalog.uuid:
            raise UnknownGroup("row belongs to a different catalog")
        found = connection.execute(
            "SELECT 1 FROM deliveries "
            "WHERE group_position = ? AND source_row = ?",
            (self.position, source_row),
        ).fetchone()
        if found is None:
            raise UnknownGroup(
                f"row {source_row} was not delivered by {self.name}"
            )
        connection.execute(
            "INSERT OR IGNORE INTO acknowledgements "
            "(group_position, source_row, session_id) VALUES (?, ?, ?)",
            (self.position, source_row, self.session_id),
        )
        connection.commit()
        return self

    def acknowledge_row(self, source_row):
        """Acknowledge a row by its original source row number."""
        return self.acknowledge(
            _RowHandle(self.name, source_row, self.catalog.uuid)
        )

    def ack(self, record):
        """Alias of :meth:`acknowledge`."""
        return self.acknowledge(record)

    def confirm(self, record):
        """Alias of :meth:`acknowledge`."""
        return self.acknowledge(record)

    def rewind(self):
        """Restart this reader at the first row of the group."""
        self._cursor = None
        self.catalog.connection.execute(
            "DELETE FROM readers WHERE session_id = ? "
            "AND group_position = ? AND reader_id = ?",
            (self.session_id, self.position, self.reader_id),
        )
        self.catalog.connection.execute(
            "DELETE FROM seen WHERE session_id = ? "
            "AND group_position = ? AND reader_id = ?",
            (self.session_id, self.position, self.reader_id),
        )
        self.catalog.connection.commit()
        return self

    def state(self):
        return self.catalog.report().group(self.name)

    def close(self):
        return None


class DeliveredRow:
    """A row handed to a downstream consumer."""

    def __init__(self, reader, source_row, payload, catalog_uuid=None):
        self.reader = reader
        self.source_row = source_row
        self.group = reader.name if reader is not None else None
        self.catalog_uuid = catalog_uuid
        self._payload = payload

    @property
    def values(self):
        if self._payload is None:
            return None
        return json.loads(self._payload)

    def acknowledge(self):
        self.reader.acknowledge(self)
        return self

    ack = acknowledge
    confirm = acknowledge

    def __eq__(self, other):
        if isinstance(other, DeliveredRow):
            return (
                self.group == other.group
                and self.source_row == other.source_row
                and self._payload == other._payload
            )
        return NotImplemented

    def __repr__(self):
        return (
            f"DeliveredRow(group={self.group!r}, "
            f"source_row={self.source_row!r}, values={self.values!r})"
        )


class PendingRows:
    """Lazily delivered but unacknowledged rows of one reader."""

    def __init__(self, reader):
        self.reader = reader

    def __iter__(self):
        cursor = self.reader._pending_cursor()
        for row in cursor:
            yield DeliveredRow(
                self.reader,
                row["source_row"],
                row["payload"],
                catalog_uuid=self.reader.catalog.uuid,
            )

    def __len__(self):
        connection = self.reader.catalog.connection
        return connection.execute(
            "SELECT COUNT(*) FROM deliveries d "
            "WHERE d.group_position = ? AND NOT EXISTS ("
            "SELECT 1 FROM acknowledgements a "
            "WHERE a.group_position = d.group_position "
            "AND a.source_row = d.source_row)",
            (self.reader.position,),
        ).fetchone()[0]

    def __bool__(self):
        return len(self) > 0


class _RowHandle:
    def __init__(self, group, source_row, catalog_uuid=None):
        self.group = group
        self.source_row = source_row
        self.catalog_uuid = catalog_uuid


class GroupState:
    """Per group views of acknowledged, pending replay and unread rows."""

    def __init__(self, catalog, name, position):
        self.catalog = catalog
        self.name = name
        self.position = position
        self.acknowledged = RowSet(catalog, position, ACKNOWLEDGED)
        self.pending = RowSet(catalog, position, PENDING)
        self.unread = RowSet(catalog, position, UNREAD)

    @property
    def total(self):
        return len(self.acknowledged) + len(self.pending) + len(self.unread)

    def summary(self):
        return OrderedDict(
            (
                (
                    ACKNOWLEDGED,
                    [row.source_row for row in self.acknowledged],
                ),
                (PENDING, [row.source_row for row in self.pending]),
                (UNREAD, [row.source_row for row in self.unread]),
            )
        )

    def counts(self):
        return OrderedDict(
            (
                (ACKNOWLEDGED, len(self.acknowledged)),
                (PENDING, len(self.pending)),
                (UNREAD, len(self.unread)),
            )
        )

    def rows(self, state):
        return getattr(self, state)

    def __repr__(self):
        return (
            f"GroupState(name={self.name!r}, "
            f"acknowledged={len(self.acknowledged)}, "
            f"pending={len(self.pending)}, unread={len(self.unread)})"
        )


class RowSet:
    """A lazily evaluated set of rows in one of the three states."""

    def __init__(self, catalog, position, state):
        self.catalog = catalog
        self.position = position
        self.state = state

    def _sql(self):
        if self.state == ACKNOWLEDGED:
            return (
                "SELECT a.group_position, r.source_row, r.payload "
                "FROM acknowledgements a JOIN rows r ON "
                "r.group_position = a.group_position "
                "AND r.source_row = a.source_row "
                "WHERE a.group_position = ? ORDER BY r.row_id"
            )
        if self.state == PENDING:
            return (
                "SELECT d.group_position, d.source_row, d.payload "
                "FROM deliveries d "
                "WHERE d.group_position = ? AND NOT EXISTS ("
                "SELECT 1 FROM acknowledgements a "
                "WHERE a.group_position = d.group_position "
                "AND a.source_row = d.source_row) "
                "ORDER BY d.delivery_id"
            )
        return (
            "SELECT r.group_position, r.source_row, r.payload FROM rows r "
            "WHERE r.group_position = ? AND NOT EXISTS ("
            "SELECT 1 FROM deliveries d "
            "WHERE d.group_position = r.group_position "
            "AND d.source_row = r.source_row) ORDER BY r.row_id"
        )

    def _count_sql(self):
        return f"SELECT COUNT(*) FROM ({self._sql()})"

    def __iter__(self):
        cursor = self.catalog.connection.execute(self._sql(), (self.position,))
        for row in cursor:
            yield _replayed(row, self.catalog.uuid)

    def __len__(self):
        return self.catalog.connection.execute(
            self._count_sql(), (self.position,)
        ).fetchone()[0]

    def __bool__(self):
        return len(self) > 0

    def __getitem__(self, index):
        if isinstance(index, slice):
            return list(self)[index]
        return next(islice(self, index, index + 1))

    def __eq__(self, other):
        return list(self) == list(other)

    def __repr__(self):
        return f"RowSet(state={self.state!r}, rows={len(self)})"


class _NameList(list):
    pass


class RecoveryReport:
    """Groups a complete catalog in first seen order."""

    def __init__(self, catalog):
        self.catalog = catalog
        self._positions = OrderedDict()
        for item in catalog.connection.execute(
            "SELECT position, name FROM groups ORDER BY position"
        ):
            self._positions[item["name"]] = item["position"]

    def group_names(self):
        return list(self._positions)

    def group(self, name):
        try:
            position = self._positions[name]
        except KeyError:
            raise GroupNotFound(f"{name!r} is not a group in this catalog")
        return GroupState(self.catalog, name, position)

    def __iter__(self):
        for name in self._positions:
            yield self.group(name)

    def __len__(self):
        return len(self._positions)

    def __getitem__(self, name):
        return self.group(name)

    def summary(self):
        return OrderedDict(
            (name, self.group(name).summary()) for name in self._positions
        )

    def counts(self):
        return OrderedDict(
            (name, self.group(name).counts()) for name in self._positions
        )


class _ReportRow:
    def __init__(self, group, source_row, payload, catalog_uuid=None):
        self.group = group
        self.source_row = source_row
        self.payload = payload
        self.catalog_uuid = catalog_uuid
        self.values = json.loads(payload)

    def __repr__(self):
        return (
            f"ReportRow(group={self.group!r}, "
            f"source_row={self.source_row!r}, values={self.values!r})"
        )


def _replayed(row, catalog_uuid=None):
    return _ReportRow(
        row["group_position"],
        row["source_row"],
        row["payload"],
        catalog_uuid,
    )


def _insert_row(connection, position, row, source_row):
    payload = json.dumps(list(row), ensure_ascii=False, default=str)
    fingerprint = _fingerprint(payload)
    connection.execute(
        "INSERT INTO rows "
        "(group_position, source_row, payload, fingerprint) "
        "VALUES (?, ?, ?, ?)",
        (position, source_row, payload, fingerprint),
    )


def _source_row(record):
    if isinstance(record, DeliveredRow):
        return record.source_row
    if hasattr(record, "source_row"):
        return record.source_row
    if isinstance(record, int):
        return record
    raise TypeError("a delivered row or source row number is required")


def _fingerprint(payload):
    return sha1(payload.encode("utf-8")).hexdigest()
