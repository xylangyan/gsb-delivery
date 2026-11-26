# 案卷合送装批器

这个目录提供不连真实邮箱的双中转规划/试算工具。它同时修正了 `IMAPClient.multiappend()` 的正文换行口径，使 MULTIAPPEND 中的每封信都和旧 `append()` 一样先把孤立 CR/LF 规范成 CRLF。

## 运行

```bash
python -m casebatch casebatch_example.json --format text
python -m casebatch casebatch_example.json --output plan.json
python -m casebatch compressed.json -q 12345678
```

也可以从根目录导入：

```python
from solution import TaskSpec, plan_task, plan_compressed

plan = plan_task(TaskSpec.from_dict(data))
compressed = plan_compressed(data)
print(compressed["manifest"].locate_case(12345678))
```

## 输入要点

- `cases[].id`：案号；同一案号的全部 `messages` 永远在同一条 APPEND 中。
- `cases[].folder`：目标文件夹；一条命令不得混文件夹。
- `cases[].predecessors`：已登记必须排在本案之前的案号。
- `allow_reorder`：未受优先级约束的案号是否允许按字典序搜索更优排列。
- `fixtures[]`：恰好两个中转夹具；`literal_plus` 决定标记是 `{n}` 还是 `{n+}`。
- `tiers[]`：每档包含 `burst_limit`（两次服务端回包之间允许的客户端字节）和 `command_limit`（整条命令字节）。
- `byte_edges` 或夹具档位上限决定整组字节档位；`transitions[category][byte_tier]` 给出下一命令档位。
- `tag`：固定命令标签，默认与测试夹具一致为 `tag`。

邮件可用 `body`、`body_b64`、`body_hex` 给正文，也可只给规范化后的 `length`。短式案卷簿可用 `length_cycle`、`flags_cycle`、`date_base/date_step_seconds`。日期必须是带偏移的 ISO-8601；空旗标显式给 `[]`。

## 字节口径

规划器逐段模拟 `IMAPClient._raw_command()` 与 `_send_literal()`：

- 命令头：`tag APPEND "folder" (...) "date"`。
- 原生 literal 标记：非 LITERAL+ 为 ` {len}\r\n`，LITERAL+ 为 ` {len+}\r\n`。
- 邮件数据使用 CRLF 规范化后的长度。
- 命令结尾 `\r\n` 计入总预算。
- 非 LITERAL+ 在每个 literal 标记后形成真实续报边界；区间包含上一 literal 数据、分隔空格、下一封信的旗标/日期和长度标记。
- LITERAL+ 没有逐 literal 续报，预算边界是整条命令完成。

超预算结果包含 `boundary`、`limit`、`sent_bytes`、超出字节数，以及压线字节所属的 `message_number/source/case_id`。

## 无解与预算来历

线性 DP 保存 `(position, fixture_a_tier, fixture_b_tier)`。若所有终点均不可达，返回 `status="infeasible"`、一个可复算的 `failure.group`、两处夹具的违规字段，以及 `failure.budget_origins`。预算来历会记录初始档，或上一卷的案号、类别、整组字节、字节档位、转移规则和新档位。

## 压缩案卷簿

`segments[]` 表示重复小段：

```json
{
  "repeat": 5000000,
  "id_start": 10000001,
  "id_step": 1,
  "id_block_step": 2,
  "cases": ["...一小段模板..."]
}
```

规划器只展开有限探测段，在 `(段内相位, 两机预算档)` 上找可重复命令环。指定案号通过等差 ID 公式定位到全局案序，再用前缀/周期环/尾段算术算出真实命令编号、命令内案号和当时两机预算档；不会为每次重复重跑候选分法。

当前工作区没有随题给出具体案号、老回执和预算数值，因此 `casebatch_example.json` 是可运行的口径样例，不是某批真实案卷的最终装批单。
