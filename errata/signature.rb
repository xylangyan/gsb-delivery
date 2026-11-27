# frozen_string_literal: true

require_relative 'value'

module Errata
  # A signed slip (legacy or a reading designated for tonight).  Every field is
  # optional: a slip with only one filled item is checked on that item alone.
  class Signature
    attr_reader :id, :kind, :table, :output, :tickets, :allowed_results,
                :error_branch, :ordered, :match

    def initialize(spec, kind)
      @id = spec['id'] || spec[:id] || "#{kind}-#{object_id}"
      @kind = kind
      @table = spec['table'] || spec[:table]
      @output = spec['output'] || spec[:output]
      @tickets = Array(spec['tickets'] || spec[:tickets] || []).map { |value| Integer(value) }
      @allowed_results = (spec['allowed_results'] || spec[:allowed_results] || []).map do |value|
        normalize(value)
      end
      @error_branch = spec['error_branch'] || spec[:error_branch]
      @ordered = spec.fetch('ordered', spec.fetch(:ordered, true))
      # "all" (default): every observed value must be one of the allowed ones.
      # "any": the reading only needs to be obtainable, i.e. at least one
      # observed value is allowed.
      @match = spec['match'] || spec[:match] || 'all'
      raise ArgumentError, "signature #{@id} has nothing to check" if empty?
    end

    def empty?
      @tickets.empty? && @allowed_results.empty? && @error_branch.nil? && @output.nil?
    end

    def legacy?
      @kind == :legacy
    end

    def normalize(value)
      Errata.normalize_value(value)
    end
  end

  # Verifies a batch selection against every signed slip and tonight's
  # designated readings, reporting each failure with its original observation.
  class SignatureChecker
    class Result < Struct.new(:ok, :failures, keyword_init: true)
      def ok?
        ok
      end
    end
    Failure = Struct.new(:signature_id, :kind, :reason, :detail, keyword_init: true)

    def initialize(views, replay, signatures, base_tables)
      @views = views
      @replay = replay
      @signatures = signatures
      @base_tables = base_tables
    end

    def check
      failures = @signatures.flat_map { |signature| check_one(signature) }
      Result.new(ok: failures.empty?, failures: failures)
    end

    private

    def check_one(signature)
      if signature.output
        check_anchored(signature)
      else
        check_direct(signature)
      end
    end

    def check_anchored(signature)
      output = @replay.outputs[signature.output]
      return [fail(signature, 'missing_output', "output #{signature.output} was not produced")] if output.nil?

      failures = []
      check_branch(signature, output, failures)
      check_tickets(signature, output, failures)
      check_results(signature, output, failures)
      failures
    end

    def check_direct(signature)
      view = @views.fetch(signature.table) do
        return [fail(signature, 'unknown_table', "table #{signature.table}")]
      end
      failures = []
      observed = signature.tickets.map do |material|
        position = view.table.position_of_material(material)
        if position.nil?
          failures << fail(signature, 'missing_ticket',
                           "ticket #{material} is not on table #{signature.table}")
          next
        end
        {
          material: material,
          position: position,
          displayed: view.raw_category_at(position),
          winner: view.winner_at(position),
          original: view.original_category_at(position)
        }
      end.compact
      check_direct_results(signature, observed, failures)
      failures
    end

    def check_branch(signature, output, failures)
      return if signature.error_branch.nil?

      branch = output.trace[:branch]
      if branch != signature.error_branch
        failures << fail(signature, 'wrong_branch',
                         "expected #{signature.error_branch}, took #{branch.inspect}")
      end
    end

    def check_tickets(signature, output, failures)
      return if signature.tickets.empty?
      return if output.tickets == signature.tickets

      failures << fail(signature, 'ticket_mismatch',
                       "signed tickets #{signature.tickets.inspect}, replay read " \
                       "#{output.tickets.inspect} (same displayed value does not count " \
                       'when the original ticket differs)')
    end

    def check_results(signature, output, failures)
      return if signature.allowed_results.empty?

      allowed = signature.allowed_results
      observed = output.scalar? ? [output.value] : Array(output.value)
      matched =
        if signature.match == 'any'
          observed.any? { |value| allowed.include?(value) }
        elsif allowed.size == 1
          observed.all? { |value| allowed.include?(value) }
        else
          observed.tally == allowed.tally
        end
      return if matched

      failures << fail(signature, 'value_mismatch',
                       "allowed #{allowed.inspect} (#{signature.match}), observed #{observed.inspect}")
    end

    def check_direct_results(signature, observed, failures)
      return if signature.allowed_results.empty?

      if signature.match == 'any'
        return if observed.any? { |row| signature.allowed_results.include?(row[:displayed]) }

        failures << fail(signature, 'value_mismatch',
                         "none of #{signature.allowed_results.inspect} is obtainable " \
                         "(observed #{observed.map { |row| row[:displayed].inspect }.uniq})")
        return
      end

      observed.each do |row|
        next if signature.allowed_results.include?(row[:displayed])

        failures << fail(signature, 'value_mismatch',
                         "ticket #{row[:material]} displays #{row[:displayed].inspect}, " \
                         "allowed #{signature.allowed_results.inspect} " \
                         "(original template #{row[:original].inspect}, winner #{row[:winner].inspect})")
      end
    end

    def fail(signature, reason, detail)
      Failure.new(signature_id: signature.id, kind: signature.kind,
                  reason: reason, detail: detail)
    end
  end
end
