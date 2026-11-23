"""Tests for the certified, mergeable quantile summary.

Every guarantee in the ingestion contract is exercised here:

* stable window identity/version, integer units, duplicate-value and
  duplicate-delivery de-duplication;
* rank error defined against every original observation;
* byte-identical canonical summaries and identical proof results along two
  differently shaped aggregation trees with retries;
* certificates that stay valid when merging upward, without raw values or
  per-window summaries at the root;
* same-precision alternative summaries and retractable-boundary replacement;
* legacy ``TDigest.to_dict`` windows that stay readable but cannot be
  certified, with the gap source named precisely.
"""

import json
import random
from bisect import bisect_right

import pytest

from tdigest import TDigest
from tdigest.certified import (
    AlternativeSummary,
    CertifiedDigest,
    CertificationError,
    LegacyWindow,
    validate_two_trees,
)

EPS = 0.05


def _make_window(seed, window_id, size=150, epsilon=EPS, domain=1000,
                 duplicate_extra=8):
    rng = random.Random(seed)
    values = [rng.randint(0, domain) for _ in range(size)]
    ids = ["%s-r%d" % (window_id, i) for i in range(size)]
    # Re-deliver a prefix (same record ids) plus repeated values inside.
    payload_values = values + values[:duplicate_extra]
    payload_ids = ids + ids[:duplicate_extra]
    digest = CertifiedDigest.from_window(
        window_id, 1, payload_values, epsilon=epsilon,
        record_ids=payload_ids)
    return digest, values


def _assert_rank_guarantee(digest, raw_values):
    """The proven interval always contains the true rank and is epsilon-wide."""
    store = digest._final_store()
    ordered = sorted(raw_values)
    cap = store.block_cap
    # block_cap = floor(epsilon * n) (>=1), so the relative width never
    # exceeds the configured precision.
    assert cap <= max(1, int(digest.epsilon * store.n))
    for value in set(ordered):
        low, high = store.rank_bounds(value)
        true_count = bisect_right(ordered, value)
        assert low <= true_count <= high
        assert high - low <= cap
    cert = digest.certificate()
    assert cert["relative_rank_error"] <= digest.epsilon + 1e-12


class TestWindowConstruction(object):

    def test_duplicate_record_ids_are_counted_once(self):
        values = [1, 2, 2, 3, 1, 4]
        ids = ["a", "b", "c", "d", "a", "e"]  # 'a' delivered twice
        digest = CertifiedDigest.from_window("w", 1, values,
                                             epsilon=0.1, record_ids=ids)
        assert digest.n == 5
        assert digest.window_ids == ["w"]

    def test_integer_units_and_stable_identity_version(self):
        digest = CertifiedDigest.from_window("win-7", 3, [1.0, 2.0, 3.0],
                                             epsilon=0.2)
        payload = digest.to_dict()
        source = payload["sources"][0]
        assert source["window_id"] == "win-7"
        assert source["version"] == 3
        assert all(isinstance(t[0], int)
                   for t in payload["store"]["tuples"])

    def test_rank_error_against_every_original_observation(self):
        digest, raw = _make_window(11, "w", size=400)
        _assert_rank_guarantee(digest, raw)

    def test_heavy_ties_remain_provable_within_block_budget(self):
        values = [5] * 500 + [6] * 300 + [7] * 10
        digest = CertifiedDigest.from_window("ties", 1, values, epsilon=0.05)
        store = digest._final_store()
        assert all(weight <= store.block_cap
                   for _v, weight, _d in store.tuples)
        _assert_rank_guarantee(digest, values)
        # The repeated value is bracketed, never mislocated beyond a block.
        low, high = digest.rank_interval(6)
        assert low <= 800 <= high


class TestTreeIndependence(object):

    def _batch(self):
        windows = []
        raw = []
        for w in range(9):
            digest, values = _make_window(100 + w, "win%d" % w)
            windows.append(digest)
            raw.extend(values)
        return windows, raw

    def test_two_trees_yield_identical_bytes_and_proof(self):
        windows, raw = self._batch()
        result = validate_two_trees(
            windows, partitions=4, tree_a_sizes=(2,), tree_b_sizes=(3,),
            retries={0: 3, 2: 1, 7: 2})
        assert result.canonical_equal
        assert result.certification_equal
        assert bool(result)
        assert result.root_a.n == len(raw)
        _assert_rank_guarantee(result.root_a, raw)
        _assert_rank_guarantee(result.root_b, raw)

    def test_canonical_bytes_ignore_pruning_and_grouping(self):
        windows, _ = self._batch()
        result = validate_two_trees(windows, partitions=3)
        assert result.root_a.to_canonical_bytes() == \
            result.root_b.to_canonical_bytes()
        # One tree pruned every internal node, the other kept full records.
        assert result.root_a.is_pruned != result.root_b.is_pruned

    def test_repeated_merges_and_retries_are_idempotent(self):
        windows, raw = self._batch()
        once = windows[0].merge(*windows[1:])
        repeated = windows[0].merge(windows[0], windows[1], *windows[2:])
        assert once.to_canonical_bytes() == repeated.to_canonical_bytes()
        assert repeated.n == len(raw)

    def test_associativity_of_merge(self):
        windows, _ = self._batch()
        left = windows[0].merge(windows[1]).merge(windows[2])
        right = windows[0].merge(windows[1].merge(windows[2]))
        assert left.to_canonical_bytes() == right.to_canonical_bytes()


