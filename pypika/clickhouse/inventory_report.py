"""
盘点报表配置与 ClickHouse 查询生成。

报表按仓（``warehouse``，仓分组键）汇总盘点明细，汇总后用一个 OR 条件决定仓是否允许
上报：

* 分支 A：仓号在允许参与读明细的白名单内，且汇总金额超限；
* 分支 B：盘点差额超限，且仓号在允许参与读明细的白名单内。

配置里的条件分两类，必须严格区分，不能因为同名而混用：

* **明细物理列条件**：直接引用明细行的物理列（例如 ``warehouse``、明细 ``amount``），
  其中只有「在两个 OR 分支中都出现、且明确作用于仓分组键」的条件才可以提前下推到
  ``PREWHERE``；
* **汇总结果条件**：引用聚合结果（例如 ``SUM(amount)``、``ABS(SUM(...))``），只能在
  ``GROUP BY`` 之后的 ``HAVING`` 阶段判定。

特别地，明细物理列 ``amount`` 与聚合 ``SUM(amount)`` 的显示别名 ``amount`` 同名。
前者不代表整组、绝不能因为同名而被当成汇总结果放进 PREWHERE，否则会在错误的阶段
悄悄执行；此时直接抛出 :class:`PrewherePushdownError`。
"""

from dataclasses import dataclass, field
from typing import Any

from pypika import ClickHouseQuery, Criterion, Field, Table
from pypika.enums import Boolean, Equality
from pypika.functions import Abs, Sum
from pypika.terms import (
    AggregateFunction,
    BasicCriterion,
    ComplexCriterion,
    ContainsCriterion,
    Not,
    Term,
    ValueWrapper,
)

EAST_CHINA = "EAST_CHINA"
SOUTH_CHINA = "SOUTH_CHINA"

EAST_CHINA_WAREHOUSES = ("WH-E1", "WH-E2")
SOUTH_CHINA_WAREHOUSES = ("WH-S1", "WH-S2")

_DETAIL_TABLE = Table("inventory_count_detail")
_WAREHOUSE_FIELD = Field("warehouse", table=_DETAIL_TABLE)
_DETAIL_AMOUNT_FIELD = Field("amount", table=_DETAIL_TABLE)
_DIFF_AMOUNT_FIELD = Field("diff_amount", table=_DETAIL_TABLE)
_RECORD_TYPE_FIELD = Field("record_type", table=_DETAIL_TABLE)
_SCRAP_RECORD_TYPE = "SCRAP"

_SUM_AMOUNT_ALIAS = "amount"
_DIFF_ABS_ALIAS = "diff_abs"


class PrewherePushdownError(ValueError):
    """条件不允许下推到 PREWHERE 时抛出。"""


@dataclass(frozen=True)
class OverLimitBranch:
    """一个 OR 分支的配置。

    :param allowed_warehouses: 明确写出的、允许该分支参与读盘点明细的仓号白名单，作用于
        仓分组键 ``warehouse``（可提前判断的仓号条件）。
    :param aggregate_limit: 明确写出的汇总结果超限条件，只在汇总后的 HAVING 阶段判定。
    :param region_first: 仓号条件在分支中的书写顺序，用于保证「配置写法换了但报告
        不变」。
    """

    name: str
    allowed_warehouses: tuple[str, ...]
    aggregate_limit: BasicCriterion
    region_first: bool = True


@dataclass(frozen=True)
class ReportConfig:
    """盘点报表配置。

    :param amount_over_limit_branch: 汇总金额超限分支（分支 A）。
    :param diff_over_limit_branch: 盘点差额超限分支（分支 B）。
    :param scrap_filter: 普通 WHERE 条件，明确不纳入报废记录，始终留在 WHERE，不参与下推。
    """

    amount_over_limit_branch: OverLimitBranch
    diff_over_limit_branch: OverLimitBranch
    scrap_filter: Criterion = field(default_factory=lambda: _RECORD_TYPE_FIELD != _SCRAP_RECORD_TYPE)


def _warehouse_criterion(warehouses: tuple[str, ...]) -> ContainsCriterion:
    return _WAREHOUSE_FIELD.isin(list(warehouses))


def _branch_criterion(branch: OverLimitBranch) -> ComplexCriterion:
    warehouse_criterion = _warehouse_criterion(branch.allowed_warehouses)
    if branch.region_first:
        return warehouse_criterion & branch.aggregate_limit
    return branch.aggregate_limit & warehouse_criterion


def _split_or_branches(criterion: Criterion) -> list[Criterion]:
    """把顶层 OR 树拆成独立分支（AND 结构保持原样）。"""
    if isinstance(criterion, ComplexCriterion) and criterion.comparator == Boolean.or_:
        return _split_or_branches(criterion.left) + _split_or_branches(criterion.right)
    return [criterion]


