import io

import pytest

from pdfminer.high_level import extract_pages
from pdfminer.layout import LTChar
from pdfminer.receipt import (
    CorrectionRegistry,
    FontIdentity,
    UnicodeCandidate,
    extract_receipt_columns,
)


class PdfBuilder:
    """Minimal indirect-object PDF writer used by the receipt tests."""

    def __init__(self) -> None:
        self.objects: dict[int, bytes] = {}
        self.next_id = 1

    def add(self, body: bytes) -> int:
        objid = self.next_id
        self.objects[objid] = body
        self.next_id += 1
        return objid

    def stream(self, attrs: bytes, data: bytes) -> bytes:
        return (
            b"<< "
            + attrs
            + b" /Length "
            + str(len(data)).encode()
            + b" >>\nstream\n"
            + data
            + b"\nendstream"
        )

    def build(self, root_id: int) -> bytes:
        out = io.BytesIO()
        out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = {0: 0}
        for objid in sorted(self.objects):
            offsets[objid] = out.tell()
            out.write(f"{objid} 0 obj\n".encode())
            out.write(self.objects[objid])
            out.write(b"\nendobj\n")
        xref_pos = out.tell()
        count = self.next_id
        out.write(f"xref\n0 {count}\n".encode())
        out.write(b"0000000000 65535 f \n")
        for objid in range(1, count):
            out.write(f"{offsets[objid]:010d} 00000 n \n".encode())
        out.write(
            b"trailer\n<< /Size "
            + str(count).encode()
            + b" /Root "
            + str(root_id).encode()
            + b" 0 R >>\nstartxref\n"
            + str(xref_pos).encode()
            + b"\n%%EOF\n",
        )
        return out.getvalue()


TOUNI_AB_C_D = (
    b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
    b"/CMapType 2 def\n1 begincodespacerange\n<0000> <FFFF>\n"
    b"endcodespacerange\n3 beginbfchar\n"
    b"<8140> <00410042>\n<0020> <0043>\n<0030> <0044>\n"
    b"endbfchar\nendcmap\nend\nend\n"
)


