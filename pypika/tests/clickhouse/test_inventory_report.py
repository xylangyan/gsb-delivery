import unittest

from pypika import Table
from pypika.clickhouse.inventory_report import (
    EAST_CHINA_WAREHOUSES,
    SOUTH_CHINA_WAREHOUSES,
    DetailRow,
    OverLimitBranch,
    PrewherePushdownError,
    ReportConfig,
    amount_over_limit_threshold,
    build_inventory_report_query,
    build_report_criterion,
    diff_over_limit_threshold,
    evaluate_report,
    first_version_config,
    revised_version_config,
    shared_warehouse_prewhere,
)
from pypika.functions import Abs, Sum

_DETAIL_TABLE = Table("inventory_count_detail")


def _mixed_rows():
    """华东、华南混在一起，且华南记录足以触发任一超限。"""
    return [
        # 华东 WH-E1：汇总金额 1500 > 1000（命中金额分支），差额绝对值 20 不超限
        DetailRow("WH-E1", "EAST_CHINA", 800, 10),
        DetailRow("WH-E1", "EAST_CHINA", 700, 10),
        # 华东 WH-E2：差额绝对值 600 > 500（命中差额分支），汇总金额 300 不超限
        DetailRow("WH-E2", "EAST_CHINA", 200, 400),
        DetailRow("WH-E2", "EAST_CHINA", 100, 200),
        # 华南 WH-S1：金额 2000 足以触发金额超限，但第一版两个分支都不收华南
        DetailRow("WH-S1", "SOUTH_CHINA", 2000, 0),
        # 华南 WH-S2：差额 700 足以触发差额超限，改版后第二分支收华南
        DetailRow("WH-S2", "SOUTH_CHINA", 50, 700),
        # 报废记录：普通 WHERE 明确排除，不得进入汇总
        DetailRow("WH-E1", "EAST_CHINA", 9999, 9999, record_type="SCRAP"),
    ]


class InventoryReportSqlTests(unittest.TestCase):
    def test_first_version_pushes_shared_east_china_warehouses_to_prewhere(self):
        sql = build_inventory_report_query(first_version_config()).get_sql()

        self.assertEqual(
            'SELECT "warehouse",SUM("amount") AS "amount",ABS(SUM("diff_amount")) AS "diff_abs" '
            'FROM "inventory_count_detail" '
            "PREWHERE \"warehouse\" IN ('WH-E1','WH-E2') "
            "WHERE \"record_type\"<>'SCRAP' "
            'GROUP BY "warehouse" '
            "HAVING (\"warehouse\" IN ('WH-E1','WH-E2') AND SUM(\"amount\")>1000) "
            "OR (ABS(SUM(\"diff_amount\"))>500 AND \"warehouse\" IN ('WH-E1','WH-E2'))",
            sql,
        )

    def test_revised_version_cannot_filter_any_region_early(self):
        sql = build_inventory_report_query(revised_version_config()).get_sql()

        self.assertNotIn("PREWHERE", sql)
        self.assertIn("WHERE \"record_type\"<>'SCRAP'", sql)
        # 第一分支仍按华东判定，第二分支已改为华南，各自留在 HAVING 中
        self.assertIn("HAVING (\"warehouse\" IN ('WH-E1','WH-E2') AND SUM(\"amount\")>1000)", sql)
        self.assertIn("OR (ABS(SUM(\"diff_amount\"))>500 AND \"warehouse\" IN ('WH-S1','WH-S2'))", sql)

    def test_scrap_filter_stays_in_plain_where_and_never_in_prewhere(self):
        sql = build_inventory_report_query(first_version_config()).get_sql()

        self.assertIn("WHERE \"record_type\"<>'SCRAP'", sql)
        prewhere_part = sql.split(" WHERE ")[0]
        self.assertNotIn("record_type", prewhere_part.split("PREWHERE")[1])

    def test_rewriting_condition_order_keeps_same_prewhere_and_report(self):
        base = first_version_config()
        reordered = ReportConfig(
            amount_over_limit_branch=OverLimitBranch(
                name="amount_over_limit",
                allowed_warehouses=EAST_CHINA_WAREHOUSES,
                aggregate_limit=amount_over_limit_threshold(1000),
                region_first=False,
            ),
            diff_over_limit_branch=OverLimitBranch(
                name="diff_over_limit",
                allowed_warehouses=EAST_CHINA_WAREHOUSES,
                aggregate_limit=diff_over_limit_threshold(500),
                region_first=True,
            ),
        )

        base_sql = build_inventory_report_query(base).get_sql()
        reordered_sql = build_inventory_report_query(reordered).get_sql()

        # HAVING 内的书写顺序变了，但 PREWHERE 与最终选中的仓完全一致
        self.assertIn("PREWHERE \"warehouse\" IN ('WH-E1','WH-E2')", reordered_sql)
        self.assertEqual(
            base_sql.split(" PREWHERE ")[1].split(" HAVING ")[0],
            reordered_sql.split(" PREWHERE ")[1].split(" HAVING ")[0],
        )
        self.assertEqual(
            evaluate_report(base, _mixed_rows()),
            evaluate_report(reordered, _mixed_rows()),
        )


