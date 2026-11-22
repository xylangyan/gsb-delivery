# OData 商品导出少件货：Pyslet 固定版本复盘

## 结论先行

在固定 Pyslet（`b30e9a439f6c0f0e2d01f1ac80986944bed7427b`）上，用同一套本地服务
记录和同一批报告参数，复现出两个互相独立的客户端缺陷：

1. **投影缺失被当成 null**：报告加选列后，`$select` 不含 `Active`。服务响应中
   该字段**不存在**（不是 `m:null="true"`），Pyslet 解码器把它置为 `None`；
   后处理只读 `entity['Active'].value`，于是把首件活跃商品当成非活跃丢弃。
2. **续页没有沿用上一页的 next**：`iterpage(set_next=True)` 只从 next 链接中
   抽取 `$skiptoken`，再用本地保存的 filter/orderby/select 重建 URL；一旦重建
   使用 `$skip` 或错误的筛选/游标，就会跳过下一页开头的商品。`P-0001` 与
   `P-0002` 的 `BinSequence` 同为 100，服务器按 `ProductID` 升序打破平局；
   跳过 `P-0001` 之后的第一个位置恰好越过 `P-0002`。

服务代码未改。复现装置在 `forensics/`，所有原始请求/响应在
`forensics/artifacts/raw/` 与 `forensics/artifacts/region/`。

## 固定参数

- 服务根：`http://host/service/`，`Server.topmax = 1`
- 报告请求：`$top=2`
- 业务筛选：`$filter=Warehouse eq 'W1'`
- 排序：`$orderby=BinSequence asc,ProductID asc`
- 投影：全字段，或“加选列”后的部分投影
  `$select=BinSequence,Name,ProductID,Region,Warehouse`（无 `Active`）
- 导航：`$expand=Category`

## 数据

| 键 | Warehouse | BinSequence | Active | Region | Category | 说明 |
|---|---|---|---|---|---|---|
| P-0001 | W1 | 100 | true | R1 | C1 | 首个活跃商品 |
| P-0002 | W1 | 100 | true | R1 | C1 | 与 P-0001 同排序值，键打破平局 |
| P-0003 | W1 | 110 | true | **真实 null** | C2 | 用于 null/展开证据 |
| P-0004 | W1 | 120 | false | R2 | C1 | 合法排除 |
| P-0005 | W1 | 130 | true | R3 | C2 | 末件活跃商品 |
| Q-0000 | W2 | 90 | true | R9 | C1 | 筛选外对照 |

应有结果：`P-0001, P-0002, P-0003, P-0005`。

## 两种首次少键机制

### 机制 A：Active 因部分响应未选中，后处理当成 null

`R1_projection_only_partial_next`（严格 next，但用部分投影，naive 过滤）第 1 页：

- 请求：`...?$filter=Warehouse eq 'W1'&$expand=Category&$select=BinSequence,Name,ProductID,Region,Warehouse&$orderby=BinSequence asc,ProductID asc&$top=2&$skip=0`
- 响应中的 P-0001：`ProductID/Name/Warehouse/BinSequence/Region` 均存在；
  `Active` **present=false**；`Note` 也 present=false。
- Pyslet 解码：`entity.is_selected('Active') == False`，
  `entity['Active'].value is None`。
- naive 后处理 `Active.value is True` 判假，P-0001 在第 1 页后处理被丢。

原始证据：`artifacts/raw/R1_projection_only_partial_next/page-01.json`
（`field_presence.P-0001.Active = {present: false, null: null}`）与
`page-01.response.xml`。同样地 P-0002/P-0003/P-0005 分别在各自页被误丢。
`R3_both_partial_rebuilt` 中 P-0003/P-0005 也证明：即使续页错，只要该页响应
有商品且 `Active` 缺失，naive 后处理仍会丢。

### 机制 B：续页未沿用上一页实际返回的 next

`R2_continuation_only_full_rebuilt`（全字段，但重建 `$skip`，naive 过滤）：

