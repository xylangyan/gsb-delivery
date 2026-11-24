#!/usr/bin/env ruby
# 观测册日期核账。
#
# 每本册从头到尾只用一种机器 EDTF 设置(历法 + 旧式语法开关),换设置只发生在
# 两册之间。本工具为整批册枚举设置分派,保留换设置次数最少且全局自洽的方案:
#   * 同一原事件号在各册的抄录必须落到同一绝对日;
#   * 已核到绝对日的观测(anchors)锁定该事件的绝对日;
#   * 底片登记显影日与观测日相隔固定天数(plates),折算后锁定观测日;
#   * 月份精度、许可日期列表([a, b])等标签接受任一相容的绝对日,
#     不把月初直接当作事件日。
#
# 用法:
#   ruby reconcile/reconcile.rb REGISTRY.yaml             核账,给入仓值/备选方案/冲突清单
#   ruby reconcile/reconcile.rb REGISTRY.yaml query 册ID  该册设置在所有最少切换方案中
#                                                         唯一时,按 EDTF 原读法列出各标签
#
# 绝对日一律用外推格里历(proleptic Gregorian)表示。

require 'edtf'
require 'yaml'
require 'date'

GREG = Date::GREGORIAN
MAX_ASSIGNMENTS = 200_000

def abs_jd(str) = Date.iso8601(str.to_s, GREG).jd
def jd_to_s(jd) = Date.jd(jd, GREG).iso8601

def jd_range_str(days)
  return '∅' if days.empty?
  return jd_to_s(days.first) if days.size == 1

  "#{jd_to_s(days.first)}..#{jd_to_s(days.last)}(#{days.size} 个相容日)"
end

# 解析结果 -> 候选绝对日(儒略日数)升序数组。
# widen: 带不精确记号(?/~)的日期向两侧放宽的天数,口径由登记给出,默认 0。
def day_set(node, widen = 0)
  days =
    case node
    when EDTF::Set
      node.to_a.flat_map { |member| day_set(member, widen) }.uniq.sort
    when EDTF::Interval
      lo = node.begin && node.begin.begin.jd
      hi = node.end && node.end.end.jd
      raise "开口区间无法定日: #{node}" unless lo && hi

      (lo..hi).to_a
    else
      lo = node.begin.jd
      hi = node.end.jd
      if widen.positive? && node.respond_to?(:uncertain?) &&
         (node.uncertain? || node.approximate?)
        lo -= widen
        hi += widen
      end
      (lo..hi).to_a
    end
  days
end

def load_registry(path)
  reg = YAML.load_file(path)
  settings = reg.fetch('settings').map do |id, h|
    { id: id.to_s, calendar: h.fetch('calendar').to_sym, legacy: !!h['legacy'] }
  end
  {
    settings: settings,
    books: reg.fetch('books'),
    anchors: reg['anchors'] || {},
    plates: reg['plates'] || [],
    widen: reg['widen'] || 0
  }
end

# 每册 x 每种设置: 全部标签读得通则该设置对该册可行,并记下各事件的候选绝对日。
def viability(reg)
  reg[:books].map do |book|
    reg[:settings].each_with_object({}) do |setting, table|
      entries = {}
      ok = book.fetch('entries').all? do |entry|
        node = EDTF.parse(entry['label'].to_s,
                          calendar: setting[:calendar], legacy: setting[:legacy])
        node && (entries[entry['event'].to_s] = day_set(node, reg[:widen]))
      end
      table[setting[:id]] = entries if ok
    end
  end
end

