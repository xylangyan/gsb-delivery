"""Tests for canonical site-key migration and reconciliation."""

import itertools
import random
from collections.abc import Iterable
from pathlib import Path

import pytest

import tldextract
from tldextract.site_migration import (
    DetailRecord,
    MigrationLedger,
    UncertainRecord,
    canonical_host,
    derive_site_key,
    legacy_host,
    reconcile,
)

FIXTURE_URL = Path(
    Path(__file__).parent, "fixtures", "site_migration_suffix_list.dat"
).as_uri()


def make_extractor() -> tldextract.TLDExtract:
    """Return an offline extractor backed by the fixed local suffix list."""
    return tldextract.TLDExtract(
        cache_dir=None,
        suffix_list_urls=[FIXTURE_URL],
        fallback_to_snapshot=False,
        include_psl_private_domains=True,
    )


EXTRACTOR = make_extractor()


def test_unicode_and_ace_are_one_host() -> None:
    """straße is legal Unicode and its ACE label is the same canonical host."""
    assert canonical_host("straße.de") == "xn--strae-oqa.de"
    assert canonical_host("xn--strae-oqa.de") == "xn--strae-oqa.de"
    assert canonical_host("STRASSE.DE") == "strasse.de"
    assert canonical_host("例え.テスト") == "xn--r8jz45g.xn--zckzah"


def test_legacy_key_reproduces_collision() -> None:
    """The old IDNA 2003 key folds the sharp s into ss."""
    assert legacy_host("straße.de") == "strasse.de"
    assert legacy_host("xn--strae-oqa.de") == "strasse.de"
    assert legacy_host("strasse.de") == "strasse.de"


def test_derive_site_key_splits_collided_sites() -> None:
    """Previously collided hosts now have distinct canonical keys."""
    sharp_s = derive_site_key("straße.de", EXTRACTOR)
    ascii_ss = derive_site_key("strasse.de", EXTRACTOR)
    ace = derive_site_key("xn--strae-oqa.de", EXTRACTOR)

    assert sharp_s.old_key == ascii_ss.old_key == ace.old_key == "strasse.de"
    assert sharp_s.new_key == ace.new_key == "xn--strae-oqa.de"
    assert ascii_ss.new_key == "strasse.de"
    assert sharp_s.original_host == "straße.de"
    assert sharp_s.current_key == sharp_s.new_key
    assert sharp_s.evidence == "straße.de"


def test_unicode_private_suffix_is_listed_not_counted() -> None:
    """A host that is only the Unicode private suffix is not a site."""
    key = derive_site_key("例え.テスト", EXTRACTOR)
    assert key.is_private
    assert key.is_suffix_only
    assert key.old_key == ""
    assert key.new_key == ""
    assert key.original_host == "例え.テスト"

    subdomain = derive_site_key("www.例え.テスト", EXTRACTOR)
    assert subdomain.new_key == "www.xn--r8jz45g.xn--zckzah"
    assert subdomain.is_suffix_only is False


def test_invalid_host_is_marked_invalid() -> None:
    """Hosts that cannot be canonicalized keep their evidence but are invalid."""
    key = derive_site_key("a_b.de", EXTRACTOR)
    assert not key.is_valid
    assert key.original_host == "a_b.de"
    assert key.new_key == ""


def test_extract_result_exposes_canonical_keys() -> None:
    """Current extraction fields are preserved while canonical keys are added."""
    sharp_s = EXTRACTOR("straße.de")
    ace = EXTRACTOR("xn--strae-oqa.de")
    ascii_ss = EXTRACTOR("strasse.de")

    assert sharp_s.suffix == "de"
    assert sharp_s.top_domain_under_public_suffix == "straße.de"
    assert sharp_s.canonical_domain == "xn--strae-oqa.de"
    assert ace.canonical_domain == "xn--strae-oqa.de"
    assert ascii_ss.canonical_domain == "strasse.de"
    assert sharp_s.canonical_domain != ascii_ss.canonical_domain


def test_unique_allocation_confirms_all_rows() -> None:
    """A total that only one allocation can meet confirms every row."""
    ledger = MigrationLedger()
    ledger.add_total("xn--strae-oqa.de", 3)
    ledger.add_total("strasse.de", 1)

    ledger.add_uncertain("a", "1", {"xn--strae-oqa.de"}, old_key="strasse.de")
    ledger.add_uncertain("a", "2", {"xn--strae-oqa.de"}, old_key="strasse.de")
    ledger.add_uncertain("a", "3", {"xn--strae-oqa.de"}, old_key="strasse.de")
    ledger.add_uncertain("a", "4", {"strasse.de"}, old_key="strasse.de")

    report = reconcile(ledger)
    assert report.reconciled
    assert len(report.confirmed) == 4
    assert {row.new_key for row in report.confirmed} == {
        "xn--strae-oqa.de",
        "strasse.de",
    }
    assert report.final_totals == {"xn--strae-oqa.de": 3, "strasse.de": 1}


