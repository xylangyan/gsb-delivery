"""Tests for the segment archive entry point (archive_entry.py)."""

import random
import unittest

from archive_entry import Archive, SourceRef
from pyunormalize import NFC


def utf8(text):
    return text.encode("utf-8")


class TestCrossSegmentComposition(unittest.TestCase):
    """a | U+0327 | U+0301 in three consecutive segments."""

    def setUp(self):
        self.archive = Archive()
        self.archive.create_volume(
            "v",
            [
                ("p1", utf8("a")),
                ("p2", utf8("̧")),
                ("p3", utf8("́")),
            ],
        )

    def test_product_matches_independent_nfc(self):
        self.assertEqual(self.archive.product_text("v"), NFC("á̧"))
        self.assertEqual(self.archive.product_text("v"), "á̧")
        self.assertTrue(self.archive.verify("v"))

    def test_composed_char_references_only_participating_segments(self):
        window = self.archive.read_window("v", 0, 2)
        self.assertEqual(window.text, "á̧")
        composed, mark = window.chars
        # á is built from the "a" of p1 and the U+0301 of p3 only;
        # the bytes of the middle segment p2 must not be included.
        self.assertEqual(composed.char, "á")
        self.assertEqual(
            composed.sources,
            (SourceRef("p1", 0, 1), SourceRef("p3", 0, 2)),
        )
        # U+0327 keeps its own product position.
        self.assertEqual(mark.char, "̧")
        self.assertEqual(mark.sources, (SourceRef("p2", 0, 2),))

    def test_reverse_lookup_from_segment_bytes(self):
        hits = self.archive.lookup_source("v", "p3", 0, 2)
        self.assertEqual([(h.position, h.char) for h in hits], [(0, "á")])
        hits = self.archive.lookup_source("v", "p2", 0, 2)
        self.assertEqual([(h.position, h.char) for h in hits], [(1, "̧")])
        hits = self.archive.lookup_source("v", "p1", 0, 1)
        self.assertEqual([(h.position, h.char) for h in hits], [(0, "á")])


class TestHangulAcrossSegments(unittest.TestCase):
    """Hangul L, V and T jamo delivered from three different segments."""

    def test_lvt_compose_into_one_syllable(self):
        archive = Archive()
        archive.create_volume(
            "h",
            [
                ("s1", utf8("ᄒ")),
                ("s2", utf8("ᅡ")),
                ("s3", utf8("ᆫ")),
            ],
        )
        self.assertEqual(archive.product_text("h"), "한")
        self.assertTrue(archive.verify("h"))
        (char,) = archive.read_window("h", 0, 1).chars
        self.assertEqual(char.char, "한")
        self.assertEqual(
            char.sources,
            (
                SourceRef("s1", 0, 3),
                SourceRef("s2", 0, 3),
                SourceRef("s3", 0, 3),
            ),
        )
        for seg_id in ("s1", "s2", "s3"):
            hits = archive.lookup_source("h", seg_id, 0, 3)
            self.assertEqual([(h.position, h.char) for h in hits], [(0, "한")])


class TestDecompositionFanOut(unittest.TestCase):
    """One source character decomposing to several product positions."""

    def test_both_product_positions_reverse_to_the_source(self):
        # U+0F73 decomposes to U+0F71 U+0F72 and is excluded from
        # recomposition, so it lands on two delivered positions.
        source = "\u0F73"
        decomposed = "ཱི"
        self.assertEqual([ord(c) for c in source], [0x0F73])
        self.assertEqual([ord(c) for c in NFC(source)], [0x0F71, 0x0F72])
        archive = Archive()
        archive.create_volume("d", [("x", utf8(source))])
        self.assertEqual(archive.product_text("d"), decomposed)
        self.assertTrue(archive.verify("d"))

        hits = archive.lookup_source("d", "x", 0, 3)
        self.assertEqual([h.position for h in hits], [0, 1])
        window = archive.read_window("d", 0, 2)
        for char in window.chars:
            self.assertEqual(char.sources, (SourceRef("x", 0, 3),))


