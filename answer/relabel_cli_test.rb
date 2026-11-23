# frozen_string_literal: true

require 'minitest/autorun'
require_relative 'relabel'
require_relative 'fixture_zip'
require 'json'
require 'zip'
require 'stringio'
require 'tmpdir'
require 'fileutils'

class RelabelCliTest < Minitest::Test
  PAD = 0xCAFE

  def setup
    @dir = Dir.mktmpdir('relabel-cli')
  end

  def teardown
    FileUtils.remove_entry(@dir)
  end

  def write(name, bytes)
    path = File.join(@dir, name)
    File.binwrite(path, bytes)
    path
  end

  def plan_for(archive, edit)
    records = ZipRelabel.central_records(archive)
    record = records.find { |r| r.name == edit.fetch('entry') }
    {
      'allowed' => [
        [record.local_header_offset, record.local_header_offset + record.header_size],
        [record.offset, record.end_offset]
      ],
      'edits' => [edit]
    }
  end

  def test_applies_a_plan_and_both_readers_agree
    bytes = FixtureZip.build([
                              { name: 'a.txt', data: 'A' * 24,
                                local_extra: FixtureZip.padding(8, id: PAD),
                                central_extra: FixtureZip.padding(8, id: PAD) }
                            ])
    archive = write('in.zip', bytes)
    plan = plan_for(bytes, {
                      'entry' => 'a.txt',
                      'name' => 'a-xx.txt',
                      'fund_local' => [{ 'id' => 'feca' }],
                      'fund_central' => [{ 'id' => 'feca' }]
                    })
    plan_path = write('plan.json', JSON.generate(plan))
    out = File.join(@dir, 'out.zip')

    status = ZipRelabel::CLI.run([archive, plan_path, '--apply', out])
    assert_equal 0, status
    assert_equal bytes.bytesize, File.size(out)

    revised = File.binread(out)
    zf = Zip::File.open_buffer(StringIO.new(revised))
    assert_equal 'A' * 24, zf.read('a-xx.txt')
    zf.close

    names = []
    Zip::InputStream.open(StringIO.new(revised)) do |zis|
      while (entry = zis.get_next_entry)
        names << entry.name
      end
    end
    assert_equal ['a-xx.txt'], names
  end

  def test_refuses_and_writes_nothing_when_budget_is_short
    bytes = FixtureZip.build([{ name: 'a.txt', data: 'A' * 24 }])
    archive = write('in.zip', bytes)
    plan = plan_for(bytes, {
                      'entry' => 'a.txt',
                      'name' => 'a-xx.txt',
                      'fund_local' => [{ 'id' => 'feca' }],
                      'fund_central' => [{ 'id' => 'feca' }]
                    })
    plan_path = write('plan.json', JSON.generate(plan))
    out = File.join(@dir, 'never.zip')

    status = ZipRelabel::CLI.run([archive, plan_path, '--apply', out])
    assert_equal 2, status
    refute File.exist?(out), 'a refused plan must not produce an archive'
  end
end
