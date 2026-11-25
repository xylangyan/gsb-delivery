# migration — MessagePack 扩展号移交排序工具

根据移交单（handover sheet，JSON）计算各采集端从旧扩展表到终表的可执行
切换顺序：**优先少借扩展号，同数时少实际切换**。每一步的在途 + 新写入
混批在所有读端都经过符号化校验（不把模板按字段值展成全量样本），保证
键、值和原业务类型都对。

## 用法

```sh
python -m migration plan examples/handover_demo.json [--json]
```

- 成功：打印每一步（切换的表、需启停的处理器、等待的收件点、清完的
  旧段、归还的临时号）。
- 失败：打印最早卡住的状态，以及该处每一候选动作被挡下的原因——
  缺哪组许可（读端 + 扩展号），或相撞见证（生成真实报文的原字段
  选择、读端、相撞键）。

## 移交单格式要点

- `segments.<名>.template.fields`：字段域，`int` 给 `[lo, hi]`（可非常
  大）、`bool`、`timestamp`（继续认 datetime 原类型与微秒精度）。
- `template.permitted`：许可组合，`{"fields": [...], "allow": [[...]]}`。
- 顶层 `shared`：跨段共用同一原值的字段组，如 `[["a.id", "b.site"]]`。
- `tables.old / tables.final / borrowable.*`：各表的 `ext_code`、嵌套
  数组 `key`（元素为 `{"const": ...}` 或 `{"field": ..., "rules": [...]}`）
  和 `value`（带声明业务类型 `type`，加载时校验与转换规则推出的类型
  一致）。
- 转换规则 `rules`：`lookup`（查表）、`sign_extend`（符号扩展）、
  `scale`（整数缩放），按顺序作用于字段原值。
- `readers`：`ext_handlers.initial / may_toggle`（准许启停的处理器）
  与 `registered`（命中已注册类型）；按 umsgpack 行为，传入的
  `ext_handlers` 优先于已注册类型。

## 模型假设

- 每次只动一张表；收件点回执可随行。旧表退役后在指定收件点全部回执
  之前仍在途；临时号随清完归还。
- 合单按帧分别解码后合并：单帧内的重复键由 umsgpack 的
  `DuplicateKeyException` 拦截，跨帧合并时 `True == 1` 等等价键会
  静默只剩一项——两者都算故障，校验覆盖。
- 键为嵌套数组，解码后是嵌套 tuple；不同版本的两个扩展号即使各自
  能正确读回业务值，只要键 tuple 可能相同即判该混批非法。
