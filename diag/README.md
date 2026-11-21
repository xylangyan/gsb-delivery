# 10:05 未启动事件复盘（rufus-scheduler 3.9.2 固定版本）

可跑样例：`incident_1005.rb`。只用 `prepend` 挂观测点（触发决策、`next_time`
变化、work_queue 真实进出、mutex 等待/取得/释放、处理函数 START/END 回执），
不改动 `lib/` 任何文件，调度行为保持原样。

运行：

```
BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby diag/incident_1005.rb
```

产出三份记录（逻辑时钟：首轮 = 10:00，窗口间隔 5 分钟映射为 5 秒）：

- `out/record_A_blocked_inside_handler.log` — 首轮阻在处理函数内
- `out/record_B_stuck_in_work_queue.log` — 首轮尚未被 worker 取出
- `out/record_C_stuck_on_mutex.log` — 首轮已出队但在等 mutex

三份记录里 incident 作业（`every '5s', overlap: false`）都**没有 10:05 的
START 回执**，但成因分属三个不同阶段。

## 三个阶段的判定

**A — 从未入队（值班员说的"禁重叠正常跳过"）**
10:05 的 trigger-decision 行显示 `running?=true overlap=false => SKIP
(never enqueued)`，随后**没有**对应的 work-queue IN 行；`next-time` 从
10:05 推进到 10:10。松开后 10:07 首轮 END，下一次执行就是 10:10 窗口，
10:05 的工作**彻底丢失，无积压**。

**B — 已在队列中，worker 尚未取**
另一个同标签作业占着唯一的 worker（`max_work_threads: 1`，
`active_workers=1`）。10:00 与 10:05 各有一条 IN 回执（size=1 → size=2），
**没有 OUT**。注意 10:05 的决策是 `running?=false => ACCEPT`：首轮还没
"运行"，`overlap: false` 不拦，照样入队。松开后 worker 依次 OUT 两条，
10:00 和 10:05 两份工作在 10:07 **背靠背补跑**（积压属实），10:10 窗口
恢复正常。

**C — 已出队但仍未调用（等 mutex）**
10:00 的 IN 之后立刻有 OUT（worker w10 取走），但接着是 `mutex WAIT`，
**没有 ACQUIRED、没有 START**——作业停在 `trigger_now` 之前。此刻队列
size=0、`running?=false`，表面上"一切空闲"。10:05 决策仍是
`running?=false => ACCEPT`：第二份拷贝入队、被 w11 取走、同样停在
`mutex WAIT`。松开持锁的同标签作业后，w10、w11 依次 ACQUIRED → START →
END → RELEASED，两份积压被 mutex 串行补跑，10:10 窗口正常。

## 为什么不能只看 work_queue 尺寸或 running?

- `running?` 只统计已进入 `trigger_now` 的线程（`jobs_core.rb` 的
  `Job#threads` 按 `t[:rufus_scheduler_job] == self` 过滤）。B、C 里首轮
 都没走到那一步，`running?` 都是 false——和"首轮不存在"无法区分。
- work_queue 尺寸在 A 和 C 里都是 0（C 被等 mutex 的 worker 取空了），
  单凭尺寸会把 C 误判成 A 的"禁重叠跳过"。B 里尺寸 ≥1 也只说明"有东西
  没消费"，说明不了是 worker 被同标签作业占住。
- 只有三联回执能区分：**触发决策时的 running?**（A 为 true，B/C 为
  false）、**IN/OUT 配对**（A 无 IN；B 有 IN 无 OUT；C IN/OUT 都有）、
  **mutex WAIT 是否挂起**（仅 C）。

## interval 与 every 的排期差异（record A 的对照组）

- `interval`：运行期间 `next_time` 被置为 `false`，只在 `post_trigger`
  （处理函数**实际结束**后）重排为 `结束时刻 + interval`。首轮 10:07 结束，
  下一窗落在 10:12——根本不存在 10:05 这个预约，整体顺延。
- `every`：`trigger` 先 `set_next_time` 再 `do_trigger`，所以即使 10:05
  被 `overlap: false` 跳过，`next_time` 也已从 10:05 推进到 10:10；被跳过
  的预约不补偿、不补跑。
- 对照组 `ref-every`（允许重叠）在 10:05 正常 START——若事故作业是这个
  配置，日志里一定会有 10:05 的启动记录，可据此排除。

## mutex 边界（可复跑的同步轨迹）

`overlap: false` 的判定只看 `running?`，而 `running?` 要等 worker 进入
`trigger_now` 才成立。作业被出队、正在 `mutex(m).synchronize` 上等待的这段
窗口里它是"不可见"的，后续每个窗口都会再排一份进来。record C 就是这条边界
的同步轨迹，可直接复跑看到：

```
10:00:00  IN  incident(10:00) → OUT(w10) → mutex WAIT(w10)   # 首轮已出队未调用
10:00:05  decision running?=false => ACCEPT                  # 禁重叠失效
          IN  incident(10:05) → OUT(w11) → mutex WAIT(w11)   # 第二份同样卡住
10:00:07  持锁者 RELEASED → w10 ACQUIRED → START → END → RELEASED
                        → w11 ACQUIRED → START → END → RELEASED  # 积压串行补跑
10:00:10  正常窗口 IN → OUT → ACQUIRED → START → END
```

若阻塞更久，10:10、10:15… 每窗都会再叠一份（`trigger_queue` 看到无空闲
worker 还会不断起新线程），积压随阻塞时长线性增长。
