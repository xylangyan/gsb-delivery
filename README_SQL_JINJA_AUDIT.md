# SQL+Jinja 原稿稽核辅助

把 `SqlJinjaLexer` 的两层读法保留下来，同时补上它丢掉的“同一实际 SQL 词”
身份。实现位于 `pygments/lexers/sql_jinja_audit.py`，兼容入口为 `solution.py`。

## 直接使用

```python
from pygments.lexers.sql_jinja_audit import audit

result = audit("SELECT 'receipt_{{ depot }}_daily' AS x")
for token in result.tokens:          # 与 SqlJinjaLexer 完全同序、同类型
    print(token.start, token.end, token.token_type, token.value)

item = result.items[0]
item.text                            # "'receipt__daily'"（只拼 SQL 片段）
item.source_spans                    # ((7, 16), (27, 34))
item.template_references[0].text     # '{{ depot }}'
item.variable_names                  # ('depot',)

result.index.same_word(item.segments[0])   # 两截同属一个 sql_index
result.equivalences()                      # 片段区间 -> 同词全部片段
```

- 每个词条都有精确 `[start, end)` 源码区间；模板文字不计入 `text`。
- 两个文字恰好一样的 SQL 字面量仍是两个 `LiteralItem`（按出现次序编号）。
- 模板区域单独保留，可点到变量名和区间；段间模板不会混入字面项。

## 新旧对比

```python
from pygments.lexers.sql_jinja_audit import compare_audits, compare_tokens

diff = compare_audits(audit(old), audit(new))
diff.changed          # 按出现次序对齐，文字相同也不合并
diff.token_changes    # 含 unchanged 的全量 token 对比
```

`compare_audits(..., by='text')` 按固定文字优先匹配；重复文字仍各自成项。

## 更正单

```python
from pygments.lexers.sql_jinja_audit import Correction, apply_correction

new_source, new_result = apply_correction(
    source, Correction('%}', occurrence=0, kind='template_close'))
```

只替换指定那次出现的模板收尾，其余字符不改。替换后重新读原稿：即使后续
字符没动，现有两层读法也可能把它们重新分到另一类；`compare_tokens` /
`compare_classification` 会把这类 `reclassified` 显式列出（后者按原字符对齐，
即使后续注释被拆成多个模板 token 也能定位）。窗口版本 `apply_correction_window` /
`correction_sheet(..., context=N)` 只回读更正点附近的一小扇窗。

## 历史轮次与片段

```python
from pygments.lexers.sql_jinja_audit import AuditHistory, FragmentSpec

history = AuditHistory()
history.define_fragment('head', "SELECT 'abc", start=0)
history.define_fragment('tail', "def' AS x", start=0)
for round_no in (1, 2, 3):
    history.add_round(round_no, ['head', 'tail'])

item, spans = history.query_item(3, 0)
# spans: 每一原区间 (round, fragment, start, end)
```

- 短档可传完整原库词条：`audit` 的 `tokens` / `items` / 区间即完整原库读法。
  `for index, token_type, value in result.tokens` 仍可按下标/类型/文字读取。
- 同一短片段可被许多轮引用；`define_fragment` 只存一份。
- 旧片段尾可和下一片头共用一条语法：`audit_fragments` / `audit_spanning`
  只连接所选片段，不拼接整份历史原稿。
- `history.window(round, start, end)` 只切、只连所请求的窗口，不先拼整轮。

## 命令行

```bash
.venv/bin/python -m pygments.lexers.sql_jinja_audit_cli audit draft.sql
.venv/bin/python -m pygments.lexers.sql_jinja_audit_cli diff old.sql new.sql
.venv/bin/python -m pygments.lexers.sql_jinja_audit_cli rounds rounds.json
```

## 验证

```bash
.venv/bin/python -m pytest tests/test_sql_jinja_audit.py -q -o addopts=''
```


## 对象门面

`solution.py` 另外提供 `SqlJinjaAuditor`，适合多次调用：

```python
from solution import SqlJinjaAuditor

auditor = SqlJinjaAuditor()
result = auditor.audit(text)
diff = auditor.compare(old_text, new_text)
```
