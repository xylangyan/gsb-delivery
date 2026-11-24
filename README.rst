pyxlsb
======

|PyPI|

``pyxlsb`` is an Excel 2007-2010 Binary Workbook (xlsb) parser for
Python. The library is currently extremely limited, but functional
enough for basic data extraction.

Install
-------

.. code:: sh

   pip install pyxlsb

Usage
-----

The module exposes an ``open_workbook(name)`` method (similar to Xlrd
and OpenPyXl) for opening XLSB files. The Workbook object representing
the file is returned.

.. code:: python

   from pyxlsb import open_workbook
   with open_workbook('Book1.xlsb') as wb:
       # Do stuff with wb

The Workbook object exposes a ``get_sheet(idx)`` method for retrieving a
Worksheet instance.

.. code:: python

   # Using the sheet index (1-based)
   with wb.get_sheet(1) as sheet:
       # Do stuff with sheet

   # Using the sheet name
   with wb.get_sheet('Sheet1') as sheet:
       # Do stuff with sheet

Tip: A ``sheets`` property containing the sheet names is available on
the Workbook instance.

The ``rows()`` method will hand out an iterator to read the worksheet
rows.

.. code:: python

   # You can use .rows(sparse=True) to skip empty rows
   for row in sheet.rows():
       print(row)
   # [Cell(r=0, c=0, v='TEXT'), Cell(r=0, c=1, v=42.1337)]

Do note that dates will appear as floats. You must use the
``convert_date(date)`` method from the ``pyxlsb`` module to turn them
into ``datetime`` instances.

.. code:: python

   from pyxlsb import convert_date
   print(convert_date(41235.45578))
   # datetime.datetime(2012, 11, 22, 10, 56, 19)

Stored formula reconciliation
------------------------------

Cached cell values are still read with ``rows()`` as above. For comparing
the stored formula *definitions* of two workbook revisions (no external
calculation engine is ever called), ``pyxlsb`` additionally exposes:

.. code:: python

   from pyxlsb import open_workbook, make_spec, reconcile

   # Rectangles are zero-based (r1, c1, r2, c2). Each segment maps a raw
   # rectangle of one workbook onto the same-shaped business rectangle;
   # segments on one side must not overlap on either raw or business side.
   spec = make_spec(
       'Report',                                   # business window sheet
       (10, 2, 13, 2),                             # business window rect
       [('Report', (0, 0, 30, 5), (0, 0, 30, 5))], # old: raw -> business
       [('Report', (20, 0, 33, 5), (0, 0, 13, 5))])# new: moved layout

   with open_workbook('old.xlsb') as old_wb, \
        open_workbook('new.xlsb') as new_wb:
       report = reconcile(old_wb, new_wb, spec)

   report.summary        # counts of equivalent / different / pending
   report.first_difference()
   report.to_dict()      # JSON-serializable detail

Each side's original records are scanned at most once per sheet. Shared
formula groups and array (CSE) formulas stay as compact definitions: they
are materialized per requested cell and never expanded into per-cell
tables, including full-column areas.

Supported formula subset: constants, arithmetic with parentheses,
same-sheet single cell and area references (absolute and relative parts),
and ``SUM`` with any number of cell/area arguments. Reference multiplicity
is preserved, so a cell that is summed twice on one side only matches when
the other side also sums it twice; one area split into two fragments still
matches when the covered business cells are identical. An array formula
that one revision replaced with per-cell calculations is reported as
different, together with the array output members.

Anything outside the subset, or a reference not covered by the supplied
segment list, is returned under ``report.pending`` with a lookup-able
location and reason - such cells are never declared equivalent.

The same check is available from the command line, taking a JSON spec
file with keys ``window``, ``old_segments`` and ``new_segments``:

.. code:: sh

   python -m pyxlsb reconcile old.xlsb new.xlsb spec.json

A worksheet also exposes its formula index and per-cell materialization
directly:

.. code:: python

   with wb.get_sheet('Report') as sheet:
       index = sheet.formula_index()       # one scan, definitions only
       kind, group, ast = sheet.materialize_formula(10, 2)
       # ast can be rendered back to A1 text via pyxlsb.formula.render_a1

.. |PyPI| image:: https://img.shields.io/pypi/v/pyxlsb.svg
   :target: https://pypi.python.org/pypi/pyxlsb
