# frozen_string_literal: true

module Prawn
  # Optional paginated settlement voucher (分页结账) entry point.
  #
  # A settlement voucher prints a batch of ledger entries, in their original
  # business order, across as few pages as possible. Every page carries:
  #
  # * a header with fixed slots for the brought-forward amount, the page
  #   number and a continuation hint,
  # * body lines (serial number + formatted description) for the entries,
  # * a footer with the page's own total (`OUT`) and the cumulative
  #   carry-forward (`END`), each starting on its own line and wrapping to
  #   the footer amount column width by actual glyph width.
  #
  # Body and footer share the available height of the printed side. An entry
  # is posted (its amount counted into the page footer) only on the page
  # where its last description line is actually printed. Long descriptions
  # may continue on the next page between complete lines; continuation pages
  # repeat the original serial number. Entries flagged `keep_together` must
  # stay on a single page. Zero-amount entries are printed and settled like
  # any other entry.
  #
  # The layout uses the minimum number of pages. Ties are broken page by
  # page from the first page on, preferring the layout that keeps more
  # original description content on the earlier page. Every page must make
  # body progress. If the batch genuinely does not fit,
  # {Prawn::Settlement::UnfittableError} is raised with the serial number of
  # the entry the layout got stuck on, and no voucher is issued.
  #
  # @example
  #   Prawn::Document.generate('voucher.pdf', page_size: [200, 160], margin: 20) do |pdf|
  #     pdf.settlement_voucher(
  #       [
  #         { serial: 'A', amount: 9, lines: ['A DONE'] },
  #         { serial: 'B', amount: 1, lines: ['B CHECK', 'B DONE'] },
  #         { serial: 'D', amount: 0, lines: ['D DONE'] }
  #       ],
  #       body_width: 120,
  #       available_height: 84,
  #       amount_width: 33
  #     )
  #   end
  module Settlement
    # Raised when a batch of entries cannot be laid out within the given
    # recto/verso geometry. The voucher is refused (nothing is rendered).
    class UnfittableError < StandardError
      # Serial number of the entry the layout got stuck on.
      attr_reader :serial

      def initialize(serial)
        @serial = serial
        super(
          "settlement batch does not fit the page geometry; " \
          "stuck at serial #{serial.inspect} -- voucher not issued"
        )
      end
    end

    # Geometry of one printed side (recto or verso) as given by the printer.
    class Side
      # Width of the body column in points.
      attr_reader :body_width
      # Vertical space shared by body and footer, in points.
      attr_reader :available_height
      # Width of the footer amount column in points.
      attr_reader :amount_width

      def initialize(body_width:, available_height:, amount_width:)
        @body_width = body_width
        @available_height = available_height
        @amount_width = amount_width
      end

      # Number of line slots that fit into the shared vertical region.
      def line_slots(line_height)
        (available_height / line_height).floor
      end
    end

    DEFAULT_OPTIONS = {
      font: 'Courier',
      font_size: 10,
      line_height: 14,
      opening_balance: 0
    }.freeze

    # Issues a paginated settlement voucher for `entries`.
    #
    # @param entries [Array<Hash>] entries in business order. Each entry
    #   must have:
    #   * `:serial` -- the ledger serial number (printed on every line, so
    #     continuation pages keep the original serial),
    #   * `:amount` -- signed integer amount, posted on the page where the
    #     entry's last line is printed,
    #   * `:lines` -- non-empty array of pre-formatted description lines,
    #   * `:keep_together` -- optional; when truthy the whole description
    #     must stay on one page.
    # @param options [Hash]
    # @option options :body_width [Number] body column width for both sides
    # @option options :available_height [Number] vertical space shared by
    #   body and footer for both sides
    # @option options :amount_width [Number] footer amount column width for
    #   both sides
    # @option options :recto [Hash] per-side overrides (`:body_width`,
    #   `:available_height`, `:amount_width`) for odd pages
    # @option options :verso [Hash] per-side overrides for even pages
    # @option options :line_height [Number] (14) height of one line slot
    # @option options :header_height [Number] (`line_height`) height of the
    #   fixed header band above the shared region
    # @option options :font [String] ('Courier')
    # @option options :font_size [Number] (10)
    # @option options :opening_balance [Integer] (0) balance carried into
    #   the first page
    # @return [nil]
    # @raise [Prawn::Settlement::UnfittableError] when the batch does not
    #   fit; `error.serial` names the entry that cannot be placed
    def settlement_voucher(entries, options = {})
      opts = DEFAULT_OPTIONS.merge(options)
      normalized = settlement_normalize_entries(entries)
      recto = settlement_side(opts, :recto)
      verso = settlement_side(opts, :verso)
      header_height = opts[:header_height] || opts[:line_height]

      [recto, verso].each do |side|
        if header_height + side.available_height > bounds.height
          raise ArgumentError,
            'settlement_voucher: header and shared region exceed the page bounds height'
        end
        if side.body_width > bounds.width
          raise ArgumentError,
            'settlement_voucher: body width exceeds the page bounds width'
        end
      end

      font(opts[:font], size: opts[:font_size]) do
        breaks = settlement_plan(normalized, recto, verso, opts)
        settlement_render(breaks, normalized, recto, verso, opts, header_height)
      end
      nil
    end

    private

    def settlement_normalize_entries(entries)
      unless entries.is_a?(Array) && !entries.empty?
        raise ArgumentError, 'settlement_voucher: entries must be a non-empty array'
      end

      entries.map do |entry|
        serial = entry[:serial]
        amount = entry[:amount]
        lines = entry[:lines]

        unless amount.is_a?(Integer)
          raise ArgumentError, "settlement_voucher: amount for #{serial.inspect} must be an Integer"
        end
        unless lines.is_a?(Array) && !lines.empty? && lines.all? { |line| line.is_a?(String) }
          raise ArgumentError, "settlement_voucher: lines for #{serial.inspect} must be a non-empty array of strings"
        end

        {
          serial: serial,
          amount: amount,
          lines: lines,
          keep_together: entry[:keep_together] ? true : false
        }
      end
    end

    def settlement_side(opts, side_key)
      specific = opts[side_key] || {}
      side = Side.new(
        body_width: specific[:body_width] || opts[:body_width],
        available_height: specific[:available_height] || opts[:available_height],
        amount_width: specific[:amount_width] || opts[:amount_width]
      )
      if [side.body_width, side.available_height, side.amount_width].any?(&:nil?)
        raise ArgumentError,
          "settlement_voucher: #{side_key} side needs :body_width, :available_height and :amount_width"
      end
      side
    end

    # Wraps a footer item into lines that fit `max_width`, breaking between
    # words first and inside overlong words by actual glyph width.
    def settlement_wrap(text, max_width)
      lines = []
      text.split(' ').each do |word|
        if lines.any? && width_of("#{lines.last} #{word}") <= max_width
          lines[-1] = "#{lines.last} #{word}"
        else
          lines.concat(settlement_break_word(word, max_width))
        end
      end
      lines
    end

    def settlement_break_word(word, max_width)
      chunks = []
      current = +''
      word.each_char do |char|
        if current.empty? || width_of(current + char) <= max_width
          current << char
        else
          chunks << current
          current = +char
        end
      end
      chunks << current unless current.empty?
      chunks
    end

    # Computes the page breaks for the batch. Returns an array of line
    # positions (starting with 0, ending with the total line count).
    def settlement_plan(entries, recto, verso, opts)
      line_height = opts[:line_height]
      opening = opts[:opening_balance]

      line_entry = []
      entries.each_with_index do |entry, index|
        entry[:lines].length.times { line_entry << index }
      end
      total_lines = line_entry.length

      # Amount posted at each line position (a position is the number of
      # lines printed so far); an entry posts where its last line lands.
      posted_at = Array.new(total_lines + 1, 0)
      position = 0
      entries.each do |entry|
        position += entry[:lines].length
        posted_at[position] += entry[:amount]
      end
      posted_prefix = [0]
      (1..total_lines).each { |p| posted_prefix[p] = posted_prefix[p - 1] + posted_at[p] }

      break_allowed = lambda do |p|
        p == total_lines ||
          line_entry[p] != line_entry[p - 1] ||
          !entries[line_entry[p]][:keep_together]
      end

      footer_lines = lambda do |from, to, side|
        occurred = posted_prefix[to] - posted_prefix[from]
        cumulative = opening + posted_prefix[to]
        settlement_wrap("OUT #{occurred}", side.amount_width).length +
          settlement_wrap("END #{cumulative}", side.amount_width).length
      end

      # Dynamic programming over line positions, keyed additionally by page
      # parity because recto/verso geometry may differ.
      states = { [0, 0] => { pages: 0, breaks: [0], lengths: [] } }
      covered = Array.new(total_lines, false)

      (0...total_lines).each do |from|
        [0, 1].each do |parity|
          state = states[[from, parity]]
          next unless state

          page_no = state[:pages] + 1
          side = page_no.odd? ? recto : verso
          slots = side.line_slots(line_height)

          (from + 1..total_lines).each do |to|
            next unless break_allowed.call(to)

            body = to - from
            next if body >= slots
            next if body + footer_lines.call(from, to, side) > slots

            covered.fill(true, from...to)

            candidate = {
              pages: page_no,
              breaks: state[:breaks] + [to],
              lengths: state[:lengths] + [body]
            }
            key = [to, page_no % 2]
            states[key] = candidate if settlement_better_layout?(candidate, states[key])
          end
        end
      end

      final = [states[[total_lines, 0]], states[[total_lines, 1]]].compact.min do |a, b|
        settlement_better_layout?(a, b) ? -1 : 1
      end

      unless final
        stuck_line = covered.index(false)
        raise UnfittableError, entries[line_entry[stuck_line]][:serial]
      end

      final[:breaks]
    end

    # Layouts are compared by page count first (fewer is better), then page
    # by page from the first page on (more body content on a page is
    # better).
    def settlement_better_layout?(candidate, incumbent)
      return true if incumbent.nil?
      return true if candidate[:pages] < incumbent[:pages]
      return false if candidate[:pages] > incumbent[:pages]

      candidate[:lengths].each_with_index do |length, index|
        other = incumbent[:lengths][index]
        return true if other.nil? || length > other
        return false if length < other
      end
      false
    end

    def settlement_render(breaks, entries, recto, verso, opts, header_height)
      line_height = opts[:line_height]
      font_size = opts[:font_size]
      opening = opts[:opening_balance]

      flat_lines = []
      entries.each do |entry|
        entry[:lines].each { |line| flat_lines << [entry[:serial], line] }
      end

      gutter = entries.map { |entry| width_of(entry[:serial].to_s) }.max + width_of(' ')

      posted_at = Array.new(flat_lines.length + 1, 0)
      position = 0
      entries.each do |entry|
        position += entry[:lines].length
        posted_at[position] += entry[:amount]
      end
      posted_prefix = [0]
      (1..flat_lines.length).each { |p| posted_prefix[p] = posted_prefix[p - 1] + posted_at[p] }

      # Index of each flattened line inside its own entry, to detect
      # continuations of entries started on a previous page.
      line_index_in_entry = []
      entries.each do |entry|
        entry[:lines].each_index { |i| line_index_in_entry << i }
      end

      breaks.each_cons(2).with_index(1) do |(from, to), page_no|
        start_new_page if page_no > 1

        side = page_no.odd? ? recto : verso
        occurred = posted_prefix[to] - posted_prefix[from]
        brought = opening + posted_prefix[from]
        cumulative = opening + posted_prefix[to]

        left = bounds.left
        region_top = bounds.top - header_height
        region_bottom = region_top - side.available_height

        settlement_render_header(
          left, side.body_width, header_height, font_size,
          brought, page_no,
          line_index_in_entry[from].positive? ? flat_lines[from][0] : nil
        )

        (from...to).each do |line_index|
          serial, text = flat_lines[line_index]
          slot_top = region_top - (line_index - from) * line_height
          baseline = slot_top - font_size - (line_height - font_size) / 2.0
          draw_text(serial.to_s, at: [left, baseline], size: font_size)
          draw_text(text, at: [left + gutter, baseline], size: font_size)
        end

        footer =
          settlement_wrap("OUT #{occurred}", side.amount_width) +
          settlement_wrap("END #{cumulative}", side.amount_width)
        footer.each_with_index do |text, index|
          slot_top = region_bottom + (footer.length - index) * line_height
          baseline = slot_top - font_size - (line_height - font_size) / 2.0
          draw_text(text, at: [left, baseline], size: font_size)
        end
      end
    end

    def settlement_render_header(left, width, header_height, font_size, brought, page_no, continued_serial)
      baseline = bounds.top - font_size - (header_height - font_size) / 2.0

      draw_text("IN #{brought}", at: [left, baseline], size: font_size)

      page_label = "PAGE #{page_no}"
      draw_text(page_label, at: [left + (width - width_of(page_label)) / 2.0, baseline], size: font_size)

      return unless continued_serial

      hint = "CONT #{continued_serial}"
      draw_text(hint, at: [left + width - width_of(hint), baseline], size: font_size)
    end
  end
end

Prawn::Document.include(Prawn::Settlement)
