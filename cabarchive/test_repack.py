#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2020 Richard Hughes <richard@hughsie.com>
#
# SPDX-License-Identifier: LGPL-2.1+
#
# pylint: disable=wrong-import-position

import datetime
import json
import os
import random
import struct
import sys
import tempfile
import unittest
import zlib

# allows us to run this from the project root
sys.path.append(os.path.realpath("."))

from cabarchive import CabArchive, CabFile, NotSupportedError
from cabarchive.repack import (
    CHUNK_SIZE,
    REASON_BREAKPOINT_NOT_ALLOWED,
    REASON_CONTENT_MISMATCH,
    REASON_DEMOTED,
    REASON_DICTIONARY_MISMATCH,
    REASON_OK,
    REASON_POSITION_NOT_ALLOWED,
    REASON_VOUCHER_TAKEN,
    Repacker,
    _parse_source_blocks,
    assemble_cab,
    main as repack_main,
)
from cabarchive.utils import FMT_CFDATA, _checksum_compute

EPOCH = datetime.datetime(2020, 1, 2, 3, 4, 5)


def _mszip_record(chunk: bytes, zdict: bytes = None) -> bytes:
    """Build one MSZIP CFDATA record, optionally chained to a dictionary."""
    if zdict is None:
        compressobj = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    else:
        compressobj = zlib.compressobj(
            9, zlib.DEFLATED, -zlib.MAX_WBITS, zdict=zdict
        )
    data = b"CK" + compressobj.compress(chunk) + compressobj.flush()
    checksum = _checksum_compute(data)
    hdr = struct.pack("<HH", len(data), len(chunk))
    checksum = _checksum_compute(hdr, checksum)
    return struct.pack(FMT_CFDATA, checksum, len(data), len(chunk)) + data


def _make_cab(files, records):
    """Assemble a source cabinet from (name, bytes) pairs and CFDATA records."""
    cffiles = []
    for name, buf in files:
        cffile = CabFile(buf=buf, mtime=EPOCH)
        cffile.filename = name
        cffiles.append((name, cffile))
    return assemble_cab(cffiles, records, set_id=0x0622)


def _chained_records(chunks, zdict_for):
    """Compress chunks, chaining a dictionary for the indices in zdict_for."""
    records = []
    for idx, chunk in enumerate(chunks):
        if idx in zdict_for:
            records.append(_mszip_record(chunk, zdict=chunks[idx - 1]))
        else:
            records.append(_mszip_record(chunk))
    return records


def _main_source():
    """Source cabinet: QC images and tables, some sharing local content.

    File layout (uncompressed offsets):
      qc/img1.jpg       0 .. 40000
      qc/img2.jpg   40000 .. 80000   (identical to img1)
      qc/img3.jpg   80000 .. 130000  (random)
      qc/img4.jpg   130000 .. 170000 (identical to img1)
      notes/table1.csv 170000 .. 190000
      notes/table2.csv 190000 .. 195000
    Blocks B0..B4 are 0x8000 bytes, B5 is 31160 bytes; B1 and B2 borrow
    the previous block as deflate dictionary.
    """
    rng = random.Random(42)
    tile = bytes(rng.randrange(256) for _ in range(1024))
    img1 = (tile * 40)[:40000]
    img3 = bytes(rng.randrange(256) for _ in range(50000))
    table1 = (b"qc,table,row\n" * 2000)[:20000]
    table2 = (b"tail,1\n" * 1000)[:5000]
    files = [
        ("qc/img1.jpg", img1),
        ("qc/img2.jpg", img1),
        ("qc/img3.jpg", img3),
        ("qc/img4.jpg", img1),
        ("notes/table1.csv", table1),
        ("notes/table2.csv", table2),
    ]
    stream = b"".join(buf for _name, buf in files)
    chunks = [stream[i : i + CHUNK_SIZE] for i in range(0, len(stream), CHUNK_SIZE)]
    assert [len(c) for c in chunks] == [32768] * 5 + [31160]
    records = _chained_records(chunks, zdict_for={1, 2})
    return _make_cab(files, records), files