def _split_and_parts(criterion: Criterion) -> list[Criterion]:
    """把一个分支顶层的 AND 树拆成独立条件。"""
    if isinstance(criterion, ComplexCriterion) and criterion.comparator == Boolean.and_:
        return _split_and_parts(criterion.left) + _split_and_parts(criterion.right)
    return [criterion]


def _region_membership(criterion: Criterion) -> ContainsCriterion | None:
    """识别「仓分组键 warehouse IN (...)」这类可提前判断的仓号条件。"""
    if isinstance(criterion, ContainsCriterion) and isinstance(criterion.term, Field):
        if criterion.term.name == _WAREHOUSE_FIELD.name:
            return criterion
    return None


def _assert_detail_amount_not_pushed(parts: list[Criterion]) -> None:
    """明细物理列 amount 不代表整组，禁止借同名别名下推到 PREWHERE。

    聚合内部的 ``amount``（``SUM(amount)``）是汇总结果的输入，随聚合条件留在 HAVING；
    只有落在任何聚合函数之外的物理列 ``amount`` 才是明细条件，遇到即拒绝。
    """
    for part in parts:
        aggregated_field_ids = {
            id(node)
            for aggregate in part.find_(AggregateFunction)
            for node in aggregate.find_(Field)
        }
        for node in part.find_(Field):
            if node.name == _DETAIL_AMOUNT_FIELD.name and id(node) not in aggregated_field_ids:
                raise PrewherePushdownError(
                    "明细物理列 'amount' 不是仓分组键，不能下推到 PREWHERE；"
                    "它与汇总别名 SUM(amount) AS amount 同名，但属于明细阶段，"
                    "只能在普通 WHERE/HAVING 中按明细语义处理。"
                )


def _intersect_membership(criteria: list[ContainsCriterion]) -> ContainsCriterion | None:
    """取各分支仓号白名单的交集，作为两个分支共用的 PREWHERE 条件。

    交集为空说明各分支允许的区域互不重合，任何区域条件都不能代表整组，此时不做
    PREWHERE 下推（返回 ``None``），交由汇总后的各分支自行判定。
    """
    common: set[Any] | None = None
    for criterion in criteria:
        values = {item.value for item in criterion.container.values}
        common = values if common is None else common & values

    if not common:
        return None

    return _WAREHOUSE_FIELD.isin(sorted(common))


def shared_warehouse_prewhere(report_criterion: Criterion) -> ContainsCriterion | None:
    """只提取两个 OR 分支共用、且明确作用于仓分组键的仓号条件。

    汇总结果条件、只在单个分支出现的条件、明细物理列 ``amount`` 条件一律不允许下推；
    遇到明细 ``amount`` 时抛出 :class:`PrewherePushdownError`。各分支白名单没有共同仓时
    返回 ``None``，即不做任何 PREWHERE 下推。
    """
    branches = _split_or_branches(report_criterion)
    memberships: list[ContainsCriterion] = []
    for branch in branches:
        parts = _split_and_parts(branch)
        _assert_detail_amount_not_pushed(parts)
        branch_memberships = [membership for part in parts if (membership := _region_membership(part))]
        if not branch_memberships:
            return None
        memberships.extend(branch_memberships)

    if len(memberships) != len(branches):
        return None
    return _intersect_membership(memberships)


def build_report_criterion(config: ReportConfig) -> ComplexCriterion:
    """按配置拼出完整的上报 OR 条件（两个分支、分支内条件顺序均按配置保留）。"""
    return _branch_criterion(config.amount_over_limit_branch) | _branch_criterion(
        config.diff_over_limit_branch
    )


def build_inventory_report_query(config: ReportConfig) -> ClickHouseQuery:
    """根据配置生成盘点报表 ClickHouse 查询。

    只有两个 OR 分支都使用、且明确属于仓分组键 ``warehouse`` 的仓号条件会进入
    ``PREWHERE``；两个超限条件（汇总结果）保留在 ``HAVING``；报废过滤保留在普通
    ``WHERE``。
    """
    report_criterion = build_report_criterion(config)

    prewhere_criterion = shared_warehouse_prewhere(report_criterion)

    sum_amount = Sum(_DETAIL_AMOUNT_FIELD).as_(_SUM_AMOUNT_ALIAS)
    diff_abs = Abs(Sum(_DIFF_AMOUNT_FIELD)).as_(_DIFF_ABS_ALIAS)

    query = (
        ClickHouseQuery.from_(_DETAIL_TABLE)
        .select(_WAREHOUSE_FIELD, sum_amount, diff_abs)
        .where(config.scrap_filter)
        .groupby(_WAREHOUSE_FIELD)
        .having(report_criterion)
    )
    if prewhere_criterion is not None:
        query = query.prewhere(prewhere_criterion)
    return query


def amount_over_limit_threshold(threshold: int) -> BasicCriterion:
    """汇总金额超限：SUM(amount) > threshold（明确的汇总结果条件）。"""
    return Sum(_DETAIL_AMOUNT_FIELD) > threshold