def test_detail_rows_are_counted_into_totals() -> None:
    """Confirmed detail rows reduce the residual the uncertain rows must fill."""
    ledger = MigrationLedger()
    ledger.add_detail("archive", "e1", "straße.de")
    ledger.add_detail("archive", "e2", "xn--strae-oqa.de")
    ledger.add_detail("archive", "e3", "strasse.de")
    ledger.add_total("xn--strae-oqa.de", 3)
    ledger.add_total("strasse.de", 4)
    ledger.add_uncertain("other", "u1", {"xn--strae-oqa.de"})
    ledger.add_uncertain("other", "u2", {"strasse.de"})
    ledger.add_uncertain("other", "u3", {"strasse.de"})
    ledger.add_uncertain("other", "u4", {"strasse.de"})

    report = reconcile(ledger, EXTRACTOR)
    assert report.confirmed_counts == {"xn--strae-oqa.de": 2, "strasse.de": 1}
    assert report.required_from_uncertain == {
        "xn--strae-oqa.de": 1,
        "strasse.de": 3,
    }
    assert report.reconciled


def test_partial_confirmation_leaves_ambiguous_rows() -> None:
    """Some rows resolve to one site while others keep two possibilities."""
    ledger = MigrationLedger()
    ledger.add_total("a.de", 2)
    ledger.add_total("b.de", 1)
    ledger.add_total("c.de", 1)

    for event_id in ("x1", "x2"):
        ledger.add_uncertain("s", event_id, {"a.de", "b.de"})
    for event_id in ("y1", "y2"):
        ledger.add_uncertain("s", event_id, {"b.de", "c.de"})

    report = reconcile(ledger)
    assert report.reconciled

    rows = {row.event_id: row for row in report.rows}
    assert rows["x1"].status == "confirmed"
    assert rows["x1"].new_key == "a.de"
    assert rows["x2"].status == "confirmed"
    assert rows["y1"].status == "ambiguous"
    assert rows["y1"].possible_sites == ("b.de", "c.de")
    assert rows["y2"].status == "ambiguous"


def test_any_allocation_must_agree_to_migrate() -> None:
    """A row migrates only when every feasible allocation names one site."""
    ledger = MigrationLedger()
    ledger.add_total("a.de", 2)
    ledger.add_total("b.de", 2)

    ledger.add_uncertain("s", "r1", {"a.de"})
    for event_id in ("r2", "r3", "r4"):
        ledger.add_uncertain("s", event_id, {"a.de", "b.de"})

    report = reconcile(ledger)
    rows = {row.event_id: row for row in report.rows}
    assert rows["r1"].status == "confirmed"
    assert rows["r1"].new_key == "a.de"
    for event_id in ("r2", "r3", "r4"):
        assert rows[event_id].status == "ambiguous"
        assert rows[event_id].possible_sites == ("a.de", "b.de")


def test_unsatisfiable_totals_report_conflict() -> None:
    """Totals that no allocation can meet are reported, not silently adjusted."""
    ledger = MigrationLedger()
    ledger.add_total("a.de", 3)
    ledger.add_total("b.de", 3)
    ledger.add_uncertain("s", "1", {"a.de"})
    ledger.add_uncertain("s", "2", {"a.de"})

    report = reconcile(ledger)
    assert not report.reconciled
    assert report.conflicts
    by_site = {site for conflict in report.conflicts for site in conflict.sites}
    assert by_site == {"a.de", "b.de"}
    residual = {
        site: count
        for conflict in report.conflicts
        for site, count in conflict.residual.items()
    }
    assert residual == {"a.de": 3, "b.de": 3}
    assert {("s", "1"), ("s", "2")} <= {
        row_id for conflict in report.conflicts for row_id in conflict.row_ids
    }
    assert all(row.migrated is False for row in report.rows)
    assert report.final_totals == {"a.de": 0, "b.de": 0}
    assert report.total_bounds == {"a.de": (0, 0), "b.de": (0, 0)}


def test_total_without_rows_is_conflict_and_not_adopted() -> None:
    """A saved total no row can supply is reported, not written into the books."""
    ledger = MigrationLedger()
    ledger.add_total("missing.de", 2)

    report = reconcile(ledger)
    assert not report.reconciled
    assert any(conflict.sites == ("missing.de",) for conflict in report.conflicts)
    assert report.final_totals == {"missing.de": 0}
    assert report.total_bounds == {"missing.de": (0, 0)}


