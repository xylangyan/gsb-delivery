"""
Tests for forensic recovery of interleaved jsonlines append streams.
"""

import json
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

import pytest

import jsonlines
from jsonlines import recovery
from jsonlines.recovery import (
    JsonType,
    OrphanBodyReceipt,
    Receipt,
    RecordReceipt,
    SourceSpec,
)


def make_receipt(
    sources: Dict[str, Tuple[JsonType, int, int]],
    records: Sequence[RecordReceipt] = (),
    orphan_bodies: Sequence[OrphanBodyReceipt] = (),
) -> Receipt:
    specs = {
        name: SourceSpec(
            name=name,
            json_type=json_type,
            min_length=min_length,
            max_length=max_length,
        )
        for name, (json_type, min_length, max_length) in sources.items()
    }
    return Receipt(
        sources=specs,
        records=tuple(records),
        orphan_bodies=tuple(orphan_bodies),
    )


def record(
    source: str,
    body_start: Optional[int] = None,
    body_length: Optional[int] = None,
    line_feed_start: Optional[int] = None,
) -> RecordReceipt:
    return RecordReceipt(
        source=source,
        body_start=body_start,
        body_length=body_length,
        line_feed_start=line_feed_start,
    )


# ---------------------------------------------------------------------------
# Candidate enumeration
# ---------------------------------------------------------------------------


def test_candidates_require_canonical_default_serialization() -> None:
    candidates = {
        (candidate.start, candidate.end): candidate.value
        for candidate in recovery._body_candidates(b"01 1.00 1.0 123")
    }
    # "01" and "1.00" are not produced by the old Writer default encoder.
    assert (0, 2) not in candidates
    assert (3, 7) not in candidates
    # "1.0" and "123" are canonical.
    assert candidates[(8, 11)] == 1.0
    assert candidates[(12, 15)] == 123


def test_candidate_enumeration_shapes() -> None:
    spans = {
        (candidate.start, candidate.end): (candidate.json_type, candidate.value)
        for candidate in recovery._body_candidates(
            b'1 1.0 true false null "x" [1] {"a": 1}'
        )
    }
    assert spans[(0, 1)] == (JsonType.INTEGER, 1)
    assert spans[(2, 5)] == (JsonType.NUMBER, 1.0)
    assert spans[(6, 10)] == (JsonType.BOOLEAN, True)
    assert spans[(11, 16)] == (JsonType.BOOLEAN, False)
    assert spans[(17, 21)] == (JsonType.NULL, None)
    assert spans[(22, 25)] == (JsonType.STRING, "x")
    assert spans[(26, 29)] == (JsonType.ARRAY, [1])
    assert spans[(30, 38)] == (JsonType.OBJECT, {"a": 1})


def test_raw_line_feed_inside_string_is_not_a_body() -> None:
    # Escaped LF (backslash + n) is fine; a raw LF inside quotes is rejected
    # by the strict decoder and can never be a default-serialized body.
    escaped = recovery._body_candidates(b'"a\\nb"\n')
    assert [(candidate.start, candidate.end) for candidate in escaped] == [(0, 6)]
    raw = recovery._body_candidates(b'"a\nb"\n')
    assert not raw


def test_multibyte_utf8_offsets_are_byte_offsets() -> None:
    data = '"abc中"\n'.encode("utf-8")
    candidates = recovery._body_candidates(data)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert (candidate.start, candidate.end) == (0, 8)
    assert data[candidate.end : candidate.end + 1] == b"\n"


def test_adjacent_bodies_are_enumerated_independently() -> None:
    # Two atomic writes placed back to back, without a separating LF.
    candidates = {
        (candidate.start, candidate.end): candidate.value
        for candidate in recovery._body_candidates(b"123[1]")
    }
    assert candidates[(0, 3)] == 123
    assert candidates[(3, 6)] == [1]
    assert (0, 1) in candidates  # number prefix "1"
    assert (0, 2) in candidates  # number prefix "12"


