# frozen_string_literal: true

require "json"

module HandoverAudit
  # Renders a Solver result as a machine-readable Hash/JSON document and as the
  # three-band receiving verdict (proven wrong, proven correct, both possible).
  class Report
    VERDICT_LABELS = {
      proven_wrong: "proven-wrong-source",
      proven_correct: "proven-correct",
      ambiguous: "either-source-possible"
    }.freeze

    def initialize(audit_case, solver)
      @audit_case = audit_case
      @solver = solver
    end

    def to_h
      if @solver.feasible?
        {
          status: "feasible",
          combination_count: combination_count,
          compatible_handover_count: @solver.witnesses.length,
          deliveries: delivery_reports,
          replay: replay_index
        }
      else
        { status: "infeasible", combination_count: combination_count, earliest_mismatch: mismatch_report }
      end
    end

    def to_json(*args)
      JSON.pretty_generate(to_h, *args)
    end

    def to_text
      lines = []
      if @solver.feasible?
        lines << "Audit feasible: #{@solver.witnesses.length} of #{combination_count} handover choices fit the book."
        @audit_case.deliveries.each_key do |delivery|
          lines << ""
          lines << "Delivery #{delivery} (expected view #{@audit_case.deliveries[delivery][:expect_view]}):"
          @solver.segments.fetch(delivery, []).each do |segment|
            lines << "  lines #{segment[:from]}-#{segment[:to]}: #{VERDICT_LABELS.fetch(segment[:verdict])}"
          end
        end
        lines << ""
        lines << "Use replay(delivery, line) for the exact statement/view/material of any line."
      else
        lines << "No handover choice matches the receiving book."
        mismatch = @solver.earliest_mismatch
        lines << "Earliest unmatchable observation: #{format_mismatch(mismatch)}"
      end
      lines.join("\n")
    end

    private

    def delivery_reports
      @audit_case.deliveries.to_h do |delivery, info|
        segments = @solver.segments.fetch(delivery, []).map do |segment|
          {
            from_line: segment[:from], to_line: segment[:to],
            verdict: segment[:verdict], label: VERDICT_LABELS.fetch(segment[:verdict])
          }
        end
        [delivery, { expected_view: info[:expect_view], segments: segments }]
      end
    end

    def replay_index
      @audit_case.deliveries.to_h do |delivery, _info|
        detail = {}
        max_line = @solver.segments.fetch(delivery, []).map { |segment| segment[:to] }.max || 0
        (1..max_line).each do |line|
          detail[line] = @solver.replay(delivery, line).map do |entry|
            {
              view: entry[:provenance][:view],
              material_id: entry[:provenance][:material_id],
              view_row: entry[:provenance][:view_ordinal] + 1,
              cursor_row: entry[:provenance][:ordinal] + 1,
              mode: entry[:provenance][:mode],
              handle: entry[:provenance][:handle]
            }
          end
        end
        [delivery, detail]
      end
    end

    def mismatch_report
      mismatch = @solver.earliest_mismatch
      return nil unless mismatch

      reason = mismatch[:reason]
      {
        at: mismatch[:order],
        delivery: reason[:delivery],
        handle: reason[:handle],
        problem: reason[:problem],
        handover_choice: mismatch[:choices]
      }
    end

    def format_mismatch(mismatch)
      return "unknown" unless mismatch

      reason = mismatch[:reason]
      reason.is_a?(Hash) ? reason[:problem] : reason.to_s
    end

    def combination_count
      @audit_case.handovers.map { |handover| handover[:choices].length }.reduce(1, :*)
    end
  end
end
