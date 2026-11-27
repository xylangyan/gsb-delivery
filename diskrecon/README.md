# diskrecon — 设备柜交接盘的入仓核对

从旧进程（经 psutil 汇出 `nowrap` 计数）留下的交接材料，核对夜班每一段
到底写了多少。磁盘名在交接中可能重复：同一个读表动作领号、两次出现同一
盘名，中间仍可能换过盘。psutil 的 `nowrap` 只对同名计数补一次旧值，分
不清回绕和换盘，也看不到两次读表之间的多次周转 —— 本工具把这些不确定
性全部显式建模。

## 交接材料 → 数据模型（`diskrecon.model`）

| 交接材料 | 模型 |
| --- | --- |
| 一次实际取数（领号、nowrap 值、能取得的原始计数、读表进程、原物理盘编号） | `Sample` |
| 转抄记录（日志标出允许对应的实际取数） | `Transcription(allowed_sources=...)` |
| 值班记录（设备序号 / 允许的盘号 + 是否可重启 + 计数位宽） | `DutyWindow` |
| 设备能力记录（每盘有限写入速率上限） | `Capability` |
| 物理覆盖清单（整盘 / 分区各自实际覆盖） | `Coverage` |
| 同一窗口已签的精确合计 | `SignedTotal` |
| 未核清、被多张表引用的换盘 | `SwapClaim` |
| 历史档省略的采样号段和步长 | `ArchiveIndex` + `OmittedSpan` |

## 核对语义

- **同盘续写**：原始计数差对位宽取模得 `base`，可行增量为
  `base + k * 2**bits`，`k` 是窗口内合法周转次数，只受设备能力上限
  约束 —— 绝不默认只过了一个计数周期。例如 8 位计数 250 → 5 得
  `base = 11`，上限 12 时确定为 11，上限 600 时可达 `{11, 267, 523}`。
- **换盘**：新盘计数从 0 重记（末次原读数即新盘下界），旧盘在窗口内
  最多写到自己的能力上限；两台盘各自按自己的能力记录核。
- **身份证据**：值班记录给了设备序号、或旧进程记下的原物理盘编号一致，
  则钉死同盘；编号不同则确定换盘；都没有时按"是否可重启"给出两种解释。
- **共享换盘**：同一个 `SwapClaim` 被多张表引用时是一个共享变量，所有
  表只用同一次解释。
- **转抄**：与允许的实际取数统一（值必须相等），不另立观测点；对不上
  任何允许取数即为失败票据。
- **已签合计**：对窗口内各段可行域做弧一致传播，能把段钉成确定数。
- **物理覆盖**：整盘视图与分区视图同时覆盖一台盘时，每个物理区段只计
  一次（分区视图优先，整盘视图不重复计入）。

## 输出（`diskrecon.reconcile`）

```python
from diskrecon import reconcile

report = reconcile(handover)
print(report.summary())

for seg in report.segments:      # 按段: 确定数+原上报来源, 或可达 [low, high]
    ...
for entry in report.physical_view():  # 按实际物理盘和时间区间
    ...

# 任选两段(或更多)的指定量能否同时对上 —— 在同一次解释内联合检查,
# 不拼不同解释的左右边界:
result = report.check_joint(("main:sda:0", 11), ("main:sda:1", 267))
```

所有票据对不上时，`report.ok` 为假，`report.conflicts` 指到失败的原
观测（样本号、转抄记录）和容量限制（设备能力记录给出的上界）。

## 复现与对照

- **现场小样**（`tests/test_nowrap.py`）：`NowrapTracker` 与 psutil 真实
  `wrap_numbers` 逐值对照，并对真实内核返回做单调性冒烟（只读）。
- **整套复现**（`tests/test_end_to_end.py`）：`FakeKernel` 假读表数据模拟
  回绕与换盘，`OldExporter` 走真实 psutil 代码路径汇出，全程不接系统外
  真实盘。
- **历史档**：按省略号段+步长过滤样本，缺口自然成为带多次合法周转的
  长段；可行域用等差数列符号表示，从不为每个计数周期或换盘抄件组合
  单独生成归档再检查。

## 运行测试

```sh
python3 -m pytest diskrecon
```

（`diskrecon/pytest.ini` 独立于 psutil 自身的 pytest 配置。）
