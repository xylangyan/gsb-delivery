"""Tests for enterprise operating zones layered over public suffix rules."""

import os
import re
from pathlib import Path

import pytest

import tldextract
from tldextract import (
    PUBLIC_SOURCE,
    RuleKind,
    ZoneEvaluator,
    ZoneList,
    ZoneRule,
)

FAKE_SUFFIX_LIST_URL = Path(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "fake_suffix_list_fixture.dat",
).as_uri()

PUBLIC_RULES = ["com", "org", "co.uk", "*.ck", "!www.ck"]

MANAGED_PARENTS = ["example.com", "example.org", "firm.co.uk", "other.co.uk"]

REGIONS = ZoneList(
    name="regions",
    priority=10,
    rules=(
        "*.example.com",
        "!hq.example.com",
        "east.example.org",
    ),
)

TEAMS = ZoneList(
    name="teams",
    priority=20,
    rules=(
        "team.example.com",
        "*.example.org",
    ),
)

DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def make_evaluator(*zone_lists: ZoneList) -> ZoneEvaluator:
    """Build an evaluator over the shared public rules."""
    return ZoneEvaluator(public_rules=PUBLIC_RULES, zone_lists=zone_lists)


def test_rule_parsing() -> None:
    """Rules parse into kinds, labels, and declared levels."""
    exact = ZoneRule.parse("Team.Example.com.")
    assert exact.kind is RuleKind.EXACT
    assert exact.labels == ("team", "example", "com")
    assert exact.declared_levels == 3

    wildcard = ZoneRule.parse("*.example.com")
    assert wildcard.kind is RuleKind.WILDCARD
    assert wildcard.labels == ("example", "com")
    assert wildcard.declared_levels == 3

    exception = ZoneRule.parse("!hq.example.com")
    assert exception.kind is RuleKind.EXCEPTION
    assert exception.labels == ("hq", "example", "com")
    assert exception.declared_levels == 3


def test_rule_parsing_rejects_garbage() -> None:
    """Malformed rule declarations are rejected."""
    for raw in ("", "!", "a..b", "."):
        with pytest.raises(ValueError):
            ZoneRule.parse(raw)


def test_longest_declaration_wins() -> None:
    """A longer declaration beats a shorter one."""
    zones = make_evaluator(TEAMS, REGIONS)
    match = zones.explain("app.team.example.com")
    assert match.matched
    assert match.kind is RuleKind.EXACT
    assert (match.suffix, match.group) == ("team.example.com", "app.team.example.com")
    assert (match.rule, match.source, match.priority) == (
        "team.example.com",
        "teams",
        20,
    )


def test_same_level_tie_broken_by_source_priority() -> None:
    """Two zone definitions with the same match length: the higher explicit priority wins."""
    zones = make_evaluator(REGIONS, TEAMS)
    wildcard_match = zones.explain("app.other.example.com")
    assert wildcard_match.kind is RuleKind.WILDCARD
    assert (wildcard_match.suffix, wildcard_match.rule, wildcard_match.source) == (
        "other.example.com",
        "*.example.com",
        "regions",
    )
    exact_match = zones.explain("app.team.example.com")
    assert exact_match.source == "teams"


def test_exception_falls_back_to_shorter_suffix() -> None:
    """A winning exception drops the grouping suffix one label."""
    zones = make_evaluator(REGIONS)
    match = zones.explain("a.hq.example.com")
    assert match.kind is RuleKind.EXCEPTION
    assert match.rule == "!hq.example.com"
    assert match.source == "regions"
    assert match.suffix == "example.com"
    assert match.group == "hq.example.com"


def test_wildcard_covers_one_more_label() -> None:
    """A wildcard covers exactly one extra host label."""
    zones = make_evaluator(TEAMS)
    match = zones.explain("a.b.example.org")
    assert match.kind is RuleKind.WILDCARD
    assert match.suffix == "b.example.org"
    assert match.group == "a.b.example.org"


def test_public_rules_are_the_fallback() -> None:
    """Hosts outside every zone rule fall back to public rules."""
    zones = make_evaluator(REGIONS, TEAMS)
    match = zones.explain("www.firm.co.uk")
    assert (match.rule, match.source, match.priority) == ("co.uk", PUBLIC_SOURCE, 0)
    assert (match.suffix, match.group) == ("co.uk", "firm.co.uk")


