#  Copyright (c) 2026, Manfred Moitzi
#  License: MIT License
import pytest
import ezdxf
from ezdxf.addons import cut_usage
from ezdxf.math import Vec2
from ezdxf.addons.cut_usage.clipping import Window

from cut_usage_reference import deduplicated_length, draw_then_clip


def make_doc():
    doc = ezdxf.new(setup=True)
    doc.header["$LTSCALE"] = 1.0
    return doc


def test_layer_length_of_a_solid_line():
    doc = make_doc()
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "CUT"})
    msp.add_line((0, 5), (10, 5), dxfattribs={"layer": "ENGRAVE"})
    usage = cut_usage.CutUsage(doc)
    lengths = {entry.layer: entry.length for entry in usage.layer_lengths()}
    assert lengths["CUT"] == 10
    assert lengths["ENGRAVE"] == 10


def test_layer_length_of_a_dashed_line_matches_draw_then_clip():
    doc = make_doc()
    msp = doc.modelspace()
    msp.add_line(
        (0, 0), (10, 0), dxfattribs={"layer": "CUT", "linetype": "DASHED"}
    )
    usage = cut_usage.CutUsage(doc)
    total = usage.layer_length("CUT")
    reference = deduplicated_length(draw_then_clip(usage))
    assert total == reference


def test_layer_length_uses_rational_units():
    doc = make_doc()
    msp = doc.modelspace()
    doc.linetypes.add("RATIONAL", pattern=[1.0, 0.25, -0.25])
    msp.add_line(
        (0, 0), (10, 0), dxfattribs={"layer": "CUT", "linetype": "RATIONAL"}
    )
    usage = cut_usage.CutUsage(doc)
    # every 0.5 unit repeat has a 0.25 dash
    assert usage.layer_length("CUT") == 5


def test_phase_continues_across_polyline_corners():
    doc = make_doc()
    msp = doc.modelspace()
    doc.linetypes.add("SIXFOUR", pattern=[1.0, 0.6, -0.4])
    # leg length 3.7 leaves the phase at 0.7 inside the first gap:
    msp.add_lwpolyline(
        [(0, 0), (3.7, 0), (3.7, 2.7)],
        format="xy",
        dxfattribs={"layer": "POLY", "linetype": "SIXFOUR"},
    )
    usage = cut_usage.CutUsage(doc)
    reference = deduplicated_length(draw_then_clip(usage))
    assert usage.layer_length("POLY") == reference
    strokes = usage.strokes()
    # the first dash of the second leg starts after the remaining gap of the
    # carried phase, not at the corner
    second_leg = [s for s in strokes if abs(s.start.x - 3.7) < 1e-9]
    assert second_leg
    assert float(second_leg[0].start.y) == pytest.approx(0.3)


def test_window_strokes_have_exact_endpoints_and_source():
    doc = make_doc()
    msp = doc.modelspace()
    doc.linetypes.add("HALF", pattern=[1.0, 0.5, -0.5])
    line = msp.add_line(
        (0, 0), (10, 0), dxfattribs={"layer": "CUT", "linetype": "HALF"}
    )
    usage = cut_usage.CutUsage(doc)
    strokes = usage.strokes_in((2, -1), (5, 1))
    assert [tuple(round(v, 6) for v in (s.start.x, s.start.y)) for s in strokes] == [
        (2, 0),
        (3, 0),
        (4, 0),
    ]
    assert all(s.origin == "LINE " + line.dxf.handle for s in strokes)
    assert sum(s.length for s in strokes) == 1.5


def test_window_edge_in_a_gap_reports_no_stroke():
    doc = make_doc()
    msp = doc.modelspace()
    doc.linetypes.add("HALF", pattern=[1.0, 0.5, -0.5])
    msp.add_line(
        (0, 0), (10, 0), dxfattribs={"layer": "CUT", "linetype": "HALF"}
    )
    usage = cut_usage.CutUsage(doc)
    # 1.5 to 2.0 is entirely inside a gap
    strokes = usage.strokes_in((1.6, -1), (1.9, 1))
    assert strokes == []


def test_coincident_strokes_are_cut_once():
    doc = make_doc()
    msp = doc.modelspace()
    for _ in range(2):
        msp.add_line(
            (0, 0), (10, 0), dxfattribs={"layer": "CUT", "linetype": "DASHED"}
        )
    usage = cut_usage.CutUsage(doc)
    single = cut_usage.CutUsage(ezdxf.new(setup=True))
    expected = usage.layer_length("CUT")
    assert expected == deduplicated_length(draw_then_clip(usage))


def test_other_layer_is_not_deduplicated_against_this_layer():
    doc = make_doc()
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "CUT", "linetype": "DASHED"})
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "OTHER", "linetype": "DASHED"})
    usage = cut_usage.CutUsage(doc)
    lengths = {entry.layer: entry.length for entry in usage.layer_lengths()}
    assert lengths["CUT"] == lengths["OTHER"]
    assert lengths["CUT"] > 0


def test_partial_overlap_is_deduplicated_exactly():
    doc = make_doc()
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "CUT"})
    msp.add_line((5, 0), (15, 0), dxfattribs={"layer": "CUT"})
    usage = cut_usage.CutUsage(doc)
    assert usage.layer_length("CUT") == 15


def test_touching_at_one_point_does_not_remove_length():
    doc = make_doc()
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "CUT"})
    msp.add_line((10, 0), (20, 0), dxfattribs={"layer": "CUT"})
    usage = cut_usage.CutUsage(doc)
    assert usage.layer_length("CUT") == 20


if __name__ == "__main__":
    pytest.main([__file__])
