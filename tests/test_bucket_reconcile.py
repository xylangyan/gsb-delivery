import json
import os
import time

import pytest

from prometheus_client import mmap_dict
from prometheus_client.bucket_reconcile import (
    Batch,
    BatchCoverage,
    BoundLine,
    Ledger,
    LedgerBlock,
    Manifest,
    ReconcileError,
    Reconciler,
    Snapshot,
    _bound_token,
    ledger_from_mmap_files,
    load_input,
    main,
    reconcile_file,
)
from prometheus_client.multiprocess import MultiProcessCollector
from prometheus_client.registry import CollectorRegistry


MANIFEST = Manifest(
    metric_name="request_latency_us",
    lines=(
        BoundLine(500.0, ("old", "new"), "v1"),
        BoundLine(750.0, ("new",), "v2"),
        BoundLine(1000.0, ("old", "new"), "v1"),
    ),
    value_domain=(0, None),
)


def make_ledgers():
    old = Ledger(
        ledger_id="old-scrape",
        layout="old",
        snapshot=Snapshot("snap-old", "scrapes/old.db", "mmap"),
        blocks=[LedgerBlock(
            process_id=7,
            increments={500.0: 2, 1000.0: 3},
            cumulative={500.0: 2, 1000.0: 5},
            count=5,
            latency_sum=3600,
            batches=[BatchCoverage("b-9", 1000.0)],
        )],
    )
    new = Ledger(
        ledger_id="new-scrape",
        layout="new",
        snapshot=Snapshot("snap-new", "scrapes/new.db", "mmap"),
        blocks=[LedgerBlock(
            process_id=7,
            increments={500.0: 2, 750.0: 1, 1000.0: 3},
            cumulative={500.0: 2, 750.0: 3, 1000.0: 6},
            count=6,
            latency_sum=4400,
            batches=[BatchCoverage("b-9", 750.0), BatchCoverage("b-12", 1000.0)],
        )],
    )
    batches = {
        "b-9": Batch("b-9", (600, 700, 750), size=1),
        "b-12": Batch("b-12", (800, 900), size=1),
    }
    return old, new, batches


def target_by_bound(report, bound):
    return next(target for target in report.targets if target.bound == float(bound))


def test_manifest_layouts_and_bounds():
    assert MANIFEST.layouts() == ("old", "new")
    assert MANIFEST.bounds("old") == (500.0, 1000.0, float("inf"))
    assert MANIFEST.bounds("new") == (500.0, 750.0, 1000.0, float("inf"))
    line = MANIFEST.line_for(750.0)
    assert line.layouts == ("new",)
    assert line.introduced == "v2"


def test_exact_when_layout_carries_the_line():
    ledger = Ledger(
        "new-only", "new",
        [LedgerBlock(1, {500.0: 2, 750.0: 1, 1000.0: 4})],
        Snapshot("s"),
    )
    report = Reconciler(MANIFEST, [ledger]).window_report([500, 750, 1000])[0]
    assert report.total_exact
    assert report.total_count == 7
    assert [(t.bound, t.value) for t in report.targets] == [
        (500.0, 2), (750.0, 3), (1000.0, 7),
    ]
    assert all(target.exact for target in report.targets)


def test_coarse_ledger_only_gives_range_and_evidence_for_750():
    ledger = Ledger(
        "old-only", "old",
        [LedgerBlock(1, {500.0: 2, 1000.0: 3})],
        Snapshot("snap-coarse"),
    )
    report = Reconciler(MANIFEST, [ledger]).window_report([500, 750, 1000])[0]
    at_500 = target_by_bound(report, 500)
    at_750 = target_by_bound(report, 750)
    at_1000 = target_by_bound(report, 1000)
    assert (at_500.exact, at_500.value) == (True, 2)
    assert not at_750.exact
    assert at_750.minimum == 2 and at_750.maximum == 5
    assert at_1000.exact and at_1000.value == 5
    evidence = at_750.evidence[0]
    assert evidence["kind"] == "unpinned_split"
    assert evidence["snapshots"][0]["snapshot"] == "snap-coarse"
    assert evidence["bound_line"]["bound"] == "750"
    assert evidence["bound_line"]["layouts"] == ["new"]