class InventoryReportSelectionTests(unittest.TestCase):
    def test_first_version_hits_both_over_limit_branches_separately(self):
        config = first_version_config()
        selected = evaluate_report(config, _mixed_rows())

        # 两个超限分支分别命中：WH-E1 只靠金额超限，WH-E2 只靠差额超限
        self.assertEqual({"WH-E1", "WH-E2"}, selected)

    def test_first_version_prewhere_filtering_does_not_change_report(self):
        config = first_version_config()
        prewhere = shared_warehouse_prewhere(build_report_criterion(config))
        self.assertIsNotNone(prewhere)

        allowed = {item.value for item in prewhere.container.values}
        rows = _mixed_rows()
        filtered_rows = [row for row in rows if row.warehouse in allowed]

        self.assertEqual(
            evaluate_report(config, rows),
            evaluate_report(config, filtered_rows),
        )
        self.assertEqual({"WH-E1", "WH-E2"}, evaluate_report(config, filtered_rows))

    def test_revised_version_judges_branches_after_aggregation(self):
        config = revised_version_config()
        selected = evaluate_report(config, _mixed_rows())

        # WH-E1 走华东金额分支；WH-S2 走华南差额分支；
        # WH-S1 虽金额超限但属华南，第一分支不收华南；WH-E2 差额超限但第二分支已改华南
        self.assertEqual({"WH-E1", "WH-S2"}, selected)

    def test_scrap_records_are_excluded_before_aggregation(self):
        config = first_version_config()
        rows = _mixed_rows()
        without_scrap = [row for row in rows if row.record_type != "SCRAP"]

        self.assertEqual(
            evaluate_report(config, rows),
            evaluate_report(config, without_scrap),
        )

        scrap_only_warehouse = [DetailRow("WH-E1", "EAST_CHINA", 9999, 9999, record_type="SCRAP")]
        self.assertEqual(set(), evaluate_report(config, scrap_only_warehouse))


class PrewherePushdownRuleTests(unittest.TestCase):
    def test_detail_amount_condition_is_rejected_even_though_alias_name_matches(self):
        # 把第二分支里重复的仓号条件换成不能代表整组的明细物理列 amount
        criterion = (
            (_DETAIL_TABLE.warehouse.isin(list(EAST_CHINA_WAREHOUSES)) & (Sum(_DETAIL_TABLE.amount) > 1000))
            | ((Abs(Sum(_DETAIL_TABLE.diff_amount)) > 500) & (_DETAIL_TABLE.amount > 0))
        )

        with self.assertRaises(PrewherePushdownError) as context:
            shared_warehouse_prewhere(criterion)

        message = str(context.exception)
        self.assertIn("amount", message)
        self.assertIn("PREWHERE", message)

    def test_amount_inside_sum_is_recognized_as_aggregate_not_detail_column(self):
        # 同名 amount 只出现在 SUM 内部时属于汇总结果，不得被误判为明细列而拒绝
        criterion = (
            (_DETAIL_TABLE.warehouse.isin(list(EAST_CHINA_WAREHOUSES)) & (Sum(_DETAIL_TABLE.amount) > 1000))
            | (
                (Abs(Sum(_DETAIL_TABLE.diff_amount)) > 500)
                & _DETAIL_TABLE.warehouse.isin(list(EAST_CHINA_WAREHOUSES))
            )
        )

        prewhere = shared_warehouse_prewhere(criterion)

        self.assertIsNotNone(prewhere)
        self.assertEqual(
            set(EAST_CHINA_WAREHOUSES),
            {item.value for item in prewhere.container.values},
        )

    def test_non_group_key_detail_condition_is_not_pushed(self):
        # 第二分支不含仓分组键条件时，不能把任何条件提前下推
        criterion = (
            (_DETAIL_TABLE.warehouse.isin(list(EAST_CHINA_WAREHOUSES)) & (Sum(_DETAIL_TABLE.amount) > 1000))
            | ((Abs(Sum(_DETAIL_TABLE.diff_amount)) > 500) & (_DETAIL_TABLE.record_type == "NORMAL"))
        )

        self.assertIsNone(shared_warehouse_prewhere(criterion))

    def test_disjoint_branch_whitelists_produce_no_prewhere(self):
        criterion = (
            (_DETAIL_TABLE.warehouse.isin(list(EAST_CHINA_WAREHOUSES)) & (Sum(_DETAIL_TABLE.amount) > 1000))
            | (
                (Abs(Sum(_DETAIL_TABLE.diff_amount)) > 500)
                & _DETAIL_TABLE.warehouse.isin(list(SOUTH_CHINA_WAREHOUSES))
            )
        )

        self.assertIsNone(shared_warehouse_prewhere(criterion))


if __name__ == "__main__":
    unittest.main()
