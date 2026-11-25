mdfreader.revision — MDF4 invalidation-bit revision
====================================================

This subpackage supports a two-stage archive workflow for reopening selected
samples of MDF4 channels that were wholesale marked invalid.

* **Planning** consumes only a work-order *manifest* (channel descriptions plus
  per-sample invalidation offsets).  The original volume's data blocks stay
  closed and the minimum set of physical data blocks to retransmit is computed.
* **Revision** opens the original volume after the plan is accepted and writes
  a revised volume that reuses every unselected block byte-for-byte.

Only approved samples regain validity.  Unapproved samples, missing samples of
other channels, raw signal values, conversions and master channels are all
preserved.

Manifest model
--------------

A manifest is a JSON object with a physical ``block_order`` and up to 80
``groups``.  Each group has a stable ``group_id``, a ``layout``
(``interleaved`` for mixed record-id groups or ``column`` for a single DV/DI
pair) and one or two repairable channels.  Every channel lists up to two
candidate bits and, per in-group sample:

* the sample's in-group sequence number,
* the original physical block id and pointer,
* the invalidation byte offset in the decompressed / record-laid-out buffer
  (column groups point into the companion DI block),
* the original value of every candidate bit,
* whether the sample is approved,
* and which existing channels already occupy each candidate bit.

Validity normalisation
----------------------

When a channel is assigned a candidate bit, the physical bit is normalised
across the whole channel group: cleared (``0``) for approved samples and set
(``1``) elsewhere.  A data block is billed only when at least one of its bytes
actually changes, so a bit that already matches the target pattern costs no
retransmission at all.

* A bit occupied by an existing channel may be selected only if normalisation
  leaves its physical pattern unchanged.
* Two work-order channels may share one bit only when their target patterns are
  identical; the shared byte is then edited once and both channels reread with
  the correct per-sample validity.
* Each channel picks exactly one candidate bit and the choice is constant for
  every record of its group.

Minimum-block planning
-----------------------

A group's coverage runs from the first to the last physical block containing
its candidate bits.  The archive guarantees any block lies in at most three
coverage intervals.  :func:`~mdfreader.revision.plan_revision` sweeps the
physical block order with a dynamic program whose frontier holds the active
groups (at most three) and all of their legal bit options, returning the global
minimum number of distinct changed blocks even when groups chain into a single
long segment.  When no legal arrangement exists it raises
:class:`~mdfreader.revision.PlanningConflict` identifying the conflicting group,
channels and samples.

Building the revised volume
---------------------------

:func:`~mdfreader.revision.build_revised_volume` walks the block graph from the
header block, relocates every cross-block link and:

* rebuilds plain DT/DV/DI payloads in place,
* decompresses, patches and recompresses DZ blocks with the same codec,
* reuses every other data block byte-for-byte (block addresses may change),
* updates the chosen CN blocks' flags (``CN_F_ALL_INVALID`` cleared, the
  invalidation-bit flag set) and their invalidation bit position.

Channel flag updates and remapped links are delivered separately from the
retransmitted data-block billing.

API
---

.. code-block:: python

   from mdfreader.revision import run_revision

   result = run_revision(
       manifest_source="work_order.json",
       source_volume="original.mf4",
       output_volume="revised.mf4",
       output_dir="delivery/",
   )
   if result["status"] == "conflict":
       print(result["conflict"])          # groups/channels/samples to report
   else:
       print(result["report"]["retransmit_blocks"])
       print(result["build_summary"]["channel_flag_updates"])

Use :func:`~mdfreader.revision.run_planning` for the planning-only stage,
:func:`~mdfreader.revision.parse_manifest` for validation and
:func:`~mdfreader.revision.conflict_report` /
:func:`~mdfreader.revision.delivery_report` for serialisable artefacts.

.. automodule:: mdfreader.revision
   :members:
   :undoc-members:
   :show-inheritance:
