Upstream Merge Planning
=======================

``deepmerge`` normally merges documents one at a time.  When a transit station
wants to merge several adjacent packets *before* forwarding them, the question
is not whether the merge is syntactically valid, but whether the warehouse
would still see exactly the same values, array order and original packet ids as
the serial work order.

That question has no purely syntactic answer: the built-in strategies are not
associative.  A field may move from a mapping to a list and back, and merging
the last two packets can resurrect fields that the serial order had already
overwritten.  :mod:`deepmerge.planner` decides safety by *executing* the
candidate package against the true serial prefix and comparing the result with
the true serial suffix.

Provenance
==========

:class:`deepmerge.trace.TrackedMerger` mirrors the built-in strategies but
returns a :class:`deepmerge.trace.Node` that records, for every retained leaf,
the original packet that supplied it.  It never aliases input documents, so a
nested template shared by two packaging requests cannot leak edits into the
other result.

.. code-block:: python

    from deepmerge.trace import TrackedMerger

    merger = TrackedMerger()
    node = merger.reduce_documents(
        [{"a": {"x": 1, "y": 2}}, {"a": {"y": 3}}],
        ["P1", "P2"],
    )
    assert node.to_value() == {"a": {"x": 1, "y": 3}}
    assert node.origin_map()[("a", "y")] == ("P2",)

Values produced by ``TrackedMerger`` compare equal to the values produced by
the corresponding :class:`~deepmerge.merger.Merger`; the origin map is an
additional parallel structure.

Work orders
===========

A :class:`deepmerge.planner.WorkOrder` bundles the packets, the candidate
blueprint versions, the declared stations, any receiver reads that must pin a
boundary, and the approved initial configuration.  Each packet carries its
original id, the level-1 station that really received it, and its legal staging
window.

.. code-block:: python

    from deepmerge.planner import Packet, Station, WorkOrder

    order = WorkOrder(
        [
            Packet("P1", "A", (0, 12), {"device": {"id": "d1"}}, permit=1),
            Packet("P2", "A", (1, 13), {"device": ["d2"]}, permit=2),
            Packet("P3", "B", (2, 14), {"device": {"id": "d3"}}, permit=3),
        ],
        {"v1": {}, "v2": {}},
        stations=[Station("A", 1), Station("B", 1), Station("hub", 2)],
        reveal_after=2,
    )

``order.explain(scenario, start, end)`` returns an
:class:`~deepmerge.planner.IntervalReport`.  A rejected candidate names the
serial prefix packet group, the merged packet group, and every path where the
warehouse would see extra or missing values, including the original ids.

``order.plan(scenario)`` returns the send-minimal plan for one published
blueprint.  ``order.robust_plan(scenarios)`` returns the non-anticipatory
plan: a common batch sent before publication, then one continuation per
version.  It chooses the publication cutoff with the smallest worst-case total
send count, breaking ties by dispatch permit.

Each plan is replayed with ``order.verify(plan, scenario)``, which compares the
state after every package arrival, not just the final state.  Equality of
values is not enough: a retained value that lost its true original packet id is
reported as an ``origin`` difference.

Repeated chapters
=================

``chapter_runs`` compresses a long shift into run-length chapters.  It stores
one entry per run, never one entry per alternative grouping.

Command line
============

.. code-block:: bash

    python -m deepmerge.plan examples/work_order.json
    python -m deepmerge.plan examples/work_order.json --json
    python -m deepmerge.demo