def test_conflicting_candidate_group_reports_rows_and_totals() -> None:
    """Rows sharing an over-subscribed site are listed with the totals."""
    ledger = MigrationLedger()
    ledger.add_total("a.de", 5)
    for event_id in ("r1", "r2"):
        ledger.add_uncertain("s", event_id, {"a.de", "b.de"})

    report = reconcile(ledger)
    assert not report.reconciled
    conflicted = [row for row in report.rows if row.status == "conflicted"]
    assert {row.event_id for row in conflicted} == {"r1", "r2"}
    assert all(row.migrated is False for row in conflicted)
    assert {site for conflict in report.conflicts for site in conflict.sites} == {
        "a.de",
        "b.de",
    }


def test_confirmed_rows_exceeding_total_are_conflicts() -> None:
    """Saved totals smaller than confirmed detail rows cannot be reconciled."""
    ledger = MigrationLedger()
    ledger.add_detail("s", "1", "straße.de")
    ledger.add_detail("s", "2", "xn--strae-oqa.de")
    ledger.add_total("xn--strae-oqa.de", 1)

    report = reconcile(ledger, EXTRACTOR)
    assert not report.reconciled
    kinds = {conflict.kind for conflict in report.conflicts}
    assert "confirmed_exceeds_total" in kinds
    conflict = next(
        conflict
        for conflict in report.conflicts
        if conflict.kind == "confirmed_exceeds_total"
    )
    assert conflict.residual == {"xn--strae-oqa.de": -1}
    assert set(conflict.row_ids) == {("s", "1"), ("s", "2")}


def test_row_identity_is_source_and_event() -> None:
    """Equal values under different events count twice; replays do not."""
    ledger = MigrationLedger()
    assert ledger.add_detail("a", "1", "straße.de")
    assert ledger.add_detail("b", "1", "straße.de")
    assert ledger.add_detail("a", "2", "straße.de")
    assert not ledger.add_detail("a", "2", "straße.de")
    assert ledger.replayed == 1

    report = reconcile(ledger, EXTRACTOR)
    assert report.confirmed_counts == {"xn--strae-oqa.de": 3}
    assert report.detail_row_count == 3


def test_replay_of_one_source_does_not_double_count() -> None:
    """Replaying a whole source is idempotent."""
    source_a: list[DetailRecord | UncertainRecord] = [
        DetailRecord("a", "1", "straße.de"),
        DetailRecord("a", "2", "strasse.de"),
    ]
    source_b: list[DetailRecord | UncertainRecord] = [
        DetailRecord("b", "1", "xn--strae-oqa.de"),
        UncertainRecord("b", "2", frozenset({"strasse.de"})),
    ]

    ledger = MigrationLedger()
    for left, right in itertools.zip_longest(source_a, source_b):
        if left is not None:
            ledger.add_row(left)
        if right is not None:
            ledger.add_row(right)
    before = reconcile(ledger, EXTRACTOR)

    ledger.replay(source_a)
    after = reconcile(ledger, EXTRACTOR)

    assert ledger.replayed == len(source_a)
    assert before.confirmed_counts == after.confirmed_counts
    assert after.old_key_counts == before.old_key_counts


def test_conflicting_duplicate_identity_is_not_preferred() -> None:
    """A conflicting replay excludes the row instead of taking the first value."""
    ledger = MigrationLedger()
    ledger.add_detail("a", "1", "straße.de")
    ledger.add_detail("a", "1", "strasse.de")

    report = reconcile(ledger, EXTRACTOR)
    assert not report.reconciled
    assert report.identity_conflicts == [
        (("a", "1"), "detail:straße.de", "detail:strasse.de")
    ]
    assert report.confirmed_counts == {}
    conflicted = [row for row in report.rows if row.status == "conflicted"]
    assert len(conflicted) == 1


def test_conflicting_totals_are_reported() -> None:
    """A second, different total for a site is reported, not overwritten."""
    ledger = MigrationLedger()
    ledger.add_total("a.de", 2)
    assert ledger.add_total("a.de", 2) is False
    ledger.add_total("a.de", 5)

    report = reconcile(ledger)
    assert report.total_conflicts == [("a.de", 2, 5)]
    assert report.saved_totals == {"a.de": 2}
    assert report.required_from_uncertain == {}
    assert report.final_totals == {}
    assert report.total_bounds == {}
    assert any(conflict.kind == "conflicting_totals" for conflict in report.conflicts)
    assert not report.reconciled


