# frozen_string_literal: true

# Standalone, dependency-free checks for the handover audit toolkit. The gem's
# own minitest suite is untouched; run this file directly:
#
#   BUNDLE_GEMFILE=.gsb/Gemfile bundle exec ruby tasks/handover_audit/test/handover_audit_test.rb

$LOAD_PATH.unshift(File.expand_path("../lib", __dir__))

require "handover_audit"

module HandoverAudit
  class HandoverAuditTest
    def initialize
      @failures = []
      @passed = 0
    end

    def run
      (methods + private_methods).grep(/\Atest_/).uniq.each do |name|
        @current = name
        send(name)
        @passed += 1
      rescue StandardError => e
        @failures << "#{name}: #{e.class}: #{e.message}\n#{e.backtrace.first(6).join("\n")}"
      end
      report
    end

    private

    def assert(condition, message = "expected truthy value")
      raise message unless condition
    end

    def refute(condition, message = "expected falsey value")
      raise message if condition
    end

    def refute_nil(value, message = "expected non-nil")
      raise message if value.nil?
    end

    def assert_equal(expected, actual, message = nil)
      raise(message || "expected #{expected.inspect}, got #{actual.inspect}") unless expected == actual
    end

    def assert_includes(collection, item, message = nil)
      raise(message || "expected #{collection.inspect} to include #{item.inspect}") unless
        collection.include?(item)
    end

    def assert_operator(left, operator, right, message = nil)
      raise(message || "expected #{left} #{operator} #{right}") unless left.public_send(operator, right)
    end

    def assert_match(pattern, string, message = nil)
      raise(message || "expected #{string.inspect} to match #{pattern.inspect}") unless pattern.match?(string)
    end

    def report
      if @failures.empty?
        puts "#{@passed} checks passed"
        exit 0
      end
      warn @failures.map { |failure| "FAIL #{failure}" }.join("\n\n")
      warn "#{@failures.length} failure(s), #{@passed} passed"
      exit 1
    end

    def base_spec
      {
        material: { table: "items", id_col: :id, view_name: "v", rows: material_rows },
        views: [
          { version: :v1, sql: "SELECT id AS code, v AS label FROM items WHERE grp = 'A' ORDER BY id" },
          { version: :v2, sql: "SELECT id AS code, v AS label FROM items WHERE grp = 'B' ORDER BY id" }
        ],
        statements: [{ id: :S1, sql: "SELECT * FROM v" }],
        groups: groups,
        handovers: handovers,
        deliveries: deliveries,
        ledger: ledger
      }
    end

    def material_rows
      [
        { id: 1, grp: "A", v: "a-one" },
        { id: 2, grp: "A", v: "a-two" },
        { id: 3, grp: "B", v: "b-one" },
        { id: 4, grp: "B", v: "b-two" },
        { id: 5, grp: "B", v: "b-three" }
      ]
    end

    def deliveries
      [
        { id: :D1, statement: :S1, expect_view: :v1,
          columns: { code: :id, label: :v },
          row_map: { method: :sequence, start: 1, step: 1 } },
        { id: :D2, statement: :S1, expect_view: :v2,
          columns: { code: :id, label: :v },
          row_map: { method: :sequence, start: 4, step: 1 } }
      ]
    end

    def groups
      {
        maybe_swap: [{ op: :pass }, { op: :swap_view, view: :v2 }],
        maybe_step: [{ op: :pass }, { op: :step, handle: :H2 }]
      }
    end

    def handovers
      [
        { id: :h0, actions: [{ op: :prepare, statement: :S1 }] },
        { id: :h1, actions: [{ op: :execute, statement: :S1, handle: :H1, mode: :hash }] },
        { id: :h2, actions: [{ op: :columns, handle: :H1 }] },
        { id: :h3, actions: [{ group: :maybe_swap }] },
        { id: :h4, actions: [{ op: :execute, statement: :S1, handle: :H2, mode: :array }] },
        { id: :h5, actions: [{ op: :step, handle: :H1 }] },
        { id: :h6, actions: [{ op: :step, handle: :H2 }] },
        { id: :h7, actions: [{ op: :step, handle: :H2 }] },
        { id: :h8, actions: [{ group: :maybe_step }] }
      ]
    end

    def ledger
      [
        { delivery: :D1, handle: :H1,
          events: [{ type: :sample, column: :code, value: 3 }, { type: :row, line: 1 }] },
        { delivery: :D2, handle: :H2,
          events: [{ type: :row, line: 1 }, { type: :row, line: 2 }, { type: :end }] }
      ]
    end

    def test_sealed_views_materialize_through_sql_only
      audit_case = Case.new(base_spec)
      assert_equal [{ code: 1, label: "a-one" }, { code: 2, label: "a-two" }],
        audit_case.materialize(:v1)
      assert_equal [3, 4, 5], audit_case.materialize(:v2).map { |row| row[:code] }
    end

    def test_stale_hash_cursor_reads_new_view_under_old_names
      solver = HandoverAudit.audit(base_spec)
      assert solver.feasible?
      assert_equal 1, solver.witnesses.length

      d1 = solver.segments[:D1].first
      assert_equal :proven_wrong, d1[:verdict]
      replay = solver.replay(:D1, 1).first
      assert_equal :v2, replay[:provenance][:view]
      assert_equal 3, replay[:provenance][:material_id]
      assert_equal 1, replay[:provenance][:view_ordinal] + 1
    end

    def test_second_cursor_starts_after_row_consumed_by_first
      solver = HandoverAudit.audit(base_spec)
      assert_equal :v2, solver.replay(:D2, 1).first[:provenance][:view]
      assert_equal 2, solver.replay(:D2, 1).first[:provenance][:view_ordinal] + 1
      assert_equal 3, solver.replay(:D2, 2).first[:provenance][:view_ordinal] + 1
      assert_equal 5, solver.replay(:D2, 2).first[:provenance][:material_id]
      assert solver.segments[:D2].all? { |segment| segment[:verdict] == :proven_correct }
    end

    def test_ambiguous_line_carries_two_witnesses
      spec = base_spec
      # v1 reads every item while v2 reads only group B, so both swapping and
      # not swapping can feed all the lines the sparse receiving book records.
      spec[:views] = [
        { version: :v1, sql: "SELECT id AS code, v AS label FROM items ORDER BY id" },
        { version: :v2, sql: "SELECT id AS code, v AS label FROM items WHERE grp = 'B' ORDER BY id" }
      ]
      spec[:ledger] = [
        { delivery: :D1, handle: :H1, events: [{ type: :row, line: 1 }] },
        { delivery: :D2, handle: :H2,
          events: [{ type: :row, line: 1 }, { type: :row, line: 2 }] }
      ]
      solver = HandoverAudit.audit(spec)
      assert solver.feasible?
      assert_operator solver.witnesses.length, :>=, 2

      views = solver.replay(:D1, 1).map { |entry| entry[:provenance][:view] }.sort
      assert_includes views, :v1
      assert_includes views, :v2
      assert_equal :ambiguous, solver.segments[:D1].first[:verdict]
    end

    def test_shared_group_reuses_one_choice_everywhere
      spec = base_spec
      spec[:groups][:echo] = [{ op: :pass }]
      spec[:handovers] = spec[:handovers].first(8) + [
        { id: :h8a, actions: [{ group: :echo }] },
        { id: :h8b, actions: [{ group: :echo }] }
      ]
      spec[:ledger] = [
        { delivery: :D1, handle: :H1, events: [{ type: :row, line: 1 }] },
        { delivery: :D2, handle: :H2, events: [{ type: :row, line: 1 }, { type: :row, line: 2 }] }
      ]
      solver = HandoverAudit.audit(spec)
      assert solver.feasible?
      solver.witnesses.each do |witness|
        echoes = witness.choices.values_at(8, 9).map { |choice| choice[:op] }
        assert_equal %i[pass pass], echoes
      end
    end

    def test_closing_one_cursor_closes_every_cursor_of_the_statement
      spec = base_spec
      spec[:handovers] = [
        { id: :h0, actions: [{ op: :prepare, statement: :S1 }] },
        { id: :h1, actions: [{ op: :execute, statement: :S1, handle: :H1, mode: :array }] },
        { id: :h2, actions: [{ op: :execute, statement: :S1, handle: :H2, mode: :array }] },
        { id: :h3, actions: [{ op: :close, handle: :H1 }] },
        { id: :h4, actions: [{ op: :step, handle: :H2 }] }
      ]
      spec[:ledger] = [
        { delivery: :D1, handle: :H1, events: [] },
        { delivery: :D2, handle: :H2, events: [{ type: :error, message: "closed statement" }] }
      ]
      solver = HandoverAudit.audit(spec)
      assert solver.feasible?, "expected the shared-close error to match the ledger"
      assert_equal :error, solver.witnesses.first.terminals.first.kind
    end

    def test_stale_binding_keeps_the_previous_parameter
      spec = {
        material: { table: "m", id_col: :id, rows: [
          { id: 1, k: "p", d: "old-param-row" }, { id: 2, k: "q", d: "q-row" }
        ] },
        views: [{ version: :v1, sql: "SELECT id AS code, d AS label, k AS key FROM m ORDER BY id" }],
        statements: [{ id: :S1, sql: "SELECT id AS code, d AS label FROM m WHERE k = :k ORDER BY id" }],
        deliveries: [{ id: :D1, statement: :S1, expect_view: :v1, id_column: :code,
                       columns: { code: :id, label: :d },
                       row_map: { method: :fixed, value: 1 } }],
        handovers: [
          { id: :h0, actions: [{ op: :prepare, statement: :S1 }] },
          { id: :h1, actions: [{ op: :execute, statement: :S1, handle: :H1, mode: :array,
                                 vars: { k: "p" } }] },
          { id: :h2, actions: [{ op: :step, handle: :H1 }] },
          { id: :h3, actions: [{ op: :execute, statement: :S1, handle: :H2, mode: :array }] },
          { id: :h4, actions: [{ op: :step, handle: :H2 }] }
        ],
        ledger: [
          { delivery: :D1, handle: :H1, events: [{ type: :row, line: 1 }] },
          { delivery: :D1, handle: :H2,
            events: [{ type: :sample, column: :label, value: "old-param-row" }, { type: :row, line: 2 }] }
        ]
      }
      solver = HandoverAudit.audit(spec)
      assert solver.feasible?, "the second execute must reuse the earlier :k binding"
      assert_equal 1, solver.replay(:D1, 2).first[:provenance][:material_id]
    end

    def test_infeasible_book_reports_earliest_mismatch
      spec = base_spec
      spec[:handovers] = [
        { id: :h0, actions: [{ op: :prepare, statement: :S1 }] },
        { id: :h1, actions: [{ op: :execute, statement: :S1, handle: :H1, mode: :hash }] },
        { id: :h2, actions: [{ op: :step, handle: :H1 }] }
      ]
      spec[:ledger] = [
        { delivery: :D1, handle: :H1,
          events: [{ type: :sample, column: :code, value: 999 }, { type: :row, line: 1 }] }
      ]
      solver = HandoverAudit.audit(spec)
      refute solver.feasible?
      mismatch = solver.earliest_mismatch
      assert_equal [0, 0], mismatch[:order]
      assert_match(/999/, mismatch[:reason][:problem])
    end

    def test_segment_row_map
      spec = base_spec
      spec[:deliveries].first[:row_map] = { method: :segment, source: [[1, 1, 3], [2, 2, 4]] }
      provenance = Provenance.new(Case.new(spec))
      assert_equal 3, provenance.expected_material_id(:D1, 1)
      assert_equal 4, provenance.expected_material_id(:D1, 2)
    end

    def test_identical_output_names_still_distinguish_view_source
      spec = base_spec
      spec[:views] = [
        { version: :v1, sql: "SELECT id AS code, v AS label FROM items WHERE id = 3" },
        { version: :v2, sql: "SELECT id AS code, v AS label FROM items WHERE id = 4" }
      ]
      spec[:handovers] = [
        { id: :h0, actions: [{ op: :prepare, statement: :S1 }] },
        { id: :h1, actions: [{ op: :execute, statement: :S1, handle: :H1, mode: :hash }] },
        { id: :h2, actions: [{ group: :maybe_swap }] },
        { id: :h3, actions: [{ op: :step, handle: :H1 }] }
      ]
      spec[:ledger] = [{ delivery: :D1, handle: :H1, events: [{ type: :row, line: 1 }] }]
      solver = HandoverAudit.audit(spec)
      assert solver.feasible?
      sources = solver.replay(:D1, 1).map { |entry| entry[:provenance][:material_id] }.sort
      assert_equal [3, 4], sources
    end

    def test_published_lines_are_numbered_delivery_wide_across_handles
      spec = base_spec
      # Both receipts belong to one delivery and read the shared statement;
      # the second handle must continue the delivery's published line numbers.
      spec[:ledger] = [
        { delivery: :D1, handle: :H1, events: [{ type: :row, line: 1 }] },
        { delivery: :D1, handle: :H2, events: [{ type: :row, line: 2 }] }
      ]
      solver = HandoverAudit.audit(spec)
      assert solver.feasible?
      handles = solver.witnesses.first.rows.map { |row| [row.pub_no, row.handle] }
      assert_equal [[1, :H1], [2, :H2]], handles
    end

    def test_closing_an_already_closed_cursor_records_error_terminal
      spec = base_spec
      spec[:handovers] = [
        { id: :h0, actions: [{ op: :prepare, statement: :S1 }] },
        { id: :h1, actions: [{ op: :execute, statement: :S1, handle: :H1, mode: :array }] },
        { id: :h2, actions: [{ op: :execute, statement: :S1, handle: :H2, mode: :array }] },
        { id: :h3, actions: [{ op: :close, handle: :H1 }] },
        { id: :h4, actions: [{ op: :close, handle: :H2 }] }
      ]
      spec[:ledger] = [
        { delivery: :D1, handle: :H1, events: [] },
        { delivery: :D2, handle: :H2, events: [{ type: :error, message: "closed statement" }] }
      ]
      solver = HandoverAudit.audit(spec)
      assert solver.feasible?
      assert_equal :error, solver.witnesses.first.terminals.first.kind
    end

    def test_replays_are_isolated_so_the_initial_view_survives_each_swap
      spec = base_spec
      spec[:handovers] = [
        { id: :h0, actions: [{ op: :prepare, statement: :S1 }] },
        { id: :h1, actions: [{ op: :execute, statement: :S1, handle: :H1, mode: :array }] },
        { id: :h2, actions: [{ group: :maybe_swap }] },
        { id: :h3, actions: [{ op: :step, handle: :H1 }] }
      ]
      # The no-swap choice must still see v1 even though another combination
      # swapped the same shared view to v2; savepoints must undo the swap.
      spec[:ledger] = [
        { delivery: :D1, handle: :H1, events: [{ type: :sample, column: :code, value: 1 }] }
      ]
      solver = HandoverAudit.audit(spec)
      assert solver.feasible?
      no_swap = solver.witnesses.find do |witness|
        witness.choices.all? { |choice| choice[:op] != :swap_view }
      end
      refute_nil no_swap
      assert_equal :v1, no_swap.rows.first.view
    end
  end
end

HandoverAudit::HandoverAuditTest.new.run