def test_public_exceptions_define_the_original_boundary() -> None:
    """Public exceptions shape the original organization boundary."""
    zones = make_evaluator(REGIONS, TEAMS)
    match = zones.explain("a.www.ck")
    assert (match.suffix, match.group) == ("ck", "www.ck")
    assert match.source == PUBLIC_SOURCE


def test_non_zone_mode_report_uses_original_public_rules() -> None:
    """The non-zone-mode report ignores zone lists."""
    zones = make_evaluator(REGIONS, TEAMS)
    match = zones.explain_public("app.team.example.com")
    assert (match.suffix, match.group) == ("com", "example.com")
    assert (match.rule, match.source) == ("com", PUBLIC_SOURCE)


def test_no_match() -> None:
    """A host matching nothing yields an empty suffix."""
    zones = make_evaluator()
    match = zones.explain("localhost")
    assert not match.matched
    assert match.suffix == ""
    assert match.group == "localhost"


def test_list_pass_order_does_not_change_results() -> None:
    """Passing the same lists in a different order keeps every result identical."""
    first = make_evaluator(REGIONS, TEAMS)
    second = make_evaluator(TEAMS, REGIONS)
    hosts = [
        "app.team.example.com",
        "app.other.example.com",
        "a.hq.example.com",
        "a.b.example.org",
        "www.firm.co.uk",
        "a.www.ck",
    ]
    for host in hosts:
        assert first.explain(host) == second.explain(host)
        assert first.explain_public(host) == second.explain_public(host)
    assert first.validate(MANAGED_PARENTS) == second.validate(MANAGED_PARENTS)


def test_each_list_and_their_combination_keep_the_boundary() -> None:
    """The publish flow: check each list, reorder, then check the overlay."""
    assert make_evaluator(REGIONS).validate(MANAGED_PARENTS).ok
    assert make_evaluator(TEAMS).validate(MANAGED_PARENTS).ok
    report = make_evaluator(TEAMS, REGIONS).validate(MANAGED_PARENTS)
    assert report.ok
    assert report.boundary_unchanged
    assert report.violations == ()
    assert "does not change the registered-organization boundary" in report.summary()


def test_subdivision_of_one_organization_is_allowed() -> None:
    """Splitting one organization into finer groups is allowed."""
    zones = ZoneEvaluator(
        public_rules=["com"],
        zone_lists=[ZoneList("depts", 10, ("eng.example.com", "ops.example.com"))],
    )
    report = zones.validate(["example.com"])
    assert report.boundary_unchanged
    assert zones.explain("a.eng.example.com").group == "a.eng.example.com"


def test_exception_merging_two_managed_parents_is_reported() -> None:
    """A cross-organization merge yields witnesses and rules."""
    zones = ZoneEvaluator(
        public_rules=PUBLIC_RULES,
        zone_lists=[ZoneList("bad", 30, ("!co.uk",))],
    )
    report = zones.validate(["firm.co.uk", "other.co.uk"])
    assert not report.ok
    assert len(report.violations) == 1
    violation = report.violations[0]
    assert (violation.rule, violation.source, violation.priority) == (
        "!co.uk",
        "bad",
        30,
    )
    assert violation.group == "co.uk"
    assert violation.organizations == ("firm.co.uk", "other.co.uk")
    assert violation.public_rules == ("co.uk", "co.uk")

    first, second = violation.witnesses
    assert first.endswith(".firm.co.uk")
    assert second.endswith(".other.co.uk")
    for witness in violation.witnesses:
        assert all(DNS_LABEL_RE.match(label) for label in witness.split("."))
    # The witnesses recompute to the merged group under the zone rules ...
    assert zones.explain(first).group == "co.uk"
    assert zones.explain(second).group == "co.uk"
    assert zones.explain(first).rule == "!co.uk"
    # ... while the original public rules keep them in different organizations.
    assert zones.explain_public(first).group == "firm.co.uk"
    assert zones.explain_public(second).group == "other.co.uk"
    assert "merges across registered organizations" in report.summary()


