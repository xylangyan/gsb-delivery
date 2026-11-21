"""
pyexcel.internal.rowstream
~~~~~~~~~~~~~~~~~~~~~~~~~~

Row by row streaming reader for workbook files.

Unlike :func:`pyexcel.get_book`, this reader never materialises a whole
workbook into a :class:`pyexcel.Book`.  Only the sheet names are read up
front while every data row is pulled lazily through the underlying row
reader (for example openpyxl's read only mode).  A handle holds exactly
one physical resource which is released as soon as the handle is closed,
even when a read is abandoned early or fails midway.

Resources opened by one handle are never shared with another handle, so
closing one read never disturbs a second reader that is in progress.

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License
"""

from pyexcel_io.reader import Reader

from pyexcel.exceptions import SheetNotFound
from pyexcel.internal.generators import BookStream, SheetStream

# Row based XLSX reading relies on openpyxl's optimised, read only
# worksheet.  Hidden row and column handling forces openpyxl out of read
# only mode, so streaming keeps the fast path unconditionally.
_STREAMING_KEYWORDS = {"skip_hidden_row_and_column": False}


class _RowSheetStream(SheetStream):
    """A sheet stream whose row generator can be closed early."""

    def __init__(self, name, payload):
        SheetStream.__init__(self, name, payload)
        self._released = False

    def close(self):
        """Stop row iteration and release this sheet's row resources."""
        if self._released:
            return
        self._released = True
        self.payload.close()


class RowBookStream:
    """
    Lazy, row by row view over a single workbook file.

    The underlying reader is opened once.  Sheet names are available
    immediately while sheet rows are consumed on demand::

        handle = open_row_book_stream(
            file_name="roster.xlsx", sheet_names=["Nov_B", "Nov_A"]
        )
        try:
            for row in handle["Nov_A"].payload:
                ...
        finally:
            handle.close()

    The object can be used as a context manager.  Closing it releases the
    resource that this handle opened itself; a file object handed in by
    the caller is left open for the caller to close.
    """

    def __init__(self, reader, sheet_streams):
        self._reader = reader
        self._closed = False
        self._book_stream = BookStream()
        self._book_stream.sheets = sheet_streams
        self._book_stream.name_array = list(sheet_streams.keys())
        for name, sheet_stream in sheet_streams.items():
            self._book_stream.__dict__[name] = sheet_stream

    def sheet_names(self):
        """Return the selected sheet names in workbook order."""
        return self._book_stream.sheet_names()

    def all_sheet_names(self):
        """Return every workbook sheet name in its original order."""
        return list(self._reader.reader.sheet_names())

    def __getitem__(self, key):
        return self._book_stream[key]

    def __iter__(self):
        return iter(self._book_stream)

    def number_of_sheets(self):
        """Return the number of selected sheets."""
        return self._book_stream.number_of_sheets()

    def close(self):
        """Release every resource held by this handle."""
        if self._closed:
            return
        self._closed = True
        # Finalise the row generators first: they hold worksheet rows
        # whose archive would otherwise stay open until garbage collection.
        for sheet_stream in self._book_stream.sheets.values():
            sheet_stream.close()
        self._reader.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _resolve_sheet_indices(reader, sheet_names=None, sheet_indexes=None):
    """Map requested sheets onto workbook positions in workbook order."""
    workbook_names = reader.reader.sheet_names()
    if sheet_names is not None:
        missing = [name for name in sheet_names if name not in workbook_names]
        if missing:
            raise SheetNotFound(
                "Sheet(s) not found in workbook: %s" % ", ".join(missing)
            )
        name_to_index = dict(
            (name, index) for index, name in enumerate(workbook_names)
        )
        indexes = [name_to_index[name] for name in sheet_names]
    elif sheet_indexes is not None:
        indexes = []
        for index in sheet_indexes:
            if index < 0 or index >= len(workbook_names):
                raise SheetNotFound("Sheet index out of range: %d" % index)
            indexes.append(index)
    else:
        indexes = list(range(len(workbook_names)))
    # De-duplicate and return the sheets in the workbook's physical order.
    return sorted(set(indexes)), workbook_names


def _row_generator(sheet_reader):
    """Yield each data row as a list of cell values."""
    for row in sheet_reader.row_iterator():
        yield list(sheet_reader.column_iterator(row))


def open_row_book_stream(
    file_name=None,
    file_stream=None,
    file_type=None,
    sheet_names=None,
    sheet_indexes=None,
    library=None,
    **keywords
):
    """
    Open a workbook for row by row streaming without loading it all.

    :param file_name: path of the workbook to open.  The file handle is
                      owned and closed by the returned handle.
    :param file_stream: an already opened binary file object.  It stays
                        owned by the caller and remains open after the
                        handle is closed.
    :param file_type: required when ``file_stream`` is given, e.g. xlsx.
    :param sheet_names: names of the sheets to expose.  Only metadata is
                        read for the other sheets.
    :param sheet_indexes: workbook positions of the sheets to expose.
    :param library: explicit pyexcel-io reader library name.
    :returns: :class:`RowBookStream` whose selected sheets are delivered in
              the workbook's original sheet order.
    """
    if file_name is None and file_stream is None:
        raise ValueError("Please provide file_name or file_stream")
    if file_name is not None and file_stream is not None:
        raise ValueError(
            "Please provide only one of file_name or file_stream"
        )

    keywords.update(_STREAMING_KEYWORDS)
    if file_type is None and file_name is not None:
        file_type = str(file_name).split(".")[-1]
    reader = Reader(file_type or "xlsx", library)
    sheet_streams = {}
    try:
        if file_name is not None:
            reader.open(file_name, **keywords)
        else:
            reader.open_stream(file_stream, **keywords)
        indexes, workbook_names = _resolve_sheet_indices(
            reader,
            sheet_names=sheet_names,
            sheet_indexes=sheet_indexes,
        )
        for index in indexes:
            sheet_reader = reader.reader.read_sheet(index)
            name = workbook_names[index]
            sheet_streams[name] = _RowSheetStream(
                name,
                _row_generator(sheet_reader),
            )
    except BaseException:
        for sheet_stream in sheet_streams.values():
            sheet_stream.close()
        if hasattr(reader, "reader"):
            reader.close()
        raise
    return RowBookStream(reader, sheet_streams)
