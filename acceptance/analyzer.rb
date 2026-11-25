# 核对器。
#
# BruteForce  —— 小夹具用：每个候选位置各完整跑一遍原库，逐一比对卡点记录。
# DirectAnalyzer —— 长验收用：不为候选重放前序与长回放。
#   1) 无探针基线只跑一遍，轮边界做时间归一化指纹，首次重复即得
#      瞬态 μ 与周期 λ，上亿轮由此压缩确定；
#   2) 候选只从该位置快照出发续跑，逐卡点比对归一化记录；
#      探针-run 轮边界指纹与基线相同即判定“再汇合”，其后永远一致；
#   3) 周期内轮次结果按相位继承到全部轮次。
module Acceptance
  Divergence = Struct.new(:template, :round, :step, :step_label, :kind,
                          :baseline, :probe, :affected, keyword_init: true)

  module RecordUtil
    module_function

    # 归一化：payload 中的时刻减去本轮起始时刻，使跨轮可比。
    def normalize_record(kind, payload, round_start)
      case kind
      when :read
        [:read, payload.map { |n, d, p, t| [n, d, p, t - round_start.to_i] }]
      else
        [kind, payload]
      end
    end

    def affected_entries(kind, baseline_payload, probe_payload)
      names = ->(pl) {
        case kind
        when :read  then pl.map(&:first)
        when :round then pl.map(&:first)
        else []
        end
      }
      b = names.(baseline_payload)
      p = names.(probe_payload)
      changed = (b | p).select { |n| b.index(n) != p.index(n) }
      changed.empty? ? (b | p) : changed
    end
  end

  class BruteForce
    def initialize(archive)
      @archive = archive
    end

    def run
      baseline_records, = full_run(nil)
      results = {}
      probe_log = []
      @archive.templates.each_with_index do |tmpl, ti|
        tmpl.repeats.times do |round|
          tmpl.probe_slots.sort.each do |slot|
            records, probe_info = full_run({ ti: ti, round: round, slot: slot })
            probe_log << { ti: ti, slot: slot, round: round, **probe_info }
            div = first_diff(baseline_records, records, ti)
            results[[ti, slot, round]] = { insertable: div.nil?, divergence: div }
          end
        end
      end
      { results: results, probe_log: probe_log }
    end

    private

    def full_run(probe)
      FakeClock.time = @archive.t0
      engine = Engine.new
      records = []
      probe_info = nil
      @archive.templates.each_with_index do |tmpl, ti|
        tmpl.repeats.times do |round|
          tmpl.steps.each_with_index do |step, i|
            if probe && probe[:ti] == ti && probe[:round] == round && probe[:slot] == i
              probe_info = engine.probe_empty
            end
            kind, payload = engine.exec_step(step)
            records << [ti, round, i, kind, payload]
          end
          if probe && probe[:ti] == ti && probe[:round] == round && probe[:slot] == tmpl.steps.length
            probe_info = engine.probe_empty
          end
        end
      end
      [records, probe_info]
    end

    def first_diff(baseline, probe, ti)
      baseline.zip(probe) do |b, p|
        next if b == p
        tmpl = @archive.templates[b[0]]
        return Divergence.new(
          template: tmpl.id, round: b[1], step: b[2],
          step_label: tmpl.steps[b[2]].label, kind: b[3],
          baseline: b[4], probe: p[4],
          affected: RecordUtil.affected_entries(b[3], b[4], p[4]),
        )
      end
      nil
    end
  end

  class DirectAnalyzer
    MAX_SIM_ROUNDS = 100_000

    def initialize(archive)
      @archive = archive
    end

    def run
      FakeClock.time = @archive.t0
      engine = Engine.new
      @archive.templates.each_with_index.map do |tmpl, ti|
        info = analyze_template(engine, tmpl, ti)
        info
      end
    end

    private

    def analyze_template(engine, tmpl, ti)
      slots = tmpl.probe_slots.sort

      # ---- 基线单遍 + 周期检测 ----
      seen = {}
      boundary_fps = []   # boundary_fps[r] = 第 r 轮 轮开始前的指纹
      round_records = []  # 每轮归一化卡点记录
      round_starts = []   # 每轮起始时刻（探针-run 归一化要用同一基准）
      slot_snaps = []     # 每轮各登记 slot 的快照
      mu = lam = nil
      r = 0
      while r < tmpl.repeats
        f = engine.fingerprint
        if seen.key?(f)
          mu = seen[f]
          lam = r - mu
          break
        end
        raise "模板 #{tmpl.id} 超过 #{MAX_SIM_ROUNDS} 轮未现周期" if r >= MAX_SIM_ROUNDS
        seen[f] = r
        boundary_fps << f
        round_starts << FakeClock.time
        round_start = FakeClock.time
        snaps = {}
        recs = []
        tmpl.steps.each_with_index do |step, i|
          snaps[i] = engine.snapshot if slots.include?(i)
          recs << RecordUtil.normalize_record(*engine.exec_step(step), round_start)
        end
        snaps[tmpl.steps.length] = engine.snapshot if slots.include?(tmpl.steps.length)
        slot_snaps << snaps
        round_records << recs
        r += 1
      end
      simulated = r
      mu ||= 0
      lam ||= simulated
      template_exit = engine.snapshot

      phase = ->(x) { x < simulated ? x : mu + (x - mu) % lam }
      boundary_fp_for = ->(x) { boundary_fps[phase.(x)] }
      round_duration = tmpl.steps.sum(&:dt)
      round_start_for = ->(x) {
        p = phase.(x)
        round_starts[p] + (x - p) * round_duration
      }

      # ---- 候选核对：只从快照续跑到再汇合或首个分歧 ----
      verdicts = {}
      probe_log = []
      simulated.times do |round|
        slots.each do |slot|
          engine.restore(slot_snaps[round][slot])
          probe_info = engine.probe_empty
          probe_log << { ti: ti, slot: slot, round: round, **probe_info }

          divergence = nil
          converged = false
          rr = round
          from_step = slot
          loop do
            round_start = round_start_for.(rr)
            tmpl.steps.each_with_index do |step, i|
              next if i < from_step
              kind, payload = engine.exec_step(step)
              got = RecordUtil.normalize_record(kind, payload, round_start)
              want = round_records[phase.(rr)][i]
              next if got == want
              divergence = Divergence.new(
                template: tmpl.id, round: rr, step: i,
                step_label: tmpl.steps[i].label, kind: kind,
                baseline: want[1], probe: got[1],
                affected: RecordUtil.affected_entries(kind, want[1], got[1]),
              )
              break
            end
            break if divergence
            rr += 1
            break if rr >= tmpl.repeats
            converged = engine.fingerprint == boundary_fp_for.(rr)
            break if converged
            from_step = 0
            if rr > round + mu + 2 * lam + 2
              raise "候选(#{tmpl.id}, slot #{slot}, round #{round})未收尾未汇合也未分歧"
            end
          end

          verdicts[[slot, round]] = {
            insertable: divergence.nil?,
            divergence: divergence,
          }
        end
      end
      engine.restore(template_exit)

      {
        template: tmpl, ti: ti, mu: mu, lam: lam, simulated: simulated,
        slots: slots, verdicts: verdicts, probe_log: probe_log,
        phase: phase,
      }
    end
  end

  # 把候选结果整理成连续段（按回放时序：轮次为主、slot 为辅）。
  module Segments
    module_function

    def build(tinfo)
      tmpl = tinfo[:template]
      slots = tinfo[:slots]
      mu = tinfo[:mu]
      lam = tinfo[:lam]
      repeats = tmpl.repeats
      verdict_at = ->(slot, round) {
        phi = round < tinfo[:simulated] ? round : mu + (round - mu) % lam
        tinfo[:verdicts][[slot, phi]]
      }

      runs = []
      merge_or_push = lambda do |insertable, from, to|
        last = runs.last
        if last && last[:insertable] == insertable
          last[:to] = to
        else
          runs << { insertable: insertable, from: from, to: to }
        end
      end

      explicit = [tinfo[:simulated], repeats].min
      explicit.times do |round|
        slots.each do |slot|
          merge_or_push.(verdict_at.(slot, round)[:insertable], [slot, round], [slot, round])
        end
      end

      tail_note = nil
      if repeats > explicit
        # 尾部按周期重复：检查一个周期块是否全同
        block_ins = (mu...(mu + lam)).flat_map { |round|
          slots.map { |slot| verdict_at.(slot, round)[:insertable] }
        }.uniq
        if block_ins.size == 1
          merge_or_push.(block_ins[0], [slots.first, explicit], [slots.last, repeats - 1])
        else
          tail_note = "尾部（round #{explicit}..#{repeats - 1}）按周期 λ=#{lam} 重复，" \
                      "各相位轮候选结果与 round #{mu}..#{mu + lam - 1} 相同"
        end
      end

      { runs: runs, tail_note: tail_note, verdict_at: verdict_at }
    end
  end
end
