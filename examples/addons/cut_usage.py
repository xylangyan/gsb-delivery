# Copyright (c) 2026, Manfred Moitzi
# License: MIT License
"""Report the cut length per process layer and inspect a paper window.

The script creates a small workshop drawing to show the API. For real work, read
an existing DXF file and pass it to the cut usage add-on.
"""
from pathlib import Path

import ezdxf
from ezdxf.addons import cut_usage


def build_sample(doc) -> None:
    doc.header["$LTSCALE"] = 1.0
    doc.layers.add("CUT", linetype="DASHED")
    doc.layers.add("ENGRAVE", linetype="CONTINUOUS")
    block = doc.blocks.new("BRACKET")
    block.add_line((0, 0), (80, 0), dxfattribs={"layer": "0"})
    block.add_line((80, 0), (80, 40), dxfattribs={"layer": "0"})
    block.add_line((0, 10), (60, 10), dxfattribs={"layer": "ENGRAVE"})
    msp = doc.modelspace()
    msp.add_blockref("BRACKET", (0, 0), dxfattribs={"layer": "CUT"})
    # the same block mirrored at x=160: its horizontal line runs back from x=160
    # to x=80 on the same support line as the first block, but with its own phase
    mirrored = msp.add_blockref("BRACKET", (160, 0), dxfattribs={"layer": "CUT"})
    mirrored.dxf.xscale = -1


def main() -> None:
    doc = ezdxf.new(setup=True)
    build_sample(doc)
    usage = cut_usage.CutUsage(doc)

    print("cut length per layer:")
    for entry in usage.layer_lengths():
        print(
            f"  {entry.layer:10s} {entry.length_float:10.3f} "
            f"({entry.support_lines} support lines)"
        )

    print("strokes in the paper window (100, -5) to (140, 5):")
    for stroke in usage.strokes_in((100, -5), (140, 5)):
        print(
            f"  {stroke.layer:10s} "
            f"({stroke.start.x:7.2f}, {stroke.start.y:7.2f}) -> "
            f"({stroke.end.x:7.2f}, {stroke.end.y:7.2f})  "
            f"length={float(stroke.length):6.3f}  {stroke.origin}"
        )

    out = Path("cut_usage_sample.dxf")
    doc.saveas(out)
    print(f"sample drawing written to: {out}")


if __name__ == "__main__":
    main()
