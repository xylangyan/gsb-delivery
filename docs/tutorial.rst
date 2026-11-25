.. _tutorial:

Tutorial
--------

There are two important classes in ``pyte``: :class:`~pyte.screens.Screen`
and :class:`~pyte.streams.Stream`. The `Screen` is the terminal screen
emulator. It maintains an in-memory buffer of text and text-attributes
to display. The `Stream` is the stream processor. It processes the input
and dispatches events. Events are things like ``LINEFEED``, ``DRAW "a"``,
or ``CURSOR_POSITION 10 10``. See the :ref:`API reference <api>` for more
details.

In general, if you just want to know what's being displayed on screen you
can do something like the following:

    >>> import pyte
    >>> screen = pyte.Screen(80, 24)
    >>> stream = pyte.Stream(screen)
    >>> stream.feed("Hello World!")
    >>> screen.display
        ['Hello World!                                                                    ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ',
         '                                                                                ']


**Note**: ``Screen`` has no idea what is the source of bytes fed into ``Stream``,
so, obviously, it **can't read** or **change** environment variables, which implies
that:

* it doesn't adjust `LINES` and `COLUMNS` on ``"resize"`` event;
* it doesn't use locale settings (`LC_*` and `LANG`);
* it doesn't use `TERM` value and expects it to be `"linux"` and only `"linux"`.

And that's it for Hello World! Head over to the `examples
<https://github.com/selectel/pyte/tree/master/examples>`_ for  more.

Capture archives
----------------

A capture archive stores a terminal byte stream, optionally run-length
encoded: consecutive identical byte blocks are stored once as a template and
referenced by a repeat count.  :class:`~pyte.archive.Archive` parses such a
file without expanding the repeats, and :class:`~pyte.ledger.Ledger` replays
it through a :class:`~pyte.screens.HistoryScreen` to extract records.

A record is the line at the top of the current scrolling region at the
moment a forward scroll (``linefeed``, ``index`` or auto-wrap) pushes it out.
Paging through history, reverse scrolls and ``CSI 3 J`` do not create
records, and records already taken are unaffected by a later clear.  Each
cell carries the archive segment, occurrence and byte range that wrote it:

    >>> import io, pyte
    >>> from pyte import archive
    >>> writer = archive.ArchiveWriter(columns=10, lines=3)
    >>> _ = writer.literal(b"one\r\ntwo\r\nthree\r\nfour\r\n")
    >>> capture = pyte.Ledger(io.BytesIO(writer.to_bytes()))
    >>> len(capture)
    2
    >>> capture.record(1).text
    'one       '
    >>> capture.record(1).stripped
    'one'
    >>> cell = capture.record(1).cells[0]
    >>> (cell.data, cell.start, cell.end)
    ('o', 0, 1)

Long runs of the same template are summarized: the ledger detects the
periodic screen transition and stores one cycle plus the repeat count, so
``len(capture)`` and ``capture.tail(n)`` stay cheap even for hundreds of
millions of repeats.  A file which does not start with the archive magic is
treated as one literal segment, so plain captures work unchanged.

