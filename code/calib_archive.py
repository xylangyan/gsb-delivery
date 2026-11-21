"""Rewrite one calibration record in a PNG calibration archive.

Archive layout (read once, in a single pass from the start):

    signature IHDR caLd caLb(record A) IDAT... caLb(record B) IEND

``caLb`` is a private ancillary chunk holding one calibration record:
a u16 record number, a u16 coefficient count, then that many float64
coefficients.  Two such chunks share the archive, distinguished only
by record number, one before the IDAT segments and one after.

``caLd`` is the archive directory, stored before IDAT: one ASCII line
per record, ``record offset length sha256hex``, giving the byte
position of the ``caLb`` chunk counted from the start of the file, the
chunk data length, and the SHA-256 of the chunk data.  Exporters seek
directly to the offsets published in the directory, so after replacing
a record the directory must be re-encoded with the new positions.
Because the directory is text, its own length may change when the
offsets change; the final positions are always computed against the
re-encoded directory, never against the original directory length.

The sink is assumed to be a forward-only stream: the rewritten archive
is assembled and verified in memory first, and only then sent to the
sink in a single write.  Any failure raises :class:`ArchiveError`
before anything is written, so no partial archive can be mistaken for
a successful result.
"""

import argparse
import binascii
import hashlib
import io
import os
import struct
import sys
import tempfile

SIGNATURE = b"\x89PNG\r\n\x1a\n"

CALIB_TAG = b"caLb"
DIRECTORY_TAG = b"caLd"
IDAT_TAG = b"IDAT"
IEND_TAG = b"IEND"


class ArchiveError(Exception):
    """A failure at a named stage, optionally tied to a record number."""

    def __init__(self, stage, record, message):
        self.stage = stage
        self.record = record
        self.message = message
        super().__init__(str(self))

    def __str__(self):
        if self.record is None:
            return "%s: %s" % (self.stage, self.message)
        return "%s: record %d: %s" % (self.stage, self.record, self.message)


class Chunk:
    def __init__(self, offset, tag, data, raw):
        self.offset = offset
        self.tag = tag
        self.data = data
        self.raw = raw
        self.record = None


def make_chunk(tag, data):
    crc = binascii.crc32(tag)
    crc = binascii.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def read_chunks(stream, stage="scan"):
    """Read every chunk of the archive exactly once, in file order."""

    sig = stream.read(len(SIGNATURE))
    if sig != SIGNATURE:
        raise ArchiveError(stage, None, "not a PNG archive (bad signature)")
    chunks = []
    offset = len(SIGNATURE)
    while True:
        header = stream.read(8)
        if len(header) < 8:
            raise ArchiveError(
                stage, None, "truncated chunk header at byte %d" % offset)
        (length,) = struct.unpack(">I", header[:4])
        tag = header[4:]
        data = stream.read(length)
        crc_bytes = stream.read(4)
        if len(data) != length or len(crc_bytes) != 4:
            raise ArchiveError(
                stage, None,
                "truncated chunk %s at byte %d" % (tag.decode("latin1"), offset))
        (stored_crc,) = struct.unpack(">I", crc_bytes)
        crc = binascii.crc32(tag)
        crc = binascii.crc32(data, crc) & 0xFFFFFFFF
        if crc != stored_crc:
            raise ArchiveError(
                stage, None,
                "CRC mismatch in chunk %s at byte %d"
                % (tag.decode("latin1"), offset))
        chunks.append(Chunk(offset, tag, data, header + data + crc_bytes))
        offset += 12 + length
        if tag == IEND_TAG:
            break
    if stream.read(1):
        raise ArchiveError(stage, None, "trailing bytes after IEND")
    return chunks


def encode_calib(record, coefficients):
    coeffs = [float(c) for c in coefficients]
    out = [struct.pack(">HH", record, len(coeffs))]
    out.extend(struct.pack(">d", c) for c in coeffs)
    return b"".join(out)


def decode_calib(data, stage="scan"):
    if len(data) < 4:
        raise ArchiveError(stage, None, "caLb chunk too short")
    record, count = struct.unpack(">HH", data[:4])
    if len(data) != 4 + 8 * count:
        raise ArchiveError(
            stage, record, "caLb length does not match coefficient count")
    coeffs = [
        struct.unpack(">d", data[4 + 8 * i:12 + 8 * i])[0]
        for i in range(count)
    ]
    return record, coeffs


