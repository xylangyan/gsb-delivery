# SPDX-FileCopyrightText: 2026 spdx contributors
#
# SPDX-License-Identifier: Apache-2.0
import os

import pytest

from spdx_tools.spdx.handoff.batch import run_handoff
from spdx_tools.spdx.handoff.planning import Revision
from spdx_tools.spdx.parser.json.json_parser import parse_from_file
from spdx_tools.spdx.parser.tagvalue.tagvalue_parser import parse_from_file as parse_tagvalue_file

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
CATALOGS = [
    "SPDXJSONExample-v2.3.spdx.json",
    "SPDXJSONExample-v2.2.spdx.json",
    "circleConversionTestInitialDocument.json",
]


def _revisions():
    return [Revision(name, parse_from_file(os.path.join(DATA, name))) for name in CATALOGS]


def _relationships(document):
    return [
        (item.spdx_element_id, item.relationship_type.name, str(item.related_spdx_element_id), item.comment)
        for item in document.relationships
    ]


def test_handoff_round_trips_every_bundled_catalog(tmp_path):
    revisions = _revisions()
    result = run_handoff(revisions, verify_small_cases=True, output_directory=str(tmp_path))

    assert result.explicit_cost > 0
    assert not result.diagnostics
    for revision in revisions:
        text = result.documents[revision.name]
        assert text
        reread = parse_tagvalue_file(str(tmp_path / (revision.name + ".spdx")))
        # Package and file declarations each appear exactly once.
        assert [package.spdx_id for package in reread.packages] == [
            package.spdx_id for package in revision.document.packages
        ]
        assert [file.spdx_id for file in reread.files] == [file.spdx_id for file in revision.document.files]
        # Relationship direction, comments and multiset must agree.
        assert sorted(_relationships(reread)) == sorted(_relationships(revision.document))


def test_scoring_revision_is_reported_with_its_cost(tmp_path):
    revisions = _revisions()
    result = run_handoff(revisions, verify_small_cases=True, output_directory=str(tmp_path))

    assert result.scoring_revision in result.per_revision_cost
    assert result.explicit_cost == max(result.per_revision_cost.values())
    assert result.explicit_cost == result.per_revision_cost[result.scoring_revision]
    assert all(verification["ok"] for verification in result.verifications)


def test_shared_order_is_stable_across_catalogs(tmp_path):
    revisions = _revisions()
    first = run_handoff(revisions, output_directory=str(tmp_path))
    second = run_handoff(list(reversed(revisions)), output_directory=str(tmp_path))
    assert first.package_order == second.package_order
    assert first.explicit_cost == second.explicit_cost


@pytest.mark.parametrize(
    "name",
    [
        "SPDXJSONExample-v2.3.spdx.json",
        "SPDXJSONExample-v2.2.spdx.json",
        "circleConversionTestInitialDocument.json",
    ],
)
def test_every_package_and_file_declared_once(tmp_path, name):
    revision = Revision(name, parse_from_file(os.path.join(DATA, name)))
    result = run_handoff([revision], verify_small_cases=True, output_directory=str(tmp_path))
    text = result.documents[name]
    for package in revision.document.packages:
        assert text.count(f"SPDXID: {package.spdx_id}\n") >= 1


def _package_ids_in_text(text):
    ids = []
    previous_was_package_name = False
    for line in text.splitlines():
        if line.startswith("PackageName: "):
            previous_was_package_name = True
        elif previous_was_package_name and line.startswith("SPDXID: "):
            ids.append(line[len("SPDXID: "):])
            previous_was_package_name = False
        elif line.strip():
            previous_was_package_name = False
    return ids


def test_shared_package_order_is_preserved(tmp_path):
    revisions = _revisions()
    result = run_handoff(revisions, verify_small_cases=False)
    for text in result.documents.values():
        emitted = _package_ids_in_text(text)
        planned = [package_id for package_id in result.package_order if package_id in emitted]
        assert emitted == planned
