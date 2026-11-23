import pickle
import random
import unittest
from unittest import mock

import numpy as np

from datasketch import (
    BatchIdConflictError,
    Correction,
    CorrectionConflictError,
    IncrementalWeightedMinHashIndex,
    NegativeFrequencyError,
    Retraction,
    UnknownVersionError,
    UnknownWordError,
)
from datasketch.weighted_minhash import WeightedMinHashGenerator

VOCAB = [f"w{i}" for i in range(24)]


def dense(frequencies, vocab=VOCAB):
    vector = np.zeros(len(vocab), dtype=np.float32)
    index = {term: i for i, term in enumerate(vocab)}
    for term, count in frequencies.items():
        vector[index[term]] = count
    return vector


def reference_signature(generator, frequencies, vocab=VOCAB):
    return generator.minhash(dense(frequencies, vocab))


def assert_signature_equal(test, actual, expected):
    test.assertIsNotNone(actual)
    test.assertEqual(actual.seed, expected.seed)
    test.assertTrue(np.array_equal(actual.hashvalues, expected.hashvalues), (actual.hashvalues, expected.hashvalues))


class TestIncrementalWeightedMinHashIndex(unittest.TestCase):
    def setUp(self):
        self.sample_size = 64
        self.seed = 7
        self.index = IncrementalWeightedMinHashIndex(VOCAB, sample_size=self.sample_size, seed=self.seed)
        self.generator = WeightedMinHashGenerator(len(VOCAB), sample_size=self.sample_size, seed=self.seed)

    # -- basic initialization and exactness ------------------------------
    def test_initialized_signatures_match_generator(self):
        documents = {
            "doc-a": {"w1": 3, "w4": 1, "w9": 7},
            "doc-b": {"w2": 1},
            "doc-c": {"w0": 2, "w3": 5, "w11": 1, "w12": 4},
        }
        version = self.index.initialize(documents)
        self.assertEqual(version, 1)
        snapshot = self.index.snapshot()
        for doc_id, frequencies in documents.items():
            assert_signature_equal(self, snapshot[doc_id], reference_signature(self.generator, frequencies))

    def test_every_signature_item_matches_full_recomputation(self):
        rng = random.Random(11)
        documents = {f"d{i}": {term: rng.randint(1, 9) for term in rng.sample(VOCAB, 8)} for i in range(6)}
        self.index.initialize(documents)
        aggregated = {doc_id: dict(freqs) for doc_id, freqs in documents.items()}
        applied_versions = 1

        for step in range(120):
            doc_id = f"d{rng.randrange(6)}"
            term = rng.choice(VOCAB)
            delta = rng.choice([-3, -2, -1, 1, 2, 3])
            if aggregated[doc_id].get(term, 0) + delta < 0:
                with self.assertRaises(NegativeFrequencyError):
                    self.index.apply_batch(f"reject-{step}", [Correction(f"rc-{step}", doc_id, term, delta)])
                self.assertEqual(self.index.current_version, applied_versions)
                continue
            self.index.apply_batch(f"b-{step}", [Correction(f"c-{step}", doc_id, term, delta)])
            applied_versions += 1
            aggregated[doc_id][term] = aggregated[doc_id].get(term, 0) + delta
            if aggregated[doc_id][term] == 0:
                del aggregated[doc_id][term]

            snapshot = self.index.snapshot()
            for other_id, frequencies in aggregated.items():
                if not frequencies:
                    self.assertNotIn(other_id, snapshot)
                    continue
                expected = reference_signature(self.generator, frequencies)
                assert_signature_equal(self, snapshot[other_id], expected)

    # -- the two-shard "A loses one in each shard, B rises" scenario ----
    def test_two_shards_each_subtract_a_but_together_change_signature(self):
        # seed=1, sample_size=128: A=22, B=21, two filler terms at 1.
        # One -1 for A leaves every item identical.  A second -1 changes
        # exactly one item, where the untouched B surfaces.
        sample_size = 128
        generator = WeightedMinHashGenerator(len(VOCAB), sample_size=sample_size, seed=1)
        frequencies = {"w0": 22, "w1": 21, "w2": 1, "w3": 1}
        base = reference_signature(generator, frequencies)
        once = dict(frequencies)
        once["w0"] -= 1
        after_one = reference_signature(generator, once)
        twice = dict(once)
        twice["w0"] -= 1
        after_two = reference_signature(generator, twice)
        self.assertTrue(np.array_equal(base.hashvalues, after_one.hashvalues))
        self.assertFalse(np.array_equal(after_one.hashvalues, after_two.hashvalues))

        index = IncrementalWeightedMinHashIndex(VOCAB, sample_size=sample_size, seed=1)
        index.initialize({"doc": frequencies})
        first = index.apply_batch("shard-1", [Correction("c1", "doc", "w0", -1)])
        self.assertEqual(first.changed_docs, ())
        assert_signature_equal(self, index.snapshot()["doc"], after_one)

        second = index.apply_batch("shard-2", [Correction("c2", "doc", "w0", -1)])
        self.assertEqual(second.changed_docs, ("doc",))
        assert_signature_equal(self, index.snapshot()["doc"], after_two)

        # B (w1) was never corrected but wins the changed sample item.
        differing = np.where(after_one.hashvalues[:, 0] != after_two.hashvalues[:, 0])[0]
        self.assertEqual(len(differing), 1)
        self.assertEqual(after_two.hashvalues[differing[0], 0], VOCAB.index("w1"))

    def test_single_application_matches_reference_and_joint_matches_reference(self):
        frequencies = {"w0": 5, "w1": 2, "w2": 1, "w3": 1}
        self.index.initialize({"doc": frequencies})
        once = dict(frequencies)
        once["w0"] -= 1
        self.index.apply_batch("one", [Correction("a", "doc", "w0", -1)])
        assert_signature_equal(self, self.index.snapshot()["doc"], reference_signature(self.generator, once))

        twice = dict(once)
        twice["w0"] -= 1
        if twice["w0"] == 0:
            del twice["w0"]
        self.index.apply_batch("two", [Correction("b", "doc", "w0", -1)])
        assert_signature_equal(self, self.index.snapshot()["doc"], reference_signature(self.generator, twice))

    # -- retractions, including out-of-order arrival ---------------------
    def test_retraction_arriving_before_correction_is_terminal(self):
        frequencies = {"w1": 2, "w2": 3}
        self.index.initialize({"doc": frequencies})
        version_before = self.index.current_version
        result = self.index.apply_batch("retract-early", retractions=[Retraction("late-correction")])
        self.assertEqual(result.version, version_before)
        self.assertEqual(self.index.current_version, version_before)

        late = self.index.apply_batch("late-batch", [Correction("late-correction", "doc", "w2", 10)])
        self.assertFalse(late.applied)
        self.assertEqual(self.index.frequencies("doc"), frequencies)
        self.assertEqual(self.index.effective_corrections(), ())
        assert_signature_equal(self, self.index.snapshot()["doc"], reference_signature(self.generator, frequencies))

    def test_late_retraction_removes_earlier_correction(self):
        frequencies = {"w1": 1, "w2": 1}
        self.index.initialize({"doc": frequencies})
        self.index.apply_batch("add", [Correction("c1", "doc", "w2", 4)])
        with_correction = dict(frequencies)
        with_correction["w2"] += 4
        assert_signature_equal(self, self.index.snapshot()["doc"], reference_signature(self.generator, with_correction))

        old_version = self.index.current_version
        old_snapshot = self.index.snapshot()
        self.index.apply_batch("retract", retractions=[Retraction("c1")])
        assert_signature_equal(self, self.index.snapshot()["doc"], reference_signature(self.generator, frequencies))
        # An old reader keeps seeing the version it was given.
        assert_signature_equal(self, old_snapshot["doc"], reference_signature(self.generator, with_correction))
        self.assertEqual(old_snapshot.version, old_version)
        self.assertEqual(self.index.snapshot(old_version).version, old_version)

    def test_retraction_and_correction_in_same_batch_never_applies(self):
        frequencies = {"w1": 4}
        self.index.initialize({"doc": frequencies})
        result = self.index.apply_batch(
            "mixed",
            [Correction("same", "doc", "w1", 5)],
            retractions=[Retraction("same")],
        )
        self.assertFalse(result.applied)
        self.assertEqual(self.index.frequencies("doc"), frequencies)

    # -- negative-frequency batches are all-or-nothing -------------------
    def test_negative_batch_is_rejected_atomically(self):
        self.index.initialize({"doc-a": {"w1": 2}, "doc-b": {"w2": 1}})
        version = self.index.current_version
        snapshot = self.index.snapshot()
        before = {doc_id: self.index.snapshot()[doc_id].hashvalues.copy() for doc_id in ("doc-a", "doc-b")}

        with self.assertRaises(NegativeFrequencyError) as raised:
            self.index.apply_batch(
                "bad",
                [
                    Correction("ok", "doc-a", "w1", 5),
                    Correction("bad", "doc-b", "w2", -2),
                ],
            )
        self.assertIn(("doc-b", "w2"), raised.exception.negative)
        self.assertEqual(self.index.current_version, version)
        self.assertIs(self.index.snapshot(), snapshot)
        for doc_id, values in before.items():
            self.assertTrue(np.array_equal(self.index.snapshot()[doc_id].hashvalues, values))
        self.assertEqual(self.index.effective_corrections(), ())

    def test_failed_batch_cannot_be_replayed_as_success(self):
        self.index.initialize({"doc": {"w1": 1}})
        with self.assertRaises(NegativeFrequencyError):
            self.index.apply_batch("bad", [Correction("c", "doc", "w1", -5)])
        # Same batch id with a valid payload is a fresh request, not a replay
        # of the failed one.
        result = self.index.apply_batch("bad", [Correction("c", "doc", "w1", 2)])
        self.assertFalse(result.replayed)
        self.assertEqual(self.index.frequencies("doc"), {"w1": 3})

    # -- idempotency -----------------------------------------------------
    def test_duplicate_batch_returns_original_result_without_double_apply(self):
        self.index.initialize({"doc": {"w1": 1, "w2": 1}})
        corrections = [Correction("c1", "doc", "w1", 4), Correction("c2", "doc", "w2", -1)]
        first = self.index.apply_batch("batch-1", corrections)
        after_first = self.index.snapshot()["doc"].hashvalues.copy()

        replay = self.index.apply_batch("batch-1", corrections)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.version, first.version)
        self.assertEqual(replay.changed_docs, first.changed_docs)
        self.assertTrue(np.array_equal(self.index.snapshot()["doc"].hashvalues, after_first))
        self.assertEqual(self.index.frequencies("doc"), {"w1": 5})

    def test_batch_id_reuse_with_different_payload_is_rejected(self):
        self.index.initialize({"doc": {"w1": 1}})
        self.index.apply_batch("batch", [Correction("c1", "doc", "w1", 1)])
        with self.assertRaises(BatchIdConflictError):
            self.index.apply_batch("batch", [Correction("c1", "doc", "w1", 2)])

    def test_correction_id_reuse_with_different_payload_is_rejected(self):
        self.index.initialize({"doc": {"w1": 1}})
        self.index.apply_batch("batch", [Correction("c1", "doc", "w1", 1)])
        with self.assertRaises(CorrectionConflictError):
            self.index.apply_batch("batch-2", [Correction("c1", "doc", "w1", 2)])

    def test_retry_after_injected_failure_matches_single_ordered_application(self):
        documents = {
            "doc-a": {"w0": 2, "w1": 1},
            "doc-b": {"w0": 1, "w2": 3},
        }
        self.index.initialize(documents)
        batch = [
            Correction("x1", "doc-a", "w0", -1),
            Correction("x2", "doc-b", "w2", 2),
            Correction("x3", "doc-a", "w1", -1),
        ]
        retractions = [Retraction("x3")]
        expected = IncrementalWeightedMinHashIndex(VOCAB, sample_size=self.sample_size, seed=self.seed)
        expected.initialize(documents)
        expected.apply_batch("batch", batch, retractions=retractions)

        # Fail while publishing, after document state has been mutated.
        real_publish = self.index._publish
        calls = {"n": 0}

        def flaky_publish(changes, removed):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("injected failure")
            return real_publish(changes, removed)

        with (
            mock.patch.object(self.index, "_publish", side_effect=flaky_publish),
            self.assertRaises(RuntimeError),
        ):
            self.index.apply_batch("batch", batch, retractions=retractions)

        self.assertEqual(self.index.current_version, 1)
        with self.assertRaises(UnknownVersionError):
            self.index.snapshot(2)
        retried = self.index.apply_batch("batch", batch, retractions=retractions)
        self.assertFalse(retried.replayed)
        self.assertEqual(self.index.current_version, 2)
        for doc_id in documents:
            assert_signature_equal(self, self.index.snapshot()[doc_id], expected.snapshot()[doc_id])

    # -- old readers and snapshot isolation ------------------------------
    def test_old_snapshot_does_not_observe_new_signatures(self):
        self.index.initialize({"doc": {"w1": 1}})
        old = self.index.snapshot()
        old_bytes = old["doc"].hashvalues.copy()

        for i in range(10):
            self.index.apply_batch(f"b{i}", [Correction(f"c{i}", "doc", "w1", 1)])
        self.assertFalse(np.array_equal(self.index.snapshot()["doc"].hashvalues, old_bytes))
        self.assertTrue(np.array_equal(old["doc"].hashvalues, old_bytes))
        with self.assertRaises(UnknownVersionError):
            self.index.snapshot(999)

    def test_document_without_positive_frequency_leaves_index(self):
        self.index.initialize({"doc": {"w1": 1}, "other": {"w2": 1}})
        result = self.index.apply_batch("remove", [Correction("c", "doc", "w1", -1)])
        self.assertIn("doc", result.removed_docs)
        self.assertNotIn("doc", self.index.snapshot())
        self.assertIsNone(self.index.signature("doc"))
        # Re-adding brings it back with a signature equal to a fresh computation.
        self.index.apply_batch("readd", [Correction("c2", "doc", "w3", 2)])
        assert_signature_equal(self, self.index.snapshot()["doc"], reference_signature(self.generator, {"w3": 2}))

    # -- recall by complete signature ------------------------------------
    def test_find_by_complete_signature(self):
        self.index.initialize({"a": {"w1": 3}, "b": {"w1": 3}, "c": {"w2": 3}})
        snapshot = self.index.snapshot()
        self.assertEqual(snapshot.find(reference_signature(self.generator, {"w1": 3})), ("a", "b"))
        self.assertEqual(snapshot.find(reference_signature(self.generator, {"w2": 3})), ("c",))

    # -- incrementality --------------------------------------------------
    def test_batch_does_not_rescan_unchanged_terms(self):
        frequencies = {term: 1 for term in VOCAB}
        frequencies["w0"] = 10
        self.index.initialize({"doc": frequencies, "other": {"w3": 2}})
        seen = []
        original = self.index._term_scores

        def recording(term, count):
            seen.append((term, count))
            return original(term, count)

        with mock.patch.object(self.index, "_term_scores", side_effect=recording):
            self.index.apply_batch("touch", [Correction("c", "doc", "w5", 4)])

        self.assertIn((VOCAB.index("w5"), 5), seen)
        # Only w5's new score is computed; the other 24 terms are untouched.
        self.assertTrue(all(term == VOCAB.index("w5") for term, _ in seen), seen)

    def test_unknown_word_is_rejected(self):
        self.index.initialize({"doc": {"w1": 1}})
        with self.assertRaises(UnknownWordError):
            self.index.apply_batch("bad", [Correction("c", "doc", "not-in-vocab", 1)])

    # -- numeric vocabulary / dense initialization -----------------------
    def test_integer_vocabulary_and_dense_vectors(self):
        index = IncrementalWeightedMinHashIndex(6, sample_size=32, seed=3)
        generator = WeightedMinHashGenerator(6, sample_size=32, seed=3)
        vector = np.array([2, 0, 6, 1, 0, 3], dtype=np.int64)
        index.initialize({"doc": vector})
        assert_signature_equal(self, index.snapshot()["doc"], generator.minhash(vector.astype(np.float32)))
        index.apply_batch("c", [Correction("c1", "doc", 2, -4)])
        expected = vector.astype(np.float32).copy()
        expected[2] -= 4
        assert_signature_equal(self, index.snapshot()["doc"], generator.minhash(expected))

    # -- persistence -----------------------------------------------------
    def test_tie_between_terms_matches_argmin_first_index(self):
        generator = WeightedMinHashGenerator(3, sample_size=8, seed=5)
        # Make term 1 a bit-identical copy of term 0's scores, forcing ties for
        # equal counts.  numpy's argmin picks the first dimension.
        generator.rs[:, 1] = generator.rs[:, 0]
        generator.betas[:, 1] = generator.betas[:, 0]
        generator.ln_cs[:, 1] = generator.ln_cs[:, 0]
        index = IncrementalWeightedMinHashIndex(generator=generator)
        index.initialize({"doc": {0: 3, 1: 3, 2: 1}})
        frequencies = np.array([3, 3, 1], dtype=np.float32)
        expected = generator.minhash(frequencies)
        assert_signature_equal(self, index.snapshot()["doc"], expected)
        # Where terms 0 and 1 tie and beat term 2, argmin's first-index rule
        # must pick term 0.
        tied = (expected.hashvalues[:, 0] == 0).sum()
        self.assertGreater(tied, 0)

    def test_pickle_round_trip_preserves_versions_and_idempotency(self):

        self.index.initialize({"doc": {"w1": 3, "w2": 1}})
        self.index.apply_batch("b1", [Correction("c1", "doc", "w1", -1)])
        restored = pickle.loads(pickle.dumps(self.index))
        self.assertEqual(restored.current_version, self.index.current_version)
        self.assertEqual(restored.frequencies("doc"), self.index.frequencies("doc"))
        replay = restored.apply_batch("b1", [Correction("c1", "doc", "w1", -1)])
        self.assertTrue(replay.replayed)
        assert_signature_equal(self, restored.snapshot()["doc"], self.index.snapshot()["doc"])

    # -- history retention ----------------------------------------------
    def test_max_history_still_keeps_pinned_older_snapshots(self):
        index = IncrementalWeightedMinHashIndex(VOCAB, sample_size=16, seed=1, max_history=2)
        index.initialize({"doc": {"w1": 1}})
        pinned = index.snapshot()
        for i in range(5):
            index.apply_batch(f"b{i}", [Correction(f"c{i}", "doc", "w1", 1)])
        self.assertEqual(index.versions, (0, 5, 6))
        with self.assertRaises(UnknownVersionError):
            index.snapshot(1)
        self.assertTrue(pinned["doc"].hashvalues.shape == (16, 2))


if __name__ == "__main__":
    unittest.main()
