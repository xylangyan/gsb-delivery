# 台架两卷 TProfile 合并交付

台架停机前存了两套 ROOT TProfile（卷一 `standA`、卷二 `standB`）。本目录给出
从旧存档 + 留存附表到新报告口径交付文件的完整管线。

## 运行

```bash
.venv/bin/python answers/make_inputs.py     # 重建输入材料（answers/data/）
.venv/bin/python answers/merge_profiles.py  # 合并、验证、交付（answers/out/）
```

`merge_profiles.py` 会自动：逐箱核准旧文件与附表 → 拆分跨界箱 → 重叠批去重
→ 按区间仿射复校准 → 合并 → 与地面真值（原始逐点记录复算）逐项比对 →
写出 `out/merged_profiles.root` 并用 uproot 读回验证 → 生成交档回单。

## 输入材料（answers/data/）

- `standA_profiles.root` / `standB_profiles.root`：两卷旧存档（旧校准、旧分箱；
  卷一 temp 的旧箱 [2,6) 跨两个目标区间；humid 两卷均缺逐箱 fBinSumw2）
- `appendix_calibration.csv`：各卷轴范围与旧校准（乘系数加固定量）
- `appendix_batches.csv`：分批分箱统计附表（旧校准下；humid 无 sumw2）
- `appendix_overlap.csv`：重叠批 B03 附表（两卷各自旧校准下的统计）
- `preview_standB_press.csv`：卷二 press 预览（均值 + 相同的显示误差）
- `ledger_equal_weight.csv`：台账，证实 humid 各批等权 1
- `new_report_spec.json`：新报告口径（区间、新校准、误差口径、超界箱口径）

## 交付物（answers/out/）

- `merged_profiles.root`：`temp`、`humid` 两份完整 profile（uproot 直接可读：
  两端超界箱、fBinEntries/有效条数、均值、kERRORMEAN 误差）
- `delivery_receipt.md`：交档回单（口径、核对记录、读回验证、press 阻塞项）

`press` 因卷二只有预览（均值可算、误差缺逐箱 Σw² 与 Σwy² 证据）未写入交付
文件，详见回单「阻塞项」。
