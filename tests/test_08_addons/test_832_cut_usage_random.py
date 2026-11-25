#  Copyright (c) 2026, Manfred Moitzi
#  License: MIT License
"""Randomized verification of the analytic totals against draw-then-clip.

Each generated drawing is short, so the reference can draw every source path
completely and clip the drawn strokes. The engine totals must match the exact
union length of the clipped reference strokes per layer.
"""
from __future__ import annotations

import random
from fractions import Fraction

import ezdxf
import pytest

from ezdxf.addons import cut_usage
from ezdxf.addons.cut_usage.geometry import support_line, to_fraction
from ezdxf.math import Vec2

from cut_usage_reference import deduplicated_length, draw_then_clip


PATTERNS = {
    "A": [1.0, 0.5, -0.5],
    "B": [1.0, 0.6, -0.4],
    "C": [2.0, 0.75, -0.5, 0.25, -0.5],
    "SOLID": None,
}


def build_doc(seed: int):
    rng = random.Random(seed)
    doc = ezdxf.new(setup=True)
    doc.header["$LTSCALE"] = 1.0
    for name, pattern in PATTERNS.items():
        if pattern is not None:
            doc.linetypes.add(name, pattern=pattern)
    block = doc.blocks.new("PART")
    for _ in range(rng.randint(1, 3)):
        pattern = rng.choice(list(PATTERNS))
        block.add_line(
            (rng.uniform(-2, 2), rng.uniform(-2, 2)),
            (rng.uniform(5, 12), rng.uniform(-2, 2)),
            dxfattribs={"layer": "CUT", "linetype": pattern},
        )
    msp = doc.modelspace()
    for _ in range(rng.randint(1, 3)):
        x = rng.uniform(0, 10)
        y = rng.uniform(0, 10)
        layer = rng.choice(["CUT", "ENGRAVE"])
        pattern = rng.choice(list(PATTERNS))
        msp.add_line(
            (x, y),
            (x + rng.uniform(2, 15), y + rng.uniform(-1, 1)),
            dxfattribs={"layer": layer, "linetype": pattern},
        )
    for _ in range(rng.randint(1, 3)):
        insert = msp.add_blockref(
            "PART",
            (rng.uniform(-5, 5), rng.uniform(-5, 5)),
            dxfattribs={"layer": rng.choice(["CUT", "ENGRAVE"])},
        )
        if rng.random() < 0.5:
            insert.dxf.rotation = rng.uniform(0, 360)
        if rng.random() < 0.3:
            insert.dxf.xscale = -1
    return doc


@pytest.mark.parametrize("seed", range(40))
def test_analytic_totals_match_draw_then_clip(seed):
    doc = build_doc(seed)
    usage = cut_usage.CutUsage(doc)
    reference = deduplicated_length(draw_then_clip(usage))
    engine = sum((entry.length for entry in usage.layer_lengths()), Fraction(0))
    assert float(engine) == pytest.approx(float(reference), abs=1e-9)


@pytest.mark.parametrize("seed", range(40))
def test_layer_totals_match_draw_then_clip_per_layer(seed):
    doc = build_doc(seed)
    usage = cut_usage.CutUsage(doc)
    strokes = draw_then_clip(usage)
    for entry in usage.layer_lengths():
        reference = deduplicated_length(
            [stroke for stroke in strokes if stroke.layer == entry.layer]
        )
        assert float(entry.length) == pytest.approx(float(reference), abs=1e-9)


@pytest.mark.parametrize("seed", range(20))
def test_window_stroke_lists_match_draw_then_clip(seed):
    from ezdxf.addons.cut_usage.clipping import Window, make_window

    doc = build_doc(seed)
    usage = cut_usage.CutUsage(doc)
    low, high = Vec2(1, 1), Vec2(6, 6)
    rectangle = make_window([low, Vec2(high.x, low.y), high, Vec2(low.x, high.y)])
    window = Window("query", [rectangle.vertices])
    got = usage.strokes(window, include_deduplicated=True)
    reference = draw_then_clip(usage, window)
    assert len(got) == len(reference)
    got_length = sum((stroke.length for stroke in got), Fraction(0))
    reference_length = sum((stroke.length for stroke in reference), Fraction(0))
    assert float(got_length) == pytest.approx(float(reference_length), abs=1e-9)
    for engine_stroke, ref_stroke in zip(
        sorted(got, key=lambda s: (float(s.start.x), float(s.start.y), float(s.end.x))),
        sorted(
            reference,
            key=lambda s: (float(s.start.x), float(s.start.y), float(s.end.x)),
        ),
    ):
        assert float(engine_stroke.start.x) == pytest.approx(
            float(ref_stroke.start.x), abs=1e-9
        )
        assert float(engine_stroke.length) == pytest.approx(
            float(ref_stroke.length), abs=1e-9
        )


@pytest.mark.parametrize("seed", range(20))
def test_deduplicated_stroke_sum_equals_analytic_total(seed):
    doc = build_doc(seed)
    usage = cut_usage.CutUsage(doc)
    for entry in usage.layer_lengths():
        strokes = usage.strokes(layer=entry.layer)
        total = sum((stroke.length for stroke in strokes), Fraction(0))
        assert float(total) == pytest.approx(float(entry.length), abs=1e-9)
