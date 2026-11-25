# batch_preview — 手册选段整批预览

同一批 Markdown 手册按选段表生成多个发行附录。本包用 Marko 解析原册，
为每个发行版生成可被现有脚注扩展读取的 Markdown，并按原册位置核对改动。

## 用法

```bash
python -m batch_preview spec.json -o outdir/            # 整批预览
python -m batch_preview spec.json -o outdir/ --releases 甲   # 只预览某一版
python -m batch_preview spec.json -o outdir/ --budget 5      # 调整求解预算（秒）
```

退出码：`0` 全部可发行；`1` 有版被卡住；`2` 有版来不及判定（预览尚未算出结果）。

可运行的例子（即需求里的甲乙两版）：

```bash
python -m batch_preview batch_preview/examples/batch.json -o /tmp/p1          # 两版都被卡住
python -m batch_preview batch_preview/examples/batch.json -o /tmp/p2 --releases 甲
python -m batch_preview batch_preview/examples/batch-with-c.json -o /tmp/p3   # 补批 c 后两版可发行
```

## 批次选段表（JSON）

```json
{
  "manuals":  {"M1": {"path": "manuals/shared.md"}, "M2": {"text": "……"}},
  "segments": {"S0": {"manual": "M1", "start_line": 1, "end_line": 2}},
  "releases": {"甲": ["S0", "S2"]},
  "marks":    [{"manual": "M1", "line": 1, "col": 2, "candidates": ["a", "b"]}],
  "time_budget_seconds": 10
}
```

- `segments`：每版的选段，按原册 1 起始行号（含首尾）截取；`releases` 决定选段顺序。
- `marks`：核准表。位置用 `line`+`col`（1 起始）或 `offset`（0 起始字符偏移）指定，
  必须落在某个引用（快捷/折叠/完整、含图片）或定义行上；`candidates` 是获准的替代标签。
- 只有标注过的位置可以改引用标签或定义标签；未标注的快捷引用、方括号文字和
  定义原文一律照录。标注过的快捷引用 `[手册]` 会改写为 `[手册][新标签]`，显示不变。

## 规则摘要

- 原册的普通链接、脚注、脚注里的链接，都按该册**实际生效**的定义核对
  （CommonMark 同名取先）；两个恰好同地址的定义按原册位置区分，绝不合并。
- 同一处原册位置在所有收录它的版里沿用同一次签出的改动（改动跨版只计一次）。
- 脚注显示编号随发行稿重排（发行稿中脚注引用统一重编号，并避开版内
  `[^...]` 方括号文字），无需标注。
- 原来只显示为方括号文字的内容（如 `[提示][a]`）必须照常显示：发行稿里
  任何定义行都不得占用它们的标签，否则该版不能发行。
- 可发行时先选**改动原册位置最少**的方案，并列时再选**各版带入定义总份数
  最少**的方案。

## 输出

- `outdir/<版名>.md`：可发行版的发行稿（选段照录 + 核准改写 + 带入的定义与
  重编号脚注），并逐项通过 Marko 重解析核对（定义可按原册位置核对、方括号
  文字保持原样、脚注引用均有定义、链接逐一对应）。
- `outdir/report.json`：
  - `changes`：按原册位置（册、行、列）列出的改动及其使用的版；
  - `blockers`：不能发行时，指出哪处未获准改动的文字卡住了哪个共同标签；
  - `releases.<版>.status`：`ok` / `blocked` / `pending`。`pending` 表示
    预览还没算出结果（批次太大超出时间预算），**不是**确认无法发行。

## 求解

决定按原册位置全局生效，因此全批联立求解；但位置之间只通过共同收录的版
相互约束，问题分解为若干互不共用选段和标签的连通组，各组独立回溯搜索
（每处至多 1+3 个选择），日常约 40 版 / 200 处标注的批次远低于 10 秒预算。
超时的组对应版标记为 `pending`，其余版照常出结果。
