# frozen_string_literal: true

# Entry point: build every cabinet x edition package and print the byte bill.
require_relative 'packer'

results = Packer.build_all
results.each do |r|
  puts "#{r[:cabinet]} / #{r[:edition]} (#{r[:rule_label]})  " \
       "#{r[:total_bytes]} bytes  budget=#{r[:budget]}  " \
       "#{r[:within_budget] ? 'OK' : 'OVER BUDGET'}"
  r[:files].each do |f|
    tag = f[:shared] ? ' [shared block]' : ''
    puts "    #{f[:bytes].to_s.rjust(6)}  #{f[:form].ljust(10)} #{f[:candidate].ljust(10)} #{f[:path]}#{tag}"
  end
end
