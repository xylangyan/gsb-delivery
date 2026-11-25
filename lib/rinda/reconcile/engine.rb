# frozen_string_literal: false
require_relative '../reconcile' unless defined?(Rinda::Reconcile::Result)

module Rinda
  module Reconcile

    # Builds the partial order and decides each disputed receipt group.
    class Engine
      attr_reader :ledger, :observations

      def initialize(ledger, observations = {})
        @ledger = ledger
        @observations = observations
      end

      def replay
        order = build_order
        if order.cyclic?
          return Result.new(
            ledger: @ledger, order: order, attributions: [], groups: {},
            scenarios: [], reads: [],
            conflict: Conflict.new(
              'no event order satisfies the forced precedence',
              evidence: order.residual.map { |id| @ledger.event(id).to_s } + order.edge_evidence
            )
          )
        end
        analyze(order)
      end

      # Apply the summary observations cumulatively, in file order. Replay
      # after each line; the first line that makes the accumulated ledger
      # unreplayable is reported with its conflicting evidence.
      def replay_summary
        steps = []
        assignments = @observations.dup
        history = Hash.new { |h, k| h[k] = [] }
        @ledger.summary.each_with_index do |obs, index|
          line = index + 1
          conflict = nil

          if obs.is_a?(Hash) && obs['receipt'] && obs['source']
            receipt = obs['receipt']
            history[receipt] << obs['source']
            if history[receipt].uniq.size > 1
              conflict = Conflict.new(
                "summary assigns #{receipt} to both " \
                "#{history[receipt].uniq[0..-2].join(', ')} and #{obs['source']}",
                evidence: [{ receipt: receipt, statements: history[receipt] }]
              )
            else
              assignments = assignments.merge(receipt => obs)
            end
          end

          result = conflict ? summary_conflict_result(conflict) :
                              Engine.new(@ledger, assignments).replay
          steps << { line: line, observation: obs, result: result }
          return SummaryResult.new(steps, line, result, result) unless result.replayable?
        end
        SummaryResult.new(steps, nil, nil, steps.empty? ? replay : steps.last[:result])
      end

      def summary_conflict_result(conflict)
        Result.new(ledger: @ledger, order: nil, attributions: [], groups: {},
                   scenarios: [], reads: [], conflict: conflict)
      end

      private

      def build_order
        edges = []
        seen = {}
        add = lambda do |from, to, why|
          return if from.nil? || to.nil? || from == to
          key = [from, to]
          return if seen[key]
          seen[key] = true
          edges << Edge.new(from, to, why)
        end

        before_pairs.each { |from, to| add.call(from, to, 'ledger states order') }

        timed = @ledger.events.select(&:timed?)
        timed.combination(2) do |a, b|
          add_temporal(add, a, b)
          add_temporal(add, b, a)
        end

        # A completed read saw a specific write, when the archive says which.
        @ledger.events.each do |ev|
          next unless ev.read? && ev.returns?
          src = @ledger.read_sources[ev.id]
          add.call(src, ev.id, 'read observed this write') if src
        end

        # A stated receipt source constrains the transfer.
        @ledger.events.each do |ev|
          next if ev.source.nil?
          add.call(ev.source, ev.id, 'stated receipt source')
        end

        # An explicit assignment from a summary observation.
        @observations.each do |receipt, obs|
          add.call(obs['source'], receipt, 'summary observation') if obs['source']
        end

        Order.new(@ledger.events, edges)
      end

      def add_temporal(add, a, b)
        return unless a.finish && b.start
        return unless a.finish <= b.start
        if a.endpoint == b.endpoint
          add.call(a.id, b.id, "endpoint #{a.endpoint} chronology")
        elsif !same_window?(a, b)
          add.call(a.id, b.id, 'confirmed cross-endpoint clock')
        end
      end

      # Two calls in one window are only known to overlap. Their spans must
      # not be read as order.
      def same_window?(a, b)
        @windows ||= {}
        if @windows.empty?
          @ledger.windows.each do |w|
            w[:events].each { |ev| @windows[ev.id] = w.object_id }
          end
        end
        wa = @windows[a.id]
        wb = @windows[b.id]
        !wa.nil? && wa == wb
      end

      def before_pairs
        @ledger.before.map { |pair| pair.is_a?(Hash) ? [pair['from'], pair['to']] : pair }
      end

      def analyze(order)
        reads = collect_reads(order)
        read_conflict = reads.lazy.map { |r| [r, read_rule_violation(r.event, order)] }
                              .find { |_r, evidence| !evidence.nil? }
        if read_conflict
          read, evidence = read_conflict
          return Result.new(
            ledger: @ledger, order: order, attributions: [], groups: {},
            scenarios: [], reads: reads,
            conflict: Conflict.new(
              "read #{read.id} returned a value no write could have signalled",
              evidence: evidence
            )
          )
        end

        receipts = collect_receipts
        by_id = receipts.each_with_object({}) { |r, h| h[r.id] = r }

        read_scenarios = read_ambiguities(reads, order)

        # A summary observation that names a receipt and a source is a
        # constraint in its own right, disputed group or not.
        @observations.each do |receipt_id, obs|
          next if obs['source'].nil?
          receipt = by_id[receipt_id]
          unless receipt
            return Result.new(
              ledger: @ledger, order: order, attributions: [], groups: {},
              scenarios: [], reads: reads,
              conflict: Conflict.new(
                "summary names #{receipt_id}, which is not a receipt",
                evidence: [{ receipt: receipt_id, source: obs['source'] }]
              )
            )
          end
          possible = candidates(receipt, order).map(&:id)
          next if possible.include?(obs['source'])
          return Result.new(
            ledger: @ledger, order: order, attributions: [], groups: {},
            scenarios: [], reads: reads,
            conflict: Conflict.new(
              "summary assigns #{receipt_id} to #{obs['source']}, which cannot have supplied it",
              evidence: [{ receipt: receipt_id, label: receipt.label,
                           value: receipt.value, stated: obs['source'],
                           possible: possible }]
            )
          )
        end

        orphans = orphan_receipts(receipts, order)
        unless orphans.empty?
          evidence = orphans.map do |r|
            { receipt: r.id, label: r.label, value: r.value,
              reason: 'no write could have put a takeable entry with this value in the bag' }
          end
          return Result.new(
            ledger: @ledger, order: order, attributions: [], groups: {},
            scenarios: [], reads: reads,
            conflict: Conflict.new(
              "receipt #{orphans.first.id} has no possible source write",
              evidence: evidence
            )
          )
        end

        groups = group_receipts(receipts)

        # Every receipt gets an attribution, disputed or not: the question is
        # "which original writes could this registration have come from".
        all_attributions = receipts.map { |r| Attribution.new(r, candidates(r, order)) }

        verdicts = {}
        scenarios = []

        groups.each do |label, group|
          per = group.map { |receipt| all_attributions.find { |a| a.receipt == receipt } }

          decision = decide(group, per, order)
          verdicts[label] = decision
          scenarios.concat(decision[:scenarios]) if decision[:verdict] == :ambiguous
          next unless decision[:verdict] == :conflict
          return Result.new(
            ledger: @ledger, order: order, attributions: all_attributions,
            groups: verdicts, scenarios: scenarios, reads: reads,
            conflict: Conflict.new(
              "receipts for #{label} have no consistent source assignment",
              evidence: decision[:evidence]
            )
          )
        end

        Result.new(
          ledger: @ledger, order: order, attributions: all_attributions,
          groups: verdicts, scenarios: scenarios + read_scenarios,
          reads: reads, conflict: nil
        )
      end

      # A waiting read that could have seen more than one matching write
      # returns whichever the wake/lock order exposes last (or first). If the
      # archive does not pin that order, two orders are admissible:
      #   * reader re-acquires between the writes -> first value
      #   * both writes land before re-acquire    -> last value
      def read_ambiguities(reads, order)
        reads.filter_map do |read|
          next unless read.sources.size > 1
          observed = read.sources.select { |w| Reconcile.same_value?(w.tuple, read.value) }
          alternatives = read.sources - observed
          next if alternatives.empty? || observed.empty?

          first = observed.first
          last = alternatives.first
          next if order.before?(last.id, first.id) || order.before?(first.id, last.id)

          acquire_between = order.linearize(pairs: [[first.id, read.id], [read.id, last.id]])
          acquire_after = order.linearize(pairs: [[first.id, last.id], [last.id, read.id]])

          [
            {
              'scenario' => "read-#{read.id}-reacquired-between-writes",
              'assignments' => { read.id => first.id },
              'order' => acquire_between,
              'description' => "read #{read.id} woke and re-took the monitor after #{first.id}, " \
                               "so it returned #{first.id} before #{last.id} was written"
            },
            {
              'scenario' => "read-#{read.id}-reacquired-after-both",
              'assignments' => { read.id => last.id },
              'order' => acquire_after,
              'description' => "both writes signalled #{read.id} before it re-took the monitor, " \
                               "so it returned #{last.id}"
            }
          ].select { |s| order.respects?(s['order']) }
        end.flatten(1)
      end

      # A returning read and the writes whose value it could have observed.
      # Expired-at-write values are included: a waiting read is handed the
      # value directly, so seeing it is not evidence that it was takeable.
      def collect_reads(order)
        @ledger.events.select { |ev| ev.read? && ev.returns? }.map do |read|
          ReadObservation.new(read, read_visibility(read, order))
        end
      end

      # Check a read against TupleSpace#read: one wait, last matching write
      # before the monitor is re-taken overwrites @found, and no liveness
      # re-check happens afterwards.
      #
      # Returns nil when the recorded value is admissible, else evidence.
      def read_rule_violation(read, order)
        value = read.value
        candidates = read_visibility(read, order)
        return nil if candidates.any? { |w| Reconcile.same_value?(w.tuple, value) }

        # The value is not any write's tuple. It can still be a read of a
        # live entry whose content the log recorded only as a template match,
        # but absent that, report the mismatch for every write that could have
        # signalled this waiter.
        return [{ receipt: read.id, value: value,
                  candidates: candidates.map { |w| { write: w.id, tuple: w.tuple } } }]
      end

      # Completed transfers, plus an errored move whose push may already have
      # reached the port. An errored move is not proof either way; it is only
      # a possible deposit.
      def collect_receipts
        events = @ledger.events.select do |ev|
          next false unless ev.transfer?
          next false unless ev.recv
          next false if ev.returns? && ev.recv_value.nil? && ev.value.nil? && ev.tuple.nil?
          ev.returns? || ev.move?
        end
        # A shared recv label means one registration reported more than once.
        by_label = Hash.new { |h, k| h[k] = [] }
        events.each { |ev| by_label[ev.recv] << ev }
        receipts = []
        by_label.each do |label, evs|
          if evs.size > 1
            group = evs.map { |ev| Receipt.new(ev, evs) }
            receipts.concat(group)
          else
            ev = evs.first
            receipts << Receipt.new(ev, [ev])
          end
        end
        receipts
      end

      # Disputed groups: same label reported twice, or same content handed to
      # two different receipts.
      def group_receipts(receipts)
        groups = {}
        receipts.group_by(&:label).each do |label, rs|
          next unless rs.size > 1
          groups[label] = rs
        end
        receipts.group_by { |r| content_key(r.value) }.each do |key, rs|
          next unless rs.size > 1
          label = rs.map(&:label).uniq.sort.join('+')
          groups[label] ||= rs
        end
        groups
      end

      # A receipt with no possible source write at all is unreplayable on its
      # own, whether or not it shares content with another receipt.
      def orphan_receipts(receipts, order)
        receipts.select { |r| candidates(r, order).empty? }
      end

      def content_key(value)
        value.inspect
      end

      # Original writes that could have supplied this receipt.
      #
      # A take/move can only ever hand over an entry that entered the bag. A
      # write that was already dead at entry never did, so it is not a source
      # for a transfer -- even though a read waiter may have observed its
      # value (see #read_visibility).
      # Original writes that could have supplied this receipt.
      #
      # A take/move can only ever hand over an entry that entered the bag. A
      # write that was already dead at entry never did, so it is not a source
      # for a transfer -- even though a read waiter may have observed its
      # value (see #read_visibility).
      #
      # Which of several live same-value entries a particular take selected
      # depends on the bag state at that instant, including entries earlier
      # takes removed. That is resolved by the group's distinct/duplicate
      # analysis rather than pruned here: pruning a write just because a newer
      # one exists would wrongly forbid the older write supplying a later take.
      def candidates(receipt, order)
        transfer = receipt.event
        @ledger.events.select do |w|
          next false unless w.write?
          next false unless w.returns?
          next false if w.expired_at_entry?
          next false unless Reconcile.same_value?(w.tuple, receipt.value)
          next false if order.before?(transfer.id, w.id)
          next false unless window_open?(w, transfer)
          true
        end
      end

      # What a returning read could have observed. A read waiter is handed the
      # value directly when an entry is written, including an entry that was
      # already expired at write time; it does not re-check liveness after
      # waking. So a read's observation is not evidence that the value was
      # ever takeable.
      def read_visibility(read, order)
        @ledger.events.select do |w|
          next false unless w.write?
          next false unless w.returns?
          # A write forced to happen after the read cannot be what it saw.
          next false if order.before?(read.id, w.id)
          true
        end
      end

      # The write must still be live at a moment the transfer could see it.
      def window_open?(write, transfer)
        return true if transfer.start.nil? && transfer.finish.nil?
        exp = write.expiry
        return true if exp.nil?
        start = transfer.start || transfer.finish
        return true if start.nil?
        start < exp
      end

      def decide(group, attributions, order)
        by_receipt = attributions.each_with_object({}) { |a, h| h[a.receipt] = a }
        pinned = group.each_with_object({}) do |receipt, h|
          src = pinned_source(receipt)
          h[receipt] = src unless src.nil?
        end

        dup = duplicate_possible?(group, attributions, order)

        if pinned.size == group.size
          # Every receipt is pinned; the log has already answered.
          return { verdict: pinned.values.uniq.size == group.size ? :distinct : :duplicate }
        end

        distinct = distinct_possible?(group, attributions, pinned)

        if dup && distinct
          { verdict: :ambiguous, scenarios: ambiguous_scenarios(group, attributions, order) }
        elsif dup
          { verdict: :duplicate }
        elsif distinct
          { verdict: :distinct }
        else
          { verdict: :conflict,
            evidence: group.map { |r| { receipt: r.id, label: r.label,
                                        candidates: by_receipt[r]&.ids } } }
        end
      end

      # The source a receipt is explicitly pinned to: a summary observation
      # first, else the event's own `source`.
      def pinned_source(receipt)
        obs = @observations[receipt.id]
        return obs['source'] if obs && obs['source']
        receipt.event.source
      end

      # Could one source write have supplied two receipts?
      #
      # The entry must still be there for the second hand-off. That happens
      # when the *earlier* hand-off was a move whose push reached the port but
      # whose delete never ran (the move raised). Two receipts sharing one
      # registration label are one registration reported twice.
      #
      # A pinned source rules a receipt out for every other source.
      def duplicate_possible?(group, attributions, order)
        by_receipt = attributions.each_with_object({}) { |a, h| h[a.receipt] = a }
        group.combination(2).any? do |r1, r2|
          s1 = by_receipt[r1]
          s2 = by_receipt[r2]
          next false if s1.nil? || s2.nil?

          shared = s1.ids & s2.ids
          shared = shared.select { |id| pinned_source(r1).nil? || pinned_source(r1) == id }
          shared = shared.select { |id| pinned_source(r2).nil? || pinned_source(r2) == id }
          next false if shared.empty?

          if r1.label == r2.label
            true
          else
            earlier, _later = ordered_receipts(r1, r2, order)
            earlier.event.move? && earlier.event.raises? &&
              earlier.event.delivered != false
          end
        end
      end

      def ordered_receipts(r1, r2, order)
        return [r1, r2] if order.before?(r1.id, r2.id)
        return [r2, r1] if order.before?(r2.id, r1.id)
        [r1, r2]
      end

      def distinct_possible?(group, attributions, pinned = {})
        domains = group.map do |receipt|
          attribution = attributions.find { |a| a.receipt == receipt }
          ids = attribution ? attribution.ids : []
          pinned.key?(receipt) ? [pinned[receipt]] : ids
        end
        return false if domains.any?(&:empty?)
        assign_distinct(domains, 0, {})
      end

      # Backtracking over distinct source selection; small by construction.
      def assign_distinct(domains, index, used)
        return true if index == domains.size
        domains[index].each do |choice|
          next if used[choice]
          used[choice] = true
          result = assign_distinct(domains, index + 1, used)
          used.delete(choice)
          return true if result
        end
        false
      end

      # Two complete stories. Each is one ordering plus one assignment for
      # every receipt; neither mixes local verdicts from the other. The
      # ordering differs between stories when the two readings imply a
      # different wake/reacquire order (a waiting read can only report the
      # first or the last matching write).
      def ambiguous_scenarios(group, attributions, order)
        a, b = canonical_pair(group, attributions)
        return [] if a.nil? || b.nil?
        shared = a.ids & b.ids
        return [] if shared.empty?

        source = shared.first
        duplicate_assignment = { a.receipt.id => source, b.receipt.id => source }

        second = (a.ids | b.ids).find { |id| id != source }
        if second
          distinct_assignment = { a.receipt.id => source, b.receipt.id => second }
        else
          distinct_assignment = { a.receipt.id => source, b.receipt.id => source }
        end

        duplicate_order = order.linearize
        distinct_order =
          if second
            # The second receipt's source must be written before it is taken;
            # the first receipt's source likewise. Those are the only extra
            # constraints this story needs.
            order.linearize(
              pairs: [[source, a.receipt.id], [second, b.receipt.id]]
            )
          else
            duplicate_order
          end

        scenarios = []
        if order.respects?(duplicate_order)
          scenarios << {
            'scenario' => 'single-original-hand-off-twice',
            'assignments' => duplicate_assignment,
            'order' => duplicate_order,
            'description' => 'the entry survived the first hand-off and was taken again'
          }
        end
        if second && order.respects?(distinct_order)
          scenarios << {
            'scenario' => 'two-original-writes',
            'assignments' => distinct_assignment,
            'order' => distinct_order,
            'description' => 'the two receipts came from two original writes'
          }
        end
        scenarios
      end

      # Pick the two receipts whose ambiguity to tell, preferring a pair that
      # shares a candidate write, then any pair.
      def canonical_pair(group, attributions)
        by_receipt = attributions.each_with_object({}) { |a, h| h[a.receipt] = a }
        group.combination(2).each do |r1, r2|
          a = by_receipt[r1]
          b = by_receipt[r2]
          next if a.nil? || b.nil?
          return [a, b] unless (a.ids & b.ids).empty?
        end
        [nil, nil]
      end
    end
  end
end
