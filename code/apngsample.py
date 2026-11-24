#!/usr/bin/env python3
# apngsample.py
"""Build the sample APNG used by the windowed-query quality checks.

The sample follows the file's own conventions:

* the default image (IDAT) is only a cover, not the first animation
  frame (the first fcTL follows the IDAT chunks);
* frame 1 paints a full-canvas gradient (SOURCE, dispose NONE);
* frame 2 temporarily covers a window with a red block, disposed with
  PREVIOUS after display;
* frame 3 blends a semi-transparent strip in from the side (OVER);
  in the same region some pixels are fully opaque, others need the
  base from before frame 2;
* frame 4 is a yellow block disposed with BACKGROUND;
* frame 5 is a magenta block elsewhere, so the strip that frame 4
  covered is already cleared when frame 5 is displayed;
* frame 6 sits in a corner that no quality-control window touches.
"""

import io
import os
import struct

import png

import apngwin

WIDTH = 32
HEIGHT = 24

# Animation frames: (rows, width, height, x, y, dispose_op, blend_op).
# Filled in by sample_apng_bytes; described here for the queries:
#   0: gradient, full canvas, SOURCE, dispose NONE
#   1: red block at (8, 6, 10, 8), SOURCE, dispose PREVIOUS
#   2: strip at (0, 10, 20, 6), OVER, dispose NONE
#   3: yellow block at (4, 2, 12, 10), SOURCE, dispose BACKGROUND
#   4: magenta block at (20, 2, 8, 6), SOURCE, dispose NONE
#   5: corner block at (24, 16, 6, 6), SOURCE, dispose NONE (cold)

BLOCK_RECT = (8, 6, 10, 8)
STRIP_RECT = (0, 10, 20, 6)
BACKGROUND_RECT = (4, 2, 12, 10)
MAGENTA_RECT = (20, 2, 8, 6)
CORNER_RECT = (24, 16, 6, 6)

RED = (200, 30, 30, 255)
YELLOW = (220, 220, 0, 255)
MAGENTA = (200, 0, 200, 255)
CYAN = (30, 200, 200, 255)
OPAQUE_GREEN = (0, 200, 0, 255)
SEMI_BLUE = (0, 0, 200, 128)


def gradient_pixel(x, y):
    return ((x * 8) % 256, (y * 10) % 256, 128, 255)


def cover_pixel(x, y):
    return ((x * 16) % 256, (y * 16) % 256, 64, 255)


def strip_pixel(x, y):
    if (x // 2) % 2 == 0:
        return OPAQUE_GREEN
    return SEMI_BLUE


def _rows(w, h, pixel):
    rows = []
    for y in range(h):
        row = []
        for x in range(w):
            row += list(pixel(x, y))
        rows.append(row)
    return rows


def _solid(w, h, rgba):
    return _rows(w, h, lambda x, y: rgba)


def _idat(rows, w, h):
    """Compress rows with PyPNG's Writer; return the IDAT content."""
    bio = io.BytesIO()
    png.Writer(w, h, greyscale=False, alpha=True, bitdepth=8).write(bio, rows)
    data = bio.getvalue()
    out = []
    pos = 8
    while pos < len(data):
        length, tag = struct.unpack('!I4s', data[pos:pos + 8])
        if tag == b'IDAT':
            out.append(data[pos + 8:pos + 8 + length])
        pos += 12 + length
    return b''.join(out)


def sample_apng_bytes():
    out = io.BytesIO()
    out.write(apngwin.SIGNATURE)
    png.write_chunk(out, b'IHDR',
                    struct.pack('!2I5B', WIDTH, HEIGHT, 8, 6, 0, 0, 0))
    frames = [
        (_rows(WIDTH, HEIGHT, gradient_pixel), WIDTH, HEIGHT, 0, 0,
         apngwin.DISPOSE_NONE, apngwin.BLEND_SOURCE),
        (_solid(*BLOCK_RECT[2:], RED), BLOCK_RECT[2], BLOCK_RECT[3],
         BLOCK_RECT[0], BLOCK_RECT[1],
         apngwin.DISPOSE_PREVIOUS, apngwin.BLEND_SOURCE),
        (_rows(*STRIP_RECT[2:], strip_pixel), STRIP_RECT[2], STRIP_RECT[3],
         STRIP_RECT[0], STRIP_RECT[1],
         apngwin.DISPOSE_NONE, apngwin.BLEND_OVER),
        (_solid(*BACKGROUND_RECT[2:], YELLOW),
         BACKGROUND_RECT[2], BACKGROUND_RECT[3],
         BACKGROUND_RECT[0], BACKGROUND_RECT[1],
         apngwin.DISPOSE_BACKGROUND, apngwin.BLEND_SOURCE),
        (_solid(*MAGENTA_RECT[2:], MAGENTA),
         MAGENTA_RECT[2], MAGENTA_RECT[3],
         MAGENTA_RECT[0], MAGENTA_RECT[1],
         apngwin.DISPOSE_NONE, apngwin.BLEND_SOURCE),
        (_solid(*CORNER_RECT[2:], CYAN), CORNER_RECT[2], CORNER_RECT[3],
         CORNER_RECT[0], CORNER_RECT[1],
         apngwin.DISPOSE_NONE, apngwin.BLEND_SOURCE),
    ]
    png.write_chunk(out, b'acTL', struct.pack('!2I', len(frames), 0))
    # The default image is only a cover, not the first animation frame.
    png.write_chunk(out, b'IDAT', _idat(_rows(WIDTH, HEIGHT, cover_pixel),
                                        WIDTH, HEIGHT))
    seq = 0
    for (rows, w, h, x, y, dispose, blend) in frames:
        png.write_chunk(out, b'fcTL', struct.pack(
            '!5I2H2B', seq, w, h, x, y, 10, 100, dispose, blend))
        seq += 1
        body = _idat(rows, w, h)
        # Split each body across two fdAT chunks.
        cut = max(1, len(body) // 2)
        for part in (body[:cut], body[cut:]):
            png.write_chunk(out, b'fdAT', struct.pack('!I', seq) + part)
            seq += 1
    png.write_chunk(out, b'IEND', b'')
    return out.getvalue()


def main(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            os.pardir, 'asset', 'apngwin-sample.png')
    with open(path, 'wb') as f:
        f.write(sample_apng_bytes())
    print(path)


if __name__ == '__main__':
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else None)
