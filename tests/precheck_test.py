"""Tests for replayable site-log prechecks."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tldextract.precheck import (
    ARCHIVED,
    IP,
    PENDING,
    REVIEW_TEXT,
    Classification,
    RowFact,
    RuleConfig,
    SiteLogPrecheck,
    UpdateLedger,
)

FIXTURE_SUFFIX_LIST = (
    Path(__file__).parent / "fixtures" / "fake_suffix_list_fixture.dat"
).as_uri()


def _suffix_path() -> Path:
    return Path(FIXTURE_SUFFIX_LIST.removeprefix("file://"))


def _engine() -> SiteLogPrecheck:
    return SiteLogPrecheck(suffix_list_path=_suffix_path())


def _record(
    engine: SiteLogPrecheck, source: str, day: str, record_no: str
) -> Classification:
    record = engine.record(source, day, record_no)
    assert record is not None
    return record


def test_primary_decides_unless_missing_then_fallback() -> None:
    """Only a missing primary value may fall back to the backup address."""
    engine = _engine()
    engine.add_row(
        RowFact("jia", "2026-11-02", "1", 1, primary="-", fallback="site.foo")
    )
    engine.add_row(
        RowFact("yi", "2026-11-02", "1", 1, primary="-", fallback="site.foo")
    )

    # The default policy treats "-" as missing, so both use the fallback.
    assert _record(engine, "jia", "2026-11-02", "1").bucket == ARCHIVED
    assert _record(engine, "yi", "2026-11-02", "1").bucket == ARCHIVED

    # jia delivers a config that makes "-" review text from day 2 on.
    engine.add_config(
        RuleConfig(
            "jia",
            1,
            missing_values=(),
            review_values=("-",),
            effective_from="2026-11-02",
        )
    )

    jia = _record(engine, "jia", "2026-11-02", "1")
    assert jia.category == REVIEW_TEXT
    assert jia.bucket == PENDING
    assert jia.value_used == "primary"
    assert jia.config_revision == 1

    # The other source keeps its own fallback classification.
    yi = _record(engine, "yi", "2026-11-02", "1")
    assert yi.bucket == ARCHIVED
    assert yi.value_used == "fallback"


def test_day_two_ip_withdrawal_does_not_touch_day_one_or_other_source() -> None:
    """A config effective from day 2 must withdraw exactly one IP count."""
    engine = _engine()
    engine.add_row(
        RowFact("jia", "2026-11-01", "8", 1, primary="-", fallback="203.0.113.7")
    )
    engine.add_row(
        RowFact("jia", "2026-11-02", "8", 1, primary="-", fallback="203.0.113.9")
    )
    engine.add_row(
        RowFact("yi", "2026-11-02", "8", 1, primary="-", fallback="203.0.113.9")
    )
    assert engine.reconciliation()["counts"][IP] == 3

    engine.add_config(
        RuleConfig(
            "jia",
            1,
            missing_values=(),
            review_values=("-",),
            effective_from="2026-11-02",
        )
    )

    counts = engine.reconciliation()["counts"]
    assert counts[IP] == 2
    assert counts[PENDING] == 1
    assert _record(engine, "jia", "2026-11-01", "8").bucket == IP
    assert _record(engine, "yi", "2026-11-02", "8").bucket == IP

    withdrawal = [update for update in engine.updates if update.op == "withdraw"]
    assert len(withdrawal) == 1
    assert withdrawal[0].day == "2026-11-02"
    assert withdrawal[0].bucket == IP
    assert withdrawal[0].delta == -1


def test_config_and_row_arrival_order_is_exchangeable() -> None:
    """Swapping the config and row arrival order gives the same outcome."""
    config = RuleConfig(
        "jia",
        1,
        missing_values=(),
        review_values=("-",),
        effective_from="2026-11-02",
    )
    rows = [
        RowFact("jia", "2026-11-01", "8", 1, primary="-", fallback="203.0.113.7"),
        RowFact("jia", "2026-11-02", "8", 1, primary="-", fallback="203.0.113.9"),
    ]

    config_first = _engine()
    config_first.add_config(config)
    for row in rows:
        config_first.add_row(row)

    rows_first = _engine()
    for row in rows:
        rows_first.add_row(row)
    rows_first.add_config(config)

    assert config_first.reconciliation() == rows_first.reconciliation()
    assert config_first.records() == rows_first.records()


def test_same_revision_conflict_counts_once_and_withdraws_site() -> None:
    """Two contents at the top revision replace a site record with one pending."""
    engine = _engine()
    engine.add_row(RowFact("jia", "2026-11-02", "5", 1, primary="shop.foo"))
    assert engine.reconciliation()["counts"][ARCHIVED] == 1

    engine.add_row(RowFact("jia", "2026-11-02", "5", 1, primary="other.foo"))
    conflict = _record(engine, "jia", "2026-11-02", "5")
    assert conflict.category == "conflict"
    assert conflict.pending is True
    assert len(conflict.conflict) == 2

    counts = engine.reconciliation()["counts"]
    assert counts[ARCHIVED] == 0
    assert counts[PENDING] == 1

    # A third different content at the same top revision is still one record.
    engine.add_row(RowFact("jia", "2026-11-02", "5", 1, primary="third.foo"))
    assert engine.reconciliation()["counts"][PENDING] == 1
    assert len(_record(engine, "jia", "2026-11-02", "5").conflict) == 3

    # A higher revision resolves the conflict.
    engine.add_row(RowFact("jia", "2026-11-02", "5", 2, primary="final.foo"))
    assert _record(engine, "jia", "2026-11-02", "5").bucket == ARCHIVED


def test_higher_revision_wins_over_late_lower_revision() -> None:
    """A late lower revision cannot overwrite a higher one."""
    engine = _engine()
    engine.add_row(RowFact("jia", "2026-11-01", "3", 2, primary="new.foo"))
    engine.add_row(RowFact("jia", "2026-11-01", "3", 1, primary="old.foo"))

    record = _record(engine, "jia", "2026-11-01", "3")
    assert record.site == "new.foo"
    assert record.content_revision == 2


def test_record_number_is_unique_per_source_and_day() -> None:
    """The same record number in two sources or days is a distinct record."""
    engine = _engine()
    engine.add_row(RowFact("jia", "2026-11-01", "8", 1, primary="a.foo"))
    engine.add_row(RowFact("jia", "2026-11-02", "8", 1, primary="b.foo"))
    engine.add_row(RowFact("yi", "2026-11-01", "8", 1, primary="c.foo"))

    assert engine.reconciliation()["records"] == 3


def test_replaying_input_and_updates_is_stable() -> None:
    """Re-sending rows and updates must not move the reconciliation."""
    engine = _engine()
    rows = [
        RowFact("jia", "2026-11-01", "8", 1, primary="-", fallback="203.0.113.7"),
        RowFact("jia", "2026-11-02", "8", 1, primary="-", fallback="203.0.113.9"),
        RowFact("jia", "2026-11-02", "5", 1, primary="shop.foo"),
    ]
    for row in rows:
        engine.add_row(row)
    config = RuleConfig(
        "jia",
        1,
        missing_values=(),
        review_values=("-",),
        effective_from="2026-11-02",
    )
    engine.add_config(config)

    before = engine.reconciliation()
    update_count = len(engine.updates)

    for row in rows:
        assert engine.add_row(row) is False
    engine.add_config(config)

    assert engine.reconciliation() == before
    assert len(engine.updates) == update_count

    ledger = UpdateLedger()
    assert ledger.apply_all(engine.updates) == update_count
    first = ledger.counts()
    assert ledger.apply_all(engine.updates) == 0
    assert ledger.counts() == first
    assert ledger.reconciliation()["balanced"] is True


def test_ledger_matches_engine_after_mixed_history() -> None:
    """The applied update stream reconciles to the engine's current counts."""
    engine = _engine()
    engine.add_row(RowFact("jia", "2026-11-01", "1", 1, primary="a.foo"))
    engine.add_row(RowFact("jia", "2026-11-02", "1", 1, primary="b.foo"))
    engine.add_row(RowFact("jia", "2026-11-02", "2", 1, primary="203.0.113.9"))
    engine.add_config(
        RuleConfig(
            "jia",
            1,
            missing_values=(),
            review_values=("-",),
            effective_from="2026-11-02",
        )
    )
    engine.add_row(RowFact("jia", "2026-11-02", "3", 1, primary="-"))

    ledger = UpdateLedger()
    ledger.apply_all(engine.updates)

    assert ledger.counts() == engine.reconciliation()["counts"]
    assert ledger.pending_review() == engine.reconciliation()["pending_review"]


def test_trace_reports_rule_and_content_revisions() -> None:
    """A current record can be traced back to its rule and content revision."""
    engine = _engine()
    engine.add_row(
        RowFact("jia", "2026-11-02", "8", 4, primary="-", fallback="site.foo")
    )
    engine.add_config(
        RuleConfig(
            "jia",
            7,
            missing_values=(),
            review_values=("-",),
            effective_from="2026-11-02",
        )
    )

    trace = engine.trace("jia", "2026-11-02", "8")
    assert trace["current"]["category"] == REVIEW_TEXT
    assert trace["current"]["config_revision"] == 7
    assert trace["current"]["content_revision"] == 4
    assert trace["config"]["revision"] == 7
    assert trace["history"]
    assert trace["history"][-1]["to_bucket"] == PENDING


def test_snapshot_restores_without_rereading_original_rows() -> None:
    """A saved state can be re-opened and corrected without the input files."""
    engine = _engine()
    engine.add_row(
        RowFact("jia", "2026-11-02", "8", 1, primary="-", fallback="site.foo")
    )
    engine.add_row(RowFact("jia", "2026-11-02", "9", 1, primary="203.0.113.9"))

    snapshot = engine.to_dict()
    restored = SiteLogPrecheck(suffix_list_path=_suffix_path())
    restored.load(snapshot)

    assert restored.reconciliation() == engine.reconciliation()
    assert restored.drain_updates() == ()

    restored.add_config(
        RuleConfig(
            "jia",
            2,
            missing_values=(),
            review_values=("-",),
            effective_from="2026-11-02",
        )
    )
    assert _record(restored, "jia", "2026-11-02", "8").bucket == PENDING
    assert _record(restored, "jia", "2026-11-02", "9").bucket == IP


