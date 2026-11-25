"""
    pyte.reader
    ~~~~~~~~~~~

    Public reader entry point for captured terminal archives.

    This module is a thin facade over :mod:`pyte.archive` and
    :mod:`pyte.ledger`; it exists so callers can use a single import for
    opening a capture, counting its records and fetching a record by position::

        >>> import pyte.reader
        >>> capture = pyte.reader.open_capture(
        ...     b"one\\r\\ntwo\\r\\nthree\\r\\nfour\\r\\n", columns=10, lines=2)
        >>> pyte.reader.count(capture)
        3
        >>> pyte.reader.get(capture, 1).stripped
        'one'

    The command line front end answers the same questions::

        $ python -m pyte.reader capture.arc --count
        $ python -m pyte.reader capture.arc --record 3
        $ python -m pyte.reader capture.arc --tail 5
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Iterator

from .archive import (
    MAGIC, VERSION, Archive, ArchiveError, ArchiveFormatError,
    ArchiveReader, ArchiveWriter, ByteRange, Occurrence, Segment,
    build_archive, encode_archive, load_archive, open_archive,
    parse_archive, read_archive, write_archive,
)
from .ledger import (
    ArchiveLedger, CaptureLedger, CaptureRecord, Cell, CellRecord,
    DEFAULT_FAST_THRESHOLD, Ledger, LedgerError, LedgerScreen, Record,
    RecordLedger, count_records, load_capture, open_capture, read_capture,
    records, scan_capture,
)

#: Short names used by callers who treat the reader as a sequence of records.
CaptureReader = Ledger
Reader = Ledger


def count(source: Any, **kwargs: Any) -> int:
    """Number of records produced by ``source``."""
    return len(source) if isinstance(source, Ledger) else count_records(
        source, **kwargs)


def get(source: Any, number: int, **kwargs: Any) -> Record:
    """Return record ``number`` (1-based, negative counts from the end)."""
    capture = source if isinstance(source, Ledger) else Ledger(source, **kwargs)
    if number < 0:
        number += len(capture) + 1
    return capture.record(number)


def read(source: Any, **kwargs: Any) -> Ledger:
    """Alias for :func:`read_capture`."""
    return Ledger(source, **kwargs)


def iter_records(source: Any, **kwargs: Any) -> Iterator[Record]:
    """Alias for :func:`records`."""
    return records(source, **kwargs)


def cell_to_dict(cell: CellRecord) -> dict[str, Any]:
    return {
        "x": cell.x,
        "data": cell.data,
        "source": cell.source,
        "occurrence": cell.occurrence,
        "start": cell.start,
        "end": cell.end,
        "stored_start": cell.stored_start,
        "stored_end": cell.stored_end,
        "fg": cell.fg,
        "bg": cell.bg,
        "bold": cell.bold,
        "italics": cell.italics,
        "underscore": cell.underscore,
        "strikethrough": cell.strikethrough,
        "reverse": cell.reverse,
        "blink": cell.blink,
    }


def record_to_dict(record: Record, cells: bool = True) -> dict[str, Any]:
    result = {
        "index": record.index,
        "text": record.text,
        "stripped": record.stripped,
        "trigger": record.trigger,
        "top": record.top,
        "bottom": record.bottom,
        "byte_range": list(record.byte_range),
    }
    if cells:
        result["cells"] = [cell_to_dict(cell) for cell in record.cells]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pyte.reader",
        description="Inspect a captured terminal archive.")
    parser.add_argument("archive", help="capture file (or '-' for stdin)")
    parser.add_argument("--columns", type=int, default=None)
    parser.add_argument("--lines", type=int, default=None)
    parser.add_argument("--history", type=int, default=100)
    parser.add_argument("--count", action="store_true",
                        help="print only the number of records")
    parser.add_argument("--record", type=int, default=None,
                        help="print record N (1-based; negative from end)")
    parser.add_argument("--tail", type=int, default=None,
                        help="print the last N records")
    parser.add_argument("--no-cells", action="store_true",
                        help="omit per-cell provenance")
    parser.add_argument("--json", action="store_true",
                        help="print JSON instead of plain text")
    args = parser.parse_args(argv)

    if args.archive == "-":
        data = sys.stdin.buffer.read()
        source: Any = data
    else:
        source = args.archive
    capture = Ledger(source, columns=args.columns, lines=args.lines,
                     history=args.history)

    def emit(record: Record) -> None:
        if args.json:
            print(json.dumps(record_to_dict(record, not args.no_cells),
                             ensure_ascii=False))
        else:
            print(f"{record.index}\t{record.trigger}\t{record.top}\t"
                  f"{record.text}")

    if args.count:
        if args.json:
            print(json.dumps({"count": len(capture)}))
        else:
            print(len(capture))
    elif args.record is not None:
        emit(capture.record(args.record))
    elif args.tail is not None:
        for record in capture.tail(args.tail):
            emit(record)
    else:
        print(len(capture) if not args.json else
              json.dumps({"count": len(capture)}))
    return 0


__all__ = [
    "Archive",
    "ArchiveError",
    "ArchiveFormatError",
    "ArchiveReader",
    "ArchiveWriter",
    "ArchiveLedger",
    "ByteRange",
    "CaptureLedger",
    "CaptureReader",
    "CaptureRecord",
    "Cell",
    "CellRecord",
    "DEFAULT_FAST_THRESHOLD",
    "Ledger",
    "LedgerError",
    "LedgerScreen",
    "MAGIC",
    "Occurrence",
    "Reader",
    "Record",
    "RecordLedger",
    "Segment",
    "VERSION",
    "build_archive",
    "cell_to_dict",
    "count",
    "count_records",
    "encode_archive",
    "get",
    "iter_records",
    "load_archive",
    "load_capture",
    "main",
    "open_archive",
    "open_capture",
    "parse_archive",
    "read",
    "read_archive",
    "read_capture",
    "record_to_dict",
    "records",
    "scan_capture",
    "write_archive",
]


if __name__ == "__main__":
    raise SystemExit(main())
