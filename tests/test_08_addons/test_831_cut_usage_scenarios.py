#  Copyright (c) 2026, Manfred Moitzi
#  License: MIT License
"""Tests for the harder cut usage scenarios.

The samples follow the warehouse realities: repeated block inserts with mirroring
and inverted clipping, different linetype periods per layer, coincident support
lines and wide windows with many strokes.
"""
import pytest
import ezdxf
from fractions import Fraction
from ezdxf.addons import cut_usage
from ezdxf.addons.cut_usage.clipping import Window
from ezdxf.math import Matrix44, Vec2

from cut_usage_reference import deduplicated_length, draw_then_clip


def make_doc():
    doc = ezdxf.new(setup=True)
    doc.header["$LTSCALE"] = 1.0
    return doc


def test_repeated_inserts_with_mirror_keep_their_own_phase():
    """Two inserts put the same block geometry on one support line.

    One insert is mirrored and shifted so that both visible lines end up on the
    same axis, but they started at different accumulated distances. Their first
    strokes both begin inside the window but keep different phases.
    """
    doc = make_doc()
    doc.linetypes.add("HALF", pattern=[1.0, 0.5, -0.5])
    block = doc.blocks.new("PART")
    block.add_line(
        (0, 0),
        (4, 0),
        dxfattribs={"layer": "CUT", "linetype": "HALF"},
    )
    block.add_line((0, 0), (0, 4), dxfattribs={"layer": "CUT"})
    msp = doc.modelspace()
    # first insert starts at x=0, second mirrored at x=6 with an offset that puts
    # its horizontal line on the same y=0 support line
    msp.add_blockref("PART", (0, 0))
    mirrored = msp.add_blockref("PART", (6, 0))
    mirrored.dxf.xscale = -1
    usage = cut_usage.CutUsage(doc)
    # the mirrored insert spans x=2..6 and runs right to left, so its dashes do
    # not coincide with the dashes of the first insert in the overlap x=2.5..3.5
    horizontal = [
        stroke
        for stroke in usage.strokes(layer="CUT", include_deduplicated=True)
        if abs(stroke.start.y) < 1e-9 and abs(stroke.end.y) < 1e-9
    ]
    indices = sorted({s.source_index for s in horizontal})
    assert len(indices) == 2
    by_source = {
        index: [s for s in horizontal if s.source_index == index]
        for index in indices
    }
    first, second = by_source[indices[0]], by_source[indices[1]]
    assert first and second
    # the mirrored insert runs right to left: its stroke starts have decreasing x
    second_x = [float(s.start.x) for s in second]
    assert second_x == sorted(second_x, reverse=True)
    overlap = [
        s
        for s in horizontal
        if max(float(s.start.x), float(s.end.x)) >= 2.5
        and min(float(s.start.x), float(s.end.x)) <= 3.5
    ]
    # both inserts contribute to the overlap with different phases: the right to
    # left strokes and the left to right strokes do not fully coincide
    assert len(overlap) == 4
    assert len({s.source_index for s in overlap}) == 2
    assert not any(s.deduplicated for s in overlap)
    lengths = {entry.layer: entry.length for entry in usage.layer_lengths()}
    assert lengths["CUT"] == deduplicated_length(draw_then_clip(usage))


def test_inverted_clip_keeps_the_hole_and_the_phase():
    doc = make_doc()
    doc.linetypes.add("HALF", pattern=[1.0, 0.5, -0.5])
    block = doc.blocks.new("PART")
    block.add_line(
        (0, 0),
        (12, 0),
        dxfattribs={"layer": "CUT", "linetype": "HALF"},
    )
    msp = doc.modelspace()
    insert = msp.add_blockref("PART", (0, 0))
    from ezdxf.xclip import XClip

    clip = XClip(insert)
    clip.set_block_clipping_path([(4, -1), (8, -1), (8, 1), (4, 1)])
    clip.invert_clipping_path(
        extents=[(-2, -2), (14, 2)], ignore_acad_compatibility=True
    )
    usage = cut_usage.CutUsage(doc)
    strokes = usage.strokes()
    # the hole 4..8 is removed, the dashes outside keep their phase
    assert all(not (4 < float(s.start.x) < 8) for s in strokes)
    total = usage.layer_length("CUT")
    expected = deduplicated_length(
        [
            s
            for s in draw_then_clip(usage)
            if not (4 < float(s.start.x) < 8)
        ]
    )
    assert total == expected


