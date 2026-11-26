# SPDX-FileCopyrightText: 2026 spdx contributors
#
# SPDX-License-Identifier: Apache-2.0
import json

from spdx_tools.spdx.handoff.planning import (
    Revision,
    analyze_revision,
    best_placement,
    choose_package_order,
    diagnose_stuck_revisions,
)
from spdx_tools.spdx.parser.jsonlikedict.json_like_dict_parser import JsonLikeDictParser

BASE = {
    "spdxVersion": "SPDX-2.3",
    "dataLicense": "CC0-1.0",
    "SPDXID": "SPDXRef-DOCUMENT",
    "name": "t",
    "documentNamespace": "https://x",
    "creationInfo": {"created": "2020-01-01T00:00:00Z", "creators": ["Person: A"]},
    "packages": [],
    "files": [],
    "relationships": [],
}


def _file(spdx_id):
    return {"SPDXID": spdx_id, "fileName": spdx_id, "checksums": [{"algorithm": "SHA1", "checksumValue": "a" * 40}]}


def _package(spdx_id, **extra):
    package = {
        "SPDXID": spdx_id,
        "name": spdx_id,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
    }
    package.update(extra)
    return package


def _build(**overrides):
    document = json.loads(json.dumps(BASE))
    document.update(overrides)
    return JsonLikeDictParser().parse(document)


def _relations(document):
    return [
        (item.spdx_element_id, item.relationship_type.name, str(item.related_spdx_element_id), item.comment)
        for item in document.relationships
    ]


def test_file_after_package_infers_contains():
    document = _build(
        packages=[_package("SPDXRef-P")],
        files=[_file("SPDXRef-F")],
        relationships=[
            {"spdxElementId": "SPDXRef-P", "relationshipType": "CONTAINS", "relatedSpdxElement": "SPDXRef-F"}
        ],
    )
    analysis = analyze_revision(Revision("r", document))
    placement, explicit, cost = best_placement(analysis, ["SPDXRef-P"])
    assert placement.packages_at_gap == {0: ["SPDXRef-P"]}
    assert explicit == []
    assert cost == 0


def test_reverse_relationship_is_not_replaced_by_file_position():
    document = _build(
        packages=[_package("SPDXRef-P")],
        files=[_file("SPDXRef-F")],
        relationships=[
            {
                "spdxElementId": "SPDXRef-F",
                "relationshipType": "CONTAINED_BY",
                "relatedSpdxElement": "SPDXRef-P",
                "comment": "why",
            }
        ],
    )
    analysis = analyze_revision(Revision("r", document))
    placement, explicit, cost = best_placement(analysis, ["SPDXRef-P"])
    # The commented reverse edge survives as a distinct relationship, alongside
    # the free forward edge the file position creates.
    assert [item.comment for item in explicit] == ["why"]
    assert cost > 0


def test_has_files_and_explicit_reverse_keeps_only_explicit_one():
    document = _build(
        packages=[_package("SPDXRef-P", hasFiles=["SPDXRef-F"])],
        files=[_file("SPDXRef-F")],
        relationships=[
            {
                "spdxElementId": "SPDXRef-F",
                "relationshipType": "CONTAINED_BY",
                "relatedSpdxElement": "SPDXRef-P",
                "comment": "why",
            }
        ],
    )
    assert _relations(document) == [("SPDXRef-F", "CONTAINED_BY", "SPDXRef-P", "why")]


def test_shared_order_skips_absent_packages():
    revision_one = Revision(
        "one",
        _build(
            packages=[_package("SPDXRef-A")],
            files=[_file("SPDXRef-F")],
            relationships=[
                {"spdxElementId": "SPDXRef-A", "relationshipType": "CONTAINS", "relatedSpdxElement": "SPDXRef-F"}
            ],
        ),
    )
    revision_two = Revision(
        "two",
        _build(
            packages=[_package("SPDXRef-B")],
            files=[_file("SPDXRef-G")],
            relationships=[
                {"spdxElementId": "SPDXRef-B", "relationshipType": "CONTAINS", "relatedSpdxElement": "SPDXRef-G"}
            ],
        ),
    )
    decision = choose_package_order([revision_one, revision_two], candidate_order=["SPDXRef-A", "SPDXRef-B"])
    assert decision.order == ["SPDXRef-A", "SPDXRef-B"]
    assert decision.explicit_cost == 0


