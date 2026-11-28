# Runnable sample for the multi-route receiving interface.
#
# It uses ordinary Kiba transforms, including one that yields a row, mutates
# it, then returns a second row, and a transform that emits a marker from
# close. Run with:
#
#   ruby -Ilib examples/fanout_receiving.rb

$LOAD_PATH.unshift(File.expand_path("../lib", __dir__))
require "kiba"

module Sample
  ADJUSTMENT_BYTES = 24
  LINE_BYTES = 40

  class Source
    def initialize(rows)
      @rows = rows
    end

    def each
      @rows.each { |row| yield row }
    end
  end

  # Emits the first variant, then changes the row before returning the second.
  # A receiver that stopped at the first yield must not see the later change.
  class SplitLines
    def process(row)
      yield row.merge(phase: :yielded, lines: row[:lines].map(&:dup))
      row[:lines] = row[:lines].map { |line| line.merge(amount: line[:amount] + 100) }
      row.merge(phase: :returned)
    end
  end

  # Emits a close marker for an order it actually saw.
  class CloseMarker
    def initialize(order_id)
      @order_id = order_id
      @seen = false
    end

    def process(row)
      @seen = true if row[:order_id] == @order_id
      row
    end

    def close
      yield({order_id: @order_id, marker: :closed}) if @seen
    end
  end

  # All destinations append to one sink so the sample can show exactly what
  # each route observed.
  class Sink
    attr_reader :events

    def initialize(name)
      @name = name
      @events = []
    end

    def record(event)
      @events << event
    end
  end

  class Picking
    def initialize(sink)
      @sink = sink
    end

    def write(row)
      if row[:lines].nil?
        @sink.record({order_id: row[:order_id], marker: row[:marker]})
        return
      end

      row[:lines].each { |line| line[:picked] = true }
      @sink.record({order_id: row[:order_id], phase: row[:phase], lines: row[:lines].map(&:dup)})
    end

    def close
      @sink.record(:closed)
    end
  end

  class Review
    def initialize(sink)
      @sink = sink
    end

    def write(row)
      if row[:lines].nil?
        @sink.record({order_id: row[:order_id], marker: row[:marker]})
        return
      end

      @sink.record({
        order_id: row[:order_id],
        phase: row[:phase],
        picked: row[:lines].map { |line| line[:picked] }
      })
    end

    def close
      @sink.record(:closed)
    end
  end

  class Settlement
    def initialize(sink)
      @sink = sink
    end

    def write(row)
      if row[:lines].nil?
        @sink.record({order_id: row[:order_id], marker: row[:marker]})
        return
      end

      @sink.record({
        order_id: row[:order_id],
        phase: row[:phase],
        amounts: row[:lines].map { |line| line[:amount] },
        adjustments_id: row[:adjustments]&.__id__
      })
    end

    def close
      @sink.record(:closed)
    end
  end

  # Keeps the first detail reference it received and only copies the paper
  # page at close. Later rows must not rewrite that page.
  class Archiver
    def initialize(sink)
      @sink = sink
      @first = nil
      @first_adjustments_id = nil
    end

    def write(row)
      if row[:lines].nil?
        @sink.record({order_id: row[:order_id], marker: row[:marker]})
        return
      end

      @first ||= row
      @first_adjustments_id ||= row[:adjustments]&.__id__
      @sink.record({order_id: row[:order_id], phase: row[:phase], retained: true})
    end

    def close
      @sink.record(if @first
        {
          copied_at_close: @first[:order_id],
          lines: @first[:lines].map(&:dup),
          adjustments_id: @first_adjustments_id
        }
      else
        :closed
      end)
    end
  end

  module_function

  def build_work_order(sinks, close_order_id)
    Kiba::Fanout::WorkOrder.build do
      order_key "order_id"

      manifest do
        node "lines", bytes: LINE_BYTES, whole: true
        node "adjustments", bytes: ADJUSTMENT_BYTES, whole: true
      end

      # Picking and its review endpoint are one isolated group. The agreed
      # picked mark is visible to both, and to no other group.
      group :picking do
        destination Picking, sinks[:picking]
        destination Review, sinks[:review]
        reference "adjustments"
        mutable "lines.*.picked"
        require_mutation "lines.*.picked"
        on_write writes: ["lines.*.picked"]
      end

      group :settlement do
        destination Settlement, sinks[:settlement]
        reference "adjustments"
        on_write reads: ["lines.*.amount"]
      end

      group :archive do
        destination Archiver, sinks[:archive]
        reference "adjustments"
        retain_first "lines"
        close_only_if_received close_order_id
        on_close reads: ["lines.*.amount"]
      end
    end
  end

  def run(rows, close_marker_for: nil)
    sinks = {
      picking: Sink.new(:picking),
      review: Sink.new(:review),
      settlement: Sink.new(:settlement),
      archive: Sink.new(:archive)
    }
    work_order = build_work_order(sinks, close_marker_for)
    mux = nil

    job = Kiba.parse do
      source Source, rows
      transform SplitLines
      transform CloseMarker, close_marker_for if close_marker_for
      destination Kiba::Fanout::Multiplexer, work_order, on_init: ->(instance) { mux = instance }
    end
    Kiba.run(job)

    sinks.merge(mux: mux)
  end
end

if $PROGRAM_NAME == __FILE__
  def summarize(label, result)
    mux = result[:mux]
    puts "== #{label} =="
    puts "picking:    #{result[:picking].events.map { |e| e.is_a?(Hash) ? [e[:order_id], e[:phase], e[:marker]] : e }.inspect}"
    puts "review:     #{result[:review].events.map { |e| e.is_a?(Hash) ? [e[:order_id], e[:phase], e[:marker]] : e }.inspect}"
    puts "settlement: #{result[:settlement].events.map { |e| e.is_a?(Hash) ? [e[:order_id], e[:phase], e[:amounts], e[:adjustments_id], e[:marker]] : e }.inspect}"
    closed = result[:archive].events.last
    puts "archive:    copied_at_close=#{closed[:copied_at_close].inspect} " \
      "lines=#{closed[:lines].inspect} adjustments_id=#{closed[:adjustments_id].inspect}"
    puts "conflicts:  #{mux.conflicts.inspect}"
    puts "budget:     #{mux.minimum_copy_bytes} bytes"
    mux.budget[:decisions].each do |decision|
      puts "  #{decision[:group]} #{decision[:node]} #{decision[:bytes]} " \
        "#{decision[:orders].inspect} #{decision[:determiner]}"
    end
    puts
  end

  shared_lines = [{amount: 10}, {amount: 20}]
  shared_adjustments = [{reason: :price, amount: 5}]
  shared_rows = [
    {order_id: "A-1", lines: shared_lines, adjustments: shared_adjustments},
    {order_id: "A-2", lines: shared_lines, adjustments: shared_adjustments}
  ]
  summarize("same list mounted on two orders", Sample.run(shared_rows, close_marker_for: "A-1"))

  first_adjustments = [{reason: :price, amount: 5}]
  second_adjustments = [{reason: :price, amount: 5}]
  distinct_rows = [
    {order_id: "B-1", lines: [{amount: 10}], adjustments: first_adjustments},
    {order_id: "B-2", lines: [{amount: 20}], adjustments: second_adjustments}
  ]
  summarize("equal-value distinct lists", Sample.run(distinct_rows, close_marker_for: "B-1"))
end
