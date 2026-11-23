Indexed interval subsequence matching
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:class:`dtaidistance.subsequence.subsequenceindex.SubsequenceIndex` indexes
one fixed query template against one long archive series.  It answers
repeated questions of the form "find the best occurrence of the template
inside archive interval ``[begin, end]``" without rescanning the archive.

The distance is the ordinary one-dimensional DTW distance with
``penalty=0`` and squared-Euclidean local cost, exactly as computed by
:func:`dtaidistance.dtw.warping_paths`.  The result is exact, not a
lower-bound filter.

.. code-block:: python

    from dtaidistance.subsequence.subsequenceindex import SubsequenceIndex

    index = SubsequenceIndex(query, series, sequence_numbers=acquisition_ids)
    match = index.query(begin=1000, end=9000)
    print(match.distance, match.begin, match.end, match.path)

A match may not borrow archive points from outside the requested interval.
Consequently, if a full-archive optimum crosses the left boundary of an
interval, the index does not reuse the truncated path; it finds the best
path wholly inside the interval.

Ties are resolved by distance, then by the earliest match end, then by the
earliest match start.  ``match.path`` is materialised lazily and only scans
the matched window.

``SubsequenceIndex.update(position, value)`` corrects one archived point in
place.  Only the affected leaf block and the path to the segment-tree root
are recomputed, so the cost does not depend on the archive length.
``correct_sequence_number`` updates the non-distance original acquisition
number track, which may contain duplicate values and is always read in the
supplied order.

The index stores O(number_of_blocks * template_length^2) values.  With a
fixed template (at most 32 points) this is linear in the archive length.
