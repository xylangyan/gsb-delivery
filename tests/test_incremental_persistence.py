# SPDX-License-Identifier: MIT

import io
import os
import pathlib
import shutil
import tempfile
import unittest

from drain3.file_persistence import FilePersistence
from drain3.incremental_persistence import (
    BASE_META_FILE,
    BASE_SNAPSHOT_FILE,
    IncrementalFilePersistence,
)
from drain3.template_miner import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig


MESSAGES = [
    "press red idle",       # line 1 -> cluster 1
    "sensor1 blue ready",   # line 2 -> cluster 2
    "sensor2 blue ready",   # line 3 -> cluster 2 template <*> blue ready, size 2
    "oven warm start",      # line 4 -> cluster 3, evicts cluster 1
    "yard blue ready",      # line 5 -> cluster 2, size 3
    "press blue ready",     # line 6 -> cluster 4, evicts cluster 3
]


def make_config() -> TemplateMinerConfig:
    # Unmasked Drain: depth 4, at most two child branches and two live clusters,
    # similarity threshold 0.6, numeric token parameterization enabled.
    config = TemplateMinerConfig()
    config.masking_instructions = []
    config.engine = "Drain"
    config.drain_depth = 4
    config.drain_max_children = 2
    config.drain_max_clusters = 2
    config.drain_sim_th = 0.6
    config.parametrize_numeric_tokens = True
    config.snapshot_compress_state = False
    return config


def cluster_signature(miner: TemplateMiner):
    return {
        cluster_id: (cluster.get_template(), cluster.size)
        for cluster_id, cluster in sorted(miner.drain.id_to_cluster.items())
    }


