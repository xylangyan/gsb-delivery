# 进货回单 PDF 栏位入仓（可见性判定）

业务只认交接页上实际还能看到的采购号。本目录给出完整流程：样例原件生成、
栏位清单、基于 HexaPDF 的可见性分析、按栏位交付的报告。

## 文件

- `generate_receipts.rb` — 生成样例批次 `receipts.pdf`（表格用 Form XObject
  套画，主表 `/FmTable` 再引用小表 `/FmCell`；同一模板在一页画两次、并跨页复用）。
- `fields.json` — 栏位清单：每个采购栏位的页码、矩形、行内阅读方向。
- `analyze_fields.rb` — 分析器，输出 `field_report.json` 与 `field_report.md`。
- `field_report.md` / `field_report.json` — 交付报告（以栏位为单位）。

## 运行

```sh
bundle exec ruby incoming_receipts/generate_receipts.rb   # 生成样例原件
bundle exec ruby incoming_receipts/analyze_fields.rb [receipts.pdf] [fields.json]
```

原件只读打开（`HexaPDF::Document.open`），不做任何写回。

## 判定方法

声明的绘制能力范围：水平字体、九十度倍数旋转、矩形裁剪、不透明填充
（无透明混合）。遇到范围外的能力时，按当时作用区域指出落在哪个栏位，
该栏位标记为需复核（`review: true`）。

1. 逐字盒取自 HexaPDF 现有解码换算结果
   （`HexaPDF::Content::Processor#decode_text_with_positioning`），在声明范围内
   每个字盒展开到页面上都是直角边界矩形。
2. 按绘制顺序重放页面（含嵌套 Form XObject），维护当前裁剪区域：
   - `q`/`Q` 保存恢复裁剪；`W`/`W*` 与当前路径矩形求交；
   - 每次 `Do` 调用表单时，先套用该次调用的 Matrix，再与该表单自身的
     BBox 求交——同一表单对象的每次引用独立结算，父表 `Q` 恢复的状态
     不会把内层裁剪带出来。
3. 字盒有效面积 = 字盒 ∩ 绘制时刻的裁剪区域；之后绘制的不透明填充
   （各自受当时裁剪限制）从中减去。字完整可读 ⇔ 整个字盒仍然可见。
   多个填充的并集共同遮盖也算（两个白框合起来盖满一个字即由此判定）；
   在文字之前画的填充不影响文字（签字数字前的白框即因此保持可读）。
4. 栏位判定：
   - 全部字完整可见 → `ok`，交付完整串；
   - 原文字被完全盖满、且更晚绘制的替代串完整可见 → `replaced`，
     交付替代值，可正常出仓；
   - 其余 → `partial`，逐字留下可核对的可见范围（页面坐标矩形），
     不从原文字对象补全。
5. 同一表单对象在不同页、不同位置的每次调用各自入账。

## 样例覆盖的场景

| 栏位 | 场景 | 结论 |
| --- | --- | --- |
| `p1_po_top` | 模板放在页顶，露全 | ok `PO-2026-0001` |
| `p1_cell_top` | 子表自身 BBox 切到第二个字 | partial（`7` 仅 23.9% 可见） |
| `p1_po_clipped` | 第二次放置靠外层裁剪只露半个字 | partial（`P` 50%） |
| `p1_cell_clipped` | 同一子表对象、同一页第二次调用 | partial（全被裁掉） |
| `p1_pair_covered` | 两个白框单独都盖不住、合起来正好盖满 `1` | partial（`AB▯2`） |
| `p1_signature` | 同位置白框画在签字数字之前 | ok `9102` |
| `p1_rewritten` | 数字被盖满、上方重写完整替代值 | replaced `NEW01`，可出仓 |
| `p2_po` / `p2_cell` | 同一模板对象换一页复用 | 各自入账 |
| `p2_note` | 图像 XObject（未声明能力）压栏位 | ok 但需复核，已指出栏位 |