def test_periods_differ_per_layer_and_overlap_late():
    """Two layers share a support line but keep separate patterns and lengths."""
    doc = make_doc()
    doc.linetypes.add("A", pattern=[1.0, 0.25, -0.75])
    doc.linetypes.add("B", pattern=[2.0, 0.5, -1.5])
    msp = doc.modelspace()
    msp.add_line((0, 0), (20, 0), dxfattribs={"layer": "L1", "linetype": "A"})
    msp.add_line((0, 0), (20, 0), dxfattribs={"layer": "L2", "linetype": "B"})
    usage = cut_usage.CutUsage(doc)
    lengths = {entry.layer: entry.length for entry in usage.layer_lengths()}
    assert lengths["L1"] == 20 * 0.25
    assert lengths["L2"] == 20 * 0.25
    # both layers are reported separately, the shared line is not merged
    assert len(lengths) == 2


def test_wide_window_with_millions_of_strokes_is_analytic():
    doc = make_doc()
    doc.linetypes.add("TINY", pattern=[0.01, 0.005, -0.005])
    msp = doc.modelspace()
    msp.add_line(
        (0, 0), (100_000, 0), dxfattribs={"layer": "CUT", "linetype": "TINY"}
    )
    usage = cut_usage.CutUsage(doc)
    # 10 million repeats, 5 million strokes, no stroke list is built
    assert usage.layer_length("CUT") == 100_000 * 0.5
    strokes = usage.strokes_in((0, -1), (2, 1))
    assert len(strokes) == 200


def test_window_edge_in_gap_and_on_dash_border():
    doc = make_doc()
    doc.linetypes.add("HALF", pattern=[1.0, 0.5, -0.5])
    msp = doc.modelspace()
    msp.add_line(
        (0, 0), (10, 0), dxfattribs={"layer": "CUT", "linetype": "HALF"}
    )
    usage = cut_usage.CutUsage(doc)
    strokes = usage.strokes_in((0.75, -1), (1.4, 1))
    # 0.75..1.0 is a gap, 1.0..1.4 is a dash
    assert len(strokes) == 1
    assert float(strokes[0].start.x) == pytest.approx(1.0)
    assert float(strokes[0].end.x) == pytest.approx(1.4)


def test_touching_support_lines_at_a_point_add_no_length():
    doc = make_doc()
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "CUT"})
    msp.add_line((10, 0), (10, 10), dxfattribs={"layer": "CUT"})
    msp.add_line((0, 0), (0, 10), dxfattribs={"layer": "CUT"})
    usage = cut_usage.CutUsage(doc)
    assert usage.layer_length("CUT") == 30


def test_insert_sources_can_be_queried_individually():
    doc = make_doc()
    block = doc.blocks.new("PART")
    block.add_line((0, 0), (10, 0), dxfattribs={"layer": "CUT"})
    msp = doc.modelspace()
    first = msp.add_blockref("PART", (0, 0))
    second = msp.add_blockref("PART", (20, 0))
    usage = cut_usage.CutUsage(doc)
    strokes = usage.strokes()
    origins = {stroke.origin for stroke in strokes}
    assert len(origins) == 2
    assert any(first.dxf.handle in stroke.origin for stroke in strokes)
    assert any(second.dxf.handle in stroke.origin for stroke in strokes)
    # the source index is unique per original insert
    assert len({stroke.source_index for stroke in strokes}) == 2


if __name__ == "__main__":
    pytest.main([__file__])


