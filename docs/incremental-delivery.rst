======================
 Incremental delivery
======================

.. module:: ZODB.batch

``ZODB.batch`` delivers object graphs from a set of legacy databases into
a new group of ordinary ``FileStorage`` databases, one release at a time.
It is meant for audit hand-offs where batches are signed in separately and
an interrupted delivery must not corrupt previously accepted batches.

A release slip names roots as ``(source database, oid)`` pairs.  The roots
and everything reachable from them through **strong** references are
copied.  References to objects that were not released are replaced by
``None`` and reported in the receipt; they do not block other slips.

``destinations`` may rename a source database or merge several sources
into one destination database.  Identities are still keyed by the source
``(database, oid)`` pair, so a source database with its own oid 17 never
collides with another source's oid 17.

Every source identity keeps exactly one destination identity for the life
of the group.  The destination oid is derived from the source database
name and oid, so a persistent weak reference written by an early slip
starts resolving when a later slip delivers its target without rewriting
the earlier record.

The group
=========

A delivered group is a directory containing one ``FileStorage`` file per
database, the internal ``__delivery__`` database holding the identity
ledger and the receipts, and a ``MANIFEST.json`` naming the current
generation::

    group/
      MANIFEST.json
      IDENTITIES.json
      RECEIPTS.json
      records-3.fs
      materials-3.fs
      __delivery__-3.fs

``IDENTITIES.json`` and ``RECEIPTS.json`` are derived mirrors of the
ledger for auditors.  The authoritative copies are persistent objects in
the ``__delivery__`` database; the mirrors can be deleted and regenerated
by reopening the group.

``MANIFEST.json`` is replaced atomically after a new generation has been
written and fsynced.  A process that stops before the replacement leaves
the previous generation in place.  A process that stops after it leaves
the new generation usable, including its receipt, so a redelivered slip
returns the identities that were already published.

Always resolve database paths through ``MANIFEST.json`` or ``Group.path``.
Generation files that the manifest does not name are not part of the
delivered group, even if they still exist after a crash.

Using the library
=================

Create a :class:`~ZODB.batch.Delivery` with the legacy sources and a
destination directory::

    delivery = Delivery(
        sources={'records': 'legacy/records.fs',
                 'materials': 'legacy/materials.fs'},
        group_dir='delivered',
    )

Release a slip::

    receipt = delivery.release('slip-1', [
        ('records', record_oid),
        ('materials', material_oid),
    ])

The receipt contains ``roots``, ``dependencies`` and ``missing`` maps.
Each identity is a two-element list ``[database, oid hex]``.  Releasing
``slip-1`` again returns the same receipt without changing the group.
Releasing a different slip that names an already delivered root reuses
that root's identity and records it in the new receipt.

The current group is reopened with::

    with delivery.open_group() as group:
        record = group.source('records', record_oid)
        material = group.source('materials', material_oid)

or directly, without a ``Delivery`` object::

    from ZODB.batch import Group

    with Group.open('delivered') as group:
        root = group.connection('records').root()

or with plain ``ZODB`` calls::

    import ZODB
    from ZODB.FileStorage import FileStorage

    database = ZODB.DB(FileStorage('delivered/records-3.fs'))

Use the generation number from ``MANIFEST.json``; the example above names
generation 3 explicitly so it does not silently open an older file after
a later slip has been published.

The connection group preserves object identity across databases, so a
cross-database reference and a weak reference to the same delivered object
return the same Python object.

Auditing
========

``Group.identities()`` returns the source-to-destination mapping as JSON,
``Group.receipt(batch)`` returns one receipt, and ``Group.receipts()``
returns all of them.  ``Group.target(database, oid)`` returns the
``(destination database, destination oid)`` pair for one source object.

An unsatisfied strong reference is recorded twice: in the receipt's
``missing`` map and as a persistent report object under the releasing
root, keyed ``missing/<database>/<oid hex>``.  Its ``links`` value lists
the owner and the missing target.

Crash safety
============

The ``inject`` argument is a test hook.  It is called as
``inject(stage, *details)`` before copying a database
(``before-copy``), after writing it (``after-write``), and around the
manifest replacement (``before-publish``, ``after-publish``).  Raising
from these hooks simulates a process stop at that point.

* A failure before ``before-publish`` discards the unfinished generation
  and leaves the previous group and its receipts intact.
* A failure after ``after-publish`` cannot roll the published generation
  back.  Redelivering the same slip finds its receipt and returns the
  original identities.
* If only some destination databases had been written, the manifest still
  names the previous generation, so the partially written files are not
  readable through the group; the next release rebuilds them.

Unreleased objects are never read: a slip's contents only enter the group
when its roots are passed to :meth:`~ZODB.batch.Delivery.release`.
