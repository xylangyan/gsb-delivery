# SPDX-FileCopyrightText: 2026 spdx contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Emit and verify TagValue packages for a JSON to TagValue handoff."""

import io
from typing import Dict, List, Optional, Sequence, Tuple

from spdx_tools.spdx.handoff.encoding import (
    classify_relationship,
    describe_relationship,
    relationship_key,
    render_relationship,
)
from spdx_tools.spdx.handoff.planning import Placement, RevisionAnalysis, _explicit_relationships
from spdx_tools.spdx.model import Document, Relationship
from spdx_tools.spdx.parser.tagvalue.tagvalue_parser import parse_from_file as parse_tagvalue_file
from spdx_tools.spdx.writer.tagvalue.annotation_writer import write_annotation
from spdx_tools.spdx.writer.tagvalue.creation_info_writer import write_creation_info
from spdx_tools.spdx.writer.tagvalue.extracted_licensing_info_writer import write_extracted_licensing_info
from spdx_tools.spdx.writer.tagvalue.file_writer import write_file
from spdx_tools.spdx.writer.tagvalue.package_writer import write_package
from spdx_tools.spdx.writer.tagvalue.snippet_writer import write_snippet
from spdx_tools.spdx.writer.tagvalue.tagvalue_writer_helper_functions import (
    write_list_of_elements,
    write_optional_heading,
    write_separator,
)


def _snippets_by_file(document: Document) -> Dict[str, List]:
    snippets: Dict[str, List] = {}
    for snippet in document.snippets:
        snippets.setdefault(snippet.file_spdx_id, []).append(snippet)
    return snippets


def write_revision(analysis: RevisionAnalysis, placement: Placement) -> str:
    """Render a revision as TagValue using the given package placement.

    Packages are declared in the gaps named by ``placement``; files stay in the
    locked order carried by the revision, and each snippet follows its file.
    Every relationship that the file placement does not already reproduce is
    written explicitly (unless it is known to be unwritable, in which case it is
    reported by the planner instead).
    """
    document = analysis.revision.document
    packages_by_id = {package.spdx_id: package for package in document.packages}
    files_by_id = {file.spdx_id: file for file in document.files}
    snippets_by_file = _snippets_by_file(document)

    output = io.StringIO()
    output.write("## Document Information\n")
    write_creation_info(document.creation_info, output)
    write_separator(output)

    for gap in range(len(analysis.file_order) + 1):
        for package_id in placement.packages_at_gap.get(gap, []):
            write_package(packages_by_id[package_id], output)
            write_separator(output)
        if gap < len(analysis.file_order):
            file_id = analysis.file_order[gap]
            write_file(files_by_id[file_id], output)
            write_separator(output)
            for snippet in snippets_by_file.get(file_id, []):
                write_snippet(snippet, output)
                write_separator(output)

    explicit, _ = _explicit_relationships(analysis, placement)
    _write_relationships_head(explicit, output)

    write_optional_heading(document.extracted_licensing_info, "## License Information\n", output)
    write_list_of_elements(
        document.extracted_licensing_info, write_extracted_licensing_info, output, with_separator=True
    )
    write_optional_heading(document.annotations, "## Annotations\n", output)
    write_list_of_elements(document.annotations, write_annotation, output, with_separator=True)
    return output.getvalue()


def _write_relationships_head(relationships: Sequence[Relationship], output):
    if not relationships:
        return
    output.write("## Relationships\n")
    for relationship in relationships:
        ok, reason = classify_relationship(relationship)
        if not ok:
            raise ValueError(f"relationship is not TagValue-writable: {reason}")
        output.write(render_relationship(relationship))
    write_separator(output)


def reread_relationships(text: str, path: str) -> List[Relationship]:
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    document = parse_tagvalue_file(path)
    return list(document.relationships)


def verify_reread(
    analysis: RevisionAnalysis, placement: Placement, path: str
) -> Dict[str, object]:
    """Write, reread and report whether the relationship multiset matches.

    Only the relationships are compared, as the revision order pins the file
    order and the shared order pins the package order; every other field is
    written by the stock writer.
    """
    text = write_revision(analysis, placement)
    reread = reread_relationships(text, path)

    wanted = _count_keys(relationship_key(relationship) for relationship in analysis.relationships)
    got = _count_keys(relationship_key(relationship) for relationship in reread)
    missing = _subtract(wanted, got)
    extra = _subtract(got, wanted)
    return {
        "revision": analysis.revision.name,
        "path": path,
        "ok": not missing and not extra,
        "missing": [_describe_key(key) for key in missing],
        "extra": [_describe_key(key) for key in extra],
        "relationship_count": len(reread),
    }


def _count_keys(keys) -> Dict[Tuple, int]:
    counts: Dict[Tuple, int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1
    return counts


def _subtract(left: Dict[Tuple, int], right: Dict[Tuple, int]) -> List[Tuple]:
    """Keys (with multiplicity) present in ``left`` beyond ``right``."""
    surplus: List[Tuple] = []
    for key, count in left.items():
        for _ in range(max(0, count - right.get(key, 0))):
            surplus.append(key)
    return surplus


def _describe_key(key: Tuple) -> Dict:
    origin, relationship_type, target, comment = key
    return {
        "spdxElementId": origin,
        "relationshipType": relationship_type.name,
        "relatedSpdxElement": target,
        "comment": comment,
    }