def test_snapshot_then_correction_keeps_update_sequence_monotonic() -> None:
    """After restoring emitted updates, later changes must use higher sequences."""
    engine = _engine()
    engine.add_row(
        RowFact("jia", "2026-11-02", "8", 1, primary="-", fallback="site.foo")
    )
    engine.add_config(
        RuleConfig(
            "jia",
            1,
            missing_values=(),
            review_values=("-",),
            effective_from="2026-11-02",
        )
    )

    restored = SiteLogPrecheck(suffix_list_path=_suffix_path())
    restored.load(engine.to_dict())
    restored.add_row(RowFact("jia", "2026-11-02", "8", 2, primary="new.foo"))

    sequences = [update.sequence for update in restored.updates]
    assert sequences == sorted(sequences)

    ledger = UpdateLedger()
    ledger.apply_all(restored.updates)
    assert ledger.counts() == restored.reconciliation()["counts"]
    assert _record(restored, "jia", "2026-11-02", "8").bucket == ARCHIVED


def test_jsonl_event_ingestion_accepts_aliases(tmp_path: Path) -> None:
    """JSON Lines events accept the documented field aliases."""
    events = tmp_path / "events.jsonl"
    events.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "row",
                        "source": "jia",
                        "data_day": "2026-11-02",
                        "record_id": "8",
                        "row_revision": 1,
                        "primary_address": "-",
                        "fallback_address": "site.foo",
                    }
                ),
                json.dumps(
                    {
                        "type": "config",
                        "source": "jia",
                        "revision": 1,
                        "missing_tokens": [],
                        "review_tokens": ["-"],
                        "from_day": "2026-11-02",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    engine = _engine()
    engine.ingest_file(events)

    record = _record(engine, "jia", "2026-11-02", "8")
    assert record.category == REVIEW_TEXT
    assert record.config_revision == 1


def test_invalid_and_conflicting_config_revisions_are_rejected() -> None:
    """Rule revisions are immutable once received."""
    engine = _engine()
    engine.add_config(RuleConfig("jia", 1, missing_values=("-",)))

    with pytest.raises(ValueError):
        engine.add_config(RuleConfig("jia", 1, missing_values=("NA",)))

    with pytest.raises(ValueError):
        RuleConfig("jia", 1, effective_from="2026-11-02", effective_to="2026-11-01")

    with pytest.raises(ValueError):
        RowFact("jia", "2026-11-02", "8", 0)


def test_cli_state_round_trip_sends_only_new_corrections(tmp_path: Path) -> None:
    """A stateful CLI run sends only the partial correction it received."""
    state = tmp_path / "state.json"
    rows = tmp_path / "rows.jsonl"
    rows.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "type": "row",
                    "source": "jia",
                    "day": "2026-11-01",
                    "record_no": "8",
                    "revision": 1,
                    "primary": "-",
                    "fallback": "203.0.113.7",
                },
                {
                    "type": "row",
                    "source": "jia",
                    "day": "2026-11-02",
                    "record_no": "8",
                    "revision": 1,
                    "primary": "-",
                    "fallback": "203.0.113.9",
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.jsonl"
    config.write_text(
        json.dumps(
            {
                "type": "config",
                "source": "jia",
                "revision": 1,
                "missing_values": [],
                "review_values": ["-"],
                "effective_from": "2026-11-02",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    first_updates = tmp_path / "first.jsonl"
    second_updates = tmp_path / "second.jsonl"

    _run_cli(rows, "--state", str(state), "--updates", str(first_updates))
    _run_cli(config, "--state", str(state), "--updates", str(second_updates))

    second = [
        json.loads(line)
        for line in second_updates.read_text(encoding="utf-8").splitlines()
    ]
    assert [update["op"] for update in second] == ["withdraw", "classify"]
    assert second[0]["bucket"] == IP
    assert second[1]["bucket"] == PENDING

    # Replaying the original rows and the correction changes nothing.
    replay_updates = tmp_path / "replay.jsonl"
    _run_cli(
        rows,
        config,
        rows,
        config,
        "--state",
        str(state),
        "--updates",
        str(replay_updates),
    )
    assert replay_updates.read_text(encoding="utf-8") == ""

    report = _run_cli(rows, config, "--state", str(state), "--report")
    report_json = json.loads(report.stdout)
    assert report_json["records"]["counts"]["ip"] == 1
    assert report_json["records"]["counts"]["pending"] == 1
    assert report_json["ledger"]["counts"] == report_json["records"]["counts"]


def _run_cli(*args: object) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "tldextract.precheck_cli",
        "--suffix-list",
        str(_suffix_path()),
        *(str(arg) for arg in args),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def test_cli_emits_jsonl_updates_and_report(tmp_path: Path) -> None:
    """The CLI ingests rows, then writes updates and a report."""
    events = tmp_path / "rows.jsonl"
    events.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "row",
                        "source": "jia",
                        "day": "2026-11-01",
                        "record_no": "8",
                        "revision": 1,
                        "primary": "-",
                        "fallback": "203.0.113.7",
                    }
                ),
                json.dumps(
                    {
                        "type": "row",
                        "source": "jia",
                        "day": "2026-11-02",
                        "record_no": "8",
                        "revision": 1,
                        "primary": "-",
                        "fallback": "203.0.113.9",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = tmp_path / "config.jsonl"
    config.write_text(
        json.dumps(
            {
                "type": "config",
                "source": "jia",
                "revision": 1,
                "missing_values": [],
                "review_values": ["-"],
                "effective_from": "2026-11-02",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    updates_path = tmp_path / "updates.jsonl"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tldextract.precheck_cli",
            "--suffix-list",
            str(_suffix_path()),
            "--updates",
            str(updates_path),
            "--report",
            str(events),
            str(config),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["records"]["counts"]["ip"] == 1
    assert report["records"]["counts"]["pending"] == 1
    assert report["records"]["balanced"] is True

    lines = updates_path.read_text(encoding="utf-8").splitlines()
    updates = [json.loads(line) for line in lines]
    assert any(update["op"] == "withdraw" for update in updates)
    assert any(
        update["op"] == "classify" and update["bucket"] == "pending"
        for update in updates
    )
