# CookieJar 判空探针可插位置核对报告

生成: `ruby -Ilib acceptance/run.rb`（时钟固定在 2026-01-01 00:00:00 UTC 起，每步推进 60s）

## 机制（原库行为）

- 探针 `CookieJar#empty?`（无参）走 `HashStore#each` 早停：按插入序遍历，清掉沿途过期条目，碰到第一个有效条目即返回 `false`；不更新访问时刻。
- 唯一副作用 = 提前清除过期条目。若某键在原回放清除它（read/gc/签收）之前被同键重录，落位由“覆盖旧位”变为“追加到队尾”，后续 read/签收 的交出顺序分歧。
- 若重录之前原回放已用 read/gc 清掉该键，则两边同为追加，分歧在签收点前消失（可插）。

## 小夹具（small_loop，3 轮，暴力全回放 + 直接核对交叉验证）

### 模板 `small_loop`（3 轮 × 9 步）

- 周期检测: 瞬态 μ=1，周期 λ=1，实际模拟 2 轮后指纹重复，其余轮次按相位继承
- 登记可插位置: slots 0..9（slot i = 第 i 步之前；slot 9 = 轮边界）

可插位置连续段（坐标 = 模板 / slot→原步骤 / 实际轮次）:
- [可插] 段1: round 0 slot 0（步骤#0 `recv(A@svc.test.example/p,max_age=150)` 之前） ～ round 0 slot 3（步骤#3 `recv(X@api.test.example/q,max_age=30)` 之前）
- [不可插] 段2: round 0 slot 4（步骤#4 `recv(A@svc.test.example/p,max_age=150)` 之前） ～ round 0 slot 4（步骤#4 `recv(A@svc.test.example/p,max_age=150)` 之前）
  - 最早可重放分歧: 模板`small_loop` slot 4（步骤#4 `recv(A@svc.test.example/p,max_age=150)` 之前） round 0 插入探针后，在 round 0 步骤#5 `read(http://svc.test.example/p)`（read）首次与旧走法不一致
  - 旧走法记录: [["A", "svc.test.example", "/p", 360], ["B", "svc.test.example", "/p", 360]]
  - 插探针后:  [["B", "svc.test.example", "/p", 360], ["A", "svc.test.example", "/p", 360]]
  - 受影响测试条目: A, B
- [可插] 段3: round 0 slot 5（步骤#5 `read(http://svc.test.example/p)` 之前） ～ round 2 slot 9（本轮末尾、轮边界）

探针记录（另行记录，不参与比对；周期内轮次按相位继承）:
- 模板`small_loop` slot 0（步骤#0 `recv(A@svc.test.example/p,max_age=150)` 之前） round 0: empty? => true，无清除
- 模板`small_loop` slot 1（步骤#1 `recv(B@svc.test.example/p,max_age=900)` 之前） round 0: empty? => false，无清除
- 模板`small_loop` slot 2（步骤#2 `read(http://svc.test.example/p)` 之前） round 0: empty? => false，无清除
- 模板`small_loop` slot 3（步骤#3 `recv(X@api.test.example/q,max_age=30)` 之前） round 0: empty? => false，无清除
- 模板`small_loop` slot 4（步骤#4 `recv(A@svc.test.example/p,max_age=150)` 之前） round 0: empty? => false，清掉过期条目 A@svc.test.example/p
- 模板`small_loop` slot 5（步骤#5 `read(http://svc.test.example/p)` 之前） round 0: empty? => false，无清除
- 模板`small_loop` slot 6（步骤#6 `del(X@api.test.example/q)` 之前） round 0: empty? => false，无清除
- 模板`small_loop` slot 7（步骤#7 `gc` 之前） round 0: empty? => false，无清除
- 模板`small_loop` slot 8（步骤#8 `round(签收)` 之前） round 0: empty? => false，无清除
- 模板`small_loop` slot 9（本轮末尾、轮边界） round 0: empty? => false，无清除
- 模板`small_loop` slot 0（步骤#0 `recv(A@svc.test.example/p,max_age=150)` 之前） round 1: empty? => false，无清除
- 模板`small_loop` slot 1（步骤#1 `recv(B@svc.test.example/p,max_age=900)` 之前） round 1: empty? => false，无清除
- 模板`small_loop` slot 2（步骤#2 `read(http://svc.test.example/p)` 之前） round 1: empty? => false，无清除
- 模板`small_loop` slot 3（步骤#3 `recv(X@api.test.example/q,max_age=30)` 之前） round 1: empty? => false，无清除
- 模板`small_loop` slot 4（步骤#4 `recv(A@svc.test.example/p,max_age=150)` 之前） round 1: empty? => false，无清除
- 模板`small_loop` slot 5（步骤#5 `read(http://svc.test.example/p)` 之前） round 1: empty? => false，无清除
- 模板`small_loop` slot 6（步骤#6 `del(X@api.test.example/q)` 之前） round 1: empty? => false，无清除
- 模板`small_loop` slot 7（步骤#7 `gc` 之前） round 1: empty? => false，无清除
- 模板`small_loop` slot 8（步骤#8 `round(签收)` 之前） round 1: empty? => false，无清除
- 模板`small_loop` slot 9（本轮末尾、轮边界） round 1: empty? => false，无清除

交叉验证: 直接核对结论与逐候选完整重放（30 候选 × 全部轮次）完全一致。

## 长验收（long_loop，120_000_000 轮压缩记录，直接核对）

### 模板 `long_loop`（120000000 轮 × 9 步）

- 周期检测: 瞬态 μ=1，周期 λ=1，实际模拟 2 轮后指纹重复，其余轮次按相位继承
- 登记可插位置: slots 0..9（slot i = 第 i 步之前；slot 9 = 轮边界）

可插位置连续段（坐标 = 模板 / slot→原步骤 / 实际轮次）:
- [可插] 段1: round 0 slot 0（步骤#0 `recv(N@svc.test.example/m,max_age=90)` 之前） ～ round 0 slot 2（步骤#2 `recv(O@api.test.example/o,max_age=500)` 之前）
- [不可插] 段2: round 0 slot 3（步骤#3 `recv(Q@beta.test.example/b,max_age=30)` 之前） ～ round 0 slot 4（步骤#4 `recv(N@svc.test.example/m,max_age=90)` 之前）
  - 最早可重放分歧: 模板`long_loop` slot 3（步骤#3 `recv(Q@beta.test.example/b,max_age=30)` 之前） round 0 插入探针后，在 round 0 步骤#5 `read(http://svc.test.example/m)`（read）首次与旧走法不一致
  - 旧走法记录: [["N", "svc.test.example", "/m", 360], ["P", "svc.test.example", "/m", 360]]
  - 插探针后:  [["P", "svc.test.example", "/m", 360], ["N", "svc.test.example", "/m", 360]]
  - 受影响测试条目: N, P
- [可插] 段3: round 0 slot 5（步骤#5 `read(http://svc.test.example/m)` 之前） ～ round 119999999 slot 9（本轮末尾、轮边界）

探针记录（另行记录，不参与比对；周期内轮次按相位继承）:
- 模板`long_loop` slot 0（步骤#0 `recv(N@svc.test.example/m,max_age=90)` 之前） round 0: empty? => true，无清除
- 模板`long_loop` slot 1（步骤#1 `recv(P@svc.test.example/m,max_age=1000)` 之前） round 0: empty? => false，无清除
- 模板`long_loop` slot 2（步骤#2 `recv(O@api.test.example/o,max_age=500)` 之前） round 0: empty? => false，无清除
- 模板`long_loop` slot 3（步骤#3 `recv(Q@beta.test.example/b,max_age=30)` 之前） round 0: empty? => false，清掉过期条目 N@svc.test.example/m
- 模板`long_loop` slot 4（步骤#4 `recv(N@svc.test.example/m,max_age=90)` 之前） round 0: empty? => false，清掉过期条目 N@svc.test.example/m
- 模板`long_loop` slot 5（步骤#5 `read(http://svc.test.example/m)` 之前） round 0: empty? => false，无清除
- 模板`long_loop` slot 6（步骤#6 `del(Q@beta.test.example/b)` 之前） round 0: empty? => false，无清除
- 模板`long_loop` slot 7（步骤#7 `gc` 之前） round 0: empty? => false，清掉过期条目 N@svc.test.example/m
- 模板`long_loop` slot 8（步骤#8 `round(签收)` 之前） round 0: empty? => false，无清除
- 模板`long_loop` slot 9（本轮末尾、轮边界） round 0: empty? => false，无清除
- 模板`long_loop` slot 0（步骤#0 `recv(N@svc.test.example/m,max_age=90)` 之前） round 1: empty? => false，无清除
- 模板`long_loop` slot 1（步骤#1 `recv(P@svc.test.example/m,max_age=1000)` 之前） round 1: empty? => false，无清除
- 模板`long_loop` slot 2（步骤#2 `recv(O@api.test.example/o,max_age=500)` 之前） round 1: empty? => false，无清除
- 模板`long_loop` slot 3（步骤#3 `recv(Q@beta.test.example/b,max_age=30)` 之前） round 1: empty? => false，无清除
- 模板`long_loop` slot 4（步骤#4 `recv(N@svc.test.example/m,max_age=90)` 之前） round 1: empty? => false，无清除
- 模板`long_loop` slot 5（步骤#5 `read(http://svc.test.example/m)` 之前） round 1: empty? => false，无清除
- 模板`long_loop` slot 6（步骤#6 `del(Q@beta.test.example/b)` 之前） round 1: empty? => false，无清除
- 模板`long_loop` slot 7（步骤#7 `gc` 之前） round 1: empty? => false，无清除
- 模板`long_loop` slot 8（步骤#8 `round(签收)` 之前） round 1: empty? => false，无清除
- 模板`long_loop` slot 9（本轮末尾、轮边界） round 1: empty? => false，无清除