def encode_directory(entries):
    """Encode ``(record, offset, length, digest_hex)`` lines as ASCII."""

    return "".join(
        "%d %d %d %s\n" % entry for entry in entries
    ).encode("ascii")


def parse_directory(data, stage="scan"):
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ArchiveError(stage, None, "directory is not ASCII")
    entries = []
    seen = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split(" ")
        if len(parts) != 4:
            raise ArchiveError(
                stage, None, "malformed directory line %r" % line)
        try:
            record, offset, length = (int(p) for p in parts[:3])
        except ValueError:
            raise ArchiveError(
                stage, None, "malformed directory line %r" % line)
        digest = parts[3]
        if record in seen:
            raise ArchiveError(stage, record, "duplicate directory entry")
        seen.add(record)
        entries.append((record, offset, length, digest))
    return entries


class Archive:
    def __init__(self, chunks, directory, entries, calib):
        self.chunks = chunks
        self.directory = directory
        self.entries = entries
        self.calib = calib


def scan_archive(stream, stage="scan"):
    """Single pass over the archive; validate directory against chunks."""

    chunks = read_chunks(stream, stage=stage)
    directory = None
    calib = {}
    seen_idat = False
    for chunk in chunks:
        if chunk.tag == IDAT_TAG:
            seen_idat = True
        elif chunk.tag == DIRECTORY_TAG:
            if directory is not None:
                raise ArchiveError(stage, None, "duplicate directory chunk")
            if seen_idat:
                raise ArchiveError(
                    stage, None, "directory chunk appears after IDAT")
            directory = chunk
        elif chunk.tag == CALIB_TAG:
            record, _ = decode_calib(chunk.data, stage=stage)
            chunk.record = record
            if record in calib:
                raise ArchiveError(
                    stage, record, "duplicate calibration record")
            calib[record] = chunk
    if directory is None:
        raise ArchiveError(stage, None, "no directory chunk")
    entries = parse_directory(directory.data, stage=stage)
    for record, offset, length, digest in entries:
        chunk = calib.get(record)
        if chunk is None:
            raise ArchiveError(
                stage, record, "directory lists missing calibration record")
        if chunk.offset != offset:
            raise ArchiveError(
                stage, record,
                "directory offset %d does not match actual position %d"
                % (offset, chunk.offset))
        if len(chunk.data) != length:
            raise ArchiveError(
                stage, record,
                "directory length %d does not match chunk data length %d"
                % (length, len(chunk.data)))
        if hashlib.sha256(chunk.data).hexdigest() != digest:
            raise ArchiveError(
                stage, record,
                "directory digest does not match chunk data")
    listed = set(entry[0] for entry in entries)
    for record in calib:
        if record not in listed:
            raise ArchiveError(
                stage, record, "calibration record not listed in directory")
    return Archive(chunks, directory, entries, calib)


def build_output(archive, record, new_data):
    """Assemble the rewritten archive in memory.

    Directory offsets are recomputed against the re-encoded directory;
    because the directory is text its length may change, so iterate to
    a fixed point instead of using the original directory length.
    """

    entry_records = [entry[0] for entry in archive.entries]
    dir_data = archive.directory.data
    for _ in range(64):
        offset = len(SIGNATURE)
        positions = {}
        for chunk in archive.chunks:
            if chunk.tag == DIRECTORY_TAG:
                size = 12 + len(dir_data)
            elif chunk is archive.calib[record]:
                size = 12 + len(new_data)
            else:
                size = len(chunk.raw)
            if chunk.tag == CALIB_TAG:
                positions[chunk.record] = offset
            offset += size
        entries = []
        for rec in entry_records:
            data = new_data if rec == record else archive.calib[rec].data
            entries.append((
                rec, positions[rec], len(data),
                hashlib.sha256(data).hexdigest()))
        new_dir = encode_directory(entries)
        if new_dir == dir_data:
            break
        dir_data = new_dir
    else:
        raise ArchiveError(
            "rewrite", record, "directory encoding did not converge")
    out = [SIGNATURE]
    for chunk in archive.chunks:
        if chunk.tag == DIRECTORY_TAG:
            out.append(make_chunk(DIRECTORY_TAG, dir_data))
        elif chunk is archive.calib[record]:
            out.append(make_chunk(CALIB_TAG, new_data))
        else:
            out.append(chunk.raw)
    return b"".join(out)


def verify_output(output, archive, record, new_data):
    """Independently re-read the rewritten archive and check it.

    Uses a fresh chunk reader: both calibration chunks must sit at the
    positions published in the new directory, with matching lengths,
    CRCs (checked while reading) and record digests; the directory must
    not point at the old positions; every other chunk, including the
    IDAT segments and their boundaries, must be byte-identical.
    """

    chunks = read_chunks(io.BytesIO(output), stage="verify")
    fresh = scan_archive(io.BytesIO(output), stage="verify")

    old_entries = dict((e[0], e) for e in archive.entries)
    replaced_old_offset = archive.calib[record].offset
    for rec, offset, length, digest in fresh.entries:
        chunk = fresh.calib[rec]
        if chunk.offset != offset:
            raise ArchiveError(
                "verify", rec,
                "directory offset %d does not match actual position %d"
                % (offset, chunk.offset))
        if len(chunk.data) != length:
            raise ArchiveError(
                "verify", rec, "directory length does not match chunk data")
        if hashlib.sha256(chunk.data).hexdigest() != digest:
            raise ArchiveError(
                "verify", rec, "directory digest does not match chunk data")
        old_offset = old_entries[rec][1]
        if old_offset > replaced_old_offset and offset == old_offset:
            raise ArchiveError(
                "verify", rec,
                "directory still points at the old position %d" % offset)
    if fresh.calib[record].data != new_data:
        raise ArchiveError(
            "verify", record, "rewritten record does not hold new data")

    def preserved(chunks):
        return [
            (c.tag, c.raw) for c in chunks
            if c.tag != DIRECTORY_TAG
            and not (c.tag == CALIB_TAG and c.record == record)
        ]

    if preserved(fresh.chunks) != preserved(archive.chunks):
        raise ArchiveError(
            "verify", None,
            "chunks outside the directory and calibration records changed")
    old_idat = [c.raw for c in archive.chunks if c.tag == IDAT_TAG]
    new_idat = [c.raw for c in fresh.chunks if c.tag == IDAT_TAG]
    if new_idat != old_idat:
        raise ArchiveError(
            "verify", None, "IDAT bytes or segmentation changed")


def rewrite(source, sink, record, coefficients):
    """Replace the coefficients of ``record`` and write to ``sink``.

    The source is read once from the start.  The sink receives the
    verified archive in a single write, only after every check has
    passed; on any failure nothing is written.
    """

    archive = scan_archive(source)
    if record not in archive.calib:
        raise ArchiveError("scan", record, "no such calibration record")
    new_data = encode_calib(record, coefficients)
    output = build_output(archive, record, new_data)
    verify_output(output, archive, record, new_data)
    sink.write(output)
    return len(output)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Replace one calibration record in a PNG archive.")
    parser.add_argument("source", help="input archive (read once)")
    parser.add_argument("sink", help="output archive path")
    parser.add_argument("record", type=int, help="record number to replace")
    parser.add_argument(
        "coefficients", help="comma-separated new coefficients")
    args = parser.parse_args(argv)
    coefficients = [float(x) for x in args.coefficients.split(",")]

    sink_dir = os.path.dirname(os.path.abspath(args.sink))
    fd, temp_path = tempfile.mkstemp(
        dir=sink_dir, prefix=".calib-", suffix=".tmp")
    try:
        with open(args.source, "rb") as source:
            with os.fdopen(fd, "wb") as sink:
                rewrite(source, sink, args.record, coefficients)
    except ArchiveError as error:
        os.unlink(temp_path)
        print("calib_archive: %s" % error, file=sys.stderr)
        return 1
    os.replace(temp_path, args.sink)
    return 0


if __name__ == "__main__":
    sys.exit(main())
