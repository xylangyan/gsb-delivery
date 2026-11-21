# Tests for calib_archive: rewriting one calibration record in a PNG
# archive while preserving the IDAT segments and keeping the archive
# directory pointing at the real chunks.

# Run from the code directory:
#   python -m unittest test_calib_archive

import hashlib
import io
import os
import struct
import sys
import tempfile
import unittest

import calib_archive
from calib_archive import ArchiveError

SIGNATURE = calib_archive.SIGNATURE

IHDR_DATA = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)

COEFFS_1 = (0.5, -1.25, 3.75)
COEFFS_2 = (2.0, 4.5)
IDAT_PARTS = (b"\x78\x9c\x63\x60", b"\x00\x00\x00\x02", b"\x00\x01")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def make_archive(coeffs1=COEFFS_1, coeffs2=COEFFS_2, idat_parts=IDAT_PARTS,
                 extra=(), mutate_directory=None, drop_record=None):
    """Build an archive blob and the directory entries it should hold.

    ``extra`` chunks (tag, data) are inserted after the second
    calibration record; ``mutate_directory`` rewrites the sealed
    directory bytes; ``drop_record`` omits a caLb chunk from the layout
    while keeping its directory entry.
    """

    data1 = calib_archive.encode_calib(1, coeffs1)
    data2 = calib_archive.encode_calib(2, coeffs2)
    dir_data = b""
    entries = None
    for _ in range(100):
        layout = [(b"IHDR", IHDR_DATA), (b"caLd", dir_data)]
        if drop_record != 1:
            layout.append((b"caLb", data1))
        layout.extend((b"IDAT", part) for part in idat_parts)
        if drop_record != 2:
            layout.append((b"caLb", data2))
        layout.extend(extra)
        layout.append((b"IEND", b""))
        offset = len(SIGNATURE)
        positions = {}
        for tag, data in layout:
            if tag == b"caLb":
                record = 1 if data is data1 else 2
                positions[record] = offset
            offset += 12 + len(data)
        entries = []
        for record, data in ((1, data1), (2, data2)):
            entries.append(
                (record, positions.get(record, 0), len(data), sha256(data)))
        new_dir = calib_archive.encode_directory(entries)
        if new_dir == dir_data:
            break
        dir_data = new_dir
    else:
        raise AssertionError("test archive directory did not converge")
    if mutate_directory is not None:
        dir_data = mutate_directory(dir_data)
    blob = [SIGNATURE]
    for tag, data in layout:
        blob.append(calib_archive.make_chunk(
            tag, dir_data if tag == b"caLd" else data))
    return b"".join(blob), entries


class ForwardOnly:
    """A sink that can only be written to, and counts the writes."""

    def __init__(self):
        self.buf = bytearray()
        self.writes = 0

    def write(self, data):
        self.writes += 1
        self.buf += data


def read_directory(blob):
    chunks = calib_archive.read_chunks(io.BytesIO(blob))
    for chunk in chunks:
        if chunk.tag == b"caLd":
            return chunk, calib_archive.parse_directory(chunk.data)
    raise AssertionError("no directory chunk")


class TestRewrite(unittest.TestCase):

    def rewrite(self, blob, record=1, coefficients=(1.0, 2.0, 3.0, 4.0, 5.0)):
        sink = ForwardOnly()
        calib_archive.rewrite(io.BytesIO(blob), sink, record, coefficients)
        return bytes(sink.buf), sink

    def test_success(self):
        blob, old_entries = make_archive()
        new_coeffs = (1.0, 2.0, 3.0, 4.0, 5.0)
        out, sink = self.rewrite(blob, coefficients=new_coeffs)
        self.assertEqual(sink.writes, 1)

        # Independent re-read of the produced archive.
        chunks = calib_archive.read_chunks(io.BytesIO(out))
        dir_chunk, entries = read_directory(out)
        by_record = {}
        for chunk in chunks:
            if chunk.tag == b"caLb":
                record, coeffs = calib_archive.decode_calib(chunk.data)
                by_record[record] = (chunk, coeffs)

        # Both records sit where the directory says, with matching
        # lengths and digests.
        for record, offset, length, digest in entries:
            chunk, _ = by_record[record]
            self.assertEqual(chunk.offset, offset)
            self.assertEqual(len(chunk.data), length)
            self.assertEqual(sha256(chunk.data), digest)

        # The directory no longer points at the old positions of
        # anything after the replaced record.
        old = dict((r, o) for r, o, _, _ in old_entries)
        new = dict((r, o) for r, o, _, _ in entries)
        self.assertNotEqual(new[2], old[2])
        self.assertEqual(new[2], by_record[2][0].offset)

        # Record 1 holds the new coefficients; record 2 is untouched.
        self.assertEqual(tuple(by_record[1][1]), new_coeffs)
        self.assertEqual(tuple(by_record[2][1]), COEFFS_2)

        # IDAT bytes and segmentation are unchanged.
        idat = [c.raw for c in chunks if c.tag == b"IDAT"]
        expect = [calib_archive.make_chunk(b"IDAT", p) for p in IDAT_PARTS]
        self.assertEqual(idat, expect)

        # IHDR and IEND survive byte-for-byte.
        self.assertEqual(chunks[0].tag, b"IHDR")
        self.assertEqual(chunks[0].data, IHDR_DATA)
        self.assertEqual(chunks[-1].tag, b"IEND")

    def test_directory_length_change(self):
        # New coefficients are ~1kB longer, pushing the second record
        # past a digit boundary so the re-encoded directory itself
        # grows; positions must still be computed against the new
        # directory, not the original one.
        blob, old_entries = make_archive()
        old_dir_chunk, _ = read_directory(blob)
        new_coeffs = tuple(float(i) for i in range(130))
        out, sink = self.rewrite(blob, coefficients=new_coeffs)
        new_dir_chunk, entries = read_directory(out)
        self.assertNotEqual(len(new_dir_chunk.data), len(old_dir_chunk.data))

        chunks = calib_archive.read_chunks(io.BytesIO(out))
        actual = {}
        for chunk in chunks:
            if chunk.tag == b"caLb":
                record, _ = calib_archive.decode_calib(chunk.data)
                actual[record] = chunk
        for record, offset, length, digest in entries:
            self.assertEqual(actual[record].offset, offset)
            self.assertEqual(len(actual[record].data), length)
            self.assertEqual(sha256(actual[record].data), digest)
        old = dict((r, o) for r, o, _, _ in old_entries)
        for record, offset, _, _ in entries:
            self.assertNotEqual(offset, old[record])

    def test_duplicate_record(self):
        # An old bad record with the same record number appears later
        # in the file, outside the directory.
        stale = (b"caLb", calib_archive.encode_calib(1, (9.9,)))
        blob, _ = make_archive(extra=(stale,))
        sink = ForwardOnly()
        with self.assertRaises(ArchiveError) as caught:
            calib_archive.rewrite(io.BytesIO(blob), sink, 1, (1.0, 2.0))
        self.assertEqual(caught.exception.stage, "scan")
        self.assertEqual(caught.exception.record, 1)
        self.assertIn("duplicate", str(caught.exception))
        self.assertEqual(sink.writes, 0)

    def test_digest_mismatch(self):
        def corrupt(dir_data):
            line1, rest = dir_data.split(b"\n", 1)
            record, offset, length, digest = line1.split(b" ")
            bad = b"0" if digest[:1] != b"0" else b"1"
            return b" ".join(
                (record, offset, length, bad + digest[1:])) + b"\n" + rest

        blob, _ = make_archive(mutate_directory=corrupt)
        sink = ForwardOnly()
        with self.assertRaises(ArchiveError) as caught:
            calib_archive.rewrite(io.BytesIO(blob), sink, 1, (1.0, 2.0))
        self.assertEqual(caught.exception.stage, "scan")
        self.assertEqual(caught.exception.record, 1)
        self.assertIn("digest", str(caught.exception))
        self.assertEqual(sink.writes, 0)

    def test_missing_record(self):
        blob, _ = make_archive(drop_record=2)
        sink = ForwardOnly()
        with self.assertRaises(ArchiveError) as caught:
            calib_archive.rewrite(io.BytesIO(blob), sink, 1, (1.0, 2.0))
        self.assertEqual(caught.exception.stage, "scan")
        self.assertEqual(caught.exception.record, 2)
        self.assertIn("missing", str(caught.exception))
        self.assertEqual(sink.writes, 0)

    def test_directory_points_wrong(self):
        def corrupt(dir_data):
            line1, rest = dir_data.split(b"\n", 1)
            record, offset, length, digest = line1.split(b" ")
            return b" ".join((
                record, str(int(offset) + 4).encode("ascii"),
                length, digest)) + b"\n" + rest

        blob, _ = make_archive(mutate_directory=corrupt)
        sink = ForwardOnly()
        with self.assertRaises(ArchiveError) as caught:
            calib_archive.rewrite(io.BytesIO(blob), sink, 1, (1.0, 2.0))
        self.assertEqual(caught.exception.stage, "scan")
        self.assertEqual(caught.exception.record, 1)
        self.assertIn("offset", str(caught.exception))
        self.assertEqual(sink.writes, 0)

    def test_unknown_record(self):
        blob, _ = make_archive()
        sink = ForwardOnly()
        with self.assertRaises(ArchiveError) as caught:
            calib_archive.rewrite(io.BytesIO(blob), sink, 7, (1.0,))
        self.assertEqual(caught.exception.record, 7)
        self.assertEqual(sink.writes, 0)


class TestCommandLine(unittest.TestCase):

    def test_cli_success_and_failure(self):
        blob, _ = make_archive()
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "in.png")
            sink = os.path.join(tmp, "out.png")
            with open(source, "wb") as f:
                f.write(blob)
            rc = calib_archive.main(
                [source, sink, "1", "1.5,2.5,3.5,4.5"])
            self.assertEqual(rc, 0)
            with open(sink, "rb") as f:
                out = f.read()
            _, entries = read_directory(out)
            self.assertEqual(len(entries), 2)

            # Failure: no new archive may be left behind.
            os.unlink(sink)
            stale = (b"caLb", calib_archive.encode_calib(1, (9.9,)))
            bad, _ = make_archive(extra=(stale,))
            with open(source, "wb") as f:
                f.write(bad)
            rc = calib_archive.main([source, sink, "1", "1.5,2.5"])
            self.assertEqual(rc, 1)
            self.assertFalse(os.path.exists(sink))
            self.assertEqual(os.listdir(tmp), ["in.png"])


if __name__ == "__main__":
    unittest.main()
