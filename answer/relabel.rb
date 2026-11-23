# frozen_string_literal: true

require_relative 'zip_relabel'
require 'json'
require 'optparse'

# Read a plan file (JSON) and either apply it or explain why it cannot be
# applied. Plan format:
#
# {
#   "allowed": [[begin, end], ...],          // absolute, half-open byte ranges
#   "edits": [
#     {
#       "entry": "a.txt",
#       "name": "a-xx.txt",                  // optional
#       "comment": "reviewed",               // optional
#       "encoding": "utf8" | "cp437" | "keep",
#       "fund_local":   [{"id": "feca", "max": 8}],
#       "fund_central": [{"id": "feca", "max": 8}],
#       "expect_offset": 123
#     }
#   ]
# }
module ZipRelabel
  module CLI
    module_function

    def run(argv)
      options = { apply: false, report: false }
      parser = OptionParser.new do |opts|
        opts.banner = 'usage: relabel.rb ARCHIVE PLAN.json [--apply OUT.zip] [--report]'
        opts.on('--apply PATH', 'write the revised archive') { |v| options[:apply] = v }
        opts.on('--report', 'print the archive survey and the plan verdicts') do
          options[:report] = true
        end
      end
      parser.parse!(argv)
      archive, plan_path = argv
      abort parser.help if archive.nil? || plan_path.nil?

      bytes = File.binread(archive)
      plan = JSON.parse(File.read(plan_path))
      edits = plan.fetch('edits').map { |raw| to_edit(raw) }
      allowed = plan.fetch('allowed').map { |b, e| b...e }

      if options[:report]
        puts JSON.pretty_generate(ZipRelabel.survey(bytes))
        result = ZipRelabel.plan(bytes, edits)
        puts "\nverdicts:"
        result.notes.each { |n| puts "  ok   #{n}" }
        result.refusals.each { |r| puts "  stop #{r}" }
        return 0 unless result.refusals.empty? && options[:apply]
      end

      if options[:apply]
        revised, result = ZipRelabel.apply(bytes, edits, allowed: allowed)
        File.binwrite(options[:apply], revised)
        puts "wrote #{options[:apply]} (#{revised.bytesize} bytes, unchanged size)"
        result.notes.each { |n| puts "  #{n}" }
      end
      0
    rescue ZipRelabel::Refused => e
      warn 'refused:'
      e.reasons.each { |r| warn "  #{r}" }
      2
    end

    def to_edit(raw)
      ZipRelabel::Edit.new(
        entry: raw.fetch('entry').b,
        name: raw['name'] && raw['name'].b,
        comment: raw['comment'] && raw['comment'].b,
        encoding: raw.fetch('encoding', 'keep').to_sym,
        fund_local: funds(raw['fund_local']),
        fund_central: funds(raw['fund_central']),
        expect_offset: raw['expect_offset']
      )
    end

    def funds(list)
      Array(list).map do |raw|
        ZipRelabel::Fund.new(id: [raw.fetch('id')].pack('H*'), max_bytes: raw['max'])
      end
    end
  end
end

exit ZipRelabel::CLI.run(ARGV) if $PROGRAM_NAME == __FILE__
