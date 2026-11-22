# 报表错码来源取证（formulas）

- 选定结果格：`REPORT!B1` = `=IF(CONFIG!A1=1, CALC!B1, CALC!B2)`
- 分支一：`CALC!B1` = `=DATA!B1*2`（源格 `DATA!B1`）
- 分支二：`CALC!B2` = `=ALT!C1+ALT!C2`（源格 `ALT!C1` 空白格、`ALT!C2` 外链 `'[ext.xlsx]Sheet1'!A1`）
- 条件：`CONFIG!A1`，取值 1 -> 分支一，其他 -> 分支二

## 运行A：CONFIG!A1 = 1

- 结果：`REPORT!B1` = **#VALUE!**（错码 #VALUE!）
- 公式已取到并编译：True；外链工作簿已加载：True
- 性质判定：结果是由求值真实算出的错误值 #VALUE!，不是公式未编译或输入未取到（见下方诊断）
- 条件取值：`CONFIG!A1` = 1 -> 执行分支一（CALC!B1）
- 本次执行子图实际算到的格子（含 IF 急切求值的对侧分支）：
    - `'[ext.xlsx]SHEET1'!A1` = #VALUE!
    - `'[report.xlsx]ALT'!C1` = <空白>
    - `'[report.xlsx]ALT'!C2` = #VALUE!
    - `'[report.xlsx]CALC'!B1` = #VALUE!
    - `'[report.xlsx]CALC'!B2` = #VALUE!
    - `'[report.xlsx]CONFIG'!A1` = 1
    - `'[report.xlsx]DATA'!B1` = 'abc'
    - `'[report.xlsx]REPORT'!B1` = #VALUE!
- 决定结果的必需证据：
    - `'[report.xlsx]CONFIG'!A1` = 1 — 条件：选中分支一
    - `'[report.xlsx]DATA'!B1` = 'abc' — 分支一源格：文本，乘数运算产生 #VALUE!
- 读到但不决定结果（可缺）：
    - `'[report.xlsx]ALT'!C1` = <空白>
    - `'[report.xlsx]ALT'!C2` = #VALUE!
    - `'[ext.xlsx]SHEET1'!A1` = #VALUE!
    - `'[report.xlsx]CALC'!B2` = #VALUE!

## 运行B：CONFIG!A1 = 2

- 结果：`REPORT!B1` = **#VALUE!**（错码 #VALUE!）
- 公式已取到并编译：True；外链工作簿已加载：True
- 性质判定：结果是由求值真实算出的错误值 #VALUE!，不是公式未编译或输入未取到（见下方诊断）
- 条件取值：`CONFIG!A1` = 2 -> 执行分支二（CALC!B2）
- 本次执行子图实际算到的格子（含 IF 急切求值的对侧分支）：
    - `'[ext.xlsx]SHEET1'!A1` = #VALUE!
    - `'[report.xlsx]ALT'!C1` = <空白>
    - `'[report.xlsx]ALT'!C2` = #VALUE!
    - `'[report.xlsx]CALC'!B1` = #VALUE!
    - `'[report.xlsx]CALC'!B2` = #VALUE!
    - `'[report.xlsx]CONFIG'!A1` = 2
    - `'[report.xlsx]DATA'!B1` = 'abc'
    - `'[report.xlsx]REPORT'!B1` = #VALUE!
- 决定结果的必需证据：
    - `'[report.xlsx]CONFIG'!A1` = 2 — 条件：选中分支二
    - `'[report.xlsx]ALT'!C2` = #VALUE! — 外链错误：取自 ext.xlsx 的 #VALUE!，经加法传播
- 读到但不决定结果（可缺）：
    - `'[report.xlsx]DATA'!B1` = 'abc'
    - `'[report.xlsx]CALC'!B1` = #VALUE!
    - `'[report.xlsx]ALT'!C1` = <空白> — 本次读到，算术中按 0 计；换成任何非错误值错码不变，故非必需

## 证据必要性检验（拿掉任一必需证据，结论即不能重现）

每次检验先清掉未执行分支的格子（证明其可缺），再移除待验证据。

- [运行A] 基线：仅清掉未执行分支（ALT!C1、ALT!C2）：通过。清 ALT!C2 -> REPORT!B1 = #VALUE!（结论仍重现）
- [运行A] 移除 CONFIG!A1（清空）：通过。清 ALT!C2 + 清 CONFIG!A1 -> REPORT!B1 = 0.0（结论无法重现（符合预期））
- [运行A] 移除 DATA!B1（清空）：通过。清 ALT!C2 + 清 DATA!B1 -> REPORT!B1 = 0.0（结论无法重现（符合预期））
- [运行B] 基线：仅清掉未执行分支（DATA!B1）：通过。清 DATA!B1 -> REPORT!B1 = #VALUE!（结论仍重现）
- [运行B] 移除 CONFIG!A1（改回 1，即条件取另一值）：通过。清 DATA!B1 + CONFIG!A1=1 -> REPORT!B1 = 0.0（结论无法重现（符合预期））
- [运行B] 移除 ALT!C2 外链（清空）：通过。清 DATA!B1 + 清 ALT!C2 -> REPORT!B1 = 0.0（结论无法重现（符合预期））
- [运行B] 对照：ALT!C1 空白格换成 7（非必需证据）：通过。ALT!C1=7 -> REPORT!B1 = #VALUE!（结论仍重现）

## 类别诊断：确实算出了错，还是公式/输入没取到

- 正式运行A/B：公式均已编译、外链工作簿均已加载，`REPORT!B1` 的 #VALUE! 是求值真实算出的错误值。
- 对照实验：删掉 ext.xlsx 后重算运行B，`REPORT!B1` = **#REF!**（formulas 对加载失败的外链单元格替换为 #REF!）——这属于"所需输入没取到"，错码也不同，应与"确实算出了错"区分。
- 同一错码两次出现的根因不同：运行A来自分支一文本乘数，运行B来自分支二外链错误；仅看错码无法区分来源，必须回溯到源格。