class TestWindowRead(unittest.TestCase):
    def test_window_matches_product_slice(self):
        archive = Archive()
        segments = [
            ("a", utf8("Café ")),
            ("b", utf8("한국어 ")),
            ("c", utf8("naïve ")),
            ("d", utf8("༳ོ")),
        ]
        archive.create_volume("w", segments)
        product = archive.product_text("w")
        self.assertTrue(archive.verify("w"))

        window = archive.read_window("w", 2, 8)
        self.assertEqual(window.text, product[2:8])
        self.assertEqual(
            [c.position for c in window.chars], list(range(2, 8))
        )
        for char in window.chars:
            self.assertEqual(char.char, product[char.position])
            self.assertTrue(char.sources)

        # Empty and full-span windows.
        self.assertEqual(archive.read_window("w", 3, 3).text, "")
        self.assertEqual(
            archive.read_window("w", 0, len(product)).text, product
        )

    def test_window_out_of_range_rejected(self):
        archive = Archive()
        archive.create_volume("w", [("a", utf8("abc"))])
        with self.assertRaises(ValueError):
            archive.read_window("w", 2, 9)


class TestVersioning(unittest.TestCase):
    def setUp(self):
        self.archive = Archive()
        self.archive.create_volume(
            "book",
            [
                ("p1", utf8("ab")),
                ("p2", utf8("e")),
                ("p3", utf8("́")),
                ("p4", utf8("xy")),
            ],
        )

    def test_correction_creates_new_version_and_keeps_old(self):
        self.assertEqual(self.archive.product_text("book"), "abéxy")
        saved_v1_window = self.archive.read_window("book", 0, 5)
        saved_v1_hits = self.archive.lookup_source("book", "p3", 0, 2)

        number = self.archive.correct_segment("book", "p3", utf8("̀"))
        self.assertEqual(number, 2)
        self.assertEqual(self.archive.version_count("book"), 2)

        # New version: ordering and source ranges re-established.
        self.assertEqual(self.archive.product_text("book"), "abèxy")
        self.assertTrue(self.archive.verify("book"))
        hits = self.archive.lookup_source("book", "p3", 0, 2)
        self.assertEqual([(h.position, h.char) for h in hits], [(2, "è")])

        # Old version still answers with the original content.
        self.assertEqual(self.archive.product_text("book", version=1), "abéxy")
        self.assertTrue(self.archive.verify("book", version=1))
        self.assertEqual(self.archive.read_window("book", 0, 5, version=1), saved_v1_window)
        self.assertEqual(
            self.archive.lookup_source("book", "p3", 0, 2, version=1),
            saved_v1_hits,
        )

    def test_correction_can_merge_across_segment_boundary(self):
        # Replacing p2 with a combining mark changes the composition of p1.
        self.archive.correct_segment("book", "p2", utf8("̂"))
        self.assertEqual(self.archive.product_text("book"), "ab̂́xy")
        self.assertTrue(self.archive.verify("book"))

    def test_unknown_segment_rejected(self):
        with self.assertRaises(KeyError):
            self.archive.correct_segment("book", "nope", utf8("x"))


class TestIncrementalReuse(unittest.TestCase):
    def test_only_affected_units_are_reprocessed(self):
        archive = Archive()
        segments = [(f"s{i}", utf8(f"w{i} ")) for i in range(300)]
        segments[100] = ("s100", utf8("e"))
        segments[101] = ("s101", utf8("́"))
        archive.create_volume("big", segments)
        v1 = archive._volumes["big"].versions[0]

        archive.correct_segment("big", "s101", utf8("̀"))
        v2 = archive._volumes["big"].versions[1]

        self.assertTrue(archive.verify("big"))
        # Units provably unaffected by the correction are reused, not
        # renormalized: they are the very same objects.
        identical = sum(1 for a, b in zip(v1.units, v2.units) if a is b)
        self.assertEqual(identical, len(v2.units) - 1)
        self.assertGreater(len(v1.units), 250)
        # The reprocessed run covers exactly the affected sequence.
        changed = [u for u in v2.units if u not in v1.units]
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0].text, "è")

    def test_correction_in_empty_segment(self):
        archive = Archive()
        archive.create_volume(
            "gap", [("a", utf8("x")), ("b", utf8("")), ("c", utf8("y"))]
        )
        self.assertEqual(archive.product_text("gap"), "xy")
        archive.correct_segment("gap", "b", utf8("́"))
        # The new mark attaches to the preceding starter across the
        # previously empty segment.
        self.assertEqual(archive.product_text("gap"), "x́y")
        self.assertTrue(archive.verify("gap"))
        self.assertTrue(archive.verify("gap", version=1))


