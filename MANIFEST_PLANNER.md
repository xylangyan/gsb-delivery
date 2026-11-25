# 共用原行清单求解器

`manifest_planner.py` 面向题面限定的查询类型：外连接、过滤、`DISTINCT` 和 `ROW_NUMBER` 窗口排名。它用 `mo_sql_parsing` 只解析一次全部固定 SQL，用 SQLite 作为小账/中账的实际复读校验器，并输出保留源表名、列和稳定行身份的共用清单。

## 输入

当前工作区没有实际原行、已签结果、视图定义、许可行或仓储开销；题目目录的其他路径也被沙箱拒绝读取，因此这里不能诚实地产出具体行 ID 清单。取得账包后，使用如下 JSON 结构：

```json
{
  "row_id_keys": ["row_id", "_row_id"],
  "default_cost": 1,
  "tables": [
    {
      "name": "flow",
      "rows": [
        {
          "row_id": "f-001",
          "storage_cost": 12,
          "columns": {"bill_no": "B1", "event_time": "2026-01-01 10:00:00"}
        }
      ]
    }
  ],
  "views": {"v_settlement": "SELECT ... FROM flow ..."},
  "allowed_rows": {"flow": ["f-001"]},
  "queries": [
    {
      "id": "settlement-rank",
      "sql": "SELECT ... FROM v_settlement ...",
      "expected": {"columns": ["bill_no", "rn"], "rows": [["B1", 1]]},
      "required_rows": [{"table": "flow", "row_id": "f-001"}],
      "order_sensitive": true
    }
  ]
}
```

`allowed_rows` 也可以是 `[{"table":"flow","row_ids":["f-001"]}]`；省略或传 `"all"` 表示全部原行均许可。

## 运行

```bash
.venv/bin/python manifest_planner.py inspect --sql "$(cat q.sql)"
.venv/bin/python manifest_planner.py solve \
  --input ledger_bundle.json \
  --output shared_manifest.json
```

输出的每一行形如：

```json
{
  "source_table": "flow",
  "row_id": "f-001",
  "storage_cost": 12,
  "columns": {"bill_no": "B1"}
}
```

## 语义保证

- 按查询输入顺序找最早失败；诊断包含 `query_id`、实际/期望行、`NULL`、行数、解析出的连接条件和窗口完整键。
- 候选清单必须逐查询复读已签结果，比较实际行序、列、`NULL` 和重复次数。
- 左连接右侧匹配行即使展示列为 `NULL` 也会保留，因为匹配状态查询仍需要该原行身份。
- 不把同值 `DISTINCT` 来源合并成同一个行对象；若重复次数或其他查询需要重复来源，这些稳定行身份会被强制保留。
- 窗口诊断保留 `PARTITION BY` 和 `ORDER BY` 的完整表达式顺序；缺失前置竞争行会改变名次，无法只保留已签第一名的显示值。
- 所有查询共用同一份选中行和同一个内存账库；输入文件只加载一次，连接键会建索引，候选集通过选择位增量切换。

## 最优性方法

若许可全集能复读已签结果，对上述受限查询类别，“能复读完整已签结果”的集合在补回其他许可行后仍保持正确。求解器因此把失败候选转成正子句，用代价加权命中集求下界，再用实际 SQL 校验候选；候选通过且下界等于当前最优代价时停止。单行缺失扫描用于锁定必须归属的原表行。

这避免了为每个组合重新读取原始文件或重建全表；但黑盒校验次数在最坏情况下仍是指数级。大账应先把原流水一次性装入 SQLite/Parquet/OLAP，并用解析出的连接键、分区键做集合式裁剪，只把剩余分歧核心交给命中集搜索。