def test_reversed_orders_across_two_catalogs_are_stuck():
    first = Revision(
        "first",
        _build(packages=[_package("SPDXRef-A"), _package("SPDXRef-B")], files=[_file("SPDXRef-F")]),
    )
    second = Revision(
        "second",
        _build(packages=[_package("SPDXRef-B"), _package("SPDXRef-A")], files=[_file("SPDXRef-G")]),
    )
    stuck = diagnose_stuck_revisions([first, second])
    assert {item.revision.name for item in stuck} == {"first", "second"}
    assert all(item.diagnostic["kind"] == "incompatible-package-order" for item in stuck)


def test_most_expensive_revision_is_scored():
    cheap = Revision(
        "cheap",
        _build(
            packages=[_package("SPDXRef-A")],
            files=[_file("SPDXRef-F")],
            relationships=[
                {"spdxElementId": "SPDXRef-A", "relationshipType": "CONTAINS", "relatedSpdxElement": "SPDXRef-F"}
            ],
        ),
    )
    expensive = Revision(
        "expensive",
        _build(
            packages=[_package("SPDXRef-A")],
            files=[_file("SPDXRef-G")],
            relationships=[
                {
                    "spdxElementId": "SPDXRef-DOCUMENT",
                    "relationshipType": "DESCRIBES",
                    "relatedSpdxElement": "SPDXRef-A",
                    "comment": "x" * 40,
                }
            ],
        ),
    )
    decision = choose_package_order([cheap, expensive])
    assert decision.scoring_revision == "expensive"
    assert decision.explicit_cost == decision.per_revision_cost["expensive"]


def test_allowed_gaps_restrict_placement():
    document = _build(
        packages=[_package("SPDXRef-P")],
        files=[_file("SPDXRef-F"), _file("SPDXRef-G")],
        relationships=[
            {"spdxElementId": "SPDXRef-P", "relationshipType": "CONTAINS", "relatedSpdxElement": "SPDXRef-G"}
        ],
    )
    revision = Revision("r", document, allowed_gaps={"SPDXRef-P": [2]})
    analysis = analyze_revision(revision)
    placement, explicit, cost = best_placement(analysis, ["SPDXRef-P"])
    assert placement.packages_at_gap == {2: ["SPDXRef-P"]}
    # Trailing the files reproduces no CONTAINS, so the wanted one is explicit.
    assert [item.related_spdx_element_id for item in explicit] == ["SPDXRef-G"]
    assert cost > 0


def test_three_way_cycle_is_reported():
    first = Revision(
        "first", _build(packages=[_package("SPDXRef-A"), _package("SPDXRef-B")], files=[_file("SPDXRef-F")])
    )
    second = Revision(
        "second", _build(packages=[_package("SPDXRef-B"), _package("SPDXRef-C")], files=[_file("SPDXRef-G")])
    )
    third = Revision(
        "third", _build(packages=[_package("SPDXRef-C"), _package("SPDXRef-A")], files=[_file("SPDXRef-H")])
    )
    stuck = diagnose_stuck_revisions([first, second, third])
    assert {item.revision.name for item in stuck} == {"first", "second", "third"}
    assert all(item.diagnostic["kind"] == "incompatible-package-order" for item in stuck)


def test_two_unrelated_revisions_are_not_stuck():
    first = Revision(
        "first", _build(packages=[_package("SPDXRef-A"), _package("SPDXRef-B")], files=[_file("SPDXRef-F")])
    )
    second = Revision(
        "second", _build(packages=[_package("SPDXRef-B"), _package("SPDXRef-C")], files=[_file("SPDXRef-G")])
    )
    assert diagnose_stuck_revisions([first, second]) == []
