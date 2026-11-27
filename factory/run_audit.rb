# frozen_string_literal: true

# Builds every package, runs the full isolated audit and prints the change-list
# / byte bill / verification report. Exits non-zero if any check fails or any
# package (or the global cap) is over budget.
require_relative 'auditor'
require_relative 'shipping_manifest'
require_relative 'feasibility'

report = Auditor.audit_all
failures_total = 0
grand_total = 0

puts '=' * 78
puts 'CABINET PACKAGE VERIFICATION (each package opened in an isolated process)'
puts '=' * 78

report.each do |entry|
  result = entry[:result]
  grand_total += result[:total_bytes]
  failures_total += entry[:failures].size
  status = entry[:failures].empty? ? 'PASS' : 'FAIL'
  puts "\n[#{status}] #{entry[:cabinet]} edition #{entry[:edition]} (#{result[:rule_label]})"
  puts "      rule source : #{result[:rule_source]}"
  puts "      bytes       : #{result[:total_bytes]} / budget #{result[:budget]} " \
       "(#{result[:within_budget] ? 'within' : 'OVER'})"
  result[:files].each do |f|
    shared = f[:shared] ? '  shared-block' : ''
    puts "        #{f[:bytes].to_s.rjust(5)} B  #{f[:form].ljust(10)} #{f[:candidate].ljust(10)} #{f[:identifier]}#{shared}"
  end
  entry[:failures].each { |m| puts "      FAILURE: #{m}" }
end

puts "\n" + '=' * 78
puts 'CROSS-EDITION ROLLBACK (same sample UTC instants via old A and new B packages)'
puts '=' * 78
Auditor.rollback_equivalence.each do |cabinet_key, identifier, equal|
  failures_total += 1 unless equal
  puts "  [#{equal ? 'PASS' : 'FAIL'}] #{cabinet_key} #{identifier}: A == B => #{equal}"
end

puts "\n" + '=' * 78
puts 'BUDGET'
puts '=' * 78
puts "  global shipped total : #{grand_total} B"
puts "  global cap           : #{Catalog::GLOBAL_BUDGET} B (#{grand_total <= Catalog::GLOBAL_BUDGET ? 'within' : 'OVER'})"

if grand_total > Catalog::GLOBAL_BUDGET
  failures_total += 1
  puts '  GLOBAL CAP EXCEEDED - minimum feasible budget would need to cover the fixed'
  puts '  loader+two-index floor of every package plus the zones/countries actually queried.'
end

puts "\n" + '=' * 78
puts 'MINIMUM FEASIBLE BUDGET PER CABINET (larger of the two editions is the cabinet floor)'
puts '=' * 78
Catalog::CABINETS.each do |cabinet|
  per = %w[A B].map { |edition| Feasibility.minimum_for(cabinet, edition) }
  floor = per.map { |info| info[:minimum_bytes] }.max
  puts "  #{cabinet.key}: A=#{per[0][:minimum_bytes]} B=#{per[1][:minimum_bytes]} " \
       "minimum cabinet budget=#{floor} (current budget #{cabinet.budget})"
end

puts "\n" + '=' * 78
puts 'BLOCKER EXAMPLE: what stops a deliberately too-tight target (C1/B at 1500 B)'
puts '=' * 78
tight = Feasibility.explain_target(Catalog.cabinet('C1'), 'B', 1500)
puts "  target 1500 B vs minimum #{tight[:minimum_bytes]} B (shortfall #{tight[:shortfall]} B)"
tight[:blocks].each { |b| puts "    blocks: #{b[:item].ljust(22)} #{b[:reason]}" }

paths = ShippingManifest.write(report)
puts "\nWrote shipping manifest:\n  #{paths.join("\n  ")}"

puts "\n#{failures_total.zero? ? 'ALL CHECKS PASSED' : "#{failures_total} CHECK(S) FAILED"}"
exit(failures_total.zero? ? 0 : 1)
