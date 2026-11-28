Shift reconciliation
====================

.. versionadded:: 0.6.1

Two consecutive shifts often run almost identical cleaning work orders, but
rebuild temporary tables under the same names and point them at different
physical raw material. The ``sqlparse.reconcile`` package decides which
already-delivered report pages can be reused and which must be redone with
the current shift's data.

The analysis is static and never contacts a real database. The supported
SQLite subset covers ``CREATE [TEMP] TABLE`` (plain and ``AS SELECT``),
``INSERT ... SELECT``, single-level ordinary ``WITH`` queries, simple joins
with ``ON`` / ``USING`` and column projections, plus ``SAVEPOINT`` /
``ROLLBACK TO SAVEPOINT`` / ``RELEASE SAVEPOINT``. Anything outside the
subset raises :class:`sqlparse.reconcile.ReconcileError`.

Lineage model
-------------

* Every external table has a fixed physical *instance* per shift. Read
  pieces carry :class:`~sqlparse.reconcile.RowMeta` with the original row
  number and primary key, so one original row is recognised across reads.
* Each ``CREATE`` / ``INSERT`` produces an immutable *generation* of a
  temporary table; rollback restores the previous generation while the
  older one stays reachable for archived pages.
* A same-named ``WITH`` CTE shadows the temporary table only inside that
  statement and never replaces an existing generation.
* A ``JOIN ... USING`` column shows one value but keeps both physical
  sources in its :class:`~sqlparse.reconcile.ColumnRef`.
* Sealed read-only generations handed in with the work order cannot be
  replaced by a same-named variable result.

The receiving end may provide several finite-value *alternatives* for each
external table. One alternative label applies to every read of one table,
so an original row always keeps one interpretation. The reconciler picks
the alternative that preserves the delivered display values, row order and
sources at the minimum read bytes and fees; a read shared by several pages
is billed once. When an alternative changes a result, the report names the
variant, the physical table instance, the column and the original row.

Work-order manifest
-------------------

A manifest is a JSON document referencing the previous and current SQL
scripts, the external table metadata and the allowed sealed generations::

    {
      "previous_file": "previous_shift.sql",
      "current_file": "current_shift.sql",
      "externals": {
        "orders": {
          "instance": "orders-v3",
          "columns": ["oid", "cid", "amount"],
          "bytes": 800,
          "fee": 6.0,
          "rows": [[101, 1, 9.5]],
          "row_meta": [{"line_no": 30, "pk": 101}],
          "variants": {"draft": [[101, 1, 0.0]]},
          "variant_costs": {"draft": [420, 3.5]},
          "previous_instance": "orders-v2",
          "previous_rows": [[101, 1, 9.5]]
        }
      },
      "allowed_seals": [{"name": "joined", "generation": "joined#2"}]
    }

Command line
------------

Run the reconciliation through the existing entry point::

    python -m sqlparse --reconcile work_order.json
    python -m sqlparse --reconcile work_order.json --reconcile-format json

Python API
----------

.. autoclass:: sqlparse.reconcile.Reconciler
   :members:

.. autoclass:: sqlparse.reconcile.ReconciliationResult
   :members:

.. autoclass:: sqlparse.reconcile.ExternalVersion
   :members:

.. autoclass:: sqlparse.reconcile.ReconcileError
   :members:

.. autofunction:: sqlparse.reconcile.load_work_order
