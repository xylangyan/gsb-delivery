# seqwrite：把 Construct 采样卷接到只准顺序写的介质

目标：在**没有空间先生成一整卷**、**每份原件只能读一次**、介质**只能向前写**
的约束下，产出与本机可定位 Construct 构建**逐字节一致**、原读取工具可直接
打开的档案卷；并在动笔前给出峰值暂存、可先交付的安全前缀与配额结论。

## 模块划分

- `seqwrite/declaration.py`：已登记的 Construct 声明（档案卷格式）与工单。
  - `build_format(streaming=False)` 是原读取工具用的权威格式；
    `streaming=True` 仅把原件 `RawCopy` 换成取数构件，布局完全一致。
  - 覆盖定长字段、VarInt 长度包（含自有局部坐标的内层包）、RawCopy 原件、
    多种 Pointer 回填、条件回填、构建期校验。
- `seqwrite/vstream.py`：`VirtualSeekableStream`。对 Construct 呈现可
  `seek/tell/read/write` 的随机流，对介质只通过 `ForwardOnlySink` 单调交付。
  地址分 `hole / staged / committed` 三态，已交付区间不可改、不可回读。
- `seqwrite/sources.py`：`OneshotSource`（整段只读一次，取过即焚）与
  `RangeSource`（长原件按 `[start,end)` 取段，范围不重复，固定空档可登记）。
- `seqwrite/fetchers.py`：`SourceBackedRawCopy`（严格走 RawCopy 的 data
  实取口径，直写不回读）与 `FeederRegistry`（按工单排列落实取件次序、
  管理原件侧暂存、落笔即释放）。
- `seqwrite/planner.py`：`BuildPlanner`。用本机可定位原入口取规范字节与
  偏移，逐候选次序全真模拟，核算组合峰值、选序、配额预检，并产出可回溯
  的推进台账。
- `seqwrite/acceptance_report.py`：给验收人的一页纸报告生成器。

## 两类暂存与峰值

- 输出侧：已写但未交付的块（头部待回填槽位会卡住安全前缀）。
- 原件侧：已从一次性原件取得、尚未落笔的字节。
- 组合峰值 = 构建过程中同一时刻「输出侧 + 原件侧」之和的最大值。
  取件排列把晚落笔的件排得越靠前，原件侧驻留越久，峰值越高，因此选序有
  真实区分度（本仓三种次序峰值分别为 96 / 108 / 100）。

## 有界暂存（峰值不随卷长膨胀）

长原件先写一个固定小前导 `LONG_LEAD`，随即跳写短头，之后的主体逐块
（`chunk_size`）从 `RangeSource` 取段、每写一块就尝试交付连续前缀。
提交路径用连续上界做 O(1) 洞检测，并在整块交付后回收块表。因此无论长
原件多大，组合峰值都钳在常数级别（本仓恒定 96 字节，已在 40 万字节卷上
验证），注册表也从不缓存长原件整卷——这正是"没有地方先做一整卷"的落点。

## 安全前缀（什么字节才准先送）

`flush()` 提交的上界取「第一个洞」与「任一未决回填站点起点」的较小者。
头部 Padding 占位写入**不会**释放回填站点，只有 Pointer 的真回跳写入完整
覆盖站点才释放。因此「暂时没变」不等于「可发」——只有能保证后面不再覆盖
的连续前缀才会推进 `committed_watermark`。回写已交付区间立即抛
`BackfillBeyondCommitted`，不会让半成品冒充可用卷。

## 配额闸

`preflight()` 在任何字节落介质前运行。不足时抛 `QuotaExceeded`，明确给出
必须同时驻留的一个原件持有区与一个阻断水线的回填区，以及
`shortfall = required - quota`。

## 关键语义与一个设计权衡

- 校验：`BuildTimeChecksum` 构建期按声明对 `orig_a` 的**原始字节**算哈希；
  解析期只读出存储值、不就当前卷重算。因为同一范围之后可能被登记号条件
  回填，对回填后字节重算必然误报。
- 条件回填：原件 A 内部固定偏移的登记号只在 `attach_registry` 选记为真时
  写入，且在校验锁定原字节之后。
- 内层长度包在独立 `BytesIO` 构建，Pointer 落点是其局部坐标，不能当整卷
  全局坐标。
- 「按当时卷末往回改」：construct 的负偏移 `Pointer(offset<0)` 用
  `whence=2`，基准是**执行时刻 EOF**；构建时与解析时 EOF 不同，同一负偏移
  落点不可被原读取工具稳定复现。因此登记格式用头部绝对槽位落点、写入值
  仍取当时卷末（落点同样远离最终末尾）；负偏移相对当时 EOF 的回跳能力在
  `VirtualSeekableStream` 层由 `test_negative_eof_seek_uses_then_eof_basis`
  专门验证。

## 运行

```bash
# 验收报告（暂存结论、安全前缀、配额示例）
.venv/bin/python -m seqwrite.acceptance_report

# 测试
.venv/bin/python -m pytest tests/test_seqwrite.py -v
```
