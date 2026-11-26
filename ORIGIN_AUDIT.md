# PysonDB 原件身份复核入口

`origin_audit.py` 是本机零额外服务的复核入口。它不按输出表合并同字面值行，而是给合并时留存的每一行保留稳定 `origin`，并严格按本仓库 `pysondb/db.py` 的 PysonDB 1.7 语义模拟。

## 直接使用

```bash
python3 origin_audit.py --case 案件.json --range CP1:CP2 --target B:b2 --cross-check
python3 origin_audit.py --case 案件.json --check --json
python3 origin_audit.py \
  --initial merged_rows.json \
  --a-log station_a.jsonl \
  --b-log station_b.jsonl \
  --checkpoints posting_points.json \
  --range P3:P4 \
  --target b-42
```

不加 `--case` 时，会在当前目录寻找 `origin-audit-case.json`、`audit-case.json` 或 `case.json`。

常用参数：

- `--range CP1:CP2`：过账范围，也支持 `CP1..CP2`、中文冒号和 `--start CP1 --end CP2`。
- `--target B:b2`：指定补录请求；也接受事件 `id`、流水行号或 `A:12` 形式。
- `--json`：输出完整机器可读报告，包含每条最小/最大见证顺序和记录位置。
- `--check`：只逐个核对已有过账点是否存在可行交错顺序。
- `--identity-map-out identity-map.json`：导出可入库旁挂的原件身份侧车，不把 `$origin` 写进 PysonDB 记录。
- `--cross-check`：把最小/最大见证顺序送回真实 `pysondb.db.JsonDatabase` 回放并比对落盘状态。
- `--allow-target-failure`：若没有“补录已过账”的前提，允许该请求在部分世界中报错。

运行内置样例和测试：

```bash
python3 origin_audit.py --selftest
python3 -m unittest tests.test_origin_audit
```

## 案件 JSON 结构

```json
{
  "case_id": "ledger-20261122",
  "id_field": "id",
  "initial": [
    {"$origin": "O1", "id": 17, "name": "same"},
    {"$origin": "O2", "id": 17, "name": "same"},
    {"$origin": "O3", "id": 18, "name": "resident"}
  ],
  "streams": {
    "A": [
      {"id": "a1", "op": "getById", "pk": 17, "result": {"id": 17}},
      {"id": "a2", "op": "updateById", "pk": 17, "data": {"id": 18}}
    ],
    "B": [
      {"id": "b1", "op": "updateById", "pk": 18, "data": {"note": "补录"}, "tags": ["supplement"]}
    ]
  },
  "checkpoints": [
    {"id": "CP1", "positions": {"A": "a1", "B": 0}},
    {"id": "CP2", "positions": {"A": "a2", "B": "b1"}}
  ]
}
```

也支持：

- PysonDB 原库形态：`{"data": [...]}`。
- 行序 + 原件表：`{"row_order": ["O1", "O2"], "originals": {"O1": {...}}}`。
- JSONL/NDJSON、CSV/TSV 和常见文本调用流水，例如 `01 getById(17) => IdNotFoundError`。
- 常见别名字段：`method/call/action`、`where/query/db_dataset`、`new_data/patch/values`、`returns/response/error`、中文“原件号/流水/过账点”等。
- 若回执额外带 `"$origin"`、`"$position"` 或 `"$index"`，它们会作为原件/位置断言参与剪枝；这些字段不会传给 PysonDB。

## 输出含义

- `written_including_deletes.min/max`：范围内成功新增、字段更新或删除所触及的不同 `origin` 数。
- `field_or_add_writes_only.min/max`：只统计新增和字段写入，不把删除计入“写到”。
- `target.certain_originals`：所有回执一致的双流交错世界中，该补录必定命中的原件交集。
- `target.possible_originals`：至少一个可行世界中命中的原件并集。
- `min_witness.steps` / `max_witness.steps`：两套可核回的全局实际执行顺序；每步带工位、原流水行号、DP 坐标、命中原件和变更前后位置。
- `diagnosis.first_impossible`：某个读返回或失败回执在所有可达状态下都不成立时，给出的最先彻底不可能事件及可达前缀见证。

## 实现要点

- `getById(17)` 只返回行序中第一个 `id == 17` 的行；`updateById` 和 `deleteById` 会循环命中所有同号行。
- `updateById` / `updateByQuery` 的 schema 检查始终针对当前第一行；删除最前一行后，后续请求按新的第一行表头判定 `SchemaError`。
- 每个原件有独立位掩码；同字面值行不会合并。
- 双流只要求各自顺序不变，全局顺序用网格 DP 枚举；相同当前库状态合并，不按每种先后保存完整历史值树。见证链只保留父指针和位置/命中摘要。
- 已核过的检查点可分段生成当前状态前沿；删除的旧原件不再进入后续前沿。