def test_max_body_length_bounds_prefix_enumeration() -> None:
    candidates = recovery._body_candidates(b"12345", max_body_length=2)
    assert candidates
    assert all(candidate.end - candidate.start <= 2 for candidate in candidates)
    assert (0, 1) in {(candidate.start, candidate.end) for candidate in candidates}
    assert (0, 3) not in {(candidate.start, candidate.end) for candidate in candidates}


# ---------------------------------------------------------------------------
# Confirmation and uncertainty
# ---------------------------------------------------------------------------


def test_single_adjacent_record_is_confirmed_with_split_ranges() -> None:
    data = b"123\n"
    receipt = make_receipt(
        {"A": (JsonType.INTEGER, 1, 3)},
        [record("A", body_start=0, body_length=3, line_feed_start=3)],
    )
    result = recovery.recover(data, receipt)
    assert result.satisfiable
    assert result.solution_count == 1
    assert len(result.confirmed) == 1
    confirmed = result.confirmed[0]
    assert confirmed.source == "A"
    assert confirmed.value == 123
    assert (confirmed.body.start, confirmed.body.end) == (0, 3)
    assert (confirmed.line_feed.start, confirmed.line_feed.end) == (3, 4)
    assert result.uncertain == ()


def test_two_line_fees_after_one_body_do_not_collapse_into_one_record() -> None:
    # "123" loading successfully must not lead to claiming a single record
    # when an extra LF is present.
    data = b"123\n\n"
    receipt = make_receipt({"A": (JsonType.INTEGER, 1, 3)}, [record("A")])
    result = recovery.recover(data, receipt)
    assert result.confirmed == ()
    assert len(result.uncertain) == 1
    region = result.uncertain[0]
    assert (region.byte_range.start, region.byte_range.end) == (0, 5)
    line_feeds = [
        alternative.line_feed
        for alternative in region.alternatives
        if alternative.line_feed is not None
    ]
    assert len(line_feeds) == len(region.alternatives)
    line_feed_ends = sorted({line_feed.end for line_feed in line_feeds})
    assert line_feed_ends == [4, 5]
    # The full-span body is one option; shorter number prefixes leave the
    # remaining bytes uncovered, so they remain listed alternatives rather
    # than being silently merged.
    placements = {
        (
            alternative.body.start,
            alternative.body.end,
            line_feed.start,
            alternative.value,
        )
        for alternative, line_feed in zip(region.alternatives, line_feeds)
    }
    assert (0, 3, 3, 123) in placements
    assert (0, 3, 4, 123) in placements
    assert (0, 1, 3, 1) in placements


def test_ambiguous_integer_split_reports_exact_uncertain_range() -> None:
    # Bodies "1"+"23" and "12"+"3" both fit the two integer receipts.
    data = b"123\n\n"
    receipt = make_receipt(
        {
            "A": (JsonType.INTEGER, 1, 2),
            "B": (JsonType.INTEGER, 1, 2),
        },
        [record("A"), record("B")],
    )
    result = recovery.recover(data, receipt)
    assert result.confirmed == ()
    assert len(result.uncertain) == 1
    region = result.uncertain[0]
    assert (region.byte_range.start, region.byte_range.end) == (0, 5)
    body_spans = {
        (alternative.source, alternative.body.start, alternative.body.end)
        for alternative in region.alternatives
    }
    assert ("A", 0, 1) in body_spans  # 1
    assert ("B", 1, 3) in body_spans  # 23
    assert ("A", 0, 2) in body_spans  # 12
    assert ("B", 2, 3) in body_spans  # 3


