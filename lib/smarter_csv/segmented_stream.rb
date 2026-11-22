# frozen_string_literal: true

module SmarterCSV
  # SegmentedStream reconciles a read-once CSV byte stream that was spliced
  # together from two independent export sources.
  #
  # The splice ("seam") is not trustworthy on its own: decoy line-start and
  # BOM-homoglyph bytes can appear inside a quoted, multi-line field near the
  # junction, and the quote-escaping rule may switch there (RFC 4180 doubled
  # quotes vs. backslash escaping). Given
  #
  #   * the raw byte segments as delivered (delivery may pause and resume),
  #   * the two quote-escaping rules allowed for the sources,
  #   * the columns every accepted record must contain, and
  #   * a finite, declared byte window for candidate seam positions,
  #
  # it enumerates every internally consistent interpretation, keeps only the
  # ones that read BOTH segments to completion without malformed data, and
  # commits just the records that are identical across every surviving
  # interpretation. Anything interpretation-dependent is withheld as
  # ambiguous rather than guessed.
  class SegmentedStream
    class Error < SmarterCSV::Error; end
    class InvalidSeamError < Error; end
    class NoConsensusError < Error; end

    # A single fully-parsed reading of the joined stream.
    Interpretation = Struct.new(:seam, :rules, :records, :status, :error, keyword_init: true) do
      def ok?
        status == :ok
      end
    end

    # A row position at which the surviving interpretations disagree.
    AmbiguousRow = Struct.new(:row_index, :variants, keyword_init: true)

    Result = Struct.new(:records, :ambiguous, :interpretations, :errors, keyword_init: true) do
      def success?
        interpretations.any?(&:ok?)
      end

      def surviving_interpretations
        interpretations.select(&:ok?)
      end
    end

    # Quote-escaping rules that may be assigned to a source.
    ALLOWED_RULES = %i[double_quotes backslash auto].freeze

    # Guard against an accidentally unbounded seam window: the cartesian
    # product of the two cut ranges must never exceed this many pairs.
    MAX_CANDIDATE_PAIRS = 10_000

    # Parses the two segments and returns the consensus Result.
    # When a block is given, each jointly-determined record is yielded in
    # order; the Result is still returned afterwards.
    #
    # Parameters:
    #   segments:       exactly two raw byte Strings, source 1 then source 2
    #   rules:          the allowed quote_escaping rules, e.g.
    #                   [:double_quotes, :backslash]
    #   required_keys:  columns every accepted record must contain
    #   seam:           finite cut window, as
    #                   { first: Range, second: Range } — candidate end
    #                   offsets inside segment 1 and start offsets inside
    #                   segment 2. nil means an exact, marker-free join.
    #   repeated_headers: when true, the first physical line of segment 2
    #                   is a repeated header and is skipped
    #   options:        regular SmarterCSV reader options
    def self.process(segments:, rules:, required_keys: nil, seam: nil, repeated_headers: false, options: nil, &block)
      new(
        segments: segments,
        rules: rules,
        required_keys: required_keys,
        seam: seam,
        repeated_headers: repeated_headers,
        options: options
      ).process(&block)
    end

    # Like .process, but raises NoConsensusError when no interpretation can
    # read the complete batch.
    def self.process!(**kwargs, &block)
      result = process(**kwargs, &block)
      return result if result.success?

      raise NoConsensusError,
            "no interpretation parsed both segments completely; errors: #{result.errors.map(&:message).inspect}"
    end

    def initialize(segments:, rules:, required_keys:, seam:, repeated_headers:, options:)
      unless segments.is_a?(Array) && segments.size == 2 && segments.all? { |s| s.is_a?(String) }
        raise InvalidSeamError, "segments must be an Array of exactly two Strings"
      end

      rules = Array(rules)
      invalid = rules - ALLOWED_RULES
      raise InvalidSeamError, "unknown quote escaping rules: #{invalid.inspect}" unless invalid.empty?
      raise InvalidSeamError, "at least one quote escaping rule is required" if rules.empty?

      @segments = segments.map { |s| s.dup.force_encoding(s.encoding) }
      @rules = rules.uniq
      @required_keys = required_keys || []
      @repeated_headers = repeated_headers
      @user_options = (options || {}).dup
      @seam = normalize_seam(seam)
    end

    def process
      probe!

      interpretations = candidate_pairs.flat_map do |cut_first, cut_second|
        rule_pairs.map do |rule_first, rule_second|
          run_interpretation(cut_first, cut_second, rule_first, rule_second)
        end
      end

      errors = interpretations.reject(&:ok?).map(&:error).compact
      consensus = build_consensus(interpretations.select(&:ok?))

      result = Result.new(
        records: consensus[:records],
        ambiguous: consensus[:ambiguous],
        interpretations: interpretations,
        errors: errors
      )

      if block_given?
        result.records.each { |record| yield record }
      end

      result
    end

    private

    # Resolve row/col separators once (auto-detection needs an IO) and build
    # a prototype Reader whose option processing and boundary detection are
    # reused for every interpretation.
    def probe!
      # Detection runs over raw bytes (binary IO) so multi-byte content and a
      # BOM homoglyph near the seam cannot break the separator scans; the
      # resolved separators are encoding-independent ASCII tokens. The caller's
      # file_encoding is restored before any record is parsed.
      joined = @segments.map(&:b).join
      probe_io = StringIO.new(joined)
      @probe_reader = Reader.new(probe_io, @user_options.merge(file_encoding: 'binary'))
      @resolved_options = @probe_reader.options.dup

      if @resolved_options[:row_sep] == :auto
        probe_io.rewind
        @resolved_options[:row_sep] = @probe_reader.send(:guess_line_ending, probe_io, @resolved_options)
      end
      if @resolved_options[:col_sep] == :auto
        probe_io.rewind
        @resolved_options[:col_sep] = @probe_reader.send(:guess_column_separator, probe_io, @resolved_options)
      end

      @resolved_options[:file_encoding] = @user_options[:file_encoding] || Reader.default_options[:file_encoding]
    end

    def normalize_seam(seam)
      first_size = @segments[0].bytesize
      second_size = @segments[1].bytesize

      if seam.nil?
        return { first: (first_size..first_size), second: (0..0) }
      end

      unless seam.is_a?(Hash) && seam[:first].is_a?(Range) && seam[:second].is_a?(Range)
        raise InvalidSeamError, "seam must be a Hash with :first and :second byte-offset Ranges"
      end

      first_range = seam[:first]
      second_range = seam[:second]

      if first_range.min < 0 || first_range.max > first_size ||
         second_range.min < 0 || second_range.max > second_size
        raise InvalidSeamError, "seam ranges exceed segment byte sizes"
      end

      { first: first_range, second: second_range }
    end

    def candidate_pairs
      @candidate_pairs ||= begin
        pairs = @seam[:first].to_a.product(@seam[:second].to_a)
        if pairs.size > MAX_CANDIDATE_PAIRS
          raise InvalidSeamError,
                "seam window yields #{pairs.size} candidate pairs, exceeding the limit of #{MAX_CANDIDATE_PAIRS}"
        end

        pairs
      end
    end

    # Ordered rule assignments for (source 1, source 2), deduplicated.
    def rule_pairs
      @rule_pairs ||= @rules.product(@rules).uniq
    end

    def run_interpretation(cut_first, cut_second, rule_first, rule_second)
      seam = [[cut_first, rule_first], [cut_second, rule_second]]
      exact_join = cut_first == @segments[0].bytesize && cut_second == 0
      split_context = rule_first != rule_second || @repeated_headers

      # The exact reassembly of the two delivered byte ranges is always a legal
      # hypothesis — delivery may pause mid-record, and a single reader over the
      # rejoined bytes stitches it exactly as one sequential read would.
      #
      # Every other cut reassigns bytes away from one of the sources, so it is
      # only legal at a record boundary provable on the raw bytes. This is what
      # rejects decoy line starts and BOM homoglyphs hidden inside a quoted,
      # multi-line field near the junction.
      unless exact_join
        if !valid_cut?(@segments[0], cut_first, rule_first) ||
           !valid_cut?(@segments[1], cut_second, rule_second)
          return Interpretation.new(
            seam: seam, rules: [rule_first, rule_second], records: [],
            status: :rejected, error: nil
          )
        end
      end

      first_bytes = @segments[0].byteslice(0, cut_first)
      second_bytes = @segments[1].byteslice(cut_second..) || EMPTY_BINARY

      records =
        if split_context
          parse_two_contexts(first_bytes, second_bytes, rule_first, rule_second)
        else
          parse_joined(first_bytes, second_bytes, rule_first)
        end

      unless records.all? { |record| satisfies_required?(record) }
        raise MalformedCSV, "a record is missing required keys #{@required_keys.inspect}"
      end

      Interpretation.new(
        seam: seam, rules: [rule_first, rule_second], records: records,
        status: :ok, error: nil
      )
    rescue SmarterCSV::Error => e
      Interpretation.new(
        seam: seam, rules: [rule_first, rule_second], records: [],
        status: :error, error: e
      )
    end

    # One reader over the reassembled bytes: multiline records that straddle the
    # delivery pause stitch exactly as they would in a single sequential read.
    def parse_joined(first_bytes, second_bytes, rule)
      options = interpretation_options(@resolved_options, rule, @required_keys, nil)
      Reader.new(StringIO.new(first_bytes + second_bytes), options).process
    end

    # Two readers when the escaping rule changes at the seam or the second
    # source repeats its header row. The first reader supplies the headers the
    # second reader must use.
    def parse_two_contexts(first_bytes, second_bytes, rule_first, rule_second)
      first_options = interpretation_options(@resolved_options, rule_first, @required_keys, nil)
      first_reader = Reader.new(StringIO.new(first_bytes), first_options)
      first_records = first_reader.process

      second_options = interpretation_options(@resolved_options, rule_second, @required_keys, first_reader.headers)
      if @repeated_headers
        second_options[:skip_lines] = second_options[:skip_lines].to_i + 1
      end
      second_records = Reader.new(StringIO.new(second_bytes), second_options).process

      first_records + second_records
    end

    def interpretation_options(base_options, rule, required_keys, user_headers)
      options = base_options.merge(
        quote_escaping: rule,
        on_bad_row: :raise,
        required_keys: required_keys
      )
      if user_headers
        options[:headers_in_file] = false
        options[:user_provided_headers] = user_headers
      end
      options
    end

    def satisfies_required?(record)
      @required_keys.all? do |key|
        value = record[key]
        record.key?(key) && !value.nil? && value != ''
      end
    end

    # A cut is only legal at a record boundary that can be proven on the raw
    # bytes: position 0, the end of the segment, or immediately after a row
    # separator while the quote state machine is outside any quoted field.
    # Decoy newlines inside quoted fields — including the marker field — and
    # BOM homoglyphs are ordinary content bytes and never qualify.
    def valid_cut?(bytes, cut, rule)
      return true if cut == 0 || cut == bytes.bytesize

      row_sep = @resolved_options[:row_sep]
      return false if cut < row_sep.bytesize

      tail = bytes.b.byteslice(cut - row_sep.bytesize, row_sep.bytesize)
      return false unless tail == row_sep.b

      prefix = bytes.byteslice(0, cut)
      outside_quotes?(prefix, rule)
    end

    # True when the prefix ends outside a quoted field under the given rule.
    # Reuses the reader's own multiline gate so the boundary decision matches
    # the parser for every quote_boundary / quote_escaping combination,
    # including :auto (which only stays open when BOTH escaping semantics do).
    def outside_quotes?(prefix, rule)
      options = @resolved_options.merge(quote_escaping: rule)
      !@probe_reader.send(:detect_multiline, prefix, options)
    end

    def build_consensus(ok_interpretations)
      return { records: [], ambiguous: [] } if ok_interpretations.empty?

      common_size = ok_interpretations.map { |interp| interp.records.size }.min
      records = []
      ambiguous = []

      common_size.times do |index|
        variants = ok_interpretations.map { |interp| comparable(interp.records[index]) }
        if variants.uniq.size == 1
          records << ok_interpretations.first.records[index]
        else
          ambiguous << AmbiguousRow.new(
            row_index: index,
            variants: ok_interpretations.map { |interp| { rules: interp.rules, seam: interp.seam, record: interp.records[index] } }
          )
        end
      end

      # Positions only some interpretations reach cannot be jointly determined.
      if ok_interpretations.map { |interp| interp.records.size }.uniq.size > 1
        ok_interpretations.each do |interp|
          (common_size...interp.records.size).each do |index|
            ambiguous << AmbiguousRow.new(
              row_index: index,
              variants: [{ rules: interp.rules, seam: interp.seam, record: interp.records[index] }]
            )
          end
        end
      end

      { records: records, ambiguous: ambiguous }
    end

    # Line numbers are bookkeeping, not field data — exclude them when
    # deciding whether two interpretations produced the same record.
    def comparable(record)
      record.reject { |key, _| key == :csv_line_number }
    end

    EMPTY_BINARY = ''.b
  end
end
