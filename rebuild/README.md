# 入库联调合成数据 · 两格重建引擎

## 现状与阻塞

本工作区是一份**纯净的上游 Faker 40.54.0 快照**（已逐文件核对：磁盘文件与 git 跟踪
完全一致、pyc 与源码字节码一致、无悬空 git 对象、无隐藏数据）。题目所需的具体夹具
（Faker 种子、地区权重、生成次序、两格起止/步长、可搭配登记、旧抽检、尾部检验值）位于
工作区之外的题目目录：

```
.../GSB1109-20261122-BAO-FAKER-PROJECT-01/prompt.txt
.../GSB1109-20261122-BAO-FAKER-PROJECT-01/attempt-1/
```

沙箱配置（`~/sandbox.sb`）与系统 TCC 均明确拒绝读取该目录，`dangerouslyDisableSandbox`
也无法绕过，因此无法取得真实数值。本目录提供的是一套**以本机 Faker 为执行内核**的
增量重建/查询引擎，一旦夹具可读即可直接灌入；同时用合成场景对题目描述的每个怪癖做了
逐条对账自检。

## 已在本机 Faker 上实证的关键语义

- `optional` 的概率换算为 `int(prob * 100)`，双精度截断：`0.29*100 -> 28`
  （`faker/proxy.py:439`）。
- 多地区下 `fake.optional.x` **取方法时**就完成地区选择并消耗一次 RNG；之后填别的项、
  再调用该方法，布尔抽签到调用时才发生。
- optional 即使最终返回 `None`，也照样消耗那次布尔抽签（该段并非没发生）。
- `fake.unique` 与 `fake.unique[locale]` 共用同一个 `_seen` 池
  （`faker/proxy.py:332`）；同一观察值可由不同重试次数得到，并改变后续 RNG/概率式走向。
- 多地区工厂共享同一个全局 `random.Random`，`Faker.seed(...)` 后完全确定可复现。

## 设计要点（对应题目要求）

- **真实内核**：所有生成都走真实 Faker API，不手工重写 unique/optional 逻辑；仅在
  Generator 的原始方法上套透明计数器统计 unique 重试次数（不改 RNG，自检与无计数参考
  实现逐值相等）。
- **懒搜索两格**：按两格原顺序枚举 `(A, B)`，用登记（registry）过滤可搭配组合，命中
  旧抽检 + 尾部检验值即计数，并保留字典序最早的一组，给出每行换算式、实际有效概率、
  重试次数与生成结果。
- **前缀共用**：`prime_prefix` 快照 RNG 状态 + unique 已用池并记忆化；`run_remaining`
  从快照续跑，自检证明续跑结果与整段直跑完全一致。跨预取绑定的切点会被显式拒绝。
- **可反复缩窄查询**：`QuerySession` 缓存已评估的格子对与前缀，缩小范围时不重复造长表；
  `save/load` 持久化，第一次建好的材料可接着查。
- **不预生成分支表**：只对实际访问到的格子对按需仿真，不存在“先列完所有两格/分支再造表”。
- **概率式**：固定档直接换算；门控档看上一行生成的整数选表式，上一行是 `None` 或无匹配
  整数时走默认式。
- **预取时序**：`Scenario.prefetch` 表达“先拿到 optional 方法、填另几项后再调用”。

## 文件

- `rebuild/engine.py`：数据结构、仿真器、查询会话、快照/持久化。
- `rebuild/cli.py`：从场景 JSON 加载并回答区间查询。
- `rebuild/example_scenario.json`：场景 JSON 范例（夹具到位后照此格式替换）。
- `rebuild/selftest.py`：合成场景 + 手写 Faker 参考实现的 11 项对账自检。

## 运行

```bash
.venv/bin/python -m rebuild.selftest
.venv/bin/python -m rebuild.cli rebuild/example_scenario.json --session rebuild/session.pkl
.venv/bin/python -m rebuild.cli rebuild/example_scenario.json --session rebuild/session.pkl \
    --range-a 2:4 --range-b 2:6
```

## 接入真实夹具

把题目目录中的种子、地区权重、行次序、两格定义、registry、旧抽检与尾部值按
`example_scenario.json` 的结构填入（行的 `expect` 与 tail 的 `expect` 填旧抽检/尾部检验
值），即可用 CLI 得到给定两格范围内的可配对数量与最早可复现组的逐行审计。
