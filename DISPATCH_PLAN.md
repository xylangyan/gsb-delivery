# 双入口修订单发单规划器

当前工作区没有真实的原记录、两版字段定义、修订登记或签批点 JSON，题目目录中的 `prompt.txt` 又被 GSB 沙箱显式禁止读取。因此不能诚实地写出真实修订号级结论；`handoff_dispatch.py` 是可直接加载真实交接单后产出该结论的通用规划器。

## 运行

```bash
.venv/bin/python handoff_dispatch.py handoff.json --format json --output plan.json
.venv/bin/python handoff_dispatch.py handoff.json --format md --output plan.md
.venv/bin/python handoff_dispatch.py --demo
.venv/bin/python -m pytest tests/test_handoff_dispatch.py -q -o addopts=''
```

可复制 `examples/dispatch-handoff.example.json` 改成真实交接单。对应输出样例在 `examples/dispatch-plan.example.json` 和 `examples/dispatch-plan.example.md`。

## 交接单结构

- `initial`：原登记记录的原始值；factory 字段也应给折算前的原始值。
- `schemas.shared` 或 `schemas.pclass` / `schemas.precord`：两入口的实际字段定义。支持 `type`、`mandatory`、`initial`、`factory`、字段 `invariant` 和全局 `invariants`。
- `revisions[]`：按原顺序登记，每条只出现一次。单字段用 `field` + `value`，多字段用 `values`。
- `revisions[].archivable`：是否有“只留档、不发给导入端”的许可；缺省为 `false`，即两条链都必须实际收到。
- `revisions[].group` 或顶层 `commit_groups`：必须处在同一次提交边界内的修订。
- `checkpoints[]`：签批点；`after` 指向最后一条已到达修订，`signoffs` 给出每项实际采用的修订号、原始值类型和缺项状态。

Python API `plan_dispatch(..., pclass_cls=..., precord_cls=...)` 可直接传入交接系统里真实的 `PClass` / `PRecord` 类，不必把任意 factory 或 invariant 降成 JSON 表达式。

## 规划规则

1. 没有留档许可的修订固定送出。
2. 签批点写明采用某修订时，该修订固定送出；在同一签批点之前、会成为该字段最新写入的后续修订必须留档。
3. 若固定送出的修订会覆盖签批点要求采用的旧修订，判定为签批点不可达。
4. 除签批点或提交约束要求外，已许可修订一律不送，以最小化真正写入数。
5. 每个签批点是提交边界；同一原子组被签批点边界切开时直接报冲突。其余情况下相邻签批点之间只提交一次，以最小化提交数。
6. 小单的自由留档组合不超过 12 项时，会按新增写入数枚举，并用原生 evolver 验证；同写入数选择提交数最少者。
7. 长单使用“每字段最新采用点 + 后缀最小采用修订”的压缩约束，不按所有留档组合重放原记录。

## Pyrsistent 语义

- `PClass` evolver 的 `set()` 只暂存原值；factory、类型、字段 invariant 和全局 invariant 主要在 `persistent()` 构造时检查。因此中间非法文本若在提交前被覆盖，可能可以通过。
- `PRecord` evolver 每次 `set()` 都会跑 factory、类型和字段 invariant。类型错误会在 `set()` 处抛 `PTypeError`；字段 invariant 或嵌套 invariant 错误会进入 `_invariant_error_codes`，之后即使覆盖成合法值，旧错误仍保留到 `persistent()`。
- 全局 invariant 只在提交时检查。货位一 `-3` 与货位二 `+3` 必须放在同一次 `persistent()`；拆成两次提交会让第一次的总量约束失败。
- 签批来源按修订指针记录，不用值相等推断。未动字段即使 factory 后的值与后续修订数字相同，`actual_revision` 仍为 `null`，原始类型仍取原登记值的类型。

## 输出

- `sent_revisions`：两种入口都照走的有序写入清单。
- `commits[]`：提交边界及每个边界内的修订。
- `archived_only[]`：只留档修订和具体原因，例如 `record_entry_rejects_write`、`shadowed_to_preserve_signoff`、`covered_by_later_sent_write`。
- `blockers[]`：无共用走法时的字段报错、被强制覆盖的签批点或被切开的原子提交组。
- `verification.checkpoints[]`：用原生 evolver 提交后核对出的实际修订来源、原始类型和缺项状态。

## 复杂度

设修订数为 `N`、签批项为 `K`：约束构造和二分定位约为 `O((N + K) log K)`，原生核对只重放最终送出的写入；内存为 `O(N + K)`。这避免了对长单重复覆盖位置做 `2^N` 次整单重放。