def test_additional_evidence_rechecks_with_stable_byte_coordinates() -> None:
    data = b"123\n\n"
    base_offset = 100
    sources = {
        "A": (JsonType.INTEGER, 1, 2),
        "B": (JsonType.INTEGER, 1, 2),
    }
    first = recovery.recover(
        data, make_receipt(sources, [record("A"), record("B")]), base_offset=base_offset
    )
    assert first.uncertain[0].byte_range.start == base_offset

    # A later fact: the body at absolute offset 100 is two bytes from A, and
    # B's line feed is the first LF. Re-checking keeps every coordinate.
    second_receipt = make_receipt(
        sources,
        [
            record("A", body_start=100, body_length=2, line_feed_start=104),
            record("B", body_start=102, body_length=1, line_feed_start=103),
        ],
    )
    second = recovery.recover(data, second_receipt, base_offset=base_offset)
    assert len(second.confirmed) == 2
    by_source = {item.source: item for item in second.confirmed}
    assert (by_source["A"].body.start, by_source["A"].body.end) == (100, 102)
    assert (by_source["A"].line_feed.start, by_source["A"].line_feed.end) == (104, 105)
    assert (by_source["B"].body.start, by_source["B"].body.end) == (102, 103)
    assert (by_source["B"].line_feed.start, by_source["B"].line_feed.end) == (103, 104)
    assert by_source["A"].value == 12
    assert by_source["B"].value == 3
    # Coordinates were already present as alternatives in the first run.
    first_spans = {
        (alternative.source, alternative.body.start, alternative.body.end)
        for alternative in first.uncertain[0].alternatives
    }
    assert ("A", 100, 102) in first_spans
    assert ("B", 102, 103) in first_spans


def test_late_line_feed_order_differs_from_body_order() -> None:
    # Body order is A then B, but B's line feed is committed before A's.
    data = b"12\n\n"  # A body [0,1), B body [1,2), LF_B [2], LF_A [3]
    receipt = make_receipt(
        {"A": (JsonType.INTEGER, 1, 1), "B": (JsonType.INTEGER, 1, 1)},
        [
            record("A", body_start=0, body_length=1, line_feed_start=3),
            record("B", body_start=1, body_length=1, line_feed_start=2),
        ],
    )
    result = recovery.recover(data, receipt)
    assert result.solution_count == 1
    by_source = {item.source: item for item in result.confirmed}
    assert (by_source["A"].body.start, by_source["A"].line_feed.start) == (0, 3)
    assert (by_source["B"].body.start, by_source["B"].line_feed.start) == (1, 2)


def test_orphan_body_is_reported_without_line_feed() -> None:
    # "1\n" is a successful record; "23" is a body whose Writer never
    # returned success.
    data = b"1\n23"
    receipt = make_receipt(
        {"A": (JsonType.INTEGER, 1, 2), "B": (JsonType.INTEGER, 1, 2)},
        [record("A", body_start=0, body_length=1, line_feed_start=1)],
        orphan_bodies=[OrphanBodyReceipt(source="B", start=2, length=2)],
    )
    result = recovery.recover(data, receipt)
    assert len(result.confirmed) == 1
    assert result.confirmed[0].value == 1
    assert len(result.uncertain) == 1
    region = result.uncertain[0]
    assert (region.byte_range.start, region.byte_range.end) == (2, 4)
    assert "successful Writer return" in region.reason
    alternative = region.alternatives[0]
    assert alternative.kind == "orphan_body"
    assert alternative.source == "B"
    assert alternative.value == 23
    assert alternative.line_feed is None


def test_uncertain_fragments_on_both_sides_of_confirmed_record() -> None:
    data = b"x1\ny"
    receipt = make_receipt(
        {"A": (JsonType.INTEGER, 1, 1)},
        [record("A", body_start=1, body_length=1, line_feed_start=2)],
    )
    result = recovery.recover(data, receipt)
    assert len(result.confirmed) == 1
    assert [
        (region.byte_range.start, region.byte_range.end) for region in result.uncertain
    ] == [(0, 1), (3, 4)]


def test_attribution_ambiguity_is_retained() -> None:
    # The same two single-digit bodies can be claimed by either source.
    data = b"12\n\n"
    receipt = make_receipt(
        {"A": (JsonType.INTEGER, 1, 1), "B": (JsonType.INTEGER, 1, 1)},
        [record("A"), record("B")],
    )
    result = recovery.recover(data, receipt)
    assert result.confirmed == ()
    region = result.uncertain[0]
    bodies_for_a = {
        alternative.body.start for alternative in region.alternatives
        if alternative.source == "A"
    }
    bodies_for_b = {
        alternative.body.start for alternative in region.alternatives
        if alternative.source == "B"
    }
    assert bodies_for_a == {0, 1}
    assert bodies_for_b == {0, 1}


