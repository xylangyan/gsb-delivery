# frozen_string_literal: true

require 'minitest/autorun'
require_relative 'zip_relabel'
require_relative 'fixture_zip'
require 'zip'
require 'stringio'
require 'tmpdir'
require 'fileutils'

# End-to-end checks for the range-strict editor.
#
# Both public readers are exercised because they walk the archive differently:
# `Zip::File` trusts the central directory, while `Zip::InputStream` walks
# local headers and uses each entry's declared sizes to find the next one.
class ZipRelabelTest < Minitest::Test
  PAD = 0xCAFE

  def setup
    @dir = Dir.mktmpdir('zip-relabel')
  end

  def teardown
    FileUtils.remove_entry(@dir)
  end

  def pad(bytes)
    FixtureZip.padding(bytes, id: PAD)
  end

  def fund(max = nil)
    [ZipRelabel::Fund.new(id: [PAD].pack('v'), max_bytes: max)]
  end

  # Every byte outside the two rewritten headers must be identical, and every
  # structural offset must be unchanged.
  def assert_only_headers_changed(before, after, records_before, edited_names)
    edited = edited_names.map(&:b)
    allowed = records_before.flat_map do |r|
      next [] unless edited.include?(r.name) || edited.include?(r.local_header_offset.to_s)

      [(r.offset...(r.offset + r.header_size)),
       (r.local_header_offset...(r.local_header_offset + 30))]
    end
    ranges = allowed.map { |r| (r.begin...r.end) }
    before.bytes.each_with_index do |byte, i|
      next if ranges.any? { |r| r.cover?(i) }
      next if before.getbyte(i) == after.getbyte(i)

      flunk "byte #{i} changed outside the rewritten headers"
    end
  end

  # --- A: name grows by 3 bytes, paid for by pad content in both headers.
  # The local header still starts at the same absolute offset, so B and the
  # directory boundary do not move and the sequential reader keeps working.
  def test_a_three_byte_growth_funded_by_padding_keeps_all_offsets
    bytes = FixtureZip.build([
                              { name: 'a.txt', data: 'A' * 40,
                                local_extra: pad(8), central_extra: pad(8) },
                              { name: 'b.txt', data: 'B' * 50 }
                            ])
    before = ZipRelabel.central_records(bytes)
    a = before.find { |r| r.name == 'a.txt' }
    b = before.find { |r| r.name == 'b.txt' }
    boundary = ZipRelabel.directory_bounds(bytes)[1]

    edit = ZipRelabel::Edit.new(entry: 'a.txt', name: 'a-xx.txt', encoding: :keep,
                                fund_local: fund, fund_central: fund,
                                expect_offset: a.local_header_offset)
    revised, plan = ZipRelabel.apply(bytes, [edit],
                                     allowed: [(a.local_header_offset...(a.local_header_offset + 60)),
                                               (a.offset...a.end_offset)])

    assert_equal bytes.bytesize, revised.bytesize
    assert_equal boundary, ZipRelabel.directory_bounds(revised)[1]
    after = ZipRelabel.central_records(revised)
    a2 = after.find { |r| r.name == 'a-xx.txt' }
    b2 = after.find { |r| r.name == 'b.txt' }
    assert_equal a.local_header_offset, a2.local_header_offset, 'A must not move'
    assert_equal b.local_header_offset, b2.local_header_offset, 'B must not move'
    assert_equal a.crc, a2.crc
    assert_equal a.compressed_size, a2.compressed_size
    local_before = ZipRelabel.parse_local(bytes, a.local_header_offset)
    local_after = ZipRelabel.parse_local(revised, a2.local_header_offset)
    assert_equal bytes[local_before.data_offset, local_before.compressed_size],
                 revised[local_after.data_offset, local_after.compressed_size]
    assert_equal 3, a2.name_length - a.name_length

    assert_reads(revised, 'a-xx.txt' => 'A' * 40, 'b.txt' => 'B' * 50)
    assert plan.notes.grep(/trimmed 3 bytes/).any?
  end

  # --- B: only B may be touched. A's roster of names/offsets is empty, and
  # removing bytes from A's header is outside the authorised ranges.
  def test_only_the_free_entry_is_edited_others_are_left_alone
    bytes = FixtureZip.build([
                              { name: 'a.txt', data: 'A' * 30, local_extra: pad(6), central_extra: pad(6) },
                              { name: 'b.txt', data: 'B' * 30, local_extra: pad(8), central_extra: pad(8) }
                            ])
    before = ZipRelabel.central_records(bytes)
    a = before.find { |r| r.name == 'a.txt' }
    b = before.find { |r| r.name == 'b.txt' }
    edit = ZipRelabel::Edit.new(entry: 'b.txt', name: 'b-xy.txt',
                                fund_local: fund, fund_central: fund,
                                expect_offset: b.local_header_offset)

    # Deliberately exclude every byte of A's headers.
    allowed = [(b.local_header_offset...(b.local_header_offset + b.header_size)),
               (b.offset...b.end_offset)]
    revised, = ZipRelabel.apply(bytes, [edit], allowed: allowed)

    after = ZipRelabel.central_records(revised)
    assert_equal a.offset, after.find { |r| r.name == 'a.txt' }.offset
    assert_equal a.crc, after.find { |r| r.name == 'a.txt' }.crc
    assert_equal b.crc, after.find { |r| r.name == 'b-xy.txt' }.crc
    assert_reads(revised, 'a.txt' => 'A' * 30, 'b-xy.txt' => 'B' * 30)
  end

  # --- A cannot meet both requirements: name needs +3 but the only spare
  # bytes live in the other entry, whose header is not writable.
  def test_a_is_refused_and_the_reason_names_the_shortfall
    bytes = FixtureZip.build([
                              { name: 'a.txt', data: 'A' * 30 },
                              { name: 'b.txt', data: 'B' * 30, local_extra: pad(8), central_extra: pad(8) }
                            ])
    before = ZipRelabel.central_records(bytes)
    a = before.find { |r| r.name == 'a.txt' }
    b = before.find { |r| r.name == 'b.txt' }
    edit = ZipRelabel::Edit.new(entry: 'a.txt', name: 'a-xx.txt', fund_local: fund,
                                fund_central: fund, expect_offset: a.local_header_offset)
    allowed = [(a.local_header_offset...(a.local_header_offset + a.header_size)),
               (a.offset...a.end_offset)]

    error = assert_raises(ZipRelabel::Refused) { ZipRelabel.apply(bytes, [edit], allowed: allowed) }
    assert_match(/local header needs 3 bytes/, error.message)
    assert_match(/central record needs 3 bytes/, error.message)

    # B remains independently editable within its own bytes.
    b_edit = ZipRelabel::Edit.new(entry: 'b.txt', name: 'b-xy.txt',
                                  fund_local: fund, fund_central: fund,
                                  expect_offset: b.local_header_offset)
    revised, = ZipRelabel.apply(bytes, [b_edit],
                                allowed: [(b.local_header_offset...(b.local_header_offset + b.header_size)),
                                          (b.offset...b.end_offset)])
    assert_reads(revised, 'a.txt' => 'A' * 30, 'b-xy.txt' => 'B' * 30)
  end

  # --- comment growth is paid from the central header only
  def test_comment_growth_funded_from_central_padding
    bytes = FixtureZip.build([{ name: 'a.txt', data: 'A' * 20, central_extra: pad(30) }])
    record = ZipRelabel.central_records(bytes).first
    comment = 'reviewed 2026-11-24'
    edit = ZipRelabel::Edit.new(entry: 'a.txt', comment: comment, fund_central: fund,
                                expect_offset: record.local_header_offset)
    revised, = ZipRelabel.apply(bytes, [edit], allowed: [(record.offset...record.end_offset)])

    after = ZipRelabel.central_records(revised).first
    assert_equal comment, after.comment
    assert_equal record.crc, after.crc
    assert_equal record.local_header_offset, after.local_header_offset
    assert_reads(revised, 'a.txt' => 'A' * 20)
  end

  # --- exact partial trim: take 3 bytes out of one 12-byte pad record
  def test_partial_padding_trim_leaves_a_valid_shorter_extra_record
    bytes = FixtureZip.build([{ name: 'a.txt', data: 'A' * 20, local_extra: pad(12),
                                central_extra: pad(12) }])
    record = ZipRelabel.central_records(bytes).first
    edit = ZipRelabel::Edit.new(entry: 'a.txt', name: 'a-xx.txt', fund_local: fund,
                                fund_central: fund, expect_offset: record.local_header_offset)
    revised, = ZipRelabel.apply(bytes, [edit],
                                allowed: [(record.local_header_offset...(record.local_header_offset + 50)),
                                          (record.offset...record.end_offset)])
    local = ZipRelabel.parse_local(revised, record.local_header_offset)
    extra = ZipRelabel.extra_records(local.extra).first
    assert_equal [PAD].pack('v'), extra.id
    # 12-byte pad = 4-byte header + 8 content; 3 bytes are taken from content.
    assert_equal 5, extra.content_length, 'pad should shrink, not disappear'
    assert_reads(revised, 'a-xx.txt' => 'A' * 20)
  end

  def test_refuses_when_nothing_is_authorised
    bytes = FixtureZip.build([{ name: 'a.txt', data: 'A' * 20 }])
    record = ZipRelabel.central_records(bytes).first
    edit = ZipRelabel::Edit.new(entry: 'a.txt', name: 'a-xx.txt', fund_local: fund,
                                fund_central: fund)
    error = assert_raises(ZipRelabel::Refused) do
      ZipRelabel.apply(bytes, [edit], allowed: [(record.offset...record.end_offset)])
    end
    assert_match(/local header needs 3 bytes/, error.message)
  end

  def test_refuses_when_target_headers_are_not_in_the_allowed_ranges
    bytes = FixtureZip.build([{ name: 'a.txt', data: 'A' * 20, local_extra: pad(8),
                                central_extra: pad(8) }])
    record = ZipRelabel.central_records(bytes).first
    edit = ZipRelabel::Edit.new(entry: 'a.txt', name: 'a-xx.txt', fund_local: fund,
                                fund_central: fund)
    error = assert_raises(ZipRelabel::Refused) { ZipRelabel.apply(bytes, [edit], allowed: []) }
    assert_match(/outside every allowed range/, error.message)
  end

  def test_utf8_sets_and_cp437_clears_the_efs_bit
    bytes = FixtureZip.build([{ name: 'a.txt', data: 'A' * 10, local_extra: pad(8),
                                central_extra: pad(8) }])
    record = ZipRelabel.central_records(bytes).first
    edit = ZipRelabel::Edit.new(entry: 'a.txt', name: 'e.txt', encoding: :utf8,
                                fund_local: fund, fund_central: fund)
    revised, = ZipRelabel.apply(bytes, [edit], allowed: [(record.offset...record.end_offset),
                                                         (record.local_header_offset...(record.local_header_offset + 50))])
    after = ZipRelabel.central_records(revised).first
    local = ZipRelabel.parse_local(revised, after.local_header_offset)
    assert_equal 0x0800, after.gp_flags & 0x0800
    assert_equal 0x0800, local.gp_flags & 0x0800

    edit2 = ZipRelabel::Edit.new(entry: 'e.txt', name: 'a.txt', encoding: :cp437,
                                 fund_local: fund, fund_central: fund)
    revised2, = ZipRelabel.apply(revised, [edit2], allowed: [(after.offset...after.end_offset),
                                                             (after.local_header_offset...(after.local_header_offset + 50))])
    final = ZipRelabel.central_records(revised2).first
    assert_equal 0, final.gp_flags & 0x0800
    assert_reads(revised2, 'a.txt' => 'A' * 10)
  end

  def test_identity_pass_is_byte_identical
    bytes = FixtureZip.build([{ name: 'one.txt', data: 'one' * 100 },
                              { name: 'two.txt', data: 'two' * 250 }])
    revised, _plan = ZipRelabel.apply(bytes, [], allowed: [])
    assert_equal bytes, revised
  end

  # Funding from a *structured* field can be invalid even when the raw bytes
  # look right: Rubyzip re-expands the trimmed record when reading, so the
  # streamed next-offset drifts. The guard must refuse that archive rather
  # than hand back something one reader accepts and the other rejects.
  def test_refuses_when_trimming_a_known_field_would_break_streaming
    bytes = File.binread(File.expand_path('../test/data/zip64-sample.zip', __dir__))
    record = ZipRelabel.central_records(bytes).find { |r| r.name == 'test.rb' }
    local = ZipRelabel.parse_local(bytes, record.local_header_offset)
    edit = ZipRelabel::Edit.new(
      entry: 'test.rb', name: 'test11.rb',
      fund_local: [ZipRelabel::Fund.new(id: 'UT', max_bytes: 2)],
      fund_central: [ZipRelabel::Fund.new(id: 'UT', max_bytes: 2)],
      expect_offset: record.local_header_offset
    )
    error = assert_raises(ZipRelabel::Refused) do
      ZipRelabel.apply(bytes, [edit],
                       allowed: [(local.offset...(local.offset + local.header_size)),
                                 (record.offset...record.end_offset)])
    end
    assert_match(/streaming reader would break/, error.message)
  end

  def test_stream_chain_is_sound_for_untouched_archives
    bytes = FixtureZip.build([
                              { name: 'one.txt', data: 'one' * 100 },
                              { name: 'two.txt', data: 'two' * 100, local_extra: pad(8) }
                            ])
    assert_empty ZipRelabel.stream_chain_problems(bytes)
  end

  def test_shared_local_header_is_reported_and_never_moved
    # Build a valid archive, then duplicate one central record so two names
    # point at the same local header. The second record uses a 5-byte name.
    bytes = FixtureZip.build([{ name: 'a.txt', data: 'A' * 32, local_extra: pad(8),
                                central_extra: pad(8) }])
    total, cdir_offset, = ZipRelabel.directory_bounds(bytes)
    record = ZipRelabel.central_records(bytes).first

    duplicate = bytes[record.offset...record.end_offset].dup
    duplicate[28, 2] = [5].pack('v')
    duplicate[46, 5] = 'a2xxx'
    duplicate[46 + 5, record.extra_length] = '' # keep extra where it was
    # Reassemble: static header + 5-byte name + original extra + comment.
    rebuilt = duplicate[0, 28] + [5].pack('v') + duplicate[30, 16] +
              'a2xxx' + record.extra + record.comment

    original_cdir = bytes[cdir_offset...]
    new_cdir = bytes[cdir_offset, record.end_offset - cdir_offset] + rebuilt +
               bytes[record.end_offset...bytes.bytesize - (original_cdir.bytesize - (record.end_offset - cdir_offset))]
    eocd_data = bytes[bytes.rindex("PK\x05\x06".b), 22]
    eocd_template = eocd_data.unpack('VvvvvVVv')
    new_eocd = [eocd_template[0], 0, 0, total + 1, total + 1,
                rebuilt.bytesize + eocd_template[5], cdir_offset, 0].pack('VvvvvVVv')

    patched = bytes[0, cdir_offset] + bytes[cdir_offset, record.end_offset - cdir_offset] +
              rebuilt + new_eocd
    plan = ZipRelabel.plan(patched, [])
    assert plan.notes.any? { |n| n.include?('shared by 2 directory records') },
           "notes were: #{plan.notes.inspect}"
  end

  private

  def assert_reads(bytes, expected)
    zf = Zip::File.open_buffer(StringIO.new(bytes))
    expected.each do |name, data|
      assert_equal data, zf.read(name), "Zip::File content for #{name}"
    end
    zf.close

    seen = {}
    Zip::InputStream.open(StringIO.new(bytes)) do |zis|
      while (entry = zis.get_next_entry)
        seen[entry.name] = zis.read
      end
    end
    expected.each do |name, data|
      assert_equal data, seen[name], "Zip::InputStream content for #{name}"
    end
  end
end
