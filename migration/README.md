# 两套标量规则下的 YAML 迁移工具包

供应方更换了隐式标量解析口径。本工具包让每个批次显式选定一套规则，
逐行输出同一标量在旧/新规则下的读值，并为两套规则能无损共同读出的字段
生成更正计划；遇到不可逆的锚点合并先报冲突、拒绝重写。

## 两套规则

| 标量 | 旧规则 `legacy`（PyYAML 1.1 口径） | 新规则 `modern`（YAML 1.2 Core） |
| --- | --- | --- |
| `on` / `off` / `yes` | `bool` | `str`（仅 `true/false` 是 bool） |
| `01` / `013` | 八进制 `int`（1 / 11） | 十进制 `int`（1 / 13）；八进制须写 `0o13` |
| `1:30` | 六十进制 `int`/`float` | `str` |
| `2021-05-01T08:30:00+08:00` | 带时区的 `datetime` | `str`（Core 无 timestamp 标签） |
| `true` / `false`、`0x..`、`0b..` | 一致 | 一致 |

两套加载器共用 `SafeConstructor`，因此显式标签
（`!!bool`、`!!int`、`!!timestamp` 等）在两套规则下构造结果完全相同——
这是"无损共同读出"的基础。

代码：`migration/rules.py`（`LegacyLoader` / `ModernLoader` /
`ModernResolver` / `ModernConstructor`）。

## 更正动作

`migration/audit.py` 并行用两套规则解析同一文本、同步遍历表示树，
按**构造值**而非仅按标签比对（因此能捕获 `013` 这种同标签不同值）：

- `tag`：保留旧类型读值时加显式标签，如 `!!bool on`、
  `!!timestamp 2021-05-02T10:00:00Z`；
- `literal`：八进制/六十进制替换为规范十进制字面量，如 `013` → `11`
  （保留旧值）或 `13`（保留新值）；
- `quote`：保留字符串读值时双引号，如 `on` → `"on"`。

每个重写文件都会用两套规则回读，断言与被保留的读值逐值相等、类型一致。

## 不可逆合并冲突

`migration/audit.py:find_merge_conflicts` 对每个映射先做 `flatten_mapping`，
再比较两套规则下键的归并情况。样本 `samples/batch_old/conflict.yaml`：

```yaml
defaults: &defaults
  on: 1      # 旧规则键为 bool True；新规则键为 str "on"
  "on": 2    # 两规则键都是 str "on"
profile:
  <<: *defaults
  on: 3
```

旧规则下 `on` 与 `"on"` 两个键相等而塌缩，新规则下是两个独立键，
合并后的数据无法还原被覆盖的字段。该文件：

- 逐行输出仍照常给出（相关行标记 `BLOCKED`）；
- `plan_rewrite` 直接抛 `ValueError`，CLI 返回非零码且不产出重写文件；
- 需人工先拆分键（例如改名或显式 `!!str on` / `!!bool on`）再重跑。

## 交错批处理

每个 `--batch 路径=规则` 独立持有自己的解析器，可交错重复出现：

```bash
.venv/bin/python -m migration.cli \
  --batch samples/batch_old=legacy \
  --batch samples/batch_new=modern \
  --batch samples/batch_old=legacy \
  --out-dir samples/rewritten --report samples/report.json
```

默认每个批次保留**本批规则**的读值（旧批保旧值、新批保新值）；
`--preserve legacy|modern` 可全局覆盖，`--quote` 强制字符串字段用引号
而非标签。退出码在任一批次存在冲突时为 `1`。

## 样本与输出

- `samples/batch_old/`：旧文件（含一个冲突文件、一个可无损修复文件）；
- `samples/batch_new/`：新导入文件；
- `samples/rewritten/`：更正后的文件；`samples/report.json`：逐文件汇总。

测试：`tests/test_scalar_migration.py`（11 项，覆盖规则差异、逐行输出、
两种保留方向、引号模式、冲突拦截与 CLI 交错处理）。
