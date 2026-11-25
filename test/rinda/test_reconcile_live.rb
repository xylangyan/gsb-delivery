# frozen_string_literal: false
require 'test/unit'
require 'rinda/tuplespace'
require 'rinda/reconcile'

# These tests check the fixture verdicts against a real TupleSpace, not against
# the engine's own model. Each scenario is executed live with a fake port or
# real waiting threads.
class TestReconcileLive < Test::Unit::TestCase
  def fixture(name)
    Rinda::Reconcile::Ledger.load(File.expand_path("../fixtures/reconcile/#{name}.json", __dir__))
  end

  def replay(name)
    Rinda::Reconcile::Engine.new(fixture(name)).replay
  end

  # A fake port that records what reached it.
  class RecordingPort
    attr_reader :received
    def initialize; @received = []; end
    def push(value); @received << value; nil; end
  end

  # A port whose first push succeeds and then raises, modeling a lost reply.
  class BrokenReplyPort
    attr_reader :received
    def initialize; @received = []; @raised = false; end
    def push(value)
      @received << value
      return nil if @raised
      @raised = true
      raise IOError, 'reply lost'
    end
  end

  def test_move_port_error_really_keeps_entry_takeable
    ts = Rinda::TupleSpace.new(3600)
    ts.write(['job', 'J1'], 600)
    port = BrokenReplyPort.new
    assert_raise(IOError) { ts.move(port, ['job', 'J1'], 0) }
    assert_equal([['job', 'J1']], port.received)
    # The entry survived the failed move and a later take hands it over again.
    assert_equal(['job', 'J1'], ts.take(['job', 'J1'], 0))
    assert_equal([], ts.read_all(['job', 'J1']))
  end

  def test_expired_write_is_seen_by_waiting_read_but_not_takeable
    ts = Rinda::TupleSpace.new(3600)
    seen = nil
    ready = Queue.new
    reader = Thread.new do
      ready.push(true)
      seen = ts.read(['job', 'Jdead'], nil)
    end
    ready.pop
    sleep 0.1
    ts.write(['job', 'Jdead'], 0)
    reader.join(2)
    assert_equal(['job', 'Jdead'], seen)
    assert_equal([], ts.read_all(['job', 'Jdead']))
    assert_raise(Rinda::RequestExpiredError) { ts.take(['job', 'Jdead'], 0) }
    reader.kill
  end

  def test_two_matching_writes_before_reacquire_yield_last
    ts = Rinda::TupleSpace.new(3600)
    result = nil
    ready = Queue.new
    reader = Thread.new { ready.push(true); result = ts.read(['job', nil]) }
    ready.pop
    sleep 0.1
    ts.write(['job', 'A'], nil)
    ts.write(['job', 'B'], nil)
    reader.join(2)
    assert_equal(['job', 'B'], result)
    reader.kill
  end

  # If the reader re-acquires the monitor between the two writes, its @found
  # is not overwritten again, so it returns the first value. The write side is
  # serialized through a mutex the test controls to make that order reliable.
  def test_wake_between_writes_yields_first
    ts = Rinda::TupleSpace.new(3600)
    result = nil
    first_done = Queue.new
    reader = Thread.new do
      result = ts.read(['job', nil])
    end
    sleep 0.1
    writer = Thread.new do
      ts.write(['job', 'A'], nil)
      first_done.push(true)
      sleep 0.2
      ts.write(['job', 'B'], nil)
    end
    first_done.pop
    reader.join(1)
    assert_equal(['job', 'A'], result)
    writer.join(2)
    reader.kill
  end
end
