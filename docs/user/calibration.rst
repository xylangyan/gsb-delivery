Calibration
===========

Instrument readings often arrive in different units and must be brought to a
single physical quantity before they are stored.  The calibration facet adds a
small, explicit layer for that workflow:

- each allowed calibration is a piecewise, continuous, strictly monotone
  polynomial;
- each edge declares the input range where it is valid;
- historical unit definitions can be pinned with a scale and offset;
- a whole input interval can be classified as uniquely convertible,
  unreachable, or ambiguous;
- signed archives are read-only, and re-validation reports only the affected
  input ranges.

The facet does not change ordinary unit conversion or contexts.  Use the
methods on ``UnitRegistry`` when you need it.

Piecewise calibrations
----------------------

A calibration segment is a polynomial on a closed input interval::

    >>> import pint
    >>> ureg = pint.UnitRegistry()
    >>> first = pint.CalibrationSegment((0, 5), pint.Polynomial.linear(2, 0))
    >>> second = pint.CalibrationSegment((5, 10), pint.Polynomial.linear(2, 0))
    >>> curve = pint.PiecewisePolynomial((first, second))
    >>> curve(3)
    6.0
    >>> round(curve.inverse(12), 6)
    6.0

Segments must meet at their shared endpoint.  A jump raises
:class:`~pint.errors.NotContinuousError`; a segment whose derivative changes
sign raises :class:`~pint.errors.NotMonotonicError`.  Both checks make the
inverse well defined.

A shortcut is available for a one-segment calibration::

    >>> scale = pint.PiecewisePolynomial.linear((0, 10), 2, 1)

Historical unit definitions
---------------------------

Some calibrations were recorded against an older definition of a unit.  An
edge may carry the historical scale and offset, or a pinned
``UnitDefinitionVersion`` can be stored in the table::

    >>> legacy = pint.CalibrationEdge(
    ...     "raw", "engineering",
    ...     pint.PiecewisePolynomial.linear((0, 10), 1),
    ...     scale=1,
    ...     offset=273.15,
    ...     name="legacy",
    ... )

A path is discarded if it needs two different pinned versions of the same
unit.  This prevents mixing two revisions of one definition inside a single
conversion.

Graphs and paths
----------------

Edges are combined into a :class:`~pint.CalibrationGraph`::

    >>> direct = pint.CalibrationEdge(
    ...     "raw", "engineering",
    ...     pint.PiecewisePolynomial.linear((0, 6), 2),
    ...     name="direct",
    ... )
    >>> to_mid = pint.CalibrationEdge(
    ...     "raw", "intermediate",
    ...     pint.PiecewisePolynomial.linear((5.5, 10), 1),
    ...     name="raw-to-intermediate",
    ... )
    >>> to_end = pint.CalibrationEdge(
    ...     "intermediate", "engineering",
    ...     pint.PiecewisePolynomial.linear((5.5, 10), 3),
    ...     name="intermediate-to-engineering",
    ... )
    >>> graph = pint.CalibrationGraph(
    ...     (direct, to_mid, to_end), source="raw", target="engineering"
    ... )

A single value is converted only when exactly one valid path can handle it::

    >>> graph.convert(2)
    4.0
    >>> graph.convert(8)
    24.0

Where two valid paths disagree, conversion raises rather than choosing the
shortest path::

    >>> graph.convert(5.75)
    Traceback (most recent call last):
    ...
    pint.errors.AmbiguousConversionError: Calibration value 5.75 has multiple results: (11.5, 17.25).

Interval coverage
-----------------

:meth:`CalibrationGraph.classify` answers the batch question.  It splits the
input interval at every change of validity and at every crossing between two
paths::

    >>> report = graph.classify((0, 10))
    >>> report.unique
    (Interval(0.0, 5.5), Interval(6.0, 10.0))
    >>> report.ambiguous
    (Interval(5.5, 6.0),)
    >>> report.unreachable
    ()

Each segment also exposes the inputs and the values produced by each path::

    >>> segment = report.segment_at(5.75)
    >>> segment.status
    'ambiguous'
    >>> segment.values
    (11.5, 17.25)
    >>> [path.label for path in segment.paths]
    ['raw -[direct]-> engineering', 'raw -[raw-to-intermediate]-> intermediate -[intermediate-to-engineering]-> engineering']

Because both paths are invertible, the values can be checked back against the
original input::

    >>> report.segment_at(2).results[0].path.evaluate(2)
    4.0

Archives
--------

A :class:`~pint.CalibrationArchive` is a signed, read-only set of tables.  It
refuses in-place changes::

    >>> table = pint.CalibrationTable("baseline", (direct,))
    >>> archive = pint.CalibrationArchive.seal("instrument-7", (table,))
    >>> archive.revision
    0
    >>> archive.revision = 1
    Traceback (most recent call last):
    ...
    pint.errors.ArchiveReadOnlyError: Calibration archive is read-only (instrument-7).

New calibrations are appended by producing a new archive revision::

    >>> bypass = pint.CalibrationTable("bypass-v2", (to_mid, to_end), version="2")
    >>> newer = archive.add_table(bypass)
    >>> newer.revision
    1
    >>> archive.revision
    0
    >>> newer.verify()
    True

Archives can be serialized with :func:`calibration_archive_to_json` and read
back with :func:`calibration_archive_from_json`; both preserve the signature.
The archive and the registry also offer file helpers::

    >>> archive.save("calibration.json")  # doctest: +SKIP
    >>> same = pint.CalibrationArchive.load("calibration.json")  # doctest: +SKIP
    >>> same.verify()  # doctest: +SKIP
    True
    >>> ureg.save_calibration_archive("calibration.json")  # doctest: +SKIP
    >>> ureg.load_calibration_file("calibration.json")  # doctest: +SKIP

Re-validation
-------------

When a new calibration table is signed, only the input ranges whose
interpretation changed need to be re-read::

    >>> report = newer.revalidate(archive, "raw", "engineering", (0, 10))
    >>> report.affected
    (Interval(5.5, 6.0), Interval(6.0, 10.0))
    >>> report.review
    (Interval(5.5, 6.0),)
    >>> report.needs_quality_review
    True

``affected`` lists ranges whose value or classification changed and ranges
where a new legal interpretation appeared.  ``review`` is the subset that
needs quality review: new ambiguity, or a new valid path even if it agrees
with the previous value.  ``unchanged`` lists ranges that can reuse their
previous result.  Untouched ranges outside the analysis interval are never
re-read.

Using a registry
----------------

The same operations are available on ``UnitRegistry``::

    >>> edge = ureg.add_calibration("raw", "engineering", curve, name="direct")
    >>> edge.name
    'direct'
    >>> report = ureg.classify_calibration((0, 10), "raw", "engineering")
    >>> report.unique
    (Interval(0.0, 5.0), Interval(5.0, 10.0))
    >>> ureg.convert_calibration(3, "raw", "engineering")
    6.0

If a graph is sealed from an archive, the registry methods use that archive;
pass ``archive=`` to analyse an older revision.

Compatibility
-------------

The calibration facet is additive.  It does not alter ``Quantity.to``,
``UnitRegistry.convert`` or the single-active-context behavior of
``ContextRegistry``.  Existing code that converts with one active context keeps
working unchanged.