def test_overlapping_batch_is_joined_not_double_counted():
    old, new, batches = make_ledgers()
    report = Reconciler(MANIFEST, [old, new], batches).window_report(
        [500, 750, 1000]
    )[0]
    # The window union is at least the bigger scrape (6) and at most 5 + 6
    # minus the one batch both ledgers share (10); it is never 11.
    assert report.total_min == 6
    assert report.total_max == 10
    assert [item["batch_id"] for item in report.overlaps] == ["b-9"]
    overlap = report.overlaps[0]
    assert overlap["ledgers"] == ["new-scrape", "old-scrape"]
    at_750 = target_by_bound(report, 750)
    assert (at_750.minimum, at_750.maximum) == (3, 7)


def test_pair_feasible_returns_witness_that_reproduces_counts():
    old, new, batches = make_ledgers()
    reconciler = Reconciler(MANIFEST, [old, new], batches)
    report = reconciler.window_report(
        [750, 1000], [{"bounds": [750, 1000], "counts": [4, 7]}]
    )[0]
    pair = report.pair_results[0]
    assert pair.feasible
    assert pair.witness is not None

    # Recompute the window counts straight from the returned allocation:
    # shared strip counts plus each ledger's non-negative residuals.
    block = pair.witness["blocks"][0]
    shared_under = {750: 0, 1000: 0}
    for klass in block["shared_classes"]:
        for strip in klass["strips"]:
            count = strip["shared_count"]
            if strip["strip"].endswith("500]") or strip["strip"].endswith("750]"):
                shared_under[750] += count
            shared_under[1000] += count
    residual_under = {750: 0, 1000: 0}
    for residual in block["ledger_residuals"]:
        count = residual["residual_count"]
        assert count >= 0
        strips = "".join(residual["band_strips"])
        if "1000" not in strips and "+Inf" not in strips:
            residual_under[750] += count
        residual_under[1000] += count
    assert shared_under[750] + residual_under[750] == 4
    assert shared_under[1000] + residual_under[1000] == 7


def test_pair_infeasible_points_at_limiting_snapshots():
    old, new, batches = make_ledgers()
    reconciler = Reconciler(MANIFEST, [old, new], batches)
    report = reconciler.window_report(
        [750, 1000], [{"bounds": [750, 1000], "counts": [0, 11]}]
    )[0]
    pair = report.pair_results[0]
    assert not pair.feasible
    assert pair.witness is None
    assert pair.achievable == ((3, 6), (7, 10))
    snapshots = {
        snap["snapshot"]
        for item in pair.evidence for snap in item["snapshots"]
    }
    assert snapshots == {"snap-old", "snap-new"}
    pinning = pair.evidence[0]["pinning_constraints"]
    assert pinning and all("snapshot" in note for note in pinning)


def test_increment_cumulative_mismatch_is_rejected_with_snapshot():
    bad = Ledger(
        "broken", "old",
        [LedgerBlock(
            7, {500.0: 2, 1000.0: 3}, cumulative={500.0: 9, 1000.0: 5},
        )],
        Snapshot("snap-broken"),
    )
    with pytest.raises(ReconcileError, match="snap-broken"):
        Reconciler(MANIFEST, [bad])


def test_unknown_layout_and_bound_are_rejected():
    ledger = Ledger(
        "mystery", "future",
        [LedgerBlock(1, {500.0: 1})],
        Snapshot("s"),
    )
    with pytest.raises(ReconcileError, match="unknown layout"):
        Reconciler(MANIFEST, [ledger])

    ledger = Ledger(
        "old-bad", "old",
        [LedgerBlock(1, {500.0: 1, 750.0: 1})],
        Snapshot("s"),
    )
    with pytest.raises(ReconcileError, match="has no line"):
        Reconciler(MANIFEST, [ledger])


def test_reused_pid_needs_distinct_generation():
    first = Ledger(
        "a", "old",
        [LedgerBlock(9, {500.0: 3, 1000.0: 1}, generation=0, latency_sum=1500)],
        Snapshot("sa"),
    )
    second = Ledger(
        "b", "old",
        [LedgerBlock(9, {500.0: 1, 1000.0: 0}, generation=1, latency_sum=400)],
        Snapshot("sb"),
    )
    report = Reconciler(MANIFEST, [first, second]).window_report([500, 1000])[0]
    assert report.total_count == 5
    assert {key.process_id for key in reconciler_keys(first, second)} == {9}
    assert {key.generation for key in reconciler_keys(first, second)} == {0, 1}


