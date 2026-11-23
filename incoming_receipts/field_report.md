# 进货回单栏位可见性报告

- 原件: `receipts.pdf`（只读打开，未修改）
- 栏位清单: `fields.json`（10 个栏位）
- 方法: HexaPDF 解码换算后的逐字盒 ∩ 实际裁剪区域 − 之后的不透明填充；逐次 Form 调用独立结算

## p1_po_top — 可入仓

- 页码: 1；栏位矩形: [55.00, 780.00, 145.00, 800.00]；阅读方向: ltr
- 交付串: `PO-2026-0001`
- 表格调用: Do /FmTable (obj 3) @ page 1 contents offset 105
- 文字指令: Tj @ form /FmTable (obj 3) offset 55

## p1_cell_top — 不可完整读取

- 页码: 1；栏位矩形: [58.00, 762.00, 80.00, 780.00]；阅读方向: ltr
- 可见部分: `X▯`（□ 表示无法核实的字，未从原文字对象补全）
- 表格调用: Do /FmTable (obj 3) @ page 1 contents offset 105 → Do /FmCell (obj 2) @ form /FmTable (obj 3) offset 87
- 文字指令: Tj @ form /FmCell (obj 2) offset 46
- 逐字可见范围:
  - `X` full 100.0% 可见: [64.00, 766.75, 70.67, 778.31]
  - `7` partial 23.9% 可见: [70.67, 766.75, 72.00, 778.31]

## p1_po_clipped — 不可完整读取

- 页码: 1；栏位矩形: [55.00, 640.00, 145.00, 660.00]；阅读方向: ltr
- 可见部分: `▯▯▯▯▯▯▯▯▯▯▯▯`（□ 表示无法核实的字，未从原文字对象补全）
- 表格调用: Do /FmTable (obj 3) @ page 1 contents offset 159
- 文字指令: Tj @ form /FmTable (obj 3) offset 55
- 逐字可见范围:
  - `P` partial 50.0% 可见: [60.00, 641.30, 64.00, 655.17]
  - `O` hidden 0.0% 可见: （无可见部分）
  - `-` hidden 0.0% 可见: （无可见部分）
  - `2` hidden 0.0% 可见: （无可见部分）
  - `0` hidden 0.0% 可见: （无可见部分）
  - `2` hidden 0.0% 可见: （无可见部分）
  - `6` hidden 0.0% 可见: （无可见部分）
  - `-` hidden 0.0% 可见: （无可见部分）
  - `0` hidden 0.0% 可见: （无可见部分）
  - `0` hidden 0.0% 可见: （无可见部分）
  - `0` hidden 0.0% 可见: （无可见部分）
  - `1` hidden 0.0% 可见: （无可见部分）

## p1_cell_clipped — 不可完整读取

- 页码: 1；栏位矩形: [58.00, 622.00, 80.00, 640.00]；阅读方向: ltr
- 可见部分: `▯▯`（□ 表示无法核实的字，未从原文字对象补全）
- 表格调用: Do /FmTable (obj 3) @ page 1 contents offset 159 → Do /FmCell (obj 2) @ form /FmTable (obj 3) offset 87
- 文字指令: Tj @ form /FmCell (obj 2) offset 46
- 逐字可见范围:
  - `X` hidden 0.0% 可见: （无可见部分）
  - `7` hidden 0.0% 可见: （无可见部分）

## p1_pair_covered — 不可完整读取

- 页码: 1；栏位矩形: [45.00, 494.00, 90.00, 514.00]；阅读方向: ltr
- 可见部分: `AB▯2`（□ 表示无法核实的字，未从原文字对象补全）
- 表格调用: 页面直接内容
- 文字指令: Tj @ page 1 contents offset 194
- 逐字可见范围:
  - `A` full 100.0% 可见: [50.00, 497.30, 58.00, 511.17]
  - `B` full 100.0% 可见: [58.00, 497.30, 66.01, 511.17]
  - `1` hidden 0.0% 可见: （无可见部分）
  - `2` full 100.0% 可见: [72.68, 497.30, 79.35, 511.17]

## p1_signature — 可入仓

- 页码: 1；栏位矩形: [395.00, 695.00, 465.00, 720.00]；阅读方向: ltr
- 交付串: `9102`
- 表格调用: 页面直接内容
- 文字指令: Tj @ page 1 contents offset 70

## p1_rewritten — 可入仓（替代值）

- 页码: 1；栏位矩形: [45.00, 444.00, 95.00, 464.00]；阅读方向: ltr
- 交付串: `NEW01`
- 备注: original characters fully covered; complete replacement value painted on top is fully visible
- 表格调用: 页面直接内容
- 文字指令: Tj @ page 1 contents offset 368；Tj @ page 1 contents offset 295

## p2_po — 可入仓

- 页码: 2；栏位矩形: [55.00, 720.00, 145.00, 740.00]；阅读方向: ltr
- 交付串: `PO-2026-0001`
- 表格调用: Do /FmTable (obj 3) @ page 2 contents offset 31
- 文字指令: Tj @ form /FmTable (obj 3) offset 55

## p2_cell — 不可完整读取

- 页码: 2；栏位矩形: [58.00, 702.00, 80.00, 720.00]；阅读方向: ltr
- 可见部分: `X▯`（□ 表示无法核实的字，未从原文字对象补全）
- 表格调用: Do /FmTable (obj 3) @ page 2 contents offset 31 → Do /FmCell (obj 2) @ form /FmTable (obj 3) offset 87
- 文字指令: Tj @ form /FmCell (obj 2) offset 46
- 逐字可见范围:
  - `X` full 100.0% 可见: [64.00, 706.75, 70.67, 718.31]
  - `7` partial 23.9% 可见: [70.67, 706.75, 72.00, 718.31]

## p2_note — 可入仓；含未声明绘制能力，需复核

- 页码: 2；栏位矩形: [45.00, 595.00, 95.00, 615.00]；阅读方向: ltr
- 交付串: `NOTE`
- 表格调用: 页面直接内容
- 文字指令: Tj @ page 2 contents offset 66
- 未声明能力: image_xobject — image XObject Im1 painted（Do @ page 2 contents offset 99）