class TestConsistencyFuzz(unittest.TestCase):
    """Small volumes: delivered order must match independent NFC."""

    POOL = [
        "a", "e", "o", "́", "̀", "̧", "̃",
        "á", "Ǻ", "ᄒ", "ᅡ", "ᆫ", "한",
        "༳", "ཱ", "ི", "̄", " ",
        "x", "1", "̈", "̣",
    ]

    def test_random_volumes_match_independent_nfc(self):
        rng = random.Random(20261124)
        archive = Archive()
        for trial in range(30):
            segments = [
                (f"s{i}", utf8(rng.choice(self.POOL)))
                for i in range(rng.randint(1, 12))
            ]
            volume_id = f"fuzz{trial}"
            archive.create_volume(volume_id, segments)
            self.assertTrue(archive.verify(volume_id), volume_id)

            product = archive.product_text(volume_id)
            window = archive.read_window(volume_id, 0, len(product))
            self.assertEqual(window.text, product)
            self.assertEqual(
                [c.position for c in window.chars], list(range(len(product)))
            )

            # Every source byte range reverse-maps to positions whose
            # provenance includes that range.
            for seg_id, data in segments:
                text = data.decode("utf-8")
                bpos = 0
                for ch in text:
                    bnext = bpos + len(ch.encode("utf-8"))
                    hits = archive.lookup_source(volume_id, seg_id, bpos, bnext)
                    self.assertTrue(hits, (volume_id, seg_id, ch))
                    for hit in hits:
                        refs = window.chars[hit.position].sources
                        self.assertIn(SourceRef(seg_id, bpos, bnext), refs)
                    bpos = bnext

    def test_random_corrections_keep_versions_consistent(self):
        rng = random.Random(1109)
        archive = Archive()
        segments = [
            (f"s{i}", utf8(rng.choice(self.POOL))) for i in range(40)
        ]
        archive.create_volume("vol", segments)
        for _ in range(15):
            seg_id = f"s{rng.randrange(40)}"
            # Corrections include emptying a segment entirely.
            archive.correct_segment(
                "vol", seg_id, utf8(rng.choice(self.POOL + [""]))
            )
            self.assertTrue(archive.verify("vol"))
        # All intermediate versions remain verifiable and queryable.
        for version in range(1, archive.version_count("vol") + 1):
            self.assertTrue(archive.verify("vol", version=version))
            product = archive.product_text("vol", version=version)
            window = archive.read_window("vol", 0, len(product), version=version)
            self.assertEqual(window.text, product)
            # Reverse index and forward provenance agree on every version.
            for hit_seg, entries in (
                (seg_id, archive.lookup_source("vol", seg_id, 0, 10**9, version=version))
                for seg_id, _ in segments
            ):
                for hit in entries:
                    refs = window.chars[hit.position].sources
                    self.assertIn(hit.source, refs)
                    self.assertEqual(hit.source.segment_id, hit_seg)


class TestEdgeCases(unittest.TestCase):
    def test_empty_and_ascii_volumes(self):
        archive = Archive()
        archive.create_volume("empty", [])
        self.assertEqual(archive.product_text("empty"), "")
        self.assertTrue(archive.verify("empty"))
        self.assertEqual(archive.read_window("empty", 0, 0).text, "")

        archive.create_volume(
            "ascii", [("a", utf8("plain ")), ("b", utf8("text"))]
        )
        self.assertEqual(archive.product_text("ascii"), "plain text")
        self.assertTrue(archive.verify("ascii"))

    def test_duplicate_segment_id_rejected(self):
        archive = Archive()
        with self.assertRaises(ValueError):
            archive.create_volume("dup", [("a", utf8("x")), ("a", utf8("y"))])

    def test_invalid_utf8_rejected(self):
        archive = Archive()
        with self.assertRaises(UnicodeDecodeError):
            archive.create_volume("bad", [("a", b"\xff\xfe")])


if __name__ == "__main__":
    unittest.main()