def test_unpaired_line_feed_region() -> None:
    data = b"\n1\n"
    receipt = make_receipt(
        {"A": (JsonType.INTEGER, 1, 1)},
        [record("A", body_start=1, body_length=1, line_feed_start=2)],
    )
    result = recovery.recover(data, receipt)
    assert len(result.confirmed) == 1
    region = result.uncertain[0]
    assert (region.byte_range.start, region.byte_range.end) == (0, 1)
    assert "line feed" in region.reason


# ---------------------------------------------------------------------------
# Unsatisfiable receipts
# ---------------------------------------------------------------------------


def test_two_records_one_body_names_incompatible_facts() -> None:
    data = b"123\n"
    receipt = make_receipt(
        {"A": (JsonType.INTEGER, 1, 3)},
        [record("A"), record("A")],
    )
    result = recovery.recover(data, receipt)
    assert not result.satisfiable
    assert result.incompatible
    assert all("A#" in item.fact for item in result.incompatible)


def test_pinned_position_without_lf_is_incompatible() -> None:
    data = b"123"
    receipt = make_receipt(
        {"A": (JsonType.INTEGER, 1, 3)},
        [record("A", body_start=0, line_feed_start=3)],
    )
    result = recovery.recover(data, receipt)
    assert not result.satisfiable
    assert any("LF" in item.reason for item in result.incompatible)


def test_contract_violation_is_incompatible() -> None:
    data = b"123\n"
    receipt = make_receipt(
        {"A": (JsonType.STRING, 1, 3)},
        [record("A", body_start=0, body_length=3)],
    )
    result = recovery.recover(data, receipt)
    assert not result.satisfiable
    assert any("contract" in item.reason for item in result.incompatible)


def test_non_canonical_body_cannot_satisfy_receipt() -> None:
    data = b"01\n"
    receipt = make_receipt(
        {"A": (JsonType.INTEGER, 2, 2)},
        [record("A")],
    )
    result = recovery.recover(data, receipt)
    assert not result.satisfiable


def test_pinned_line_feed_before_body_is_diagnosed() -> None:
    # Validation cannot reject this in isolation, but no option places the
    # line feed write after the body write of the same record.
    data = b"\n12"
    receipt = make_receipt(
        {"A": (JsonType.INTEGER, 2, 2)},
        [record("A", body_start=1, body_length=2, line_feed_start=0)],
    )
    result = recovery.recover(data, receipt)
    assert not result.satisfiable
    assert result.incompatible
    assert any("A#0" in item.fact for item in result.incompatible)


# ---------------------------------------------------------------------------
# Other JSON types and serialization
# ---------------------------------------------------------------------------


def test_default_object_serialization_is_recovered() -> None:
    data = b'{"a": 1, "b": [1, 2]}\n'
    receipt = make_receipt(
        {"A": (JsonType.OBJECT, 1, 40)},
        [
            record(
                "A",
                body_start=0,
                body_length=len(data) - 1,
                line_feed_start=len(data) - 1,
            )
        ],
    )
    result = recovery.recover(data, receipt)
    assert result.confirmed[0].value == {"a": 1, "b": [1, 2]}


def test_float_body_round_trips() -> None:
    data = b"1.0\n"
    receipt = make_receipt(
        {"A": (JsonType.NUMBER, 3, 3)},
        [record("A", body_start=0, body_length=3, line_feed_start=3)],
    )
    result = recovery.recover(data, receipt)
    assert result.confirmed[0].value == 1.0


# ---------------------------------------------------------------------------
# Enumeration limits and serialization
# ---------------------------------------------------------------------------


def test_truncation_is_flagged_and_confirms_nothing() -> None:
    data = b"1\n\n\n\n"
    receipt = make_receipt({"A": (JsonType.INTEGER, 1, 1)}, [record("A")])
    result = recovery.recover(data, receipt, solution_limit=2)
    assert result.truncated
    assert result.solution_count == 2
    assert result.confirmed == ()
    assert result.uncertain
    assert all("truncated" in region.reason for region in result.uncertain)


