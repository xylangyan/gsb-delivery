# MySQL 流水事务级核对结果

## 输入状态

当前工作区只包含 `python-mysql-replication` 源码，未发现两份封存 binlog、经核清单或独立表定义；题目目录在本运行沙箱中不可读。因此下面的 `A1`–`A7` 是题面给出的样本/稽核项次序，不是臆造的 MySQL 事务号。拿到清单后，应把每行替换为清单确认的卷身份、binlog 文件、GTID/事务号和提交次序。

## 核对结果（按题面样本出现次序）

| 稽核序 | 实际事务定位 | 对象/键 | 行变更序列与证据 | 结论 | 第一处真正差异或需补证据 |
|---|---|---|---|---|---|
| `A1` | 待清单填入：卷 ID、binlog、GTID/事务号、TP 外包区间、包内序号 | 同一个计数键 `K` | 普通卷为 `K: v -> v-1 -> v`；压缩包内也解出两次对应的行变更 | **相同** | 无。必须按两次变更及前后值核对；期末值相同本身不是充分证据 |
| `A2` | 同上，需记录第二次写的包内序号/字节偏移 | 同一个计数键 `K` | 普通卷有两次写，第二边只保留一次写 | **不一致** | 若第一次变更相同，第一处差异是第 2 次变更缺失；若唯一一次写与普通卷第 1 次不同，则以第 1 个不匹配字段为第一处差异 |
| `A3` | 同上，需记录 NULL 写入及补回两次变更的序号 | 另一个键 `N` | 一侧存在 `N: x -> NULL -> x`，另一侧没有对应的 NULL 往返 | **不一致** | 第一处真正差异是显式写入 `NULL` 的那次变更；后续补回原值不抵消该差异。若两边都有同序、同值的 NULL 往返，则应判相同 |
| `A4` | 表映射定位到同一 TP 包内的 `Table_map_event` | `table_id -> schema.table/列定义` | 表映射先于同包行事件出现；`Table_map_event` 可以不作为稽核事件类型输出，但必须作为解码元数据消费 | **解析前提成立，不得跨事务补映射** | 同包映射缺失或包尾截断时，补该 TP 原包范围及当时表定义；不能借用另一事务中数值恰好相同的 `table_id` |
| `A5` | 待清单填入前值事件的卷、事务、包内序号/偏移 | 某个未写入镜像的字段 `C` | 行镜像未写 `C`，但能从同一卷经核的原行前值证据补证 | **可补证并继续判定，不属于 `A6` 类缺证** | 必须用补证值与对照侧实际值比较：相符则报“相同（前值补证）”，不符则在该字段报第一处差异；报告需附前值证据定位 |
| `A6` | 待补：表、主键、事务号、字段名、镜像侧、原包范围 | 某个未写入镜像的字段 `C` | 镜像中没有该字段，也没有对应原行/前值证据 | **证据不足，不能判相同或不一致** | 需补 `C` 的实际值或完整行镜像、可核验前值，以及对应 TP/普通事件的原始包范围；不得把“未写入”猜成 SQL `NULL`、未变更或某个实际值 |
| `A7` | 截断的 `Transaction_payload_event`：保留外包起止位移和已解出内部偏移 | 包内前几行 | 压缩包尾部未到齐；开头解出的几行看似相同 | **待核（`INCOMPLETE_PAYLOAD`）** | 这些行只能作为前缀证据，不能出具“相同”结论；需补缺失包尾/完整 TP 范围。若完整前缀已包含可验证的真正差异，可先报第一处差异并注明尾部未核 |

`A7` 不阻塞其他包完整的事务：完整事务继续按 `A1`–`A6` 的规则对账并给出独立结论。

## 唯一可查定位键

mysqlbinlog 对 TP 包内多个事件可能显示相同的外层结束位移，因此不能只用 `end_log_pos` 定位“哪一次原改动”。每条变更至少记录：

```text
volume_id + binlog_file + verified_txn_ref + commit_order
+ outer_TP_start_pos + outer_TP_end_pos
+ inner_event_index + inner_byte_offset/range
+ local_table_id + schema.table + event_type
+ row_index_in_event + primary_key
```

同一外层结束位移下，`inner_event_index` 和 `inner_byte_offset` 是区分第一次、第二次原改动的关键；同一行事件含多行时还要保留 `row_index_in_event`。

## 字段三态判定

现有读取器的行字典会把显式 NULL 和“列位图未包含”都表现为 Python `None`，但同时提供 `before_none_sources` / `after_none_sources`：

- 显式 SQL `NULL`：none-source 为 `null`。
- 镜像未写入该列：none-source 为 `cols bitmap`，不能当作显式 NULL。
- 其他不可直接当普通值的状态（如超范围日期、JSON 部分更新）：按对应 none-source 标记继续补证，不能猜值。

因此核对单元应是“字段状态 + 值 + 证据来源”，而不是只比较最终行字典。

## 代码现状提示

- `pymysqlreplication/constants/NONE_SOURCE.py:1` 已定义 `null` 与 `cols bitmap` 等来源。
- `pymysqlreplication/row_event.py:253` 对列位图未包含的字段记录 `COLS_BITMAP`；`pymysqlreplication/row_event.py:259` 对显式 NULL 记录 `NULL`。
- `pymysqlreplication/row_event.py:99` 在找不到 `table_id` 映射时会把行事件标记为不处理；解 TP 包时必须先建立包内局部 `table_map`，再解码后续行事件。
- 当前快照的 `pymysqlreplication/constants/BINLOG.py:34` 之后未注册 `TRANSACTION_PAYLOAD_EVENT`（MySQL 类型码 `0x28`），`pymysqlreplication/packet.py:116` 会把未知事件回落为 `NotImplementedEvent`。实际跑批前需要先解包并把内部事件字节交给现有行事件类。

## 仍需提供的输入

1. 经核清单：两侧卷 ID、binlog/归档路径、表定义版本、对应事务号/GTID 与提交次序。
2. 两份封存流水或可读取的归档片段；若文件不在工作区，请放入 `inputs/` 或提供可读绝对路径。
3. `A6` 类事项所需的表名、主键、字段名、镜像侧和原行/前值定位。
4. `A7` 截断 TP 包的缺失尾部或完整原始包范围。