def reconciler_keys(*ledgers):
    return Reconciler(MANIFEST, list(ledgers)).block_keys


def test_impossible_latency_sum_is_rejected():
    ledger = Ledger(
        "n", "new",
        [LedgerBlock(1, {500.0: 2, 750.0: 0, 1000.0: 0}, latency_sum=999999)],
        Snapshot("s"),
    )
    with pytest.raises(ReconcileError, match="latency sum"):
        Reconciler(MANIFEST, [ledger]).window_report([750])


def test_equal_labels_do_not_imply_overlap():
    # Two processes carrying identical labels stay separate blocks; batches only
    # overlap when they reference the same registered batch id.
    left = Ledger(
        "l", "new",
        [LedgerBlock(1, {500.0: 1, 750.0: 0, 1000.0: 0}, labels={"route": "/x"})],
        Snapshot("sl"),
    )
    right = Ledger(
        "r", "new",
        [LedgerBlock(2, {500.0: 1, 750.0: 0, 1000.0: 0}, labels={"route": "/x"})],
        Snapshot("sr"),
    )
    report = Reconciler(MANIFEST, [left, right]).window_report([500])[0]
    assert report.total_count == 2
    assert report.overlaps == []


def test_large_counts_do_not_enumerate_items_or_microseconds():
    old = Ledger(
        "o", "old", [LedgerBlock(2, {500.0: 400_000, 1000.0: 600_000})],
        Snapshot("so"),
    )
    new = Ledger(
        "n", "new",
        [LedgerBlock(2, {500.0: 400_000, 750.0: 250_000, 1000.0: 450_000})],
        Snapshot("sn"),
    )
    start = time.monotonic()
    report = Reconciler(MANIFEST, [old, new]).window_report([500, 750, 1000])[0]
    assert time.monotonic() - start < 5
    at_750 = target_by_bound(report, 750)
    assert (at_750.minimum, at_750.maximum) == (650_000, 1_650_000)
    # Decision variables are strip counts, never per-item/per-microsecond.
    reconciler = Reconciler(MANIFEST, [old, new])
    assert len(reconciler.domains) <= reconciler.strip_count


def _write_histogram_mmap(path, pid, metric, band_increments, latency_sum):
    # Real multiprocess mmap files hold one increment counter per histogram
    # band (an observation increments exactly one band); the cumulative le
    # display is only assembled later by MultiProcessCollector.merge.
    filename = os.path.join(path, f"histogram_{pid}.db")
    store = mmap_dict.MmapedDict(filename)
    help_text = "help"
    total = sum(band_increments.values())
    for bound, value in band_increments.items():
        key = mmap_dict.mmap_key(
            metric, metric + "_bucket", ["le"], [_bound_token(bound)], help_text
        )
        store.write_value(key, float(value), 0.0)
    inf_key = mmap_dict.mmap_key(
        metric, metric + "_bucket", ["le"], ["+Inf"], help_text
    )
    store.write_value(inf_key, float(total), 0.0)
    sum_key = mmap_dict.mmap_key(metric, metric + "_sum", [], [], help_text)
    store.write_value(sum_key, float(latency_sum), 0.0)
    store.close()
    return filename


