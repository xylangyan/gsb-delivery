Receiving-ledger date cleaning
==============================

The date-cleaning helper handles receiving ledgers (收货流水) supplied
as XLSX and CSV. It accepts literal dates directly and accepts
formula-derived dates only when finance supplies a matching confirmation.

Rules
-----

- Only the configured date column is interpreted. Other columns are
  copied unchanged.
- XLSX cells are read in two views: formula text and cached value. The
  cleaner never recalculates a formula.
- A literal XLSX date is accepted when it is a real date cell, or a
  correctly date-formatted serial number.
- A number without a date number format is a plain number and is
  rejected. It does not become a date merely because its value could be
  read as an Excel serial number.
- CSV text must parse as a real calendar date. Text such as
  ``2026-02-30`` is rejected as an invalid calendar date, and ``46000``
  is rejected as a plain number.
- A formula with a cached date remains ``unconfirmed_formula_cache``
  until a confirmation is supplied.
- A formula without a cached result is ``formula_without_cache``. A
  genuinely empty date cell is ``blank_cell``. These are separate
  statuses.

Confirmations
-------------

A confirmation identifies the ledger version, page (sheet), cell address
and cached value::

    {
      "confirmation_id": "fin-2026-01",
      "ledger_version": "sha256:...",
      "sheet": "North",
      "cell": "B3",
      "cached_value": "2026-01-06",
      "shipment_no": "S-N-002"
    }

The ledger version is a content hash of the supplied ledger. A
confirmation from one delivery cannot promote a matching cell in another
delivery, even when the page, cell, formula and cache look the same.
When a page contains two active ledgers on the same address, include
``shipment_no`` to disambiguate. A confirmation that names a
``target_delivery`` or ``target_ledger_version`` is tested against that
supply, and a rejected attempt is recorded against the intended cell.

Applying a later confirmation changes only its target record. Previously
rejected attempts remain in each record's history and in the run's
reconciliation list.

Outputs
-------

``write_xlsx`` writes accepted records with a real ``date`` value and a
``yyyy-mm-dd`` number format in the designated date column.
``write_csv`` writes accepted records with ISO ``YYYY-MM-DD`` text.
Unaccepted records are omitted from both standard outputs.

``write_problem_receipt`` emits JSON with one entry per issue. Each entry
keeps the true source (``formula`` or ``xlsx_literal`` or ``csv_text``),
the formula when present, the raw value and the cached value.

Example
-------

.. code-block:: python

   from pyexcel.date_cleaning import CleaningRun, Confirmation

   run = CleaningRun()
   first = run.add_delivery("delivery-1.xlsx")
   second = run.add_delivery("delivery-2.xlsx")

   stale = Confirmation(
       confirmation_id="batch-one",
       ledger_version=first.version,
       sheet="North",
       cell="B3",
       cached_value="2026-01-06",
       shipment_no="S-N-002",
   )
   print(run.apply_confirmation(stale, target=second).outcome)  # rejected

   follow_up = Confirmation(
       confirmation_id="batch-two",
       ledger_version=second.version,
       sheet="North",
       cell="B3",
       cached_value="2026-01-06",
       shipment_no="S-N-002",
   )
   print(run.apply_confirmation(follow_up, target=second).outcome)  # applied

The same workflow is available on the command line::

    python -m pyexcel.date_cleaning.cli \
        --delivery delivery-1.xlsx \
        --delivery delivery-2.xlsx \
        --delivery control.csv \
        --confirmation confirmations.json \
        --out-dir cleaned
