# SPDX-FileCopyrightText: 2026 spdx contributors
#
# SPDX-License-Identifier: Apache-2.0
from datetime import datetime

import pytest

from spdx_tools.spdx.handoff.encoding import (
    UnwritableRelationship,
    classify_comment,
    classify_relationship,
    relationship_byte_cost,
    render_relationship,
)
from spdx_tools.spdx.model import (
    Actor,
    ActorType,
    Checksum,
    ChecksumAlgorithm,
    CreationInfo,
    Document,
    File,
    Relationship,
    RelationshipType,
)
from spdx_tools.spdx.parser.tagvalue.tagvalue_parser import parse_from_file

HEADER = """\
SPDXVersion: SPDX-2.3
DataLicense: CC0-1.0
SPDXID: SPDXRef-DOCUMENT
DocumentName: d
DocumentNamespace: https://x
Creator: Person: a
Created: 2020-01-01T00:00:00Z
"""
FILE_BLOCK = """\
FileName: f
SPDXID: SPDXRef-F
FileChecksum: SHA1: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
"""


def _reread_comment(tmp_path, comment):
    relationship = Relationship(
        "SPDXRef-DOCUMENT", RelationshipType.DESCRIBES, "SPDXRef-F", comment=comment
    )
    text = HEADER + render_relationship(relationship) + FILE_BLOCK
    path = tmp_path / "doc.spdx"
    path.write_text(text, encoding="utf-8", newline="")
    document = parse_from_file(str(path))
    reread = [
        item
        for item in document.relationships
        if item.spdx_element_id == "SPDXRef-DOCUMENT"
        and item.relationship_type == RelationshipType.DESCRIBES
        and str(item.related_spdx_element_id) == "SPDXRef-F"
    ]
    assert len(reread) == 1
    return reread[0].comment


@pytest.mark.parametrize(
    "comment",
    [
        None,
        "",
        "plain",
        "a\tb",
        " leading",
        "trailing ",
        "two words",
        "NONE",
        "NOASSERTION",
        "Relationship",
        "PackageName",
        "a\nb",
        "\na",
        "a\n",
        "a\n\nb",
        "</text>",
        "a</text>b",
        "<text></text>",
        "<text>",
        "a<text>b",
        "a\n<text>b",
        "中文",
        "a\x00b",
    ],
)
def test_writable_comments_round_trip(tmp_path, comment):
    writable, reason = classify_comment(comment)
    assert writable, reason
    assert _reread_comment(tmp_path, comment) == comment


@pytest.mark.parametrize(
    "comment",
    [
        "a\rb",
        "\r",
        "a\r\nb",
        "a\n</text>\nb",
        "a\nb</text>",
        "<text>\n</text>",
        "\n</text>",
        "<text>x</text>",
    ],
)
def test_unwritable_comments_are_rejected(tmp_path, comment):
    writable, reason = classify_comment(comment)
    assert not writable
    assert reason
    with pytest.raises(UnwritableRelationship):
        render_relationship(
            Relationship("SPDXRef-DOCUMENT", RelationshipType.DESCRIBES, "SPDXRef-F", comment=comment)
        )


def test_byte_cost_counts_utf8_not_characters():
    ascii_relationship = Relationship("SPDXRef-DOCUMENT", RelationshipType.DESCRIBES, "SPDXRef-F")
    chinese_relationship = Relationship(
        "SPDXRef-DOCUMENT", RelationshipType.DESCRIBES, "SPDXRef-F", comment="中文\n中文"
    )
    assert relationship_byte_cost(ascii_relationship) == len(
        "Relationship: SPDXRef-DOCUMENT DESCRIBES SPDXRef-F\n".encode("utf-8")
    )
    # A multi-line value forces the <text> form; the four characters cost 12
    # UTF-8 bytes, not four.
    comment_line = "RelationshipComment: <text>中文\n中文</text>\n"
    assert len("中文\n中文".encode("utf-8")) == 13
    assert relationship_byte_cost(chinese_relationship) - relationship_byte_cost(
        ascii_relationship
    ) == len(comment_line.encode("utf-8"))


def test_related_id_may_be_none_or_noassertion():
    for value in ("NONE", "NOASSERTION"):
        writable, reason = classify_relationship(
            Relationship("SPDXRef-DOCUMENT", RelationshipType.DESCRIBES, value)
        )
        assert writable, reason


def test_identifier_with_whitespace_is_unwritable():
    writable, reason = classify_relationship(
        Relationship("SPDXRef-DOCUMENT", RelationshipType.DESCRIBES, "SPDXRef F")
    )
    assert not writable
    assert "whitespace" in reason
