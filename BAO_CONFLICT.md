# 可核实冲突：缺少原单与两条原式

## 结论

`status = conflict`，冲突类型为 `missing_source_records_expressions_and_deletion_rules`。

当前工作区是 `jsonata-python 0.7.2` 源码快照，但没有以下判定“最短原记录选录”所必需的输入：

- 采购记录整页 `R = [r_1, ..., r_N]` 及其原编号、原顺序。
- 两条必须原样回放的 JSONata 表达式 `E1`、`E2` 的字符串原文。
- 必须同留同删的货号组 `G`。
- 禁止删除的交接行编号集合 `H`。
- 工单给定的结果键、值、数组次序、`null` 与缺项标准。
- 除题面策略描述外，“已批准删法”的具体边界。

因此 `minimum_original_record_count`、`selection`、从选中项回到原编号的映射均无法真实给出；任何具体货号或编号都会是虚构。

## 已核查事实

- 工作区除 `.venv/` 与 `.git/` 外只有 JSONata 源码、测试和项目文件；没有采购记录 JSON/CSV/XLSX/PDF。
- 对全部 44 个 Git 对象做字节搜索，`货号`、`采购` 均无命中；`D1009` 只出现在库实现/异常文案中。
- 题面文件元数据为 1383 字节；送达对话的中文正文 UTF-8 编码也正好是 1383 字节，说明没有另一段未进入对话的更长题面。
- 上级题目目录与 `attempt-1/` 在当前沙箱中返回 `Operation not permitted`；未绕过该隔离，也未读取其中内容。

## 原库真实语义探针

运行：

```bash
.venv/bin/python bao_conflict_replay.py
```

实际输出：

```text
main-sku-group
  expr: rows{mainSku: $sum(qty)}
  result: {"A": 1, "B": 2}
supplement-sku-group
  expr: rows{supplementSku: $sum(qty)}
  result: {"M": 1, "A": 2}
same-group-collision
  expr: rows{(mainSku): $sum(qty), (supplementSku): $sum(qty)}
  error: D1009: Multiple key definitions evaluate to same key: A
duplicated-last-raw
  expr: rows^(n)[[-1,-1]].original_id
  result: ["W3", "W3"]
missing-last-qty
  expr: rows^(n)[[-1,-1]].qty
  result: undefined
after-unreported-w3-deleted
  expr: rows^(n)[[-1,-1]].qty
  result: [2, 2]
explicit-null-last-qty
  expr: rows^(n)[[-1,-1]].qty
  result: [null, null]
```

这些 `W1/W2/W3` 只是为核验题面机制而构造的探针，不是原单编号，也不是所要求的最短选录。

探针确认：

- 同一个 key-expression 索引重复产生相同键时，当前库会聚合；两个不同 pair 先后产生同一个键时，实际先报 `D1009`，键名是 `A`。实现见 `src/jsonata/jsonata.py:989`。
- `[[-1,-1]]` 会把同一个末项加入结果两次；重复索引处理见 `src/jsonata/jsonata.py:568`。
- 末位记录缺少字段时结果是 `undefined`（工单应记为缺项）；字段存在且值为 JSON `null` 时结果是 `[null, null]`。
- 删除一条在投影结果中表现为缺项的末位记录后，位置选择会改选前一条，并仍重复两次。

## 为什么不能证明“最小”

“最少保留几条”是相对于固定原记录集合和批准删法的极值。题面线索本身甚至允许不同的抽象最小探针：

- 一条 `mainSku` 与 `supplementSku` 都为 `"A"` 的记录即可触发 `D1009`。
- 一条记录即可让 `[[-1,-1]].original_id` 输出 `["O1", "O1"]`。
- 两条有序记录即可演示“删除未进报表的末项后，被选中的末项发生切换”。

货号连保组和不可删交接行还可能强制加入任意额外记录。没有 `R/G/H/E1/E2/expected`，既不能把这些探针映射回原编号，也不能认证任何选录为最少。

## 补齐输入后可直接求解

请提供如下结构的案件文件，或直接把原始记录与两条表达式贴回：

```json
{
  "records": [{"original_id": "R001", "...": "..."}],
  "expression_a": "...",
  "expression_b": "...",
  "linked_sku_groups": [["SKU-1", "SKU-2"]],
  "immutable_handoff_ids": ["R010"],
  "expected": {"keys": {}}
}
```

拿到后可用当前 `.venv` 实跑原库，按连保组和交接行约束做分段/增量最小化，并逐条回放错误码、键值、数组次序、`null` 与缺项。