def test_node_budget_caps_a_hard_search() -> None:
    # A long ambiguous digit run with many loose records cannot exhaust the
    # caller; the tiny node budget forces a bounded, explicitly truncated run.
    data = b"1" * 80 + b"\n" * 20
    specs = {f"S{i}": (JsonType.INTEGER, 1, 2) for i in range(6)}
    receipt = make_receipt(
        specs, [record(f"S{i % 6}") for i in range(15)]
    )
    result = recovery.recover(
        data, receipt, node_budget=50, trial_budget=500
    )
    assert result.truncated
    assert result.confirmed == ()
    assert result.uncertain


def test_trial_budget_caps_huge_option_lists() -> None:
    # One unpinned record over many bodies and many line feeds: its option
    # list is the cartesian product, bounded by the trial budget.
    data = b"1" * 40 + b"\n" * 40
    receipt = make_receipt({"A": (JsonType.INTEGER, 1, 1)}, [record("A")])
    result = recovery.recover(data, receipt, trial_budget=10)
    assert result.truncated
    assert result.confirmed == ()


def test_result_is_json_serializable() -> None:
    data = b"123\n\n"
    receipt = make_receipt({"A": (JsonType.INTEGER, 1, 3)}, [record("A")])
    result = recovery.recover(data, receipt)
    payload = json.dumps(result.as_dict())
    parsed = json.loads(payload)
    assert parsed["analyzed_range"] == {"start": 0, "end": 5}
    assert parsed["satisfiable"] is True
    assert parsed["confirmed"] == []


def test_receipt_from_dict() -> None:
    receipt = recovery.receipt_from_dict(
        {
            "sources": {
                "A": {"json_type": "integer", "min_length": 1, "max_length": 3}
            },
            "records": [
                {"source": "A", "body_start": 0, "body_length": 3, "line_feed_start": 3}
            ],
            "orphan_bodies": [{"source": "A", "start": 9, "length": 2}],
        }
    )
    assert receipt.sources["A"].json_type is JsonType.INTEGER
    assert receipt.records[0].line_feed_start == 3
    assert receipt.orphan_bodies[0].length == 2


def test_recover_file_never_writes(tmp_path: Path) -> None:
    stream = tmp_path / "stream.jsonl"
    stream.write_bytes(b"123\n")
    before = stream.read_bytes()
    receipt = make_receipt(
        {"A": (JsonType.INTEGER, 1, 3)},
        [record("A", body_start=0, body_length=3, line_feed_start=3)],
    )
    result = recovery.recover_file(str(stream), receipt)
    assert stream.read_bytes() == before
    assert result.confirmed[0].value == 123


def test_public_api_exports() -> None:
    for name in [
        "Receipt",
        "RecordReceipt",
        "OrphanBodyReceipt",
        "SourceSpec",
        "JsonType",
        "ByteRange",
        "ConfirmedRecord",
        "UncertainRegion",
        "Alternative",
        "IncompatibleFact",
        "RecoveryResult",
        "recover",
        "recover_file",
        "receipt_from_dict",
    ]:
        assert hasattr(jsonlines, name)


def test_cli_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from jsonlines.__main__ import main

    stream = tmp_path / "stream.jsonl"
    stream.write_bytes(b"123\n")
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "sources": {
                    "A": {"json_type": "integer", "min_length": 1, "max_length": 3}
                },
                "records": [
                    {
                        "source": "A",
                        "body_start": 0,
                        "body_length": 3,
                        "line_feed_start": 3,
                    }
                ],
            }
        )
    )
    exit_code = main(["recover", str(stream), "--receipt", str(receipt_path)])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "confirmed A#0" in output
    assert "body:      [0, 3)" in output
    assert "line feed: [3, 4)" in output

    exit_code = main(
        ["recover", str(stream), "--receipt", str(receipt_path), "--json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["confirmed"][0]["value"] == 123
