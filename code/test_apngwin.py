#!/usr/bin/env python3
# test_apngwin.py
# Tests for apngwin: windowed APNG frame-value queries.
# Run from the code/ directory:
#   python test_apngwin.py

import os
import struct
import tempfile
import unittest

import png

import apngsample
import apngwin
from apngwin import DEFAULT


# The quality-control queries: a few frames, one small rectangle each.
QUERIES = [
    ('cover', (DEFAULT, 4, 4, 8, 6)),
    ('base', (0, 0, 0, 10, 10)),
    ('block', (1, 8, 6, 10, 8)),
    ('strip', (2, 6, 10, 12, 6)),
    ('background', (3, 4, 2, 12, 10)),
    ('cleared', (4, 4, 2, 12, 10)),
]

# Animation frame that no query touches; its body stays in cold storage.
COLD_FRAME = 5


class TestApngWin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='apngwin-')
        cls.path = os.path.join(cls.tmp, 'sample.png')
        with open(cls.path, 'wb') as f:
            f.write(apngsample.sample_apng_bytes())
        cls.file_size = os.path.getsize(cls.path)

    def engine(self):
        return apngwin.ApngWindows(self.path)

    def test_register(self):
        index = apngwin.register(self.path)
        # The file's edition mark is registered.
        self.assertEqual(index.signature, apngwin.SIGNATURE)
        self.assertEqual(index.actl, (6, 0))
        self.assertEqual(len(index.frames), 6)
        # The default image is only a cover, not the first frame.
        self.assertTrue(index.default_is_cover)
        self.assertEqual(len(index.default_data), 1)
        # Every frame body was split across two fdAT chunks.
        for spans in index.frame_data:
            self.assertEqual([s.type for s in spans], [b'fdAT', b'fdAT'])
        # Registered ranges match an independent walk of the file.
        with open(self.path, 'rb') as f:
            data = f.read()
        pos = 8
        for span in index.spans:
            length, tag = struct.unpack('!I4s', data[pos:pos + 8])
            self.assertEqual((span.type, span.offset, span.length),
                             (tag, pos + 8, length))
            pos += 12 + length
        self.assertEqual(pos, len(data))

    def test_windows_match_playback(self):
        # Every windowed query matches one full playback cropped afterwards.
        with self.engine() as engine:
            for name, (frame, x, y, w, h) in QUERIES:
                result = engine.query(frame, x, y, w, h)
                reference = apngwin.playback(self.path, frame, (x, y, w, h))
                self.assertEqual(result.rows, reference,
                                 'window %r differs from playback' % name)

    def test_cover_is_not_first_frame(self):
        with self.engine() as engine:
            cover = engine.query(DEFAULT, 0, 0, 8, 8)
            base = engine.query(0, 0, 0, 8, 8)
        self.assertNotEqual(cover.rows, base.rows)
        self.assertEqual(cover.rows[0][0], apngsample.cover_pixel(0, 0))
        self.assertEqual(base.rows[0][0], apngsample.gradient_pixel(0, 0))

    def test_previous_block_not_reused(self):
        # Frame 2's red block is restored away (PREVIOUS) before the
        # strip is displayed; a semi-transparent strip pixel above the
        # old block area blends with the gradient, not with the block.
        with self.engine() as engine:
            result = engine.query(2, 6, 10, 12, 6)
        # Canvas pixel (11, 11) is a semi-transparent blue strip pixel.
        x, y = 11 - 6, 11 - 10
        base = apngsample.gradient_pixel(11, 11)
        expect = apngwin._over(apngsample.SEMI_BLUE, base)
        self.assertEqual(result.rows[y][x], expect)
        restored_with_block = apngwin._over(apngsample.SEMI_BLUE,
                                            apngsample.RED)
        self.assertNotEqual(result.rows[y][x], restored_with_block)
        # Frame 1 (the red block) was never decoded for this query.
        self.assertNotIn(1, result.decoded)

    def test_background_dispose_clears(self):
        with self.engine() as engine:
            shown = engine.query(3, 4, 2, 12, 10)
            cleared = engine.query(4, 4, 2, 12, 10)
        # While displayed, the BACKGROUND frame covers its rectangle.
        for row in shown.rows:
            for px in row:
                self.assertEqual(px, apngsample.YELLOW)
        # The next frame displays the same strip already cleared.
        for row in cleared.rows:
            for px in row:
                self.assertEqual(px, (0, 0, 0, 0))
        # Proving the clearance needed no compressed body at all.
        self.assertEqual(cleared.decoded, [])

    def test_cold_frame_never_read(self):
        with self.engine() as engine:
            for name, (frame, x, y, w, h) in QUERIES:
                engine.query(frame, x, y, w, h)
            self.assertNotIn(COLD_FRAME, engine.decoded)
            cold_spans = ([engine.index.frames[COLD_FRAME]]
                          + engine.index.frame_data[COLD_FRAME])
            for span in cold_spans:
                self.assertNotIn((span.offset, span.length), engine.reads)

    def test_opaque_window_keeps_base_cold(self):
        # A window fully covered by opaque OVER pixels needs nothing
        # from earlier frames; their bodies stay in cold storage.
        with self.engine() as engine:
            result = engine.query(2, 8, 10, 2, 2)
            self.assertEqual(result.decoded, [2])
            for i in (0, 1):
                for span in engine.index.frame_data[i]:
                    self.assertNotIn((span.offset, span.length),
                                     result.reads)
            reference = apngwin.playback(self.path, 2, (8, 10, 2, 2))
            self.assertEqual(result.rows, reference)

    def test_reads_are_registered_ranges(self):
        with self.engine() as engine:
            for name, (frame, x, y, w, h) in QUERIES:
                engine.query(frame, x, y, w, h)
            registered = set((s.offset, s.length) for s in engine.index.spans)
            for read in engine.reads:
                self.assertIn(read, registered)
            # Range reads never amount to the whole file.
            total = sum(length for _, length in engine.reads)
            self.assertLess(total, self.file_size)

    def test_streams_decompressed_once(self):
        with self.engine() as engine:
            for name, (frame, x, y, w, h) in QUERIES:
                engine.query(frame, x, y, w, h)
            # Each needed compressed stream is decompressed exactly
            # once and shared by every window.
            self.assertEqual(sorted(engine.decoded),
                             sorted(set(engine.decoded)))
            self.assertEqual(set(engine.decoded), {0, 1, 2, 3})

    def test_window_outside_canvas(self):
        with self.engine() as engine:
            self.assertRaises(png.Error, engine.query, 0, 30, 0, 4, 4)
            self.assertRaises(png.Error, engine.query, 0, 0, 0, 0, 4)
            self.assertRaises(png.Error, engine.query, 6, 0, 0, 4, 4)


if __name__ == '__main__':
    unittest.main(__name__)
