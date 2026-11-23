"""Tests for budgeted multi-tenant rollup planning."""

import pytest

from ddsketch.ddsketch import DDSketch
from ddsketch.ddsketch import LogCollapsingHighestDenseDDSketch
from ddsketch.ddsketch import LogCollapsingLowestDenseDDSketch
from ddsketch.rollup import CandidateConfiguration
from ddsketch.rollup import Capability
from ddsketch.rollup import RollupPlan
from ddsketch.rollup import TenantContract
from ddsketch.rollup import VolumeScenario
from ddsketch.rollup import WindowSketch
from ddsketch.rollup import _merged_sketch
from ddsketch.rollup import merge_without_plan
from ddsketch.rollup import plan_rollup


def _sketch(values, bin_limit=256, relative_accuracy=0.01, cls=None):
    cls = cls or LogCollapsingLowestDenseDDSketch
    sketch = cls(relative_accuracy, bin_limit)
    for value in values:
        sketch.add(value)
    return sketch


def _unit(tenant, old, new=None, incoming_range=(0, 0), capability=None):
    if capability is None:
        capability = Capability.from_sketch(old, "lowest")
    return WindowSketch(
        window="2026-11-22T10:31",
        tenant=tenant,
        old=old,
        new=new,
        capability=capability,
        incoming_range=incoming_range,
    )


def _contract(tenant, percentiles=(0.1, 0.5, 0.9), bound=0.02, priorities=None):
    return TenantContract(
        tenant,
        percentiles,
        {percentile: bound for percentile in percentiles},
        priorities or {},
    )


def test_healthy_tenant_is_certified_from_merged_buckets():
    old = _sketch(range(1, 101))
    new = _sketch([50, 50])
    unit = _unit("healthy", old, new)
    contract = _contract("healthy")
    candidate = CandidateConfiguration("roomy", 256, "lowest", 0.01)

    plan = plan_rollup([unit], [contract], [candidate], budget=1024)

    assert isinstance(plan, RollupPlan)
    assert plan.chosen_configuration if hasattr(plan, "chosen_configuration") else True
    assert len(plan.certified) == 3
    assert plan.issued == [
        ("2026-11-22T10:31", "healthy", 0.1),
        ("2026-11-22T10:31", "healthy", 0.5),
        ("2026-11-22T10:31", "healthy", 0.9),
    ]
    for decision in plan.certified:
        evidence = decision.evidence
        assert evidence.total_count == 102
        certified_scenarios = [
            item for item in evidence.scenarios if item["certified"]
        ]
        assert len(certified_scenarios) == len(evidence.scenarios)
        assert decision.value == certified_scenarios[0]["fields"]["reported_value"]


def test_evidence_is_recomputable_from_actual_merged_buckets():
    old = _sketch(range(1, 101))
    contract = _contract("audit", percentiles=(0.5,), bound=0.02)
    candidate = CandidateConfiguration("roomy", 256, "lowest", 0.01)
    plan = plan_rollup([_unit("audit", old)], [contract], [candidate], budget=1024)

    decision = plan.certified[0]
    fields = decision.evidence.scenarios[0]["fields"]
    merged = _merged_sketch(
        _unit("audit", old), candidate
    )
    store = merged._store
    key = fields["bucket_key"]

    assert fields["bucket_count"] == store.bins[key - store.offset]
    assert fields["source_keys"] == store.represented_range(key)
    assert fields["value_low"] <= decision.value <= fields["value_high"]
    relative_error = abs(decision.value - fields["value_low"]) / fields["value_low"]
    assert relative_error <= contract.bound(0.5) + 1e-12


def test_old_collapse_is_not_recoverable_by_more_buckets():
    old = _sketch(
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000],
        bin_limit=8,
    )
    capability = Capability.from_sketch(old, "lowest")
    assert capability.positive_range is not None
    unit = _unit("legacy", old, _sketch([1000] * 5, bin_limit=8))
    contract = _contract("legacy", percentiles=(0.1, 0.9), bound=0.02)

    tight = CandidateConfiguration("tight", 8, "lowest", 0.01)
    huge = CandidateConfiguration("huge", 4096, "lowest", 0.01)
    plan = plan_rollup([unit], [contract], [tight, huge], budget=100000)

    decisions = {decision.percentile: decision for decision in plan.decisions}
    assert decisions[0.1].certified is False
    assert decisions[0.1].reason == "rank-fell-in-collapsed-bucket"
    assert decisions[0.1].inherited_collapse is True
    assert decisions[0.1].witnesses
    witness = decisions[0.1].witnesses[0]
    assert witness.damaged_range is not None
    low, high = witness.damaged_range
    capability_low, capability_high = capability.positive_range
    assert low <= capability_high and high >= capability_low

    for candidate in (tight, huge):
        outcome = plan.candidate_outcomes[unit.key][candidate.name]
        assert outcome["decisions"][0].certified is False
        assert outcome["decisions"][0].inherited_collapse is True