# 给定整批设置分派,合并各事件的候选绝对日并施加锚点与底片约束。
# 自洽则返回 {事件 => 候选绝对日数组},否则返回 nil。
def reconcile_events(reg, viable, assignment)
  events = {}
  reg[:books].each_with_index do |book, bi|
    table = viable[bi][assignment[bi]]
    book.fetch('entries').each do |entry|
      ev = entry['event'].to_s
      events[ev] = events.key?(ev) ? events[ev] & table[ev] : table[ev].dup
    end
  end
  reg[:anchors].each do |ev, date|
    ev = ev.to_s
    events[ev] = events.key?(ev) ? events[ev] & [abs_jd(date)] : [abs_jd(date)]
  end
  relations = []
  reg[:plates].each do |plate|
    ev = plate.fetch('event').to_s
    offset = plate.fetch('offset')
    if plate['developed']
      events[ev] = (events[ev] || []) & [abs_jd(plate['developed']) - offset]
    else
      relations << [ev, plate.fetch('developed_event').to_s, offset]
    end
  end
  loop do
    changed = false
    relations.each do |obs, dev, offset|
      obs_days = events[obs] || []
      dev_days = events[dev] || []
      new_obs = obs_days & dev_days.map { |d| d - offset }
      new_dev = dev_days & obs_days.map { |d| d + offset }
      if new_obs != obs_days || new_dev != dev_days
        events[obs] = new_obs
        events[dev] = new_dev
        changed = true
      end
    end
    break unless changed
  end
  return nil if events.any? { |_, days| days.empty? }

  events
end

# 枚举整批设置分派,保留换设置次数最少且自洽的方案。
def solve(reg, viable)
  per_book = viable.map(&:keys)
  if per_book.any?(&:empty?)
    stuck = reg[:books].each_with_index.select { |_, i| per_book[i].empty? }
                       .map { |b, _| b['id'] }
    raise "以下册没有任何可行设置(有标签在所有设置下都读不通): #{stuck.join(', ')}"
  end
  total = per_book.reduce(1) { |n, ids| n * ids.size }
  raise "分派空间过大(#{total}),请裁剪设置候选" if total > MAX_ASSIGNMENTS

  best = nil
  solutions = []
  indices = [0] * per_book.size
  loop do
    assignment = per_book.each_with_index.map { |ids, i| ids[indices[i]] }
    switches = assignment.each_cons(2).count { |a, b| a != b }
    if best.nil? || switches <= best
      events = reconcile_events(reg, viable, assignment)
      if events
        if best.nil? || switches < best
          best = switches
          solutions = []
        end
        solutions << { settings: assignment, events: events }
      end
    end
    pos = per_book.size - 1
    pos -= 1 while pos >= 0 && (indices[pos] += 1) == per_book[pos].size && (indices[pos] = 0)
    break if pos.negative?
  end
  [best, solutions]
end

def print_solution(reg, solution)
  reg[:books].each_with_index do |book, i|
    puts "  《#{book['id']}》 -> 设置 #{solution[:settings][i]}"
  end
  solution[:events].sort.each do |ev, days|
    puts "    #{ev}: #{jd_range_str(days)}"
  end
end

def report(reg, viable, best, solutions)
  puts "最少换设置次数: #{best};自洽的最低切换方案共 #{solutions.size} 份"
  puts

  events = solutions.flat_map { |s| s[:events].keys }.uniq.sort
  archive = {}
  events.each do |ev|
    sets = solutions.map { |s| s[:events][ev] }.uniq
    archive[ev] = sets.first.first if sets.size == 1 && sets.first.size == 1
  end

  puts '== 入仓值(所有最低切换方案确认同一实际日期) =='
  if archive.empty?
    puts '  (无)'
  else
    archive.each { |ev, jd| puts "  #{ev}: #{jd_to_s(jd)}" }
  end
  ambiguous = events - archive.keys
  unless ambiguous.empty?
    puts
    puts '== 仍未定日的事件(各方案取值) =='
    ambiguous.each do |ev|
      puts "  #{ev}:"
      solutions.each_with_index do |s, i|
        puts "    方案#{i + 1}: #{jd_range_str(s[:events][ev])}"
      end
    end
  end

  return unless solutions.size > 1

  puts
  puts '== 两份可解释原件的最低切换方案 =='
  [solutions.first, solutions.last].uniq.each_with_index do |solution, i|
    puts "-- 方案#{i + 1} --"
    print_solution(reg, solution)
  end
end

# 没有任何自洽分派时,列出互相卡住的抄录与底片登记。
def diagnose(reg, viable)
  puts '没有共同解释。互相卡住的登记如下:'
  puts
  entries = []
  reg[:books].each_with_index do |book, bi|
    book.fetch('entries').each do |entry|
      entries << { book: book['id'], bi: bi, event: entry['event'].to_s,
                   label: entry['label'].to_s }
    end
  end
  entries.group_by { |e| e[:event] }.each do |ev, group|
    next if group.size < 2

    group.combination(2) do |a, b|
      compatible = viable[a[:bi]].keys.product(viable[b[:bi]].keys).any? do |sa, sb|
        (viable[a[:bi]][sa][ev] & viable[b[:bi]][sb][ev]).any?
      end
      next if compatible

      puts "  抄录冲突 事件 #{ev}: 《#{a[:book]}》「#{a[:label]}」 与 " \
           "《#{b[:book]}》「#{b[:label]}」在任何设置组合下都落不到同一绝对日"
    end
  end
  reg[:plates].each do |plate|
    ev = plate.fetch('event').to_s
    offset = plate.fetch('offset')
    possible = entries.select { |e| e[:event] == ev }.flat_map do |e|
      viable[e[:bi]].values.flat_map { |table| table[ev] }
    end.uniq
    ok =
      if plate['developed']
        possible.include?(abs_jd(plate['developed']) - offset)
      else
        other = entries.select { |e| e[:event] == plate['developed_event'].to_s }
                       .flat_map do |e|
          viable[e[:bi]].values.flat_map { |table| table[e[:event]] }
        end.uniq
        possible.any? { |d| other.include?(d + offset) }
      end
    next if ok

    what = plate['developed'] || "事件 #{plate['developed_event']}"
    puts "  底片冲突 #{plate['id']}: 事件 #{ev} 的观测日候选 " \
         "(#{possible.map { |d| jd_to_s(d) }.join(', ')}) 与显影登记 " \
         "#{what} 相隔 #{offset} 天对不上"
  end
end

# 普通设置明确的一册: 按 EDTF 原读法列出各标签。
def query(reg, viable, solutions, book_id)
  idx = reg[:books].index { |b| b['id'].to_s == book_id }
  raise "找不到册《#{book_id}》" unless idx

  used = solutions.map { |s| s[:settings][idx] }.uniq
  unless used.size == 1
    puts "《#{book_id}》的设置在最低切换方案中不唯一: #{used.join(', ')},暂不能定读法"
    return
  end
  setting = reg[:settings].find { |s| s[:id] == used.first }
  puts "《#{book_id}》设置确定: #{setting[:id]} " \
       "(calendar: #{setting[:calendar]}, legacy: #{setting[:legacy]})"
  reg[:books][idx].fetch('entries').each do |entry|
    label = entry['label'].to_s
    node = EDTF.parse(label, calendar: setting[:calendar], legacy: setting[:legacy])
    puts "  #{entry['event']}  「#{label}」  EDTF 读法: #{node.edtf}  " \
         "绝对日: #{jd_range_str(day_set(node, reg[:widen]))}"
  end
end

if __FILE__ == $PROGRAM_NAME
  path = ARGV[0] or abort "用法: ruby #{$PROGRAM_NAME} REGISTRY.yaml [query 册ID]"
  reg = load_registry(path)
  viable = viability(reg)
  best, solutions = solve(reg, viable)
  if ARGV[1] == 'query'
    abort '没有自洽方案,无法定读法' if solutions.empty?

    book_id = ARGV[2] or abort('query 需要册ID')
    query(reg, viable, solutions, book_id)
  elsif solutions.empty?
    diagnose(reg, viable)
  else
    report(reg, viable, best, solutions)
  end
end
