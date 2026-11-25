#!/usr/bin/env ruby
# 判空探针可插位置核对入口。
# 用法: ruby -Ilib acceptance/run.rb   （在仓库根目录执行）
require 'uri'
require 'http/cookie'
require_relative 'fake_clock'
require_relative 'archive'
require_relative 'engine'
require_relative 'analyzer'

FakeClock.install!

module Acceptance
  module Report
    module_function

    def slot_desc(tmpl, slot)
      if slot >= tmpl.steps.length
        "slot #{slot}（本轮末尾、轮边界）"
      else
        "slot #{slot}（步骤##{slot} `#{tmpl.steps[slot].label}` 之前）"
      end
    end

    def coord(tmpl, slot, round)
      "模板`#{tmpl.id}` #{slot_desc(tmpl, slot)} round #{round}"
    end

    def divergence_lines(tinfo, run)
      tmpl = tinfo[:template]
      slots = tinfo[:slots]
      # 收集段内候选（非可插段都在显式轮次内；周期尾部若有非可插按相位代表+轮次平移）
      cands = []
      from, to = run[:from], run[:to]
      (from[1]..to[1]).each do |round|
        slots.each do |slot|
          next if round == from[1] && slot < from[0]
          next if round == to[1] && slot > to[0]
          phi = round < tinfo[:simulated] ? round : tinfo[:mu] + (round - tinfo[:mu]) % tinfo[:lam]
          v = tinfo[:verdicts][[slot, phi]]
          next if v[:insertable]
          div = v[:divergence]
          abs_round = div.round + (round - phi)
          cands << [abs_round, div.step, slot, round, div]
        end
      end
      return ['  （无候选？请检查段构造）'] if cands.empty?
      cands.sort_by! { |ar, st, _, _, _| [ar, st] }
      abs_round, _step, slot, round, div = cands.first
      affected = div.affected.join(', ')
      [
        "  - 最早可重放分歧: #{coord(tmpl, slot, round)} 插入探针后，" \
        "在 round #{abs_round} 步骤##{div.step} `#{div.step_label}`（#{div.kind}）首次与旧走法不一致",
        "  - 旧走法记录: #{div.baseline.inspect}",
        "  - 插探针后:  #{div.probe.inspect}",
        "  - 受影响测试条目: #{affected}",
      ]
    end

    def emit_template(lines, tinfo, segs)
      tmpl = tinfo[:template]
      lines << "### 模板 `#{tmpl.id}`（#{tmpl.repeats} 轮 × #{tmpl.steps.length} 步）"
      lines << ''
      lines << "- 周期检测: 瞬态 μ=#{tinfo[:mu]}，周期 λ=#{tinfo[:lam]}，" \
               "实际模拟 #{tinfo[:simulated]} 轮后指纹重复，其余轮次按相位继承"
      lines << "- 登记可插位置: slots #{tinfo[:slots].first}..#{tinfo[:slots].last}" \
               "（slot i = 第 i 步之前；slot #{tmpl.steps.length} = 轮边界）"
      lines << ''
      lines << '可插位置连续段（坐标 = 模板 / slot→原步骤 / 实际轮次）:'
      segs[:runs].each_with_index do |run, i|
        from_s = slot_desc(tmpl, run[:from][0])
        to_s = slot_desc(tmpl, run[:to][0])
        n = nil
        mark = run[:insertable] ? '可插' : '不可插'
        lines << "- [#{mark}] 段#{i + 1}: round #{run[:from][1]} #{from_s} ～ " \
                 "round #{run[:to][1]} #{to_s}"
        unless run[:insertable]
          divergence_lines(tinfo, run).each { |l| lines << l }
        end
      end
      lines << "- 备注: #{segs[:tail_note]}" if segs[:tail_note]
      lines << ''
      lines << '探针记录（另行记录，不参与比对；周期内轮次按相位继承）:'
      tinfo[:probe_log].each do |e|
        purged = e[:purged].empty? ? '无清除' : "清掉过期条目 #{e[:purged].join(', ')}"
        lines << "- #{coord(tmpl, e[:slot], e[:round])}: empty? => #{e[:result]}，#{purged}"
      end
      lines << ''
    end
  end
end

small = Acceptance::Fixtures.small
long  = Acceptance::Fixtures.long

puts '== 小夹具: 暴力逐候选全回放核对 =='
t0 = Process.clock_gettime(Process::CLOCK_MONOTONIC)
brute = Acceptance::BruteForce.new(small).run
puts format('   暴力核对完成 %.2fs', Process.clock_gettime(Process::CLOCK_MONOTONIC) - t0)

puts '== 小夹具: 直接核对（周期检测）交叉验证 =='
small_direct = Acceptance::DirectAnalyzer.new(small).run

mismatch = []
small_direct.each do |tinfo|
  tinfo[:verdicts].each do |(slot, round), v|
    b = brute[:results][[tinfo[:ti], slot, round]]
    if b[:insertable] != v[:insertable]
      mismatch << "verdict @(slot #{slot}, round #{round}): brute=#{b[:insertable]} direct=#{v[:insertable]}"
    elsif !v[:insertable]
      bd = b[:divergence]
      dd = v[:divergence]
      if [bd.round, bd.step] != [dd.round, dd.step]
        mismatch << "divergence @(slot #{slot}, round #{round}): " \
                    "brute=(r#{bd.round},s#{bd.step}) direct=(r#{dd.round},s#{dd.step})"
      end
    end
  end
end
if mismatch.empty?
  puts '   交叉验证通过: 直接核对与暴力全回放结论完全一致'
else
  puts '   交叉验证失败:'
  mismatch.each { |m| puts "   - #{m}" }
  exit 1
end

puts '== 长验收: 直接核对（1.2 亿轮压缩，不展开） =='
t0 = Process.clock_gettime(Process::CLOCK_MONOTONIC)
long_direct = Acceptance::DirectAnalyzer.new(long).run
puts format('   直接核对完成 %.2fs', Process.clock_gettime(Process::CLOCK_MONOTONIC) - t0)

lines = []
lines << '# CookieJar 判空探针可插位置核对报告'
lines << ''
lines << "生成: `ruby -Ilib acceptance/run.rb`（时钟固定在 #{Time.at(Acceptance::Fixtures::T0).utc} 起，每步推进 60s）"
lines << ''
lines << '## 机制（原库行为）'
lines << ''
lines << '- 探针 `CookieJar#empty?`（无参）走 `HashStore#each` 早停：按插入序遍历，' \
         '清掉沿途过期条目，碰到第一个有效条目即返回 `false`；不更新访问时刻。'
lines << '- 唯一副作用 = 提前清除过期条目。若某键在原回放清除它（read/gc/签收）之前被同键重录，' \
         '落位由“覆盖旧位”变为“追加到队尾”，后续 read/签收 的交出顺序分歧。'
lines << '- 若重录之前原回放已用 read/gc 清掉该键，则两边同为追加，分歧在签收点前消失（可插）。'
lines << ''
lines << '## 小夹具（small_loop，3 轮，暴力全回放 + 直接核对交叉验证）'
lines << ''
small_direct.each do |tinfo|
  Acceptance::Report.emit_template(lines, tinfo, Acceptance::Segments.build(tinfo))
end
lines << "交叉验证: 直接核对结论与逐候选完整重放（#{brute[:results].size} 候选 × 全部轮次）完全一致。"
lines << ''
lines << '## 长验收（long_loop，120_000_000 轮压缩记录，直接核对）'
lines << ''
long_direct.each do |tinfo|
  Acceptance::Report.emit_template(lines, tinfo, Acceptance::Segments.build(tinfo))
end

File.write(File.expand_path('REPORT.md', __dir__), lines.join("\n") + "\n")
puts '报告已写入 acceptance/REPORT.md'
