# frozen_string_literal: true

# Work-order catalog. This is the single place where the cabinet requirements,
# the two full tzdata-style rule sources (A = old/recovered ledger, B = new
# ledger), the reusable generation candidates with their measured byte costs,
# the shared (deduplicated) module blocks and the per-cabinet byte budget are
# declared. The generator, packer and auditor all read from here, so nothing
# about the job is hard coded into the tooling.

module Catalog
  # Root of the factory and of the build output.
  ROOT      = File.expand_path(__dir__)
  BUILD_DIR = File.join(ROOT, 'build')

  # The full, "two complete editions" source material. Each rule source is a
  # short local tzdata-style file covering only the zones the cabinets need.
  # Both are parsed by the same reusable generator (generator.rb), so every
  # emitted module is produced by a work-order-approved rewrite form.
  RULE_SOURCES = {
    'A' => File.join(ROOT, 'rules_a_old.tzdata'),
    'B' => File.join(ROOT, 'rules_b_new.tzdata')
  }.freeze

  # Human label for each rule version (A is the recovered, previously
  # separately-accounted ledger; B is the current ledger after the rename).
  RULE_LABEL = {
    'A' => 'A-old-recovered',
    'B' => 'B-new-current'
  }.freeze

  # A reusable candidate. `form` selects the approved rewrite form used by the
  # generator:
  #   :full_rule   - parse the rule source for the zone and emit every offset
  #                  and transition it defines (a real DataTimezone module).
  #   :window_rule - like :full_rule but retain only transitions inside a local
  #                  window (the surrounding offset is kept so boundary reads
  #                  fold in correctly).
  #   :link        - emit a LinkedTimezone alias pointing at `target`.
  # `bytes` is the measured, on-device size of the .rb module produced for
  # this candidate (re-measured by the packer; kept here for transparent
  # budgeting). `version` states which rule edition this candidate is cut from.
  Candidate = Struct.new(:code, :form, :identifier, :version, :target, :bytes, keyword_init: true)

  # The candidate catalogue. Candidates are reusable across cabinets.
  CANDIDATES = {
    # --- America/New_York family (the renamed US/Eastern points here) ---
    'NY-FULL-B'  => Candidate.new(code: 'NY-FULL-B',  form: :full_rule,   identifier: 'America/New_York', version: 'B', bytes: 846),
    'NY-WIN-B'   => Candidate.new(code: 'NY-WIN-B',   form: :window_rule, identifier: 'America/New_York', version: 'B', bytes: 589),
    'NY-WIN-A'   => Candidate.new(code: 'NY-WIN-A',   form: :window_rule, identifier: 'America/New_York', version: 'A', bytes: 589),
    'USE-LINK-B' => Candidate.new(code: 'USE-LINK-B', form: :link,       identifier: 'US/Eastern',       version: 'B', target: 'America/New_York', bytes: 379),
    'USE-LINK-A' => Candidate.new(code: 'USE-LINK-A', form: :link,       identifier: 'US/Eastern',       version: 'A', target: 'America/New_York', bytes: 379),

    # --- America/Chicago family (US/Central points here) ---
    'CHI-FULL-B' => Candidate.new(code: 'CHI-FULL-B', form: :full_rule,   identifier: 'America/Chicago', version: 'B', bytes: 844),
    'CHI-WIN-B'  => Candidate.new(code: 'CHI-WIN-B',  form: :window_rule, identifier: 'America/Chicago', version: 'B', bytes: 587),
    'CHI-WIN-A'  => Candidate.new(code: 'CHI-WIN-A',  form: :window_rule, identifier: 'America/Chicago', version: 'A', bytes: 587),
    'USC-LINK-B' => Candidate.new(code: 'USC-LINK-B', form: :link,       identifier: 'US/Central',       version: 'B', target: 'America/Chicago', bytes: 378),
    'USC-LINK-A' => Candidate.new(code: 'USC-LINK-A', form: :link,       identifier: 'US/Central',       version: 'A', target: 'America/Chicago', bytes: 378),

    # --- Asia/Kolkata family. In the recovered old ledger (A) Asia/Calcutta is
    # still its own separately-accounted data zone. In the current ledger (B)
    # the rename turns Asia/Calcutta into a link to canonical Asia/Kolkata.
    'KOL-FULL-B' => Candidate.new(code: 'KOL-FULL-B', form: :full_rule,   identifier: 'Asia/Kolkata', version: 'B', bytes: 420),
    'KOL-FULL-A' => Candidate.new(code: 'KOL-FULL-A', form: :full_rule,   identifier: 'Asia/Kolkata', version: 'A', bytes: 420),
    'CAL-FULL-A' => Candidate.new(code: 'CAL-FULL-A', form: :full_rule,   identifier: 'Asia/Calcutta', version: 'A', bytes: 422),
    'CAL-LINK-B' => Candidate.new(code: 'CAL-LINK-B', form: :link,       identifier: 'Asia/Calcutta', version: 'B', target: 'Asia/Kolkata', bytes: 381),

    # --- Asia/Dhaka family. Same recovery->rename pattern as Kolkata. ---
    'DHA-FULL-B' => Candidate.new(code: 'DHA-FULL-B', form: :full_rule,   identifier: 'Asia/Dhaka', version: 'B', bytes: 416),
    'DHA-FULL-A' => Candidate.new(code: 'DHA-FULL-A', form: :full_rule,   identifier: 'Asia/Dhaka', version: 'A', bytes: 416),
    'DAC-FULL-A' => Candidate.new(code: 'DAC-FULL-A', form: :full_rule,   identifier: 'Asia/Dacca', version: 'A', bytes: 416),
    'DAC-LINK-B' => Candidate.new(code: 'DAC-LINK-B', form: :link,       identifier: 'Asia/Dacca', version: 'B', target: 'Asia/Dhaka', bytes: 373),

    # --- Europe/London (constant + DST, used for overlap/gap evidence) ---
    'LON-FULL-B' => Candidate.new(code: 'LON-FULL-B', form: :full_rule,   identifier: 'Europe/London', version: 'B', bytes: 830),
    'LON-WIN-B'  => Candidate.new(code: 'LON-WIN-B',  form: :window_rule, identifier: 'Europe/London', version: 'B', bytes: 573),
    'LON-WIN-A'  => Candidate.new(code: 'LON-WIN-A',  form: :window_rule, identifier: 'Europe/London', version: 'A', bytes: 573),

    # --- Shared constant anchor (one tiny block reused by every cabinet) ---
    'UTC-FULL-A' => Candidate.new(code: 'UTC-FULL-A', form: :full_rule, identifier: 'Etc/UTC', version: 'A', bytes: 406),
    'UTC-FULL-B' => Candidate.new(code: 'UTC-FULL-B', form: :full_rule, identifier: 'Etc/UTC', version: 'B', bytes: 406)
  }.freeze

  # The shared, deduplicated module blocks (see below) plus loader/index
  # overhead mean every package carries a near-fixed floor. The global cap is
  # the total on-device byte ceiling across all shipped cabinet x edition
  # packages.
  GLOBAL_BUDGET = 22_000

  # Shared, deduplicated module blocks. A block resolves to an edition-specific
  # candidate, so the same logical block is rebuilt for A and B but bought once
  # per cabinet edition. After a package is unpacked the module is still visible
  # through the original TZInfo entry point (require
  # 'tzinfo/data/definitions/...'); the packer materialises it as a normal
  # on-disk definition. SHARED-UTC is referenced by every cabinet (real reuse);
  # SHARED-KOLKATA/SHARED-US-EAST show per-cabinet dedup of a larger zone.
  SHARED_BLOCKS = {
    'SHARED-UTC'      => { 'provides' => 'Etc/UTC',          'candidate' => { 'A' => 'UTC-FULL-A', 'B' => 'UTC-FULL-B' } },
    'SHARED-KOLKATA'  => { 'provides' => 'Asia/Kolkata',     'candidate' => { 'A' => 'KOL-FULL-A', 'B' => 'KOL-FULL-B' } },
    'SHARED-US-EAST'  => { 'provides' => 'America/New_York', 'candidate' => { 'A' => 'NY-WIN-A',   'B' => 'NY-WIN-B' } }
  }.freeze

  # A local-time window that audit samples must fall inside. Bounds are given
  # as UTC epoch seconds; the auditor folds the outside in (it does not truncate
  # to the UTC edge).
  Window = Struct.new(:name, :start_epoch, :end_epoch, keyword_init: true)

  # Per-cabinet work order.
  #   zones     : canonical identifiers that must be queryable on the cabinet
  #   countries : ISO codes whose (ordered, filtered) candidate list is needed
  #   samples   : audit samples; each names the rule version that must
  #               recompute it, a local time with NO embedded UTC offset, and
  #               whether it is the old (pre-swap) or new (post-swap) ledger
  #   windows   : local windows that reused candidates must cover in full
  #   budget    : total on-device byte ceiling
  Cabinet = Struct.new(
    :key, :title, :zones, :countries, :samples, :windows, :ledger_window,
    :candidate_allowlist, :prefer, :shared_blocks, :budget, keyword_init: true
  )

  CABINETS = [
    Cabinet.new(
      key: 'C1',
      title: 'US East audit cabinet',
      zones: ['America/New_York', 'US/Eastern', 'America/Chicago', 'US/Central', 'Etc/UTC'],
      countries: ['US'],
      # US has many zones; only the cabinet's already-queried candidates keep
      # their full-edition visible position. Every other US index entry must be
      # dropped rather than left dangling at a module that was not shipped.
      windows: {
        # Old ledger window straddles the Nov fall-back (overlap hour inside).
        'old_us' => Window.new(name: 'old_us', start_epoch: Time.utc(2025, 10, 20).to_i,
                                           end_epoch: Time.utc(2025, 11, 10).to_i),
        # New ledger window straddles the Mar spring-forward (skipped hour inside).
        'new_us' => Window.new(name: 'new_us', start_epoch: Time.utc(2026, 2, 25).to_i,
                                           end_epoch: Time.utc(2026, 3, 15).to_i)
      },
      # Which window clips each edition's window_rule candidates.
      ledger_window: { 'A' => 'old_us', 'B' => 'new_us' },
      samples: [
        # Old sample landing in the repeated 01:00-02:00 local hour (two UTC).
        { id: 'America/New_York', version: 'A', ledger: 'old',
          local: Time.utc(2025, 11, 2, 1, 30), expect: :overlap },
        # New sample after the spring skip (a local time that simply does not
        # exist before the jump is also checked separately by the auditor).
        { id: 'America/New_York', version: 'B', ledger: 'new',
          local: Time.utc(2026, 3, 9, 9, 0), expect: :single },
        { id: 'America/Chicago', version: 'B', ledger: 'new',
          local: Time.utc(2026, 3, 9, 8, 0), expect: :single }
      ],
      candidate_allowlist: %w[NY-WIN-A NY-WIN-B NY-FULL-B USE-LINK-A USE-LINK-B
                              CHI-WIN-A CHI-WIN-B CHI-FULL-B USC-LINK-A USC-LINK-B],
      # The swap plan: old samples recompute under A, new under B. Links keep
      # the edition's candidate ordering and canonical identifier (no aliases
      # collapsed into the new canonical entry).
      prefer: { 'America/New_York' => { old: 'NY-WIN-A', new: 'NY-WIN-B' },
                'US/Eastern'       => { old: 'USE-LINK-A', new: 'USE-LINK-B' },
                'America/Chicago'  => { old: 'CHI-WIN-A', new: 'CHI-WIN-B' },
                'US/Central'       => { old: 'USC-LINK-A', new: 'USC-LINK-B' } },
      shared_blocks: ['SHARED-US-EAST', 'SHARED-UTC'],
      budget: 4200
    ),

    Cabinet.new(
      key: 'C2',
      title: 'South Asia rename cabinet',
      zones: ['Asia/Kolkata', 'Asia/Calcutta', 'Asia/Dhaka', 'Asia/Dacca', 'Etc/UTC'],
      countries: ['IN', 'BD'],
      windows: {
        # Constant-offset zones: a wide window that must be covered in full.
        'asia' => Window.new(name: 'asia', start_epoch: Time.utc(2025, 1, 1).to_i,
                                         end_epoch: Time.utc(2027, 1, 1).to_i)
      },
      ledger_window: { 'A' => 'asia', 'B' => 'asia' },
      samples: [
        # Old name still carried in returned (old) samples; must resolve through
        # the link to the canonical zone, recomputed under the old ledger A.
        { id: 'Asia/Calcutta', version: 'A', ledger: 'old',
          local: Time.utc(2025, 6, 1, 12, 0), expect: :single },
        { id: 'Asia/Dhaka', version: 'B', ledger: 'new',
          local: Time.utc(2026, 6, 1, 12, 0), expect: :single }
      ],
      candidate_allowlist: %w[KOL-FULL-A KOL-FULL-B CAL-FULL-A CAL-LINK-B
                              DHA-FULL-A DHA-FULL-B DAC-FULL-A DAC-LINK-B],
      prefer: { 'Asia/Kolkata' => { old: 'KOL-FULL-A', new: 'KOL-FULL-B' },
                'Asia/Calcutta' => { old: 'CAL-FULL-A', new: 'CAL-LINK-B' },
                'Asia/Dhaka'    => { old: 'DHA-FULL-A', new: 'DHA-FULL-B' },
                'Asia/Dacca'    => { old: 'DAC-FULL-A', new: 'DAC-LINK-B' } },
      shared_blocks: ['SHARED-KOLKATA', 'SHARED-UTC'],
      budget: 4000
    ),

    Cabinet.new(
      key: 'C3',
      title: 'London overlap/gap evidence cabinet',
      zones: ['Europe/London', 'Etc/UTC'],
      countries: ['GB'],
      windows: {
        'old_gb' => Window.new(name: 'old_gb', start_epoch: Time.utc(2025, 10, 20).to_i,
                                           end_epoch: Time.utc(2025, 11, 5).to_i),
        'new_gb' => Window.new(name: 'new_gb', start_epoch: Time.utc(2026, 3, 20).to_i,
                                           end_epoch: Time.utc(2026, 4, 5).to_i)
      },
      ledger_window: { 'A' => 'old_gb', 'B' => 'new_gb' },
      samples: [
        # Old sample inside the repeated 01:00 local hour (BST->GMT fall back).
        { id: 'Europe/London', version: 'A', ledger: 'old',
          local: Time.utc(2025, 10, 26, 1, 30), expect: :overlap },
        # New sample after the spring-forward (GMT->BST skipped 01:00-02:00).
        { id: 'Europe/London', version: 'B', ledger: 'new',
          local: Time.utc(2026, 3, 30, 9, 0), expect: :single }
      ],
      candidate_allowlist: %w[LON-WIN-A LON-WIN-B LON-FULL-B],
      prefer: { 'Europe/London' => { old: 'LON-WIN-A', new: 'LON-WIN-B' } },
      shared_blocks: ['SHARED-UTC'],
      budget: 2600
    )
  ].freeze

  def self.cabinet(key)
    CABINETS.find { |c| c.key == key } or raise "unknown cabinet #{key}"
  end

  # A window_rule module is clipped to the union of every named local window
  # for the cabinet. The union still excludes deep history (so the module stays
  # small) but contains both the fall-back overlap and the spring-forward gap.
  # That lets an already-booked reading be re-verified after the swap and again
  # after switching back, because both edition packages cover the full span.
  def self.coverage_bounds(cabinet)
    starts = cabinet.windows.values.map(&:start_epoch)
    ends = cabinet.windows.values.map(&:end_epoch)
    [starts.min, ends.max]
  end

  def self.candidate(code)
    CANDIDATES[code] or raise "unknown candidate #{code}"
  end
end
