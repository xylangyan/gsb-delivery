# frozen_string_literal: true
require 'test/unit'
require 'pstore'
require 'tmpdir'
require 'fileutils'

class PStoreIncrementalTest < Test::Unit::TestCase
  Message = Struct.new(:id, :body)
  Invoice = Data.define(:number, :lines)
  Node = Data.define(:value, :holder)

  def setup
    @dir = Dir.mktmpdir("pstore-inc")
    @file = File.join(@dir, "ledger.pstore")
    @journal = PStore::Incremental.journal_path(@file)
  end

  def teardown
    FileUtils.remove_entry(@dir) if @dir && File.exist?(@dir)
  end

  def build(shared:, cycle: true)
    original = {shared: shared, note: "before"}
    original[:self] = original if cycle
    shard = [shared, {id: "tail"}]
    PStore.new(@file).transaction do |store|
      store[:shard] = shard
      store[:original] = original
    end
    [shard, original, shared]
  end

  def read
    PStore.new(@file).transaction(true) { |store| yield store }
  end

  def test_shared_object_update_is_visible_from_both_roots
    shared = {id: "S", version: 1}
    build(shared: shared)
    store = PStore.new(@file)

    handle = store.handle(:original, [:shared])
    receipt = store.amend_shared(handle, {id: "S", version: 2})
    assert_true(receipt.applied?)

    read do |s|
      assert_equal({id: "S", version: 2}, s[:original][:shared])
      assert_equal({id: "S", version: 2}, s[:shard][0])
      assert_same(s[:original][:shared], s[:shard][0])
      assert_same(s[:original], s[:original][:self])
    end
  end

  def test_single_reference_replacement_keeps_the_other_root
    shared = {id: "S", version: 1}
    build(shared: shared)
    store = PStore.new(@file)

    store.amend_replace(:shard, [1], {id: "replacement"})

    read do |s|
      assert_equal({id: "replacement"}, s[:shard][1])
      assert_equal({id: "S", version: 1}, s[:original][:shared])
      assert_same(s[:original][:shared], s[:shard][0])
      assert_same(s[:original], s[:original][:self])
    end
  end

  def test_replacing_one_shared_reference_does_not_touch_the_other
    shared = {id: "S"}
    build(shared: shared)
    store = PStore.new(@file)

    store.amend_replace(:shard, [0], {id: "new"})

    read do |s|
      assert_equal({id: "new"}, s[:shard][0])
      assert_equal({id: "S"}, s[:original][:shared])
    end
  end

  def test_cycles_survive_both_kinds_of_amendment
    shared = {id: "S", n: 1}
    build(shared: shared)
    store = PStore.new(@file)

    store.amend_shared(store.handle(:original, [:shared]), {id: "S", n: 2})
    store.amend_replace(:shard, [1], {id: "tail2"})

    read do |s|
      assert_same(s[:original], s[:original][:self])
      assert_same(s[:original][:shared], s[:shard][0])
      assert_equal({id: "S", n: 2}, s[:shard][0])
    end
  end

  def test_path_replacement_preserves_a_cycle_in_the_same_root
    cycle = {value: 1}
    cycle[:self] = cycle
    PStore.new(@file).transaction do |store|
      store[:root] = cycle
      store[:other] = cycle
    end
    store = PStore.new(@file)

    store.amend_replace(:root, [:value], 2)

    read do |s|
      assert_equal(2, s[:root][:value])
      assert_same(s[:root], s[:root][:self])
      assert_equal(1, s[:other][:value])
      assert_same(s[:other], s[:other][:self])
      refute_same(s[:root], s[:other])
    end
  end

  def test_untouched_shared_subgraphs_keep_their_identity
    inner = {value: 1}
    list = [inner, inner]
    PStore.new(@file).transaction do |store|
      store[:a] = {list: list, inner: inner}
      store[:b] = {list: list, inner: inner}
    end
    store = PStore.new(@file)

    store.amend_replace(:a, [:inner, :value], 2)
    read do |s|
      assert_same(s[:a][:list], s[:b][:list])
      assert_same(s[:a][:list][0], s[:a][:list][1])
      assert_same(s[:b][:inner], s[:b][:list][0])
      assert_equal(1, s[:b][:inner][:value])
    end

    store.amend_shared(store.handle(:b, [:inner]), {value: 3})
    read do |s|
      assert_equal(3, s[:b][:inner][:value])
      assert_equal(3, s[:b][:list][0][:value])
      assert_equal(2, s[:a][:inner][:value])
    end
  end

  def test_shared_data_record_update_reaches_both_roots
    record = Invoice.new(1, ["a"])
    build(shared: record)
    store = PStore.new(@file)

    store.amend_shared(store.handle(:original, [:shared]), Invoice.new(2, ["b"]))

    read do |s|
      assert_equal(Invoice.new(2, ["b"]), s[:original][:shared])
      assert_equal(Invoice.new(2, ["b"]), s[:shard][0])
      assert_same(s[:original][:shared], s[:shard][0])
    end
  end

  def test_path_replacement_keeps_an_indirect_data_cycle
    holder = {value: 1}
    data = Node.allocate
    data.send(:initialize, value: 1, holder: holder)
    holder[:outer] = data

    PStore.new(@file).transaction { |store| store[:root] = data }
    store = PStore.new(@file)
    store.amend_replace(:root, [:holder, :value], 2)

    read do |s|
      assert_equal(2, s[:root].holder[:value])
      assert_same(s[:root], s[:root].holder[:outer])
    end
  end

  def test_record_types_are_supported
    PStore::Incremental.record(Message)
    msg = Message.new(1, "one")
    build(shared: msg)
    store = PStore.new(@file)

    store.amend_replace(:original, [:shared], Message.new(2, "two"))

    read do |s|
      assert_equal(Message.new(2, "two"), s[:original][:shared])
      assert_equal(Message.new(1, "one"), s[:shard][0])
    end
  end

  def test_data_records_are_supported
    record = Invoice.new(1, ["a"])
    build(shared: record)
    store = PStore.new(@file)

    store.amend_replace(:original, [:shared], Invoice.new(2, ["b"]))

    read do |s|
      assert_equal(Invoice.new(2, ["b"]), s[:original][:shared])
      assert_equal(Invoice.new(1, ["a"]), s[:shard][0])
    end
  end

  def test_untrusted_values_are_rejected
    build(shared: {id: "S"})
    store = PStore.new(@file)

    assert_raise(PStore::AmendmentError) do
      store.amend_replace(:original, [:note], Object.new)
    end
    read { |s| assert_equal("before", s[:original][:note]) }
  end

  def test_only_affected_objects_are_written
    big = Array.new(2000) { |i| {id: i, body: "x" * 40} }
    small = {n: 0}
    PStore.new(@file).transaction do |store|
      store[:big] = big
      store[:small] = small
    end
    full_size = File.size(@file)
    store = PStore.new(@file)

    receipt = store.amend_replace(:small, [:n], 1)

    assert_true(receipt.bytes_written < full_size / 10,
                "journal frame #{receipt.bytes_written} should be far smaller than #{full_size}")
    read { |s| assert_equal(1, s[:small][:n]) }
  end

  def test_retry_of_same_amendment_is_idempotent
    build(shared: {id: "S"})
    store = PStore.new(@file)
    handle = store.handle(:original, [:shared])

    first = store.amend_shared(handle, {id: "S2"})
    second = store.amend_shared(handle, {id: "S2"})

    assert_true(first.applied?)
    assert_false(second.applied?)
    assert_equal(0, second.bytes_written)
    assert_equal(1, store.ledger_version)
  end

  def test_retry_with_a_freshly_resolved_handle_is_idempotent
    build(shared: {id: "S"})
    store = PStore.new(@file)

    store.amend_shared(store.handle(:original, [:shared]), {id: "S2"})

    again = store.amend_shared(store.handle(:original, [:shared]), {id: "S2"})
    assert_false(again.applied?)
    assert_equal(1, store.ledger_version)
  end

  def test_reused_token_with_different_payload_is_rejected
    build(shared: {id: "S"})
    store = PStore.new(@file)
    handle = store.handle(:original, [:shared])
    receipt = store.amend_shared(handle, {id: "A"}, token: "batch-1")

    fresh = store.handle(:original, [:shared])
    assert_raise(PStore::AmendmentError) do
      store.amend_shared(fresh, {id: "B"}, token: "batch-1")
    end
    assert_equal("batch-1", receipt.token)
  end

  def test_torn_tail_reads_the_complete_old_or_new_version
    build(shared: {id: "S", n: 0})
    store = PStore.new(@file)
    handle = store.handle(:original, [:shared])
    store.amend_shared(handle, {id: "S", n: 1})

    data = File.binread(@journal)
    File.binwrite(@journal, data.byteslice(0, data.bytesize - 7))

    read do |s|
      assert_equal(0, s[:original][:shared][:n])
      assert_equal(0, s[:shard][0][:n])
      assert_same(s[:original][:shared], s[:shard][0])
    end

    PStore.new(@file).amend_shared(handle, {id: "S", n: 1})

    read do |s|
      assert_equal(1, s[:original][:shared][:n])
      assert_equal(1, s[:shard][0][:n])
      assert_same(s[:original][:shared], s[:shard][0])
    end
  end

  def test_delivered_snapshot_is_a_plain_pstore_file
    shared = {id: "S", n: 1}
    build(shared: shared)
    store = PStore.new(@file)
    store.amend_shared(store.handle(:original, [:shared]), {id: "S", n: 2})
    store.amend_replace(:shard, [1], {id: "tail2"})

    destination = File.join(@dir, "delivered.pstore")
    store.deliver(destination)

    assert_false(File.exist?(PStore::Incremental.journal_path(destination)))
    legacy = PStore.new(destination)
    legacy.transaction(true) do |s|
      assert_equal({id: "S", n: 2}, s[:original][:shared])
      assert_equal({id: "tail2"}, s[:shard][1])
      assert_same(s[:original][:shared], s[:shard][0])
      assert_same(s[:original], s[:original][:self])
    end
  end

  def test_compact_merges_journal_into_plain_snapshot
    build(shared: {id: "S", n: 1})
    store = PStore.new(@file)
    store.amend_shared(store.handle(:original, [:shared]), {id: "S", n: 2})
    store.compact!

    assert_false(File.exist?(@journal))
    read do |s|
      assert_equal({id: "S", n: 2}, s[:original][:shared])
      assert_same(s[:original][:shared], s[:shard][0])
    end
    PStore.new(@file).transaction { |s| s[:later] = true }
    read { |s| assert_true(s[:later]) }
  end

  def test_many_amendments_do_not_reread_unchanged_roots_into_journal
    build(shared: {id: "S", n: 0})
    store = PStore.new(@file)
    20.times do |i|
      store.amend_replace(:original, [:note], "v#{i}")
    end
    assert_true(File.size(@journal) < 20_000)
    read { |s| assert_equal("v19", s[:original][:note]) }
  end
end
