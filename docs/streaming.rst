`pycldf.streaming`
==================

The :class:`pycldf.streaming.BatchExporter` complements the regular ORM
(:mod:`pycldf.orm`) for exports of large tables:

* every table file is opened only once and read sequentially, advancing just
  far enough to locate the references needed by the current batch,
* references returned before a target table is fully read are marked
  :attr:`~pycldf.streaming.RefStatus.UNIQUE` ("unique so far") instead of being
  treated as definite,
* duplicate keys encountered later retract every earlier reference using that
  key (scoped per target table and per key domain: CLDF id vs. local primary
  key), reported together with the affected source records and reference paths,
* missing keys yield :attr:`~pycldf.streaming.RefStatus.NOT_FOUND`, blank
  references yield :attr:`~pycldf.streaming.RefStatus.EMPTY` without opening the
  target file, and conflict-free results match the regular ORM queries after
  :meth:`~pycldf.streaming.BatchExporter.finalize`.

.. automodule:: pycldf.streaming
   :members:
   :exclude-members: RefStatus
