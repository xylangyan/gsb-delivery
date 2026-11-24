# A source which serves one page at a time, keyed by an opaque cursor. Reading
# a cursor again is always possible (cursors are reopenable), and the number of
# reads per cursor is recorded so tests can assert that confirmed pages are not
# read again.
class TestPagedSource
  Page = Kiba::Checkpoint::Page

  attr_reader :read_counts

  def initialize(pages)
    @pages = pages
    @read_counts = Hash.new(0)
  end

  def read_page(cursor)
    @read_counts[cursor] += 1
    rows, next_cursor = @pages.fetch(cursor)
    Page.new(rows, next_cursor)
  end
end

# A source whose confirmed cursors expire: a cursor placed in +discarded+ may
# not be read again. This models "pages read before the day ended are gone".
class TestExpiringPagedSource < TestPagedSource
  attr_reader :discarded

  def initialize(pages, discarded = [])
    super(pages)
    @discarded = discarded
  end

  def read_page(cursor)
    if discarded.include?(cursor)
      raise "page #{cursor.inspect} has expired"
    end
    super
  end
end

# A transform which explodes each row into two yields and then returns a
# distinct value, pinning down the exact ordering of the existing runner.
class TestYieldTwiceThenReturn
  def initialize(suffix = "returned")
    @suffix = suffix
  end

  def process(row)
    yield({id: "#{row[:id]}-a"})
    yield({id: "#{row[:id]}-b"})
    {id: "#{row[:id]}-#{@suffix}"}
  end
end

# Splits a row into several rows on yield and returns nothing.
class TestSplitTransform
  def initialize(count)
    @count = count
    @counter = 0
  end

  def process(row)
    @count.times do
      @counter += 1
      yield({id: "#{row[:id]}-#{@counter}"})
    end
    nil
  end

  def snapshot
    @counter
  end

  def restore(state)
    @counter = state
  end
end

# Buffers rows and emits them all on close, one at a time.
class TestBufferingTransform
  def initialize(tail = {id: "close-tail"})
    @tail = tail
    @buffer = []
  end

  def process(row)
    @buffer << row
    nil
  end

  def close
    @buffer << @tail
    @buffer.each { |row| yield row }
    @buffer = []
  end

  def snapshot
    @buffer.dup
  end

  def restore(state)
    @buffer = state
  end
end

# A close emitter which produces a lot of rows, to prove the runner hands them
# downstream as they are produced.
class TestManyCloseRows
  attr_reader :yielded

  def initialize(count)
    @count = count
    @yielded = 0
  end

  def close
    @count.times do |i|
      @yielded += 1
      yield({id: "close-#{i}"})
    end
  end

  def snapshot
    @yielded
  end

  def restore(state)
    @yielded = state
  end
end

# A close emitter whose rows are all values that a later close emitter will
# also produce. Both sets come from different layers and must be preserved.
class TestStaticCloseEmitter
  def initialize(rows)
    @rows = rows
    @emitted = 0
  end

  def process(row)
    row
  end

  def close
    @rows.drop(@emitted).each do |row|
      @emitted += 1
      yield row
    end
  end

  def snapshot
    @emitted
  end

  def restore(state)
    @emitted = state
  end
end

# Destination which records every write and can answer whether a write id
# landed. It records duplicate write ids so idempotency can be asserted.
class TestLedgerDestination
  attr_reader :rows, :write_ids, :duplicates

  def initialize(rows = [], write_ids = nil, landed = nil)
    @rows = rows
    @write_ids = write_ids || []
    @landed = landed || {}
    @duplicates = []
  end

  def write(row, write_id: nil)
    @duplicates << write_id if write_id && @rows.any? { |r| r[:write_id] == write_id }
    @rows << row.merge(write_id: write_id)
    @write_ids << write_id
    @landed[write_id] = true if write_id
  end

  def landed?(write_id)
    @landed[write_id] || @rows.any? { |r| r[:write_id] == write_id }
  end
end

# A destination which can stop before a write lands, and can report a write as
# in doubt (accepted by the remote system but not yet confirmed).
class TestFlakyDestination < TestLedgerDestination
  attr_accessor :fail_on_write

  def initialize(rows = [], write_ids = nil, landed = nil)
    super
    @fail_on_write = nil
    @doubtful_ids = []
  end

  def write(row, write_id: nil)
    if fail_on_write && fail_on_write.call(row, write_id)
      @doubtful_ids << write_id
      raise "destination stopped"
    end
    super
  end

  def landed?(write_id)
    return false if @doubtful_ids.include?(write_id) && !@landed[write_id]
    super
  end

  def confirm!(write_id)
    @landed[write_id] = true
    @doubtful_ids.delete(write_id)
  end
end

# Counts the close-emitted rows it sees and re-emits them unchanged, so a
# later layer can independently emit rows with the same values.
class TestPassThroughCloseEmitter
  def initialize(rows)
    @rows = rows
    @emitted = 0
  end

  def process(row)
    row
  end

  def close
    @rows.drop(@emitted).each do |row|
      @emitted += 1
      yield row.dup
    end
  end

  def snapshot
    @emitted
  end

  def restore(state)
    @emitted = state
  end
end

# Forwards to a destination object created by the test, so the test can keep
# inspecting the same instance the runner uses.
class TestDestinationRef
  def initialize(target)
    @target = target
  end

  def write(row, write_id: nil)
    @target.write(row, write_id: write_id)
  end

  def landed?(write_id)
    @target.landed?(write_id)
  end

  def close
    @target.close if @target.respond_to?(:close)
  end
end

# Forwards to a source object created by the test.
class TestSourceRef
  def initialize(target)
    @target = target
  end

  def read_page(cursor)
    @target.read_page(cursor)
  end
end

# A destination which stops writing after a given number of successful writes,
# simulating a stop mid-run. It still keeps landed ids so a restart can skip.
class TestStoppingDestination < TestLedgerDestination
  attr_accessor :stop_after

  def initialize(rows = [], write_ids = nil, landed = nil)
    super
    @stop_after = nil
  end

  def write(row, write_id: nil)
    if stop_after && @rows.size >= stop_after
      raise "stopped after #{stop_after} writes"
    end
    super
  end
end
