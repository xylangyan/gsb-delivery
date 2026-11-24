require_relative "helper"

class TestResumableCheckpointStore < Kiba::Test
  def test_hash_store_round_trip
    hash = {}
    store = Kiba::Checkpoint::HashStore.new(hash)
    store.save(a: 1, b: [2, 3])
    assert_equal({a: 1, b: [2, 3]}, hash)
    assert_equal({a: 1, b: [2, 3]}, store.load)
  end

  def test_memory_store_isolated_from_caller_mutations
    store = Kiba::Checkpoint::MemoryStore.new
    store.save(rows: [{a: 1}])
    loaded = store.load
    loaded[:rows] << {a: 2}
    assert_equal [{a: 1}], store.load[:rows]
  end

  def test_file_store_atomic_round_trip
    path = "test/tmp/checkpoint.bin"
    remove_files(path)
    store = Kiba::Checkpoint::FileStore.new(path)
    assert_nil store.load
    store.save(phase: :running)
    assert_equal({phase: :running}, store.load)
    store.clear
    assert_nil store.load
  end

  def test_status_of_empty_store
    state = Kiba::Checkpoint.status(Kiba::Checkpoint::MemoryStore.new)
    assert state.empty?
    assert_equal :new, state.phase
    refute state.pending?
  end
end
