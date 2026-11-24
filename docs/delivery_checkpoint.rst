##########################
Resumable delivery checkpoint
##########################

``BinLogStreamReader`` can reconnect from the GTIDs it has *read*.  That is
enough to continue a stream, but a warehouse delivery needs a stronger and
separate record: which rows are only visible through an XA ``PREPARE``, which
transactions reached a final result, which target writes were acknowledged,
what range is still missing, and which source can supply the next segment.

``pymysqlreplication.delivery.DeliveryCheckpoint`` provides that record.  It
is independent of a live connection, JSON serialisable, and can be fed either
automatically by the stream reader or explicitly by a test source.

Using the checkpoint

Pass a checkpoint (or its path) to the reader::

    from pymysqlreplication import BinLogStreamReader, DeliveryCheckpoint

    checkpoint = DeliveryCheckpoint(path="delivery.json", default_source="A")
    stream = BinLogStreamReader(
        connection_settings={...},
        server_id=100,
        auto_position="3E11FA47-71CA-11E1-9E33-C80AA9429562:1-100",
        only_events=[WriteRowsEvent],
        delivery_checkpoint=checkpoint,
        delivery_source="A",
    )

The reader's return value is unchanged: callers that only consume stream
events keep working.  The checkpoint additionally records GTID boundaries,
row events and XA lifecycle events.

When switching sources, keep the checkpoint and change ``delivery_source``::

    checkpoint.switch_source("B", resume_gtid="...:101-180",
                             pending_write_ack=last_group_id)

XA semantics

``XA PREPARE`` on source A keeps its rows locally, even if source B has
already discarded that segment.  A later ``XA COMMIT`` on B, under a
different GTID, joins the prepared group by its XA identity and publishes one
complete result.

- ``checkpoint.prepared_only()`` returns groups that are still only prepared.
- ``checkpoint.final_groups()`` returns groups with a commit or rollback.
- ``checkpoint.publish()`` publishes every complete final group exactly once.
- ``checkpoint.verify()`` reports duplicate or incomplete publications,
  pending prepared groups, blocked groups and missing ranges.

A rolled-back group is published with an empty row set.  Reusing an XA name
creates a new group generation; an old target receipt cannot confirm it.

Stream evidence versus target confirmation

Reading a GTID only advances the stream cursor.  It never confirms that the
warehouse accepted a write.  Use ``confirm_target`` with the receipt returned
by the target::

    checkpoint.confirm_target(key=group_id, receipt="write-42")

A receipt is bound to the transaction attempt that produced it.  A pending
write recorded before switching sources stays pending until this call.

Mirror manifests

``checkpoint.apply_manifest(...)`` accepts a source's statement about which
GTIDs belong to the same group.  Two sources reporting the same GTID can
corroborate a group, but a manifest cannot substitute for a target receipt.

A complete manifest may also carry ``prepare_rows`` and then fill a group that
was never seen locally.  A ``tail_only`` manifest can only corroborate: if it
cannot locate the prepare content, the affected transaction stays blocked and
is not published.  Unrelated complete transactions continue to publish, and
existing evidence is preserved.

Missing ranges and resumption

``record_missing_range`` records a range a source could not cover.  The
per-source positions are independent, because the two collectors cover
different binlog ranges::

    plan = checkpoint.resume_plan()
    plan["source"]      # next source
    plan["sources"]     # {source: resumable gtid set}
    plan["missing_ranges"]
    plan["pending_target_acks"]

Persist with ``checkpoint.save()`` or ``autosave=True``; reopen with
``DeliveryCheckpoint.load(path)``.  Reopening never republishes a transaction
that was already published.
