# 巡检拼卷工具 (Volume Combine)

在 CombinePDF 之上实现的受控拼卷工具。它把两期（或更多期）巡检 PDF 按拼卷单
插成一卷，并保证每条内链、书签和已外发工单名都落到**自己那一期实物版本**的出页。

## 为什么不能直接用 `CombinePDF#<<`

CombinePDF 在解析阶段就会：

- 把所有 name tree 条目统一改名为 `CombinePDF_0000001` 之类（见
  `lib/combine_pdf/pdf_protected.rb` 的 `rebuild_names`），原始锚名（如 `读数`）
  在重开时已经丢失；
- 同名目的地合并后只指向同一页，无法表达“同一锚名在两期各指各的抽样页”；
- 页面整数页号、页对象、名字三种目标不会按各自源卷分别解读。

本工具因此：

1. 用只读的 `RawReader` 在原始字节上记录原卷真实结构（旧版目录字典 `/Dests`、
   新版嵌套 name tree、页对象/整数/名字三类链接、书签父子、外部 URI）；
2. 用 `StablePDF`（`CombinePDF::PDF` 子类，关掉 `rebuild_names` 自动改名）承载
   实际交付件；
3. 由 `Planner` 做唯一的落点决策，`Combiner` 只负责执行，`Auditor` 重开交付件
   逐条复核。

## 目录

- `lib/volume_combine/tiny_pdf.rb` — 最小 PDF 写出器，用于造两份很小的原卷，
  能精确制造旧版/新版目的地写法。
- `lib/volume_combine/fixtures.rb` — 两份原卷（A 用旧版目录字典，B 用新版
  嵌套 name tree；说明页在两卷文本与链接完全一致）。
- `lib/volume_combine/raw_reader.rb` — 原始字节只读扫描，保留真实锚名与视图。
- `lib/volume_combine/manifest.rb` — 拼卷单/收件单 schema。
- `lib/volume_combine/planner.rb` — 落点决策（同组优先 → 获准替代 → 报缺项）。
- `lib/volume_combine/stable_pdf.rb` — 关闭自动改名的 CombinePDF 子类。
- `lib/volume_combine/combiner.rb` — 深拷贝出页、重写链接、重建书签章节、
  显式生成输出 name tree。
- `lib/volume_combine/auditor.rb` — 用 CombinePDF 重开交付件并逐条核对。
- `lib/volume_combine/report.rb` — Markdown 表 + JSON 核对报告。
- `manifests/` — 主拼卷单与“去掉几页”的未来组合示例。
- `test/run_all.rb` — 无外部测试依赖的回归套件（40 条断言）。

## 使用

依赖通过仓库自带的 `.gsb/Gemfile` 提供（路径指向本仓库的 combine_pdf）。

```sh
# 1. 生成两份小原卷到 tmp/fixtures
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby volume/bin/volume_combine fixtures tmp/fixtures

# 2. 按拼卷单拼卷并核对
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby volume/bin/volume_combine build \
  volume/manifests/manifest.json \
  tmp/deliver/combined.pdf \
  tmp/deliver/report.md \
  tmp/deliver/report.json

# 3. 回归测试
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby volume/test/run_all.rb
```

退出码为 0 表示重开后没有落点不一致；`report.md` 列出每条内链、书签、外部
工单名的具体出页、原始指向和实际采用依据。

## 拼卷单 schema

`layout` 给出每个出页位置的原卷、原页（1 起）和组号；`allowed_substitutes`
是替代白名单；`work_orders` 是已外发、必须原样保留的工单名。

```json
{
  "sources": { "A": { "path": "tmp/fixtures/volume_a.pdf" } },
  "layout": [
    { "slot": 1, "source": "A", "page": 1, "group": 1 }
  ],
  "allowed_substitutes": [
    { "source": "A", "page": 3, "group": 3, "target_group": 1 }
  ],
  "work_orders": [
    { "name": "外发-读数A", "source": "A", "page": 3, "group": 1, "view": ["FitH", 700] }
  ]
}
```

## 落点规则

对从某组发出、指向 `(原卷, 原页)` 的引用：

1. **同组优先**：该页在本组有实际出现时，用本组出现；
2. **获准替代**：本组没有时，只有 `allowed_substitutes` 白名单允许，才跳到
   允许的另一组出现，并在报告里标注 `permit`；
3. **否则报缺项**：不允许跳到另一期同名页。缺项链接被置为不可导航，缺项
   书签从大纲移除，失活工单名不写入 name tree——全部进缺项表。

## 命名处理

- 已外发工单名在输出 name tree 中**原样保留**，各自落到收件单指定的卷/页/视图，
  不会被另一期同名入口占成别的页。
- 两期都存在的固定名（`读数`、`锚点甲`）属于内部冲突，按 `名称#原卷` 改写
  （如 `读数#A`、`读数#B`），各自指向本卷实物页；这两个固定名的冲突在报告
  “固定名冲突”表分别列出。
- 原链接的裁切位置和缩放请求（`Fit`/`FitH`/`XYZ` 等及坐标）照原写法保留。

## 校验方式

`Auditor` 真正用 `CombinePDF.load` 重开交付件，确认：

- 每条内链/书签解析到的出页号与 `Planner` 决策一致，视图一致；
- 外部 URI 原样保留；
- name tree 用 `RawReader` 直接读输出字节（绕开 CombinePDF 载入即改名的行为），
  核对工单名原样存在且落点正确；
- 缺项链接在交付件里确实不可导航。
