.. _hyperloglog:

HyperLogLog
===========

HyperLogLog is capable of estimating the cardinality (the number of
distinct values) of dataset in a single pass, using a small and fixed
memory space. HyperLogLog is first introduced in this
`paper <http://algo.inria.fr/flajolet/Publications/FlFuGaMe07.pdf>`__ by
Philippe Flajolet, Éric Fusy, Olivier Gandouet and Frédéric Meunier.

.. code:: python

    from datasketch import HyperLogLog

    data1 = ['hyperloglog', 'is', 'a', 'probabilistic', 'data', 'structure', 'for',
    'estimating', 'the', 'cardinality', 'of', 'dataset', 'dataset', 'a']

    h = HyperLogLog()
    for d in data1:
      h.update(d.encode('utf8'))
    print("Estimated cardinality is", h.count())

    s1 = set(data1)
    print("Actual cardinality is", len(s1))

As in MinHash, you can also control the accuracy of HyperLogLog by
changing the parameter p.

.. code:: python

    # This will give better accuracy than the default setting (8).
    h = HyperLogLog(p=12)

Interestingly, there is no speed penalty for using higher p value.
However the memory usage is exponential to the p value.

.. figure:: /_static/hyperloglog_benchmark.png
   :alt: HyperLogLog Benchmark

As in MinHash, you can also merge two HyperLogLogs to create a union
HyperLogLog.

.. code:: python

    h1 = HyperLogLog()
    h2 = HyperLogLog()
    h1.update('test'.encode('utf8'))
    # The makes h1 the union of h2 and the original h1.
    h1.merge(h2)
    # This will return the cardinality of the union
    h1.count()

.. _hyperloglog_plusplus:

HyperLogLog++
=============

`HyperLogLog++ <http://research.google.com/pubs/pub40671.html>`__ is an
enhanced version of HyperLogLog by Google with the following changes: \*
Use 64-bit hash values instead of the 32-bit used by HyperLogLog \* A
more stable bias correction scheme based on experiments on many datasets
\* Sparse representation (not implemented here)

HyperLogLog++ object shares the same interface as HyperLogLog. So you
can use all the HyperLogLog functions in HyperLogLog++.

.. code:: python

    from datasketch import HyperLogLogPlusPlus

    # Initialize an HyperLogLog++ object.
    hpp = HyperLogLogPlusPlus()
    # Everything else is the same as HyperLogLog

.. _hyperloglog_archive:

Recovering merged registers from archived summaries
====================================================

After a collector switch, shards (fixed sets of events) may be archived at
different HyperLogLog precisions: some shards only have a p=4 register,
others a p=5 register. An archived summary covers a known set of shards and
was built with a known hash version; summaries can overlap, and the original
events are gone.

:func:`recover_merged_registers` merges the summaries for a requested set of
shards and returns the highest-precision register the observations determine
uniquely. Summaries built with different hash versions are never mixed.

.. code:: python

    from datasketch import ArchivedSummary, recover_merged_registers

    # A p=4 summary of shard A and a p=5 summary of shard B.
    summaries = [
        ArchivedSummary("a4", shards=["A"], p=4, reg=reg_a4, hash_version="v1"),
        ArchivedSummary("b5", shards=["B"], p=5, reg=reg_b5, hash_version="v1"),
    ]
    result = recover_merged_registers(summaries, shards=["A", "B"], precision=5)

At p=5, ``result.fixed_mask`` marks the registers that every input consistent
with the observations must share. Where it is ``False``, the register is
ambiguous: :meth:`RecoveryResult.witnesses` gives two register vectors (and
:meth:`RecoveryResult.witness_inputs` two concrete sets of hash values) that
both satisfy every summary but differ at that register, while sharing the
same p=4 register. ``result.determinable_precision`` reports the highest
precision at which the whole register is fixed.

Contradictory observations raise :class:`ArchiveConflictError`; use
:func:`detect_conflicts` to inspect them per hash version, or pass
``on_conflict="report"`` to keep the partially recoverable registers.


