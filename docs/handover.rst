Handover mode
=============

Handover mode lets two shifts work against the same recorded cassette without
either of them changing the original file, and then hand a minimized,
owner-ID-free cassette to another team.

Everything handover-related lives next to the ordinary cassette in a separate
*handover book* (``<cassette>.handover.yaml`` by default). The book is never
part of the cassette's interactions, so request matchers never see it, and the
original cassette stays a normal VCR file that can still be read directly.

Concepts
--------

Each shift opens the cassette with its own :class:`~vcr.handover.HandoverConfig`,
identified by a ``shift_id``. The book keeps, per shift:

- an independent playback cursor,
- the original occurrences the shift has actually used (each registered once,
  however many times it is replayed, including after ``rewind()``),
- the new interactions the shift has recorded,
- the withdrawals it has observed.

A shift's progress is written as soon as that shift's cassette context exits.
It never waits for the other shift to close.

Owner-ID rules
--------------

Owner IDs can appear in the request (URI, headers, JSON body), the response
(headers, JSON body) and in next-page cursors (a request query parameter or a
JSON field inside a response body). Each location is described by a
:class:`~vcr.handover.Location`.

An :class:`~vcr.handover.OwnerIdRules` object declares:

- replacement *groups*: each original owner ID maps to the values it may be
  replaced with;
- the locations each group rewrites;
- optional individual :class:`~vcr.handover.Permit` permissions for locations
  that have no group rule;
- optional ``group_match_on`` matcher tuples, i.e. the matchers the receiving
  team will use for each group.

.. code-block:: python

    from collections import OrderedDict
    from vcr.handover import HandoverConfig, Location, OwnerIdRules, Permit

    rules = OwnerIdRules(
        groups=OrderedDict([("OW-1", ("NW-7", "NW-8"))]),
        locations={
            Location("request", "uri"): "OW-1",
            Location("response", "header", "X-Owner"): "OW-1",
            Location("cursor", "query", "owner"): "OW-1",
            Location("cursor", "body", "next"): "OW-1",
        },
        permits=(Permit(Location("request", "header", "X-Trace"), "OW-1", "NW-9"),),
        group_match_on={"OW-1": (method, path, query)},
    )

Opening a shift
---------------

Pass the config as ``handover`` to ``use_cassette`` (or ``Cassette.use``). In
handover mode the original cassette file is never written; all progress goes to
the book. A shift that needs to record new pages (continuation recording) opens
with ``record_mode="new_episodes"``; the new pages land in the handover book,
not the original file.

.. code-block:: python

    config_a = HandoverConfig("shift-a", rules, page_turns=("p1", "p2", "p3"))
    with vcr.use_cassette("inbox.yaml", handover=config_a,
                          record_mode="new_episodes") as cassette:
        cassette.play_response(request)
        cassette.rewind()
        cassette.play_response(request)  # same occurrence, registered once

Reopening with the same ``shift_id`` restores that shift's cursor, used
occurrences and recorded pages. Two identical fetches made in the same open
session are two separate pickups and are both kept; re-running the same fetch
after a reopen does not record it again, and pages the other shift has already
submitted are never deleted.

Withdrawals
-----------

The duty officer can withdraw an original occurrence that has already appeared:

.. code-block:: python

    cassette.handover_withdraw(occurrence_index)

The book records exactly which appeared original was pulled. A shift that has
already reached the page keeps its local playback (it can finish its local
replay), but a shift that opens afterwards no longer sees the page, and the
withdrawn occurrence is left out of the issued cassette.

Building the issued cassette
----------------------------

:func:`~vcr.handover.build_handover_cassette` gathers the original occurrences
actually used by any shift (minus withdrawals) plus every shift's recorded
pages, rewrites the owner IDs and writes the cassette for the other team.

The rewrite search:

- never leaves an original owner ID in a declared location,
- rejects any assignment that makes two previously distinct requests match
  under a group's specified matchers, including collisions between surviving
  original pages and newly recorded ones,
- keeps the original occurrence order so each page is still reached in the same
  sequence,
- prefers the assignment using the fewest individual permits and breaks ties by
  declaration (receipt-manifest) order.

If no safe assignment exists, the current shift's progress is saved, the
previous good issued file is kept, and :class:`~vcr.handover.HandoverBuildError`
reports the receiving configuration and the page turn that could not be handed
over safely.

The original cassette is read with the builder's ``serializer``; pass
``target_serializer`` to write the issued cassette in another format (for
example a JSON file produced from a YAML source).

The build is journaled in the book. A process stopped after the book is written
but before the issued cassette is swapped in can simply call the builder again:
it resumes from the last completed step, records nothing twice and does not
remove pages a later shift submitted.

Offline scripts
---------------

:class:`~vcr.handover.OfflineScript` drives a cassette through a finite list of
declared page turns and retry paths. It never opens a socket or reaches an
external service; an action whose page turn was not declared is rejected.