- 第 1 页请求：`...&$top=2&$skip=0`
- 第 1 页响应真实 next：
  `...?$filter=Warehouse%20eq%20'W1'&$expand=Category&$orderby=BinSequence%20asc%2C%20ProductID%20asc&$skiptoken=1`
- 第 2 页实际请求：`...?$filter=Warehouse%20eq%20'W1'&$expand=Category&$orderby=BinSequence%20asc%2C%20ProductID%20asc&$top=2&$skip=2`
  —— **不等于上一页 next**
- 第 2 页响应：只有 P-0003；P-0002 **从未出现在任何页响应**。

原始证据：
`artifacts/raw/R2_continuation_only_full_rebuilt/page-01.response.xml`（next）、
`page-02.request.txt`（实际请求）、`page-02.response.xml`（P-0003）。

`C2_next_only_partial_next` 则严格追随 next，第 2 页请求就是上一页 next
（`request_equals_previous_next=true`），响应确实包含 P-0002；但 naive 后处理
因 `Active` 缺失又把它丢掉。这恰好把两个机制分离开。

### 同序值的键打破平局

P-0001 与 P-0002 都 `BinSequence=100`。服务按 `ProductID asc` 稳定排序，
P-0001 在前、P-0002 在第二页首。正确续页从 P-0001 之后的 skiptoken 开始；
重建 `$skip=2` 表示“已返回 2 条”，但第 1 页因 `topmax=1` 实际只返回 1 条，
于是一步跨过了 P-0002。页边界：**第 1 页 → 第 2 页之间**。

## 逐键首次漏点

完整台账：`artifacts/ledger.json`。基线为 `R0`（全字段、严格 next、naive），
它完整得到四个活跃键。

| 键 | 首次漏在哪一页 | 哪个边界 | 证据 |
|---|---|---|---|
| P-0001 | 1 | 客户端后处理（Active 未选中→None） | R1 p1 present=false；C2 p1 同样丢；C1 p1 回查后保留 |
| P-0002 | 1→2 | 续页越过；服务第 2 页本应是 P-0002 | R2/C1/X2 所有页均无 P-0002；R0/C2/C3 p2 有 P-0002 |
| P-0003 | 3 | 客户端后处理（Active 未选中→None） | R1/C2 p3 present=false；R3 在 p2 遇到同样问题 |
| P-0004 | 4 | 合法排除（Active=false）；部分投影下也会被误判，但结果集不变 | R0/C2/C3 p4 Active=false；R1 p4 Active 缺失 |
| P-0005 | 5 | 客户端后处理（Active 未选中→None） | R1/C2 p5 present=false；R3 p3 同样 |
| Q-0000 | — | 筛选外，从未进入 W1 页 | 所有 run 均无 Q-0000 |
| P-0002（同序键） | 1→2 | 键打破平局后的下一页首条 | 第 1 页 next=skiptoken=1；重建 skip=2 取 p3 的 P-0003 |

注：`P-0004` 在部分投影下也会因 `Active` 缺失被判 null；它不是少件，但它证明
“加选列后 Active 不可见”是普遍现象，而非只影响首件。

## 报告矩阵

同一服务实例、同一数据、同一参数；只交换投影与续页做法：

| run | 投影 | 续页 | 客户端过滤 | 结果 | 缺 |
|---|---|---|---|---|---|
| R0 | 全字段 | 严格 next | naive | P-0001,P-0002,P-0003,P-0005 | 无 |
| R1 | 部分（无 Active） | 严格 next | naive | （空） | P-0001,P-0002,P-0003,P-0005 |
| R2 | 全字段 | 重建 `$skip` | naive | P-0001,P-0003,P-0005 | P-0002 |
| R3 | 部分 | 重建 `$skip` | naive | （空） | 全部 |

### 三种改变

以 R3 为故障组合，逐一交换：

