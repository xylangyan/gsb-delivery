# frozen_string_literal: true

require 'combine_pdf'
require_relative 'raw_reader'

module VolumeCombine
  # Reopens the delivered PDF with CombinePDF and independently verifies that
  # every internal link, bookmark and external work order name lands on the
  # concrete output page the planner predicted. Each verified row keeps the
  # original reference and the basis actually used (same-group occurrence or a
  # manifest permit), so the delivery can be audited end to end.
  #
  # CombinePDF's own parser renames name tree entries on load, so the name tree
  # is verified separately with the read-only RawReader against the raw bytes,
  # preserving the exact on-disk spellings. The file is still fully reopened by
  # CombinePDF to prove it is a valid, reloadable delivery.
  class Auditor
    Row = Struct.new(:kind, :origin, :original, :output_slot, :output_marker, :view, :basis, :detail, keyword_init: true)
    Discrepancy = Struct.new(:where, :expected, :actual)

    def initialize(manifest, raw_models, planner, output_path, name_tree_rows: [])
      @manifest = manifest
      @raw_models = raw_models
      @planner = planner
      @output_path = output_path
      @name_tree_rows = name_tree_rows
      @discrepancies = []
      @rows = []
    end

    def audit
      @pdf = CombinePDF.load(@output_path)
      @pages = @pdf.pages
      @page_index = build_page_index
      @markers = @pages.map { |page| extract_marker(page) }

      @raw_output = RawReader.read(@output_path)

      verify_links
      verify_bookmarks
      verify_name_tree
      verify_gaps
      self
    end

    def ok?
      @discrepancies.empty?
    end

    attr_reader :rows, :discrepancies, :markers

    def gaps
      @planner.gaps
    end

    def name_conflicts
      @planner.name_conflicts
    end

    private

    def build_page_index
      index = {}.compare_by_identity
      @pages.each_with_index { |page, i| index[page] = i + 1 }
      index
    end

    def extract_marker(page)
      stream_text = collect_streams(page[:Contents])
      stream_text =~ /SRC=([^\s\)]+)/
      Regexp.last_match(1)
    end

    def collect_streams(value)
      case value
      when Hash
        if value.key?(:raw_stream_content)
          value[:raw_stream_content].to_s
        else
          value.values.map { |sub| collect_streams(sub) }.join
        end
      when Array
        value.map { |item| collect_streams(item) }.join
      else
        ''
      end
    end

    def destination_page(dest)
      return nil unless dest.is_a?(Array)
      page_ref = dest[0]
      page = page_ref.is_a?(Hash) ? page_ref[:referenced_object] : page_ref
      @page_index[page]
    end

    def destination_view(dest)
      dest.is_a?(Array) ? dest[1..] : nil
    end

    def annot_destination(annot)
      if annot[:A]
        action = annot[:A][:referenced_object] || annot[:A]
        action[:D]
      else
        annot[:Dest]
      end
    end

    def verify_links
      @manifest.slots.each do |slot|
        page = @pages[slot.slot - 1]
        annots = page[:Annots]
        annots = annots[:referenced_object] if annots.is_a?(Hash) && annots.key?(:referenced_object)
        model = @raw_models.fetch(slot.source)
        model.links.each do |link|
          next unless link.page_index == slot.page_index
          decision = @planner.link_decisions[[slot.slot, link.order]]
          annot = Array(annots)[link.order]
          annot = annot[:referenced_object] if annot.is_a?(Hash) && annot.key?(:referenced_object)
          check_link(slot, link, decision, annot)
        end
      end
    end

    def check_link(slot, link, decision, annot)
      if decision.nil?
        # Planner reported a gap: the navigation must have been neutralized.
        if annot.key?(:A) || annot.key?(:Dest)
          @discrepancies << Discrepancy.new("slot #{slot.slot} link ##{link.order + 1}", 'gap (no navigation)', 'still navigable')
        end
        return
      end

      if decision.ref_type == :uri
        action = annot[:A][:referenced_object] || annot[:A]
        actual_uri = action[:URI]
        if actual_uri != link.target
          @discrepancies << Discrepancy.new("slot #{slot.slot} link ##{link.order + 1} URI", link.target, actual_uri)
        end
        @rows << Row.new(kind: 'URI', origin: decision.origin_label, original: link.target,
                         output_slot: slot.slot, output_marker: @markers[slot.slot - 1],
                         view: nil, basis: 'external', detail: decision.detail)
        return
      end

      dest = annot_destination(annot)
      actual_slot = destination_page(dest)
      expected_view = decision.view
      if actual_slot != decision.output_slot
        @discrepancies << Discrepancy.new(decision.origin_label, "slot #{decision.output_slot}", "slot #{actual_slot}")
      end
      if destination_view(dest) != expected_view
        @discrepancies << Discrepancy.new("#{decision.origin_label} view", expected_view.inspect, destination_view(dest).inspect)
      end
      @rows << Row.new(kind: 'link', origin: decision.origin_label,
                       original: "#{decision.original_kind}:#{stringify(decision.original_target)}",
                       output_slot: actual_slot, output_marker: @markers[actual_slot - 1],
                       view: destination_view(dest), basis: decision.basis.to_s, detail: decision.detail)
    end

    def verify_bookmarks
      expected = @planner.bookmark_walk
      return if expected.empty?

      actual = outline_preorder(@pdf.outlines[:First]).select { |node| node[:Dest] }
      if actual.length != expected.length
        @discrepancies << Discrepancy.new('outline node count', expected.length, actual.length)
      end
      actual.zip(expected).each do |node, decision|
        break unless decision
        dest = node[:Dest]
        actual_slot = destination_page(dest)
        if actual_slot != decision.output_slot
          @discrepancies << Discrepancy.new(decision.origin_label, "slot #{decision.output_slot}", "slot #{actual_slot}")
        end
        @rows << Row.new(kind: 'bookmark', origin: decision.origin_label,
                         original: decision.original_target, output_slot: actual_slot,
                         output_marker: @markers[actual_slot - 1], view: destination_view(dest),
                         basis: decision.basis.to_s, detail: decision.detail)
      end
    end

    # Depth first, siblings before descending: the order produced by the
    # combiner's chapter layout.
    def outline_preorder(node_ref)
      out = []
      node = node_ref.is_a?(Hash) ? (node_ref[:referenced_object] || node_ref) : node_ref
      while node
        out << node
        out.concat(outline_preorder(node[:First])) if node[:First]
        node = node[:Next] ? (node[:Next][:referenced_object] || node[:Next]) : nil
      end
      out
    end

    def verify_name_tree
      # Read straight from the output bytes so CombinePDF's load-time renaming
      # cannot hide the real on-disk spellings. Names are ASCII-8BIT on disk;
      # normalize every lookup key to that encoding.
      actual = @raw_output.name_dests.each_with_object({}) do |dest, memo|
        memo[dest.name.dup.force_encoding(Encoding::ASCII_8BIT)] = { slot: dest.page_index + 1, view: dest.view }
      end

      @name_tree_rows.each do |row|
        key = row[:name].dup.force_encoding(Encoding::ASCII_8BIT)
        found = actual[key]
        if found.nil?
          @discrepancies << Discrepancy.new("name tree entry #{stringify(row[:name])}", 'present', 'missing on disk')
          next
        end
        if found[:slot] != row[:slot]
          @discrepancies << Discrepancy.new("name #{stringify(row[:name])}", "slot #{row[:slot]}", "slot #{found[:slot]}")
        end
        if row[:kind] == :work_order
          @rows << Row.new(kind: 'work_order',
                           origin: "work order #{stringify(row[:name])}",
                           original: stringify(row[:name]),
                           output_slot: found[:slot], output_marker: @markers[found[:slot] - 1],
                           view: found[:view], basis: row[:basis].to_s, detail: row[:detail])
        end
      end

      # Guard: every external work order name must exist verbatim and must not
      # have been hijacked by the other period's same-named entry.
      @planner.work_order_decisions.each_key do |name|
        key = name.dup.force_encoding(Encoding::ASCII_8BIT)
        unless actual.key?(key)
          @discrepancies << Discrepancy.new("work order name #{stringify(name)}", 'reserved verbatim', 'not found')
        end
      end
    end

    def verify_gaps
      gaps.each do |gap|
        @rows << Row.new(kind: 'gap', origin: gap.origin_label,
                         original: "#{gap.source} p#{gap.source_page}", output_slot: nil,
                         output_marker: nil, view: nil, basis: 'none', detail: gap.reason)
      end
    end

    def stringify(value)
      case value
      when String then value.dup.force_encoding('UTF-8')
      else value.inspect
      end
    end
  end
end