def test_mmap_ledger_matches_collect_totals_and_hand_allocation(tmp_path):
    metric = "request_latency_us"
    # One process file in the old (coarse) layout and one in the new layout.
    # Values written are per-band increments; cumulative displays are derived.
    old_file = _write_histogram_mmap(
        tmp_path, 101, metric,
        {500.0: 2, 1000.0: 3}, latency_sum=3600,
    )
    new_file = _write_histogram_mmap(
        tmp_path, 202, metric,
        {500.0: 2, 750.0: 1, 1000.0: 3}, latency_sum=4400,
    )

    # Authoritative totals straight from the original multi-process collector.
    registry = CollectorRegistry()
    MultiProcessCollector(registry, path=str(tmp_path))
    collected = {
        sample.labels["le"]: sample.value
        for metric_family in registry.collect()
        for sample in metric_family.samples
        if sample.name == metric + "_bucket"
    }

    old_ledger = ledger_from_mmap_files(
        "old-scrape", "old", [old_file], Snapshot("snap-old", old_file, "mmap")
    )
    new_ledger = ledger_from_mmap_files(
        "new-scrape", "new", [new_file], Snapshot("snap-new", new_file, "mmap")
    )
    assert old_ledger.blocks[0].increments == {500.0: 2, 1000.0: 3}
    assert old_ledger.blocks[0].cumulative == {500.0: 2, 1000.0: 5}
    assert new_ledger.blocks[0].increments == {500.0: 2, 750.0: 1, 1000.0: 3}

    # Separate processes, so the window is simply the union; every published
    # cumulative line must be reproduced exactly and agree with collect.
    report = Reconciler(
        MANIFEST, [old_ledger, new_ledger]
    ).window_report([500, 750, 1000])[0]
    at_500 = target_by_bound(report, 500)
    at_1000 = target_by_bound(report, 1000)
    assert at_500.exact and at_500.value == int(collected["500.0"]) == 4
    assert at_1000.exact and at_1000.value == int(collected["1000.0"]) == 11

    # Hand allocation for the new process's six items: two at 480us (in the
    # <=500 strip), one at 750us (upper-bound inclusive, so still in le<=750),
    # and three in (750, 1000]. This reproduces the new layout's cumulative
    # lines exactly without enumerating any microsecond continuum.
    new_block = new_ledger.blocks[0]
    hand_values = [480, 480, 750, 900, 820, 760]
    hand_cumulative = {
        bound: sum(1 for value in hand_values if value <= bound)
        for bound in (500.0, 750.0, 1000.0)
    }
    assert hand_cumulative[500.0] == 2
    assert hand_cumulative[750.0] == 3
    assert hand_cumulative == new_block.cumulative