def diff_over_limit_threshold(threshold: int) -> BasicCriterion:
    """盘点差额超限：ABS(SUM(diff_amount)) > threshold（明确的汇总结果条件）。"""
    return Abs(Sum(_DIFF_AMOUNT_FIELD)) > threshold


def first_version_config() -> ReportConfig:
    """第一版：两个分支都只允许华东仓，仓号条件书写顺序不同。"""
    return ReportConfig(
        amount_over_limit_branch=OverLimitBranch(
            name="amount_over_limit",
            allowed_warehouses=EAST_CHINA_WAREHOUSES,
            aggregate_limit=amount_over_limit_threshold(1000),
            region_first=True,
        ),
        diff_over_limit_branch=OverLimitBranch(
            name="diff_over_limit",
            allowed_warehouses=EAST_CHINA_WAREHOUSES,
            aggregate_limit=diff_over_limit_threshold(500),
            region_first=False,
        ),
    )


def revised_version_config() -> ReportConfig:
    """改版：仅把第二分支（盘点差额超限）的华东仓改成华南仓，其余不变。"""
    base = first_version_config()
    return ReportConfig(
        amount_over_limit_branch=base.amount_over_limit_branch,
        diff_over_limit_branch=OverLimitBranch(
            name="diff_over_limit",
            allowed_warehouses=SOUTH_CHINA_WAREHOUSES,
            aggregate_limit=diff_over_limit_threshold(500),
            region_first=False,
        ),
    )


@dataclass(frozen=True)
class DetailRow:
    """一条盘点明细（测试/求值用）。"""

    warehouse: str
    region: str
    amount: int
    diff_amount: int
    record_type: str = "NORMAL"


def _criterion_value(criterion: Criterion, row: DetailRow, totals: dict[str, dict[str, int]]) -> bool:
    """对单仓求值一个条件。

    明细物理列取该行的值，聚合结果取该仓的汇总值，二者同名也不会混淆。
    """
    if isinstance(criterion, ComplexCriterion):
        left = _criterion_value(criterion.left, row, totals)
        right = _criterion_value(criterion.right, row, totals)
        if criterion.comparator == Boolean.and_:
            return left and right
        if criterion.comparator == Boolean.or_:
            return left or right
        raise ValueError(f"不支持的布尔算子: {criterion.comparator}")

    if isinstance(criterion, Not):
        return not _criterion_value(criterion.term, row, totals)

    if isinstance(criterion, ContainsCriterion):
        return _term_value(criterion.term, row, totals) in {
            item.value for item in criterion.container.values
        }

    if isinstance(criterion, BasicCriterion):
        left = _term_value(criterion.left, row, totals)
        right = _term_value(criterion.right, row, totals)
        if criterion.comparator == Equality.eq:
            return left == right
        if criterion.comparator == Equality.ne:
            return left != right
        if criterion.comparator == Equality.gt:
            return left > right
        if criterion.comparator == Equality.gte:
            return left >= right
        if criterion.comparator == Equality.lt:
            return left < right
        if criterion.comparator == Equality.lte:
            return left <= right
        raise ValueError(f"不支持的比较算子: {criterion.comparator}")

    raise ValueError(f"不支持的条件类型: {type(criterion)!r}")


def _term_value(term: Term, row: DetailRow, totals: dict[str, dict[str, int]]) -> Any:
    if isinstance(term, AggregateFunction):
        aggregate = totals[row.warehouse]
        if term.name == "SUM":
            arg = term.args[0]
            if not isinstance(arg, Field):
                raise ValueError("仅支持对物理列的 SUM 求值")
            return aggregate[arg.name]
        if term.name == "ABS":
            return abs(_term_value(term.args[0], row, totals))
        raise ValueError(f"不支持的聚合函数: {term.name}")

    if isinstance(term, ValueWrapper):
        return term.value

    if isinstance(term, Field):
        return getattr(row, term.name)

    raise ValueError(f"不支持的表达式类型: {type(term)!r}")


def _warehouse_totals(rows: list[DetailRow]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        warehouse_total = totals.setdefault(row.warehouse, {"amount": 0, "diff_amount": 0})
        warehouse_total["amount"] += row.amount
        warehouse_total["diff_amount"] += row.diff_amount
    return totals


def evaluate_report(config: ReportConfig, rows: list[DetailRow]) -> set[str]:
    """按配置语义在明细数据上求值，返回最终允许上报的仓号集合。

    先做普通 WHERE（报废过滤，被过滤的明细不参与汇总），再按仓汇总，最后对原始 OR
    条件做 HAVING 判定，用来证明 PREWHERE 下推没有改变报表结果。
    """
    report_criterion = build_report_criterion(config)

    eligible_rows = [row for row in rows if _criterion_value(config.scrap_filter, row, {})]
    totals = _warehouse_totals(eligible_rows)

    selected: set[str] = set()
    for row in eligible_rows:
        if _criterion_value(report_criterion, row, totals):
            selected.add(row.warehouse)
    return selected
