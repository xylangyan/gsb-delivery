# Whisper 老档重建（archive_recon）

两台采集机用不同粒度、不同缺测门槛记录了同一条 1 秒原序列。本工具把两台老档、
目标层和原秒放进**同一个联合模型**，只在该格对**所有相容原序列**都给同一结论时
落确定值；否则留空并说明要补哪段原秒。

## 为什么不能用逐格区间再聚合

逐格求“各自可能的最大值”再聚合，会造出**没有任何一份原采样能同时取到**的值。
本工具对每个目标格直接在最细的原秒上求解，细格无法单独定值、但因为共享同一批
原秒而能共同确定粗格的情况会被正确识别（`tests/test_reconstruct.py` 里的
`test_two_fine_cells_jointly_pin_a_coarse_cell` 即是此例）。

## 证据规则

- **只认聚合格**：`origin=aggregate` 的老格才约束原秒。`origin=direct` 是直接写进
  老粗格的值，只记入 `provenance`，绝不生成原秒证据。
- **空格分两种**：`state=empty, reason=threshold` 表示该格没达到覆盖门槛（子格
  计数低于 `ceil(xFilesFactor × 子格数)`）；`state=lost` 表示后来回卷覆盖，当前
  槽里的字节不被借用，也不当成“当年稀疏”。
- **真实 0 不是缺测**：原秒存在且读数为 0 时记 `value: 0.0`；只有所有相容序列都
  不观测该秒时才记 `missing`。
- **按真实层级与门槛核**：每个档案层用它自己的聚合方法与
  `ceil(xFilesFactor × 子格数)` 门槛；粗层由它相邻的细层聚合。
- **冲突不生成新值**：若老档证据互斥，输出 `status=conflict` 与最小冲突核
  （`minimal_conflict`），不写 `.wsp`。

## 输入：移交清单

`manifest.json` 是移交材料的机器可读形式：

```json
{
  "series": {"start": 1008, "seconds": 96, "range": [0, 12],
             "known":  [{"second": 1088, "value": 0.0}],
             "missing": [1056],
             "ranges": [{"second": 1020, "range": [0, 12]}]},
  "publications": ["A-2026-01-05", "B-2026-01-06"],
  "target": {"archives": [[4, 24], [12, 12], [24, 8]],
             "xFilesFactor": 0.5, "aggregationMethod": "average"},
  "legacy": [
    {"id": "A", "archives": [[4, 24], [24, 8]], "xFilesFactor": 0.25,
     "aggregationMethod": "average",
     "cells": [
       {"level": 4, "start": 1008, "state": "value", "value": 4.0,
        "origin": "aggregate", "publication": "A-2026-03-01"},
       {"level": 4, "start": 1056, "state": "empty",
        "reason": "threshold", "publication": "A-2026-03-01"},
       {"level": 6, "start": 1020, "state": "lost",
        "reason": "rollover", "publication": "B-2026-03-02"},
       {"level": 24, "start": 1032, "state": "value", "value": 6.0,
        "origin": "direct", "publication": "A-2026-04-09"}
     ]}
  ]
}
```

- `series.start` 必须是所有档案步长的整数倍；窗口由每个档案完整覆盖。
- `known` 是已确证的逐秒读数（含真实 0）；`missing` 是已确证的整秒缺测；
  两者互斥。`ranges` 给未留明细的秒一个有限读数范围，缺省用 `series.range`。
- 老档的最细层按秒写入；更粗层由 `archives` 顺序逐级聚合。

## 输出

`python -m archive_recon.cli manifest.json -o out/`：

- `out/handover.wsp`：标准 Whisper 文件，`whisper.fetch` 可直接按层读取；
- `out/reconstruction.json`：
  - `levels[].cells[]` 每格 `state = value | missing | pending`，`pending` 带
    `pending_reason`（`value-range` 或 `presence`）；
  - `evidence`：触及该格的聚合老格；
  - `to_change`：能改变该待补格的原秒及其当前范围；
  - `provenance` / `ignored_cells`：直写值与回卷格；
  - `readback`：用 `whisper.fetch` 回读各层的实测结果；
  - `conflicts` / `conflict`：互斥证据与最小冲突核。

## 运行

```bash
python demo/build_demo.py          # 重新生成双机样例与交付物到 demo/out/
python -m archive_recon.cli demo/fixtures/manifest.json -o demo/out
python demo/verify_handover.py demo/out/reconstruction.json demo/out/handover.wsp
python -m archive_recon.cli demo/fixtures/conflicting_manifest.json -o demo/out_conflict
python -m pytest -p no:rerunfailures tests/test_reconstruct.py
```

依赖：`whisper.py`（本仓库）与 `scipy`（HiGHS MILP）。

## 已知边界

- 只支持 `average` 与 `sum` 两种聚合规则；其它规则会显式报 `NotImplementedError`，
  不做近似。
- 模型规模随“原秒数 × 档案层数”增长；当前样例（96 秒、三层）秒级求解。
- `.wsp` 每层一个读数，无法表达“存在性未定”的中间态；这类格按未定写入空槽，
  完整状态只在 `reconstruction.json` 中。