def _main_sheet(table2_new, img3_new):
    return {
        "packages": [
            {
                "output": "batch-a.cab",
                "files": [
                    {"name": "qc/img1.jpg", "from": "qc/img1.jpg"},
                    {"name": "qc/img2.jpg", "from": "qc/img2.jpg"},
                    {"name": "qc/img3.jpg", "from": "qc/img3.jpg"},
                    {"name": "qc/img4.jpg", "from": "qc/img4.jpg"},
                    {"name": "notes/table1.csv", "from": "notes/table1.csv"},
                    {"name": "notes/table2.csv", "text": table2_new},
                ],
                "blocks": {
                    "0": [0],
                    "1": [32768],
                    "2": [65536],
                    "3": [98304],
                    "4": [131072],
                    "5": [163840],
                },
            },
            {
                "output": "batch-b.cab",
                "files": [
                    {"name": "qc/img2.jpg", "from": "qc/img2.jpg"},
                    {"name": "qc/img1.jpg", "from": "qc/img1.jpg"},
                    {"name": "qc/img3.jpg", "text": img3_new},
                    {"name": "qc/img4.jpg", "from": "qc/img4.jpg"},
                    {"name": "notes/table1.csv", "from": "notes/table1.csv"},
                    {"name": "notes/table2.csv", "from": "notes/table2.csv"},
                ],
                "breakpoints": [
                    0,
                    25536,
                    40000,
                    58304,
                    72768,
                    105536,
                    129072,
                    160768,
                    161840,
                    193000,
                ],
                "blocks": {
                    "0": [40000, 40100],
                    "1": [72768],
                    "2": [25536],
                    "4": [129072],
                    "5": [161840],
                },
            },
        ]
    }


class TestRepack(unittest.TestCase):
    def test_two_batches(self):
        table2_new = ("tail,2\n" * 1000)[:5000]
        img3_new = ("newimg3data" * 5000)[:48000]
        source_buf, files = _main_source()
        contents = dict(files)

        # sanity: the source itself is a valid cabinet
        arc = CabArchive(source_buf)
        self.assertEqual(len(arc), 6)

        repacker = Repacker(source_buf)
        self.assertEqual(len(repacker.blocks), 6)
        result = repacker.build(_main_sheet(table2_new, img3_new))
        self.assertEqual(len(result.packages), 2)
        pkg_a, pkg_b = result.packages

        # every reused voucher is spent at most once across both deliveries
        receipts = [p.receipt for pkg in result.packages for p in pkg.placements]
        self.assertEqual(len(receipts), len(set(receipts)))

        # batch A reuses B0..B4, only the changed tail is recompressed
        self.assertEqual(
            [(p.receipt, p.offset) for p in pkg_a.placements],
            [(0, 0), (1, 32768), (2, 65536), (3, 98304), (4, 131072)],
        )
        self.assertEqual(pkg_a.reused_bytes, 163840)
        self.assertEqual(pkg_a.fresh_bytes, 31160)

        # batch B loses the shared vouchers and reuses only B5
        self.assertEqual([(p.receipt, p.offset) for p in pkg_b.placements], [(5, 161840)])
        self.assertEqual(pkg_b.reused_bytes, 31160)
        self.assertEqual(pkg_b.fresh_bytes, 161840)

        # rejected sheet requests carry their reason
        rejected_a = {(r["receipt"], r["position"]): r["reason"] for r in pkg_a.rejected}
        self.assertEqual(rejected_a.get((5, 163840)), REASON_CONTENT_MISMATCH)
        rejected_b = {(r["receipt"], r["position"]): r["reason"] for r in pkg_b.rejected}
        self.assertEqual(rejected_b.get((0, 40000)), REASON_VOUCHER_TAKEN)
        self.assertEqual(rejected_b.get((4, 129072)), REASON_VOUCHER_TAKEN)
        self.assertEqual(rejected_b.get((1, 72768)), REASON_CONTENT_MISMATCH)
        self.assertEqual(rejected_b.get((2, 25536)), REASON_CONTENT_MISMATCH)
        self.assertEqual(rejected_b.get((0, 40100)), REASON_BREAKPOINT_NOT_ALLOWED)

        # the same is queryable after the fact
        self.assertEqual(repacker.explain(1, 0, 40000), REASON_VOUCHER_TAKEN)
        self.assertEqual(repacker.explain(1, 0, 40100), REASON_BREAKPOINT_NOT_ALLOWED)
        self.assertEqual(repacker.explain(1, 0, 12345), REASON_POSITION_NOT_ALLOWED)
        self.assertEqual(repacker.explain(1, 1, 72768), REASON_CONTENT_MISMATCH)
        self.assertEqual(repacker.explain(0, 0, 0), REASON_OK)
        self.assertEqual(repacker.explain(1, 5, 161840), REASON_OK)

        # both packages reopen and match the sheet item by item
        self.assertEqual(result.verification, [])
        arc_a = CabArchive(pkg_a.cab_bytes)
        self.assertEqual(
            list(arc_a.keys()),
            [
                "qc\\img1.jpg",
                "qc\\img2.jpg",
                "qc\\img3.jpg",
                "qc\\img4.jpg",
                "notes\\table1.csv",
                "notes\\table2.csv",
            ],
        )
        self.assertEqual(arc_a["qc\\img1.jpg"].buf, contents["qc/img1.jpg"])
        self.assertEqual(arc_a["qc\\img3.jpg"].buf, contents["qc/img3.jpg"])
        self.assertEqual(arc_a["notes\\table2.csv"].buf, table2_new.encode())
        arc_b = CabArchive(pkg_b.cab_bytes)
        self.assertEqual(
            list(arc_b.keys()),
            [
                "qc\\img2.jpg",
                "qc\\img1.jpg",
                "qc\\img3.jpg",
                "qc\\img4.jpg",
                "notes\\table1.csv",
                "notes\\table2.csv",
            ],
        )
        self.assertEqual(arc_b["qc\\img3.jpg"].buf, img3_new.encode())
        self.assertEqual(arc_b["notes\\table2.csv"].buf, contents["notes/table2.csv"])

        # grafted blocks keep compressed body and checksum byte for byte
        _set_id, out_blocks = _parse_source_blocks(pkg_a.cab_bytes)
        for placement in pkg_a.placements:
            found = [b for b in out_blocks if b.offset == placement.offset]
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].record, repacker.blocks[placement.receipt].record)
            self.assertEqual(found[0].checksum, placement.checksum)

    def test_dictionary_mismatch(self):
        # a block borrowing the previous block as dictionary cannot be
        # grafted behind changed preceding content, while a self contained
        # block with the same output can
        chunk_x = (b"the-quick-brown-fox-jumps-over-13-lazy-dogs\n" * 1000)[:32768]
        records = [
            _mszip_record(chunk_x),
            _mszip_record(chunk_x, zdict=chunk_x),
        ]
        source_buf = _make_cab([("a.bin", chunk_x + chunk_x)], records)
        prefix = "z" * 100
        sheet = {
            "packages": [
                {
                    "output": "p1.cab",
                    "files": [
                        {"name": "b.bin", "text": prefix + chunk_x.decode()}
                    ],
                    "breakpoints": [0, 100, 32868],
                    "blocks": {"0": [100], "1": [100]},
                }
            ]
        }
        repacker = Repacker(source_buf)
        result = repacker.build(sheet)
        pkg = result.packages[0]

        # the self contained block is grafted, the dependent one is not
        self.assertEqual([(p.receipt, p.offset) for p in pkg.placements], [(0, 100)])
        rejected = {(r["receipt"], r["position"]): r["reason"] for r in pkg.rejected}
        self.assertEqual(rejected.get((1, 100)), REASON_DICTIONARY_MISMATCH)
        self.assertEqual(repacker.explain(0, 1, 100), REASON_DICTIONARY_MISMATCH)
        self.assertEqual(repacker.explain(0, 0, 100), REASON_OK)

        self.assertEqual(result.verification, [])
        arc = CabArchive(pkg.cab_bytes)
        self.assertEqual(arc["b.bin"].buf, (prefix + chunk_x.decode()).encode())

    def test_demote_truncated_dictionary(self):
        # the candidate check sees a 32k window, but the parser only offers
        # the previous block's output; a block that decodes against the
        # window but not against the short previous block is demoted to
        # freshly compressed data instead of corrupting the package
        chunk_x = (b"the-quick-brown-fox-jumps-over-13-lazy-dogs\n" * 1000)[:32768]
        records = [
            _mszip_record(chunk_x),
            _mszip_record(chunk_x, zdict=chunk_x),
        ]
        source_buf = _make_cab([("a.bin", chunk_x + chunk_x)], records)
        prefix = "w" * 100
        sheet = {
            "packages": [
                {
                    "output": "p1.cab",
                    "files": [
                        {
                            "name": "c.bin",
                            "text": prefix + chunk_x.decode() + chunk_x.decode(),
                        }
                    ],
                    "breakpoints": [0, 32868, 65636],
                    "blocks": {"1": [32868]},
                }
            ]
        }
        repacker = Repacker(source_buf)
        result = repacker.build(sheet)
        pkg = result.packages[0]

        # receipt 1 passed the candidate check but cannot follow a 100 byte
        # block, so it is demoted and the voucher is reported as spent
        self.assertEqual(pkg.placements, [])
        self.assertEqual(
            pkg.demoted, [{"receipt": 1, "offset": 32868, "reason": REASON_DEMOTED}]
        )
        self.assertEqual(pkg.fresh_bytes, 65636)
        self.assertEqual(repacker.explain(0, 1, 32868), REASON_DEMOTED)
        self.assertEqual(result.verification, [])
        arc = CabArchive(pkg.cab_bytes)
        self.assertEqual(
            arc["c.bin"].buf, (prefix + chunk_x.decode() + chunk_x.decode()).encode()
        )

    def test_unsupported_source(self):
        with open("data/multi-folder-compressed.cab", "rb") as f:
            with self.assertRaises(NotSupportedError):
                Repacker(f.read())
        with open("data/simple.cab", "rb") as f:
            with self.assertRaises(NotSupportedError):
                Repacker(f.read())

    def test_cli(self):
        chunk_x = (b"the-quick-brown-fox-jumps-over-13-lazy-dogs\n" * 1000)[:32768]
        records = [_mszip_record(chunk_x), _mszip_record(chunk_x, zdict=chunk_x)]
        source_buf = _make_cab([("a.bin", chunk_x + chunk_x)], records)
        sheet = {
            "source": "src.cab",
            "packages": [
                {
                    "output": "p1.cab",
                    "files": [
                        {"name": "b.bin", "text": chunk_x.decode()},
                        {"name": "c.bin", "data": "payload.bin"},
                    ],
                    "blocks": {"0": [0]},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "src.cab"), "wb") as f:
                f.write(source_buf)
            with open(os.path.join(tmpdir, "payload.bin"), "wb") as f:
                f.write(b"payload" * 100)
            sheet_path = os.path.join(tmpdir, "sheet.json")
            with open(sheet_path, "w", encoding="utf-8") as f:
                json.dump(sheet, f)
            report_path = os.path.join(tmpdir, "report.json")
            rc = repack_main([sheet_path, "--report", report_path])
            self.assertEqual(rc, 0)
            with open(os.path.join(tmpdir, "p1.cab"), "rb") as f:
                arc = CabArchive(f.read())
            self.assertEqual(arc["b.bin"].buf, chunk_x)
            self.assertEqual(arc["c.bin"].buf, b"payload" * 100)
            with open(report_path, "r", encoding="utf-8") as f:
                report = json.load(f)
            self.assertEqual(report["verification"], [])
            self.assertEqual(
                report["packages"][0]["placements"],
                [
                    {
                        "receipt": 0,
                        "offset": 0,
                        "size": 32768,
                        "checksum": report["packages"][0]["placements"][0]["checksum"],
                        "compressed_size": report["packages"][0]["placements"][0][
                            "compressed_size"
                        ],
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
