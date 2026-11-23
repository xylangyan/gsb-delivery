# frozen_string_literal: true

require_relative 'zip_relabel'
require_relative 'fixture_zip'
require 'zip'
require 'stringio'
require 'json'

# Build the two worked examples described in the task and print a per-entry
# verdict for each. Also proves both readers see the same result.
module Demo
  PAD = [0xCAFE].pack('v')

  module_function

  def fund(max = nil)
    [ZipRelabel::Fund.new(id: PAD, max_bytes: max)]
  end

  # Scenario A: the requested new name is three bytes longer, and the only
  # authorised slack is adjustable padding inside the entry's own two headers.
  def scenario_a
    bytes = FixtureZip.build([
                              { name: 'a.txt', data: 'alpha payload' * 4,
                                local_extra: FixtureZip.padding(8, id: 0xCAFE),
                                central_extra: FixtureZip.padding(8, id: 0xCAFE) },
                              { name: 'b.txt', data: 'beta payload' * 4,
                                local_extra: FixtureZip.padding(8, id: 0xCAFE),
                                central_extra: FixtureZip.padding(8, id: 0xCAFE) }
                            ])
    record = ZipRelabel.central_records(bytes).find { |r| r.name == 'a.txt' }
    edit = ZipRelabel::Edit.new(
      entry: 'a.txt', name: 'a-xx.txt', encoding: :keep,
      fund_local: fund, fund_central: fund, expect_offset: record.local_header_offset
    )
    allowed = [(record.local_header_offset...(record.local_header_offset + record.header_size)),
               (record.offset...record.end_offset),
               # B is independently editable too, and is not referenced by A.
               *begin
                 b = ZipRelabel.central_records(bytes).find { |r| r.name == 'b.txt' }
                 [(b.local_header_offset...(b.local_header_offset + b.header_size)),
                  (b.offset...b.end_offset)]
               end]
    revised, plan = ZipRelabel.apply(bytes, [edit], allowed: allowed)
    [bytes, revised, plan]
  end

  # Scenario A-strict: the new name grows by three bytes, but the only padding
  # lives in B's headers and B is outside the authorised ranges.
  def scenario_a_strict
    bytes = FixtureZip.build([
                              { name: 'a.txt', data: 'alpha payload' * 4 },
                              { name: 'b.txt', data: 'beta payload' * 4,
                                local_extra: FixtureZip.padding(8, id: 0xCAFE),
                                central_extra: FixtureZip.padding(8, id: 0xCAFE) }
                            ])
    record = ZipRelabel.central_records(bytes).find { |r| r.name == 'a.txt' }
    edit = ZipRelabel::Edit.new(
      entry: 'a.txt', name: 'a-xx.txt', encoding: :keep,
      fund_local: fund, fund_central: fund, expect_offset: record.local_header_offset
    )
    allowed = [(record.local_header_offset...(record.local_header_offset + record.header_size)),
               (record.offset...record.end_offset)]
    ZipRelabel.apply(bytes, [edit], allowed: allowed)
  end

  # Scenario B: only the entry nothing else references may change.
  def scenario_b
    bytes = FixtureZip.build([
                              { name: 'a.txt', data: 'alpha payload' * 4 },
                              { name: 'b.txt', data: 'beta payload' * 4,
                                local_extra: FixtureZip.padding(32, id: 0xCAFE),
                                central_extra: FixtureZip.padding(32, id: 0xCAFE) }
                            ])
    b = ZipRelabel.central_records(bytes).find { |r| r.name == 'b.txt' }
    edit = ZipRelabel::Edit.new(
      entry: 'b.txt', name: 'b-xx.txt', comment: 'display name only',
      fund_local: fund, fund_central: fund, expect_offset: b.local_header_offset
    )
    allowed = [(b.local_header_offset...(b.local_header_offset + b.header_size)),
               (b.offset...b.end_offset)]
    revised, plan = ZipRelabel.apply(bytes, [edit], allowed: allowed)
    [bytes, revised, plan]
  end

  def readers(bytes)
    central = {}
    zf = Zip::File.open_buffer(StringIO.new(bytes))
    zf.entries.each { |e| central[e.name] = [zf.read(e), e.crc] }
    zf.close

    streamed = {}
    Zip::InputStream.open(StringIO.new(bytes)) do |zis|
      while (entry = zis.get_next_entry)
        streamed[entry.name] = [zis.read, entry.crc]
      end
    end
    { central: central, streamed: streamed }
  end

  def run
    a_before, a_after, a_plan = scenario_a
    puts '== scenario A: 3-byte growth funded from the entry\'s own padding'
    puts JSON.pretty_generate(ZipRelabel.survey(a_before))
    puts 'plan:'
    a_plan.notes.each { |n| puts "  ok   #{n}" }
    puts "readers before: #{readers(a_before).inspect}"
    puts "readers after:  #{readers(a_after).inspect}"
    puts

    puts '== scenario A-strict: growth needed, but only B has spare bytes'
    begin
      scenario_a_strict
    rescue ZipRelabel::Refused => e
      e.reasons.each { |r| puts "  stop #{r}" }
    end
    puts

    b_before, b_after, b_plan = scenario_b
    puts '== scenario B: only the unreferenced entry is edited'
    b_plan.notes.each { |n| puts "  ok   #{n}" }
    puts "readers before: #{readers(b_before).inspect}"
    puts "readers after:  #{readers(b_after).inspect}"
  end
end

Demo.run if $PROGRAM_NAME == __FILE__
