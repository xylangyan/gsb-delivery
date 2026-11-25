# 排班回执对账器

工作区初始内容只有 IceCube 仓库，没有题面所述的真实档案或具体查询窗口。`reconcile/shift_reconciliation.rb` 提供可复用的任意窗口对账器；`reconcile/small_log.rb` 是覆盖题面特殊条件的小日志。

## 运行

```bash
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec bin/reconcile demo
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec bin/reconcile check examples/archive.example.json
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec bin/reconcile window \
  examples/archive.example.json 2026-12-01T08:55:00Z 2026-12-01T09:05:00Z --verify
```

`window --json` 默认只输出压缩区间；加 `--pairs` 才展开逐条配对和完整不可避免缺采列表。

窗口会反复切换时，只加载一次档案：

```bash
printf '%s\n' \
  '{"start":"2026-12-01T08:55:00Z","end":"2026-12-01T09:05:00Z"}' \
  '{"start":"2026-12-02T00:35:00Z","end":"2026-12-02T00:45:00Z"}' |
  BUNDLE_GEMFILE=.gsb/Gemfile bundle exec bin/reconcile query examples/archive.example.json
```

## 档案格式

示例见 `examples/archive.example.json`。

- `horizon.start`、`horizon.end`：全档对账范围；非终止 IceCube 规则必须给右端。
- `schedules[].hash`：IceCube `Schedule#to_hash` 的 JSON 形式。
- `schedules[].yaml` 或 `ical`：也支持 IceCube 原生 YAML/iCalendar。
- `extra_times`：零星加开。
- `exception_times`：取消项。
- 普通回执用 `id`、`at`、`schedule`、`early`、`late`。
- `early` 表示回执最多早到多少秒；`late` 表示最多晚到多少秒。也可给对称的 `tolerance`。
- 成段回执用 `start`、`step`、`count`、`gaps`、`id_prefix`/`id_template`，不展开成逐秒表。
- `hard_matches` 用 `receipt` 加 `schedule` + `shift_index`，或直接给 `shift`。

同一 `id` 的多条回执按重发合并；不同 `id` 即使同秒也是不同回执。班次 ID 为 `日程编号#IceCube班次序`，因此不同日程同秒启动仍是不同班次。同一日程内多条规则落在同一时刻时，直接使用 IceCube `occurrences_between` 的去重结果。

## 结果含义

设全档硬配对数为 `H`，删除硬配对端点后的自由二分图最大匹配数为 `K`。

- 全档最多可认下回执：`H + K`。
- 窗口内班次总数为 `N`，其中硬配对班次为 `H_W`。
- 先在窗口内自由班次上求最大匹配，再用增广路扩展为全档 `K` 个匹配，得到窗口内最多可认下班次数 `H_W + r(W)`，所以最少补采为 `N - H_W - r(W)`。
- 先在窗口外自由班次上求秩 `r(\\overline W)`，再扩展到 `K`，得到窗口内最少可认下班次数 `H_W + K - r(\\overline W)`，所以最多补采为 `N - H_W - K + r(\\overline W)`。

两次分段匹配分别返回达到最少、最多补采的完整全档配对。长配对会压缩成原回执号区间/段序号区间和 `日程#首班..#末班`。

删除硬配对端点后，一个自由班次若至少有一条可用边，就能从任意最大匹配中直接替换相邻回执的原配对，因此它总能出现在某个全档最优对账中。全档最优里都不可避免的缺采，恰好是没有任何可用自由回执边的班次；取其时间最早者即可。

## 复杂度

处理过程只保存稀疏班次、去重回执和候选边，不构造逐秒配对网格。候选边通过各日程时间数组上的二分查找生成。Hopcroft–Karp 的复杂度为 `O(E sqrt(V))`；每个窗口额外执行两次分段最大匹配。

## 小日志校对结果

内置小日志当前生成 30 个班次、27 张去重回执（另有 1 条同号重发）和 1 个硬配对；全档最优认下 28 个配对、缺 2 次。最早不可避免缺采是 `RUN#8 @ 2026-11-22T01:10:00Z`。

上午窄窗口虽然有两张回执都能碰到上午班，但扩展回全档后，结果固定为 `R-AM-ONLY -> DAY#1`、`R-BOTH -> DAY#2`。`SWAP#1` 的窄窗口可演示同一 `R-SWAP` 在两个全档最优配对中分别认下 `SWAP#1` 或 `SWAP#2`，对应窗口补采 0 或 1。

验证：

```bash
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec rspec spec/reconcile_spec.rb
```
