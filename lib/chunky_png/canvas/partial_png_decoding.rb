# frozen-string-literal: true

module ChunkyPNG
  class Canvas
    # Forensic decoding of PNG pixel streams that are only partially available.
    #
    # Unlike {ChunkyPNG::Canvas::PNGDecoding}, which requires the complete,
    # inflated pixel stream, this module works from scattered *fragments* of
    # the filtered byte stream. Each fragment knows its absolute start
    # position in the stream that a complete decode would have produced.
    #
    # The decoder reconstructs every byte that the PNG filtering rules, the
    # previous scanline and the palette boundaries allow, and reports exactly
    # which pixel colors are proven regardless of the unobserved bytes.
    # Positions that could take more than one color stay in an explicit
    # "unknown" state; contradictions that make every candidate assembly
    # illegal are reported separately.
    #
    # The current scope matches the sorting-line recovery case: indexed
    # images (color mode 3) at 1, 2, 4 and 8 bit depth, both non-interlaced
    # and Adam7 interlaced. Complete PNGs still go through the regular
    # decoding path.
    #
    # @see ChunkyPNG::Canvas::PNGDecoding
    module PartialPNGDecoding
      # A contradiction discovered while assembling or decoding fragments.
      #
      # Contradictions are value objects, not raised exceptions: a forensic
      # decode should report everything that is wrong in one pass.
      class Contradiction
        # @return [String] Human readable description of the contradiction.
        attr_reader :message

        # @return [Array<Integer>] Stream positions involved, if any.
        attr_reader :positions

        # @return [Array<String>] Transfer record ids involved, if any.
        attr_reader :records

        def initialize(message, positions: [], records: [])
          @message = message
          @positions = positions
          @records = records
        end

        def to_s
          location = []
          location << "positions #{positions.join(", ")}" unless positions.empty?
          location << "records #{records.join(", ")}" unless records.empty?
          suffix = location.empty? ? "" : " (#{location.join("; ")})"
          "#{message}#{suffix}"
        end

        def ==(other)
          other.is_a?(Contradiction) &&
            message == other.message &&
            positions == other.positions &&
            records == other.records
        end

        alias eql? ==

        def hash
          [message, positions, records].hash
        end
      end

      # One contiguous observed fragment of the filtered pixel stream.
      class TransferFragment
        # @return [Integer] Absolute position of the first fragment byte in
        #   the complete filtered stream.
        attr_reader :start_pos

        # @return [String] The observed bytes, stored as a binary string.
        attr_reader :bytes

        # @return [String] Id of the {TransferRecord} this fragment belongs to.
        attr_reader :record_id

        def initialize(start_pos, bytes, record_id)
          @start_pos = start_pos.to_i
          @bytes = bytes.to_s.dup.b
          @record_id = record_id
          raise ArgumentError, "Fragment start positions cannot be negative!" if @start_pos.negative?
          raise ArgumentError, "Fragment bytes cannot be empty!" if @bytes.empty?
        end

        # @return [Integer] Absolute position of the last fragment byte.
        def end_pos
          start_pos + bytes.bytesize - 1
        end

        # @return [Range<Integer>] Range of absolute stream positions covered.
        def range
          start_pos..end_pos
        end

        # @param attrs [Hash] Hash representation accepted by the entry points.
        # @param record_id [String] Id of the owning transfer record.
        # @return [TransferFragment]
        def self.from_attrs(attrs, record_id)
          new(attrs.fetch(:start_pos), attrs.fetch(:bytes), record_id)
        end
      end

      # A transfer record: one read of the filtered stream, possibly split
      # into several separated fragments.
      #
      # Records that share a {#group} are alternative versions of the same
      # read: exactly one of them can be part of any legal assembly. Records
      # without a group are unconditional and always participate.
      class TransferRecord
        # @return [String] Unique identifier of the record.
        attr_reader :id

        # @return [String, nil] Group identifier for mutually exclusive reads.
        attr_reader :group

        # @return [Array<TransferFragment>] Fragments observed in this record.
        attr_reader :fragments

        def initialize(id, group: nil, fragments: [])
          @id = id.to_s
          @group = group.nil? ? nil : group.to_s
          @fragments = []
          fragments.each { |fragment| add_fragment(fragment) }
        end

        # Adds a fragment to this record.
        # @param fragment [TransferFragment, Hash] A fragment or a Hash with
        #   +:start_pos+ and +:bytes+ keys.
        # @return [TransferFragment] the added fragment.
        def add_fragment(fragment)
          fragment = TransferFragment.from_attrs(fragment, id) unless fragment.is_a?(TransferFragment)
          raise ArgumentError, "Fragment belongs to another transfer record!" unless fragment.record_id == id
          @fragments << fragment
          fragment
        end

        # @return [Array<Range<Integer>>] Positions covered by this record.
        def covered_ranges
          fragments.map(&:range)
        end

        # @param attrs [Hash] Hash representation accepted by the entry points.
        # @return [TransferRecord]
        def self.from_attrs(attrs)
          record = new(attrs.fetch(:id), group: attrs[:group])
          Array(attrs[:fragments]).each { |fragment| record.add_fragment(fragment) }
          record
        end
      end

      # Sparse map of the filtered stream bytes for one concrete assembly of
      # transfer records.
      #
      # The map enumerates every legal way to pick one version record out of
      # each mutually exclusive group. Every yielded observation map contains
      # exactly the bytes that the chosen assembly can vouch for; every other
      # stream position is simply absent and therefore unobserved.
      class ByteMap
        # @return [Array<TransferRecord>] All known transfer records.
        attr_reader :records

        # @return [Array<Contradiction>] Contradictions between unconditional
        #   (ungrouped and confirmed) observations.
        attr_reader :contradictions

        def initialize(records)
          @records = records
          @contradictions = []
          @unconditional = {}
          @unconditional_sources = {}
          @groups = {}
          build_unconditional_map!
          build_group_variants!
        end

        # Enumerates every legal assembly as a pair of an observation map and
        # the chosen group records.
        #
        # @yieldparam observed [Hash{Integer => Integer}] Map of absolute
        #   stream position to the observed byte value.
        # @yieldparam chosen [Hash{String => TransferRecord}] The version
        #   record chosen for every group.
        # @return [Enumerator, nil] An enumerator when called without a block.
        def each_solution(&block)
          return enum_for(:each_solution) unless block

          return if @contradictions.any?

          backtrack(@groups.keys, 0, @unconditional.dup, {}, &block)
        end

        private

        def build_unconditional_map!
          records.each do |record|
            next if record.group
            merge_record!(record, @unconditional, @unconditional_sources, @contradictions)
          end
        end

        def build_group_variants!
          records.each do |record|
            next unless record.group
            @groups[record.group] ||= []
            @groups[record.group] << record
          end
        end

        def merge_record!(record, observed, sources, contradictions)
          record.fragments.each do |fragment|
            fragment.bytes.each_byte.with_index do |byte, offset|
              position = fragment.start_pos + offset
              if observed.key?(position) && observed[position] != byte
                contradictions << Contradiction.new(
                  "Conflicting observed byte at stream position #{position}",
                  positions: [position],
                  records: [sources[position], record.id].uniq,
                )
              else
                observed[position] = byte
                sources[position] = record.id
              end
            end
          end
        end

        def backtrack(group_names, index, observed, chosen, &block)
          if index == group_names.length
            yield observed, chosen
            return
          end

          group = group_names[index]
          @groups[group].each do |record|
            next_observed, conflicts = merge_variant(record, observed)
            next if conflicts.any?

            chosen[group] = record
            backtrack(group_names, index + 1, next_observed, chosen, &block)
            chosen.delete(group)
          end
        end

        def merge_variant(record, observed)
          merged = observed.dup
          conflicts = []
          record.fragments.each do |fragment|
            fragment.bytes.each_byte.with_index do |byte, offset|
              position = fragment.start_pos + offset
              if merged.key?(position) && merged[position] != byte
                conflicts << Contradiction.new(
                  "Version record #{record.id} conflicts with an overlapping observation at position #{position}",
                  positions: [position],
                  records: [record.id],
                )
              else
                merged[position] = byte
              end
            end
          end
          [merged, conflicts]
        end
      end

      # Result of a forensic rectangle query.
      #
      # Only colors that every legal assembly agrees on are returned as
      # proven RGBA values. Everything else stays explicitly unknown, and the
      # candidate colors are retained for inspection.
      class PartialQueryResult
        # @return [Integer] Width of the full original image.
        attr_reader :width

        # @return [Integer] Height of the full original image.
        attr_reader :height

        # @return [Array<Integer>] The queried rectangle as [x0, y0, x1, y1].
        attr_reader :rect

        # @return [Array<Contradiction>] Contradictions preventing any legal
        #   assembly of the whole fragment set.
        attr_reader :contradictions

        # @return [Array<Contradiction>] Contradictions of assemblies that
        #   could not form a legal image while other assemblies still did.
        attr_reader :assembly_contradictions

        def initialize(width, height, rect, contradictions: [])
          @width = width
          @height = height
          @rect = rect
          @contradictions = []
          # Contradictions found inside individual alternative assemblies.
          # They only invalidate that assembly; if at least one assembly is
          # legal they stay here rather than failing the whole query.
          @assembly_contradictions = []
          contradictions.each { |contradiction| add_contradiction(contradiction) }
          # The intersection holds colors shared by every legal assembly and
          # determines which positions are proven; the union retains every
          # color that remains possible at a still-pending position.
          @intersection = {}
          @union = {}
        end

        # Records a contradiction, skipping duplicates reported by several
        # alternative assemblies.
        # @param contradiction [Contradiction]
        # @return [void]
        def add_contradiction(contradiction)
          @contradictions << contradiction unless @contradictions.include?(contradiction)
        end

        # Records a contradiction that only invalidates a single assembly.
        # @param contradiction [Contradiction]
        # @return [void]
        def add_assembly_contradiction(contradiction)
          @assembly_contradictions << contradiction unless @assembly_contradictions.include?(contradiction)
        end

        # Records the colors one legal assembly allows at a position.
        # @param x [Integer] Full-image x coordinate.
        # @param y [Integer] Full-image y coordinate.
        # @param colors [Array<Integer>] Candidate RGBA colors.
        # @return [void]
        def observe(x, y, colors)
          colors = colors.uniq
          key = [x, y]
          if @intersection.key?(key)
            @intersection[key] &= colors
            @union[key] |= colors
          else
            @intersection[key] = colors
            @union[key] = colors
          end
        end

        # @param x [Integer] Full-image x coordinate.
        # @param y [Integer] Full-image y coordinate.
        # @return [Integer, nil] The proven RGBA value, or nil when the
        #   position cannot be unified or was not touched by the query.
        def color_at(x, y)
          colors = @intersection[[x, y]]
          colors.first if colors && colors.length == 1
        end

        # @param x [Integer] Full-image x coordinate.
        # @param y [Integer] Full-image y coordinate.
        # @return [Array<Integer>] All RGBA colors still possible at the
        #   position across the legal assemblies; an empty array means it was
        #   never analyzed.
        def candidates_at(x, y)
          @union.fetch([x, y], [])
        end

        # @param x [Integer] Full-image x coordinate.
        # @param y [Integer] Full-image y coordinate.
        # @return [Boolean] Whether all legal assemblies agree on one RGBA.
        def proven?(x, y)
          !color_at(x, y).nil?
        end

        # @param x [Integer] Full-image x coordinate.
        # @param y [Integer] Full-image y coordinate.
        # @return [Boolean] Whether the position has more than one possible
        #   color and therefore stays pending.
        def unknown?(x, y)
          colors = @intersection[[x, y]]
          colors.nil? || colors.length != 1
        end

        # @return [Hash{Array<Integer> => Integer}] Proven colors keyed by
        #   [x, y] full-image coordinates.
        def proven_pixels
          @intersection.each_with_object({}) do |(coords, colors), proven|
            proven[coords] = colors.first if colors.length == 1
          end
        end

        # @return [Hash{Array<Integer> => Array<Integer>}] Pending positions
        #   with all colors that remain possible across the assemblies.
        def unknown_pixels
          @union.reject { |coords, _| @intersection[coords].length == 1 }
        end

        # @return [Array<Array<Integer, nil>>] Rows of the queried rectangle;
        #   each cell is a proven RGBA value or nil for a pending position.
        def to_grid
          x0, y0, x1, y1 = rect
          (y0..y1).map { |y| (x0..x1).map { |x| color_at(x, y) } }
        end

        # @return [Boolean] Whether the fragment set admits at least one legal
        #   image for the queried region.
        def legal?
          contradictions.empty?
        end
      end

      # Decodes a rectangle from fragment observations and a reliable header
      # plus palette, supplied through a datastream.
      #
      # @param ds [ChunkyPNG::Datastream] Datastream providing the IHDR, PLTE
      #   and tRNS chunks. Its IDAT chunks, if any, are ignored.
      # @param transfers [Array<TransferRecord, Hash>] Transfer records,
      #   optionally given as Hashes with +:id+, +:group+ and +:fragments+.
      # @param rect [Array<Integer>] Query rectangle as [x0, y0, x1, y1],
      #   inclusive, in full-image coordinates.
      # @param confirmed [Array<Hash>] Extra fragments that must hold in every
      #   assembly, given as +:start_pos+ and +:bytes+ hashes.
      # @return [PartialQueryResult] The proven and pending colors.
      def from_partial_datastream(ds, transfers:, rect:, confirmed: [])
        header = ds.header_chunk
        decoding_palette = nil
        if header.color == ChunkyPNG::COLOR_INDEXED
          decoding_palette = ChunkyPNG::Palette.from_chunks(ds.palette_chunk, ds.transparency_chunk)
        end

        decode_partial_pixelstream(
          transfers,
          header.width, header.height, header.color, header.depth, header.interlace,
          decoding_palette, rect, confirmed: confirmed,
        )
      end

      # Decodes a rectangle from fragment observations of the filtered stream.
      #
      # @param transfers [Array<TransferRecord, Hash>] The transfer records.
      # @param width [Integer] Full image width from the reliable IHDR.
      # @param height [Integer] Full image height from the reliable IHDR.
      # @param color_mode [Integer] PNG color mode; only COLOR_INDEXED (3) is
      #   currently supported.
      # @param depth [Integer] Bit depth; 1, 2, 4 and 8 are supported.
      # @param interlace [Integer] INTERLACING_NONE or INTERLACING_ADAM7.
      # @param decoding_palette [ChunkyPNG::Palette] Palette with a decoding
      #   map, including tRNS alpha values.
      # @param rect [Array<Integer>] Query rectangle [x0, y0, x1, y1].
      # @param confirmed [Array<Hash>] Unconditional extra fragments.
      # @return [PartialQueryResult]
      # @raise [ChunkyPNG::NotSupported] for color modes or depths outside the
      #   indexed scope.
      def decode_partial_pixelstream(
        transfers, width, height, color_mode, depth, interlace, decoding_palette, rect, confirmed: []
      )
        unless color_mode == ChunkyPNG::COLOR_INDEXED && [1, 2, 4, 8].include?(depth)
          raise ChunkyPNG::NotSupported,
            "Partial decoding currently supports indexed images at 1, 2, 4 or 8 bit depth!"
        end
        unless interlace == ChunkyPNG::INTERLACING_NONE || interlace == ChunkyPNG::INTERLACING_ADAM7
          raise ChunkyPNG::NotSupported, "Unknown interlacing method #{interlace}!"
        end
        if decoding_palette.nil? || !decoding_palette.can_decode?
          raise ChunkyPNG::ExpectationFailed, "This palette is not suitable for decoding!"
        end

        x0, y0, x1, y1 = normalize_rect!(rect, width, height)
        records = normalize_transfers!(transfers, confirmed)

        result = PartialQueryResult.new(width, height, [x0, y0, x1, y1])
        stream_size = expected_stream_size(width, height, depth, interlace)
        validate_fragment_bounds!(records, stream_size, result)
        return result if result.contradictions.any?

        byte_map = ByteMap.new(records)
        byte_map.contradictions.each { |contradiction| result.add_contradiction(contradiction) }
        return result if result.contradictions.any?

        solutions = byte_map.each_solution.to_a
        if solutions.empty?
          result.add_contradiction(
            Contradiction.new("No legal assembly: every version record conflicts with an overlapping observation"),
          )
          return result
        end

        legal_assemblies = 0
        solutions.each do |observed, chosen|
          local, local_contradictions = decode_solution(
            observed, chosen, width, height, depth, interlace, decoding_palette, x0, y0, x1, y1
          )
          if local_contradictions.any?
            record_ids = chosen.values.map(&:id)
            local_contradictions.each do |contradiction|
              result.add_assembly_contradiction(annotate(contradiction, record_ids))
            end
          else
            legal_assemblies += 1
            local.each { |(x, y), colors| result.observe(x, y, colors) }
          end
        end

        if legal_assemblies.zero?
          result.assembly_contradictions.each { |contradiction| result.add_contradiction(contradiction) }
        end
        result
      end

      private

      def normalize_rect!(rect, width, height)
        x0, y0, x1, y1 = rect
        raise ArgumentError, "Rectangle needs four coordinates!" unless x0 && y0 && x1 && y1
        x0, x1 = x0.to_i, x1.to_i
        y0, y1 = y0.to_i, y1.to_i
        x0, x1 = x1, x0 if x0 > x1
        y0, y1 = y1, y0 if y0 > y1
        unless x0 >= 0 && y0 >= 0 && x1 < width && y1 < height
          raise ChunkyPNG::OutOfBounds, "Rectangle #{rect.inspect} is outside the #{width}x#{height} image!"
        end
        [x0, y0, x1, y1]
      end

      def normalize_transfers!(transfers, confirmed)
        records = transfers.map do |transfer|
          transfer.is_a?(TransferRecord) ? transfer : TransferRecord.from_attrs(transfer)
        end
        ids = records.map(&:id)
        raise ArgumentError, "Transfer record ids must be unique!" unless ids.uniq.length == ids.length

        unless confirmed.empty?
          records << TransferRecord.new("__confirmed__#{records.length}", fragments: confirmed)
        end
        records
      end

      # Total length of the filtered pixel stream implied by the IHDR.
      def expected_stream_size(width, height, depth, interlace)
        if interlace == ChunkyPNG::INTERLACING_NONE
          return ChunkyPNG::Color.pass_bytesize(ChunkyPNG::COLOR_INDEXED, depth, width, height)
        end

        (0...7).reduce(0) do |total, pass|
          pass_width, pass_height = adam7_pass_size(pass, width, height)
          total + ChunkyPNG::Color.pass_bytesize(ChunkyPNG::COLOR_INDEXED, depth, pass_width, pass_height)
        end
      end

      # Flags fragments that reference positions beyond the stream the header
      # describes. Such bytes cannot belong to any legal image.
      def validate_fragment_bounds!(records, stream_size, result)
        records.each do |record|
          record.fragments.each do |fragment|
            next if fragment.end_pos < stream_size
            result.add_contradiction(
              Contradiction.new(
                "Fragment of record #{record.id} reaches position #{fragment.end_pos}, " \
                "outside the #{stream_size}-byte filtered stream",
                positions: [fragment.start_pos, fragment.end_pos],
                records: [record.id],
              ),
            )
          end
        end
      end

      def decode_solution(observed, chosen, width, height, depth, interlace, palette, x0, y0, x1, y1)
        local = {}
        local_contradictions = []

        each_relevant_pass(
          width, height, depth, interlace, x0, y0, x1, y1
        ) do |pass, pass_width, pass_height, pass_start, row_indices, col_indices|
          analyze_pass(
            observed, pass, pass_width, pass_height, pass_start, depth, palette,
            row_indices, col_indices, interlace, local, local_contradictions,
          )
        end

        [local, local_contradictions]
      end

      # Tags an assembly-level contradiction with the version records that
      # produced it, so illegal assemblies can be reported in isolation.
      def annotate(contradiction, record_ids)
        Contradiction.new(
          contradiction.message,
          positions: contradiction.positions,
          records: (contradiction.records + record_ids).uniq,
        )
      end

      def each_relevant_pass(width, height, depth, interlace, x0, y0, x1, y1)
        if interlace == ChunkyPNG::INTERLACING_NONE
          yield 0, width, height, 0, (y0..y1).to_a, (x0..x1).to_a
          return
        end

        pass_start = 0
        (0...7).each do |pass|
          pass_width, pass_height = adam7_pass_size(pass, width, height)
          unless pass_width.zero? || pass_height.zero?
            current_start = pass_start
            pass_start += ChunkyPNG::Color.pass_bytesize(ChunkyPNG::COLOR_INDEXED, depth, pass_width, pass_height)

            x_shift, x_offset, y_shift, y_offset = adam7_multiplier_offset(pass)
            row_indices = pass_coordinates(y0, y1, y_shift, y_offset)
            col_indices = pass_coordinates(x0, x1, x_shift, x_offset)
            unless row_indices.empty? || col_indices.empty?
              yield pass, pass_width, pass_height, current_start, row_indices, col_indices
            end
          end
        end
      end

      # Maps full-image coordinates in a range to the indices of one Adam7
      # pass, keeping only coordinates on the pass sampling grid.
      def pass_coordinates(low, high, shift, offset)
        stride = 1 << shift
        first = low + (((offset - low) % stride) + stride) % stride
        return [] if first > high
        (first..high).step(stride).map { |coordinate| (coordinate - offset) >> shift }
      end

      def analyze_pass(
        observed, pass, pass_width, pass_height, pass_start, depth, palette,
        row_indices, col_indices, interlace, local, contradictions
      )
        line_length = ChunkyPNG::Color.scanline_bytesize(ChunkyPNG::COLOR_INDEXED, depth, pass_width)
        pixel_size = ChunkyPNG::Color.pixel_bytesize(ChunkyPNG::COLOR_INDEXED, depth)
        analyzer = ScanlineAnalyzer.new(
          observed, pass_start, line_length, pixel_size, pass_height, pass_width,
          palette_entry_count(palette), depth, row_indices, contradictions,
        )

        analyzer.run_through(row_indices.max)

        row_indices.each do |row_index|
          row_sets = analyzer.row_sets_at(row_index)
          next if row_sets.nil?

          col_indices.each do |col_index|
            # prune_to_palette! already turned bytes with no in-palette value
            # into a full set together with a contradiction, which invalidates
            # this whole assembly, so every remaining field maps to a color.
            x, y = full_coordinates(interlace, pass, col_index, row_index)
            local[[x, y]] = colors_at(palette, depth, row_sets, col_index)
          end
        end
      end

      def full_coordinates(interlace, pass, col_index, row_index)
        return [col_index, row_index] if interlace == ChunkyPNG::INTERLACING_NONE
        x_shift, x_offset, y_shift, y_offset = adam7_multiplier_offset(pass)
        [(col_index << x_shift) | x_offset, (row_index << y_shift) | y_offset]
      end

      # The number of legal palette indices, counting duplicate RGBA entries.
      def palette_entry_count(palette)
        palette.respond_to?(:decoding_map_length) ? palette.decoding_map_length : palette.length
      end

      def colors_at(palette, depth, row_sets, col_index)
        index_set = index_values_at(row_sets, col_index, depth)
        if index_set.nil?
          palette.to_a
        else
          index_set.sort.map { |index| palette[index] }.compact
        end
      end

      # Extracts the possible palette indices for one pixel from the possible
      # byte values of its scanline byte. nil means every index is possible.
      def index_values_at(row_sets, col_index, depth)
        pixels_per_byte = 8 / depth
        byte_index = col_index / pixels_per_byte
        byte_set = row_sets[byte_index]
        return nil if byte_set.nil?

        field_shift = 8 - depth - (col_index % pixels_per_byte) * depth
        mask = (1 << depth) - 1
        byte_set.map { |byte| (byte >> field_shift) & mask }.uniq
      end

      # Set-based abstract interpreter for the PNG scanline filters.
      #
      # Every reconstructed byte is represented by the sorted set of values it
      # can still take. A nil set means the full 0..255 range. Only the
      # previous scanline and the explicitly retained (queried) rows are kept,
      # so a small rectangle query never allocates storage for unrelated
      # passes or for the whole image, however tall it is.
      class ScanlineAnalyzer
        def initialize(
          observed, pass_start, line_length, pixel_size, pass_height, pass_width,
          palette_size, depth, retained_rows, contradictions
        )
          @observed = observed
          @pass_start = pass_start
          @line_length = line_length
          @pixel_size = pixel_size
          @pass_height = pass_height
          @pass_width = pass_width
          @palette_size = palette_size
          @depth = depth
          @retained_rows = retained_rows.to_set
          @contradictions = contradictions
          @next_row_index = 0
          @previous_sets = nil
          @retained = {}
        end

        # Reconstructs scanlines from the pass start up to and including the
        # given row index, retaining the queried rows for later extraction.
        def run_through(last_row)
          while @next_row_index <= last_row && @next_row_index < @pass_height
            row_index = @next_row_index
            sets = reconstruct_row(row_index)
            @retained[row_index] = sets if @retained_rows.include?(row_index)
            @previous_sets = sets
            @next_row_index += 1
          end
        end

        # @return [Array<Array<Integer>, nil>, nil] Reconstructed byte sets
        #   for a retained queried row, or nil if it was not analyzed.
        def row_sets_at(row_index)
          @retained[row_index]
        end

        private

        def reconstruct_row(row_index)
          pos = @pass_start + row_index * (@line_length + 1)
          previous_sets = row_index.zero? ? nil : @previous_sets
          raw_sets = (1..@line_length).map { |offset| observed_set(pos + offset) }
          filter = @observed[pos]

          sets =
            if filter.nil?
              # The filter byte itself is unobserved: every legal filter type
              # remains possible, and their outcomes are unioned.
              (0..4).reduce(nil) do |union, filter_type|
                candidate = apply_filter(filter_type, raw_sets, previous_sets)
                union_rows(union, candidate)
              end
            else
              if filter > 4
                @contradictions << Contradiction.new(
                  "Illegal scanline filter type #{filter} at stream position #{pos}",
                  positions: [pos],
                )
                return Array.new(@line_length) { nil }
              end
              apply_filter(filter, raw_sets, previous_sets)
            end

          prune_to_palette!(sets, pos)
          sets
        end

        def observed_set(position)
          @observed.key?(position) ? [@observed[position]] : nil
        end

        def apply_filter(filter_type, raw_sets, previous_sets)
          case filter_type
            when ChunkyPNG::FILTER_NONE then raw_sets.dup
            when ChunkyPNG::FILTER_SUB then apply_sub(raw_sets)
            when ChunkyPNG::FILTER_UP then apply_up(raw_sets, previous_sets)
            when ChunkyPNG::FILTER_AVERAGE then apply_average(raw_sets, previous_sets)
            when ChunkyPNG::FILTER_PAETH then apply_paeth(raw_sets, previous_sets)
          end
        end

        def apply_sub(raw_sets)
          output = []
          raw_sets.each_with_index do |raw_set, index|
            predictor = index >= @pixel_size ? output[index - @pixel_size] : [0]
            output << add_sets(raw_set, predictor)
          end
          output
        end

        def apply_up(raw_sets, previous_sets)
          raw_sets.each_with_index.map do |raw_set, index|
            predictor = previous_sets.nil? ? [0] : previous_sets[index]
            add_sets(raw_set, predictor)
          end
        end

        def apply_average(raw_sets, previous_sets)
          output = []
          raw_sets.each_with_index do |raw_set, index|
            left = index >= @pixel_size ? output[index - @pixel_size] : [0]
            up = previous_sets.nil? ? [0] : previous_sets[index]
            output << add_sets(raw_set, average_predictors(left, up))
          end
          output
        end

        def apply_paeth(raw_sets, previous_sets)
          output = []
          raw_sets.each_with_index do |raw_set, index|
            left = index >= @pixel_size ? output[index - @pixel_size] : [0]
            if previous_sets.nil?
              up = [0]
              diagonal = [0]
            else
              up = previous_sets[index]
              diagonal = index >= @pixel_size ? previous_sets[index - @pixel_size] : [0]
            end
            output << add_sets(raw_set, paeth_predictors(left, up, diagonal))
          end
          output
        end

        # Reconstructed byte = filtered byte + predictor (mod 256). A full set
        # on either side necessarily spans every output byte.
        def add_sets(raw_set, predictor_set)
          return nil if raw_set.nil? || predictor_set.nil?
          raw_set.product(predictor_set).map { |raw, predictor| (raw + predictor) & 0xff }.uniq
        end

        def average_predictors(left, up)
          return nil if left.nil? || up.nil?
          left.product(up).map { |a, b| (a + b) >> 1 }.uniq
        end

        def paeth_predictors(left, up, diagonal)
          return nil if left.nil? || up.nil? || diagonal.nil?
          left.product(up).product(diagonal).map do |(a, b), c|
            paeth(a, b, c)
          end.uniq
        end

        def paeth(a, b, c)
          p = a + b - c
          pa = (p - a).abs
          pb = (p - b).abs
          pc = (p - c).abs
          if pa <= pb
            pa <= pc ? a : c
          else
            pb <= pc ? b : c
          end
        end

        def union_rows(left, right)
          return right if left.nil?
          left.each_with_index.map { |set, index| union_sets(set, right[index]) }
        end

        def union_sets(left, right)
          return nil if left.nil? || right.nil?
          union = left | right
          union.length >= 256 ? nil : union
        end

        # Removes reconstructed byte values that would encode a palette index
        # outside the palette. Unused padding bits of the final scanline byte
        # are not constrained because they do not represent pixels.
        def prune_to_palette!(sets, row_pos)
          pixels_per_byte = 8 / @depth
          mask = (1 << @depth) - 1
          sets.each_with_index do |set, byte_index|
            next if set.nil?

            first_column = byte_index * pixels_per_byte
            last_column = [first_column + pixels_per_byte, @pass_width].min - 1
            legal = set.select do |byte|
              (first_column..last_column).all? do |column|
                field_shift = 8 - @depth - (column % pixels_per_byte) * @depth
                ((byte >> field_shift) & mask) < @palette_size
              end
            end

            if legal.empty?
              @contradictions << Contradiction.new(
                "Reconstructed byte at stream position #{row_pos + 1 + byte_index} " \
                "has no value compatible with the palette boundary",
                positions: [row_pos + 1 + byte_index],
              )
            end
            sets[byte_index] = legal.empty? ? nil : legal
          end
        end
      end
    end
  end
end
