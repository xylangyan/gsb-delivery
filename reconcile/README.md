# 观测册日期核账

为旧测光数据补实际日期。每本观测册从头到尾只用一种机器 EDTF 设置
(历法 + 旧式日期语法开关),换设置只发生在两册之间。`reconcile.rb`
枚举整批设置分派,保留换设置次数最少且全局自洽的方案,并据以核账。

## 用法

```
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby reconcile/reconcile.rb REGISTRY.yaml
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby reconcile/reconcile.rb REGISTRY.yaml query 册ID
```

- 核账:给入仓值(所有最低切换方案确认同一实际日期的事件)、未定日事件的
  各方案取值、两份可解释原件的最低切换方案;无共同解释时列出互相卡住的
  抄录与底片登记。
- `query 册ID`:该册设置在全部最低切换方案中唯一时,按 EDTF 原读法
  列出各标签及其绝对日。

## 登记格式(REGISTRY.yaml)

- `settings`:机器可能用过的设置。`calendar` 取 `gregorian`(全程格里历)、
  `julian`(全程儒略历)、`italy`(按登记日 1582-10-15 由儒略历改格里历,
  也可换 `england` 或改革日的儒略日数);`legacy` 为旧式日期语法开关
  (如 `^julian` 后缀、集合内空格等旧式写法只在 `legacy: true` 时读得通)。
- `widen`:不精确记号(`?`/`~`)口径,向两侧放宽的天数,默认 0。
- `books`:观测册,按原来的先后排列。每册 `entries` 为
  `{ event: 原事件号, label: 原标签 }`;同一事件号把不同册抄的同一次
  测光连起来。
- `anchors`:已核到绝对日的观测,`事件号: YYYY-MM-DD`(外推格里历)。
- `plates`:底片登记。`developed: YYYY-MM-DD` 加 `offset: 天数` 表示
  显影日 = 观测日 + 天数;或用 `developed_event: 事件号` 关联另一事件。

## 口径

- 绝对日一律用外推格里历表示;儒略历标签按历法差折算
  (如儒略历 1582-10-05 = 格里历 1582-10-15)。
- 月份精度、许可日期列表(`[a, b]`)、未指定数字等标签接受任一相容的
  绝对日,不把月初直接当作事件日。
- 按改革日换历的设置下,落在被删去的日子(如 1582-10-05..14)上的标签
  读不通,该设置对整册即不可行。

## 文件

- `reconcile.rb` — 求解器。
- `registry.yaml` — 演示批:覆盖儒略/格里/改革日换历、旧式语法开关、
  同事件跨册互抄、1582 年 10 月标签、锚点、底片天数、月份与列表标签。
- `registry-conflict.yaml` — 冲突演示批:演示无共同解释时的冲突清单。
