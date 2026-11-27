# 值班只读核账入口（ADTK Pipenet 事故复核）

本目录是一套**只读**核账工具：不接触、不重训、不替换现场留下来的模型，
所有经过都在内存里按 ADTK 0.6.2 的精确语义重放。语义已用仓库内真实
ADTK 在 `crosscheck_adtk.py` 中逐条核对一致（`tests/` 会自动调用它）。

## 文件

- `audit.py`：核账引擎与 CLI（纯标准库），负责枚举可能经过、重放记录、
  定位首个对不上的记录、给出逐分钟告警归属与最少补查分钟。
- `case_20261127.json`：本次泵站事故的案例（可作为新案例的模板）。
- `crosscheck_adtk.py`：用真实 ADTK 类复现同一事故的独立交叉核对脚本。
- `_stubs/`：本机 venv 缺 scipy/sklearn/statsmodels/matplotlib/tabulate
  时的最小导入桩，仅为让 ADTK 的相关类可导入，不参与任何数值计算。
- `tests/test_audit.py`：断言引擎结果与手算、真实 ADTK 完全一致。

## 用法

```bash
# 人读报告
.venv/bin/python duty_audit/audit.py duty_audit/case_20261127.json

# 机器可读 JSON
.venv/bin/python duty_audit/audit.py duty_audit/case_20261127.json --json

# 自检（含真实 ADTK 交叉核对）
.venv/bin/python -m pytest duty_audit/tests/ -q
```

## 案例 JSON 口径（同类事故照此填写）

- `branches`：每条泵线/检测分支。目前支持
  - `regression_positive`：正向 `RegressionAD`，需给 `columns`、`target`、
    `c`，以及原检查点训练数据 `train`（回归器固定为取均值语义）；
  - `quantile_high`：高位 `QuantileAD`，需给单列、`high_quantile` 和
    原检查点训练数据。
- `wirings`：候选接法。`share_regressor` 描述哪些回归分支共用同一个
  回归器（以 `anchor` 为基准，装载时把其均值复制给 `members`）；
  残差界限始终保留各分支自己的，不随接法改变。
- `choice_points`：回执缺失时允许取多值的选择点（装载方式、进程是否
  续用、是否做过标定等），引擎枚举其笛卡尔积。
- `records`：按时间顺序的记录，每条给 `op`（`fit`/`fit_detect`/
  `detect`/`reload`）、`input`（缺测写 `null`）、`skip` 跳过名单；
  已知失败的调用给 `failed_node`；有汇总标签的给 `observed`
  （`true`/`false`/`null` 分别对应告警/正常/未知）。
  只在某些选择下才发生的记录用 `only_if` 标注。
- `archive_minutes`：可派工补查的分钟，每钟一份输入；补查只回汇总标签。

## 输出解读

- **相符经过 / 已排除经过**：每条候选经过都会被完整重放；任何一步与
  失败节点或汇总标签对不上，即在该记录处记为首个对不上的记录并排除。
- **逐分钟告警归属**：`确定触发` 是所有相符经过都触发的分支，
  `可能触发` 是至少一条经过触发的分支，`缺数分支` 是输出 NaN 的分支。
- **最少补查分钟**：在归档分钟里找最小子集，使其划分能力等同于全部
  归档分钟；天然不可分的经过会进入**证据缺口**，并注明还缺哪个时点的
  装载/执行回执或分支级标签。
