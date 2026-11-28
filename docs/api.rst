=============
API Reference
=============

Merger
======

.. autoclass:: deepmerge.merger.Merger
    :members:

Strategies
==========

.. automodule:: deepmerge.strategy.core
    :members:

.. automodule:: deepmerge.strategy.dict
    :members:

.. automodule:: deepmerge.strategy.list
    :members:

.. automodule:: deepmerge.strategy.set
    :members:

.. automodule:: deepmerge.strategy.fallback
    :members:

.. automodule:: deepmerge.strategy.type_conflict
    :members:

Exceptions
==========

.. automodule:: deepmerge.exception
    :members:
    :undoc-members:

Planning
========

.. automodule:: deepmerge.trace
    :members: MergePolicy, PathRule, Node, TrackedMerger

.. automodule:: deepmerge.planner
    :members: Packet, Station, ReadEvent, WorkOrder, Plan, Package, RobustPlan,
        IntervalReport, Difference, canonical_value, chapter_runs
