#!/usr/bin/env python
"""Build the small PNG exhibits with the pinned PyPNG (0.0.21).

Generates into attribution/out/:
  parcel_jia_download.png   8x2 indexed mask, tRNS declares index 7
  parcel_yi_download.png    transparent, pHYs 100x200 px/m (meter unit).
                            The two downloads are byte-identical.
  old_archive_ratio_only.png  same mask, pHYs 100x200 with unit=0
                            (aspect ratio only, no meter declaration).
"""
import os

import png

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")

WIDTH, HEIGHT = 8, 2

INDEX_NORMAL = 1
INDEX_EMPTY_INTENDED = 3
INDEX_DECLARED_TRANSPARENT = 7

ROWS = [
    [7, 7, 7, 7, 1, 1, 1, 3],
    [7, 7, 7, 7, 1, 1, 1, 3],
]

PALETTE = [
    (0, 0, 0, 255),
    (34, 139, 34, 255),
    (0, 0, 0, 255),
    (30, 144, 255, 255),
    (0, 0, 0, 255),
    (0, 0, 0, 255),
    (0, 0, 0, 255),
    (128, 128, 128, 0),
]

X_PX_PER_M = 100
Y_PX_PER_M = 200


def write_mask(path, unit_is_meter):
    writer = png.Writer(
        width=WIDTH,
        height=HEIGHT,
        bitdepth=8,
        palette=PALETTE,
        x_pixels_per_unit=X_PX_PER_M,
        y_pixels_per_unit=Y_PX_PER_M,
        unit_is_meter=unit_is_meter,
    )
    with open(path, "wb") as handle:
        writer.write(handle, ROWS)


def main():
    os.makedirs(OUT, exist_ok=True)
    targets = [
        ("parcel_jia_download.png", True),
        ("parcel_yi_download.png", True),
        ("old_archive_ratio_only.png", False),
    ]
    for name, meter in targets:
        path = os.path.join(OUT, name)
        write_mask(path, unit_is_meter=meter)
        print("wrote %s (unit_is_meter=%s)" % (path, meter))


if __name__ == "__main__":
    main()