def test_nested_insert_keeps_provenance_and_phase():
    doc = make_doc()
    doc.linetypes.add("HALF", pattern=[1.0, 0.5, -0.5])
    inner = doc.blocks.new("INNER")
    inner.add_line(
        (0, 0), (5, 0), dxfattribs={"layer": "CUT", "linetype": "HALF"}
    )
    outer = doc.blocks.new("OUTER")
    outer.add_blockref("INNER", (0, 0))
    msp = doc.modelspace()
    top = msp.add_blockref("OUTER", (0, 0))
    top.dxf.rotation = 90
    usage = cut_usage.CutUsage(doc)
    strokes = usage.strokes()
    assert strokes
    # the origin names both inserts, the innermost entity is last
    assert all(stroke.origin.count("INSERT") == 1 for stroke in strokes)
    assert all(top.dxf.handle in stroke.origin for stroke in strokes)
    # the phase is unchanged by the rotation: dashes start at the source origin
    first = min(strokes, key=lambda s: float(s.start.y))
    assert float(first.start.y) == pytest.approx(0.0, abs=1e-9)


def test_multi_insert_grid_repeats_the_block():
    doc = make_doc()
    block = doc.blocks.new("PART")
    block.add_line((0, 0), (2, 0), dxfattribs={"layer": "CUT"})
    msp = doc.modelspace()
    insert = msp.add_blockref("PART", (0, 0))
    insert.dxf.row_count = 2
    insert.dxf.row_spacing = 5
    insert.dxf.column_count = 2
    insert.dxf.column_spacing = 3
    usage = cut_usage.CutUsage(doc)
    assert usage.layer_length("CUT") == 8
    assert len(usage.strokes()) == 4


def test_disabled_clipping_reports_the_full_length():
    doc = make_doc()
    block = doc.blocks.new("PART")
    block.add_line((0, 0), (10, 0), dxfattribs={"layer": "CUT"})
    msp = doc.modelspace()
    insert = msp.add_blockref("PART", (0, 0))
    from ezdxf.xclip import XClip

    clip = XClip(insert)
    clip.set_block_clipping_path([(2, -1), (5, -1), (5, 1), (2, 1)])
    assert cut_usage.CutUsage(doc).layer_length("CUT") == 3
    clip.disable_clipping()
    assert cut_usage.CutUsage(doc).layer_length("CUT") == 10


def test_viewport_layer_override_changes_the_pattern():
    doc = make_doc()
    doc.layers.add("CUT", linetype="DASHED")
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "CUT"})
    psp = doc.layouts.new("Sheet")
    viewport = psp.add_viewport(
        center=(5, 5), size=(10, 10), view_center_point=(5, 0), view_height=10
    )
    layer = doc.layers.get("CUT")
    overrides = layer.get_vp_overrides()
    overrides.set_linetype(viewport.dxf.handle, "CONTINUOUS")
    overrides.commit()
    usage = cut_usage.CutUsage(doc, doc.modelspace(), viewport=viewport)
    assert usage.layer_length("CUT") == 10
    # outside of the viewport the layer linetype is still DASHED
    assert cut_usage.CutUsage(doc).layer_length("CUT") < 10


def test_viewport_maps_modelspace_to_paperspace():
    doc = make_doc()
    doc.linetypes.add("HALF", pattern=[1.0, 0.5, -0.5])
    msp = doc.modelspace()
    msp.add_line(
        (0, 0), (100, 0), dxfattribs={"layer": "CUT", "linetype": "HALF"}
    )
    psp = doc.layouts.new("Sheet")
    viewport = psp.add_viewport(
        center=(50, 50), size=(100, 100), view_center_point=(50, 0), view_height=50
    )
    usage = cut_usage.CutUsage(doc, doc.modelspace(), viewport=viewport)
    # the cut length stays in drawing units
    assert usage.layer_length("CUT") == 25
    strokes = usage.strokes()
    assert len(strokes) == 50
    # a paper dash is twice the drawing length, but reports the drawing length
    assert float(strokes[0].end.x - strokes[0].start.x) == pytest.approx(1.0)
    assert float(strokes[0].length) == pytest.approx(0.5)