def test_json_document_round_trip_and_cli(tmp_path, capsys):
    old, new, batches = make_ledgers()
    document = {
        "manifest": {
            "metric_name": MANIFEST.metric_name,
            "lines": [
                {"bound": 500, "layouts": ["old", "new"], "introduced": "v1"},
                {"bound": 750, "layouts": ["new"], "introduced": "v2"},
                {"bound": 1000, "layouts": ["old", "new"], "introduced": "v1"},
            ],
            "value_domain": [0, None],
        },
        "ledgers": [
            {
                "ledger_id": "old-scrape", "layout": "old",
                "snapshot": {"snapshot_id": "snap-old"},
                "blocks": [{
                    "process_id": 7,
                    "increments": {"500": 2, "1000": 3},
                    "cumulative": {"500": 2, "1000": 5},
                    "sum": 3600,
                    "batches": [{"batch_id": "b-9", "bound": 1000}],
                }],
            },
            {
                "ledger_id": "new-scrape", "layout": "new",
                "snapshot": {"snapshot_id": "snap-new"},
                "blocks": [{
                    "process_id": 7,
                    "increments": {"500": 2, "750": 1, "1000": 3},
                    "cumulative": {"500": 2, "750": 3, "1000": 6},
                    "sum": 4400,
                    "batches": [
                        {"batch_id": "b-9", "bound": 750},
                        {"batch_id": "b-12", "bound": 1000},
                    ],
                }],
            },
        ],
        "batches": [
            {"batch_id": "b-9", "allowed_values": [600, 700, 750], "size": 1},
            {"batch_id": "b-12", "allowed_values": [800, 900], "size": 1},
        ],
        "target_bounds": [500, 750, 1000],
        "pair_queries": [
            {"bounds": [750, 1000], "counts": [4, 7]},
        ],
    }
    path = tmp_path / "scrape.json"
    path.write_text(json.dumps(document))

    manifest, ledgers, loaded_batches, targets, pairs = load_input(str(path))
    assert manifest.metric_name == MANIFEST.metric_name
    assert len(ledgers) == 2 and len(loaded_batches) == 2
    assert targets == [500, 750, 1000]

    reports = reconcile_file(str(path))
    assert reports[0].pair_results[0].feasible

    rc = main([str(path), "--target", "750", "--pair", "750:3,1000:6"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    target = next(
        item for item in payload[0]["targets"] if item["bound"] == 750
    )
    assert target["minimum"] == 3
    assert payload[0]["pair_results"][0]["feasible"] is True


def test_reports_are_grouped_by_labels():
    ledgers = [
        Ledger("l", "new", [
            LedgerBlock(1, {500.0: 1, 750.0: 0, 1000.0: 0}, labels={"route": "/a"}),
            LedgerBlock(2, {500.0: 2, 750.0: 0, 1000.0: 0}, labels={"route": "/b"}),
        ], Snapshot("s")),
    ]
    reports = Reconciler(MANIFEST, ledgers).window_report([500])
    groups = {tuple(report.labels_group.items()): report.total_count for report in reports}
    assert groups == {(("route", "/a"),): 1, (("route", "/b"),): 2}


def test_value_domain_that_empties_a_strip_is_rejected():
    # The (10, +Inf) strip has no integer in a domain capped at 10, so building
    # the strip geometry must fail loudly rather than silently dropping items.
    manifest = Manifest(
        "tight",
        lines=(BoundLine(10.0, ("old", "new")),),
        value_domain=(0, 10),
    )
    ledger = Ledger(
        "l", "old", [LedgerBlock(1, {10.0: 1})], Snapshot("s"),
    )
    with pytest.raises(ReconcileError, match="declared value domain"):
        Reconciler(manifest, [ledger])


def test_batch_value_domain_pins_shared_count():
    # A batch both ledgers cover, restricted to the (500, 750] strip, forces at
    # least one shared item there, tightening the corresponding shared variable.
    old, new, default_batches = make_ledgers()
    batches = dict(default_batches)
    batches["b-9"] = Batch("b-9", (700, 750), size=1)
    # b-9 stays registered in both ledgers (old under 1000, new under 750).
    assert {c.batch_id for c in old.blocks[0].batches} == {"b-9"}
    reconciler = Reconciler(MANIFEST, [old, new], batches)
    shared_strip_vars = [
        name for name in reconciler.domains
        if name.endswith("|s1") and "new-scrape+old-scrape" in name
    ]
    assert len(shared_strip_vars) == 1
    name = shared_strip_vars[0]
    assert reconciler._optimize({name: 1}, False) == 1

    without = Reconciler(MANIFEST, [old, new])
    assert without._optimize({name: 1}, False) == 0


def test_inf_target_reports_total_and_json_safe():
    from prometheus_client.bucket_reconcile import _json_safe, INF
    ledger = Ledger(
        "l", "new",
        [LedgerBlock(1, {500.0: 2, 750.0: 1, 1000.0: 4})],
        Snapshot("s"),
    )
    report = Reconciler(MANIFEST, [ledger]).window_report([INF])[0]
    target = report.targets[0]
    assert target.bound == INF and target.exact and target.value == 7
    encoded = _json_safe(report.to_dict())
    assert encoded["targets"][0]["bound"] == "+Inf"


def test_pair_query_validates_arity():
    reconciler = Reconciler(
        MANIFEST, [Ledger("l", "new", [LedgerBlock(1, {500.0: 1})], Snapshot("s"))]
    )
    with pytest.raises(ReconcileError, match="exactly two bounds"):
        reconciler.window_report([], [{"bounds": [750], "counts": [1]}])
    with pytest.raises(ReconcileError, match="exactly two counts"):
        reconciler.window_report([], [{"bounds": [750, 1000], "counts": [1]}])


def test_single_ledger_batch_can_span_bands():
    # A batch only the new ledger knows, whose allowed values span two bands,
    # must reserve room in aggregate rather than pinning each band separately.
    ledger = Ledger(
        "new", "new",
        [LedgerBlock(
            3, {500.0: 1, 750.0: 1, 1000.0: 1},
            batches=[BatchCoverage("only-new", 1000.0)],
        )],
        Snapshot("snap-new"),
    )
    reconciler = Reconciler(
        MANIFEST, [ledger], {"only-new": Batch("only-new", (480, 700), size=2)}
    )
    report = reconciler.window_report([1000])[0]
    assert report.total_count == 3
    assert report.overlaps == []
