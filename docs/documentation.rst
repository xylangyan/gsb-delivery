API Documentation
=================


MinHash
-------

.. autoclass:: datasketch.MinHash
    :members:
    :special-members:

Lean MinHash
------------

.. autoclass:: datasketch.LeanMinHash
    :members:
    :special-members:

b-bit MinHash
-------------

.. autoclass:: datasketch.bBitMinHash
    :members:
    :special-members:


Weighted MinHash
----------------

.. autoclass:: datasketch.WeightedMinHashGenerator
    :members:
    :special-members:

.. autoclass:: datasketch.WeightedMinHash
    :members:
    :special-members:

MinHash LSH
-----------

.. autoclass:: datasketch.MinHashLSH
    :members:
    :special-members:

Asynchronous MinHash LSH
------------------------

.. autoclass:: datasketch.aio.AsyncMinHashLSH
    :members:
    :special-members:

LSHBloom
------------------------

.. autoclass:: datasketch.MinHashLSHBloom
    :members:
    :special-members:

MinHash LSH Forest
------------------

.. autoclass:: datasketch.MinHashLSHForest
    :members:
    :special-members:

MinHash LSH Ensemble
--------------------

.. autoclass:: datasketch.MinHashLSHEnsemble
    :members:
    :special-members:

HyperLogLog
-----------

.. autoclass:: datasketch.HyperLogLog
    :members:
    :special-members:

HyperLogLog++
-------------

.. autoclass:: datasketch.HyperLogLogPlusPlus
    :members:
    :special-members:


HNSW
----

.. autoclass:: datasketch.HNSW
    :members:
    :special-members:


HNSW Merge Replay
-----------------

Two branch jobs can append to copies of the same base HNSW index while an
:class:`~datasketch.hnsw_merge.OperationRecorder` captures the structural
evidence each operation actually observed. At merge time
:func:`~datasketch.hnsw_merge.replay_operation_log` reuses an operation's
committed graph changes and random-state advancement without any distance
evaluation while its evidence still holds, and otherwise re-executes it from
the original entry point. :func:`~datasketch.hnsw_merge.full_replay` is the
all-distance reference path whose result must match.

.. automodule:: datasketch.hnsw_merge
    :members:
    :special-members: