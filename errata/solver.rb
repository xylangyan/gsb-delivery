# frozen_string_literal: true

require 'set'
require_relative 'loader'
require_relative 'replay'

module Errata
  # Finds the cheapest night list.  Deferral cost is paid for every batch held
  # back, so the search minimises the cost of unselected batches; equal-cost
  # lists are broken by preferring earlier approval numbers tonight.
  class Solver
    class Result < Struct.new(:feasible, :selected, :deferred, :deferral_cost,
                              :check_result, :traces, :unsat_core, keyword_init: true)
      def feasible?
        feasible
      end
    end

    def initialize(plan)
      @plan = plan
      @batches = plan.batches
      @costs = @batches.each_with_object({}) { |batch, memo| memo[batch.id] = batch.cost }
      @forced, @searchable = partition_batches
      @forced_ids = @forced.map(&:id)
      @memo = {}
    end

    def solve
      with_signatures(@plan.signatures) do
        best = search(@searchable.map(&:id), [], 0)
        return best if best

        core = find_unsat_core
        build_infeasible(core)
      end
    end

    private

    # A batch touching nothing any signature or work order can observe is
    # always cheaper to send tonight and cannot break a check.
    def partition_batches
      relevant = relevant_batch_ids
      forced = []
      searchable = []
      @batches.each do |batch|
        (relevant.include?(batch.id) ? searchable : forced) << batch
      end
      [forced, searchable]
    end

    def relevant_batch_ids
      ids = Set.new
      @plan.signatures.each do |signature|
        ids.merge(batches_touching_table(signature.table)) if signature.table
      end
      @plan.work_orders.each do |step|
        ids.merge(batches_touching_table(step['table'] || step[:table]))
      end
      ids
    end

    def batches_touching_table(table_id)
      return [] unless table_id

      @batches.select { |batch| batch.touches_table?(table_id) }.map(&:id)
    end

    def search(remaining, chosen, deferred_cost)
      return evaluate(chosen + @forced_ids) if remaining.empty?

      head = remaining.first
      tail = remaining[1..]

      # Approval-order tie-break: explore sending the earliest approval first.
      best = search(tail, chosen + [head], deferred_cost)
      # Strict pruning only: an equal-cost defer branch is still explored and
      # the approval-order tie is decided by #better, so a feasible deferred
      # solution is never lost merely because its select subtree was infeasible.
      defer_cost = deferred_cost + @costs.fetch(head)
      if best.nil? || defer_cost <= best.deferral_cost
        best = better(best, search(tail, chosen, defer_cost))
      end
      best
    end

    def better(current, candidate)
      return candidate if current.nil?
      return current if candidate.nil?
      return current if candidate.deferral_cost > current.deferral_cost
      return candidate if candidate.deferral_cost < current.deferral_cost

      prefer_earlier(current, candidate)
    end

    def prefer_earlier(left, right)
      left_set = left.selected.to_set
      right_set = right.selected.to_set
      @batches.each do |batch|
        next if left_set.include?(batch.id) == right_set.include?(batch.id)

        return left_set.include?(batch.id) ? left : right
      end
      left
    end

    # Returns a feasible Result, or nil when this selection breaks a check.
    def evaluate(selected_ids)
      key = selected_ids.sort
      return @memo[key] if @memo.key?(key)

      views = build_views(selected_ids)
      replay = run_replay(views)
      if replay.is_a?(Hash)
        @memo[key] = nil
        return nil
      end

      checker = SignatureChecker.new(views, replay, @plan.signatures, @plan.table_index)
      result = checker.check
      @memo[key] = result.ok? ? feasible(selected_ids, result, replay) : nil
      @memo[key]
    end

    def build_views(selected_ids)
      @plan.tables.each_with_object({}) do |table, memo|
        memo[table.id] = TableView.new(table, selected_ids.filter_map { |id| @plan.night.by_id[id] })
      end
    end

    def run_replay(views)
      replay = Replay.new(views, @plan.work_orders)
      replay.run
      replay
    rescue StandardError => e
      { failures: [SignatureChecker::Failure.new(
        signature_id: 'work_orders', kind: :tonight,
        reason: 'replay_error', detail: "#{e.class}: #{e.message}"
      )] }
    end

    def feasible(selected_ids, result, replay)
      selected_set = selected_ids.to_set
      deferred = @batches.reject { |batch| selected_set.include?(batch.id) }
      Result.new(
        feasible: true,
        selected: @batches.select { |batch| selected_set.include?(batch.id) }.map(&:id),
        deferred: deferred.map(&:id),
        deferral_cost: deferred.sum(&:cost),
        check_result: result, traces: replay.traces, unsat_core: nil
      )
    end

    # Irreducible infeasible set: keep dropping a signature while the remaining
    # requirements still admit no night list.
    def find_unsat_core
      core = @plan.signatures.dup
      @plan.signatures.dup.each do |candidate|
        trial = core - [candidate]
        next if trial.empty?

        core.delete(candidate) if with_signatures(trial) { search(@searchable.map(&:id), [], 0).nil? }
      end
      core
    end

    def with_signatures(signatures)
      original = @plan.signatures
      @plan.instance_variable_set(:@signatures, signatures)
      @memo.clear
      yield
    ensure
      @plan.instance_variable_set(:@signatures, original)
      @memo.clear
    end

    def build_infeasible(core)
      reference_ids = @batches.map(&:id)
      views = build_views(reference_ids)
      replay = run_replay(views)
      replay = nil if replay.is_a?(Hash)
      checker = SignatureChecker.new(views, replay || null_replay, core, @plan.table_index)
      result = checker.check
      Result.new(
        feasible: false, selected: [], deferred: reference_ids,
        deferral_cost: @batches.sum(&:cost), check_result: result,
        traces: replay&.traces || [], unsat_core: core
      )
    end

    def null_replay
      Object.new.tap do |replay|
        replay.define_singleton_method(:outputs) { {} }
      end
    end
  end
end
