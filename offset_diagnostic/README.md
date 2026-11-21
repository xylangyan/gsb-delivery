# Pycel OFFSET 月报“显示 0”诊断

值班怀疑 OFFSET 越界，但“显示 0”也可能来自一个**真正读到的零**或**空单元格**。
本目录提供一个可复跑的本地诊断，用**固定版本的真实 pycel 行为**区分这几种原因，
全程只调用既有求值/日志/插件式入口，**不修改 pycel 任何源码或生产逻辑**。

- 实测环境：pycel `1.0b30`、Python 3.12.13、openpyxl 3.1.5（版本写在报告抬头与 JSON 里）。
- 行为结论仅对该 pycel 版本负责；升级版本后请重跑本诊断核对。

## 运行

```bash
# 在仓库根目录（含 .venv）执行
.venv/bin/python offset_diagnostic/build_workbook.py   # 生成工作簿
.venv/bin/python offset_diagnostic/diagnose.py         # 跑诊断，打印报告并写 JSON
```

产物：

- `offset_zero_diagnostic.xlsx`：诊断工作簿。
- `diagnostic_evidence.json`：逐格的三层证据（机器可读）。

## 工作簿布局（三个不同的页）

- `Active`：工作簿活动页。**顶层**未限定地址（在编译器入口直接输入）解析到这里。
- `Formula`：所有诊断公式所在页。公式**内部**写的未限定引用在编译期绑定到本页。
- `Tgt`：显式限定目标页（公式里写 `Tgt!...`）。

在 Excel 最后一列附近，同坐标（锚列 `XFC`=16383、边界列 `XFD`=16384）放三类可分辨标记，
且三页取值不同，因此返回值本身就能暴露引用到底绑到了哪一页：

- 醒目的非零值（如 Tgt 的 4242 / 3333）。
- 可分辨的“真零”：整数 `0`（Tgt!XFC1）。
- 明确空白：该格从不写入，读回是 `None`（Tgt!XFC2）。

`Formula` 页同时放置限定（`Tgt!`）与未限定两套，每套三种表达式：
裸 `OFFSET`、`SUM(OFFSET(...))`、`IFERROR(SUM(OFFSET(...)),0)`。
所有 OFFSET 的宽度都引用同一个可变单元格 `Formula!B2`（记为 W）。

## 三种“输出同为 0”的情况

- A 合法矩形，锚点是真零、后项非零（行 1：`XFC1=0`，`XFD1=4242`）。
  裸 OFFSET 多格引用在取值阶段被**截断到左上角**，只读起点 → 真零 `0`。
- B 目标格本身为空、相邻区仍合法（行 2：`XFC2` 空，`XFD2=77`）。
  裸 OFFSET 读回 `None`（真实空白，不是零）。
- C 首格合法但宽度跨出最后一列（行 3，W=3 时右端到第 16385 列）。
  `offset()` 返回字符串 `#REF!`，被 `IFERROR(...,0)` 吸收成表面 0。

诊断用**同一个编译器实例**把 W 依次改成 1、2、3（仅改这一个输入并清空公式缓存重算），
再让 `Formula` 页上另一个合法的醒目目标（行 4，3333/4444）触发相同的表面结果作为对照。

## 每个公式捕捉的三层证据（见 JSON）

1. 引用解析层：OFFSET 实际返回的 sheet 与矩形（`offset_resolutions`），
   越界时 `status="REF"`、矩形为 `null`。
2. 取值层真正读取的区域：对 `_evaluate_range` 打点得到 `range_reads`（含读到的元组）。
3. 普通/兜底表达式返回值，以及 pycel 自身的 `truncating to ...` 日志（`truncation_logs`）。

## 值班如何据此定位是哪一次

- 目标为空：裸公式返回 `None` 而非 `0`；“只把该空格写成真零”的单因对照里
  裸 OFFSET 发生 `None -> 0` 的翻转，而其他格不动。
- 消费时截断/未读区域：W=2 时出现 `truncating to 'Tgt!XFC1'` 日志；
  `range_reads` 为空证明动态区域**根本没被解引用**。同格的字面 `SUM(Tgt!XFC4:XFD4)`
  返回 7777，而 `SUM(OFFSET(...))` 返回 0 —— 同样的 3333/4444，仅动态引用形式读不到。
- 引用超界：OFFSET 解析层直接是 `#REF!`；裸公式与 SUM 暴露 `#REF!`，
  只有 IFERROR 把它变成 0；把宽度从 3 改回 2 后 `#REF!` 立即消失（单因对照）。

## 三种 sheet 上下文的区分

- 活动页默认：顶层输入未限定地址 `R4` → 解析到 `Active`（值 222）。
- 公式页上下文：住在 `Formula` 页的 `=OFFSET(R4,0,0)`（L2）→ 编译期绑定 `Formula`（值 111）。
- 显式目标页：`=OFFSET(Tgt!XFC1,...)` → 解析到 `Tgt`。

## 涉及的 pycel 内部行为（1.0b30，源码只读引用，未改动）

- 越界判定与 `#REF!`：`src/pycel/lib/lookup.py` 的 `offset()`（`end_col > MAX_COL` 时返回 `REF_ERROR`）。
- 多格引用截断到起点：`src/pycel/excelcompiler.py` `_evaluate()` 中的
  “truncating to ...”分支。
- 动态区域未被 SUM 解引用：编译产物是 `sum_(offset(...))`，命名空间里的 `sum_`
  不带 `refs_wrapper`，`AddressRange` 经 `flatten` 被当作不可展开标量，
  `_numerics` 过滤后为空 → SUM 返回 0（见 `src/pycel/excellib.py` 的 `sum_`/`_numerics`）。
- IFERROR 吸收错误字符串：`src/pycel/lib/logical.py` 的 `iferror()`。
- 未限定引用按公式所在页绑定：`src/pycel/excelformula.py` 引用发射时用 `self.cell.sheet`；
  顶层无 sheet 地址才在 `src/pycel/excelcompiler.py` 用活动页名补全。