def test_two_sources_with_different_phase_on_one_support_line():
    doc = make_doc()
    doc.linetypes.add("HALF", pattern=[1.0, 0.5, -0.5])
    msp = doc.modelspace()
    # both paths start well before the window and end after it, so their first
    # visible dashes both begin inside the window but at different phases
    msp.add_line(
        (-10.1, 0), (10, 0), dxfattribs={"layer": "CUT", "linetype": "HALF"}
    )
    msp.add_line(
        (-7.3, 0), (13, 0), dxfattribs={"layer": "CUT", "linetype": "HALF"}
    )
    usage = cut_usage.CutUsage(doc)
    strokes = usage.strokes_in((0, -1), (2, 1), layer="CUT")
    intervals = sorted(
        (round(float(s.start.x), 6), round(float(s.end.x), 6)) for s in strokes
    )
    assert intervals == [
        (0.0, 0.4),
        (0.7, 0.9),
        (0.9, 1.4),
        (1.7, 1.9),
        (1.9, 2.0),
    ]
    assert float(sum(s.length for s in strokes)) == pytest.approx(1.4)


def test_mirrored_insert_and_inverted_clip_on_one_support_line():
    doc = make_doc()
    doc.linetypes.add("P1", pattern=[1.0, 0.4, -0.6])
    block = doc.blocks.new("PART")
    block.add_line(
        (0, 0), (10, 0), dxfattribs={"layer": "CUT", "linetype": "P1"}
    )
    msp = doc.modelspace()
    first = msp.add_blockref("PART", (0, 0))
    mirrored = msp.add_blockref("PART", (20, 0))
    mirrored.dxf.xscale = -1
    from ezdxf.xclip import XClip

    clip_first = XClip(first)
    clip_first.set_block_clipping_path([(3, -1), (12, -1), (12, 1), (3, 1)])
    clip_mirrored = XClip(mirrored)
    clip_mirrored.set_block_clipping_path([(12, -1), (17, -1), (17, 1), (12, 1)])
    clip_mirrored.invert_clipping_path(
        extents=[(5, -2), (25, 2)], ignore_acad_compatibility=True
    )
    usage = cut_usage.CutUsage(doc)
    # both visible pieces are on y=0
    assert len(usage.groups) == 1
    group = usage.groups[0]
    assert len(group.pieces) == 2
    # the mirrored piece runs opposite to the canonical support direction
    assert sorted(piece.axis_forward for piece in group.pieces) == [False, True]
    # their phases differ, so the mirrored dashes do not coincide with the first
    strokes = usage.strokes()
    first_source = [s for s in strokes if s.source_index == 0]
    mirrored_source = [s for s in strokes if s.source_index == 1]
    assert first_source and mirrored_source
    first_starts = {round(float(s.start.x), 6) for s in first_source}
    mirrored_starts = {round(float(s.start.x), 6) for s in mirrored_source}
    assert first_starts.isdisjoint(mirrored_starts)
    # each retained its own phase, nothing was dropped
    assert len(strokes) == len(first_source) + len(mirrored_source)
    assert all(not s.deduplicated for s in strokes)
    assert usage.layer_length("CUT") == deduplicated_length(
        draw_then_clip(usage)
    )


def test_one_layer_with_two_patterns_uses_the_common_repeat():
    doc = make_doc()
    doc.linetypes.add("A", pattern=[1.0, 0.3, -0.7])
    doc.linetypes.add("B", pattern=[2.0, 0.7, -1.3])
    msp = doc.modelspace()
    msp.add_line(
        (0, 0), (1_000_000, 0), dxfattribs={"layer": "CUT", "linetype": "A"}
    )
    msp.add_line(
        (0, 0), (1_000_000, 0), dxfattribs={"layer": "CUT", "linetype": "B"}
    )
    usage = cut_usage.CutUsage(doc)
    # period A is 1 with a 0.3 dash; period B is 2 with a 0.7 dash.
    # inside one common repeat of 2: [0,0.7] and [1,1.3] -> 1.0
    assert usage.layer_length("CUT") == Fraction(500_000)
    # the common repeat is not laid out stroke by stroke
    assert usage.groups[0].measure() == Fraction(500_000)
