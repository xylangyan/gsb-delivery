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

Segment Rollover
----------------

Phased retirement of segmented MinHash/LSH index generations. A segment is
first *prepared* (its successor locations are planned under an explicit raw
token permit) and then *published* (atomically retired once every historical
group it serves keeps a queryable entry point for every still-valid document
version). Queries run against immutable published views, so routing and
membership always come from the same published version.

.. autoclass:: datasketch.SegmentRolloverManager
    :members:
    :special-members:

.. autoclass:: datasketch.ShardIndex
    :members:
    :special-members:

.. autoclass:: datasketch.DocumentVersion
    :members:
    :special-members:

.. autoclass:: datasketch.TokenConfirmation
    :members:
    :special-members:

.. autoclass:: datasketch.GroupIssuance
    :members:
    :special-members:

.. autoclass:: datasketch.RetirementPlan
    :members:
    :special-members:

.. autoclass:: datasketch.PublishedView
    :members:
    :special-members:

.. autoclass:: datasketch.BlockedEntry
    :members:
    :special-members:

.. autoclass:: datasketch.TokenAccessDeniedError
    :members:
    :special-members:

.. autoclass:: datasketch.RetirementBlockedError
    :members:
    :special-members: