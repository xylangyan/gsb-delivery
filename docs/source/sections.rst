Composite-header sections
=========================

One worksheet sometimes holds several tables. Each table starts with a
two-row composite header, and the same visible field path can appear in
more than one table, possibly in more than one physical column.

``pyexcel.sections`` reads such a worksheet without flattening it into a
single ``Sheet``. The caller names each section and gives its two header
rows. Field paths are looked up inside that section only::

    >>> from pyexcel.sections import Section, get_section_records
    >>> sections = [
    ...     Section("first", (0, 1)),
    ...     Section("second", (5, 6)),
    ... ]
    >>> records = get_section_records(
    ...     file_name="prices.xlsx",
    ...     sections=sections,
    ...     fields=["本币/价格", "原币/价格", "原币/税率"],
    ...     selections={"second": {"原币/价格": "J"}},
    ... )

Each record carries its section, its zero-based ``source_row`` and the
physical source of every projected field::

    >>> record = records[0]
    >>> record.section
    'first'
    >>> record.source_row
    2
    >>> record.sources["原币/价格"].cell
    'E3'

Header inheritance
------------------

A merged header value is inherited only by the columns of its own
section. When a merge starts in a hierarchy row of one section, it names
the columns it covers up to the end of that hierarchy row. It never names
the header of a later section, and a merge that starts on a data cell
never replaces that data value or another data cell.

Because inheritance is section-scoped, a path such as ``原币/价格`` can
have one real candidate in one section and two in another. A candidate is
only counted when the section's own labels make the full path, so a
column whose label is inherited from an anchor left of the projection is
still a candidate while an incomplete path is not.

Ambiguity and selection
-----------------------

When a field has several real candidates, ``iget_section_records`` yields
an :class:`~pyexcel.sections.AmbiguousField` event instead of guessing::

    >>> from pyexcel.sections import iget_section_records
    >>> stream = iget_section_records(
    ...     file_name="prices.xlsx",
    ...     sections=sections,
    ...     fields=["原币/价格"],
    ... )
    >>> for event in stream:
    ...     if event.kind == "ambiguous":
    ...         letters = [c.column_letter for c in event.candidates]
    ...         print(event.section, letters)
    ...         stream.select(event.section, event.field, "J")
    ...     elif event.kind == "record":
    ...         print(event.source_row, event["原币/价格"])

A selection names one physical column (letter, zero-based index or a
``Candidate``) and applies to that section only. Selecting a column for
one section never resolves another section's field. A selection outside
the real candidates, or a different column for an already selected
section field, raises :class:`~pyexcel.sections.InvalidSelectionError`.

Records that are already resolved are delivered before the ambiguous
section is reached, so a record can be consumed while a later section is
still unresolved. A record from the ambiguous section is yielded with the
field listed in ``pending_fields``. After ``select``, the stream yields a
:class:`~pyexcel.sections.FieldPatch` for each delivered record. A patch
contains only the newly resolved fields and the original ``source_row``;
no record is sent twice.

``get_section_records`` is the all-at-once form. It raises
:class:`~pyexcel.sections.AmbiguousFieldError` with the same candidates
when a field still needs a selection.

Unmerged control copy
---------------------

``save_unmerged_control`` writes a copy whose section header merges are
removed. The anchor keeps its value and the covered cells stay empty, so
the merged label no longer names the covered columns. Pass
``fill_merged_cells=True`` when the control must keep the same visible
text in every covered cell; each column then carries a literal label
instead of an inherited one. Comparing the two workbooks shows which
discoverable paths and ambiguities really came from header inheritance.
Merges outside the given sections' header bands, including a merge that
crosses into the next section, are left alone.

The older single-section, single-row header behaviour of ``get_sheet``,
``get_records`` and ``iget_records`` is unchanged.
