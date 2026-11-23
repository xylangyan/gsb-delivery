# 多站点归档合并：最低费用 Kerchunk 索引

本目录是一个完全离线、确定性的小型实例，把题面里的规则逐条落到真实的
压缩字节、真实的 `(uri, offset, length)` 引用和可审计的费用账上。所有数据
块都用 zstd 压缩，发布的索引可直接用 `fsspec` 引用文件系统 + `zarr` 重读。

## 运行

```bash
.venv/bin/python -m contract_kerchunk.run
.venv/bin/python -m pytest contract_kerchunk/test_contract.py -q
```

产物都在 `contract_kerchunk/out/`：

| 文件 | 内容 |
| --- | --- |
| `archive_A.bin` / `archive_B.bin` | 两套源归档，内部块格不同 |
| `manifest.json` | 块映射、审批清单、合同、真值、时间轴 |
| `plan.json` | 每个目标窗口的决策、付费物理范围、缺许可报告、总费用 |
| `index.json` | 可发布的 kerchunk 引用（v2 zarr 引用格式） |
| `published/**/*.bin` | 补算后持久化的新压缩块与坐标块 |
| `verification.json` / `optimality.json` | 重读校验与最优性审计 |

## 题面规则如何落地

- **审批清单逐点定正误**（`manifest.json -> ledger`）：时间索引 2 是
  A/B 重复冲突点（temp 取 A，wind 因许可取 B）；索引 5 对所有变量都是
  真正缺测；索引 6 上 B 的 temp 是坏值 99，清单只接受 A。
- **盘上块格不同**：A 的 temp/rh/soil 用块格 4、wind 用整块 8；B 统一用
  块格 2。目标块格在合同 `target_chunks` 中逐变量指定。
- **多块指向同一段原始字节**：B 的 `gust` 不写新字节，块映射直接复用
  B/wind 的 `(offset, length)`。
- **能整块直引就直引（零费用）**：如 `temp[0:4]` 与 A 的原生块字节一致，
  索引直接引用 `archive_A.bin` 内的原始范围；`rh` 三个窗口与 `gust[6:8]`
  同理。
- **许可之外同值也不许选**：wind 第一目标窗由「A 的前半压缩块 + B 的
  后半值」补算而成；A 后半块虽然数值相同，但 `window_rules` 明确不许可，
  规划器不会选它。
- **物理范围读取只付一次**：付费键是 `(file, start, end)`。B 的 153、170、
  187 三段同时被 wind 与 gust 的补算需要，合并后只各计费一次（共 9 个
  唯一物理范围）。
- **远引用必须持久**：补算结果写入 `published/` 后以
  `[绝对 file:// 路径, offset, length]` 发布，索引里数据块键没有任何
  内联/临时字节（见 `no_inline_ephemeral_refs` 校验）。
- **拼不出合法块就报缺口**：`soil` 的目标窗 `[6, 8]` 在读合同里完全没有
  授权范围，`plan.json -> infeasible` 明确给出变量、目标窗口和尚缺的
  原数据许可；索引对该窗写一个只含填充值的持久块，重读不会吐出陈旧源值。

## 费用与最优性

- 单价：A 每次物理范围读取 3，B 每次 2（`read_fee`）。
- 审批清单为每个位置唯一确定来源，因此每个可行窗口所需的源字节闭包是
  唯一的；所有零费用直引全部取满后，付费范围只剩被迫读取的 9 段。
- 总费用 = 4×3（A）+ 5×2（B）= **22**。
- `optimality.py` 独立从清单+合同重推被迫读取闭包，断言其与计费范围完全
  一致、直引块字节与真值一致，从而证明 22 是最低费用（`optimal: true`）。

## 重读等价性

`verifier.py` 重新打开 `index.json`，逐项核对坐标、数值、缺测位置、来源
标注、引用地址可达性与费用勾稽，共 31 项检查全部通过。
