================================================================================
Group a single pass source
================================================================================

A source that can only be scanned once may contain rows for many groups
interleaved across the file.  ``iget_groups`` consumes the source once,
assigns every data row a stable source row number, and writes the grouped
result to a catalog directory.  Downstream processes can then read each
group row by row without the original input.

.. code-block:: python

   >>> import pyexcel as p
   >>> catalog = p.iget_groups(
   ...     "catalog",
   ...     group_by=0,
   ...     file_name="suppliers.csv",
   ... )
   >>> catalog.group_names()
   ['A', 'B', 'C']
   >>> for row in catalog.reader("A"):
   ...     print(row.source_row, row.values)
   ...     row.acknowledge()
   0 ['A', 'first']
   2 ['A', 'second']

The catalog directory contains a SQLite database and a small manifest.  It is
self-contained: once it has been written the source file can be removed.

First seen order
----------------

Groups are reported in the order their first row appeared in the source, not
in sorted order.  Row identity is the source row number, so two rows with
identical content remain two distinct records.  Group names are derived from
the values of the key column.

Delivery, acknowledgement and replay
-------------------------------------

``Catalog.reader(group)`` returns one record at a time.  :meth:`next` returns
``None`` when the group is drained, and a record can be acknowledged either
through ``record.acknowledge()`` or ``reader.acknowledge(record)``.
Acknowledgements only affect the confirmed group and never consume another
group.  Two readers opened at the same time keep independent positions; one
reader acknowledging a row does not make the other reader skip it.

After a process restart, open the catalog with ``open_catalog`` and call
``recover()``:

.. code-block:: python

   >>> catalog = p.open_catalog("catalog")
   >>> report = catalog.recover()
   >>> report.group_names()
   ['A', 'B', 'C']
   >>> report["A"].summary()
   OrderedDict([('acknowledged', [0]), ('pending', [2]), ('unread', [])])

A fresh reader automatically replays rows that were delivered but never
acknowledged, then continues with rows that were never read.  Rows confirmed
in an earlier session are not sent again.  A group that was never opened
starts from its first row.

Incomplete catalogs
-------------------

If the source ended early, for example because the tail of the input was
damaged, the catalog is sealed as incomplete.  ``recover()`` and
``Catalog.reader()`` raise ``pyexcel.exceptions.CatalogIncomplete`` instead of
returning a partial result.  ``Catalog.report()`` still shows which groups and
rows were scanned, and ``Catalog.states()`` gives the counts.

Separate catalogs
-----------------

Every catalog directory uses its own database and manifest.  Source row
numbers are local to a catalog; a row acknowledged in one catalog is rejected
when passed to a reader of another catalog.  Rebuilding the same directory
replaces its contents and its catalog identity.

Large groups
------------

Rows are stored and paged on disk.  ``Catalog.reader()``, ``recover()`` and
``GroupState`` never load a whole group into memory; ``state.counts()`` is a
cheap way to inspect sizes before iterating.

The existing ``iget_array``, ``iget_records`` and ``iget_book`` entry points
are unchanged and keep their on-demand behaviour.