def test_budget_tradeoff_reports_witness_per_candidate():
    old_a = _sketch(range(1, 101))
    old_b = _sketch(range(200, 301))
    contract_a = _contract("a", percentiles=(0.5,), priorities={0.5: 10})
    contract_b = _contract("b", percentiles=(0.5,), priorities={0.5: 1})
    candidate = CandidateConfiguration("standard", 64, "lowest", 0.01)

    plan = plan_rollup(
        [_unit("a", old_a), _unit("b", old_b)],
        [contract_a, contract_b],
        [candidate],
        budget=128,
    )

    assert plan.issued == [("2026-11-22T10:31", "a", 0.5)]
    assert plan.used == 128
    shortfalls = {shortfall.tenant: shortfall for shortfall in plan.shortfalls}
    assert "a" not in shortfalls
    assert shortfalls["b"].reason == "budget-tradeoff"
    assert "standard" in shortfalls["b"].candidates


def test_volume_scenarios_keep_ranks_far_from_damaged_edge_certifiable():
    old = _sketch(range(1, 101))
    unit = WindowSketch(
        window="w",
        tenant="steady",
        old=old,
        capability=Capability.from_sketch(old, "lowest"),
        scenarios=[
            VolumeScenario("calm", 0, "none"),
            VolumeScenario("low-20", 20, "low"),
            VolumeScenario("high-20", 20, "high"),
        ],
    )
    contract = _contract("steady", percentiles=(0.5,), bound=0.02)
    candidate = CandidateConfiguration("standard", 128, "lowest", 0.01)

    plan = plan_rollup([unit], [contract], [candidate], budget=1024)

    decision = plan.certified[0]
    scenario_names = [item["scenario"] for item in decision.evidence.scenarios]
    assert scenario_names == ["calm", "low-20", "high-20"]
    assert all(item["certified"] for item in decision.evidence.scenarios)


def test_unknown_incoming_volume_yields_rank_witnesses():
    old = _sketch(range(1, 101), bin_limit=512)
    unit = _unit("volatile", old, incoming_range=(0, 20))
    contract = _contract("volatile", percentiles=(0.1,), bound=0.02)
    candidate = CandidateConfiguration("standard", 128, "lowest", 0.01)

    plan = plan_rollup([unit], [contract], [candidate], budget=1024)

    decision = plan.decisions[0]
    assert decision.certified is False
    assert decision.reason == "rank-in-unknown-incoming-values"
    witness = decision.witnesses[0]
    assert witness.placement == "low"
    assert witness.extra == 20
    assert witness.known_rank is None
    assert witness.future_rank == pytest.approx(0.1 * (100 + 20 - 1))


def test_zero_report_is_certified_only_when_zero_bucket_is_exactly_zero():
    zeros = _sketch([0, 0, 0])
    contract = _contract("zeros", percentiles=(0.5,), bound=0.01)
    candidate = CandidateConfiguration("standard", 64, "lowest", 0.01)
    plan = plan_rollup([_unit("zeros", zeros)], [contract], [candidate], budget=128)
    assert plan.certified[0].value == 0
    assert plan.certified[0].evidence.scenarios[0]["fields"]["exact_zero_count"] == 3

    contaminated = _sketch([0, 0, 5e-324])
    plan = plan_rollup(
        [_unit("zeros", contaminated)], [contract], [candidate], budget=128
    )
    assert plan.decisions[0].certified is False
    assert plan.decisions[0].reason == "zero-bucket-contains-nonzero-values"


