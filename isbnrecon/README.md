# isbnrecon — ISBN 补录束对账与工单复核

基于 `isbnlib` 的两侧（冻结旧目录 / 当日补录）书目对账工具。补录束可以横跨
上亿个主体、互相重叠，账册全程不逐号扫描、不按书号存账：

- 主体轴是 12 位 ISBN-13 主体（不含校验位），ISBN-10 束映射到 `978` 前缀；
- 校验位由 isbnlib 的 `check_digit10`/`check_digit13` 唯一确定，支持判定下沉到
  数位 DP 的叶子，按纸面放行的末位字符集合决定一个主体是否真正成书；
- 同一主体在同一侧被多束支持只算一本（字符集取并集）；
- 计数与最小书号由 `counting.py` 的数位 DP 给出，DP 状态数只与主体位数和校验和
  范围有关，与区间长度无关。

## 结果三态

每一侧对一个书目键的结果只有三种（`isbnrecon/results.py`）：

- `Segmented(reg_prefix, group, publisher)`：完整登记前缀、组号、出版者段；
  出版者段为空串仍是一个独立的分段结果；
- `UNSEGMENTED`（有凭据但未分段）：至少一束支持，但本表查不到组或规则；
- `ABSENT`（缺席）：该侧没有任何仍有效的支持束。

两侧都缺席的书不进当前账格，但仍可作为迁移的迁出/迁入端点。

## 模块

- `bundles.py`：`Bundle`（唯一编号、ISBN-10/13、等宽闭区间、末位字符集）与
  `BundleSet`。区间索引是主体轴上的线段树，点查询/区间相交为 `O(log n + k)`；
  撤回单束只动该编号并惰性重建，不重读其它束。
- `registrytable.py`：每侧独立、可整组替换规则的登记表，严格复刻
  `isbnlib._msk.msk` 的滑窗组号查找与「七位读数（含校验位、右侧补零）」规则匹配。
- `counting.py`：数位 DP。`tally_with_min` 单遍给出每个标签的精确计数和字典序
  最小 ISBN-13；`count_realized`/`min_realized` 是单种类便捷接口。
- `ledger.py`：`Ledger`、`SideView`、`Correction`、`MigrationReview`。主体轴只在
  束边界和登记组边界切分；规则边的校验位歧义每条规则至多两个主体，按单点处理，
  其余稳定区间整体走 DP。
- `drill.py`：题面换表演练的可运行复现。

## 工单复核

`Correction` 可在一张工单里混合：

- `withdraw_bundle(side, bundle_id)` / `replace_bundle(side, bundle)`；
- `replace_group_rules(reg_prefix, rules)` 换掉新表指定登记组的全部规则。

`Ledger.review(order)` 只在受影响轴区域上计算四视图（更正前旧/新、更正后旧/新）
分类，输出 `(旧结果, 新结果) -> (旧结果, 新结果)` 的迁移格，每格带计数和字典序
最小 ISBN-13。返回的 `before_grid`/`after_grid` 同样限定在受影响区域；需要全轴
账格时显式调用 `Ledger.grid()`。`MigrationReview.evidence(isbn13)` 可分别回查
该书在四个视图上仍有效的支持束、纸面号、末位字符集和登记结果。

## 演练

```
python -m isbnrecon.drill
```

输出与题面一致：`9789993075882` 从两侧未分段迁到（旧未分段，新缺席）；
`9789993075899` 从（旧空段，新 `75`）迁到两侧空段，新侧凭据是仍在的 ISBN-13 束；
原（旧空段，新 `75`）格更正后为空。

## 测试

```
python -m pytest tests -o addopts=""
```

（仓库自带 `setup.cfg` 的 `--cov` 插件未安装，故覆盖 `addopts`。）测试含与暴力
逐号账的多组随机对拍、2 亿主体区间的性能断言、979 独立性、ISBN-10 `X` 校验位、
重叠束去重和工单局部性检查。