class IncrementalCheckpointTest(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.classic_snapshot = os.path.join(self.tmpdir, "classic.bin")
        self.checkpoint_dir = os.path.join(self.tmpdir, "checkpoints")
        os.makedirs(self.checkpoint_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def seed_base_from_classic_snapshot(self):
        """Train lines 1-2 with the classic full-snapshot mode and use its file
        as the base snapshot for the lightweight mode."""
        classic = TemplateMiner(FilePersistence(self.classic_snapshot), make_config())
        classic.add_log_message(MESSAGES[0])
        classic.add_log_message(MESSAGES[1])
        shutil.copy(self.classic_snapshot, os.path.join(self.checkpoint_dir, BASE_SNAPSHOT_FILE))

    def open_miner(self):
        persistence = IncrementalFilePersistence(self.checkpoint_dir)
        return TemplateMiner(persistence, make_config()), persistence

    # ------------------------------------------------------------- main flow

    def test_full_scenario_with_compaction_crashes_and_partial_write(self):
        self.seed_base_from_classic_snapshot()

        # --- lightweight mode starts at line 3 ---
        miner, persistence = self.open_miner()
        self.assertIsNone(miner.get_watermark())

        result = miner.add_log_message(MESSAGES[2], watermark=3)
        self.assertEqual("cluster_template_changed", result["change_type"])
        self.assertEqual(2, result["cluster_id"])
        self.assertEqual(2, result["cluster_size"])
        self.assertEqual("<*> blue ready", result["template_mined"])
        self.assertEqual(3, miner.get_watermark())
        self.assertEqual(1, persistence.checkpoint_count())
        # The checkpoint only carries the single changed cluster.
        record = persistence.journal_records[-1]
        self.assertEqual([2], [cluster["cluster_id"] for cluster in record["clusters"]])
        journal_after_line3 = persistence.journal_size_bytes()
        self.assertGreater(journal_after_line3, 0)

        # --- begin compaction up to line 3, keep ingesting before the switch ---
        miner.begin_checkpoint_compaction(3)
        self.assertTrue(persistence.compacting_in_progress())

        # Crash BEFORE the switch: restart discards the staged snapshot and
        # resumes from the complete line-3 checkpoint.
        crashed, crashed_persistence = self.open_miner()
        self.assertFalse(crashed_persistence.compacting_in_progress())
        self.assertEqual(3, crashed.get_watermark())
        self.assertEqual([1, 2], sorted(crashed.drain.id_to_cluster.keys()))
        self.assertEqual(2, crashed.drain.id_to_cluster[2].size)
        self.assertEqual("<*> blue ready", crashed.drain.id_to_cluster[2].get_template())

        # Redo the in-flight compaction and actually switch it this time, after
        # line 4 was ingested and checkpointed while compaction was staged.
        miner, persistence = self.open_miner()
        miner.begin_checkpoint_compaction(3)
        line4 = miner.add_log_message(MESSAGES[3], watermark=4)
        self.assertEqual(3, line4["cluster_id"])
        self.assertEqual([2, 3], sorted(miner.drain.id_to_cluster.keys()))
        miner.commit_checkpoint_compaction(3)

        # Base snapshot now covers line 3; the line-4 checkpoint is retained on
        # top of it instead of being replayed from the beginning of history.
        self.assertEqual(3, persistence.base_watermark)
        self.assertEqual(4, persistence.last_watermark())
        self.assertEqual(1, persistence.checkpoint_count())

        # Crash AFTER the switch: restart resumes after line 4.
        reopened, reopened_persistence = self.open_miner()
        self.assertEqual(4, reopened.get_watermark())
        self.assertEqual([2, 3], sorted(reopened.drain.id_to_cluster.keys()))
        self.assertEqual(2, reopened.drain.id_to_cluster[2].size)

        # --- lines 5 and 6 ---
        line5 = reopened.add_log_message(MESSAGES[4], watermark=5)
        self.assertEqual(2, line5["cluster_id"])
        self.assertEqual(3, line5["cluster_size"])

        line6 = reopened.add_log_message(MESSAGES[5], watermark=6)
        self.assertEqual(4, line6["cluster_id"])
        self.assertEqual([2, 4], sorted(reopened.drain.id_to_cluster.keys()))
        self.assertNotIn(3, reopened.drain.id_to_cluster)
        # The eviction was recorded in the journal.
        self.assertIn(3, reopened_persistence.journal_records[-1]["deleted_cluster_ids"])

        # A restart at the end keeps the same learning result.
        final_miner, _ = self.open_miner()
        self.assertEqual(6, final_miner.get_watermark())
        self.assertEqual([2, 4], sorted(final_miner.drain.id_to_cluster.keys()))
        self.assertEqual(3, final_miner.drain.id_to_cluster[2].size)
        self.assertEqual("<*> blue ready", final_miner.drain.id_to_cluster[2].get_template())

    def test_partial_line4_checkpoint_falls_back_to_line3(self):
        self.seed_base_from_classic_snapshot()
        miner, persistence = self.open_miner()
        miner.add_log_message(MESSAGES[2], watermark=3)
        miner.begin_checkpoint_compaction(3)
        miner.add_log_message(MESSAGES[3], watermark=4)
        miner.commit_checkpoint_compaction(3)

        # The line-4 checkpoint is the newest durable record.  Simulate the
        # process dying while that checkpoint was only half written.
        persistence.simulate_partial_last_checkpoint_write()

        recovered, _ = self.open_miner()
        self.assertEqual(3, recovered.get_watermark())
        self.assertEqual([1, 2], sorted(recovered.drain.id_to_cluster.keys()))
        self.assertEqual(2, recovered.drain.id_to_cluster[2].size)

        # Line 4 must be re-read; the final cluster ids/counts stay identical.
        recovered.add_log_message(MESSAGES[3], watermark=4)
        line5 = recovered.add_log_message(MESSAGES[4], watermark=5)
        line6 = recovered.add_log_message(MESSAGES[5], watermark=6)
        self.assertEqual(2, line5["cluster_id"])
        self.assertEqual(3, line5["cluster_size"])
        self.assertEqual(4, line6["cluster_id"])
        self.assertEqual([2, 4], sorted(recovered.drain.id_to_cluster.keys()))

    # ---------------------------------------------------- reconciliation

    def test_every_restart_matches_classic_full_snapshot(self):
        self.seed_base_from_classic_snapshot()
        miner, _ = self.open_miner()
        miner.add_log_message(MESSAGES[2], watermark=3)
        miner.compact_checkpoint(3)
        miner.add_log_message(MESSAGES[3], watermark=4)
        miner.add_log_message(MESSAGES[4], watermark=5)
        miner.add_log_message(MESSAGES[5], watermark=6)

        query_messages = MESSAGES + [
            "sensor7 blue ready",   # matches cluster 2
            "press green ready",    # matches cluster 4
            "oven cold stop",       # no match
        ]

        for prefix in (3, 4, 5, 6):
            with self.subTest(prefix=prefix):
                # Reference: classic full snapshot trained on the same prefix.
                classic_path = os.path.join(self.tmpdir, f"classic_{prefix}.bin")
                reference = TemplateMiner(FilePersistence(classic_path), make_config())
                for message in MESSAGES[:prefix]:
                    reference.add_log_message(message)
                reloaded_reference = TemplateMiner(FilePersistence(classic_path), make_config())

                # Incremental miner: same inputs, same base save location, then a
                # fresh process re-opens the checkpoint directory.
                inc_dir = os.path.join(self.tmpdir, f"inc_{prefix}")
                os.makedirs(inc_dir)
                shutil.copy(self.classic_snapshot, os.path.join(inc_dir, BASE_SNAPSHOT_FILE))
                incremental = TemplateMiner(IncrementalFilePersistence(inc_dir), make_config())
                for line_number, message in enumerate(MESSAGES[2:prefix], start=3):
                    incremental.add_log_message(message, watermark=line_number)
                restarted = TemplateMiner(IncrementalFilePersistence(inc_dir), make_config())

                self.assertEqual(
                    cluster_signature(reloaded_reference), cluster_signature(restarted)
                )
                for message in query_messages:
                    expected = reloaded_reference.match(message)
                    actual = restarted.match(message)
                    expected_id = expected.cluster_id if expected is not None else None
                    actual_id = actual.cluster_id if actual is not None else None
                    self.assertEqual(expected_id, actual_id, message)

    # ------------------------------------------------- proportional saves

    def test_checkpoint_size_grows_with_actual_changes(self):
        self.seed_base_from_classic_snapshot()
        miner, persistence = self.open_miner()

        miner.add_log_message(MESSAGES[2], watermark=3)
        one_change_bytes = persistence.journal_size_bytes()
        self.assertEqual(1, persistence.checkpoint_count())

        # A "none" match only grows a cluster and triggers no checkpoint, so no
        # extra state is written when nothing structurally changes.
        none_result = miner.add_log_message("yard blue ready", watermark=99)
        self.assertEqual("none", none_result["change_type"])
        self.assertEqual(1, persistence.checkpoint_count())
        self.assertEqual(one_change_bytes, persistence.journal_size_bytes())

        # A later structural checkpoint carries the pending size growth plus
        # the new cluster, and stays far smaller than a full snapshot rewrite.
        full_snapshot_bytes = os.path.getsize(self.classic_snapshot)
        miner.add_log_message(MESSAGES[3], watermark=4)
        self.assertEqual(2, persistence.checkpoint_count())
        latest_record = persistence.journal_records[-1]
        self.assertEqual({2, 3}, {c["cluster_id"] for c in latest_record["clusters"]})
        self.assertLess(persistence.journal_size_bytes(), full_snapshot_bytes * 3)

        # After compaction the journal is empty regardless of library size.
        miner.compact_checkpoint(4)
        self.assertEqual(0, persistence.journal_size_bytes())
        self.assertEqual(0, persistence.checkpoint_count())
        self.assertEqual(4, persistence.base_watermark)
        # The compacted base snapshot on disk is a complete, loadable snapshot.
        self.assertTrue(pathlib.Path(self.checkpoint_dir, BASE_SNAPSHOT_FILE).exists())
        self.assertTrue(pathlib.Path(self.checkpoint_dir, BASE_META_FILE).exists())

    def test_watermark_required_in_incremental_mode(self):
        self.seed_base_from_classic_snapshot()
        miner, _ = self.open_miner()
        with self.assertRaises(ValueError):
            miner.add_log_message(MESSAGES[2])

    def test_classic_mode_does_not_require_watermark(self):
        # The original full-snapshot mode keeps working unchanged.
        miner = TemplateMiner(FilePersistence(self.classic_snapshot), make_config())
        result = miner.add_log_message(MESSAGES[0])
        self.assertEqual(1, result["cluster_id"])

    def test_periodic_checkpoint_flushes_size_only_growth(self):
        # A "none" match grows a cluster without changing its template.  When a
        # periodic checkpoint eventually fires, that size growth must be
        # journaled even though no structural change triggered the save.
        config = make_config()
        config.snapshot_interval_minutes = 0  # every save opportunity is periodic
        self.seed_base_from_classic_snapshot()
        persistence = IncrementalFilePersistence(self.checkpoint_dir)
        miner = TemplateMiner(persistence, config)

        # Establish the wildcard template first; this match then only grows it.
        miner.add_log_message(MESSAGES[2], watermark=3)
        result = miner.add_log_message("yard blue ready", watermark=4)
        self.assertEqual("none", result["change_type"])
        self.assertEqual(2, persistence.checkpoint_count())
        latest = persistence.journal_records[-1]
        self.assertEqual([2], [cluster["cluster_id"] for cluster in latest["clusters"]])
        self.assertEqual(3, latest["clusters"][0]["size"])
        self.assertEqual(4, persistence.last_watermark())

        restarted, _ = self.open_miner()
        self.assertEqual(4, restarted.get_watermark())
        self.assertEqual(3, restarted.drain.id_to_cluster[2].size)

    def test_journal_only_bootstrap_without_base_snapshot(self):        # Lightweight mode must also work when no prior full snapshot exists.
        empty_dir = os.path.join(self.tmpdir, "fresh")
        os.makedirs(empty_dir)
        persistence = IncrementalFilePersistence(empty_dir)
        miner = TemplateMiner(persistence, make_config())
        miner.add_log_message(MESSAGES[0], watermark=1)
        miner.add_log_message(MESSAGES[1], watermark=2)

        restarted, restarted_persistence = self._open(empty_dir)
        self.assertEqual(2, restarted.get_watermark())
        self.assertEqual([1, 2], sorted(restarted.drain.id_to_cluster.keys()))
        self.assertEqual(2, restarted_persistence.checkpoint_count())

    @staticmethod
    def _open(directory):
        persistence = IncrementalFilePersistence(directory)
        return TemplateMiner(persistence, make_config()), persistence

    def test_prefix_tree_is_rebuilt_on_replay(self):
        # Tree search (used by match) must work after journal replay even though
        # only cluster objects, not the tree, are stored in the journal.
        self.seed_base_from_classic_snapshot()
        miner, _ = self.open_miner()
        miner.add_log_message(MESSAGES[2], watermark=3)
        miner.add_log_message(MESSAGES[3], watermark=4)

        restarted, _ = self.open_miner()
        matched = restarted.match("sensor8 blue ready")
        self.assertIsNotNone(matched)
        self.assertEqual(2, matched.cluster_id)

        # The printed tree must be structurally identical as well.
        def tree_lines(m):
            buffer = io.StringIO()
            m.drain.print_tree(buffer)
            return buffer.getvalue()

        live = TemplateMiner(IncrementalFilePersistence(
            tempfile.mkdtemp(dir=self.tmpdir)), make_config())
        for line_number, message in enumerate(MESSAGES[:4], start=1):
            live.add_log_message(message, watermark=line_number)
        self.assertEqual(tree_lines(live), tree_lines(restarted))


if __name__ == "__main__":
    unittest.main()
