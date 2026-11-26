# frozen_string_literal: true

module HandoverAudit
  # Enumerates every concrete handover allowed by the shift log's option slots,
  # replays each one on a single shared database (schema changes roll back, so
  # the long material table is built only once), keeps exactly the replays that
  # satisfy the receiving book, and classifies every published line from the
  # union of their provenance.
  class Solver
    Witness = Struct.new(:choices, :rows, :terminals, keyword_init: true)

    MAX_COMBINATIONS = 200_000

    attr_reader :witnesses, :failures, :audit_case, :provenance

    def initialize(audit_case)
      @audit_case = audit_case
      @provenance = Provenance.new(audit_case)
      @witnesses = []
      @failures = []
    end

    def solve
      combination_count = combination_size
      if combination_count > MAX_COMBINATIONS
        raise Case::SpecError,
          "#{combination_count} handover combinations exceed the cap of #{MAX_COMBINATIONS}; " \
          "split the audit or narrow the option groups"
      end

      build_shared_database
      begin
        each_combination do |choices|
          run_combination(choices)
        end
      ensure
        close_shared_database
      end
      classify
      self
    end

    def feasible?
      !@witnesses.empty?
    end

    # Classification per delivery: ordered list of (range, verdict) segments.
    def segments
      @segments
    end

    def line_detail(delivery_id, pub_no)
      @line_detail.dig(delivery_id, pub_no)
    end

    # Every distinct provenance tuple a compatible handover gives for one
    # delivery-wide published line, with the handover choices that produce it
    # (the witnesses needed when the line cannot be pinned to one source).
    def replay(delivery_id, pub_no)
      detail = line_detail(delivery_id, pub_no)
      return [] unless detail

      detail[:possibilities].map do |tuple|
        witness = @witnesses.find do |entry|
          entry.rows.any? { |row| row.delivery == delivery_id && row.pub_no == pub_no && tuple_for(row) == tuple }
        end
        { provenance: tuple, handover: witness.choices }
      end
    end

    # When no handover fits, the earliest receiving observation that every
    # candidate fails to satisfy.
    def earliest_mismatch
      return nil if feasible?

      @failures.min_by { |failure| failure[:order] }
    end

    private

    def run_combination(choices)
      engine = Engine.new(@audit_case, @provenance, choices, @db)
      begin
        engine.run
      rescue Engine::Halt => halt
        action = halt.observation[:action] || {}
        handle = action[:handle]
        delivery =
          begin
            handle && @audit_case.handle_to_delivery(handle)
          rescue Case::SpecError
            nil
          end
        @failures << {
          order: halt_order(halt), kind: :halt, choices: choices,
          reason: {
            problem: halt.observation[:message],
            handle: handle,
            delivery: delivery
          }
        }
        return
      end

      mismatch = verify_ledger(engine)
      if mismatch
        @failures << { order: mismatch[:order], kind: :ledger, reason: mismatch, choices: choices }
      else
        @witnesses << Witness.new(choices: choices, rows: engine.rows.dup, terminals: engine.terminals.dup)
      end
    end

    def verify_ledger(engine)
      @audit_case.ledger.each_with_index do |entry, entry_index|
        rows = engine.rows_for(entry[:handle])
        rows_by_pub_no = rows.to_h { |row| [row.pub_no, row] }
        terminal = engine.terminal_for(entry[:handle])
        pointer = first_pub_no(rows)
        entry[:events].each_with_index do |event, event_index|
          mismatch = check_event(entry, event, rows_by_pub_no, terminal, pointer)
          pointer += 1 if event[:type] == :row
          next unless mismatch

          return mismatch.merge(order: [entry_index, event_index], handle: entry[:handle],
            delivery: entry[:delivery])
        end
      end
      nil
    end

    def first_pub_no(rows)
      rows.map(&:pub_no).min || 1
    end

    def check_event(entry, event, rows_by_pub_no, terminal, pointer)
      case event[:type]
      when :row
        check_row_event(entry, event, rows_by_pub_no)
      when :sample
        check_sample_event(entry, event, rows_by_pub_no, pointer)
      when :end
        { problem: "expected the cursor to end, it is still open" } unless terminal&.kind == :end
      when :error
        check_error_event(event, terminal)
      end
    end

    # Plain row events only certify that a continuous line was written; the
    # material it carries is judged against the approval slip during
    # classification, not by the receiving book itself.
    def check_row_event(entry, event, rows_by_pub_no)
      return if rows_by_pub_no.key?(event[:line])

      { problem: "publish line #{event[:line]} of #{entry[:delivery]} was never written" }
    end

    def check_sample_event(entry, event, rows_by_pub_no, pointer)
      pub_no = event[:line] || pointer
      row = rows_by_pub_no[pub_no]
      return { problem: "spot check on line #{pub_no} of #{entry[:delivery]} has no row" } unless row

      observed = row.value(event[:column])
      return if observed == event[:value]

      {
        problem: "spot check #{event[:column].inspect}=#{event[:value].inspect} on line #{pub_no} " \
                 "of #{entry[:delivery]} observed #{observed.inspect}"
      }
    end

    def check_error_event(event, terminal)
      return { problem: "expected an error but the cursor did not raise" } unless terminal&.kind == :error
      return if event[:message].nil? || terminal.message.include?(event[:message].to_s)

      { problem: "error #{terminal.message.inspect} does not mention #{event[:message].inspect}" }
    end

    def halt_order(halt)
      handover_id = halt.observation[:handover]
      slot = @audit_case.handovers.index { |handover| handover[:id] == handover_id }
      [slot || @audit_case.handovers.length, 0]
    end

    def classify
      @line_detail = Hash.new { |hash, key| hash[key] = {} }
      @witnesses.each do |witness|
        witness.rows.each do |row|
          detail = @line_detail[row.delivery][row.pub_no] ||= {
            possibilities: Set.new, correct: Set.new, verdicts: Set.new
          }
          tuple = tuple_for(row)
          detail[:possibilities] << tuple
          detail[:correct] << tuple if correct?(row)
        end
      end
      build_segments
    end

    def correct?(row)
      delivery = @audit_case.deliveries.fetch(row.delivery)
      expected_id = @provenance.expected_material_id(row.delivery, row.pub_no)
      row.view == delivery[:expect_view] && row.material_id == expected_id
    end

    def tuple_for(row)
      {
        handle: row.handle, mode: row.mode, view: row.view,
        ordinal: row.ordinal, view_ordinal: row.view_ordinal,
        material_id: row.material_id,
        snapshot_names: row.snapshot_names, live_names: row.live_names
      }
    end

    def build_segments
      @segments = {}
      @line_detail.each do |delivery, lines|
        verdicts = lines.keys.sort.to_h { |pub_no| [pub_no, verdict_for(lines[pub_no])] }
        @segments[delivery] = compress(verdicts)
      end
    end

    def verdict_for(detail)
      if detail[:correct].length == detail[:possibilities].length
        :proven_correct
      elsif detail[:correct].empty?
        :proven_wrong
      else
        :ambiguous
      end
    end

    def compress(verdicts)
      verdicts.chunk { |pub_no, verdict| verdict }.map do |verdict, pairs|
        { verdict: verdict, from: pairs.first.first, to: pairs.last.first }
      end
    end

    def combination_size
      @audit_case.handovers.map { |handover| handover[:choices].length }.reduce(1, :*)
    end

    def each_combination
      slots = @audit_case.handovers.map { |handover| handover[:choices] }
      first, *rest = slots
      if rest.empty?
        first.each { |choice| yield [choice] }
      else
        first.product(*rest).each { |choices| yield(choices) }
      end
    end

    def build_shared_database
      @db = SQLite3::Database.new(":memory:")
      @db.results_as_hash = false
      columns = @audit_case.material_columns
      declaration = columns.map { |column| "\"#{column}\"" }.join(", ")
      @db.execute("CREATE TABLE \"#{@audit_case.material_table}\" (#{declaration})")
      @audit_case.material_by_id.each_value do |row|
        placeholders = (["?"] * columns.length).join(", ")
        @db.execute(
          "INSERT INTO \"#{@audit_case.material_table}\" (#{declaration}) VALUES (#{placeholders})",
          columns.map { |column| row[column] }
        )
      end
      @audit_case.initial_views.each do |name, version|
        @db.execute("CREATE VIEW \"#{name}\" AS #{@audit_case.view_body(version)}")
      end
    end

    def close_shared_database
      @db&.close
    end
  end
end
