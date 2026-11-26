from __future__ import annotations

from pathlib import Path

import pytest

from charset_normalizer import api, from_bytes
from charset_normalizer.audit import (
    ENCODING_NOT_FOUND,
    NO_CANDIDATE,
    audit_configurations,
    compare_configurations,
    first_candidate_difference,
    inspect_configuration,
    parse_switch_specs,
    primary_and_representative,
    switch_impact_report,
)

DATA = Path(__file__).resolve().parent.parent / "data"


def read_sample(name: str) -> bytes:
    return (DATA / name).read_bytes()


def test_single_config_returns_primary_and_real_submatch_owner() -> None:
    payload = read_sample("sample-chinese.txt")

    primary, representative = primary_and_representative(payload, encoding="cp950")

    assert primary is not None
    assert primary.encoding == "big5"
    assert representative is not None
    assert representative.encoding == "big5"
    assert "cp950" in representative.could_be_from_charset

    record = inspect_configuration(payload, encoding="cp950")
    assert record.is_submatch is True
    assert (
        record.representative is representative
        or record.representative.encoding == "big5"
    )


def test_named_representative_can_become_primary_after_earlier_owner_disabled() -> None:
    payload = read_sample("sample-chinese.txt")

    record = inspect_configuration(
        payload, {"cp_exclusion": ["big5"]}, encoding="big5hkscs"
    )

    assert record.best is not None
    assert record.best.encoding == "big5hkscs"
    assert record.representative is record.best
    assert record.is_submatch is False


def test_greek_alias_and_similar_encoding_follow_native_entry() -> None:
    payload = read_sample("sample-greek.txt")
    native = from_bytes(payload, cp_exclusion=["cp1253"]).best()

    record = inspect_configuration(payload, {"cp_exclusion": ["cp1253"]})

    assert native is not None
    assert record.best is not None
    assert record.best.encoding == native.encoding == "iso8859_7"
    assert record.text == str(native)

    looked_up = inspect_configuration(payload, encoding="iso8859_7")
    assert looked_up.representative is not None
    assert looked_up.representative.encoding == "cp1253"
    assert looked_up.is_submatch is True


def test_signature_file_keeps_native_bom_detection() -> None:
    payload = read_sample("sample-english.bom.txt")

    record = inspect_configuration(payload)

    assert record.best is not None
    assert record.best.encoding == "utf_8"
    assert record.best.bom is True
    assert not record.text.startswith("﻿")


def test_no_candidate_and_missing_named_encoding_are_distinct() -> None:
    payload = "café".encode("utf-8")

    empty = inspect_configuration(payload, {"cp_isolation": ["ascii"]})
    missing = inspect_configuration(payload, encoding="cp1251")

    assert empty.best is None
    assert empty.status == NO_CANDIDATE
    assert missing.best is not None
    assert missing.representative is None
    assert missing.lookup_status == ENCODING_NOT_FOUND


def test_expected_text_mismatch_is_recorded_separately() -> None:
    payload = read_sample("sample-greek.txt")

    record = inspect_configuration(payload, expected_text="definitely not this text")

    assert record.best is not None
    assert record.status == "expected_mismatch"


def test_audit_deduplicates_effective_configuration_and_counts_identity() -> None:
    payload = read_sample("sample-chinese.txt")
    specs = parse_switch_specs(
        {"big5_switch": {"encodings": ["big5"], "default": True}}
    )
    configs = {
        "site-a": {"switches": {"big5_switch": True}},
        "site-b": {"cp_exclusion": ["big5"]},
        "group-one": [{"name": "site-c", "switches": {"big5_switch": False}}],
        "group-two": [{"name": "site-d", "cp_exclusion": ["big5"]}],
    }

    report = audit_configurations(payload, configs, specs, include_impacts=True)

    assert report.total_configurations == 4
    assert report.distinct_configurations == 2
    by_encoding = {group.encoding: group for group in report.groups}
    assert by_encoding["big5"].count == 1
    assert by_encoding["big5hkscs"].count == 1
    assert by_encoding["big5hkscs"].record_count == 3
    assert by_encoding["big5hkscs"].witness is not None
    divergence = by_encoding["big5hkscs"].witness.first_divergence
    assert divergence.encoding == "big5"
    assert divergence.left_allowed is True
    assert divergence.right_allowed is False
    assert [item.switch for item in report.switch_sheet] == ["big5_switch"]
    assert by_encoding["big5"].effective_switch_sheet[0].switch == "big5_switch"


def test_first_candidate_difference_points_to_earliest_changed_choice() -> None:
    payload = read_sample("sample-russian.txt")

    divergence = first_candidate_difference(
        payload,
        {"cp_exclusion": ["koi8_r"]},
        {"cp_exclusion": ["cp1251", "koi8_r"]},
    )

    assert divergence is not None
    assert divergence.encoding == "cp1251"
    assert divergence.left_allowed is True
    assert divergence.right_allowed is False


def test_compare_reports_whether_result_actually_changed() -> None:
    payload = read_sample("sample-chinese.txt")

    changed = compare_configurations(
        payload,
        {"name": "with-big5"},
        {"name": "without-big5", "cp_exclusion": ["big5"]},
    )
    unchanged = compare_configurations(
        payload,
        {"name": "one", "cp_exclusion": ["cp1251"]},
        {"name": "two", "cp_exclusion": ["cp1252"]},
    )

    assert changed.encoding_changed is True
    assert changed.outcome_changed is True
    assert changed.first_divergence.encoding == "big5"
    assert unchanged.first_divergence is not None
    assert unchanged.outcome_changed is False


def test_tied_switch_toggles_whole_encoding_group() -> None:
    payload = read_sample("sample-greek.txt")
    specs = parse_switch_specs(
        {
            "greek_pair": {
                "encodings": ["cp1253", "iso8859_7"],
                "default": True,
                "together": True,
            }
        }
    )

    impacts = switch_impact_report(payload, specs)

    assert len(impacts) == 1
    impact = impacts[0]
    assert impact.together is True
    assert set(impact.encodings) == {"cp1253", "iso8859_7"}
    assert impact.changes_outcome is True
    assert impact.variant_encoding == "mac_greek"
    assert impact.first_divergence is not None
    assert impact.first_divergence.encoding in {"cp1253", "iso8859_7"}


def test_shared_cache_reuses_mess_probe_across_configs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = read_sample("sample-french-1.txt")
    original = api.mess_ratio
    calls: list[tuple] = []

    def counting_mess_ratio(*args: object, **kwargs: object) -> float:
        calls.append(args)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(api, "mess_ratio", counting_mess_ratio)
    cache: dict = {}

    inspect_configuration(
        payload, {"cp_isolation": ["cp1252", "iso8859_15"]}, shared_cache=cache
    )
    first_count = len(calls)
    inspect_configuration(
        payload,
        {"cp_isolation": ["cp1252", "iso8859_15", "cp1250"]},
        shared_cache=cache,
    )

    assert first_count > 0
    assert len(calls) >= first_count
    assert "mess_ratio" in cache
