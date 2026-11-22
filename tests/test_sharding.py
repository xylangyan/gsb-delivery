import sqlite3
import unittest

from sqlglot.sharding import ShardingError, merge_shard_results, plan_shards

ROWS = [
    (1, "g1", "2026-01-01", 10),
    (2, "g1", "2026-01-02", 20),
    (3, "g1", "2026-01-02", 30),
    (4, "g1", "2026-01-02", 40),
    (5, "g1", "2026-01-03", 50),
    (6, "g1", "2026-01-03", 60),
    (7, "g1", "2026-01-04", 70),
    (8, "g2", "2026-01-01", 5),
    (9, "g2", "2026-01-01", 15),
    (10, "g2", "2026-01-02", 25),
]

COVERAGES = ["row_id <= 3", "row_id BETWEEN 4 AND 7", "row_id >= 8"]

RANK_AND_MOVING_SUM = """
SELECT row_id, group_id, occur_date, amount,
  RANK() OVER (PARTITION BY group_id ORDER BY occur_date) AS daily_rank,
  SUM(amount) OVER (
    PARTITION BY group_id ORDER BY row_id ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
  ) AS moving_sum
FROM events
ORDER BY row_id
"""


def run_sql(sql):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE events (row_id INTEGER, group_id TEXT, occur_date TEXT, amount REAL)"
    )
    conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", ROWS)
    result = [dict(row) for row in conn.execute(sql).fetchall()]
    conn.close()
    return result


def run_sharded(sql, coverages=COVERAGES, **kwargs):
    plan = plan_shards(sql, coverages, **kwargs)
    assert plan.ok, plan.unsupported
    return plan, merge_shard_results(plan, [run_sql(shard_sql) for shard_sql in plan.shard_sql])


