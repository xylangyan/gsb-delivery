# frozen_string_literal: true

require 'ox'

# Ox::ColdStoreDiff replays a cold-store page manifest through two fixed
# Ox.sax_html rule sets and reports which table cells ("bins") they disagree
# on inside a byte window of the expanded page.
#
# Manifest entry shapes:
#
#   { name: "head", bytes: "..." }                       one fragment reference
#   { name: "row", repeat: 3, fragments: [               a fragment group whose
#       { name: "front", bytes: "..." },                   fragments form one round
#       { name: "back",  bytes: "..." }] }                 repeated :repeat times
#
# A bare String is a one-off fragment. A plain fragment may carry :repeat,
# which is a group containing that single fragment.
#
# A repeated group always carries complete <tr>...</tr> framing, so the SAX
# element stack is back at the same depth at every round boundary. Round one is
# parsed physically; every later round is a byte-identical homologue reached by
# stride arithmetic, and whole runs of middle rounds are simply omitted from
# the played reference list rather than having their bytes fed to the parser.
#
# Rule set A puts "div" on :off. Rule set B puts "div" on :block and "hr" on
# :abort. Every other element keeps the Ox.sax_html default hint. Both decode
# character entities.
module Ox
  module ColdStoreDiff
    # Hard ceiling from the task statement: from opening the manifest to the
    # first answer, bytes read and bytes processed over the original page each
    # stay at or below this many bytes, even for a hundred million rounds.
    BUDGET = 512 * 1024

    # One named fragment. Bytes are frozen binary strings.
    class Fragment
      attr_reader :name, :bytes

      def initialize(name, bytes)
        @name = name.to_s
        @bytes = bytes.b.dup.freeze
      end

      def size
        @bytes.bytesize
      end
    end

    # A manifest entry: either a single reference or a group of fragments
    # repeated :rounds times as one round.
    class Segment
      attr_reader :fragments, :rounds

      def initialize(fragments, rounds)
        @fragments = fragments
        @rounds = rounds
        raise ArgumentError, 'segment needs at least one fragment' if @fragments.empty?
        raise ArgumentError, 'round count must be positive' unless @rounds.positive?
      end

      def stride
        @stride ||= @fragments.sum(&:size)
      end

      def span
        stride * @rounds
      end

      def repeated?
        @rounds > 1
      end
    end

    # Expanded-page geometry over the manifest without materializing the
    # expansion. Repeated groups are handled with stride arithmetic.
    class Manifest
      attr_reader :segments, :length

      def initialize(segments)
        @segments = segments
        @length = segments.sum(&:span)
        @cumulative = compute_cumulative
      end

      def self.build(raw_entries)
        segments = Array(raw_entries).each_with_index.map do |entry, idx|
          build_segment(entry, idx)
        end
        new(segments)
      end

      # Locate a zero-based expanded byte offset.
      #
      # Returns [slot, name, round, local_offset]: slot is the ordinal fragment
      # reference across the whole manifest, round is the 1-based round inside
      # its group (1 for one-off fragments), local_offset is the byte offset
      # inside the referenced fragment.
      def locate(offset)
        raise RangeError, "offset #{offset} outside page of #{@length} bytes" if offset.negative? || offset > @length

        seg_idx = @cumulative.bsearch_index { |cum| offset < cum } || @segments.size - 1
        segment = @segments[seg_idx]
        seg_base = segment_base(seg_idx)
        within = [offset - seg_base, segment.span - 1].min
        round = [within / segment.stride, segment.rounds - 1].min
        local_in_round = offset - seg_base - round * segment.stride
        frag_idx = fragment_index(segment, local_in_round)
        frag_start = segment.fragments[0...frag_idx].sum(&:size)
        local = offset == @length ? segment.fragments[frag_idx].size : local_in_round - frag_start
        slot = base_slot(seg_idx) + round * segment.fragments.size + frag_idx
        [slot, segment.fragments[frag_idx].name, round + 1, local]
      end

      # One anchor pattern inside a segment round.
      #
      # local_anchor    - anchor offset within one round
      # frag_idx/name   - fragment reference carrying the anchor '<'
      # local_in_frag   - anchor offset inside that fragment
      AnchorLocal = Struct.new(:seg_idx, :local_anchor, :frag_idx, :name, :local_in_frag, keyword_init: true)

      # Anchor pattern plus the rounds whose expanded anchor falls in a window.
      Template = Struct.new(:local, :round_lo, :round_hi, :first_anchor, keyword_init: true) do
        def count
          round_hi - round_lo + 1
        end
      end

      # Anchor templates intersecting the half-open window [lo, hi), in page
      # order of their first in-window anchor. Never iterates rounds: window
      # round ranges are derived arithmetically.
      def anchor_templates(lo, hi)
        lo = 0 if lo.negative?
        hi = @length if hi > @length
        raise ArgumentError, 'empty or invalid window' if lo >= hi

        templates = []
        @segments.each_with_index do |segment, seg_idx|
          base = segment_base(seg_idx)
          round_anchors(seg_idx).each do |local|
            r_lo = first_round(base, segment.stride, segment.rounds, local.local_anchor, lo)
            r_hi = last_round(base, segment.stride, segment.rounds, local.local_anchor, hi)
            next if r_lo.nil? || r_hi.nil? || r_lo > r_hi

            templates << Template.new(local: local,
                                      round_lo: r_lo,
                                      round_hi: r_hi,
                                      first_anchor: base + (r_lo - 1) * segment.stride + local.local_anchor)
          end
        end
        templates.sort_by(&:first_anchor)
      end

      def segment_base(seg_idx)
        seg_idx.zero? ? 0 : @cumulative[seg_idx - 1]
      end

      def base_slot(seg_idx)
        @segments[0...seg_idx].sum { |seg| seg.rounds * seg.fragments.size }
      end

      # Concrete fragment reference descriptor for one occurrence.
      Ref = Struct.new(:seg_idx, :round_idx, :frag_idx, :global_base, :slot, :name, :bytes, keyword_init: true)

      def refs_for_round(seg_idx, round_idx)
        segment = @segments[seg_idx]
        base = segment_base(seg_idx)
        segment.fragments.each_with_index.map do |fragment, frag_idx|
          frag_offset = segment.fragments[0...frag_idx].sum(&:size)
          Ref.new(seg_idx: seg_idx,
                  round_idx: round_idx,
                  frag_idx: frag_idx,
                  global_base: base + round_idx * segment.stride + frag_offset,
                  slot: base_slot(seg_idx) + round_idx * segment.fragments.size + frag_idx,
                  name: fragment.name,
                  bytes: fragment.bytes)
        end
      end

      private

      # Bytes that legally follow "<td" in a start tag.
      TD_DELIMITERS = [0x20, 0x09, 0x0A, 0x0D, 0x0C, 0x2F, 0x3E].freeze

      def self.build_segment(entry, idx)
        case entry
        when Segment
          entry
        when String
          Segment.new([Fragment.new("f#{idx}", entry)], 1)
        when Hash
          if entry.key?(:fragments)
            group_name = entry[:name] || "g#{idx}"
            fragments = Array(entry[:fragments]).each_with_index.map do |frag, j|
              build_fragment(frag, "#{group_name}#{j}")
            end
            Segment.new(fragments, entry[:repeat] || 1)
          else
            Segment.new([build_fragment(entry, entry[:name] || "f#{idx}")], entry[:repeat] || 1)
          end
        else
          raise ArgumentError, "unsupported manifest entry: #{entry.class}"
        end
      end

      def self.build_fragment(spec, default_name)
        case spec
        when Fragment
          spec
        when String
          Fragment.new(default_name, spec)
        when Hash
          if spec[:bytes].nil? && spec[:content].nil?
            raise ArgumentError, "fragment #{default_name} has no :bytes"
          end

          Fragment.new(spec[:name] || default_name, spec[:bytes] || spec[:content])
        else
          raise ArgumentError, "unsupported fragment: #{spec.class}"
        end
      end

      def compute_cumulative
        total = 0
        @segments.map { |seg| total += seg.span }
      end

      def fragment_index(segment, local_in_round)
        offset = local_in_round
        segment.fragments.each_with_index do |fragment, idx|
          return idx if offset < fragment.size

          offset -= fragment.size
        end
        segment.fragments.size - 1
      end

      # Local anchor offsets inside one round of each segment.
      #
      # The distinct fragments are concatenated once in manifest order (their
      # total stays within the 32 KiB distinct-byte bound) and scanned as one
      # representative string, so a tag split on any fragment or segment edge
      # is found exactly once and mapped back to the fragment owning its '<'.
      def all_anchors
        @all_anchors ||= build_all_anchors
      end

      def round_anchors(seg_idx)
        all_anchors.select { |anchor| anchor.seg_idx == seg_idx }
      end

      def build_all_anchors
        joined = ''.b
        owners = [] # [distinct_start, seg_idx, frag_idx, segment_distinct_base]
        seg_base = 0
        @segments.each_with_index do |segment, seg_idx|
          segment.fragments.each_with_index do |fragment, frag_idx|
            owners << [joined.bytesize, seg_idx, frag_idx, seg_base]
            joined << fragment.bytes
          end
          seg_base += segment.stride
        end
        starts = owners.map(&:first)
        scan_real_td_anchors(joined).map do |pos|
          owner_idx = starts.bsearch_index { |start| start > pos } || starts.size
          owner_idx -= 1
          distinct_start, seg_idx, frag_idx, segment_base = owners[owner_idx]
          fragment = @segments[seg_idx].fragments[frag_idx]
          AnchorLocal.new(seg_idx: seg_idx,
                          local_anchor: pos - segment_base,
                          frag_idx: frag_idx,
                          name: fragment.name,
                          local_in_frag: pos - distinct_start)
        end
      end

      # Real <td> start-tag anchors only. Comments, CDATA sections and quoted
      # attribute values can contain the same bytes without being tags, so the
      # scan is a small markup state machine rather than a plain regex.
      def scan_real_td_anchors(bytes)
        positions = []
        index = 0
        size = bytes.bytesize
        while index < size
          if bytes.getbyte(index) == 0x3C # '<'
            next_byte = bytes.getbyte(index + 1)
            if sub_bytes?(bytes, index, '<!--')
              close = bytes.index('-->', index + 4)
              index = close ? close + 3 : size
              next
            elsif sub_bytes?(bytes, index, '<![CDATA[')
              close = bytes.index(']]>', index + 9)
              index = close ? close + 3 : size
              next
            elsif markup_start?(next_byte)
              positions << index if td_tag_at?(bytes, index)
              index = skip_markup(bytes, index)
              next
            end
          end
          index += 1
        end
        positions
      end

      def markup_start?(byte)
        return false if byte.nil?

        byte == 0x21 || # '!'
          byte == 0x2F || # '/'
          byte == 0x3F || # '?'
          (byte >= 0x41 && byte <= 0x5A) || # A-Z
          (byte >= 0x61 && byte <= 0x7A) # a-z
      end

      def sub_bytes?(bytes, index, literal)
        bytes.byteslice(index, literal.bytesize) == literal
      end

      def td_tag_at?(bytes, index)
        first = bytes.getbyte(index + 1)
        second = bytes.getbyte(index + 2)
        third = bytes.getbyte(index + 3)
        ascii_letter?(first, 0x74) && # 't' or 'T'
          ascii_letter?(second, 0x64) && # 'd' or 'D'
          TD_DELIMITERS.include?(third)
      end

      def ascii_letter?(byte, lower)
        byte == lower || byte == lower - 0x20
      end

      # Walk one markup construct from its '<' to just past the closing '>',
      # honoring quoted attribute values so a '<' or '>' inside them does not
      # end the construct early.
      def skip_markup(bytes, index)
        quote = nil
        cursor = index + 1
        size = bytes.bytesize
        while cursor < size
          byte = bytes.getbyte(cursor)
          if quote
            quote = nil if byte == quote
          elsif byte == 0x22 || byte == 0x27 # '"' or "'"
            quote = byte
          elsif byte == 0x3E # '>'
            return cursor + 1
          end
          cursor += 1
        end
        size
      end

      # Smallest round r (1-based) with base + (r-1)*stride + local >= lo.
      def first_round(base, stride, rounds, local, lo)
        diff = lo - base - local
        round = diff.positive? ? (diff + stride - 1) / stride + 1 : 1
        round <= rounds ? round : nil
      end

      # Largest round r with base + (r-1)*stride + local < hi.
      def last_round(base, stride, rounds, local, hi)
        diff = hi - 1 - base - local
        return nil if diff.negative?

        round = (diff / stride) + 1
        [round, rounds].min
      end
    end

    # One cell record as produced by one rule set. Positions are zero-based
    # offsets in the expanded page.
    class Cell
      attr_accessor :anchor, :id, :text, :raw_start, :raw_end, :close_pos, :status, :abort_pos

      def initialize(anchor)
        @anchor = anchor
        @id = nil
        @text = ''.b
        @raw_start = nil
        @raw_end = nil
        @close_pos = nil
        @status = :open
        @abort_pos = nil
      end

      def add_text(decoded, run_start, run_end)
        @text << decoded
        @raw_start = run_start if @raw_start.nil? || run_start < @raw_start
        @raw_end = run_end if @raw_end.nil? || run_end > @raw_end
      end

      def byte_range
        @raw_start.nil? ? nil : [@raw_start, @raw_end]
      end

      def shifted(delta)
        copy = Cell.new(@anchor + delta)
        copy.id = @id
        copy.text = @text.dup
        copy.raw_start = @raw_start.nil? ? nil : @raw_start + delta
        copy.raw_end = @raw_end.nil? ? nil : @raw_end + delta
        copy.close_pos = @close_pos.nil? ? nil : @close_pos + delta
        copy.abort_pos = @abort_pos.nil? ? nil : @abort_pos + delta
        copy.status = @status
        copy
      end
    end

    # Maps the virtual byte stream Ox parses (only played rounds, concatenated)
    # onto real expanded-page offsets. Jumped rounds simply do not appear in
    # the virtual stream, so no skip bookkeeping is needed inside readpartial.
    class PositionMap
      def initialize
        # [virtual_start, virtual_end, global_base]
        @segments = []
        @virtual_cursor = 0
      end

      def add_played(bytes, global_base)
        @segments << [@virtual_cursor, @virtual_cursor + bytes.bytesize, global_base]
        @virtual_cursor += bytes.bytesize
      end

      def global_offset(virtual_offset)
        # Played segments are contiguous and ordered in virtual space, so the
        # first segment ending after virtual_offset contains it.
        seg = @segments.bsearch { |_start, stop, _base| virtual_offset < stop } || @segments.last
        start, _stop, base = seg
        base + (virtual_offset - start)
      end
    end

    # SAX handler recording td cell lifecycles for one overlay.
    #
    # Ox callback @pos is a one-based count over the virtual played stream;
    # real_pos() translates it through the playback PositionMap.
    class BinHandler < Ox::Sax
      attr_reader :cells, :abort_pos, :aborted
      attr_accessor :raw_window, :position_map

      def initialize
        super()
        @cells = []
        @open = []
        @abort_pos = nil
        @aborted = false
        @synthetic_close = false
        @pos = nil
        @line = nil
        @column = nil
      end

      def start_element(name)
        return unless name == :td

        @open << Cell.new(real_pos)
      end

      def attr(name, value)
        return unless name == :id && !@open.empty?

        @open.last.id = value
      end

      def text(value)
        return if @open.empty?

        run_start = real_pos
        run_end = @raw_window.next_markup(run_start)
        @open.last.add_text(value.b, run_start, run_end)
      end

      def end_element(name)
        return unless name == :td

        cell = @open.pop
        return if cell.nil?

        if @synthetic_close
          # EOF sweep: Ox closes the element mechanically but no row was ever
          # handed off, so the cell stays open without a close position.
          cell.status = :open
        else
          cell.close_pos = real_pos
          cell.status = :delivered
        end
        @cells << cell
      end

      def abort(name)
        @aborted = true
        # Ox does not refresh @pos on the abort path, so locate the abort tag
        # in the original bytes instead (it may be split across references).
        @abort_pos = @raw_window.last_anchor_for(name.to_s)
        @open.each { |cell| cell.abort_pos = @abort_pos }
      end

      def error(message, _line, _column)
        # The EOF sweep emits "not closed" immediately before synthetic ends.
        @synthetic_close = message.include?('not closed')
      end

      def finish
        @open.each do |cell|
          # Start callback arrived but no end callback ever did.
          cell.status = :open
          @cells << cell
        end
        @open.clear
        @cells.sort_by!(&:anchor)
      end

      def cell_by_anchor
        @cells.each_with_object({}) { |cell, h| h[cell.anchor] = cell }
      end

      private

      def real_pos
        @position_map.global_offset(@pos - 1)
      end
    end

    # Remembers the original bytes physically fed to Ox, keyed by their real
    # expanded-page offsets. Jumped rounds leave gaps; a text run and its
    # closing '<' always sit in one continuous played segment because jumps
    # happen only at round boundaries between complete tags.
    class RawWindow
      def initialize
        @segments = []
      end

      def push(bytes, global_start)
        @segments << [global_start, bytes.b.dup]
      end

      # Offset of the first '<' at or after offset, i.e. the markup that closes
      # the text run starting at offset. A run can span consecutive references
      # (a split entity), so the search continues into contiguous segments but
      # never crosses the gap left by a jumped round. Falls back to the end of
      # the last contiguous segment.
      def next_markup(offset)
        start_idx = @segments.index { |base, bytes| offset >= base && offset < base + bytes.bytesize }
        return offset if start_idx.nil?

        start_idx.upto(@segments.size - 1) do |idx|
          base, bytes = @segments[idx]
          if idx > start_idx
            prev_base, prev_bytes = @segments[idx - 1]
            break unless prev_base + prev_bytes.bytesize == base
          end
          local = idx == start_idx ? offset - base : 0
          marker = bytes.index('<', local)
          return base + marker unless marker.nil?
        end
        base, bytes = @segments[start_idx]
        base + bytes.bytesize
      end

      # Global offset of the abort tag for element name. Prefetching can leave
      # future, non-contiguous references in the window, so the stored segments
      # are split into maximal contiguous runs and the first real tag in each
      # run is found; the abort is the earliest such tag in page order. A tag
      # split across contiguous references is handled by scanning the joined
      # run rather than one segment.
      def last_anchor_for(name)
        candidates = []
        each_contiguous_run do |base, joined|
          pos = first_real_tag(joined, name)
          candidates << base + pos unless pos.nil?
        end
        candidates.min
      end

      def each_contiguous_run
        run = []
        flush = lambda do
          return if run.empty?

          joined = ''.b
          run.each { |_base, bytes| joined << bytes }
          yield(run.first[0], joined)
          run = []
        end
        @segments.each do |base, bytes|
          if run.any?
            prev_base, prev_bytes = run.last
            flush.call unless prev_base + prev_bytes.bytesize == base
          end
          run << [base, bytes]
        end
        flush.call
      end

      # First tag match outside comments. The abort tag is the first real tag
      # of its name the parser reaches; identical-looking text inside
      # <!-- ... --> must not count.
      def first_real_tag(bytes, name)
        tag_re = /<#{Regexp.escape(name)}(?=[\s\/>])/i
        scan_pos = 0
        while (comment = bytes.index('<!--', scan_pos))
          tag = tag_re.match(bytes, scan_pos)
          if tag && tag.begin(0) < comment
            return tag.begin(0)
          end
          close = bytes.index('-->', comment + 4)
          return nil if close.nil?

          scan_pos = close + 3
        end
        tag = tag_re.match(bytes, scan_pos)
        tag&.begin(0)
      end
    end

    # readpartial IO over the played fragment references. Every reference is
    # registered with the PositionMap and mirrored into the RawWindow under its
    # real global base, so jumped rounds leave explicit gaps in both.
    class PlaybackIO
      attr_reader :bytes_read

      def initialize(refs, position_map, raw_window)
        @refs = refs
        @position_map = position_map
        @raw_window = raw_window
        @pending = ''.b
        @bytes_read = 0
      end

      def readpartial(max)
        while @pending.bytesize < max && !@refs.empty?
          ref = @refs.shift
          @position_map.add_played(ref.bytes, ref.global_base)
          @raw_window.push(ref.bytes, ref.global_base)
          @pending << ref.bytes
        end
        raise EOFError if @pending.empty?

        out = @pending.byteslice(0, max)
        @pending = @pending.byteslice(out.bytesize..) || ''.b
        @bytes_read += out.bytesize
        out
      end
    end

    Side = Struct.new(:status, :id, :text, :byte_range, :close_pos, :abort_pos, keyword_init: true)

    # One template-level difference: the round-1 detail plus the in-window
    # rounds that repeat it homologously.
    Diff = Struct.new(:anchor, :slot, :ref, :round, :local_offset,
                      :a, :b, :reasons, :round_lo, :round_hi, :cell_count, keyword_init: true)

    Report = Struct.new(:count, :first, :templates, :anchors_total, :bytes_read,
                        :bytes_processed, :within_budget, :page_length, keyword_init: true)

    class << self
      # Compare the two fixed rule sets over a window of the expanded page.
      #
      # manifest - Array of manifest entries (see Manifest.build)
      # window   - [lo, hi) zero-based expanded byte offsets; nil hi means the
      #            end of the page manifest, which is the only page tail.
      #
      # Returns a Report. :count is the number of differing cells in the
      # window and :first is the earliest differing cell with both sides'
      # status, id, decoded text, original byte range and close position.
      def diff(manifest, window)
        manifest = Manifest.build(manifest)
        lo, hi = window
        hi = manifest.length if hi.nil?
        raise ArgumentError, 'empty or invalid window' if lo.negative? || hi > manifest.length || lo >= hi

        plan = PlaybackPlan.new(manifest)
        templates = manifest.anchor_templates(lo, hi)

        handler_a = run_side(manifest, plan, { 'div' => :off })
        handler_b = run_side(manifest, plan, { 'div' => :block, 'hr' => :abort })

        differences = build_differences(manifest, templates, handler_a, handler_b)
        total_cells = templates.sum(&:count)
        Report.new(count: differences.sum(&:cell_count),
                   first: differences.first,
                   templates: differences,
                   anchors_total: total_cells,
                   bytes_read: plan.bytes_read,
                   bytes_processed: plan.bytes_read,
                   within_budget: plan.bytes_read <= BUDGET,
                   page_length: manifest.length)
      end

      private

      def run_side(manifest, plan, overlay_changes)
        handler = BinHandler.new
        raw_window = RawWindow.new
        position_map = PositionMap.new
        handler.raw_window = raw_window
        handler.position_map = position_map
        io = PlaybackIO.new(plan.refs.dup, position_map, raw_window)
        overlay = Ox.sax_html_overlay.merge(overlay_changes)
        Ox.sax_html(handler, io, convert_special: true, overlay: overlay)
        handler.finish
        plan.observe(io.bytes_read)
        handler
      end

      def build_differences(manifest, templates, handler_a, handler_b)
        known_a = handler_a.cell_by_anchor
        known_b = handler_b.cell_by_anchor
        templates.filter_map do |template|
          local = template.local
          segment = manifest.segments[local.seg_idx]
          base = manifest.segment_base(local.seg_idx)
          sample_anchor = base + local.local_anchor
          a = known_a[sample_anchor] || missing(sample_anchor)
          b = known_b[sample_anchor] || missing(sample_anchor)
          # The abort is a single real event in round one, not a per-round
          # homologue, so its offset is reported verbatim on every missing cell.
          abort_anchor = b.abort_pos || (handler_b.abort_pos if b.status == :no_callback && handler_b.aborted)
          reasons = compare_cells(a, b)
          next if reasons.empty?

          first_round = template.round_lo
          first_anchor = template.first_anchor
          delta = (first_round - 1) * segment.stride
          slot = manifest.base_slot(local.seg_idx) +
                 (first_round - 1) * segment.fragments.size + local.frag_idx
          side_a = to_side(a, delta)
          side_b = to_side(b, delta)
          side_b.abort_pos = abort_anchor
          Diff.new(anchor: first_anchor,
                   slot: slot,
                   ref: local.name,
                   round: first_round,
                   local_offset: local.local_in_frag,
                   a: side_a,
                   b: side_b,
                   reasons: reasons,
                   round_lo: template.round_lo,
                   round_hi: template.round_hi,
                   cell_count: template.count)
        end
      end

      def compare_cells(a, b)
        return ["status: #{a.status} vs #{b.status}"] if a.status != b.status

        reasons = []
        reasons << 'id differs' if a.id != b.id
        reasons << 'text differs' if a.text != b.text
        reasons << 'byte range differs' if a.byte_range != b.byte_range
        reasons << 'close position differs' if a.close_pos != b.close_pos
        reasons
      end

      def missing(anchor)
        cell = Cell.new(anchor)
        cell.status = :no_callback
        cell
      end

      def to_side(cell, delta)
        Side.new(status: cell.status,
                 id: cell.id,
                 text: cell.text.dup,
                 byte_range: cell.byte_range&.then { |lo, hi| [lo + delta, hi + delta] },
                 close_pos: cell.close_pos.nil? ? nil : cell.close_pos + delta,
                 abort_pos: cell.abort_pos.nil? ? nil : cell.abort_pos + delta)
      end
    end

    # Builds the O(segment count) reference list physically fed to Ox. Each
    # repeated group plays round one, jumps all middle rounds, and plays the
    # final round so the parser reaches later segments with a correct element
    # stack. The jumped rounds never appear in the list at all.
    class PlaybackPlan
      attr_reader :refs, :bytes_read

      def initialize(manifest)
        @manifest = manifest
        @bytes_read = 0
        @refs = build_refs
      end

      def observe(read_bytes)
        @bytes_read = [@bytes_read, read_bytes].max
      end

      private

      def build_refs
        refs = []
        @manifest.segments.each_with_index do |segment, seg_idx|
          refs.concat(@manifest.refs_for_round(seg_idx, 0))
          next unless segment.repeated?

          if segment.rounds > 1
            refs.concat(@manifest.refs_for_round(seg_idx, segment.rounds - 1))
          end
        end
        refs
      end
    end
  end
end