def test_suffix_only_rows_are_separate_and_uncounted() -> None:
    """Hosts that are only a suffix are listed but never inflate site counts."""
    ledger = MigrationLedger()
    ledger.add_detail("s", "1", "例え.テスト")
    ledger.add_detail("s", "2", "www.例え.テスト")
    ledger.add_detail("s", "3", "de")
    ledger.add_detail("s", "4", "a_b.de")

    report = reconcile(ledger, EXTRACTOR)
    rows = {row.event_id: row for row in report.rows}
    assert rows["1"].status == "suffix_only"
    assert rows["3"].status == "suffix_only"
    assert rows["4"].status == "invalid"
    assert rows["2"].status == "confirmed"
    assert report.confirmed_counts == {"www.xn--r8jz45g.xn--zckzah": 1}
    assert [row.event_id for row in report.suffix_only] == ["1", "3"]
    assert report.old_key_counts[""] == 3
    assert report.new_key_counts[""] == 3
    assert sum(report.old_key_counts.values()) == sum(report.new_key_counts.values())


def test_reports_old_new_and_evidence_for_reconciliation() -> None:
    """Each row and mapping exposes both conventions plus original evidence."""
    ledger = MigrationLedger()
    ledger.add_detail("archive", "e1", "straße.de")
    ledger.add_detail("archive", "e2", "strasse.de")
    ledger.add_total("xn--strae-oqa.de", 1)
    ledger.add_total("strasse.de", 1)

    report = reconcile(ledger, EXTRACTOR)
    rows = {row.event_id: row for row in report.rows}
    assert rows["e1"].old_key == "strasse.de"
    assert rows["e1"].new_key == "xn--strae-oqa.de"
    assert rows["e1"].evidence == "straße.de"
    assert rows["e2"].old_key == "strasse.de"
    assert rows["e2"].new_key == "strasse.de"
    assert report.old_to_new["strasse.de"] == {
        "strasse.de": 1,
        "xn--strae-oqa.de": 1,
    }
    assert report.old_key_counts["strasse.de"] == 2
    assert report.new_key_counts == {"xn--strae-oqa.de": 1, "strasse.de": 1}


def test_large_overlapping_candidate_group_does_not_enumerate_allocations() -> None:
    """A big overlapping group is decided by flow, not allocation enumeration."""
    ledger = MigrationLedger()
    count = 1200
    for index in range(count):
        ledger.add_uncertain("big", str(index), {"a.de", "b.de"})
    ledger.add_total("a.de", count)
    ledger.add_total("b.de", 0)

    report = reconcile(ledger)
    assert report.reconciled
    assert len(report.rows) == count
    assert all(row.status == "confirmed" for row in report.rows)
    assert {row.new_key for row in report.rows} == {"a.de"}


@pytest.mark.parametrize("seed", range(25))
def test_flow_matches_brute_force_on_small_groups(seed: int) -> None:
    """Per-row possibility from flow agrees with exhaustive enumeration."""
    rng = random.Random(seed)
    sites = ["a.de", "b.de", "c.de", "d.de"]

    group_sets = set()
    for _ in range(rng.randint(1, 3)):
        size = rng.randint(1, 2)
        group_sets.add(frozenset(rng.sample(sites, size)))

    counts = {group: rng.randint(1, 3) for group in group_sets}
    constrained = {
        site: rng.randint(0, 3) for site in rng.sample(sites, rng.randint(1, 3))
    }

    feasible_rows: list[set[str]] = []
    for group in group_sets:
        feasible_rows.extend([set(group)] * counts[group])

    feasible_allocations = [
        allocation
        for allocation in _enumerate(feasible_rows)
        if _meets_totals(allocation, constrained)
    ]

    ledger = MigrationLedger()
    for site, total in constrained.items():
        ledger.add_total(site, total)
    event = 0
    for group, group_count in counts.items():
        for _ in range(group_count):
            ledger.add_uncertain("s", str(event), group)
            event += 1

    report = reconcile(ledger)
    actual_ok = not any(
        conflict.kind == "unsatisfiable_totals" for conflict in report.conflicts
    )
    assert actual_ok is bool(feasible_allocations)

    if not feasible_allocations:
        return

    for index in range(len(feasible_rows)):
        expected = {allocation[index] for allocation in feasible_allocations}
        row = next(row for row in report.rows if row.event_id == str(index))
        assert set(row.possible_sites) == expected


def _enumerate(rows: list[set[str]]) -> Iterable[list[str]]:
    """Yield every assignment of one site to each row."""
    for combo in itertools.product(*[sorted(row) for row in rows]):
        yield list(combo)


def _meets_totals(allocation: list[str], constrained: dict[str, int]) -> bool:
    """Whether an allocation matches every constrained site total exactly."""
    tally: dict[str, int] = {}
    for site in allocation:
        tally[site] = tally.get(site, 0) + 1
    return all(tally.get(site, 0) == count for site, count in constrained.items())
