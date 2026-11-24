# frozen_string_literal: true

require "test/unit"
require "tmpdir"
require "fileutils"
require_relative "../lib/pstore/streaming_extract"

class StreamingExtractTest < Test::Unit::TestCase
  def setup
    @dir = Dir.mktmpdir("pstore-extract")
    @source = File.join(@dir, "source.pstore")
    @dest = File.join(@dir, "audit.pstore")
  end

  def teardown
    FileUtils.remove_entry(@dir)
  end

  def write_source(table)
    File.binwrite(@source, Marshal.dump(table))
  end

  def extract(keys, chunk_size: 64)
    PStoreExtract::StreamingExtract.extract(
      source_path: @source,
      dest_path: @dest,
      keys: keys,
      chunk_size: chunk_size
    )
  end

  def load_dest
    Marshal.load(File.binread(@dest))
  end

  def assert_bytes_match_expected(expected_table)
    assert_equal Marshal.dump(expected_table).b, File.binread(@dest).b
  end

  def test_primitive_values_and_containers
    table = {
      :int => 12345,
      :neg => -987,
      :big => 2 ** 200,
      :flag_t => true,
      :flag_f => false,
      :nothing => nil,
      :sym => :hello,
      :arr => [1, "two", :three, true, nil, [4, 5]],
      :hash => { :a => 1, "b" => 2 }
    }
    write_source(table)
    result = extract(table.keys)
    assert_equal [], result.faults
    assert_bytes_match_expected(table)
    loaded = load_dest
    assert_equal 12345, loaded[:int]
    assert_equal(-987, loaded[:neg])
    assert_equal 2 ** 200, loaded[:big]
    assert_equal :hello, loaded[:sym]
    assert_equal [1, "two", :three, true, nil, [4, 5]], loaded[:arr]
  end

  def test_string_encodings_are_preserved
    table = {
      :utf8 => "héllo wörld".dup.force_encoding("UTF-8"),
      :binary => (+"\x00\x01\xFE\xFF").force_encoding("ASCII-8BIT"),
      :ascii => "plain".dup.force_encoding("US-ASCII"),
      :utf16 => "wide".dup.encode("UTF-16")
    }
    write_source(table)
    extract(table.keys)
    loaded = load_dest
    assert_equal Encoding::UTF_8, loaded[:utf8].encoding
    assert_equal Encoding::ASCII_8BIT, loaded[:binary].encoding
    assert_equal Encoding::US_ASCII, loaded[:ascii].encoding
    assert_equal Encoding::UTF_16, loaded[:utf16].encoding
    assert_equal "wide".encode("UTF-16"), loaded[:utf16]
    assert_bytes_match_expected(table)
  end

  def test_output_opens_in_plain_pstore_read_only_transaction
    require "pstore"
    write_source({ :audit => [1, 2, 3], :secret => "x" * 1000 })
    extract([:audit])
    store = PStore.new(@dest)
    seen = nil
    store.transaction(true) do
      assert_equal [:audit], store.keys
      seen = store[:audit]
    end
    assert_equal [1, 2, 3], seen
  end

  def test_shared_array_first_defined_in_excluded_material_index
    shared = [1, 2, ["payload", :tag]]
    table = {
      :materials => { :m1 => shared, :m2 => [9] },
      :submission => { :s1 => shared }
    }
    write_source(table)
    result = extract([:submission])
    assert_equal [], result.faults
    expected = { :submission => { :s1 => [1, 2, ["payload", :tag]] } }
    assert_bytes_match_expected(expected)
    loaded = load_dest
    assert_equal [1, 2, ["payload", :tag]], loaded[:submission][:s1]
  end

  def test_equal_but_distinct_strings_are_not_merged
    first = "same value".dup
    second = "same value".dup
    table = { :k1 => [first], :k2 => [second] }
    write_source(table)
    extract([:k1, :k2])
    loaded = load_dest
    assert_not_same loaded[:k1][0], loaded[:k2][0]
    assert_equal loaded[:k1][0], loaded[:k2][0]
    assert_bytes_match_expected(table)
  end

  def test_shared_string_stays_shared
    shared = "same object".dup
    table = { :k1 => [shared], :k2 => [shared] }
    write_source(table)
    extract([:k1, :k2])
    loaded = load_dest
    assert_same loaded[:k1][0], loaded[:k2][0]
    assert_bytes_match_expected(table)
  end

  def test_self_referencing_array_and_root_back_reference
    inner = []
    inner << inner
    table = { :audit => { :selfy => inner, :root => nil }, :drop => 7 }
    table[:audit][:root] = table
    write_source(table)
    extract([:audit])
    loaded = load_dest
    assert_equal [:audit], loaded.keys
    assert_same loaded, loaded[:audit][:root]
    assert_same loaded[:audit][:selfy], loaded[:audit][:selfy][0]
    expected_inner = []
    expected_inner << expected_inner
    expected = { :audit => { :selfy => expected_inner, :root => nil } }
    expected[:audit][:root] = expected
    assert_bytes_match_expected(expected)
  end

  def test_root_ivar_back_reference_keeps_slot_zero
    table = { :audit => 1, :drop => 2 }
    table.instance_variable_set(:@trace, 7)
    table[:back] = [table]
    write_source(table)
    extract([:audit, :back])
    loaded = load_dest
    assert_same loaded, loaded[:back][0]
    assert_equal 7, loaded.instance_variable_get(:@trace)
    expected = { :audit => 1 }
    expected.instance_variable_set(:@trace, 7)
    expected[:back] = [expected]
    assert_bytes_match_expected(expected)
  end

  def test_symbol_first_registered_in_discarded_entry
    table = { :drop_me => [:zeta, :eta], :keep_me => { :zeta => 1, :eta => 2 } }
    write_source(table)
    result = extract([:keep_me])
    assert_equal [], result.faults
    loaded = load_dest
    assert_equal [:keep_me], loaded.keys
    assert_equal 1, loaded[:keep_me][:zeta]
    assert_bytes_match_expected(:keep_me => { :zeta => 1, :eta => 2 })
  end

  def test_root_order_follows_source_not_request_order
    table = { :a => 1, :b => 2, :c => 3 }
    write_source(table)
    extract([:c, :a])
    assert_equal [:a, :c], load_dest.keys
  end

  def test_string_root_keys
    table = { "alpha" => 1, :beta => 2, "gamma".dup.force_encoding("UTF-8") => 3 }
    write_source(table)
    extract([:beta, "alpha"])
    loaded = load_dest
    assert_equal ["alpha", :beta], loaded.keys
    assert_equal 1, loaded["alpha"]
  end

  def test_missing_key_is_reported_but_others_delivered
    write_source({ :a => 1, :b => 2 })
    result = extract([:a, :nope])
    assert_equal [":a"], result.delivered_keys
    assert_equal [:missing_key], result.faults.map(&:type)
    assert_equal [:a], load_dest.keys
  end

  def test_hash_with_default_value
    table = { :keep => Hash.new(7).merge(:x => 1), :drop => 9 }
    write_source(table)
    extract([:keep])
    loaded = load_dest
    assert_equal 7, loaded[:keep][:absent]
    assert_equal 1, loaded[:keep][:x]
    assert_bytes_match_expected(:keep => Hash.new(7).merge(:x => 1))
  end

  def test_long_counts_and_index_forms
    big_array = Array.new(300) { |i| i }
    many_objects = Array.new(200) { [] }
    table = { :big_array => big_array, :many_objects => many_objects << many_objects.first }
    write_source(table)
    extract([:big_array, :many_objects])
    loaded = load_dest
    assert_equal 300, loaded[:big_array].size
    assert_same loaded[:many_objects].last, loaded[:many_objects].first
    assert_bytes_match_expected(table)
  end

  def test_large_payload_streams_without_full_size_memory
    skip "memory guard runs on darwin" unless RUBY_PLATFORM.include?("darwin")
    payload = "M" * (200 * 1024 * 1024)
    table = { :message => payload, :drop => "x" * 1024 }
    write_source(table)
    script = File.join(@dir, "run.rb")
    File.binwrite(script, <<~RUBY)
      require "#{File.expand_path("../lib/pstore/streaming_extract", __dir__)}"
      PStoreExtract::StreamingExtract.extract(
        source_path: #{@source.inspect},
        dest_path: #{@dest.inspect},
        keys: [:message],
        chunk_size: 4096
      )
    RUBY
    output = IO.popen(["/usr/bin/time", "-l", "ruby", script], err: [:child, :out], &:read)
    assert File.exist?(@dest), output
    match = output.match(/maximum resident set size\s+(\d+)/)
    assert match, output
    peak_mb = match[1].to_i / (1024 * 1024)
    assert_operator peak_mb, :<, 120, "peak RSS #{peak_mb}MB should stay far below the 200MB payload"
    assert_equal payload, Marshal.load(File.binread(@dest))[:message]
  end

  def test_truncated_later_entry_holds_back_only_unconfirmed_entries
    table = { :a => 1, :b => ["ok"], :c => "z" * 500 }
    write_source(table)
    full_size = File.size(@source)
    File.truncate(@source, full_size - 300)
    result = extract([:a, :b, :c])
    assert_include result.faults.map(&:type), :broken_structure
    loaded = load_dest
    assert_equal [:a, :b], loaded.keys
    assert_equal ["ok"], loaded[:b]
    fault = result.faults.find { |f| f.type == :broken_structure }
    assert_not_nil fault.position
  end

  def test_dangling_link_reports_position_and_affected_entry
    shared = []
    table = { :a => [shared, shared], :b => 2 }
    write_source(table)
    bytes = File.binread(@source).b
    index_byte_pos = nil
    (1...bytes.bytesize).each do |index|
      if bytes.getbyte(index - 1) == 0x40 && bytes.getbyte(index) == 0x07
        index_byte_pos = index
        break
      end
    end
    assert_not_nil index_byte_pos
    marker_pos = index_byte_pos - 1
    patched = bytes.dup
    patched.setbyte(index_byte_pos, 0x7F) # short form 0x7F -> object slot 122, never defined
    File.binwrite(@source, patched)
    result = extract([:a, :b])
    dangling = result.faults.find { |f| f.type == :dangling_reference }
    assert_not_nil dangling
    assert_equal marker_pos, dangling.position
    assert_include dangling.keys, ":a"
    loaded = load_dest
    assert_equal [:b], loaded.keys
  end

  def test_reader_skips_string_bodies_instead_of_buffering_them
    File.binwrite(@source, Marshal.dump({ :a => "x" * 10_000, :b => 1 }))
    max_read = 0
    bound_read = File.instance_method(:read)
    File.send(:define_method, :read) do |*args, &block|
      result = bound_read.bind_call(self, *args, &block)
      max_read = result.bytesize if result.is_a?(String) && result.bytesize > max_read
      result
    end
    begin
      # Structural walk with a 128 byte buffer must never pull the 10k body;
      # the excluded payload is seeked over, not read.
      PStoreExtract::StreamingExtract.extract(
        source_path: @source,
        dest_path: @dest,
        keys: [:b],
        chunk_size: 128
      )
    ensure
      File.send(:define_method, :read, bound_read)
    end
    assert_operator max_read, :<=, 128
    assert_equal 1, load_dest[:b]
  end

  def test_empty_source_file_is_treated_as_empty_store
    File.binwrite(@source, "")
    result = extract([:a])
    assert_equal [], result.delivered_keys
    assert_equal [:missing_key], result.faults.map(&:type)
    assert_equal({}, load_dest)
  end

  def test_all_keys_missing_still_writes_loadable_empty_hash
    write_source({ :a => 1 })
    result = extract([:x, :y])
    assert_equal [], result.delivered_keys
    assert_equal 2, result.faults.size
    assert_equal({}, load_dest)
  end

  def test_cli_manifest_and_exit_status
    write_source({ :keep => 1, :drop => 2 })
    manifest = File.join(@dir, "keys.txt")
    File.binwrite(manifest, "# audit list\n:keep\n:absent\n")
    require "open3"
    cli = File.expand_path("../bin/pstore-extract", __dir__)
    stdout, stderr, status = Open3.capture3(
      RbConfig.ruby, "-I#{File.expand_path("../lib", __dir__)}", cli,
      @source, @dest, "--manifest", manifest
    )
    assert status.success?, stderr
    assert_include stdout, ":keep"
    assert_include stdout, "missing_key"
    assert_equal [:keep], load_dest.keys
  end

  def test_cli_reports_dangling_entry_with_nonzero_status
    shared = []
    write_source({ :a => [shared, shared], :b => 2 })
    bytes = File.binread(@source).b
    index_byte_pos = (1...bytes.bytesize).find do |index|
      bytes.getbyte(index - 1) == 0x40 && bytes.getbyte(index) == 0x07
    end
    patched = bytes.dup
    patched.setbyte(index_byte_pos, 0x7F)
    File.binwrite(@source, patched)
    require "open3"
    cli = File.expand_path("../bin/pstore-extract", __dir__)
    _stdout, _stderr, status = Open3.capture3(
      RbConfig.ruby, "-I#{File.expand_path("../lib", __dir__)}", cli,
      @source, @dest, "--keys", ":a,:b"
    )
    assert_equal 1, status.exitstatus
    assert_equal [:b], load_dest.keys
  end

  def test_truncated_root_ivar_wrapper_is_dropped_cleanly
    table = { :a => 1, :b => ["ok"] }
    table.instance_variable_set(:@trace, 9)
    write_source(table)
    File.truncate(@source, File.size(@source) - 8)
    result = extract([:a, :b])
    assert_include result.faults.map(&:type), :broken_structure
    loaded = load_dest
    # Both confirmed entries survive; only the damaged root ivar is dropped.
    assert_equal [:a, :b], loaded.keys
    assert_equal ["ok"], loaded[:b]
    assert_equal [], loaded.instance_variables
  end
end
