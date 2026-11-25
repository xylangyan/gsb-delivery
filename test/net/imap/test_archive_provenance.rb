# frozen_string_literal: true

require "net/imap"
require "test/unit"

class ArchiveProvenanceTest < Net::IMAP::TestCase
  AP = Net::IMAP::ArchiveProvenance
  Ref = AP::Reference

  def ref(mailbox, validity, uid)
    AP.ref(mailbox, validity, uid)
  end

  # The statement's worked example: source mailbox UIDs 10, 20, 30; a missing
  # EXPUNGE before either retrieval, which can only delete seqno 1 or 2; the
  # first COPY takes the first two messages, the second takes only the first.
  def build_ten_twenty_thirty
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: [10, 20, 30])
    ledger.expunge(mailbox: "src", uidvalidity: 5, step: 1,
                   candidate_runs: [AP::Run.new(first: 1, last: 2)])
    ledger.copy(source_mailbox: "src", source_uidvalidity: 5,
                target_mailbox: "dst-a", target_uidvalidity: 9,
                step: 2, source_seqnos: [1, 2], assigned_uids: [100, 101])
    ledger.copy(source_mailbox: "src", source_uidvalidity: 5,
                target_mailbox: "dst-b", target_uidvalidity: 9,
                step: 3, source_seqnos: [1], assigned_uids: [200])
    ledger
  end

  def test_copyuid_pairing_is_exact
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: [10, 20, 30])
    ledger.copy(source_mailbox: "src", source_uidvalidity: 5,
                target_mailbox: "dst", target_uidvalidity: 9,
                step: 1, source_uids: [30, 10], assigned_uids: [100, 101])
    verdict, witnesses =
      ledger.related?(ref("dst", 9, 101), ref("src", 5, 10))
    assert_equal :always_related, verdict
    assert_equal ref("src", 5, 10),
                 witnesses[:same].first[:origins][ref("dst", 9, 101)]
    verdict, = ledger.related?(ref("dst", 9, 100), ref("src", 5, 10))
    assert_equal :never_related, verdict
  end

  def test_equal_bodies_are_not_related_without_shared_origin
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: [10, 20])
    ledger.copy(source_mailbox: "src", source_uidvalidity: 5,
                target_mailbox: "dst", target_uidvalidity: 9,
                step: 1, source_uids: [10], assigned_uids: [100])
    verdict, = ledger.related?(ref("src", 5, 20), ref("dst", 9, 100))
    assert_equal :never_related, verdict
  end

  def test_each_first_copy_alone_is_ambiguous
    ledger = build_ten_twenty_thirty
    # dst-a/100 alone can trace back to UID 10 or UID 20.
    verdict, witnesses =
      ledger.related?(ref("dst-a", 9, 100), ref("src", 5, 10))
    assert_equal :unknown, verdict
    assert witnesses[:same].any?
    assert witnesses[:different].any?
  end

  def test_the_two_first_copies_are_always_the_same_origin
    ledger = build_ten_twenty_thirty
    verdict, witnesses =
      ledger.related?(ref("dst-a", 9, 100), ref("dst-b", 9, 200))
    assert_equal :always_related, verdict
    assert witnesses[:same].any?
    assert_empty witnesses[:different]
    # But without a measurement the actual original UID is still undecided.
    origins = witnesses[:same].map { _1[:origins][ref("dst-a", 9, 100)].uid }
    assert_include origins, 10
    assert_include origins, 20
  end

  def test_fetch_measurement_pins_the_origin_to_uid_20
    ledger = build_ten_twenty_thirty
    ledger.fetch(mailbox: "src", uidvalidity: 5, seqno: 1, uid: 20, step: 4)
    verdict, witnesses =
      ledger.related?(ref("dst-a", 9, 100), ref("dst-b", 9, 200))
    assert_equal :always_related, verdict
    witnesses[:same].each do |witness|
      assert_equal ref("src", 5, 20),
                   witness[:origins][ref("dst-a", 9, 100)]
      assert_equal ref("src", 5, 20),
                   witness[:origins][ref("dst-b", 9, 200)]
    end
    # The second message copied into dst-a must then be UID 30.
    verdict, = ledger.related?(ref("dst-a", 9, 101), ref("src", 5, 30))
    assert_equal :always_related, verdict
  end

  def test_witnesses_are_replayable_against_the_records
    ledger = build_ten_twenty_thirty
    _, witnesses =
      ledger.related?(ref("dst-a", 9, 100), ref("dst-b", 9, 200))
    witness = witnesses[:same].first
    assignment = witness[:expunge_positions]
    assert_equal 1, assignment.size
    assert_include [1, 2], assignment.values.first
    # Re-follow every chain with the witness's concrete expunge positions.
    position = assignment.values.first
    chain = ledger.chain_for(ref("dst-a", 9, 100), {0 => position})
    assert_equal ref("dst-a", 9, 100), chain.first
    assert_include [10, 20], chain.last.uid
  end

  def test_mailbox_recreation_starts_a_new_generation
    old_box = AP::Ledger.new
    old_box.snapshot(mailbox: "box", uidvalidity: 1, uids: [10, 20])
    old_box.copy(source_mailbox: "box", source_uidvalidity: 1,
                 target_mailbox: "vault", target_uidvalidity: 3,
                 step: 1, source_uids: [20], assigned_uids: [7])
    old_box.snapshot(mailbox: "box", uidvalidity: 2, uids: [10, 20])
    # Same name, new UIDVALIDITY: UID 20 of gen 2 is a different message and
    # must not be conflated with the copy traced to gen 1.
    verdict, = old_box.related?(ref("box", 2, 20), ref("vault", 3, 7))
    assert_equal :never_related, verdict
    verdict, = old_box.related?(ref("box", 1, 20), ref("vault", 3, 7))
    assert_equal :always_related, verdict
  end

  def test_recopied_archive_follows_the_chain_to_the_original
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: [10, 20, 30])
    ledger.copy(source_mailbox: "src", source_uidvalidity: 5,
                target_mailbox: "dst-a", target_uidvalidity: 9,
                step: 1, source_uids: [20], assigned_uids: [100])
    ledger.copy(source_mailbox: "dst-a", source_uidvalidity: 9,
                target_mailbox: "dst-b", target_uidvalidity: 11,
                step: 2, source_uids: [100], assigned_uids: [300])
    verdict, witnesses =
      ledger.related?(ref("dst-b", 11, 300), ref("src", 5, 20))
    assert_equal :always_related, verdict
    chain = witnesses[:same].first[:chains][ref("dst-b", 11, 300)]
    assert_equal [ref("dst-b", 11, 300), ref("dst-a", 9, 100),
                  ref("src", 5, 20)], chain
  end

  def test_copies_only_become_visible_at_visible_step
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: [10, 20])
    ledger.copy(source_mailbox: "src", source_uidvalidity: 5,
                target_mailbox: "dst", target_uidvalidity: 9,
                step: 1, visible_step: 3,
                source_seqnos: [1], assigned_uids: [100])
    # A second retrieval at step 2 still sees only the original mailbox state;
    # the target copy entering at step 3 does not shift sequence numbers.
    ledger.copy(source_mailbox: "src", source_uidvalidity: 5,
                target_mailbox: "dst2", target_uidvalidity: 9,
                step: 2, source_seqnos: [1, 2], assigned_uids: [200, 201])
    verdict, = ledger.related?(ref("dst2", 9, 200), ref("dst", 9, 100))
    assert_equal :always_related, verdict
  end

  def test_million_uid_range_keeps_ranges_compact
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "big", uidvalidity: 5, uids: (1..1_000_000))
    gen = ledger.generation!("big", 5)
    assert_equal 1, gen.segments.size
    ledger.expunge(mailbox: "big", uidvalidity: 5, step: 1,
                   candidate_runs: [AP::Run.new(first: 100, last: 900_000)])
    ledger.copy(source_mailbox: "big", source_uidvalidity: 5,
                target_mailbox: "dst", target_uidvalidity: 9,
                step: 2, source_seqnos: [500_000], assigned_uids: [10])
    # The copy at seqno 500000 traces to UID 500000 (expunge later in range)
    # or UID 500001 (expunge at or before seqno 500000): both possible, and
    # computed without expanding the range.
    verdict, witnesses =
      ledger.related?(ref("dst", 9, 10), ref("big", 5, 500_000))
    assert_equal :unknown, verdict
    uids = witnesses[:same].map { _1[:origins][ref("dst", 9, 10)].uid }
    assert_include uids, 500_000
    assert_not_include uids, 500_001
    verdict, witnesses =
      ledger.related?(ref("dst", 9, 10), ref("big", 5, 500_001))
    assert_equal :unknown, verdict
  end

  def test_fetch_narrows_a_long_candidate_range_symbolically
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "big", uidvalidity: 5, uids: (1..1_000_000))
    ledger.expunge(mailbox: "big", uidvalidity: 5, step: 1,
                   candidate_runs: [AP::Run.new(first: 1, last: 1_000_000)])
    # Measuring UID 42 at seqno 42 forces the deletion to be after seqno 42.
    ledger.fetch(mailbox: "big", uidvalidity: 5, seqno: 42, uid: 42, step: 2)
    domain = ledger.domains.fetch(0)
    assert_equal AP::Run.new(first: 43, last: 1_000_000), domain.runs.first
    assert_empty ledger.inconsistent_observations
  end

  def test_unplayable_fetch_is_reported_separately
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: [10, 20, 30])
    ledger.expunge(mailbox: "src", uidvalidity: 5, step: 1,
                   candidate_runs: [AP::Run.new(first: 1, last: 2)])
    # After deleting seqno 1 or 2, seqno 3 can never still hold UID 10.
    ledger.fetch(mailbox: "src", uidvalidity: 5, seqno: 3, uid: 10, step: 2)
    refute_empty ledger.inconsistent_observations
    observation = ledger.inconsistent_observations.first
    assert_equal 3, observation[:seqno]
    assert_match(/no candidate position/, observation[:reason])
  end

  def test_multiple_candidate_runs_and_never_related_verdict
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: [10, 20, 30, 40])
    ledger.expunge(mailbox: "src", uidvalidity: 5, step: 1,
                   candidate_runs: [AP::Run.new(first: 1, last: 1),
                                    AP::Run.new(first: 3, last: 3)])
    ledger.copy(source_mailbox: "src", source_uidvalidity: 5,
                target_mailbox: "dst", target_uidvalidity: 9,
                step: 2, source_seqnos: [2], assigned_uids: [100])
    # Deleting seqno 1 leaves UID 30 at seqno 2; deleting seqno 3 leaves
    # UID 20 at seqno 2, so the copy can be either UID 20 or UID 30.
    verdict, = ledger.related?(ref("dst", 9, 100), ref("src", 5, 20))
    assert_equal :unknown, verdict
    verdict, witnesses =
      ledger.related?(ref("dst", 9, 100), ref("src", 5, 30))
    assert_equal :unknown, verdict
    # UID 40 is never the source: after either deletion it sits at seqno 3.
    verdict, witnesses =
      ledger.related?(ref("dst", 9, 100), ref("src", 5, 40))
    assert_equal :never_related, verdict
    assert_empty witnesses[:same]
  end

  def test_two_expunges_keep_lower_and_upper_bounds_together
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: [10, 20, 30, 40])
    ledger.expunge(mailbox: "src", uidvalidity: 5, step: 1,
                   candidate_runs: [AP::Run.new(first: 1, last: 1)])
    ledger.expunge(mailbox: "src", uidvalidity: 5, step: 2,
                   candidate_runs: [AP::Run.new(first: 1, last: 3)])
    # After two deletions, seqno 1 holds UID 30 only when both deletions are
    # at or before the traced position; every other replay yields another UID.
    ledger.copy(source_mailbox: "src", source_uidvalidity: 5,
                target_mailbox: "dst", target_uidvalidity: 9,
                step: 3, source_seqnos: [1], assigned_uids: [100])
    verdict, witnesses =
      ledger.related?(ref("dst", 9, 100), ref("src", 5, 30))
    assert_equal :unknown, verdict
    same_positions = witnesses[:same].map { _1[:expunge_positions].values }
    assert_include same_positions, [1, 1]
    # UID 40 can never reach seqno 1: even deleting seqno 1 twice leaves it at
    # seqno 2.
    verdict, = ledger.related?(ref("dst", 9, 100), ref("src", 5, 40))
    assert_equal :never_related, verdict
  end

  def test_fetch_between_two_expunges_narrows_with_both_bounds
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: (1..100).to_a)
    ledger.expunge(mailbox: "src", uidvalidity: 5, step: 1,
                   candidate_runs: [AP::Run.new(first: 1, last: 99)])
    ledger.expunge(mailbox: "src", uidvalidity: 5, step: 3,
                   candidate_runs: [AP::Run.new(first: 1, last: 99)])
    # UID 51 at seqno 50 splits the first domain into the two regions
    # consistent with the measurement and leaves the second unconstrained,
    # all without enumerating the candidate positions.
    ledger.fetch(mailbox: "src", uidvalidity: 5, seqno: 50, uid: 51, step: 4)
    assert_empty ledger.inconsistent_observations
    assert_equal ["1:50", "52:99"], ledger.domains.fetch(0).runs.map(&:to_s)
    assert_equal ["1:99"], ledger.domains.fetch(1).runs.map(&:to_s)
  end

  def test_newly_delivered_uids_append_after_existing_messages
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: [10, 20])
    ledger.deliver(mailbox: "src", uidvalidity: 5, uids: [30, 40], step: 1)
    ledger.copy(source_mailbox: "src", source_uidvalidity: 5,
                target_mailbox: "dst", target_uidvalidity: 9,
                step: 2, source_uids: [40], assigned_uids: [100])
    verdict, = ledger.related?(ref("dst", 9, 100), ref("src", 5, 40))
    assert_equal :always_related, verdict
  end

  def test_unknown_generation_raises
    ledger = AP::Ledger.new
    assert_raise(AP::InconsistentStatement) do
      ledger.related?(ref("nope", 1, 1), ref("nope", 1, 2))
    end
  end

  def test_unknown_uid_in_known_generation_raises
    ledger = AP::Ledger.new
    ledger.snapshot(mailbox: "src", uidvalidity: 5, uids: [10, 20])
    assert_raise(AP::InconsistentStatement) do
      ledger.related?(ref("src", 5, 99), ref("src", 5, 10))
    end
  end

  def test_run_and_reference_validation
    assert_raise(ArgumentError) { AP::Run.new(first: 3, last: 2) }
    reference = ref("INBOX", 7, 11)
    assert_equal "INBOX/7/11", reference.to_s
    assert_equal reference, AP.ref("INBOX", 7, 11)
  end
end