| 改变 | run | 结果 | 仍缺 | 说明 |
|---|---|---|---|---|
| 只更正客户端过滤（Active 未选中时按选择判断/回查） | C1 | P-0001,P-0003,P-0005 | **P-0002** | 首件被救回，但续页仍越键 |
| 只严格追随 next | C2 | （空） | 全部 | P-0002 重新出现在 p2，但 naive 仍因 Active 缺失丢弃 |
| 两者均更正 | C3 | P-0001,P-0002,P-0003,P-0005 | 无 | 完整恢复 |

补充：C2b 直接用 Pyslet 原生 `collection.iterpage(set_next=True)`（部分投影、
naive 过滤）同样得到空集。它逐页取到的键集合与 C2 相同，但请求仍保留
`$top=2`（`...&$top=2&$skiptoken=1`），而 C2 逐字使用 next 链接
（`...&$skiptoken=1`，无 `$top`）。两者都能取到每一页，说明严格 next 只解决
“取到页”，不解决“解码/过滤”；同时可见 Pyslet 的 `iterpage` 会重组 URL
（保留本地 top），而不是逐字请求 next 链接。

## 两个独立控制令

`P-0002` 会被两个问题同时遮蔽，因此需要至少让一个不生效：

- **控制 1（让续页问题不生效）**：`C1_filter_only_partial_rebuilt`。
  部分投影 + 重建 `$skip` + 选择感知过滤。续页仍错（p2 直接 P-0003），
  但 P-0001/P-0003/P-0005 全部保留，只有 P-0002 缺。证明 P-0002 不是 Active
  问题。
- **控制 2（让投影问题不生效）**：`R2_continuation_only_full_rebuilt`。
  全字段 + 重建 `$skip` + naive 过滤。`Active` 全程可见，P-0001/P-0003/P-0005
  均保留，只有 P-0002 缺。证明 P-0002 不是投影问题。

两份控制结果相同（仅缺 P-0002），但独立地把同一观察归因到续页：
C1 中 Active 问题被关闭，R2 中 Active 问题根本不存在。

## 续页缺陷的两种错误重建

同一根因（不以服务返回的 next 为准）可以有不同表现：

- `X1_wrong_cursor_full_rebuilt_skiptoken1`：重建时固定 `$skiptoken=1`。
  第 2 页重复返回 P-0001 之后的 P-0002，第 3/4/5 页继续重复 P-0002，
  永远见不到 P-0003/P-0005。证明错误游标会“原地打转”。
- `X2_wrong_filter_full_rebuilt`：重建时改错筛选为 `Warehouse eq 'W2'`。
  第 2 页直接空集，P-0002/P-0003/P-0005 全部消失；客户端把空集当成结束。
  证明错误筛选会让整段商品静默消失。

两者都保留在 `artifacts/raw/`，便于对照“错筛选”和“错游标”。

## Region 真 null 与导航展开

P-0003 的 `Region` 是数据源真实 null，并带 `Category` 导航（C2 的 Region 也是
真实 null）。证据：

- `region/expanded.response.xml`：
  `<d:Region m:null="true"/>`，同一条目下 `<m:inline><entry>...<d:Region m:null="true"/></entry></m:inline>`。
- `region/unexpanded.response.xml`：`Region` 仍为 `<d:Region m:null="true"/>`；
  `Category` 只有自闭合 link，无 `<m:inline>`。
- `region/expanded.json`：`Region.selected=true, value=null`；
  `Category.expanded=true, key=C2, Category.Region.value=null`。
- `region/unexpanded.json`：`Region.selected=true, value=null`；
  `Category.expanded=false`，无子实体。

结论：展开只决定导航是否内联，不会把真实 null 变成“未选中”，也不会把
“未选中”变成 null。报告必须同时保留 **字段存在性**、**m:null 属性** 和
**is_selected**；只保留客户端 `.value` 会把三者混为 `None`。

## 只保存客户端结果或只比总数，为什么不够

- **只保存客户端结果**：无法区分“服务没返回”和“客户端过滤掉”。
  `R2` 与 `C2` 的最终结果都缺 P-0002，但 R2 的 p2 响应根本没有 P-0002，
  C2 的 p2 响应有 P-0002（只是被判 null）。没有原始页就无法判因。
