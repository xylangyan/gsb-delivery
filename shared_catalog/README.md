# 共同目录（Shared Survey-Tree Catalog）

两个小组从同一棵调查分类树各自导出覆盖语言：甲组归档为 CLDF 关联的 Newick
（`raw/groupA_tree.nwk`），乙组归档在 NEXUS（`raw/groupB_tree.nex`）。本目录把两份归档
合并为一个可经普通 CLDF `TreeTable` 读回的共同目录，并为每条边保留来源对应。

## 设计原则

- 每份原树都可由一份“完整母本”经**裁掉本组建档范围外的叶、再压掉只有一个后继的中间点**
  精确复现（`scripts/gapnewick.py: reconstruct`），断言在构建时与测试中双重校验。
- 枝长同一单位（`change`）。**没有长度的枝是归档缺口（gap），不是零长**，在所有阶段保持
  无冒号写法；缺口与实测段相加取实测值，缺口加缺口仍是缺口。
- 压缩后只剩总长度的枝登记为路径合计，绝不冒充共同树里某一条边的独立观测。
- 支持注记按各自选中语言集合归属，重复注记不算两条独立支持，也不为对方未覆盖的语言作证。
- 硬冲突在共同树里留成多叉（polytomy），争议内边只 withhold、不取平均；其余树照常入册。

## 目录结构

```
shared_catalog/
  raw/                         # 两份原始归档（不可变输入）
    groupA_tree.nwk            # 甲组 CLDF 关联 Newick
    groupB_tree.nex            # 乙组 NEXUS（TREES 块，字面 LanguageTable ID）
  provenance/
    alt1_backbone.nwk          # 替代解释 I：完整母本，复现甲组
    alt2_backbone.nwk          # 替代解释 II：完整母本，复现乙组
    edge_ledger.csv            # 每条边的长度/缺口/证据状态/来源
    merge_ledger.csv           # 每次单后继点压缩的上下段与合计
    annotation_ledger.csv      # 支持注记的归属与独立性裁定
    conflict_report.csv        # 硬冲突的不相容原路径与处理
    interpretations.csv        # 两份可复读替代解释及回放步骤
  catalog/                     # 可交接的 CLDF 数据集（Generic 模块）
    Generic-metadata.json
    languages.csv  media.csv  trees.csv
    edge_ledger.csv merge_ledger.csv annotation_ledger.csv
    conflict_report.csv interpretations.csv
    trees/{shared_summary,groupA_slice,groupB_slice,alt1_full,alt2_full}.nwk
  scripts/gapnewick.py         # 缺口感知的剪枝/压缩/序列化原语
  scripts/build_catalog.py     # 由 raw + provenance 生成 catalog 的构建器
  tests/test_catalog.py        # 端到端对账测试
```

## 语言身份

8 个可汇合身份 `L01`–`L08` 两组都覆盖；甲组另有 `L11,L12`，乙组另有 `L13,L14`。
`languages.csv` 用 `Coverage_A/Coverage_B` 标出覆盖范围。两组“同名但不是同一语言记录”：

| Name | 甲组记录 | 乙组记录 |
| --- | --- | --- |
| Koro | `L11` (koro1311) | `L13` (koro1313) |
| Mori | `L12` (mori1312) | `L14` (mori1314) |

合并只按 `ID` 进行，名字相同不构成同一身份。

## 关键情形与证据位置

- **夹在两个已知分叉之间的原分支（压缩路径）**：`(L01,L02)` 上方有两个分叉。
  甲组建档时丢掉第一个分叉（母本里压掉 `F1`，0.03+0.02=0.05），乙组丢掉第二个分叉
  （压掉 `F2`，0.02+0.06=0.08）。收到共同范围后又各自压成 0.10 与 0.10。两个 0.10 只是
  各自看到的路径合计，见 `merge_ledger.csv`，在 `edge_ledger.csv` 标为
  `two_path_totals_equal`，不当作同一条共同边的两次观测。
- **同长异注记**：甲 `SA:0.07[A_bs=94]` 与乙 `SB:0.07[B_bs=91]` 裁剪后落在同一切分
  `{L07,L08}:0.07`。`annotation_ledger.csv` 把两条注记分别限定在各自语言集合，
  `Independent_Support=No`，不合并为独立支持，也不外延到对方语言。
- **缺口**：甲的 `L02`、乙的 `L14` 无枝长。共同树保留 `L02` 的缺口，并在
  `edge_ledger.csv` 注明乙组对该端实测 0.12 而不导入；缺口永不序列化成 `:0`。
- **硬冲突**：甲组内部为 `(L03,L04):0.05`，乙组为 `(L04,L05):0.05`，两条原路径不能同时
  成立。共同树把 `(L03,L04,L05):0.04` 留成三歧，争议的 0.05 内边不入共同树；
  详见 `conflict_report.csv`。`t_groupA/t_groupB/t_alt1/t_alt2` 不受影响、照常入册。
- **两份可复读的差异证据**：`alt1_backbone.nwk` 与 `alt2_backbone.nwk` 叶集合相同，但歧区
  拓扑与路径切分不同（如 `{L01,L02,L13}` 上方分别为 0.03 与 0.06；KF 区分别跟甲、跟乙）。
  各自经同一回放过程精确复现对应归档，步骤记录在 `interpretations.csv`。

## 重建与验证

```bash
# 重新生成 catalog（幂等，可重复）
.venv/bin/python shared_catalog/scripts/build_catalog.py

# 端到端对账测试
.venv/bin/python -m pytest shared_catalog/tests/test_catalog.py -o addopts=""
```

测试直接对 `raw/` 原始归档复算，不信任构建器自身断言，覆盖：母本精确复现、缺口存活、
根到叶路径合计对账、冲突多叉、压缩合计不被当成共同边、注记独立性、同名异记录、
两份替代差异以及 pycldf `TreeTable` 读回。

## 经 TreeTable 读回

```python
from pycldf import Dataset
from pycldf.trees import TreeTable

ds = Dataset.from_metadata("shared_catalog/catalog/Generic-metadata.json")
for tree in TreeTable(ds):
    node = tree.newick()                 # newick.Node，缺口表现为无 :length
    print(tree.id, tree.tree_branch_length_unit, node.get_leaf_names())
```

五棵入册树：`t_shared`（共同摘要，冲突区多叉）、`t_groupA`、`t_groupB`（与原卷逐边对账）、
`t_alt1`、`t_alt2`（两份替代解释）。元数据含 `edge_ledger → trees`、
`interpretations → trees`、`trees → media` 外键；`Dataset.validate()` 通过。