def test_mapping_mismatch_makes_candidate_inapplicable():
    old = _sketch(range(1, 101), relative_accuracy=0.01)
    capability = Capability.from_sketch(old, "lowest")
    unit = _unit("pinned", old, capability=capability)
    contract = _contract("pinned", percentiles=(0.5,), bound=0.02)
    candidate = CandidateConfiguration("coarse", 64, "lowest", 0.02)

    plan = plan_rollup([unit], [contract], [candidate], budget=1024)

    assert plan.decisions == []
    assert plan.shortfalls[0].reason == "no-applicable-candidate"
    assert (
        plan.candidate_outcomes[unit.key]["coarse"]["reason"]
        == "mapping-incompatible-with-old-summary"
    )


def test_merge_without_plan_preserves_ordinary_merge():
    target = DDSketch(0.01)
    source = DDSketch(0.01)
    for value in range(1, 101):
        source.add(value)
    merge_without_plan(target, source)
    assert target.count == 100
    assert target.get_quantile_value(0.5) == pytest.approx(50, rel=0.01)


def test_collapse_provenance_tracks_both_edges_and_survives_absorb():
    lowest = _sketch([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100], bin_limit=4)
    low, high = lowest._store.represented_range(lowest._store.min_key)
    assert low < high

    highest = _sketch(
        [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100],
        bin_limit=4,
        cls=LogCollapsingHighestDenseDDSketch,
    )
    low, high = highest._store.represented_range(highest._store.max_key)
    assert low < high

    absorbed = LogCollapsingLowestDenseDDSketch(0.01, 4)
    absorbed.absorb(lowest)
    assert absorbed._store.represented_range(absorbed._store.min_key) == (
        lowest._store.represented_range(lowest._store.min_key)
    )


def test_dp_matches_exhaustive_search_on_small_instance():
    units = [
        _unit("a", _sketch(range(1, 101))),
        _unit("b", _sketch(range(1, 101))),
    ]
    contracts = [_contract("a"), _contract("b")]
    candidates = [
        CandidateConfiguration("small", 4, "lowest", 0.01),
        CandidateConfiguration("medium", 8, "lowest", 0.01),
    ]

    plan = plan_rollup(units, contracts, candidates, budget=32)
    best = None
    for first in candidates:
        for second in candidates:
            if first.cost() + second.cost() > 32:
                continue
            score = 0
            for unit, configuration in zip(units, (first, second)):
                outcome = plan.candidate_outcomes[unit.key][configuration.name]
                score += sum(
                    1 for decision in outcome["decisions"] if decision.certified
                )
            best = score if best is None else max(best, score)

    assert len(plan.certified) == best


def test_empty_windows_and_missing_contracts_are_harmless():
    empty = _sketch([])
    contract = _contract("empty", percentiles=(0.5,), bound=0.02)
    candidate = CandidateConfiguration("standard", 64, "lowest", 0.01)
    plan = plan_rollup(
        [_unit("empty", empty), WindowSketch("w", "uncontracted", empty)],
        [contract],
        [candidate],
        budget=128,
    )
    assert plan.certified == []
    assert plan.decisions[0].reason == "empty-window"
    assert plan.shortfalls[0].reason == "empty-window"


def test_configuration_validation():
    with pytest.raises(ValueError):
        CandidateConfiguration("bad", 0, "lowest", 0.01).validate()
    with pytest.raises(ValueError):
        CandidateConfiguration("bad", 10, "middle", 0.01).validate()
    with pytest.raises(ValueError):
        CandidateConfiguration("bad", 10, "lowest", 1.0).validate()
    with pytest.raises(ValueError):
        plan_rollup([], [], [], budget=10)
    with pytest.raises(ValueError):
        plan_rollup([], [], [CandidateConfiguration("ok", 1, "lowest", 0.01)], budget=-1)


def test_merged_new_counts_push_rank_into_inherited_edge():
    # The old summary put almost all of its mass in the lowest bucket and
    # folded every lower key into that bucket.
    old = _sketch([1] * 990 + [1000] * 10, bin_limit=8)
    capability = Capability.from_sketch(old, "lowest")
    new = _sketch([1] * 20, bin_limit=8)
    unit = _unit("pushed", old, new, capability=capability)
    contract = _contract("pushed", percentiles=(0.5,), bound=0.02)

    tight = CandidateConfiguration("tight", 8, "lowest", 0.01)
    roomy = CandidateConfiguration("roomy", 4096, "lowest", 0.01)
    plan = plan_rollup([unit], [contract], [tight, roomy], budget=100000)

    decision = plan.decisions[0]
    assert decision.certified is False
    assert decision.reason == "rank-fell-in-collapsed-bucket"
    assert decision.inherited_collapse is True
    witness = decision.witnesses[0]
    assert witness.scenario == "base"
    assert witness.damaged_range[0] <= witness.bucket_key <= witness.damaged_range[1]
    assert witness.damaged_range[0] < witness.damaged_range[1]
    assert witness.future_rank == pytest.approx(0.5 * (1020 - 1))

    # More buckets do not repair the old summary: every candidate fails.
    for candidate in (tight, roomy):
        outcome = plan.candidate_outcomes[unit.key][candidate.name]
        assert outcome["decisions"][0].certified is False
        assert outcome["decisions"][0].inherited_collapse is True