def test_merge_outside_the_managed_space_is_not_reported() -> None:
    """With a single managed parent, all grouped hosts share one organization."""
    zones = ZoneEvaluator(
        public_rules=PUBLIC_RULES,
        zone_lists=[ZoneList("bad", 30, ("!co.uk",))],
    )
    report = zones.validate(["firm.co.uk"])
    assert report.boundary_unchanged


def test_exception_knocking_back_a_public_wildcard_is_reported() -> None:
    """Knocking a public wildcard back to a shorter suffix merges organizations."""
    zones = ZoneEvaluator(
        public_rules=["com", "*.example.com"],
        zone_lists=[ZoneList("zap", 5, ("!a.example.com",))],
    )
    report = zones.validate(["example.com"])
    assert not report.ok
    violation = report.violations[0]
    assert violation.rule == "!a.example.com"
    assert violation.group == "a.example.com"
    first, second = violation.witnesses
    assert first != second
    assert first.endswith(".a.example.com")
    assert second.endswith(".a.example.com")
    assert violation.organizations == (first, second)
    assert violation.public_rules == ("*.example.com", "*.example.com")
    assert zones.explain(first).group == zones.explain(second).group
    assert zones.explain_public(first).group != zones.explain_public(second).group


def test_exception_falling_back_only_to_the_public_suffix_is_allowed() -> None:
    """Fallback down to exactly the public suffix changes nothing."""
    zones = ZoneEvaluator(
        public_rules=["com"],
        zone_lists=[ZoneList("zap", 5, ("!hq.example.com",))],
    )
    assert zones.validate(["example.com"]).boundary_unchanged


def test_exception_outside_every_managed_parent_is_ignored() -> None:
    """Exceptions unreachable from the managed space cannot violate."""
    zones = ZoneEvaluator(
        public_rules=PUBLIC_RULES,
        zone_lists=[ZoneList("zap", 5, ("!co.jp",))],
    )
    assert zones.validate(MANAGED_PARENTS).boundary_unchanged


def test_validation_is_order_independent_even_with_violations() -> None:
    """Validation reports are identical regardless of list pass order."""
    bad = ZoneList("bad", 30, ("!co.uk",))
    first = ZoneEvaluator(public_rules=PUBLIC_RULES, zone_lists=[bad, REGIONS])
    second = ZoneEvaluator(public_rules=PUBLIC_RULES, zone_lists=[REGIONS, bad])
    parents = ["firm.co.uk", "other.co.uk"]
    assert first.validate(parents) == second.validate(parents)


def test_zone_list_normalizes_rules_and_validates_name() -> None:
    """Zone lists normalize rules and require a name."""
    zone_list = ZoneList("z", 1, ["a.example.com"])
    assert zone_list.rules == ("a.example.com",)
    with pytest.raises(ValueError):
        ZoneList("", 1, ())


def test_tldextract_integration_offline() -> None:
    """A TLDExtract-backed evaluator explains hosts offline."""
    extractor = tldextract.TLDExtract(
        cache_dir=None, suffix_list_urls=[FAKE_SUFFIX_LIST_URL]
    )
    zones = extractor.zone_evaluator([ZoneList("depts", 10, ("dept.foo",))])
    match = zones.explain("www.dept.foo")
    assert (match.suffix, match.group, match.source) == (
        "dept.foo",
        "www.dept.foo",
        "depts",
    )
    public_match = zones.explain_public("www.dept.foo")
    assert (public_match.suffix, public_match.group, public_match.source) == (
        "foo",
        "dept.foo",
        PUBLIC_SOURCE,
    )


def test_tldextract_integration_validation_offline() -> None:
    """A TLDExtract-backed evaluator validates offline."""
    extractor = tldextract.TLDExtract(
        cache_dir=None, suffix_list_urls=[FAKE_SUFFIX_LIST_URL]
    )
    zones = extractor.zone_evaluator([ZoneList("bad", 10, ("!a.co.jp",))])
    report = zones.validate(["co.jp"])
    assert not report.ok
    violation = report.violations[0]
    assert violation.rule == "!a.co.jp"
    assert violation.group == "a.co.jp"
    first, second = violation.witnesses
    assert first.endswith(".a.co.jp")
    assert second.endswith(".a.co.jp")
    assert violation.organizations == (first, second)
