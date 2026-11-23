=========
jsonlines
=========

.. py:currentmodule:: jsonlines

``jsonlines`` is a Python library to simplify working with jsonlines_
and ndjson_ data.

.. _jsonlines: http://jsonlines.org/
.. _ndjson: https://github.com/ndjson/ndjson-spec

This data format is straight-forward: it is simply one valid JSON
value per line, encoded using UTF-8. While code to consume and create
such data is not that complex, it quickly becomes non-trivial enough
to warrant a dedicated library when adding data validation, error
handling, support for both binary and text streams, and so on. This
small library implements all that (and more!) so that applications
using this format do not have to reinvent the wheel.


Features
========

* Sensible behaviour for most use cases

  * transparently handles ``str`` and ``bytes``, both for input and output
  * supports multiple JSON libraries, e.g. ``json`` (standard library), ``orjson``, ``ujson``
  * transparently handles UTF-8 BOM (if present)
  * useful error messages
  * prevents gotchas, e.g. uses standard-compliant line breaking, unlike `str.splitlines`_

  .. _str.splitlines: https://docs.python.org/3/library/stdtypes.html#str.splitlines

* Convenient :py:func:`~jsonlines.open()` function

  * makes simple cases trivial to write
  * takes a file name and a mode
  * returns either a :py:class:`~jsonlines.Reader` or
    :py:class:`~jsonlines.Writer` instance
  * can be used as a context manager

* Flexible :py:class:`~jsonlines.Reader`

  * wraps a file-like object or any other iterable yielding lines
  * can read lines directly via the
    :py:meth:`~jsonlines.Reader.read()` method
  * can be used as an iterator, either directly or via the
    :py:meth:`~jsonlines.Reader.iter()` method
  * can validate data types, including `None` checks
  * can skip invalid lines during iteration
  * provides decent error messages
  * can be used as a context manager
  * allows complete control over decoding using a custom ``loads``
    callable

* Flexible :py:class:`~jsonlines.Writer`

  * wraps a file-like object
  * can produce compact output
  * can sort keys (deterministic output)
  * can flush the underlying stream after each write
  * can be used as a context manager
  * allows complete control over encoding using a custom ``dumps``
    callable


Installation
============

::

  pip install jsonlines

The supported Python versions are 3.8+.


User guide
==========

Import the ``jsonlines`` module to get started:

.. code-block:: python

    import jsonlines


The convenience function :py:func:`jsonlines.open()` takes a file name
and returns either a reader or writer, making simple cases extremely
simple::

  with jsonlines.open('input.jsonl') as reader:
      for obj in reader:
          ...

  with jsonlines.open('output.jsonl', mode='w') as writer:
      writer.write(...)

A :py:class:`Reader` typically wraps a file-like object::

  fp = io.BytesIO(...)  # readable file-like object
  reader = jsonlines.Reader(fp)
  first = reader.read()
  second = reader.read()
  reader.close()
  fp.close()

Instead of a file-like object, any iterable yielding JSON encoded
strings can be provided::

  lines = ['1', '2', '3']
  reader = jsonlines.Reader(lines)

While the :py:meth:`Reader.read` method can be used directly, it is
often more convenient to use iteration::

  for obj in reader:
      ...

Custom iteration flags, such as type checks, can be specified by
calling :py:meth:`Reader.iter()` instead::

  for obj in reader.iter(type=dict, skip_invalid=True):
      ...

A :py:class:`Writer` wraps a file-like object, and can write a single
object, or multiple objects at once::

  fp = io.BytesIO()  # writable file-like object
  writer = jsonlines.Writer(fp)
  writer.write(...)
  writer.write_all([
      ...,
      ...,
      ...,
  ])
  writer.close()
  fp.close()

Both readers and writers can be used as a context manager, in which
case they will be closed automatically. Note that this will not close
a passed-in file-like object since that object’s life span is
controlled by the calling code. Example::

  fp = io.BytesIO()  # file-like object
  with jsonlines.Writer(fp) as writer:
      writer.write(...)
  fp.close()

Note that the :py:func:`jsonlines.open()` function *does* close the
opened file, since the open file is not explicitly opened by the
calling code. That means no ``.close()`` is needed there::

  with jsonlines.open('input.jsonl') as reader:
      ...

This should be enough to get started. See the API docs below for
more details.


API
===

.. autofunction:: jsonlines.open

.. autoclass:: jsonlines.Reader
   :members:
   :inherited-members:

.. autoclass:: jsonlines.Writer
   :members:
   :inherited-members:

Forensic recovery of interleaved append streams
================================================

The old :class:`Writer` performs two independent stream writes per record —
the JSON body and the line feed — and multiple writers may append to the
same stream. Because each append is atomic but interleaved, a body and its
line feed need not be adjacent, and a successful-looking prefix (for
example ``123`` followed by two line feeds) cannot be assumed to be one
record. The :mod:`jsonlines.recovery` module reconstructs what can be
proven from the raw bytes plus a *receipt* of remembered write facts,
without ever modifying the analysed file.

Receipt facts are limited to what is actually known:

* per-source contracts: the JSON type and body byte-length range each
  source produces;
* records whose Writer returned success, optionally with remembered body
  start/length and line feed positions in absolute file coordinates;