def build_receipt_pdf() -> tuple[bytes, FontIdentity, FontIdentity]:
    """The same form object is drawn twice at page x=0 and x=-2.

    Form Fm1 uses actual font object font1 and is shown twice through
    ``[<8140> 100 <0020> <0030>] TJ`` at 10pt, 100% scaling and 1pt character
    spacing.  A second form Fm2 also uses the resource name /F1 but resolves to
    a different font object (font2); it lives on another page and must keep
    its original reading when font1 is corrected.  The column boundary is the
    page coordinate x=12.
    """
    builder = PdfBuilder()

    touni1_id = builder.add(builder.stream(b"", TOUNI_AB_C_D))
    descendant1_id = builder.add(
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ReceiptFontOne "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        b"/FontDescriptor << /Type /FontDescriptor /FontName /ReceiptFontOne >> "
        b"/DW 1000 /W [ 33088 [600] 32 [400] 48 [400] ] >>",
    )
    font1_id = builder.add(
        b"<< /Type /Font /Subtype /Type0 /BaseFont /ReceiptFontOne "
        b"/Encoding /Identity-H /ToUnicode "
        + str(touni1_id).encode()
        + b" 0 R /DescendantFonts ["
        + str(descendant1_id).encode()
        + b" 0 R] >>",
    )

    touni2_id = builder.add(builder.stream(b"", TOUNI_AB_C_D))
    descendant2_id = builder.add(
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ReceiptFontTwo "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        b"/FontDescriptor << /Type /FontDescriptor /FontName /ReceiptFontTwo >> "
        b"/DW 1000 /W [ 33088 [600] 32 [400] 48 [400] ] >>",
    )
    font2_id = builder.add(
        b"<< /Type /Font /Subtype /Type0 /BaseFont /ReceiptFontTwo "
        b"/Encoding /Identity-H /ToUnicode "
        + str(touni2_id).encode()
        + b" 0 R /DescendantFonts ["
        + str(descendant2_id).encode()
        + b" 0 R] >>",
    )

    tj = b"BT /F1 10 Tf 1 0 0 1 0 0 Tm 100 Tz 1 Tc [<8140> 100 <0020> <0030>] TJ ET"
    form1_id = builder.add(
        builder.stream(
            b"/Type /XObject /Subtype /Form /BBox [0 0 200 20] "
            b"/Resources << /Font << /F1 "
            + str(font1_id).encode()
            + b" 0 R >> >>",
            tj,
        ),
    )
    form2_id = builder.add(
        builder.stream(
            b"/Type /XObject /Subtype /Form /BBox [0 0 200 20] "
            b"/Resources << /Font << /F1 "
            + str(font2_id).encode()
            + b" 0 R >> >>",
            tj,
        ),
    )

    # Page 0 draws the same Fm1 twice: once at x=0, once at x=-2.
    content1_id = builder.add(
        builder.stream(
            b"",
            b"q 1 0 0 1 0 0 cm /Fm1 Do Q\n"
            b"q 1 0 0 1 -2 0 cm /Fm1 Do Q\n",
        ),
    )
    # Page 1 only uses the other font object.
    content2_id = builder.add(
        builder.stream(b"", b"q 1 0 0 1 0 0 cm /Fm2 Do Q\n"),
    )
    pages_id = builder.add(b"<< /Type /Pages /Kids [] /Count 2 >>")
    page1_id = builder.add(
        b"<< /Type /Page /Parent "
        + str(pages_id).encode()
        + b" 0 R /MediaBox [0 0 100 100] "
        b"/Resources << /XObject << /Fm1 "
        + str(form1_id).encode()
        + b" 0 R >> >> /Contents "
        + str(content1_id).encode()
        + b" 0 R >>",
    )
    page2_id = builder.add(
        b"<< /Type /Page /Parent "
        + str(pages_id).encode()
        + b" 0 R /MediaBox [0 0 100 100] "
        b"/Resources << /XObject << /Fm2 "
        + str(form2_id).encode()
        + b" 0 R >> >> /Contents "
        + str(content2_id).encode()
        + b" 0 R >>",
    )
    builder.objects[pages_id] = (
        b"<< /Type /Pages /Kids ["
        + str(page1_id).encode()
        + b" 0 R "
        + str(page2_id).encode()
        + b" 0 R] /Count 2 >>"
    )
    catalog_id = builder.add(
        b"<< /Type /Catalog /Pages " + str(pages_id).encode() + b" 0 R >>",
    )
    data = builder.build(catalog_id)
    return (
        data,
        FontIdentity(objid=font1_id, basefont="ReceiptFontOne"),
        FontIdentity(objid=font2_id, basefont="ReceiptFontTwo"),
    )


def test_code_correction_splits_glyph_and_moves_column() -> None:
    data, font1, font2 = build_receipt_pdf()
    session = extract_receipt_columns(io.BytesIO(data), column_bounds=(12.0,))
    assert session.current_version == 1

    v1 = session.contents()
    # Old reading: 8140 is one CID rendered as "AB"; both drawings stay left.
    assert v1[(0, 0)] == "ABCDABCD"
    assert (0, 1) not in v1
    # Page 1 uses the other font object and is read independently.
    assert v1[(1, 0)] == "ABCD"

    cid_30 = [glyph for glyph in session.glyphs(0) if glyph.cid == 0x30]
    first_30 = max(cid_30, key=lambda glyph: glyph.x0)
    second_30 = min(cid_30, key=lambda glyph: glyph.x0)
    # Pre-correction origins (page coordinates): 11 for the first drawing and
    # 9 for the second (its form is translated by -2).
    assert first_30.x0 == pytest.approx(11.0)
    assert second_30.x0 == pytest.approx(9.0)
    assert first_30.column == 0 and second_30.column == 0
    assert first_30.figure == "Fm1" and second_30.figure == "Fm1"
    assert first_30.font == font1 and second_30.font == font1

    reissues = session.apply_code_correction(
        font1,
        fragment=b"\x81\x40",
        cids=(0x81, 0x40),
        widths=(300, 300),
        characters=("A", "B"),
    )

    # Only page 0 uses the corrected font object; page 1 is not re-read.
    assert session.reread_pages == {0}
    assert session.parsed_pages == {0, 1}
    assert len(reissues) == 2
    by_column = {reissue.column: reissue for reissue in reissues}
    assert by_column[0].page == 0
    assert by_column[0].old_content == "ABCDABCD"
    assert by_column[0].new_content == "ABCABCD"
    assert by_column[1].old_content == ""
    assert by_column[1].new_content == "D"
    assert all(
        reissue.old_version == 1 and reissue.new_version == 2 for reissue in reissues
    )

    v2 = session.contents()
    assert v2[(0, 0)] == "ABCABCD"
    assert v2[(0, 1)] == "D"
    # Page 1 keeps its original reading.
    assert v2[(1, 0)] == "ABCD"
    # The old snapshot remains readable for readers that started on v1.
    assert session.contents(version=1)[(0, 0)] == "ABCDABCD"

    page0 = session.glyphs(0)
    # Records are emitted in paint order: all glyphs of the first drawing, then
    # all glyphs of the second.  Each drawing contributes four glyphs now.
    first_drawing = page0[:4]
    second_drawing = page0[4:]
    assert [g.text for g in first_drawing] == ["A", "B", "C", "D"]
    assert [g.text for g in second_drawing] == ["A", "B", "C", "D"]

    moved_30 = next(g for g in first_drawing if g.cid == 0x30)
    stayed_30 = next(g for g in second_drawing if g.cid == 0x30)
    # Origins move 11 -> 12 and 9 -> 10; the half-open window [12, ...) moves
    # only the first glyph into the right column.
    assert moved_30.x0 == pytest.approx(12.0)
    assert moved_30.column == 1
    assert stayed_30.x0 == pytest.approx(10.0)
    assert stayed_30.column == 0

    split_a = next(g for g in page0 if g.cid == 0x81)
    split_b = next(g for g in page0 if g.cid == 0x40)
    assert split_a.byte_range == (0, 1)
    assert split_b.byte_range == (1, 2)
    assert split_a.string_bytes == b"\x81\x40"
    assert split_a.operator == "TJ"
    assert split_a.stream_objid is not None
    assert split_a.font == font1

    # The other font object (page 1) is untouched by the correction.
    other = session.glyphs(1)
    other_ab = next(g for g in other if g.cid == 0x8140)
    assert other_ab.text == "AB"
    assert other_ab.byte_range == (0, 2)
    assert other_ab.font == font2


def test_unicode_evidence_conflicts_until_confirmed() -> None:
    data, _font1, font2 = build_receipt_pdf()
    session = extract_receipt_columns(io.BytesIO(data), column_bounds=(12.0,))
    before = session.contents()

    conflict = session.add_unicode_evidence(
        font2,
        cid=0x30,
        candidates=(
            UnicodeCandidate(text="D", byte_range=(0, 2)),
            UnicodeCandidate(text="E", byte_range=(0, 2)),
        ),
        width=400,
    )
    # Fixed width means both readings land in the same columns.
    assert conflict.columns_identical is True
    assert conflict.current_text == "D"
    assert {candidate.text for candidate in conflict.candidates} == {"D", "E"}
    assert all(glyph.byte_range == (0, 2) for glyph in conflict.glyphs)
    # No reissue of the text while the evidence is merely recorded.
    assert session.contents() == before

    reissues = session.confirm_unicode(font2, cid=0x30, text="E")
    assert len(reissues) == 1
    reissue = reissues[0]
    assert reissue.old_content.endswith("D")
    assert reissue.new_content.endswith("E")
    # Positions are unchanged, so the glyph stays in the same left column.
    assert reissue.column == 0


def test_half_codes_from_two_strings_are_not_joined() -> None:
    registry = CorrectionRegistry()
    data, font1, _font2 = build_receipt_pdf()
    session = extract_receipt_columns(
        io.BytesIO(data),
        column_bounds=(12.0,),
        registry=registry,
    )
    # This fragment can only appear by stitching two independent strings.
    # It must never match.
    session.apply_code_correction(
        font1,
        fragment=b"\x40\x00",
        cids=(0x40,),
        widths=(300,),
        characters=("X",),
    )
    assert all(glyph.text != "X" for glyph in session.glyphs(0))

    # The in-string split still works and both glyphs share string index 0.
    session.apply_code_correction(
        font1,
        fragment=b"\x81\x40",
        cids=(0x81, 0x40),
        widths=(300, 300),
        characters=("A", "B"),
    )
    strings = {
        glyph.string_index
        for glyph in session.glyphs(0)
        if glyph.figure == "Fm1" and glyph.cid in (0x81, 0x40)
    }
    assert strings == {0}


def test_original_page_extraction_is_unchanged() -> None:
    data, _font1, _font2 = build_receipt_pdf()
    chars: list[LTChar] = []

    def walk(obj: object) -> None:
        if isinstance(obj, LTChar):
            chars.append(obj)
        elif hasattr(obj, "__iter__"):
            for child in obj:
                walk(child)

    for page in extract_pages(io.BytesIO(data)):
        walk(page)
    texts = [char.get_text() for char in chars]
    assert "AB" in texts
    assert "C" in texts
    assert "D" in texts


def test_snapshots_remain_readable_across_versions() -> None:
    data, font1, _font2 = build_receipt_pdf()
    session = extract_receipt_columns(io.BytesIO(data), column_bounds=(12.0,))
    v1 = session.current_version
    session.apply_code_correction(
        font1,
        fragment=b"\x81\x40",
        cids=(0x81, 0x40),
        widths=(300, 300),
        characters=("A", "B"),
    )
    v2 = session.current_version
    # A second correction on font 2 would not touch page 0; instead confirm a
    # further font-1 amendment publishes yet another readable snapshot.
    session.apply_code_correction(
        font1,
        fragment=b"\x00\x20",
        cids=(0x20,),
        widths=(400,),
        characters=("C",),
    )
    v3 = session.current_version
    assert v1 < v2 < v3
    assert session.contents(version=v1)[(0, 0)] == "ABCDABCD"
    assert session.contents(version=v2)[(0, 1)] == "D"
    assert session.contents(version=v3) == session.contents()


def test_extract_accepts_file_path_and_closes_handle() -> None:
    from tests.tempfilepath import TemporaryFilePath

    data, _font1, _font2 = build_receipt_pdf()
    with TemporaryFilePath(suffix=".pdf") as path:
        with open(path, "wb") as fp:
            fp.write(data)
        session = extract_receipt_columns(path, column_bounds=(12.0,))
        try:
            assert session.contents()[(0, 0)] == "ABCDABCD"
        finally:
            session.close()


def test_provenance_tracks_tj_operator() -> None:
    data, _font1, _font2 = build_receipt_pdf()
    # The form uses TJ; every provenance record must name that operator and
    # expose the content stream object id and the concrete font object.
    session = extract_receipt_columns(io.BytesIO(data), column_bounds=(12.0,))
    for glyph in session.glyphs(0):
        assert glyph.operator == "TJ"
        assert glyph.stream_objid is not None
        assert glyph.font.objid is not None
        assert glyph.string_bytes
        assert glyph.byte_range[0] < glyph.byte_range[1]

