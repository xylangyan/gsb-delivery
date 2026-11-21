# 两批方言 CLDF 词表的可追溯合并

## 场景与输入

| 批次 | 词形 | 概念 | 同源判断 | 同源集合 | 说明 |
|---|---|---|---|---|---|
| 甲 (`inputs/jia`) | F1, F2 | P1, P2 | C1, C2 | S1, S2 | 标准列名 |
| 乙 (`inputs/yi`) | F1, F3 | P1, P3 | C1, C3 | S1, S3 | 关系列用本地名（`Begriff`、`Beleg`、`Verwandtschaftsgruppe` 等） |

要点：

- 甲、乙各自的 `F1`、`C1`、`P1` 是**不同记录**，只是拼写相同（两批 F1 的词形文本同为 `hant`）。
- 乙的 `P3` 名称同样是 "hand"，但**未经对照确认**，不与任何甲的概念合并。
- 唯一确认共用的概念是甲 `P1` ≡ 乙 `P1`，由 `inputs/concept_mapping.json` 显式声明。
- 乙的关系列采用本地名称，元数据 `propertyUrl` 仍标注标准 CLDF 属性，合并按语义取列而非按表头名。

## 稳定 ID 规则（与加载次序无关）

- 批次内记录的输出 ID 一律为 `<批次>-<原ID>`，如 `jia-F1`、`yi-C1`、`jia-S1`。
- 仅对照文件中列出的概念合并为一行，输出 ID 取对照文件里的固定值 `param-shared-hand-P1`。
- 同源集合、同源判断、词形**绝不**跨批次合并；同拼写 ID 不会自行坍缩。
- 处理顺序按批次名字典序固定，因此甲→乙与乙→甲两次交付逐字节相同（已用 `diff -r` 验证）。

## 可追溯输出

每个交付目录（`deliverables/merge_*`）含：

- `forms.csv` / `parameters.csv` / `cognates.csv` / `cognatesets.csv` 及
  `Wordlist-metadata.json`（仅题面要求的 Wordlist 表 + 同源关系表）；
- 每行附带 `Source_Batch`、`Source_ID` 溯源列；
- `provenance.tsv`：输出 ID → 源批次 + 原 ID 的完整反向对照表（共用概念对应两行来源）。

元数据中的外键约束：`forms.Parameter_ID → parameters.ID`、
`cognates.Form_ID → forms.ID`、`cognates.Cognateset_ID → cognatesets.ID`，
重新打开后可经 ORM 走 同源判断 → 词形 → 概念、同源判断 → 同源集合 的关系。

## 缺行处理

- 合并前删除甲的 `F1` 词形行，但保留引用它的同源判断 `C1`（见 `inputs/jia_no_f1`）。
- 合并器只允许在**同一批次内**解析引用；解析不到时跳过该行，并在
  `reports/missing_damaged.tsv` 报告所属批次、所在表、行原 ID、缺失表与缺失 ID：
  `jia / CognateTable / C1 → FormTable / F1`。
- 乙的 `yi-F1` 仍在，但绝不会被拿来补甲的空缺；甲的 `jia-C1` 被跳过而非改接到乙的词形。

## 复现

```bash
../.venv/bin/python scripts/build_inputs.py   # 仅重建输入
../.venv/bin/python scripts/run_all.py        # 全流程：3 次合并 + 101 项验证
```

验证结果见 `reports/verification.txt`，覆盖：

- 三个交付均通过 `Wordlist.validate()`（含参照完整性）；
- 甲、乙 F1/C1 独立；仅 P1 共用；P3 等同名概念保持各自身份；
- 逐行 ORM 外键遍历，且每条同源判断只连回本批次的词形与同源集合；
- 按输出 ID 反查源批次/原 ID，并与原始输入的词形文本核对；
- 甲→乙与乙→甲交付目录逐字节一致，溯源完全相同；
- 完整交付与缺行交付之间，所有仍输出记录的 来源→输出 ID 对应完全一致。
