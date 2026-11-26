Delivery Reconciliation
=======================

Buffered handlers can deliver the same original record more than once.  A
:class:`~logbook.GroupHandler` sends its buffer when the group is popped, so a
group that is bound again after a failure replays the records it had already
received.  A :class:`~logbook.FingersCrossedHandler` escalates the records
leading up to the trigger and then keeps forwarding new ones.  When those
deliveries are replayed under different receiving quotas, the audit questions
are the same:

* Which original numbers were really received?
* How many times was each one received?
* Where did two quota profiles first disagree?
* After a failure, which prefix had been received, and which sources
  re-emitted it later?

This page shows how :mod:`logbook.reconciliation` answers them.  The reference
is in :doc:`api/reconciliation`.

Two quotas
----------

A quota profile gives every receiving tag a balance limit, a starting balance
and a per-category delta table.  The receiver calculates the new balance before
accepting a record.  A negative result is a failure: that record is not
received, while the prefix that was already accepted and the balance it left
behind remain valid.  An accepted record's balance is capped at the limit.
The rest of the batch is still settled; the archive only decides whether the
group may hand over another round.

.. code-block:: python

    from logbook.reconciliation import (
        Candidate,
        DeliveryArchive,
        GroupRound,
        Quota,
        QuotaProfile,
        reconcile,
    )

    offline = QuotaProfile(
        "offline",
        {"mail": Quota(limit=5, initial=4, deltas={"order": 2, "refund": -5})},
    )
    online = QuotaProfile(
        "online",
        {"mail": Quota(limit=10, initial=6, deltas={"order": 2, "refund": -5})},
    )

The archive
-----------

The archive records the rounds exactly as the outer handlers handed them over.
It also says whether a group is used again after a failure, so the simulator
does not invent a continuation rule.

.. code-block:: python

    archive = DeliveryArchive(
        rounds=(
            GroupRound(
                "checkout",
                (
                    Candidate("1001", "order", source="cart"),
                    Candidate("1002", "refund", source="cart"),
                    Candidate("1003", "order", source="cart"),
                ),
                attempt=1,
            ),
            GroupRound(
                "checkout",
                (
                    Candidate("1001", "order", source="retry"),
                    Candidate("1002", "refund", source="retry"),
                    Candidate("1003", "order", source="retry"),
                ),
                attempt=2,
            ),
        ),
        continue_after_failure=True,
    )
    report = reconcile(archive, [offline, online])

Receipt counts
--------------

:meth:`ReconciliationReport.counts` returns the received count per original
number.  :meth:`ReconciliationReport.round_counts` narrows the same answer to a
group, round or attempt.

.. code-block:: python

    report.counts("offline")
    # Counter({'1001': 2, '1002': 1, '1003': 1})
    report.counts("online")
    # Counter({'1001': 2, '1002': 2, '1003': 2})

    report.round_counts(group="checkout", attempt=2)
    # {'offline': Counter({'1001': 1}),
    #  'online': Counter({'1001': 1, '1002': 1, '1003': 1})}

:meth:`ReconciliationReport.first_missing` and
:meth:`ReconciliationReport.first_duplicate` point at the first gap or repeat,
including the group, attempt and source that caused it.

First divergence
----------------

:meth:`ReconciliationReport.first_divergence` returns the first archive
position where the two profiles delivered differently, with each profile's
receiving tag or ``None`` and the number of records it had accepted before
that handover.  This is the actual output position where the two quotas parted
ways.  In the example above, ``offline`` refuses record ``1002`` after
accepting four records, while ``online`` accepts it, so
:meth:`ReconciliationReport.first_missing` reports ``1002`` for ``offline``.

Failure drill-down
------------------

:meth:`ReconciliationReport.failure_detail` opens one failure.  It shows the
received prefix, the refused record and the later receipts for the same
numbers, which are exactly the records a retry re-emits.

Compressed history
------------------

An :class:`~logbook.reconciliation.OperationHistory` stores small operation
segments with a repeat count of up to one billion.  Interval and merged counts
are calculated arithmetically, so the ledger stays much smaller than the
archive it describes.

.. code-block:: python

    from logbook.reconciliation import MAX_REPEAT, Candidate, OperationHistory

    history = OperationHistory()
    history.append(
        [Candidate("1001", "order"), Candidate("1002", "refund")],
        repeat=MAX_REPEAT,
    )
    history.counts_in_range(0, 100)
    # Counter({'1001': 50, '1002': 50})

Command line
------------

For an auditor working from a JSON archive, :func:`~logbook.reconciliation.main`
prints the whole report, including per-round summaries and every profile's
failure list:

.. code-block:: console

    python -m logbook.reconciliation archive.json > report.json

The document has a ``quotas`` mapping from profile name to tag quotas and the
same ``rounds`` shape shown above.  ``tag_order`` fixes the order in which the
output factory selects receiving tags, so both profiles select the same tag.