def test_second_tenant_rank_stays_away_from_both_edges():
    old = _sketch(range(1, 101), bin_limit=512)
    unit = WindowSketch(
        window="w",
        tenant="steady",
        old=old,
        capability=Capability.from_sketch(old, "lowest"),
        scenarios=[
            VolumeScenario("calm", 0, "none"),
            VolumeScenario("low-40", 40, "low"),
            VolumeScenario("high-40", 40, "high"),
        ],
    )
    contract = _contract("steady", percentiles=(0.5,), bound=0.02)
    candidate = CandidateConfiguration("standard", 512, "lowest", 0.01)

    plan = plan_rollup([unit], [contract], [candidate], budget=4096)

    decision = plan.certified[0]
    for result in decision.evidence.scenarios:
        fields = result["fields"]
        assert result["certified"] is True
        assert fields["key_low"] > 0
        assert fields["key_high"] < fields["value_high"] * 1e6

    # Values differ by volume placement, so no single unconditional value is
    # claimed; the per-scenario values remain available for the caller.
    assert decision.value is None
    assert set(decision.values_by_scenario) == {"calm", "low-40", "high-40"}


def test_rank_to_scenario_value_depends_on_placement():
    old = _sketch(range(1, 101), bin_limit=512)
    unit = WindowSketch(
        window="w",
        tenant="moves",
        old=old,
        capability=Capability.from_sketch(old, "lowest"),
        scenarios=[
            VolumeScenario("calm", 0, "none"),
            VolumeScenario("low-40", 40, "low"),
            VolumeScenario("high-40", 40, "high"),
        ],
    )
    contract = _contract("moves", percentiles=(0.5,), bound=0.05)
    candidate = CandidateConfiguration("standard", 512, "lowest", 0.01)

    plan = plan_rollup([unit], [contract], [candidate], budget=4096)
    decision = plan.certified[0]

    # Each volume placement exposes its own conditional bucket value.
    assert set(decision.values_by_scenario) == {"calm", "low-40", "high-40"}
    values = list(decision.values_by_scenario.values())
    assert len({round(value, 6) for value in values}) == 3
    assert decision.value is None


def test_rank_pushed_into_edge_by_known_new_summary():
    # Old alone: the median sits in a retained bucket.
    old = _sketch([1] * 40 + [100] * 60, bin_limit=8)
    capability = Capability.from_sketch(old, "lowest")
    contract = _contract("pushed", percentiles=(0.5,), bound=0.02)
    candidate = CandidateConfiguration("roomy", 8, "lowest", 0.01)

    alone = WindowSketch(
        "w",
        "pushed",
        old,
        capability=capability,
        scenarios=[VolumeScenario("alone", 0, "none")],
    )
    alone_plan = plan_rollup([alone], [contract], [candidate], budget=64)
    assert alone_plan.certified

    # Merging the new summary moves the median into the old folded edge.
    new = _sketch([1] * 100, bin_limit=8)
    pushed = WindowSketch(
        "w",
        "pushed",
        old,
        capability=capability,
        scenarios=[
            VolumeScenario("alone", 0, "none"),
            VolumeScenario("after-new", 0, "none", sketch=new),
        ],
    )
    plan = plan_rollup([pushed], [contract], [candidate], budget=64)

    decision = plan.decisions[0]
    assert decision.certified is False
    assert decision.reason == "rank-fell-in-collapsed-bucket"
    by_scenario = {
        item["scenario"]: item for item in decision.evidence.scenarios
    }
    assert by_scenario["alone"]["certified"] is True
    assert by_scenario["after-new"]["certified"] is False
