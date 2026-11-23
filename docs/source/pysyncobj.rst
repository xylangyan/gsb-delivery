pysyncobj package
=================

SyncObj
-------

.. autoclass:: pysyncobj.SyncObj
    :members:

replicated
----------

.. autofunction:: pysyncobj.replicated

replicated_sync
---------------
.. autofunction:: pysyncobj.replicated_sync

SyncObjConf
-----------

.. autoclass:: pysyncobj.SyncObjConf
    :members:

FAIL_REASON
-----------

.. autoclass:: pysyncobj.FAIL_REASON
    :members:


SERIALIZER_STATE
----------------

.. autoclass:: pysyncobj.SERIALIZER_STATE
    :members:


SyncObjConsumer
---------------

.. autoclass:: pysyncobj.SyncObjConsumer
    :members:

Consumer identity migration
---------------------------

Snapshots and log entries used to identify consumers by their position in the
``consumers`` list. Reordering that list (or adding a consumer before an
existing one) therefore moved old state to the wrong object.

Give each consumer a stable business identity instead::

    sites = ReplDict()
    failedBatches = ReplDict()

    conf = SyncObjConf(
        # Identities of the old release, in the old registration order.
        legacyConsumers=['sites', 'failedBatches'],
        # Old numeric method IDs and the business call each one meant.
        legacyMethods={
            0: ('__syncobj__', 'addValue'),
            1: ('sites', 'set'),
            2: ('sites', 'pop'),
            3: ('failedBatches', 'set'),
            4: ('failedBatches', 'pop'),
        },
        # Code version at which the identity registry takes over.
        consumerRegistryVersion=1,
    )

    syncObj = SyncObj('serverA:4321', ['serverB:4321'],
                      conf,
                      consumers=[('failedBatches', failedBatches),
                                 ('sites', sites),
                                 ('retryQueue', ReplDict())])

A consumer can also carry its identity directly, either as
``SyncObjConsumer(consumerID='sites')`` or by setting ``consumerID`` on the
instance before creating the ``SyncObj``. ``SyncObjConf.consumerIDs`` accepts
the same identities in the order of the ``consumers`` list.

The migration is designed for a rolling upgrade:

- Before ``consumerRegistryVersion`` is enabled, new nodes keep emitting the
  historical numeric log entries, so old nodes can still apply them.
- Old snapshots are restored strictly by the positions in
  ``legacyConsumers``; new snapshots store state under business identities and
  no longer depend on list positions.
- While the registry is still on the old version, snapshots also contain the
  old positional section followed by the identity map, so old nodes can still
  install them.
- Numeric log entries left in the journal are resolved through
  ``legacyMethods``. A missing or conflicting manifest is reported before any
  snapshot data or log entry is applied.
- After the switch, old snapshots can still be combined with the remaining
  uncompressed tail of the old journal; a snapshot taken after the switch can
  then be used for subsequent restarts.
