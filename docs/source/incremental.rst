===============================================================================
Resumable CSV projection
===============================================================================

Supplier files are often headerless: the meaning of each column comes from an
agreement rather than from a first row.  They can also be large enough that an
import should not keep the whole file in memory, and an import may confirm
records asynchronously so that a restart must continue where it left off
instead of replaying everything it already wrote.

:class:`pyexcel.incremental.CSVProjection` reads a headerless CSV file one
logical record at a time, projects a declared list of columns in a declared
order and reports, for every field, one of three update intents.

Declaring the projection
------------------------

Both the projected columns and the full positional schema of the file are
given explicitly::

    from pyexcel import CSVProjection

    projection = CSVProjection(
        columns=["order_no", "store_no", "amount"],
        field_names=["order_no", "store_no", "amount", "note"],
        file_name="supplier.csv",
        progress_file="supplier.progress.json",
    )

``field_names`` describes every column in the file from left to right.  The
file itself has no header row.  A projected name that is not part of the
schema raises :class:`ProjectionConfigurationError`.

Update intents
--------------

Every delivered record carries one :class:`FieldUpdate` per projected column.
``FieldUpdate.intent`` is an :class:`UpdateIntent`:

``UpdateIntent.ABSENT``
    The logical record is shorter than the column index, so this record does
    not mention the column at all.  ``value`` is ``None``; an import must leave
    the stored value untouched.

``UpdateIntent.CLEAR``
    The column is present but empty, whether as an explicit empty field
    (``a,,c``) or as a quoted empty string (``a,"",c``).  ``value`` is ``""``;
    an import must clear the stored value.

``UpdateIntent.SET``
    The column carries a value.  ``value`` holds the text converted with the
    usual CSV heuristics, so ``"0"`` becomes ``0``.  ``FieldUpdate.text`` keeps
    the original text for auditing.

.. code-block:: python

    for record in projection:
        for update in record.updates.values():
            print(record.index, record.position.line_number, update.column,
                  update.intent, update.value)
        projection.ack(record)

Source positions
----------------

``record.position`` is a :class:`SourcePosition` with byte offsets and line
numbers.  ``offset`` points at the first byte of the logical record;
``end_offset`` points at the first byte after its terminator.  A record whose
field contains a quoted newline consumes several physical lines, and its
position reflects the real bytes, not the number of records read so far.

Resuming
--------

An import confirms a record only after the downstream store has accepted it.
Calling ``ack()`` (or its alias ``confirm()``) persists a checkpoint that
contains byte offsets and counters only -- no part of the file content.  A new
``CSVProjection`` using the same ``progress_file`` and the same unchanged
source starts at the last confirmed record:

.. code-block:: python

    projection = CSVProjection(
        ["order_no", "store_no", "amount"],
        ["order_no", "store_no", "amount", "note"],
        file_name="supplier.csv",
        progress_file="supplier.progress.json",
    )
    for record in projection:
        store(record)
        projection.ack(record)

The checkpoint is written atomically.  If the source content changed, or the
checkpoint belongs to a different projection, the constructor raises
:class:`StaleProgressError` or :class:`ProjectionConfigurationError` instead of
resuming at a meaningless byte.

A record that was delivered but not confirmed is delivered again in full.
Confirmed records are never replayed.  A record that cannot be projected at
all -- because it is wider than the declared schema or because the file ends
inside a quoted field -- raises an error carrying the zero-based logical record
index and, where meaningful, the offending column.  Such a record never
advances the checkpoint, so the progress made before it remains reusable.

Errors
------

``RecordTooWideError``
    A record has more fields than the declared schema.  ``column_index`` is the
    index of the first extra field and ``extra_fields`` contains the surplus
    values.

``UnterminatedFieldError``
    The file ended inside a quoted field.

``MalformedCSVError``
    Text followed a closing quote when ``strict`` is enabled (the default).

All three derive from :class:`CSVFormatError` and expose ``record_index``,
``column_index``, ``column_name``, ``line_number`` and ``position``.

Chunking
--------

``chunk_size`` selects how many bytes are read at a time.  The projection is
independent of chunk boundaries; a buffer may contain bytes that belong to the
next record without affecting the result.  When a resumed projection has a
different ``chunk_size`` the delivered records, their positions and their
intents are identical to those of a single sequential pass.

The default CSV behaviour of pyexcel is unchanged: :func:`pyexcel.get_sheet`,
:func:`pyexcel.iget_records` and the other signature functions keep using the
existing CSV reader.
