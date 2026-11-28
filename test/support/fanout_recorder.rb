module FanoutTestSupport
  class Source
    def initialize(rows)
      @rows = rows
    end

    def each
      @rows.each { |row| yield row }
    end
  end

  # Records a shallow view of each delivered row so later in-place mutations
  # cannot hide a leak. `action` runs before the snapshot is recorded.
  class Recorder
    attr_reader :name, :rows

    def initialize(name, action: nil, close_snapshot: false)
      @name = name.to_s
      @action = action
      @close_snapshot = close_snapshot
      @rows = []
      @last = nil
      @closed = false
    end

    def write(row)
      @action&.call(row)
      # A close-time archiver keeps the reference it received first and only
      # copies the page at close. Ordinary recorders snapshot on write.
      if @close_snapshot
        @first ||= row
      else
        @rows << snapshot(row)
      end
    end

    def close
      @closed = true
      @rows << snapshot(@first) if @close_snapshot && @first
    end

    def closed?
      @closed
    end

    # Build a destination class receiving this recorder. Kiba instantiates
    # destinations by class, so the recorder is passed as a constructor arg.
    def destination_class
      recorder = self
      Class.new do
        define_method(:write) { |row| recorder.write(row) }
        define_method(:close) { recorder.close }
      end
    end

    private

    def snapshot(row)
      {
        order_id: row[:order_id],
        marker: row[:marker],
        lines: row.fetch(:lines, []).map(&:dup),
        adjustments: row[:adjustments]&.map(&:dup),
        adjustments_id: row[:adjustments]&.__id__,
        lines_id: row.fetch(:lines, []).__id__
      }
    end
  end
end
