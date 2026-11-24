# 馆藏借件核账日报工具

工作区中没有发现真实的登记、回执或日期窗口文件，因此这里不能生成具体馆藏的事实日报。`loan_audit.py` 是按题面约束实现的通用核账器，真实登记整理成 JSON 后即可出账。

## 运行

```bash
.venv/bin/python loan_audit.py registry.json --format md --output report.md
.venv/bin/python loan_audit.py registry.json --format json --output report.json
```

也可以用 `--window 1900-03-01:1900-03-31` 覆盖登记中的窗口。完整字段示例见 `examples/loan-registry.example.json`，生成样例见 `examples/loan-daily-report.example.md`。

## 输入要点

- `loans[].item`：已确认的原件编号。
- `loans[].order`：同一原件的借件先后；缺省时使用数组顺序。
- `receipts[].copies[]`：同一张回执的整份抄件。求解器一次只选一份，不会把不同抄件中的借出、归还日期拼接使用。
- `copies[].dates`：键为借件端点，值为确定日期、日期数组、EDTF 表达式或 `{"start": ..., "end": ...}`。
- 一张回执可以在同一个 `dates` 中写多个端点，因此会保留“同一张回执同时列两件馆藏”的相关性。
- 多张回执写同一端点时，所选日期域必须有共同日期。
- `symbol_ranges`：按 `?`、`~`、`%` 等符号配置登记认可的前后天数。
- `date_domains`：为任意不精确表达式或命名域直接指定 `dates` 或 `start/end`。

## 核账规则

- 在外区间为半开区间 `[checkout, return)`：借出日计数，归还日不计数。
- 同一原件的有序借件必须满足 `previous_return <= next_checkout`。
- 每个独立回执连通批分别求极值，再相加得到全馆最小值和最大值。
- 联合抄件造成的“两件中恰有一件在外”由同一组抄件选择统一求解，不会按原件独立放宽。
- 每个日期段的 `min_witness` 和 `max_witness` 都给出可落实的整组回执、抄件、端点日期和借件记录。
- 相邻日期只有在极值、批次结构和见证端点结论都一致时才合并。
- 没有共同解释的连通批进入 `gaps`，其中给出最小冲突回执集合、相关原件和端点；其他批次继续正常出账。

## 测试

```bash
.venv/bin/python -m pytest tests/test_loan_audit.py -q -o addopts=''
```
