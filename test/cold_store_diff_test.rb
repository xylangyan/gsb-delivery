#!/usr/bin/env ruby
# frozen_string_literal: true

# Cold-store manifest replay comparison for the two fixed Ox.sax_html rule
# sets. The manifest stores ordered fragment references that can repeat for
# many rounds; the engine must answer byte-window queries over the expanded
# page without expanding it and without reading more than ColdStoreDiff::BUDGET
# bytes even for a hundred million rounds.

$LOAD_PATH << File.join(File.dirname(__FILE__), '../lib')
$LOAD_PATH << File.join(File.dirname(__FILE__), '../ext')

require 'test/unit'
require 'ox/cold_store_diff'

class ColdStoreDiffTest < ::Test::Unit::TestCase
  FRONT = '<tr><td id="bin">A&'
  BACK  = 'amp;B<td id="qty">9<!--<hr>--></tr>'
  OPEN  = '<div><table>'
  CLOSE = '</table></div><table><tr><td id="ok">Z</td></tr></table>'

  def manifest(rounds)
    [
      { name: 'open', bytes: OPEN },
      { name: 'row', repeat: rounds,
        fragments: [{ name: 'front', bytes: FRONT }, { name: 'back', bytes: BACK }] },
      { name: 'close', bytes: CLOSE }
    ]
  end

  def assembled(rounds)
    OPEN + (FRONT + BACK) * rounds + CLOSE
  end

  def test_narrative_first_bin_differs_on_status_text_range_and_close
    rounds = 2
    page = assembled(rounds)
    bin_anchor = page.index('<td')
    report = Ox::ColdStoreDiff.diff(manifest(rounds), [bin_anchor, bin_anchor + 1])

    assert_equal(1, report.count)
    first = report.first
    assert_equal(bin_anchor, first.anchor)
    assert_equal('front', first.ref)
    assert_equal(1, first.round)
    assert_equal(4, first.local_offset)

    # Rule set A: entity decoded across the two fragments, row handed off when
    # the next cell starts.
    assert_equal(:delivered, first.a.status)
    assert_equal('bin', first.a.id)
    assert_equal('A&B', first.a.text)
    lo, hi = first.a.byte_range
    assert_equal('A&amp;B', page.byteslice(lo, hi - lo))
    assert_equal(page.index('<td', bin_anchor + 1), first.a.close_pos)

    # Rule set B never emitted the cell.
    assert_equal(:no_callback, first.b.status)
    assert_nil(first.b.id)
    assert_equal('', first.b.text)
    assert_nil(first.b.byte_range)
    assert_nil(first.b.close_pos)
  end

  def test_locate_resolves_fragment_reference_round_and_local_offset
    rounds = 3
    page = assembled(rounds)
    man = Ox::ColdStoreDiff::Manifest.build(manifest(rounds))
    anchor = page.index('<td') # round one, inside front

    slot, ref, round, local = man.locate(anchor)
    assert_equal('front', ref)
    assert_equal(1, round)
    assert_equal(4, local)
    assert_equal(1, slot)

    stride = FRONT.bytesize + BACK.bytesize
    round_three_anchor = OPEN.bytesize + 2 * stride + 4
    slot, ref, round, local = man.locate(round_three_anchor)
    assert_equal('front', ref)
    assert_equal(3, round)
    assert_equal(4, local)
    assert_equal(5, slot) # open(1) + two rounds * 2 refs, zero-based slot 5
  end

  def test_full_window_matches_direct_whole_page_replay
    rounds = 4
    page = assembled(rounds)
    report = Ox::ColdStoreDiff.diff(manifest(rounds), [0, page.bytesize])

    # Two cells per round differ (bin and qty are suppressed on side B); the
    # cell after </div> is delivered by both.
    assert_equal(rounds * 2, report.count)
    assert_equal(rounds * 2 + 1, report.anchors_total)
    assert_equal(2, report.templates.size)
    assert_equal([1, 1], report.templates.map(&:round_lo))
    assert_equal([rounds, rounds], report.templates.map(&:round_hi))

    # The cell outside the div must not appear among differences: with only a
    # comment-borne <hr>, rule set B recovers after </div> and delivers it too.
    ok_anchor = page.rindex('<td')
    assert(report.templates.none? { |d| d.anchor == ok_anchor })
  end

  def test_middle_round_narrow_window_uses_translated_coordinates
    rounds = 10
    man = Ox::ColdStoreDiff::Manifest.build(manifest(rounds))
    stride = FRONT.bytesize + BACK.bytesize
    anchor = man.segment_base(1) + 4 * stride + 4 # round five bin
    report = Ox::ColdStoreDiff.diff(manifest(rounds), [anchor, anchor + 1])

    assert_equal(1, report.count)
    first = report.first
    assert_equal(5, first.round)
    assert_equal(anchor, first.anchor)
    assert_equal(anchor + 20, first.a.close_pos)
    assert_equal([anchor + 13, anchor + 20], first.a.byte_range)
    assert_equal(:no_callback, first.b.status)
  end

  def test_hundred_million_rounds_wide_window_stays_inside_budget
    rounds = 100_000_000
    stride = FRONT.bytesize + BACK.bytesize
    group_lo = OPEN.bytesize
    group_hi = group_lo + stride * rounds

    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    report = Ox::ColdStoreDiff.diff(manifest(rounds), [group_lo, group_hi])
    elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started

    assert_equal(2 * rounds, report.count)
    assert(report.bytes_read <= Ox::ColdStoreDiff::BUDGET)
    assert(report.bytes_processed <= Ox::ColdStoreDiff::BUDGET)
    assert(report.within_budget)
    assert(elapsed < 5)
  end

  def test_hundred_million_rounds_tail_window_without_abort_is_delivered_by_both
    rounds = 100_000_000
    stride = FRONT.bytesize + BACK.bytesize
    page_length = OPEN.bytesize + stride * rounds + CLOSE.bytesize
    ok_anchor = page_length - CLOSE.bytesize + CLOSE.index('<td')

    report = Ox::ColdStoreDiff.diff(manifest(rounds), [ok_anchor, ok_anchor + 1])
    # The comment <hr> never aborts B, so after </div> both sides deliver.
    assert_equal(0, report.count)
    assert(report.bytes_read <= Ox::ColdStoreDiff::BUDGET)
  end

  def test_hundred_million_rounds_tail_window_after_abort_is_a_only
    rounds = 100_000_000
    abort_manifest = [
      { name: 'open', bytes: OPEN },
      { name: 'row', repeat: rounds,
        fragments: [{ name: 'front', bytes: FRONT },
                    { name: 'back', bytes: 'amp;B<td id="qty">9' },
                    { name: 'h', bytes: '<h' },
                    { name: 'r', bytes: 'r></tr>' }] },
      { name: 'close', bytes: CLOSE }
    ]
    man = Ox::ColdStoreDiff::Manifest.build(abort_manifest)
    ok_anchor = man.length - CLOSE.bytesize + CLOSE.index('<td')

    report = Ox::ColdStoreDiff.diff(abort_manifest, [ok_anchor, ok_anchor + 1])
    assert_equal(1, report.count)
    assert(report.bytes_read <= Ox::ColdStoreDiff::BUDGET)
    assert_equal(:delivered, report.first.a.status)
    assert_equal(:no_callback, report.first.b.status)
  end

  def test_real_split_hr_aborts_even_inside_blocked_div
    split_manifest = [
      { name: 'head', bytes: '<div><table><tr><td id="bin">A&amp;B<td id="qty">9' },
      { name: 'h', bytes: '<h' },
      { name: 'r', bytes: 'r></tr></table></div><table><tr><td id="ok">Z</td></tr></table>' }
    ]
    page = split_manifest.map { |entry| entry[:bytes] }.join
    report = Ox::ColdStoreDiff.diff(split_manifest, [page.index('<td'), page.index('<td') + 1])

    assert_equal(1, report.count)
    assert_equal(page.index('<hr'), report.first.b.abort_pos)
    assert_equal(:no_callback, report.first.b.status)
    assert_equal(:delivered, report.first.a.status)
  end

  def test_comment_hr_does_not_abort_but_real_hr_does
    page = OPEN + FRONT + BACK + CLOSE
    man = Ox::ColdStoreDiff::Manifest.build(manifest(1))
    plan = Ox::ColdStoreDiff::PlaybackPlan.new(man)
    handler = replay_b(man, plan)
    assert(!handler.aborted)

    split_manifest = [
      { name: 'head', bytes: '<div><table><tr><td id="bin">9' },
      { name: 'h', bytes: '<h' },
      { name: 'r', bytes: 'r></tr></table></div>' }
    ]
    split_page = split_manifest.map { |entry| entry[:bytes] }.join
    split_man = Ox::ColdStoreDiff::Manifest.build(split_manifest)
    split_handler = replay_b(split_man, Ox::ColdStoreDiff::PlaybackPlan.new(split_man))
    assert(split_handler.aborted)
    assert_equal(split_page.index('<hr'), split_handler.abort_pos)
  end

  def test_open_cell_is_distinct_from_delivered_and_no_callback
    # The page ends inside the cell: start callback happened, no end callback.
    entries = [{ name: 'tail', bytes: '<div><table><tr><td id="bin">still open' }]
    man = Ox::ColdStoreDiff::Manifest.build(entries)
    plan = Ox::ColdStoreDiff::PlaybackPlan.new(man)

    handler_a = replay(man, plan, { 'div' => :off })
    cell_a = handler_a.cells.first
    assert_equal(:open, cell_a.status)
    assert_equal('bin', cell_a.id)
    assert_nil(cell_a.close_pos)
    assert_equal('still open', cell_a.text)

    handler_b = replay(man, plan, { 'div' => :block, 'hr' => :abort })
    assert(handler_b.cells.empty?)
  end

  def test_split_tag_name_and_attribute_across_fragments
    parts = ['<div><table><tr><', 'td id="bi', 'n">A&amp;', 'B</td></tr></table></div>']
    page = parts.join
    entries = parts.each_with_index.map { |bytes, idx| { name: "p#{idx}", bytes: bytes } }
    man = Ox::ColdStoreDiff::Manifest.build(entries)

    templates = man.anchor_templates(0, man.length)
    assert_equal([page.index('<td')], templates.map(&:first_anchor))

    report = Ox::ColdStoreDiff.diff(entries, [0, page.bytesize])
    first = report.first
    assert_equal('bin', first.a.id)
    assert_equal('A&B', first.a.text)
    lo, hi = first.a.byte_range
    assert_equal('A&amp;B', page.byteslice(lo, hi - lo))
  end

  def test_entity_split_preserves_original_byte_range
    parts = ['<table><tr><td id="x">A&', 'amp;B</td></tr></table>']
    page = parts.join
    entries = parts.each_with_index.map { |bytes, idx| { name: "q#{idx}", bytes: bytes } }
    man = Ox::ColdStoreDiff::Manifest.build(entries)
    anchor = man.anchor_templates(0, man.length).first.first_anchor
    plan = Ox::ColdStoreDiff::PlaybackPlan.new(man)
    handler = replay(man, plan, { 'div' => :off })

    cell = handler.cell_by_anchor[anchor]
    lo, hi = cell.byte_range
    assert_equal('A&B', cell.text)
    assert_equal('A&amp;B', page.byteslice(lo, hi - lo))
  end

  def test_comment_and_attribute_lookalikes_are_not_anchors
    # A real cell, then the same bytes inside a comment and inside an
    # attribute value; only the real tag is an anchor.
    entries = [{ name: 'p',
                 bytes: '<table><tr><td id="real">x</td>' \
                        '<!--<td id="fake">-->' \
                        '<span data-x="<td id=q>"></span></tr></table>' }]
    man = Ox::ColdStoreDiff::Manifest.build(entries)
    anchors = man.anchor_templates(0, man.length)
    assert_equal(1, anchors.size)
    assert_equal(entries.first[:bytes].index('<td'), anchors.first.first_anchor)
  end

  def test_text_identical_but_range_or_close_difference_is_flagged
    a = Ox::ColdStoreDiff::Cell.new(0)
    a.status = :delivered
    a.add_text('same', 10, 14)
    a.close_pos = 20

    b = Ox::ColdStoreDiff::Cell.new(0)
    b.status = :delivered
    b.add_text('same', 10, 15)
    b.close_pos = 21

    reasons = Ox::ColdStoreDiff.send(:compare_cells, a, b)
    assert_equal(['byte range differs', 'close position differs'], reasons)
  end

  def test_only_manifest_end_counts_as_page_tail
    man = Ox::ColdStoreDiff::Manifest.build(manifest(2))
    assert_raise(RangeError) { man.locate(man.length + 1) }
    assert_nothing_raised { man.locate(man.length) }
    assert_raise(ArgumentError) { Ox::ColdStoreDiff.diff(manifest(2), [0, man.length + 1]) }
  end

  private

  def replay(manifest, plan, overlay_changes)
    handler = Ox::ColdStoreDiff::BinHandler.new
    raw_window = Ox::ColdStoreDiff::RawWindow.new
    position_map = Ox::ColdStoreDiff::PositionMap.new
    handler.raw_window = raw_window
    handler.position_map = position_map
    io = Ox::ColdStoreDiff::PlaybackIO.new(plan.refs.dup, position_map, raw_window)
    overlay = Ox.sax_html_overlay.merge(overlay_changes)
    Ox.sax_html(handler, io, convert_special: true, overlay: overlay)
    handler.finish
    handler
  end

  def replay_b(manifest, plan)
    replay(manifest, plan, { 'div' => :block, 'hr' => :abort })
  end
end
