# frozen_string_literal: true

require "net/imap"
require "test/unit"

# Cross-checks the symbolic ArchiveProvenance engine against a brute-force
# oracle that enumerates every legal missing-EXPUNGE position and replays the
# mailbox state machine directly.
class ArchiveProvenanceOracleTest < Net::IMAP::TestCase
  AP = Net::IMAP::ArchiveProvenance

  class BruteForceLedger
    def initialize
      @appended   = Hash.new { |h, k| h[k] = [] }
      @deletions  = Hash.new { |h, k| h[k] = [] }
      @seqno_copies = []
      @fetches    = []
      @edges      = {}
      @events     = []
    end

    def snapshot(mailbox:, uidvalidity:, uids:, **)
      @appended[[mailbox, uidvalidity]].concat(uids.to_a)
    end

    def expunge(mailbox:, uidvalidity:, step:, candidate_runs:, var_id:, **)
      key = [mailbox, uidvalidity]
      event_index = @events.size
      @events << [:expunge, key]
      positions = candidate_runs.flat_map { |run| (run.first..run.last).to_a }
      @deletions[key] << {var_id: var_id, event_index: event_index,
                          positions: positions}
    end

    def copy(source_mailbox:, source_uidvalidity:, target_mailbox:,
             target_uidvalidity:, source_uids: nil, assigned_uids:,
             source_seqnos: nil, **)
      source_key = [source_mailbox, source_uidvalidity]
      target_key = [target_mailbox, target_uidvalidity]
      @appended[target_key].concat(assigned_uids)
      if source_uids
        source_uids.zip(assigned_uids).each do |uid, target_uid|
          @edges[AP.ref(target_mailbox, target_uidvalidity, target_uid)] =
            AP.ref(source_mailbox, source_uidvalidity, uid)
        end
      else
        event_index = @events.size
        @events << [:seqno_copy, source_key]
        @seqno_copies << {source_key: source_key, target_key: target_key,
                          event_index: event_index, seqnos: source_seqnos,
                          assigned: assigned_uids}
      end
    end

    def fetch(mailbox:, uidvalidity:, seqno:, uid:, **)
      key = [mailbox, uidvalidity]
      @events << [:fetch, key]
      @fetches << {key: key, event_index: @events.size - 1,
                   seqno: seqno, uid: uid}
    end

    def event_count = @events.size

    def verdict(ref1, ref2)
      same = false
      different = false
      each_assignment do |assignment|
        edges = build_edges(assignment)
        next if edges.nil? # assignment deletes a position that no longer exists
        next unless fetches_hold?(assignment)
        if origin(edges, ref1) == origin(edges, ref2)
          same = true
        else
          different = true
        end
      end
      if same && different then :unknown
      elsif same then :always_related
      elsif different then :never_related
      else :inconsistent end
    end

    # Returns [seqno, uid] for a FETCH that at least one assignment allows.
    def feasible_fetch(key, event_index, rng)
      each_assignment do |assignment|
        state = state_at(key, event_index, assignment)
        next if state.nil? || state.empty?
        seqno = 1 + rng.rand(state.size)
        return [seqno, state.fetch(seqno - 1)]
      end
      nil
    end

    private

    def each_assignment
      variables = @deletions.values.flatten
      combine(variables, {}) { |assignment| yield assignment }
    end

    def combine(deletions, acc)
      return [yield(acc)] if deletions.empty?
      deletion, *rest = deletions
      deletion[:positions].flat_map do |position|
        acc[deletion[:var_id]] = position
        combine(rest, acc) { |assignment| yield assignment }
      end
    end

    def state_at(key, event_index, assignment)
      remaining = @appended[key].dup
      @deletions[key].each do |deletion|
        next if deletion[:event_index] >= event_index
        position = assignment.fetch(deletion[:var_id])
        return nil if position > remaining.size
        remaining.delete_at(position - 1)
      end
      remaining
    end

    def build_edges(assignment)
      edges = @edges.dup
      @seqno_copies.each do |copy|
        state = state_at(copy[:source_key], copy[:event_index], assignment)
        return nil if state.nil?
        copy[:seqnos].each do |seqno|
          return nil if seqno > state.size
        end
        copy[:seqnos].zip(copy[:assigned]).each do |seqno, target_uid|
          source_uid = state.fetch(seqno - 1)
          edges[AP.ref(copy[:target_key].first, copy[:target_key].last,
                       target_uid)] =
            AP.ref(copy[:source_key].first, copy[:source_key].last, source_uid)
        end
      end
      edges
    end

    def origin(edges, ref)
      seen = {}
      current = ref
      while (parent = edges[current])
        raise "provenance cycle" if seen[current]
        seen[current] = true
        current = parent
      end
      current
    end

    def fetches_hold?(assignment)
      @fetches.all? do |fetch|
        state = state_at(fetch[:key], fetch[:event_index], assignment)
        next false if state.nil?
        state.fetch(fetch[:seqno] - 1, nil) == fetch[:uid]
      end
    end
  end

  def test_randomized_small_mailboxes_match_the_oracle
    rng = Random.new(20261125)
    80.times do
      scenario = build_random_scenario(rng.rand(1_000_000))
      symbolic = scenario[:ledger]
      oracle = scenario[:oracle]
      scenario[:query_refs].combination(2).each do |ref1, ref2|
        expected = oracle.verdict(ref1, ref2)
        actual, = symbolic.related?(ref1, ref2)
        assert_equal expected, actual,
          "scenario #{scenario[:seed]}: #{ref1} vs #{ref2}"
      end
    end
  end

  def test_ten_twenty_thirty_matches_the_oracle
    scenario = build_ten_twenty_thirty
    symbolic, oracle, refs = scenario[:ledger], scenario[:oracle],
                             scenario[:query_refs]
    refs.combination(2).each do |ref1, ref2|
      assert_equal oracle.verdict(ref1, ref2),
                   symbolic.related?(ref1, ref2).first,
                   "#{ref1} vs #{ref2}"
    end
  end

  private

  def build_ten_twenty_thirty
    ledger = AP::Ledger.new
    oracle = BruteForceLedger.new
    uids = [10, 20, 30]
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: uids)
    oracle.snapshot(mailbox: "src", uidvalidity: 5, uids: uids)
    var_id = 0
    run = AP::Run.new(first: 1, last: 2)
    ledger.expunge(mailbox: "src", uidvalidity: 5, step: 1,
                   candidate_runs: [run])
    oracle.expunge(mailbox: "src", uidvalidity: 5, step: 1,
                   candidate_runs: [run], var_id: var_id)
    refs = uids.map { |uid| AP.ref("src", 5, uid) }
    [[2, "dst-a", 9, [1, 2], [100, 101]],
     [3, "dst-b", 9, [1], [200]]].each do |step, box, validity, seqnos, assigned|
      ledger.copy(source_mailbox: "src", source_uidvalidity: 5,
                  target_mailbox: box, target_uidvalidity: validity,
                  step: step, source_seqnos: seqnos, assigned_uids: assigned)
      oracle.copy(source_mailbox: "src", source_uidvalidity: 5,
                  target_mailbox: box, target_uidvalidity: validity,
                  step: step, source_seqnos: seqnos, assigned_uids: assigned)
      assigned.each { |uid| refs << AP.ref(box, validity, uid) }
    end
    {ledger: ledger, oracle: oracle, query_refs: refs, seed: 0}
  end

  def build_random_scenario(seed)
    rng = Random.new(seed)
    ledger = AP::Ledger.new
    oracle = BruteForceLedger.new
    validity = 5
    uids = Array.new(3 + rng.rand(4)) { |idx| (idx + 1) * 10 }
    ledger.snapshot(mailbox: "src", uidvalidity: validity, uids: uids)
    oracle.snapshot(mailbox: "src", uidvalidity: validity, uids: uids)
    refs = uids.map { |uid| AP.ref("src", validity, uid) }
    var_count = 0
    copy_count = 0

    (1 + rng.rand(3)).times do
      break if uids.size - var_count <= 1
      if rng.rand(2) == 0
        # Always include seqno 1 so the candidate set survives clamping.
        extra = (2..uids.size).to_a.shuffle(random: rng).first(rng.rand(2))
        positions = [1, *extra].sort
        runs = positions.map { |pos| AP::Run.new(first: pos, last: pos) }
        ledger.expunge(mailbox: "src", uidvalidity: validity,
                       step: copy_count * 10 + 1, candidate_runs: runs)
        oracle.expunge(mailbox: "src", uidvalidity: validity,
                       step: copy_count * 10 + 1, candidate_runs: runs,
                       var_id: var_count)
        var_count += 1
      end

      target = "dst-#{copy_count}"
      target_validity = 9 + copy_count
      # Every assignment has deleted one message per prior expunge, so this is
      # the smallest mailbox size any replay can have at this point.
      guaranteed_size = uids.size - var_count
      take = 1 + rng.rand(guaranteed_size)
      seqnos = (1..take).to_a
      assigned = Array.new(take) { |idx| 1000 + copy_count * 100 + idx }
      step = copy_count * 10 + 2
      if rng.rand(2) == 0
        # COPYUID pairing uses actual UIDs; pick the highest UIDs, which the
        # low-position candidate deletions can never remove.
        source_uids = uids.last(take)
        ledger.copy(source_mailbox: "src", source_uidvalidity: validity,
                    target_mailbox: target, target_uidvalidity: target_validity,
                    step: step, source_uids: source_uids,
                    assigned_uids: assigned)
        oracle.copy(source_mailbox: "src", source_uidvalidity: validity,
                    target_mailbox: target, target_uidvalidity: target_validity,
                    step: step, source_uids: source_uids,
                    assigned_uids: assigned)
      else
        ledger.copy(source_mailbox: "src", source_uidvalidity: validity,
                    target_mailbox: target, target_uidvalidity: target_validity,
                    step: step, source_seqnos: seqnos,
                    assigned_uids: assigned)
        oracle.copy(source_mailbox: "src", source_uidvalidity: validity,
                    target_mailbox: target, target_uidvalidity: target_validity,
                    step: step, source_seqnos: seqnos,
                    assigned_uids: assigned)
      end
      assigned.each { |uid| refs << AP.ref(target, target_validity, uid) }

      if rng.rand(2) == 0
        measurement = oracle.feasible_fetch(["src", validity],
                                            oracle.event_count, rng)
        if measurement
          seqno, uid = measurement
          fetch_step = copy_count * 10 + 3
          ledger.fetch(mailbox: "src", uidvalidity: validity,
                       seqno: seqno, uid: uid, step: fetch_step)
          oracle.fetch(mailbox: "src", uidvalidity: validity,
                       seqno: seqno, uid: uid, step: fetch_step)
        end
      end
      copy_count += 1
    end

    {ledger: ledger, oracle: oracle, query_refs: refs.uniq, seed: seed}
  end
end
