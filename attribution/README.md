# 地块归档面积归因（固定 PyPNG 0.0.21，可复跑）

两份下载的 8×2 索引掩码字节完全相同，下游报出完全相同的面积；
值班员留下甲、乙两种事故说法。本目录给出可复跑的归因，不改库
（`code/png.py` 未动），全部通过仓库自带 `.venv` 里固定的
PyPNG 0.0.21 公开 API（`png.Reader` / `png.Writer`）完成。

## 复跑命令

```sh
cd attribution
../.venv/bin/python make_samples.py                 # 生成小样本到 out/
../.venv/bin/python attribution.py selftest         # 全链路断言
../.venv/bin/python attribution.py receipt  out/parcel_jia_download.png
../.venv/bin/python attribution.py receipt  out/old_archive_ratio_only.png
../.venv/bin/python attribution.py compare  out/parcel_jia_download.png out/parcel_yi_download.png
../.venv/bin/python attribution.py replay   out/parcel_jia_download.png --scenario jia
../.venv/bin/python attribution.py replay   out/parcel_yi_download.png --scenario yi
../.venv/bin/python attribution.py adjudicate out/parcel_yi_download.png evidence/collection_list.txt
../.venv/bin/python attribution.py adjudicate out/parcel_yi_download.png evidence/field_review.txt
```

## 样本

- `out/parcel_jia_download.png`、`out/parcel_yi_download.png`：8×2、8 bit
  索引色。索引 7 占 8 位、索引 3 占 2 位、正常类索引 1 占 6 位。
  PLTE 第 7 项 alpha=0（PyPNG 据此写出 tRNS，声明 7 透明）；
  pHYs = 100×200 px/米。两文件 sha256 相同。
- `out/old_archive_ratio_only.png`：同一掩码，pHYs 100×200 但
  unit=0 —— 只能给出像素比例的旧归档，缺米声明。

## 公开回执核对（receipt）

PyPNG 回执确认：索引分布 7×8 / 3×2 / 1×6；`Reader.palette()` 合成
PLTE+tRNS 后仅索引 7 alpha=0；pHYs = 100×200 px/unit、unit=meter。
下游口径（非透明即有效）：有效 8 像素，面积 8/(100·200) = 0.0004 m²，
两份下载结果逐项相同。旧归档 unit=0 时回执拒绝给面积：
缺米声明的图不靠比例硬算。

## 两种因果口径重放（replay，文件观测不变）

- 甲：完整重采样同一块地，真实网格 200×200 px/米，旧 pHYs 被复制，
  7 确实透明。有效 8 像素，真实面积 8/(200·200) = 0.0002 m²。
  下游没有误判任何位置，错在过时 pHYs 的横向比例，面积被高估 2 倍。
- 乙：另一块同样本网格的区域，pHYs 100×200 正确；空类本是索引 3，
  误配成索引 7，而 7 在乙是有效类。有效 14 像素，真实面积
  14/(100·200) = 0.0007 m²。被误判的位置是 8 个索引 7 像素
  (0..3, 0..1)，下游面积被低估。

三个面积（0.0004 / 0.0002 / 0.0007 m²）互不相同；任何口径得出的
面积都不能强加给另一口径。

## 不可判定性与外部证据（compare / adjudicate）

两文件字节相同 ⇒ 直方图、tRNS、pHYs、下游面积全部相同；只凭字节
和最终结果无法在甲、乙之间取舍，必须取外部证据。

- 采集清单（`evidence/collection_list.txt`，只说两个物理边界
  0.08 m × 0.01 m）：推出真实网格 100×200 px/米 ⇒ 排除甲的
  “旧 pHYs 被复制”几何（200×200），与乙一致。仍缺：索引 7 是否
  真空——边界不含类语义，有效像素数（8 还是 14）与面积仍未定。
- 现场复核（`evidence/field_review.txt`，只说两个具体点：
  (0,0) 索引 7 复核为有效，(7,0) 索引 3 复核为无效）：排除甲的
  “7 确实透明”，与乙的误配说法一致。仍缺：真实米制比例——点的
  有效性不含 px/m，面积量级仍未定。
- 旧归档只给 pHYs 比例 1:2（unit=0）：甲复制旧 pHYs 后比例必然
  相同，乙如实写 100×200 比例也相同 ⇒ 什么也排除不了；且缺米
  声明，不能靠比例硬算面积。

任取一种证据只能钉死一个轴（几何或类语义），另一轴仍缺；两种
证据齐备才能唯一确定乙：14 有效像素 × 1/(100·200) m² = 0.0007 m²。
