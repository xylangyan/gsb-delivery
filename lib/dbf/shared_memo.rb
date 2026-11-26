# frozen_string_literal: true

require 'digest'
require 'fileutils'

module DBF
  # Plans one shared byte store that a dBase III reader and a FoxPro reader can
  # each read back with their own native code, from their own file name.
  #
  # dBase III memos start on 512-byte block boundaries, keep bytes up to a 0x1A
  # terminator, and drop NUL bytes. FoxPro memos start on the block size in
  # their file header (64 bytes in practice), carry an 8-byte type/length
  # header, and return exactly the declared length. A 512-byte dBase block holds
  # eight FoxPro blocks, so the room a dBase memo leaves after its terminator
  # holds FoxPro memos without changing the dBase readback.
  #
  # A plan is a list of segments, not a materialised image, so a large volume is
  # described and checked without writing one copy per candidate block set.
  # Verification reads the segments through the real memo classes.
  module SharedMemo
    Item = Struct.new(
      :record, :field, :dbf_offset, :width, :deleted, :old_block, :value,
      keyword_init: true
    ) do
      def to_h
        {record:, field:, deleted:, old_block:, width:, value_length: value&.bytesize}
      end
    end

    Segment = Struct.new(:offset, :length, :origin, :bytes, keyword_init: true) do
      def stop
        offset + length
      end

      def overlaps?(from, to)
        offset < to && stop > from
      end

      def bytes_at(from, to)
        bytes.byteslice(from - offset, to - from)
      end
    end

    Placement = Struct.new(:value, :offset, :block, :slots, keyword_init: true)

    Conflict = Struct.new(:side, :record, :field, :old_block, :new_block, :reason, keyword_init: true) do
      def to_h
        {side:, record:, field:, old_block:, new_block:, reason:}
      end
    end

    # One memo bank as it stands today: every physical record, deleted or not.
    class Bank
      attr_reader :name, :version, :block_size, :items, :max_size

      def initialize(name:, version:, block_size:, items:, max_size: nil)
        @name = name
        @version = version
        @block_size = block_size
        @items = items
        @max_size = max_size
      end

      def memo_class
        VersionConfig.new(version).memo_class
      end

      def reader(io)
        memo_class.new(io, version, max_size:)
      end

      def values
        items.filter_map(&:value)
      end

      def distinct_values
        values.map(&:b).uniq
      end

      def nil_count
        items.count { |item| item.value.nil? }
      end

      def empty_count
        items.count { |item| item.value && item.value.bytesize.zero? }
      end

      def deleted_items
        items.count(&:deleted)
      end

      def header_bytes
        memo_class == Memo::Foxpro ? 8 : 0
      end

      def slots_for(value)
        total = value.bytesize + header_bytes
        [(total + block_size - 1) / block_size, 1].max
      end
    end

    # Scans a table and its memo file through the library's own memo class.
    # Every physical record is visited, including records flagged deleted, so
    # their memo pointers are checked too.
    def self.scan(dbf, memo = nil, name: nil, max_size: nil)
      table = Table.new(dbf, memo)
      version = table.version
      memo = memo || discovered_memo(dbf)
      raise DBF::FileNotFoundError, "no memo file for #{File.basename(dbf.to_s)}" unless memo

      memo_reader = VersionConfig.new(version).memo_class.open(memo, version, max_size:)
      block_size = memo_reader.block_size
      header_length = table.header_length
      record_length = table.record_length
      columns = table.columns
      offsets = table.column_offsets
      memo_columns = columns.each_with_index.select { |column, _| column.type == 'M' }
      items = []
      File.open(dbf, 'rb') do |file|
        table.record_count.times do |index|
          base = header_length + index * record_length
          file.seek(base)
          flag = file.read(1)
          deleted = flag && flag.getbyte(0) == 0x2A
          memo_columns.each do |column, column_index|
            file.seek(base + 1 + offsets[column_index])
            pointer = decode_pointer(file.read(column.length), version)
            items << Item.new(
              record: index,
              field: column.name,
              dbf_offset: base + 1 + offsets[column_index],
              width: column.length,
              deleted: deleted,
              old_block: pointer,
              value: pointer.positive? ? memo_reader.get(pointer) : nil
            )
          end
        end
      end
      memo_reader.close
      table.close
      Bank.new(name: name || File.basename(dbf, '.*'), version:, block_size:, items:, max_size:)
    end

    def self.decode_pointer(raw, version)
      return 0 unless raw

      %w[30 31].include?(version) ? raw.unpack1('V').to_i : raw.to_i
    end

    def self.discovered_memo(dbf)
      path = dbf.is_a?(String) ? dbf : (dbf.path if dbf.respond_to?(:path))
      path && FileHandler.find_memo(path)
    end

    def self.encode_pointer(block, width, version)
      if %w[30 31].include?(version)
        [block].pack('V').ljust(width, "\x00")
      else
        block.to_s.rjust(width, ' ')
      end
    end

    def self.sha(value)
      value && Digest::SHA256.hexdigest(value.b)
    end

    # The shared store: dBase memos on 512-byte blocks, FoxPro memos on their
    # own block grid nested in the free bytes those dBase memos leave.
    #
    # allowed_dbase / allowed_foxpro limit which old block numbers may move:
    # nil allows every block to move; an array of ranges pins every other memo
    # at its current block, and the plan reports a conflict if that cannot be
    # honoured.
    class Plan
      attr_reader :dbase, :foxpro, :segments, :conflicts, :verification, :signed_spans, :empty_fox_offset

      def initialize(dbase:, foxpro:, allowed_dbase: nil, allowed_foxpro: nil,
                     header_slots: false, signed_spans: [])
        @dbase = dbase
        @foxpro = foxpro
        @allowed_dbase = allowed_dbase
        @allowed_foxpro = allowed_foxpro
        @header_slots = header_slots
        @signed_spans = signed_spans
        @segments = []
        @conflicts = []
        @dbase_placements = {}
        @fox_placements = {}
        @empty_fox_offset = nil
        place_fox_header
        place_signed_spans
        place_pinned
        place_dbase_memos
        place_fox_memos
        place_empty_fox_memo
        verify!
      end

      def size
        @segments.map(&:stop).max || 0
      end

      def header_slots?
        @header_slots
      end

      def dbase_blocks
        (size.to_f / dbase.block_size).ceil
      end

      def foxpro_blocks
        (size.to_f / foxpro.block_size).ceil
      end

      def io
        SegmentIO.new(@segments, size)
      end

      def materialize
        store = io
        store.read(size) || ''.b
      end

      def write_to(path)
        File.open(path, 'wb') do |file|
          store = io
          while (chunk = store.read(65_536)) && !chunk.empty?
            file.write(chunk)
          end
        end
        path
      end

      def bytes_at(offset, length)
        store = io
        store.seek(offset)
        store.read(length) || ''.b
      end

      def new_block(item, side)
        return 0 if item.value.nil?

        placement = side == :dbase ? @dbase_placements[item.value.b] : @fox_placements[item.value.b]
        placement && placement.block
      end

      # Which original byte ranges one receiver's block uses, and the value it
      # reads back. Works with either side's own block numbering.
      def provenance(side, block)
        bank = side == :dbase ? dbase : foxpro
        start = block * bank.block_size
        stop = start + bank.block_size
        used = @segments.select { |segment| segment.overlaps?(start, stop) }
        value = bank.reader(io).get(block)
        {
          side:, block:, bytes: start...stop,
          records: records_at(side, block),
          sources: used.sort_by(&:offset).map { |segment| describe(segment) },
          value:,
          value_length: value&.bytesize,
          value_sha256: SharedMemo.sha(value),
          truncated: bank.max_size ? value&.bytesize == bank.max_size : false
        }
      end

      # For each side, the old -> new block pairs and the ones that change.
      # The changed pairs are the block numbers the handover sheet must approve.
      def adjustments(side = nil)
        sides = side ? [side] : %i[dbase foxpro]
        sides.to_h do |name|
          bank = name == :dbase ? dbase : foxpro
          pairs = bank.items.reject { |item| item.old_block.zero? }.map do |item|
            [item.old_block, new_block(item, name)]
          end
          changed = pairs.select { |old, new| old != new }
          [name, {
            pairs: pairs.uniq.sort_by(&:first).to_h,
            changed: changed.uniq.sort_by(&:first).to_h,
            unchanged: pairs.uniq.count { |old, new| old == new }
          }]
        end
      end

      def report
        {
          pool_bytes: size,
          dbase_blocks: dbase_blocks,
          foxpro_blocks: foxpro_blocks,
          header_slots: header_slots?,
          dbase: bank_report(dbase),
          foxpro: bank_report(foxpro),
          verification:,
          conflicts: @conflicts.map(&:to_h),
          signed_spans: signed_span_report,
          adjustments: adjustments,
          segments: @segments.sort_by(&:offset).map { |segment| describe(segment) }
        }
      end

      # Writes a copy of a side's .dbf whose memo pointers name the new blocks,
      # so the shared store can be opened under that side's own memo extension.
      def rewrite_dbf(side, source, target)
        bank = side == :dbase ? dbase : foxpro
        FileUtils.cp(source, target)
        File.open(target, 'r+b') do |file|
          bank.items.each do |item|
            file.seek(item.dbf_offset)
            file.write(SharedMemo.encode_pointer(new_block(item, side), item.width, bank.version))
          end
        end
        target
      end

      private

      def place_fox_header
        header = "\x00".b * 512
        header[6, 2] = [foxpro.block_size].pack('n')
        add(0, 512, :fox_header, header)
        @fox_header_run = header_slots? ? [foxpro.block_size, (512 / foxpro.block_size) - 1] : nil
      end

      def place_signed_spans
        @signed_spans.each do |span|
          offset = span.fetch(:offset)
          bytes = span.fetch(:bytes).b
          if occupied?(offset, offset + bytes.bytesize) && bytes_at(offset, bytes.bytesize) != bytes
            @conflicts << Conflict.new(reason: "signed bytes at #{offset} differ")
            next
          end
          add(offset, bytes.bytesize, :signed, bytes)
        end
      end

      def place_pinned
        place_pinned_side(dbase, @allowed_dbase, :dbase)
        place_pinned_side(foxpro, @allowed_foxpro, :foxpro)
      end

      def place_pinned_side(bank, allowed, side)
        return if allowed.nil?

        bank.distinct_values.each do |value|
          old_block = bank.items.find { |item| item.value&.b == value && !item.old_block.zero? }&.old_block
          next if old_block.nil? || movable?(allowed, old_block)

          side == :dbase ? pin_dbase(value, old_block) : pin_fox(value, old_block)
        end
      end

      # A memo the sheet may not move must keep its old block number. If the
      # block is already taken, that is a conflict naming the records that
      # point at it; the memo is packed elsewhere and verification reports the
      # block as moved.
      def pin_dbase(value, old_block)
        offset = old_block * dbase.block_size
        length = value.bytesize + 1
        return place_dbase(value, old_block, :pinned) if can_place?(offset, length)

        block_conflict!(:dbase, old_block, "block #{old_block} is pinned but occupied")
      end

      def pin_fox(value, old_block)
        offset = old_block * foxpro.block_size
        length = foxpro.slots_for(value) * foxpro.block_size
        return place_fox(value, offset, :pinned) if can_place?(offset, length)

        block_conflict!(:foxpro, old_block, "block #{old_block} is pinned but occupied")
      end

      def can_place?(offset, length)
        @segments.none? { |segment| offset < segment.stop && offset + length > segment.offset }
      end

      def block_conflict!(side, old_block, reason)
        bank = side == :dbase ? dbase : foxpro
        @conflicts << Conflict.new(side:, old_block:, reason:)
        bank.items.select { |item| item.old_block == old_block }.each do |item|
          @conflicts << Conflict.new(side:, record: item.record, field: item.field,
                                     old_block:, reason:)
        end
      end

      def movable?(allowed, block)
        allowed.any? { |range| range.cover?(block) }
      end

      def place_dbase_memos
        values = dbase.distinct_values.reject { |value| @dbase_placements.key?(value) }
        values = values.sort_by { |value| -value.bytesize }
        cursor = 1
        values.each do |value|
          cursor = place_dbase(value, cursor, :dbase)
        end
        @dbase_end = (cursor * dbase.block_size)
      end

      def place_dbase(value, from_block, origin)
        return from_block if @dbase_placements.key?(value)

        blocks = (value.bytesize + 1 + dbase.block_size - 1) / dbase.block_size
        block = find_dbase_block(value, blocks, from_block)
        if block.nil?
          @conflicts << Conflict.new(side: :dbase, reason: 'no free block range for dBase memo')
          return from_block
        end
        offset = block * dbase.block_size
        add(offset, value.bytesize, origin, value) unless value.bytesize.zero?
        add(offset + value.bytesize, 1, :terminator, "\x1A".b)
        @dbase_placements[value] = Placement.new(value:, offset:, block:, slots: blocks)
        block + blocks
      end

      def find_dbase_block(value, blocks, from_block)
        candidate = from_block
        loop do
          span_start = candidate * dbase.block_size
          span_stop = span_start + value.bytesize + 1
          return candidate unless occupied?(span_start, span_stop)
          candidate += 1
        end
      end

      # FoxPro memos go, largest first, into the 64-byte runs the dBase memos
      # and pinned FoxPro memos leave free; whatever does not fit is appended
      # after the last used byte.
      def place_fox_memos
        values = foxpro.distinct_values.reject { |value| value.bytesize.zero? || @fox_placements.key?(value) }
        needs = values.map { |value| [value, foxpro.slots_for(value)] }.sort_by { |_, slots| -slots }
        runs = free_slot_runs
        use = Array.new(runs.size, 0)
        needs.each do |value, slots|
          index = runs.each_index.find { |i| runs[i][1] - use[i] >= slots }
          if index
            use[index] += slots
            offset = runs[index][0] + (use[index] - slots) * foxpro.block_size
            place_fox(value, offset, :foxpro)
          else
            place_fox_overflow(value, slots)
          end
        end
        @fox_end = @segments.map(&:stop).max || 0
      end

      def free_slot_runs
        runs = []
        runs << @fox_header_run if @fox_header_run
        floor = 512
        ceiling = @segments.map(&:stop).max || 512
        @segments.sort_by(&:offset).each do |segment|
          next if segment.stop <= floor
          if segment.offset > floor
            run = slot_run(floor, segment.offset)
            runs << run if run
          end
          floor = [floor, segment.stop].max
        end
        run = slot_run(floor, ceiling)
        runs << run if run
        runs
      end

      def slot_run(from, to)
        start = align(from, foxpro.block_size)
        slots = (to - start) / foxpro.block_size
        slots.positive? ? [start, slots] : nil
      end

      def place_fox_overflow(value, slots)
        cursor = align(@segments.map(&:stop).max || 0, foxpro.block_size)
        place_fox(value, cursor, :foxpro)
      end

      def place_fox(value, offset, origin)
        return if @fox_placements.key?(value)

        placement = Placement.new(value:, offset:, block: offset / foxpro.block_size,
                                  slots: foxpro.slots_for(value))
        @fox_placements[value] = placement
        add(offset, 8, :fox_header, [1, value.bytesize].pack('NN'))
        add(offset + 8, value.bytesize, origin, value) unless value.bytesize.zero?
      end

      # A FoxPro memo reads as "" only when its 8-byte header ends the file:
      # a length of zero means no memo, and any byte after the header becomes
      # content. The empty memo therefore sits last.
      def place_empty_fox_memo
        empty = foxpro.distinct_values.find { |value| value.bytesize.zero? }
        return unless empty

        cursor = align(@segments.map(&:stop).max || 0, foxpro.block_size)
        add(cursor, 8, :fox_header, [1, 1].pack('NN'))
        @empty_fox_offset = cursor
        @fox_placements[empty] = Placement.new(value: empty, offset: cursor,
                                               block: cursor / foxpro.block_size, slots: 1)
        @fox_end = cursor + 8
      end

      def add(offset, length, origin, bytes)
        return if length.zero?

        segment = Segment.new(offset:, length:, origin:, bytes: bytes.b)
        @segments.each do |other|
          next unless segment.overlaps?(other.offset, other.stop)
          next if same_bytes?(segment, other)

          @conflicts << Conflict.new(reason: "bytes at #{offset} overlap")
          return
        end
        @segments << segment
      end

      def same_bytes?(one, other)
        from = [one.offset, other.offset].max
        to = [one.stop, other.stop].min
        one.bytes_at(from, to) == other.bytes_at(from, to)
      end

      def occupied?(from, to)
        @segments.any? { |segment| segment.overlaps?(from, to) }
      end

      def verify!
        results = {}
        %i[dbase foxpro].each do |side|
          bank = side == :dbase ? dbase : foxpro
          allowed = side == :dbase ? @allowed_dbase : @allowed_foxpro
          reader = bank.reader(io)
          checked = 0
          failed = []
          bank.items.each do |item|
            next if item.old_block.zero?

            block = new_block(item, side)
            got = block.zero? ? nil : reader.get(block)
            checked += 1
            next if same_value?(got, item.value)
            next if block_pinned?(allowed, item.old_block, block)

            failed << item
            @conflicts << Conflict.new(side:, record: item.record, field: item.field,
                                       old_block: item.old_block, new_block: block,
                                       reason: 'native readback differs')
          end
          results[side] = {items: bank.items.size, checked:, failed: failed.size}
        end
        @verification = results
      end

      # A memo the sheet may not move that had to move anyway is a conflict,
      # even when its old block read differently.
      def block_pinned?(allowed, old_block, block)
        return false if block == old_block

        !allowed.nil? && !movable?(allowed, old_block)
      end

      def same_value?(got, expected)
        return true if got.nil? && expected.nil?
        return false if got.nil? || expected.nil?

        got.b == expected.b
      end

      def signed_span_report
        @signed_spans.map do |span|
          offset = span.fetch(:offset)
          bytes = span.fetch(:bytes).b
          {offset:, length: bytes.bytesize, match: bytes_at(offset, bytes.bytesize) == bytes}
        end
      end

      def records_at(side, block)
        bank = side == :dbase ? dbase : foxpro
        bank.items.select { |item| new_block(item, side) == block }.map do |item|
          {record: item.record, field: item.field, deleted: item.deleted, old_block: item.old_block}
        end
      end

      def bank_report(bank)
        {
          name: bank.name,
          version: bank.version,
          block_size: bank.block_size,
          records: bank.items.map(&:record).uniq.size,
          items: bank.items.size,
          deleted_items: bank.deleted_items,
          nil_items: bank.nil_count,
          empty_items: bank.empty_count,
          max_size: bank.max_size,
          distinct_values: bank.distinct_values.size,
          distinct_bytes: bank.distinct_values.sum(&:bytesize)
        }
      end

      def describe(segment)
        {offset: segment.offset, length: segment.length, origin: segment.origin,
         sha256: SharedMemo.sha(segment.bytes)}
      end

      def align(value, boundary)
        ((value + boundary - 1) / boundary) * boundary
      end
    end

    # Lazy IO view over a segment list. Gaps read as zeroes; the full image is
    # never built, so a large volume is checked without one copy per candidate
    # block set.
    class SegmentIO
      def initialize(segments, size)
        @segments = segments.sort_by(&:offset)
        @size = size
        @pos = 0
      end

      attr_reader :size

      def pos
        @pos
      end

      def seek(position)
        @pos = position
        self
      end

      def rewind
        @pos = 0
        self
      end

      def eof?
        @pos >= @size
      end

      def read(length = nil)
        length = @size - @pos if length.nil? && @pos < @size
        return ''.b if length.nil?
        return nil if @pos >= @size && length.positive?

        stop = [@pos + length, @size].min
        result = String.new(capacity: [stop - @pos, 0].max, encoding: Encoding::BINARY)
        position = @pos
        index = first_segment_covering(position)
        while index < @segments.length && @segments[index].offset < stop
          segment = @segments[index]
          from = [position, segment.offset].max
          to = [stop, segment.stop].min
          if to > from
            result << ("\x00".b * (from - position)) if from > position
            result << segment.bytes_at(from, to)
            position = to
          end
          index += 1
        end
        result << ("\x00".b * (stop - position)) if position < stop
        @pos = stop
        result
      end

      def close
        nil
      end

      def closed?
        false
      end

      private

      # Segments never overlap, so the first one ending after +position+ also
      # starts the covering run. Binary search keeps a read O(log n + spans)
      # instead of scanning the whole plan on every memo.
      def first_segment_covering(position)
        @segments.bsearch_index { |segment| segment.stop > position } || @segments.length
      end
    end
  end
end
