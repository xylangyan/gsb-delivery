# SPDX-FileCopyrightText: 2026 spdx contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Exact TagValue encoding of SPDX-2 relationship statements.

The tag-value writer shipped with :mod:`spdx_tools` emits ``Relationship`` and
``RelationshipComment`` lines, but several relationship texts that the JSON
reader accepts do not survive a tag-value write/read round trip.  This module
makes the encoding explicit and *checkable*: every relationship is classified as
either byte-exact writable, or rejected with a reason that can be reported
before a handoff.

The rules are derived from ``spdx_tools.spdx.parser.tagvalue``:

* A bare (non ``<text>``) value is read with the ``LINE`` token, which strips
  surrounding whitespace.  It must also not equal a reserved tag name (the
  lexer re-tokenises it as a tag) and must not begin with ``<text>`` (the lexer
  would switch into text state).
* A ``<text>`` value preserves surrounding whitespace but requires a non-empty
  body, may not contain the closing marker ``</text>``, and may not be empty.
* Every ``\\r`` is translated to ``\\n`` by Python's universal-newline handling,
  so any carriage return is impossible to preserve.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

from spdx_tools.spdx.model import Relationship, RelationshipType, SpdxNoAssertion, SpdxNone

# Tag names understood by the tag-value lexer.  A bare value equal to one of
# these is tokenised as a tag instead of a value.
RESERVED_TAGS = frozenset(
    {
        "SPDXVersion",
        "DataLicense",
        "DocumentName",
        "SPDXID",
        "DocumentComment",
        "DocumentNamespace",
        "ExternalDocumentRef",
        "Creator",
        "Created",
        "CreatorComment",
        "LicenseListVersion",
        "Annotator",
        "AnnotationDate",
        "AnnotationComment",
        "AnnotationType",
        "SPDXREF",
        "Relationship",
        "RelationshipComment",
        "PackageName",
        "PackageVersion",
        "PackageDownloadLocation",
        "FilesAnalyzed",
        "PackageSummary",
        "PackageSourceInfo",
        "PackageFileName",
        "PackageSupplier",
        "PackageOriginator",
        "PackageChecksum",
        "PackageVerificationCode",
        "PackageDescription",
        "PackageComment",
        "PackageLicenseDeclared",
        "PackageLicenseConcluded",
        "PackageLicenseInfoFromFiles",
        "PackageLicenseComments",
        "PackageCopyrightText",
        "PackageHomePage",
        "ExternalRef",
        "ExternalRefComment",
        "PackageAttributionText",
        "PrimaryPackagePurpose",
        "BuiltDate",
        "ReleaseDate",
        "ValidUntilDate",
        "FileName",
        "FileType",
        "FileChecksum",
        "LicenseConcluded",
        "LicenseInfoInFile",
        "FileCopyrightText",
        "LicenseComments",
        "FileComment",
        "FileNotice",
        "FileContributor",
        "FileAttributionText",
        "LicenseID",
        "ExtractedText",
        "LicenseName",
        "LicenseCrossReference",
        "LicenseComment",
        "SnippetSPDXID",
        "SnippetName",
        "SnippetComment",
        "SnippetCopyrightText",
        "SnippetLicenseComments",
        "SnippetFromFileSPDXID",
        "SnippetLicenseConcluded",
        "LicenseInfoInSnippet",
        "SnippetAttributionText",
        "SnippetByteRange",
        "SnippetLineRange",
        "NOASSERTION",
        "NONE",
    }
)


class UnwritableRelationship(ValueError):
    """Raised when a relationship cannot be represented byte-exactly in TagValue."""


@dataclass(frozen=True)
class CommentEncoding:
    """How a comment must be written to read back unchanged."""

    style: str  # "bare" or "text"

    def render(self, tag: str, comment: str) -> str:
        if self.style == "text":
            return f"{tag}: <text>{comment}</text>\n"
        if comment == "":
            return f"{tag}: \n"
        return f"{tag}: {comment}\n"


def _bare_is_exact(comment: str) -> bool:
    # Bare values are lexed with ``:.+`` and stripped; a value without
    # surrounding whitespace, not a reserved tag reads back unchanged.  A value
    # that starts with ``<text>`` switches the lexer into text state, which is
    # only benign for the degenerate ``<text></text>`` (an empty body is not
    # unwrapped).  An empty comment is written as an empty bare line.
    if comment == "":
        return True
    if "\n" in comment or comment != comment.strip() or comment in RESERVED_TAGS:
        return False
    if comment.startswith("<text>"):
        return comment == "<text></text>"
    return True


def _text_is_exact(comment: str) -> bool:
    # ``<text>`` bodies must be non-empty and must not terminate the block early.
    return comment != "" and "</text>" not in comment


def _classify_comment(comment: Optional[str]) -> Optional[CommentEncoding]:
    """Return the exact encoding for ``comment`` or ``None`` if impossible."""
    if comment is None:
        return None
    if "\r" in comment:
        return None
    if _bare_is_exact(comment):
        return CommentEncoding("bare")
    if _text_is_exact(comment):
        return CommentEncoding("text")
    return None


def classify_comment(comment: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Return ``(writable, reason)`` for a relationship comment."""
    if comment is None:
        return True, None
    if _classify_comment(comment) is not None:
        return True, None
    if "\r" in comment:
        return False, "carriage return is translated to a line break by the tag-value reader"
    if "\n" in comment and "</text>" in comment:
        return False, "multi-line comment contains the literal closing marker '</text>'"
    if "\n" not in comment:
        return False, "single-line comment has whitespace a bare line cannot keep, or is a reserved tag"
    return False, "comment cannot be represented byte-exactly"


def related_id_text(relationship: Relationship) -> str:
    related = relationship.related_spdx_element_id
    if isinstance(related, (SpdxNone, SpdxNoAssertion)):
        return str(related)
    return str(related)


def relationship_type_text(relationship: Relationship) -> str:
    return relationship.relationship_type.name


def _identifier_error(value: str, label: str) -> Optional[str]:
    if not isinstance(value, str):
        return f"{label} is not a string"
    if value == "":
        return f"{label} is empty"
    if any(ch.isspace() for ch in value):
        return f"{label} contains whitespace"
    return None


def classify_relationship(relationship: Relationship) -> Tuple[bool, Optional[str]]:
    """Return ``(writable, reason)`` for a whole relationship statement."""
    reason = _identifier_error(relationship.spdx_element_id, "spdxElementId")
    if reason:
        return False, reason
    reason = _identifier_error(related_id_text(relationship), "relatedSpdxElement")
    if reason:
        return False, reason
    return classify_comment(relationship.comment)


def render_relationship(relationship: Relationship) -> str:
    """Render a relationship exactly, raising if it is not representable."""
    writable, reason = classify_relationship(relationship)
    if not writable:
        raise UnwritableRelationship(reason)
    head = (
        f"Relationship: {relationship.spdx_element_id} "
        f"{relationship_type_text(relationship)} {related_id_text(relationship)}\n"
    )
    encoding = _classify_comment(relationship.comment)
    if encoding is None:
        return head
    return head + encoding.render("RelationshipComment", relationship.comment)


def relationship_byte_cost(relationship: Relationship) -> int:
    """UTF-8 byte count of the relationship as it will be written."""
    return len(render_relationship(relationship).encode("utf-8"))


def relationship_key(relationship: Relationship) -> Tuple[str, RelationshipType, str, Optional[str]]:
    return (
        relationship.spdx_element_id,
        relationship.relationship_type,
        related_id_text(relationship),
        relationship.comment,
    )


def describe_relationship(relationship: Relationship) -> Dict[str, Union[str, None]]:
    """A JSON-friendly description used in handoff reports."""
    return {
        "spdxElementId": relationship.spdx_element_id,
        "relationshipType": relationship_type_text(relationship),
        "relatedSpdxElement": related_id_text(relationship),
        "comment": relationship.comment,
    }


def classify_relationships(relationships: List[Relationship]) -> Tuple[List[Relationship], List[Dict]]:
    """Split relationships into writable ones and diagnostics for the rest."""
    writable: List[Relationship] = []
    diagnostics: List[Dict] = []
    for relationship in relationships:
        ok, reason = classify_relationship(relationship)
        if ok:
            writable.append(relationship)
        else:
            diagnostics.append(
                {
                    "kind": "unwritable-relationship",
                    "reason": reason,
                    "relationship": describe_relationship(relationship),
                }
            )
    return writable, diagnostics
