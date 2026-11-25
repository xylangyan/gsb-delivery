# frozen_string_literal: true

module Net
  class IMAP < Protocol

    # = \Net::IMAP::ArchiveProvenance
    #
    # Offline reconstruction of which archived message copies share the same
    # original message, replaying an archival transcript (a "statement")
    # without opening any connection.
    #
    # A message is located by the triple _mailbox name_, _UIDVALIDITY_ (the
    # mailbox generation) and _UID_.  Two copies are the same mail only when
    # their provenance chains, followed through the \COPY records, reach the
    # same original; equal bodies are irrelevant.  When a mailbox of the same
    # name is recreated, the new generation starts with a new UIDVALIDITY and
    # old copies still trace the generation they were copied from.
    #
    #   ledger = Net::IMAP::ArchiveProvenance::Ledger.new
    #   ledger.snapshot(mailbox: "INBOX", uidvalidity: 102, uids: 10..30)
    #   ledger.copy(source_mailbox: "INBOX", source_uidvalidity: 102,
    #               target_mailbox: "Archive", target_uidvalidity: 7,
    #               source_uids: [10, 20], assigned_uids: [100, 101], step: 3)
    #   verdict, witnesses = ledger.related?(ref1, ref2)
    #
    # == Ambiguity kept symbolic
    #
    # A \COPY without a +COPYUID+ response code is paired by message sequence
    # number, smallest first, against the assigned-UID order recorded in the
    # handoff table; a \COPY _with_ +COPYUID+ uses the actual UID pairing.
    #
    # A missing \EXPUNGE only loses the deleted message sequence number: each
    # one becomes a variable whose domain is the collector's candidate runs
    # (intervals).  A later \FETCH UID is a hard measurement and narrows that
    # domain with simple threshold constraints.  Sequence-to-UID lookups are
    # evaluated as piecewise expressions over those variables, so a lost
    # position inside a million-message range is handled with a few interval
    # splits instead of enumerating every candidate.  Two copies that depend
    # on the same variable stay correlated (they match in every replay even
    # when neither has a unique source on its own).
    module ArchiveProvenance
      class << self

        # Builds a Reference.
        def ref(mailbox, uidvalidity, uid)
          Reference.new(mailbox: mailbox, uidvalidity: uidvalidity, uid: uid)
        end

      end

      # A message reference: mailbox +mailbox+, generation +uidvalidity+ and
      # +uid+ within that generation.
      Reference = Data.define(:mailbox, :uidvalidity, :uid) do
        def initialize(mailbox:, uidvalidity:, uid:)
          super(mailbox: String(mailbox),
                uidvalidity: Integer(uidvalidity),
                uid: Integer(uid))
        end

        def to_s = "#{mailbox}/#{uidvalidity}/#{uid}"
      end

      # Raised when a record cannot belong to any replay of the statement
      # (unknown generation, empty sequence position, malformed pairing...).
      class InconsistentStatement < Error
      end

      # An inclusive run of consecutive sequence numbers or UIDs.
      Run = Data.define(:first, :last) do
        def initialize(first:, last:)
          first = Integer(first)
          last  = Integer(last)
          raise ArgumentError, "inverted run #{first}..#{last}" if first > last
          super(first: first, last: last)
        end

        def to_s = first == last ? first.to_s : "#{first}:#{last}"
      end

      # A domain of a missing-EXPUNGE variable: an ordered list of disjoint
      # Runs.  All operations are interval arithmetic; the individual sequence
      # positions are never enumerated.
      class Domain
        # @return [Array<Run>]
        attr_reader :runs

        def initialize(runs)
          @runs = merge_runs(Array(runs).map { |r| to_run(r) })
        end

        def empty? = runs.empty?

        # @return [Run, nil] the portion with values <= +threshold+
        def restrict_le(threshold)
          parts = runs.filter_map do |run|
            next if run.first > threshold
            Run.new(first: run.first, last: [run.last, threshold].min)
          end
          parts.empty? ? nil : Domain.new(parts)
        end

        # @return [Run, nil] the portion with values > +threshold+
        def restrict_gt(threshold)
          parts = runs.filter_map do |run|
            next if run.last <= threshold
            Run.new(first: [run.first, threshold + 1].max, last: run.last)
          end
          parts.empty? ? nil : Domain.new(parts)
        end

        # Intersects with a single threshold condition, +op+ being +:le+ or
        # +:gt+.  Returns the narrowed domain, or nil when nothing survives.
        def restrict(op, threshold)
          op == :le ? restrict_le(threshold) : restrict_gt(threshold)
        end

        # Applies threshold conditions, each given as <tt>[op, threshold]</tt>.
        def apply_conditions(conditions)
          domain = self
          conditions.each do |op, threshold|
            domain = domain.restrict(op, threshold)
            return nil if domain.nil?
          end
          domain
        end

        # Smallest value in the domain.
        def smallest = runs.first.first

        # Union of several domains (any branch may be true).
        def self.union(domains)
          new(domains.compact.flat_map(&:runs))
        end

        private

        def to_run(value)
          value.is_a?(Run) ? value : Run.new(first: value.begin, last: value.end)
        end

        def merge_runs(runs)
          runs.sort_by(&:first).each_with_object([]) do |run, merged|
            if merged.last && run.first <= merged.last.last + 1
              prev = merged.pop
              merged << Run.new(first: prev.first,
                                last: [prev.last, run.last].max)
            else
              merged << run
            end
          end
        end
      end

      # One branch of a piecewise expression: when every condition in +conds+
      # holds, the expression takes +value+.  Each variable may carry a lower
      # bound (<tt>:gt</tt>, exclusive) and an upper bound (<tt>:le</tt>,
      # inclusive), stored as <tt>{var_id => {le: t, gt: t}}</tt>.
      class Piece
        # @return [Hash{Integer => Hash{Symbol => Integer}}]
        attr_reader :conds
        # @return [Reference, Integer]
        attr_reader :value

        def initialize(conds, value)
          @conds = conds
          @value = value
        end

        # Returns a copy with one more bound, or nil when it contradicts an
        # existing bound on the same variable.
        def with_condition(var_id, op, threshold)
          merged = self.class.add_condition(conds, var_id, op, threshold)
          merged && Piece.new(merged, value)
        end

        # Combines the conditions of two pieces, or nil on contradiction.
        def merge_conditions(other)
          merged = self.class.deep_dup(conds)
          other.conds.each do |var_id, bounds|
            bounds.each do |op, threshold|
              merged = self.class.add_condition(merged, var_id, op, threshold)
              return nil if merged.nil?
            end
          end
          merged
        end

        # Adds a bound, tightening the existing one or detecting a
        # contradiction; returns the updated conditions hash or nil.
        def self.add_condition(conds, var_id, op, threshold)
          merged = deep_dup(conds)
          bounds = merged[var_id] ||= {}
          if bounds.key?(op)
            bounds[op] = [bounds[op], threshold].send(op == :le ? :min : :max)
          else
            bounds[op] = threshold
          end
          lower = bounds[:gt]
          upper = bounds[:le]
          return nil if lower && upper && lower >= upper
          merged
        end

        def self.deep_dup(conds)
          conds.transform_values(&:dup)
        end
      end

      # Replay state for one generation of one mailbox.  UID segments are kept
      # as ranges (never expanded), so a million-UID mailbox costs one Range.
      class MailboxGeneration
        # @return [String]
        attr_reader :mailbox
        # @return [Integer]
        attr_reader :uidvalidity
        # @return [Array<Hash>] ordered UID segments, each ascending
        attr_reader :segments
        # @return [Array<Hash>] missing expunges, in occurrence order
        attr_reader :deletions

        def initialize(mailbox:, uidvalidity:, uids:, step: 0)
          @mailbox     = String(mailbox)
          @uidvalidity = Integer(uidvalidity)
          @segments    = []
          @deletions   = []
          append_uids(uids, visible_step: step)
        end

        # Number of messages appended so far (before any expunge), restricted
        # to segments readable at transcript +step+.
        def appended_count(step = nil)
          visible_segments(step).sum { |segment| segment[:range].size }
        end

        # Appends delivered UIDs.  A Range is stored as one segment and never
        # expanded; +visible_step+ is the transcript step from which later
        # commands may read the new messages (a COPY batch only becomes
        # readable at its entry point into the target mailbox).
        def append_uids(uids, visible_step:)
          if uids.is_a?(Range)
            append_range(uids, visible_step)
          else
            Array(uids).each do |uid|
              append_range((Integer(uid)..Integer(uid)), visible_step)
            end
          end
        end

        # UID at the +index+-th appended position (1-based) among segments
        # readable at +step+, independent of expunges.
        def appended_uid_at(index, step = nil)
          visible = visible_segments(step)
          total = visible.sum { |segment| segment[:range].size }
          if index < 1 || index > total
            raise InconsistentStatement,
                  "seqno #{index} is empty in #{mailbox} gen #{uidvalidity}"
          end
          offset = index
          visible.each do |segment|
            range = segment[:range]
            return range.begin + offset - 1 if offset <= range.size
            offset -= range.size
          end
        end

        # Number of messages present at transcript +step+, after every
        # expunge recorded before that step.
        def count_at(step, event_index)
          appended_count(step) -
            deletions.count { |d| d[:event_index] < event_index }
        end

        # Registers a missing EXPUNGE variable at transcript +event_index+.
        def register_deletion(var_id, event_index, step, max_position)
          deletions << {var_id: var_id, event_index: event_index,
                        step: step, max_position: max_position}
        end

        # Piecewise UID expression for the message at sequence position
        # +seqno+ at transcript event +event_index+ (step +step+), under
        # +domains+.  Returns Piece objects whose value is an Integer UID.
        #
        # Deletions are undone in reverse chronological order: starting from
        # the final sequence position +seqno+, a deletion at a position at or
        # before the position currently traced pushes the appended index up
        # by one; a later deletion leaves it untouched.
        def uid_expression(event_index, seqno, domains, step)
          applicable = deletions.reject { |d| d[:event_index] >= event_index }
          pieces = [Piece.new({}, seqno)]
          applicable.reverse_each do |deletion|
            var_id = deletion[:var_id]
            domain = domains.fetch(var_id)
            size_before_deletion = deletion[:max_position]
            pieces = pieces.flat_map do |piece|
              position = piece.value
              undo_shift(piece, var_id, domain, position,
                         size_before_deletion) +
                keep_position(piece, var_id, domain, position)
            end
          end
          pieces.map do |piece|
            Piece.new(piece.conds, appended_uid_at(piece.value, step))
          end
        end

        private

        def append_range(range, visible_step)
          range = (Integer(range.begin)..Integer(range.end))
          if segments.last && segments.last[:visible_step] <= visible_step &&
             range.begin == segments.last[:range].end + 1
            previous = segments.pop[:range]
            segments << {range: (previous.begin..range.end),
                         visible_step: visible_step}
          else
            segments << {range: range, visible_step: visible_step}
          end
        end

        def visible_segments(step)
          return segments if step.nil?
          segments.select { |segment| segment[:visible_step] <= step }
        end

        # The deletion sits at or before the traced position: undoing it
        # moves the appended index up by one.  Impossible when the traced
        # position was the last message remaining after that deletion, since
        # no message can then have sat above the deleted one.
        def undo_shift(piece, var_id, domain, position, size_before_deletion)
          return [] if position >= size_before_deletion
          return [] if domain.restrict_le(position).nil?
          shifted = Piece.new(piece.conds, piece.value + 1)
          next_piece = shifted.with_condition(var_id, :le, position)
          next_piece ? [next_piece] : []
        end

        # The deletion sits after the traced position: the appended index is
        # unchanged.
        def keep_position(piece, var_id, domain, position)
          return [] if domain.restrict_gt(position).nil?
          next_piece = piece.with_condition(var_id, :gt, position)
          next_piece ? [next_piece] : []
        end
      end

      # Accumulates the archival statement and answers provenance queries.
      class Ledger
        # @return [Hash{Array(String,Integer) => MailboxGeneration}]
        attr_reader :generations
        # @return [Array<Hash>] every record, in transcript order
        attr_reader :records
        # @return [Hash{Reference => Array<Piece>]} target ref -> source expr
        attr_reader :edges
        # @return [Hash{Integer => Domain}] expunge variable id -> domain
        attr_reader :domains

        def initialize
          @generations = {}
          @records     = []
          @edges       = {}
          @domains     = {}
          @variables   = {}
          @unplayable  = []
          @event_index = 0
        end

        # Records the initial ordered UID segment of a mailbox generation.
        # +uids+ may be an array, a Range, or any enumerable of integers;
        # ranges are kept compact and never expanded.
        def snapshot(mailbox:, uidvalidity:, uids:, step: 0)
          key = [String(mailbox), Integer(uidvalidity)]
          if generations.key?(key)
            raise ArgumentError,
                  "snapshot already recorded for #{mailbox} gen #{uidvalidity}"
          end
          generations[key] = MailboxGeneration.new(
            mailbox: mailbox, uidvalidity: uidvalidity, uids: uids, step: step)
          self
        end

        # Records UIDs newly delivered into an existing generation.
        def deliver(mailbox:, uidvalidity:, uids:, step:)
          generation!(mailbox, uidvalidity).append_uids(uids, visible_step: step)
          records << register_record(:deliver, step,
                                     mailbox: mailbox, uidvalidity: uidvalidity)
          self
        end

        # Records a \COPY.
        #
        # With +source_uids+ (a +COPYUID+ response), sources and
        # +assigned_uids+ are paired in order as actual UIDs.  Without it,
        # +source_seqnos+ are the message sequence numbers at the retrieval
        # point, smallest first, paired with +assigned_uids+ in that order.
        # +visible_step+ marks when later commands can read the new copies;
        # it defaults to the retrieval +step+.
        def copy(source_mailbox:, source_uidvalidity:,
                 target_mailbox:, target_uidvalidity:,
                 step:, visible_step: nil,
                 source_uids: nil, assigned_uids:, source_seqnos: nil)
          visible_step ||= step
          if visible_step < step
            raise ArgumentError, "visible_step cannot precede the COPY step"
          end
          source = generation!(source_mailbox, source_uidvalidity)
          target_key = [String(target_mailbox), Integer(target_uidvalidity)]
          target = generations[target_key] ||= MailboxGeneration.new(
            mailbox: target_mailbox, uidvalidity: target_uidvalidity,
            uids: [], step: visible_step)
          assigned = Array(assigned_uids).map(&:to_i)
          event_index = @event_index
          if source_uids
            sources = Array(source_uids).map(&:to_i)
            unless sources.size == assigned.size
              raise ArgumentError, "source and assigned UID counts differ"
            end
            pieces_by_target = sources.zip(assigned).to_h do |uid, target_uid|
              ref = Reference.new(mailbox: source.mailbox,
                                  uidvalidity: source.uidvalidity, uid: uid)
              [target_uid, [Piece.new({}, ref)]]
            end
          else
            unless source_seqnos
              raise ArgumentError,
                    "COPY without COPYUID needs source_seqnos"
            end
            seqnos = Array(source_seqnos).map(&:to_i)
            unless seqnos == seqnos.sort && seqnos.uniq.size == seqnos.size
              raise ArgumentError,
                    "source_seqnos must be distinct and in ascending order"
            end
            unless seqnos.size == assigned.size
              raise ArgumentError, "seqno and assigned UID counts differ"
            end
            pieces_by_target = seqnos.zip(assigned).to_h do |seqno, target_uid|
              [target_uid,
               source.uid_expression(event_index, seqno, domains, step).map do |piece|
                 Piece.new(piece.conds,
                           Reference.new(mailbox: source.mailbox,
                                         uidvalidity: source.uidvalidity,
                                         uid: piece.value))
               end]
            end
          end
          record = register_record(:copy, step,
                                   source_mailbox: source.mailbox,
                                   source_uidvalidity: source.uidvalidity,
                                   target_mailbox: target.mailbox,
                                   target_uidvalidity: target.uidvalidity,
                                   assigned_uids: assigned,
                                   source_uids: source_uids,
                                   source_seqnos: source_seqnos,
                                   visible_step: visible_step)
          target.append_uids(assigned, visible_step: visible_step)
          pieces_by_target.each do |target_uid, pieces|
            target_ref = Reference.new(mailbox: target.mailbox,
                                       uidvalidity: target.uidvalidity,
                                       uid: target_uid)
            edges[target_ref] = pieces
          end
          record
        end

        # Records a missing \EXPUNGE at +step+.  +candidate_runs+ is one or
        # more runs of sequence positions; exactly one message was deleted.
        def expunge(mailbox:, uidvalidity:, step:, candidate_runs:)
          gen = generation!(mailbox, uidvalidity)
          var_id = @variables.size
          runs = Array(candidate_runs).map do |run|
            run.is_a?(Run) ? run : Run.new(first: run.begin, last: run.end)
          end
          record = register_record(:expunge, step,
                                   mailbox: mailbox, uidvalidity: uidvalidity,
                                   candidate_runs: runs, var_id: var_id)
          max_position = gen.count_at(step, record[:event_index])
          clamped = Domain.new(runs).restrict_le(max_position)
          if clamped.nil?
            raise InconsistentStatement,
                  "EXPUNGE candidates in #{mailbox} gen #{uidvalidity} all " \
                  "exceed mailbox size #{max_position} at step #{step}"
          end
          @variables[var_id] = record
          @domains[var_id] = clamped
          gen.register_deletion(var_id, record[:event_index], step, max_position)
          record
        end

        # Records a measured \FETCH UID: +uid+ was at sequence position
        # +seqno+ at +step+.  Measurements contradicting every possible
        # expunge position are held back for #inconsistent_observations.
        def fetch(mailbox:, uidvalidity:, seqno:, uid:, step:)
          gen = generation!(mailbox, uidvalidity)
          record = register_record(:fetch, step,
                                   mailbox: mailbox, uidvalidity: uidvalidity,
                                   seqno: Integer(seqno), uid: Integer(uid))
          pieces =
            begin
              gen.uid_expression(record[:event_index], Integer(seqno),
                                 domains, step)
            rescue InconsistentStatement
              []
            end
          survivors = pieces.select { |piece| piece.value == uid }
          if survivors.empty?
            @unplayable << record.merge(reason: "no candidate position leaves " \
                                                "UID #{uid} at seqno #{seqno}")
            return record
          end
          narrow_by_fetch(gen, survivors)
          record
        end

        # :call-seq:
        #   ledger.related?(ref1, ref2) -> [verdict, witnesses]
        #
        # +verdict+ is one of:
        #
        # - +:always_related+ — every replay reaches the same original,
        # - +:never_related+ — every replay keeps the two originals apart,
        # - +:unknown+ — both outcomes have at least one replay,
        # - +:inconsistent+ — no replay survives the measurements; see
        #   #inconsistent_observations.
        #
        # +witnesses+ is keyed by +:same+ and +:different+; each entry is a
        # list of concrete replays (expunge positions plus provenance chains)
        # that can be checked against the original records.
        def related?(ref1, ref2)
          ref1 = coerce_ref(ref1)
          ref2 = coerce_ref(ref2)
          ensure_known_ref!(ref1)
          ensure_known_ref!(ref2)
          expr1 = origin_expression(ref1)
          expr2 = origin_expression(ref2)
          same_cells = []
          different_cells = []
          expr1.each do |piece1|
            expr2.each do |piece2|
              conds = piece1.merge_conditions(piece2)
              next if conds.nil?
              next unless conditions_satisfiable?(conds)
              cell = [piece1, piece2, conds]
              if piece1.value == piece2.value
                same_cells << cell
              else
                different_cells << cell
              end
            end
          end
          verdict =
            if same_cells.empty? && different_cells.empty?
              :inconsistent
            elsif different_cells.empty?
              :always_related
            elsif same_cells.empty?
              :never_related
            else
              :unknown
            end
          witnesses = {}
          witnesses[:same] = same_cells.map do |piece1, piece2, conds|
            build_witness(ref1, ref2, piece1, piece2, conds)
          end
          witnesses[:different] = different_cells.map do |piece1, piece2, conds|
            build_witness(ref1, ref2, piece1, piece2, conds)
          end
          [verdict, witnesses]
        end

        # FETCH records that cannot be part of any replay, each with the
        # reason and the record coordinates needed to find it in the
        # statement.
        def inconsistent_observations = @unplayable.dup

        # Looks up a generation, raising on unknown mailbox/UIDVALIDITY.
        def generation!(mailbox, uidvalidity)
          generations.fetch([String(mailbox), Integer(uidvalidity)]) do
            raise InconsistentStatement,
                  "unknown generation #{mailbox} UIDVALIDITY #{uidvalidity}"
          end
        end

        # Follows the provenance edges for a concrete assignment of expunge
        # positions and returns the full chain from +ref+ to its original.
        def chain_for(ref, assignment)
          ref = coerce_ref(ref)
          ensure_known_ref!(ref)
          chain = [ref]
          seen = {}
          while (pieces = edges[ref])
            raise InconsistentStatement, "provenance cycle at #{ref}" if seen[ref]
            seen[ref] = true
            matching = pieces.find { |piece| holds?(piece.conds, assignment) }
            raise InconsistentStatement,
                  "no edge branch matches the assignment for #{ref}" unless matching
            ref = matching.value
            chain << ref
          end
          chain
        end

        private

        def register_record(kind, step, **fields)
          record = {kind: kind, step: step, event_index: @event_index, **fields}
          @event_index += 1
          records << record
          record
        end

        def coerce_ref(value)
          return value if value.is_a?(Reference)
          Reference.new(mailbox: value.fetch(:mailbox),
                        uidvalidity: value.fetch(:uidvalidity),
                        uid: value.fetch(:uid))
        end

        def ensure_known_ref!(ref)
          gen = generation!(ref.mailbox, ref.uidvalidity)
          known = gen.segments.any? { |segment| segment[:range].cover?(ref.uid) }
          unless known
            raise InconsistentStatement,
                  "UID #{ref.uid} was never present in #{ref.mailbox} " \
                  "generation #{ref.uidvalidity}"
          end
        end

        def narrow_by_fetch(gen, survivors)
          gen.deletions.map { _1[:var_id] }.uniq.each do |var_id|
            narrowed = Domain.union(survivors.filter_map do |piece|
              bounds = piece.conds[var_id]
              domain = domains.fetch(var_id)
              next domain if bounds.nil?
              bounds.reduce(domain) do |current, (op, threshold)|
                current_restricted = current.restrict(op, threshold)
                break nil if current_restricted.nil?
                current_restricted
              end
            end)
            @domains[var_id] = narrowed unless narrowed.empty?
          end
        end

        # Expands a reference's edge expression until every leaf is an
        # original (a reference with no incoming edge), inlining conditions.
        def origin_expression(ref, seen = {})
          return [Piece.new({}, ref)] unless edges.key?(ref)
          raise InconsistentStatement, "provenance cycle at #{ref}" if seen[ref]
          seen = seen.merge(ref => true)
          edges.fetch(ref).flat_map do |edge_piece|
            origin_expression(edge_piece.value, seen).filter_map do |inner|
              merged = edge_piece.merge_conditions(inner)
              Piece.new(merged, inner.value) if merged
            end
          end
        end

        def conditions_satisfiable?(conds)
          conds.each do |var_id, bounds|
            domain = domains.fetch(var_id)
            bounds.each do |op, threshold|
              domain = domain.restrict(op, threshold)
              return false if domain.nil?
            end
          end
          true
        end

        def holds?(conds, assignment)
          conds.all? do |var_id, bounds|
            position = assignment.fetch(var_id)
            bounds.all? do |op, threshold|
              op == :le ? position <= threshold : position > threshold
            end
          end
        end

        def build_witness(ref1, ref2, piece1, piece2, conds)
          assignment = {}
          domains.each_key do |var_id|
            bounds = conds[var_id]
            narrowed =
              if bounds
                domains.fetch(var_id).apply_conditions(bounds.to_a)
              else
                domains.fetch(var_id)
              end
            raise InconsistentStatement,
                  "witness cell has no satisfying expunge position" if narrowed.nil?
            assignment[var_id] = narrowed.smallest
          end
          chain1 = chain_for(ref1, assignment)
          chain2 = chain_for(ref2, assignment)
          {
            expunge_positions: render_assignment(assignment),
            origins: {ref1 => chain1.last, ref2 => chain2.last},
            chains: {ref1 => chain1, ref2 => chain2},
          }
        end

        def render_assignment(assignment)
          assignment.to_h do |var_id, position|
            record = @variables.fetch(var_id)
            ["#{record[:mailbox]} gen #{record[:uidvalidity]} " \
             "step #{record[:step]} (expunge ##{var_id})", position]
          end
        end
      end

    end
  end
end
