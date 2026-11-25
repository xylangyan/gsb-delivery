# 发单规划结果

- 结论：可行
- 真正送出的修订数：3
- 提交次数：1

## 写入清单与提交边界
1. `APPROVAL`：`R12`, `R18`, `R19`

## 只留档修订

- `R7`：record_entry_rejects_write；PTypeError: Invalid type for field StockTicket.location_no, was str
- `R8`：covered_by_later_sent_write；location_no is later delivered by R12

## 阻断项

- 无

## 签批点核对

- [OK] `APPROVAL` / `location_no`：revision=R12, raw_type=int, missing=False
- [OK] `APPROVAL` / `loc1`：revision=R18, raw_type=int, missing=False
- [OK] `APPROVAL` / `loc2`：revision=R19, raw_type=int, missing=False
- [OK] `APPROVAL` / `scale_cm`：revision=None, raw_type=str, missing=False