class TestCertificatesAndRootMemory(object):

    def test_certificate_round_trips_upward(self):
        windows, raw = self._batch()
        lower = windows[0].merge(windows[1], windows[2])
        lower_cert = lower.certificate()
        root = lower.merge(*windows[3:])
        # The lower-layer certificate hash still matches its exact summary.
        assert lower.verify_certificate(lower_cert)
        # The named input is present in the upward certificate via its hash.
        root_cert = root.certificate()
        hashes = {w["store_hash"] for w in root_cert["windows"]
                  if w["window_id"] == windows[0].sources[0].window_id}
        assert lower_cert["windows"][0]["store_hash"] in hashes
        assert root.verify_certificate(root_cert)
        _assert_rank_guarantee(root, raw)

    def _batch(self):
        windows, raw = [], []
        for w in range(6):
            digest, values = _make_window(200 + w, "c%d" % w)
            windows.append(digest)
            raw.extend(values)
        return windows, raw

    def test_pruned_root_holds_no_raw_values_or_window_summaries(self):
        windows, _ = self._batch()
        root = windows[0].merge(*windows[1:]).prune()
        canonical = root.canonical_dict()
        serialized = json.dumps(canonical)
        assert "record_ids" not in serialized
        for window in canonical["windows"]:
            assert "tuples" not in window
        assert root.is_pruned
        # Still queryable and certifiable from the compact representation.
        assert root.certificate()["certificate_hash"]

    def test_memory_budget_scales_with_precision_and_sources(self):
        windows, _ = self._batch()
        root = windows[0].merge(*windows[1:]).finalize()
        budget = root.memory_budget(source_count=len(windows))
        assert budget["blocks"] <= budget["precision_budget"] + 1
        assert budget["manifests"] == len(windows)
        tighter = CertifiedDigest.from_window(
            "tight", 1, [i % 50 for i in range(5000)], epsilon=0.01)
        looser = CertifiedDigest.from_window(
            "loose", 1, [i % 50 for i in range(5000)], epsilon=0.1)
        assert tighter.memory_budget(1)["precision_budget"] > \
            looser.memory_budget(1)["precision_budget"]

    def test_pruned_intermediate_still_merges_upward(self):
        windows, raw = self._batch()
        lower_raw = raw[:2 * 150]  # only the two windows folded into lower
        lower = windows[0].merge(windows[1]).prune()
        extra, extra_raw = _make_window(299, "extra")
        upward = lower.merge(extra)
        assert upward.n == len(lower_raw) + len(extra_raw)
        assert upward.certified
        _assert_rank_guarantee(upward, lower_raw + extra_raw)

    def test_finalized_root_is_compressed_and_not_remergeable(self):
        windows, _ = self._batch()
        root = windows[0].merge(*windows[1:]).finalize()
        assert root._store.compressed
        with pytest.raises(CertificationError) as exc:
            root.merge(windows[0])
        assert exc.value.gap_sources[0]["reason"] == \
            "compressed_summary_not_mergeable"


