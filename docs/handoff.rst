Reader-pair handoff
===================

The :mod:`aiostream.handoff` module coordinates the replacement of a *pair*
of document readers (a scale-slip reader and an acceptance-slip reader) while
the two daily reading lanes keep running in parallel.

The two readers of a pair are always swapped together, so a new scale slip
can never be stitched onto an old acceptance slip under the same receipt
number. A candidate pair only takes over once it has caught up with the last
receipt number that was actually stored in the warehouse, can produce the
next number on both lanes, and matches the digests recorded on the warehouse
receipts. The warehouse is accessed through idempotent submit/lookup calls so
that a lost acknowledgement can be recovered before a candidate is allowed to
overwrite the corresponding number.

Retiring a pair (superseded candidate, failed reader or job shutdown) closes
its readers exactly once; a late cleanup or an error reported while closing
an old pair never undoes a takeover that already happened.

Example
-------

.. code-block:: python

    from aiostream.handoff import (
        HandoffCoordinator,
        ReaderPair,
        ReceptionLedger,
    )

    ledger = ReceptionLedger(warehouse)
    coordinator = HandoffCoordinator(ledger)

    async for entry in coordinator.run(initial_pair):
        print(entry.receipt_number, entry.reader_pair_id)
        # A later device update can call coordinator.advertise(new_pair);
        # it only takes over once it has proven itself against the ledger.

Reference
---------

.. automodule:: aiostream.handoff
   :members:
