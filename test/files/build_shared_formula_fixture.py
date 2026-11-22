#!/usr/bin/env python3
"""Build the shared-formula workbook fixtures used by the specs.

Two versions of the same monthly report are generated deterministically. The
worksheet XML is written by hand because spreadsheet writers tend to normalise
shared formulas away, and the fixture needs to pin down the exact OOXML shape.

Version 1 (shared_formula_cache.xlsx)
  * a shared master at C4, outside the A5:D9 window the report reader consumes
  * an explicit formula override at C5, inside the shared range
  * a dependent with an empty cached value (C6)
  * a dependent with no cached <v> at all (C7)
  * dependents with normal cached values (C4, C8, C9)

Version 2 (shared_formula_cache_v2.xlsx)
  * the shared range is narrowed to C4:C6
  * C7:C9 still carry stale <f t="shared" si="0"/> markers from the old range
  * those stale dependents must keep their cached values but must NOT have a
    formula reconstructed from the old C4 anchor
"""

from pathlib import Path
import zipfile

OUT = Path(__file__).resolve().parent


CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>
"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""

WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets>
<sheet name="Monthly" sheetId="1" r:id="rId1"/>
</sheets>
</workbook>
"""

WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
</styleSheet>
"""


def monthly_sheet(shared_ref):
    """The monthly report sheet, parameterised by the shared master's ref."""
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<dimension ref="A1:D9"/>
<sheetData>
<row r="1"><c r="A1" t="inlineStr"><is><t>Region</t></is></c><c r="B1" t="inlineStr"><is><t>Plan</t></is></c><c r="C1" t="inlineStr"><is><t>Actual</t></is></c><c r="D1" t="inlineStr"><is><t>Note</t></is></c></row>
<row r="2"><c r="A2" t="inlineStr"><is><t>Published</t></is></c><c r="B2"><v>1</v></c><c r="C2"><v>2</v></c><c r="D2" t="inlineStr"><is><t>"quoted text"</t></is></c></row>
<row r="3"/>
<row r="4"><c r="A4" t="inlineStr"><is><t>North</t></is></c><c r="B4"><v>10</v></c><c r="C4"><f t="shared" ref="{shared_ref}" si="0">SUM($A4:B4)+&quot;x&quot;</f><v>12</v></c><c r="D4"><v>0</v></c></row>
<row r="5"><c r="A5" t="inlineStr"><is><t>South</t></is></c><c r="B5"><v>20</v></c><c r="C5"><f>SUM($A5:B5)*2</f><v>44</v></c><c r="D5"><v>0</v></c></row>
<row r="6"><c r="A6" t="inlineStr"><is><t>East</t></is></c><c r="B6"><v>30</v></c><c r="C6"><f t="shared" si="0"/><v></v></c><c r="D6"><v>0</v></c></row>
<row r="7"><c r="A7" t="inlineStr"><is><t>West</t></is></c><c r="B7"><v>40</v></c><c r="C7"><f t="shared" si="0"/></c><c r="D7"><v>0</v></c></row>
<row r="8"><c r="A8" t="inlineStr"><is><t>Central</t></is></c><c r="B8"><v>50</v></c><c r="C8"><f t="shared" si="0"/><v>60</v></c><c r="D8"><v>0</v></c></row>
<row r="9"><c r="A9" t="inlineStr"><is><t>Island</t></is></c><c r="B9"><v>70</v></c><c r="C9"><f t="shared" si="0"/><v></v></c><c r="D9"><v>0</v></c></row>
</sheetData>
</worksheet>
"""


def build(path, shared_ref):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("xl/workbook.xml", WORKBOOK)
        z.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        z.writestr("xl/styles.xml", STYLES)
        z.writestr("xl/worksheets/sheet1.xml", monthly_sheet(shared_ref))


if __name__ == "__main__":
    build(OUT / "shared_formula_cache.xlsx", "C4:C9")
    build(OUT / "shared_formula_cache_v2.xlsx", "C4:C6")