class TestSharding(unittest.TestCase):
    def test_rank_and_moving_sum_two_windows(self):
        _, merged = run_sharded(RANK_AND_MOVING_SUM, stable_keys=("row_id",))
        self.assertEqual(merged, run_sql(RANK_AND_MOVING_SUM))

    def test_peer_count_across_boundary(self):
        # the three 2026-01-02 peers are split 2/1 across shard_0 and shard_1
        _, merged = run_sharded(RANK_AND_MOVING_SUM, stable_keys=("row_id",))
        ranks = {row["row_id"]: row["daily_rank"] for row in merged}
        self.assertEqual([ranks[2], ranks[3], ranks[4]], [2, 2, 2])
        self.assertEqual(ranks[5], 5)

    def test_moving_sum_uses_carried_rows(self):
        # row 4's window is rows 2..4: the two rows entering the window come
        # from the previous shard, not a borrowed total, and row 4 is not
        # treated as the global first row
        _, merged = run_sharded(RANK_AND_MOVING_SUM, stable_keys=("row_id",))
        sums = {row["row_id"]: row["moving_sum"] for row in merged}
        self.assertEqual(sums[4], 20 + 30 + 40)
        self.assertEqual(sums[8], 5)

    def test_aux_columns(self):
        sql = """
SELECT group_id,
  RANK() OVER (PARTITION BY group_id ORDER BY occur_date) AS daily_rank,
  SUM(amount) OVER (
    PARTITION BY group_id ORDER BY row_id ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
  ) AS moving_sum
FROM events
"""
        plan, merged = run_sharded(
            sql,
            aux_columns=("occur_date", "row_id", "amount"),
            stable_keys=("row_id",),
        )
        self.assertEqual(merged, run_sql(sql))
        self.assertIn("__shard_aux_occur_date", plan.aux_columns)
        for row in merged:
            self.assertNotIn("__shard_aux_occur_date", row)

    def test_missing_aux_column(self):
        sql = """
SELECT group_id,
  SUM(amount) OVER (
    PARTITION BY group_id ORDER BY row_id ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
  ) AS moving_sum
FROM events
"""
        plan = plan_shards(sql, COVERAGES, aux_columns=("row_id",), stable_keys=("row_id",))
        self.assertFalse(plan.ok)
        self.assertEqual(len(plan.unsupported), 1)
        self.assertIn("amount", plan.unsupported[0])
        self.assertIn("SUM(amount) OVER", plan.unsupported[0])

    def test_range_frame_unsupported(self):
        sql = """
SELECT group_id,
  SUM(amount) OVER (
    PARTITION BY group_id ORDER BY occur_date
    RANGE BETWEEN 2 PRECEDING AND CURRENT ROW
  ) AS ranged_sum
FROM events
"""
        plan = plan_shards(sql, COVERAGES, stable_keys=("row_id",))
        self.assertFalse(plan.ok)
        self.assertIn("RANGE BETWEEN 2 PRECEDING AND CURRENT ROW", plan.unsupported[0])

    def test_nondeterministic_frame_order_unsupported(self):
        sql = """
SELECT group_id, occur_date, amount,
  SUM(amount) OVER (
    PARTITION BY group_id ORDER BY occur_date ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
  ) AS moving_sum
FROM events
"""
        plan = plan_shards(sql, COVERAGES)
        self.assertFalse(plan.ok)
        self.assertIn("deterministic", plan.unsupported[0])
        self.assertIn("SUM(amount) OVER", plan.unsupported[0])

    def test_window_function_unsupported(self):
        sql = """
SELECT row_id, group_id,
  LAG(amount) OVER (PARTITION BY group_id ORDER BY row_id) AS prev_amount
FROM events
"""
        plan = plan_shards(sql, COVERAGES, stable_keys=("row_id",))
        self.assertFalse(plan.ok)
        self.assertIn("LAG(amount) OVER", plan.unsupported[0])

    def test_plain_query(self):
        sql = "SELECT row_id, amount FROM events WHERE amount > 20 ORDER BY row_id"
        plan, merged = run_sharded(sql)
        self.assertEqual(plan.handoffs, [])
        self.assertEqual(merged, run_sql(sql))

    def test_unbounded_preceding(self):
        sql = """
SELECT row_id, group_id, amount,
  SUM(amount) OVER (
    PARTITION BY group_id ORDER BY row_id ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  ) AS running_total
FROM events
ORDER BY row_id
"""
        _, merged = run_sharded(sql, stable_keys=("row_id",))
        self.assertEqual(merged, run_sql(sql))

    def test_row_number_and_dense_rank(self):
        sql = """
SELECT row_id, group_id, occur_date,
  ROW_NUMBER() OVER (PARTITION BY group_id ORDER BY row_id) AS rn,
  DENSE_RANK() OVER (PARTITION BY group_id ORDER BY occur_date) AS dr
FROM events
ORDER BY row_id
"""
        _, merged = run_sharded(sql, stable_keys=("row_id",))
        self.assertEqual(merged, run_sql(sql))

    def test_avg_frame(self):
        sql = """
SELECT row_id, group_id, amount,
  AVG(amount) OVER (
    PARTITION BY group_id ORDER BY row_id ROWS BETWEEN 2 PRECEDING AND CURRENT ROW
  ) AS moving_avg
FROM events
ORDER BY row_id
"""
        _, merged = run_sharded(sql, stable_keys=("row_id",))
        self.assertEqual(merged, run_sql(sql))

    def test_merge_raises_on_unsupported(self):
        plan = plan_shards(
            "SELECT NTILE(4) OVER (ORDER BY row_id) AS quartile FROM events",
            COVERAGES,
            stable_keys=("row_id",),
        )
        self.assertFalse(plan.ok)
        with self.assertRaises(ShardingError):
            merge_shard_results(plan, [])

    def test_describe(self):
        plan, _ = run_sharded(RANK_AND_MOVING_SUM, stable_keys=("row_id",))
        description = plan.describe()
        self.assertIn("peer count at boundary key", description)
        self.assertIn("last 2 row(s) of amount", description)
        self.assertIn("sort boundary (occur_date)", description)
        self.assertIn("sort boundary (row_id)", description)


if __name__ == "__main__":
    unittest.main()
