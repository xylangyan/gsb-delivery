# ZODB 根范围增量交接档

这个目录提供一个不依赖业务类可导入的 ZODB 交接器。它按交接单列出的根和封卷 TID 范围，逐笔计算“该 TID 提交后从这些根可达”的对象图、Pickle 字节和 Blob 附件，生成只追加、可校验、可重放到新 `FileStorage` 的交接档。

工作区中没有挂载真实的两份 `Data.fs`、Blob 目录或交接单；`order.example.json` 是输入格式，`demo-archive/` 是用同构 Undo/移除/恢复场景生成的可运行样例。

## 命令

在仓库根目录使用自带依赖的解释器：

```bash
PY=.venv/bin/python

$PY handoff/zodb_handoff.py selftest
$PY handoff/zodb_handoff.py build --order handoff/order.json --through-tid 0x...
$PY handoff/zodb_handoff.py verify --archive handoff/archive
$PY handoff/zodb_handoff.py replay \
  --archive handoff/archive \
  --target-fs handoff/newrepo/Data.fs \
  --target-blobs handoff/newrepo/blobs
$PY handoff/zodb_handoff.py verify-target \
  --archive handoff/archive \
  --target-fs handoff/newrepo/Data.fs \
  --target-blobs handoff/newrepo/blobs
$PY handoff/zodb_handoff.py ack \
  --archive handoff/archive --batch 2 --by receiver \
  --target-fs handoff/newrepo/Data.fs \
  --target-blobs handoff/newrepo/blobs
```

退出码：`0` 表示全部根核实；`3` 表示归档结构和已交付内容有效，但至少有一个根/事务存在缺版本或缺附件；`4` 表示交接档哈希或结构损坏；`5` 表示重放库核对失败。

## 增量交接流程

1. 第一阶段只封到第二笔：`build --through-tid <T2>`，把生成的 `archive/` 交给接收方。
2. 接收方运行 `verify`、`replay --through 2`、`verify-target --through 2`，确认后运行 `ack --batch 2`。
3. 第三笔到达后再次运行 `build --through-tid <T3>`。新批次追加为 `batches/000003-<TID>/`，不会重写 `batches/000002-*/batch.json`、`receipt.json` 或 `acknowledgements/000002-*.json`。
4. 接收方再次 `replay --through 3`。重放状态记录在 `Data.fs.handoff-state.json`，已确认批次会跳过且回执哈希必须保持不变。

## 目录结构

```text
archive/
  archive.json                         # 总索引、版本去重表、根绑定
  order.resolved.json                  # 交接单副本
  store/objects/<sha前2>/<sha其余>      # 内容寻址的解析后 Pickle
  store/blobs/<sha前2>/<sha其余>        # 内容寻址的附件字节
  batches/000001-<tid>/batch.json       # 该 TID 的闭包、边、问题、写入清单
  batches/000001-<tid>/receipt.json     # 批次哈希和前一回执哈希
  acknowledgements/                    # 不可覆盖的接收回执
```

## 关键语义

- 每个检查点 `<T>` 对每个 OID 只选 `rev_tid <= T` 的最大事务版本，绝不调用当前值来代替历史版本。
- Undo 产生的新 TID 仍是独立检查点。若新记录通过回指引用旧字节，工具保存解析后的逻辑字节，同时在 `provenance.data_txn`、`origin_tid` 中保留回指 TID；目标库已有原事务时，重放会重新形成 FileStorage 回指。
- 中间从所有允许根移除的对象不会出现在该 TID 的可见闭包中；之后重新可达时，仍按当时选出的精确 `(OID, rev_tid)` 交付。
- 多个根引用同一对象时，`store/` 和批次 `writes` 按 `(OID, rev_tid)` 与 SHA-256 去重；每个根的 `edges` 和生成的 ZODB 根绑定保留实际 OID 引用。
- 跨封卷借字节只接受精确 `(OID, TID)` 记录，或 Undo/Backpointer 明确给出的 `data_txn`，并校验 SHA-256；只遇到相同 OID 的当前版本不会被采用。
- Blob 按精确 `(OID, rev_tid)` 查找；若 Undo 版本没有独立 Blob 文件，只允许沿记录给出的原始 `data_txn` 查找并校验哈希。
- 缺版本、缺附件、无法解析的 Pickle 会写入对应批次的 `issues`，包含根标签、检查点 TID、OID、期望版本、源 FileStorage 路径、记录偏移和搜索过的 Blob 路径。其他根的 `verified` 结果和已写入内容不受影响。
- 若 `range.tids` 或 `common_history_tids` 列出的整笔 TID 在所有源中都不存在，会生成 `missing_transaction: true` 的 gap 批次；它不会被伪造成可核对状态，重放时只记录 gap 而不写空事务。

## 演示

```bash
.venv/bin/python handoff/zodb_handoff.py verify --archive handoff/demo-archive
.venv/bin/python handoff/zodb_handoff.py verify-target \
  --archive handoff/demo-archive \
  --target-fs handoff/demo-target/Data.fs \
  --target-blobs handoff/demo-target/blobs
```

演示中 `T1` 见 `p1` 与附件 `p1\n`，`T2` 见 `p2` 且 `D` 根不可见，`T3` 是撤销 `T2` 的新事务并恢复 `p1` 与 `D`。`P` 和 `R1` 始终指向同一 OID。

`demo-archive.tar.gz` 是 `demo-archive/` 的打包副本；真实交接时建议每确认一笔就单独备份或分发对应 `batches/NNNNNN-*` 与只增的 `store/`。