- **只比总数**：`$count` 对同一筛选返回 5（`artifacts/count.json`），
  基线、R2、C1 都可能得到 3 条，R1/C2 都可能得到 0 条；总数相同但缺口键
  不同，且无法看出“第 1 页实际只有 1 条、next 却是 skiptoken=1”，也无法
  看出“Active 字段在原始 XML 中不存在”。

因此报告必须同时保留四件套：**每页原始响应键集合、字段存在性/m:null、真实
next 链接、客户端实际请求**。本项目 `write_run()` 正是按这四件套落盘。

## 可直接重复的请求

```http
# 第 1 页（部分投影）：响应缺 Active，next 指向 P-0002
GET /service/Products?$filter=Warehouse%20eq%20'W1'&$expand=Category&$select=BinSequence%2CName%2CProductID%2CRegion%2CWarehouse&$orderby=BinSequence%20asc%2C%20ProductID%20asc&$top=2&$skip=0 HTTP/1.1
Accept: application/atom+xml

# 正确续页：取 P-0002
GET /service/Products?$filter=Warehouse%20eq%20'W1'&$expand=Category&$select=BinSequence%2CName%2CProductID%2CRegion%2CWarehouse&$orderby=BinSequence%20asc%2C%20ProductID%20asc&$skiptoken=1 HTTP/1.1
Accept: application/atom+xml

# 错误重建：跳过 P-0002，直接 P-0003
GET /service/Products?$filter=Warehouse%20eq%20'W1'&$expand=Category&$select=BinSequence%2CName%2CProductID%2CRegion%2CWarehouse&$orderby=BinSequence%20asc%2C%20ProductID%20asc&$top=2&$skip=2 HTTP/1.1
Accept: application/atom+xml

# Region 真 null + 展开
GET /service/Products('P-0003')?$expand=Category&$select=ProductID%2CName%2CWarehouse%2CBinSequence%2CActive%2CRegion HTTP/1.1
Accept: application/atom+xml;type=entry

# Region 真 null + 未展开
GET /service/Products('P-0003')?$select=ProductID%2CName%2CWarehouse%2CBinSequence%2CActive%2CRegion HTTP/1.1
Accept: application/atom+xml;type=entry
```

无需 socket 即可重放已保存的请求：

```bash
cd forensics
../.venv/bin/python replay.py \
  artifacts/raw/R2_continuation_only_full_rebuilt/page-02.request.txt \
  --response artifacts/raw/R2_continuation_only_full_rebuilt/page-02.response.xml
# 输出末尾应为 BYTE-IDENTICAL
```

## 复跑

```bash
cd forensics
../.venv/bin/python run_matrix.py
../.venv/bin/python checks.py     # PASS: 40 assertions
```

`checks.py` 对以下内容做独立断言：基线完整；R1 全部因 Active 缺失而丢；
R2/C1 仅缺 P-0002；C2 能看到 P-0002 但仍丢；C3 完整；原生 iterpage 能到达
所有页；X1/X2 各自重现续页缺口；部分投影每页 Active 均 absent 且 Region
present；Region 展开/未展开均为真 null；`$count=5`；关键页重放逐字节一致。

## 文件索引

- `forensics/harness.py`：确定性数据集、WSGI 传输、原始页解析、选择感知回查
- `forensics/run_matrix.py`：全部 run、Region 证据、`$count`、`ledger.json`
- `forensics/replay.py`：从已保存请求重放服务响应
- `forensics/checks.py`：独立断言
- `forensics/artifacts/matrix.json`：每次 run 的逐页摘要与保留/缺失键
- `forensics/artifacts/ledger.json`：逐键首次漏点与边界
- `forensics/artifacts/raw/<run>/page-NN.{request.txt,response.xml,json}`：
  原始请求、原始响应、解析摘要
- `forensics/artifacts/region/{expanded,unexpanded}.{request.txt,response.xml,json}`
- `forensics/artifacts/count.json`：筛选后总数
