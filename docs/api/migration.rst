Migration Verification
======================

The migration verification entry point checks whether two multi-step
indexing plans select the same *cells* of an array for every allowed array
shape, without reading any array data.  This is useful when rewriting an
indexing plan (for example, to target a new storage index) while the source
array is only available in cold storage and its concrete shapes are known
only as finite per-axis length ranges.

Two plans are considered incompatible as soon as the same output position
refers to different original coordinates, even if the stored values would
happen to be equal.

.. autofunction:: ndindex.verify_migration

.. autoclass:: ndindex.IndexPlan
   :members:

.. autoclass:: ndindex.MigrationReport
   :members:
