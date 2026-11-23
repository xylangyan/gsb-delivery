from __future__ import annotations

import pytest

import pint
from pint.errors import (
    AmbiguousConversionError,
    ArchiveReadOnlyError,
    NoPathError,
    NotContinuousError,
    NotMonotonicError,
    OutsideDomainError,
)
from pint.facets.calibration import (
    Affine,
    CalibrationArchive,
    CalibrationEdge,
    CalibrationGraph,
    CalibrationSegment,
    CalibrationTable,
    Interval,
    PiecewisePolynomial,
    Polynomial,
    UnitDefinitionVersion,
    calibration_archive_from_json,
    calibration_archive_to_json,
    interval_from_quantities,
)


def direct_edge() -> CalibrationEdge:
    """One-step calibration valid on the first part of the raw range."""
    return CalibrationEdge(
        "raw",
        "engineering",
        PiecewisePolynomial.linear((0, 6), 2.0, 0.0),
        name="direct",
    )


def two_step_edges() -> tuple[CalibrationEdge, CalibrationEdge]:
    # The detour is valid on the second half (5.5..10) and disagrees with the
    # direct edge on the overlap 5.5..6.
    return (
        CalibrationEdge(
            "raw",
            "intermediate",
            PiecewisePolynomial.linear((5.5, 10), 1.0, 0.0),
            name="raw-to-intermediate",
        ),
        CalibrationEdge(
            "intermediate",
            "engineering",
            PiecewisePolynomial.linear((5.5, 10), 3.0, 0.0),
            name="intermediate-to-engineering",
        ),
    )


def demo_graph() -> CalibrationGraph:
    return CalibrationGraph(
        (direct_edge(), *two_step_edges()), source="raw", target="engineering"
    )


class TestPolynomial:
    def test_polynomial_evaluation_and_derivative(self):
        poly = Polynomial((1.0, -2.0, 3.0))
        assert poly(2) == 9.0
        assert poly.derivative().coefficients == (-2.0, 6.0)

    def test_polynomial_composition(self):
        outer = Polynomial((1.0, 2.0))
        inner = Polynomial((3.0, 4.0))
        composed = outer.compose(inner)
        assert composed(2) == outer(inner(2))

    def test_affine_roundtrip(self):
        affine = Affine(2.0, 5.0)
        assert affine(3) == 11.0
        assert affine.inverse(11.0) == 3.0


class TestPiecewiseCalibration:
    def test_continuous_piecewise_is_accepted(self):
        curve = PiecewisePolynomial(
            (
                calibration_segment(0, 5, 2.0),
                calibration_segment(5, 10, 2.0),
            )
        )
        assert curve(2) == 4.0
        assert curve(8) == 16.0

    def test_discontinuous_piecewise_is_rejected(self):
        with pytest.raises(NotContinuousError):
            PiecewisePolynomial(
                (
                    calibration_segment(0, 5, 2.0),
                    calibration_segment(5, 10, 3.0),
                )
            )

    def test_non_monotone_segment_is_rejected(self):
        with pytest.raises(NotMonotonicError):
            _non_monotone_segment()

    def test_inverse_roundtrip(self):
        curve = PiecewisePolynomial.linear((0, 10), 2.0, 1.0)
        for value in (0.0, 1.5, 7.0, 10.0):
            assert curve.inverse(curve(value)) == pytest.approx(value, abs=1e-8)


def calibration_segment(lo, hi, slope, intercept=0.0):
    return CalibrationSegment(Interval(lo, hi), Polynomial.linear(slope, intercept))


def _non_monotone_segment():
    return CalibrationSegment(Interval(0, 2), Polynomial((0.0, 1.0, -1.0)))


class TestInterval:
    def test_split_and_hull(self):
        interval = Interval(0, 10)
        pieces = interval.split([2, 5, 8])
        assert [(p.lo, p.hi) for p in pieces] == [(0, 2), (2, 5), (5, 8), (8, 10)]
        assert interval.hull(Interval(20, 30)) == Interval(0, 30)

    def test_empty_intersection(self):
        assert Interval(0, 1).intersection(Interval(2, 3)).is_empty
        assert not Interval(0, 2).intersection(Interval(1, 3)).is_empty


class TestGraphClassification:
    def test_unique_gap_and_ambiguous_ranges(self):
        report = demo_graph().classify((0, 10))
        assert report.status == "mixed"
        assert report.unique == (Interval(0.0, 5.5), Interval(6.0, 10.0))
        assert report.ambiguous == (Interval(5.5, 6.0),)
        assert report.unreachable == ()

    def test_first_part_uses_direct_edge(self):
        graph = demo_graph()
        assert graph.convert(2.0) == 4.0
        assert graph.convert(5.4) == pytest.approx(10.8)

    def test_second_part_uses_detour(self):
        graph = demo_graph()
        assert graph.convert(8.0) == 24.0

    def test_overlap_needs_review_and_lists_both_values(self):
        graph = demo_graph()
        with pytest.raises(AmbiguousConversionError) as exc:
            graph.convert(5.75)
        assert sorted(exc.value.results) == pytest.approx([11.5, 17.25])

        report = graph.classify((5.75, 5.75))
        segment = report.segment_at(5.75)
        assert segment.status == "ambiguous"
        assert [path.label for path in segment.paths] == [
            "raw -[direct]-> engineering",
            "raw -[raw-to-intermediate]-> intermediate"
            " -[intermediate-to-engineering]-> engineering",
        ]

    def test_a_value_where_both_paths_agree_is_unique(self):
        # The two paths are distinct (so both are legitimate) but happen to
        # produce the same value everywhere.
        graph = CalibrationGraph(
            (
                CalibrationEdge(
                    "raw",
                    "engineering",
                    PiecewisePolynomial.linear((0, 5), 2),
                    name="direct",
                ),
                CalibrationEdge(
                    "raw",
                    "intermediate",
                    PiecewisePolynomial.linear((0, 5), 1),
                    name="bypass-first",
                ),
                CalibrationEdge(
                    "intermediate",
                    "engineering",
                    PiecewisePolynomial.linear((0, 5), 2),
                    name="bypass-second",
                ),
            ),
            source="raw",
            target="engineering",
        )
        report = graph.classify((0, 5))
        assert report.unique == (Interval(0, 5),)
        assert report.segment_at(3).paths_by_value[0][1] == tuple(graph.paths())
        assert graph.convert(3) == 6.0

    def test_no_path_raises(self):
        graph = CalibrationGraph(
            (
                CalibrationEdge(
                    "raw", "engineering", PiecewisePolynomial.linear((0, 5), 1)
                ),
            ),
            source="raw",
            target="engineering",
        )
        with pytest.raises(NoPathError):
            graph.convert(7)
        report = graph.classify((4, 8))
        assert report.unreachable == (Interval(5, 8),)
        assert report.unique == (Interval(4, 5),)

    def test_single_edge_outside_domain(self):
        with pytest.raises(OutsideDomainError):
            direct_edge()(7)

    def test_all_paths_can_round_trip(self):
        graph = demo_graph()
        report = graph.classify((0, 10))
        checked = 0
        for coverage in graph.coverages((0, 10)):
            for sample in (
                coverage.interval.lo,
                coverage.interval.midpoint,
                coverage.interval.hi,
            ):
                output = coverage.transform(sample)
                restored = coverage.transform.inverse(output)
                assert restored == pytest.approx(sample, abs=1e-7)
                checked += 1
        assert checked >= 6
        assert report.unique

    def test_ambiguous_inverse(self):
        # A second path whose output range overlaps the direct edge but maps
        # values differently.  An output of 5 is produced at raw=2.5 by the
        # direct edge and at raw=10/3 by the alternate path.
        alternate_first = CalibrationEdge(
            "raw",
            "alternate",
            PiecewisePolynomial.linear((0, 4), 1.0, 0.0),
            name="alternate-first",
        )
        alternate_second = CalibrationEdge(
            "alternate",
            "engineering",
            PiecewisePolynomial.linear((0, 4), 1.5, 0.0),
            name="alternate-second",
        )
        graph = demo_graph()
        graph.extend((alternate_first, alternate_second))
        with pytest.raises(AmbiguousConversionError) as exc:
            graph.invert(5.0)
        assert sorted(exc.value.results) == pytest.approx([2.5, 10 / 3])

    def test_path_with_two_versions_of_same_unit_is_discarded(self):
        first = CalibrationEdge(
            "raw",
            "mid",
            PiecewisePolynomial.linear((0, 10), 1.0),
            name="first",
            unit_versions=(("legacy_scale", "v1"),),
        )
        second = CalibrationEdge(
            "mid",
            "engineering",
            PiecewisePolynomial.linear((0, 10), 1.0),
            name="second",
            unit_versions=(("legacy_scale", "v2"),),
        )
        graph = CalibrationGraph((first, second), source="raw", target="engineering")
        assert graph.paths() == ()
        with pytest.raises(NoPathError):
            graph.convert(1)

    def test_legacy_unit_definition_scale_and_offset(self):
        edge = CalibrationEdge(
            "raw",
            "engineering",
            PiecewisePolynomial.linear((0, 10), 1.0, 0.0),
            scale=1.0,
            offset=273.15,
            name="legacy",
        )
        assert edge(0) == 273.15
        assert edge.inverse(273.15) == pytest.approx(0, abs=1e-8)


class TestArchives:
    def make_archive(self):
        table = CalibrationTable("signed-v1", (direct_edge(),))
        return CalibrationArchive.seal("calibration", (table,), revision=0)

    def test_archive_is_read_only(self):
        archive = self.make_archive()
        assert archive.read_only
        with pytest.raises(ArchiveReadOnlyError):
            archive.revision = 2
        with pytest.raises(ArchiveReadOnlyError):
            archive.tables[0].name = "changed"

    def test_add_table_returns_new_revision_and_verifies(self):
        archive = self.make_archive()
        table = CalibrationTable("signed-v2", two_step_edges(), version="2")
        newer = archive.add_table(table)
        assert newer.revision == 1
        assert archive.revision == 0
        assert newer.verify()
        assert archive.verify()

    def test_archive_json_roundtrip(self):
        archive = self.make_archive()
        restored = calibration_archive_from_json(calibration_archive_to_json(archive))
        assert restored.verify()
        assert restored.signature == archive.signature
        assert restored.tables[0].edges[0].name == "direct"

    def test_registry_seal_keeps_only_new_edges_in_revision(self):
        ureg = pint.UnitRegistry()
        ureg.add_calibration(
            "raw",
            "engineering",
            PiecewisePolynomial.linear((0, 6), 2.0),
            name="direct",
        )
        first = ureg.seal_calibration_archive("v1")
        ureg.add_calibration(
            "raw",
            "intermediate",
            PiecewisePolynomial.linear((5.5, 10), 1.0),
            name="bypass-first",
        )
        ureg.add_calibration(
            "intermediate",
            "engineering",
            PiecewisePolynomial.linear((5.5, 10), 3.0),
            name="bypass-second",
        )
        second = ureg.seal_calibration_archive("v2")
        assert first.revision == 0
        assert second.revision == 1
        assert [table.name for table in second.tables] == ["v1", "v2"]
        assert [edge.name for edge in second.tables[1].edges] == [
            "bypass-first",
            "bypass-second",
        ]
        # The complete history still contains the signed direct edge.
        assert [edge.name for edge in second.edges] == [
            "direct",
            "bypass-first",
            "bypass-second",
        ]
        assert second.verify()

    def test_table_signature_changes_with_edges(self):
        table = CalibrationTable("v1", (direct_edge(),))
        changed = table.add_edge(two_step_edges()[0])
        assert changed.signature != table.signature
        assert changed.verify()

    def test_unit_definitions_are_stamped(self):
        definition = UnitDefinitionVersion("legacy_deg", "1990", 2.0, 10.0)
        table = CalibrationTable("v1", (direct_edge(),), unit_definitions=(definition,))
        archive = CalibrationArchive.seal("cal", (table,))
        assert archive.table("v1").unit_definitions == (definition,)
        assert definition.affine(3) == 16.0


class TestRevalidation:
    def make_pair(self):
        old = CalibrationArchive.seal(
            "calibration", (CalibrationTable("v1", (direct_edge(),)),), revision=0
        )
        bypass = CalibrationTable("bypass", two_step_edges(), version="2")
        new = old.add_table(bypass)
        return old, new

    def test_affected_ranges_are_only_changed_parts(self):
        old, new = self.make_pair()
        report = new.revalidate(old, "raw", "engineering", (0, 10))
        assert report.previous_revision == 0
        assert report.current_revision == 1
        # The direct edge is untouched on 0..5.5, so those results are reused.
        assert report.unchanged == (Interval(0, 5.5),)
        assert Interval(5.5, 6) in report.affected
        assert Interval(6, 10) in report.affected
        assert report.review == (Interval(5.5, 6),)
        assert report.needs_quality_review

    def test_unrelated_range_is_reused(self):
        old, new = self.make_pair()
        report = new.revalidate(old, "raw", "engineering", (100, 200))
        assert report.affected == ()
        assert report.unchanged == (Interval(100, 200),)

    def test_new_agreeing_path_still_needs_quality_review(self):
        duplicate = CalibrationEdge(
            "raw",
            "engineering",
            PiecewisePolynomial.linear((0, 6), 2.0, 0.0),
            name="direct-copy",
        )
        old = CalibrationArchive.seal(
            "calibration", (CalibrationTable("v1", (direct_edge(),)),), revision=0
        )
        new = old.add_table(CalibrationTable("bypass", (duplicate,), version="2"))
        report = new.revalidate(old, "raw", "engineering", (0, 10))
        # The numeric value did not change, so downstream values can be reused.
        assert report.changed_values == ()
        # But the new legal interpretation must be sent to quality review.
        assert report.review == (Interval(0, 6),)
        assert report.affected == (Interval(0, 6),)
        assert "raw -[direct-copy]-> engineering" in report.new_paths

    def test_no_new_edge_means_no_change(self):
        old, new = self.make_pair()
        report = new.revalidate(new, "raw", "engineering", (0, 10))
        assert report.affected == ()
        assert report.unchanged == (Interval(0, 10),)


class TestRegistryIntegration:
    def test_existing_registry_conversion_is_unchanged(self):
        ureg = pint.UnitRegistry()
        assert ureg("1 m").to("cm").magnitude == pytest.approx(100)

    def test_registry_calibration_methods(self):
        ureg = pint.UnitRegistry()
        ureg.add_calibration(
            "raw",
            "engineering",
            PiecewisePolynomial.linear((0, 6), 2.0, 0.0),
            name="direct",
        )
        raw, engineering = two_step_edges()
        ureg.add_calibration(edge=raw)
        ureg.add_calibration(edge=engineering)
        report = ureg.classify_calibration((0, 10), "raw", "engineering")
        assert report.ambiguous == (Interval(5.5, 6.0),)
        assert ureg.convert_calibration(2, "raw", "engineering") == 4.0
        with pytest.raises(AmbiguousConversionError):
            ureg.convert_calibration(5.75, "raw", "engineering")

    def test_registry_archive_workflow(self):
        ureg = pint.UnitRegistry()
        ureg.add_calibration(
            "raw",
            "engineering",
            PiecewisePolynomial.linear((0, 6), 2.0, 0.0),
            name="direct",
        )
        old = ureg.seal_calibration_archive("v1")
        ureg.add_calibration(edge=two_step_edges()[0])
        ureg.add_calibration(edge=two_step_edges()[1])
        new = ureg.seal_calibration_archive("v2")
        report = ureg.revalidate_calibration(
            old, (0, 10), "raw", "engineering", archive=new
        )
        assert report.review
        assert Interval(5.5, 6.0) in report.review

    def test_interval_from_quantities(self):
        ureg = pint.UnitRegistry()
        interval = interval_from_quantities(0 * ureg.m, 100 * ureg.cm)
        assert interval == Interval(0.0, 1.0)

    def test_quantity_interval_classification(self):
        ureg = pint.UnitRegistry()
        ureg.add_calibration(
            "raw",
            "engineering",
            PiecewisePolynomial.linear((0, 6), 2.0),
            name="direct",
        )
        first, second = two_step_edges()
        ureg.add_calibration(edge=first)
        ureg.add_calibration(edge=second)
        rows = ureg.classify_calibration_quantity(
            (0 * ureg.dimensionless, 10 * ureg.dimensionless),
            "raw",
            "engineering",
            unit=ureg.dimensionless,
        )
        statuses = {status for _, status, _ in rows}
        assert statuses == {"unique", "ambiguous"}
        ambiguous = [values for _, status, values in rows if status == "ambiguous"]
        assert sorted(v.magnitude for v in ambiguous[0]) == pytest.approx([11.5, 17.25])