class TestAlternativeSummaryAndReplacement(object):

    def test_alternative_summary_is_byte_equivalent(self):
        _, raw = _make_window(300, "alt")
        streaming = CertifiedDigest.from_window("alt", 1, raw, epsilon=EPS)
        bulk = AlternativeSummary.from_window("alt", 1, raw, epsilon=EPS)
        assert bulk.to_certified()._final_store().to_list() == \
            streaming._final_store().to_list()
        restored = AlternativeSummary.from_dict(bulk.to_dict())
        assert restored.to_certified().to_canonical_bytes() == \
            streaming.to_canonical_bytes()

    def test_replacement_only_at_retractable_boundary(self):
        first, raw = _make_window(301, "rep")
        neighbor, _ = _make_window(302, "nb")
        node = first.merge(neighbor)
        new_record = CertifiedDigest.from_window(
            "rep", 2, [v + 1 for v in raw], epsilon=EPS,
            supersedes=(1,)).sources[0]
        replaced = node.replace_window("rep", new_record)
        assert replaced.replacements == [{
            "window_id": "rep", "old_version": 1, "new_version": 2}]
        assert replaced.certified

        with pytest.raises(CertificationError) as exc:
            node.prune().replace_window("rep", new_record)
        assert exc.value.gap_sources[0]["reason"] == \
            "non_retractable_boundary"

    def test_replacement_requires_advancing_version(self):
        first, raw = _make_window(303, "rep")
        stale = CertifiedDigest.from_window(
            "rep", 1, raw, epsilon=EPS).sources[0]
        with pytest.raises(CertificationError) as exc:
            first.replace_window("rep", stale)
        assert exc.value.gap_sources[0]["reason"] == \
            "version_not_advancing"


class TestLegacyAndGaps(object):

    def _legacy(self, window_id="old"):
        td = TDigest()
        td.batch_update([random.Random(7).randint(0, 100)
                         for _ in range(60)])
        return CertifiedDigest.from_legacy_dict(window_id, 1, td.to_dict())

    def test_legacy_window_is_readable_but_uncertified(self):
        legacy = self._legacy()
        assert 0 <= legacy.percentile(50) <= 100
        with pytest.raises(CertificationError) as exc:
            legacy.certificate()
        assert exc.value.gap_sources[0]["window_id"] == "old"
        assert exc.value.gap_sources[0]["reason"] == \
            "legacy_tdigest_no_certificate"

    def test_legacy_gap_blocks_new_precision_certificate(self):
        good, _ = _make_window(400, "good")
        mixed = good.merge(self._legacy())
        assert not mixed.certified
        gap_ids = {g["window_id"] for g in mixed.gap_sources}
        assert "old" in gap_ids
        # Approximate values remain available despite the missing proof.
        assert mixed.percentile(50) is not None

    def test_ambiguous_retry_overwrite_is_named(self):
        a = CertifiedDigest.from_window(
            "x", 1, [1, 2, 3], epsilon=EPS, record_ids=["a", "b", "c"])
        b = CertifiedDigest.from_window(
            "x", 1, [1, 2, 9], epsilon=EPS, record_ids=["a", "b", "d"])
        merged = a.merge(b)
        assert not merged.certified
        assert any(g["window_id"] == "x" and
                   g["reason"] == "ambiguous_retry_overwrite"
                   for g in merged.gap_sources)

    def test_unverifiable_supersession_is_named(self):
        record = CertifiedDigest.from_window(
            "x", 2, [1, 2], epsilon=EPS, supersedes=(1,)).sources[0]
        standalone = CertifiedDigest(EPS, [record], record.store)
        merged = standalone.merge()
        assert any(g["reason"] == "unverifiable_supersession"
                   for g in merged.gap_sources)

    def test_two_trees_report_identical_gap_sources(self):
        good_windows = [_make_window(500 + i, "g%d" % i)[0]
                        for i in range(4)]
        legacy = self._legacy()
        leaves = good_windows + [legacy]
        result = validate_two_trees(leaves, partitions=2,
                                    tree_a_sizes=(2,), tree_b_sizes=(3,))
        assert result.canonical_equal
        assert result.certification_equal
        assert not result.root_a.certified
        view = result.details["tree_a_certificate"]
        assert view == result.details["tree_b_certificate"]
        assert ("old", "legacy_tdigest_no_certificate") in view["gaps"]


class TestSerialization(object):

    def test_dict_roundtrip_preserves_canonical_bytes(self):
        windows, raw = [], []
        for w in range(4):
            digest, values = _make_window(600 + w, "s%d" % w)
            windows.append(digest)
            raw.extend(values)
        root = windows[0].merge(*windows[1:])
        restored = CertifiedDigest.from_dict(root.to_dict())
        assert restored.to_canonical_bytes() == root.to_canonical_bytes()
        _assert_rank_guarantee(restored, raw)

    def test_bare_tdigest_dict_is_treated_as_legacy(self):
        td = TDigest()
        td.update(1)
        digest = CertifiedDigest.from_dict(td.to_dict())
        assert isinstance(digest.sources[0], LegacyWindow)
        assert not digest.certified

    def test_canonical_bytes_are_json_and_stable(self):
        digest, _ = _make_window(700, "j")
        raw_bytes = digest.to_canonical_bytes()
        decoded = json.loads(raw_bytes.decode("ascii"))
        assert decoded["schema"].startswith("certified-")
        assert digest.to_canonical_bytes() == raw_bytes
