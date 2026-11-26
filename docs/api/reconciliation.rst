Delivery Reconciliation
=======================

.. module:: logbook.reconciliation

Buffered handlers such as :class:`~logbook.GroupHandler` and
:class:`~logbook.FingersCrossedHandler` can hand the same original record to
an inner handler more than once.  This module models the receiving side of
that traffic under one or more quota profiles and answers the questions an
auditor asks: which original numbers were delivered, how often, where the
runs first disagree, and what a failed attempt had already received.

The archive
-----------

A :class:`DeliveryArchive` is the recorded sequence of group rounds.  Each
:class:`GroupRound` carries the original :class:`Candidate` records in the
order the group handed them over.  A candidate's ``number`` identifies the
original record and its ``category`` selects the balance delta.  The archive's
``continue_after_failure`` setting says whether an outer group is used again
after a failure; a round may override it.  No separate continuation rule is
invented.  A refusal never stops the rest of the current batch: later records
are still settled, while the rule only decides whether the group may hand over
another round.

Quota profiles
--------------

A :class:`QuotaProfile` maps tag names to :class:`Quota` objects.  Each quota
has a balance limit, a starting balance and a per-category delta table.  The
simulator calculates the new balance before accepting a record: a negative
result raises :class:`QuotaExhausted` and the record is not received, while an
accepted record's balance is capped at the limit.  The received prefix and its
balance remain valid.

Starting a new output does not replenish a tag.  If the original output factory
reuses the tag it selected first, that tag's remaining balance continues.  A
repeated original record is settled again whenever a group or window hands it
over, because only its number and category matter.

.. autoclass:: Quota
   :members:

.. autoclass:: QuotaProfile
   :members:

.. autoclass:: Candidate
   :members:

.. autoclass:: GroupRound
   :members:

.. autoclass:: DeliveryArchive
   :members:

Simulation
----------

.. autoclass:: DeliverySimulator
   :members:

.. autofunction:: simulate

.. autoclass:: ProfileResult
   :members:

.. autoclass:: Receipt
   :members:

.. autoclass:: DeliveryFailure
   :members:

Reconciliation
--------------

.. autofunction:: reconcile

.. autoclass:: ReconciliationReport
   :members:

.. autoclass:: Divergence
   :members:

.. autoclass:: FailureDetail
   :members:

.. autoclass:: SequenceIssue
   :members:

.. autoclass:: RoundSummary
   :members:

Compressed history
------------------

A :class:`HistorySegment` holds a small operation segment and a repeat count,
so a segment can stand in for up to one billion repetitions.  An
:class:`OperationHistory` concatenates segments and answers interval and merged
counts arithmetically, without expanding the repetitions.

.. autoclass:: HistorySegment
   :members:

.. autoclass:: OperationHistory
   :members:

.. autoclass:: HistoryWalk
   :members:

.. autofunction:: merged_counts

Comparing with the real handlers
--------------------------------

:class:`QuotaReceiver` is a handler that applies a quota profile as records
arrive.  Put it inside a real :class:`~logbook.GroupHandler` or
:class:`~logbook.FingersCrossedHandler` to capture the handler's batching,
escalation and replay behavior.  :class:`BatchRecorder` records the batches and
reasons a handler produced so they can be turned back into a
:class:`DeliveryArchive`.

.. autoclass:: QuotaReceiver
   :members:

.. autoclass:: Received
   :members:

.. autoclass:: Rejected
   :members:

.. autoclass:: BatchRecorder
   :members:

.. autoclass:: RecordBatch
   :members:

JSON and the command line
-------------------------

.. autofunction:: archive_from_dict

.. autofunction:: profile_from_dict

.. autofunction:: main