* body writes whose Writer never returned success (the bytes may exist,
  but no line feed or successful record can be claimed for them);
* per-source record ordering (the order in the receipt).

Recovery enumerates every byte span that is a canonical default-serialized
JSON body (so ``01`` or ``1.00`` are never mistaken for writer output),
then solves the placement constraints: bodies never overlap, line feeds
are distinct, and each source's writes keep their success order. The
result distinguishes:

* **confirmed records** — source plus the exact body and line feed byte
  ranges in every feasible interpretation;
* **uncertain regions** — exact byte ranges that stay ambiguous, with the
  concrete placements (including competing source attributions and orphan
  bodies) that remain possible;
* **incompatible facts** — receipt entries that no ordering can satisfy.

Coordinates are always absolute byte offsets, so adding a later fact (for
example a remembered body length and device) re-checks the same bytes with
stable coordinates::

  from jsonlines.recovery import (
      JsonType, Receipt, RecordReceipt, SourceSpec, recover,
  )

  receipt = Receipt(
      sources={
          "A": SourceSpec("A", JsonType.INTEGER, 1, 2),
          "B": SourceSpec("B", JsonType.INTEGER, 1, 2),
      },
      records=[RecordReceipt("A"), RecordReceipt("B")],
  )
  result = recover(b"123\n\n", receipt)
  # result.confirmed is empty; result.uncertain lists the exact range and
  # both splits ("1"+"23" and "12"+"3") as alternatives.

A command line interface is available for read-only analysis::

  python -m jsonlines recover stream.bin --receipt receipt.json --json

.. automodule:: jsonlines.recovery
   :members:
   :undoc-members:

.. autoclass:: jsonlines.Error
   :members:

.. autoclass:: jsonlines.InvalidLineError
   :members:


Contributing
============

The source code and issue tracker for this package can be found on GitHub:

  https://github.com/wbolster/jsonlines


Version history
===============

* 4.0.0, released at 2023-09-01

  * use ‘orjson’ or ‘ujson’ for reading if available
    (`#81 <https://github.com/wbolster/jsonlines/pull/81>`_)

  * drop support for end-of-life Python versions; this package is now
    Python 3.8+ only.
    (`#80 <https://github.com/wbolster/jsonlines/pull/80>`_,
    `#80 <https://github.com/wbolster/jsonlines/pull/85>`_)

* 3.1.0, released at 2022-07-01

  * Return number of chars/bytes written by :py:meth:`Writer.write()`
    and :py:meth:`~Writer.write_all()`
    (`#73 <https://github.com/wbolster/jsonlines/pull/73>`_)

  * allow ``mode='x'`` in :py:func:`~jsonlines.open()`
    to open a file for exclusive creation
    (`#74 <https://github.com/wbolster/jsonlines/issues/74>`_)

* 3.0.0, released at 2021-12-04

  * add type annotations; adopt mypy in strict mode
    (`#58 <https://github.com/wbolster/jsonlines/pull/58>`_,
    `#62 <https://github.com/wbolster/jsonlines/pull/62>`_)

  * ignore UTF-8 BOM sequences in various scenarios
    (`#69 <https://github.com/wbolster/jsonlines/pull/69>`_)

  * support ``dumps()`` callables returning bytes again
    (`#64 <https://github.com/wbolster/jsonlines/issues/64>`_)

  * add basic support for rfc7464 text sequences
    (`#61 <https://github.com/wbolster/jsonlines/pull/61>`_)

  * drop support for ``numbers.Number`` in ``type=`` arguments
    (`#63 <https://github.com/wbolster/jsonlines/issues/63>`_)

* 2.0.0, released at 2021-01-04

  * drop support for end-of-life Python versions; this package is now
    Python 3.6+ only.
    (`#54 <https://github.com/wbolster/jsonlines/pull/54>`_,
    `#51 <https://github.com/wbolster/jsonlines/pull/51>`_)

* 1.2.0, released at 2017-08-17

  * allow ``mode='a'`` in :py:func:`~jsonlines.open()`
    to allow appending to an existing file
    (`#31 <https://github.com/wbolster/jsonlines/pull/31>`_)

* 1.1.3, released at 2017-07-19

  * fix incomplete iteration when given list containing empty strings
    (`#30 <https://github.com/wbolster/jsonlines/issues/30>`_)

* 1.1.2, released at 2017-06-26

  * documentation tweaks
  * enable building universal wheels

* 1.1.1, released at 2017-06-04

  * include licensing information in sdist
    (`#27 <https://github.com/wbolster/jsonlines/issues/27>`_)
  * doc tweaks

* 1.1.0, released at 2016-10-07

  * rename first argument to :py:class:`Reader` since it is not
    required to be a file-like object
  * actually check that the reader/writer is not closed when
    performing operations
  * improved `repr()` output
  * doc tweaks

* 1.0.0, released at 2016-10-05

  * minimum Python versions are Python 3.4+ and Python 2.7+
  * implemented lots of configuration options
  * add proper exceptions handling
  * add proper documentation
  * switch to semver


* 0.0.1, released at 2015-03-02

  * initial release with basic functionality


License
=======

.. include:: ../LICENSE.rst
