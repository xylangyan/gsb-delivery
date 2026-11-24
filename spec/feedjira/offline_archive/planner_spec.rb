# frozen_string_literal: true

require "spec_helper"

module Feedjira
  module OfflineArchive
    describe Planner do
      let(:alpha) { SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom")) }
      let(:beta) { SourceDocument.new("beta", fixture_path("sources", "beta-1120.atom")) }
      let(:gamma) { SourceDocument.new("gamma", fixture_path("sources", "gamma-1120.atom")) }

      def item(document, id, **extra)
        entry = document.entry_by_id(id)
        {
          source: document.name,
          id: entry.id,
          version: entry.version,
          entry: entry,
          signature: nil,
          resources: {},
          resolved_references: {}
        }.merge(extra)
      end

      def selection(entries, must_group: [])
        issue = Catalog::Issue.new(
          id: "issue", cutoff: Time.utc(2026, 11, 21), page_limit: 5,
          sources: [], entries: [], must_group: must_group, retractions: []
        )
        Selector::Selection.new(issue: issue, entries: entries, retractions: [], unselected: [])
      end

      it "places independent entries on as few pages as the limit allows" do
        items = %w[a1 a2 a4].map { |id| item(alpha, "tag:alpha.example,2026:#{id}") }
        pages, unplaced = described_class.call(selection(items), page_limit: 5)
        expect(pages.length).to eq(1)
        expect(unplaced).to eq([])
      end

      it "splits when the page limit is smaller than the entry count" do
        items = %w[a1 a2 a4].map { |id| item(alpha, "tag:alpha.example,2026:#{id}") }
        pages, = described_class.call(selection(items), page_limit: 2)
        expect(pages.length).to eq(2)
      end

      it "keeps a must_group together" do
        items = %w[a1 a2 a4].map { |id| item(alpha, "tag:alpha.example,2026:#{id}") }
        pages, unplaced = described_class.call(
          selection(items, must_group: [%w[alpha:tag:alpha.example,2026:a1 alpha:tag:alpha.example,2026:a4]]),
          page_limit: 2
        )
        expect(unplaced).to eq([])
        grouped = pages.find { |page| page.entries.any? { |entry| entry[:id].end_with?("a1") } }
        expect(grouped.entries.map { |entry| entry[:id] }).to include("tag:alpha.example,2026:a4")
      end

      it "reports a must_group larger than the page limit" do
        items = %w[a1 a2 a4].map { |id| item(alpha, "tag:alpha.example,2026:#{id}") }
        pages, unplaced = described_class.call(
          selection(items, must_group: [%w[alpha:tag:alpha.example,2026:a1 alpha:tag:alpha.example,2026:a2 alpha:tag:alpha.example,2026:a4]]),
          page_limit: 2
        )
        expect(unplaced.first.kind).to eq(:page_limit)
        expect(unplaced.first.constraints[:group_size]).to eq(3)
        expect(pages).to eq([])
      end

      it "reports a missing must_group member" do
        items = [item(alpha, "tag:alpha.example,2026:a1")]
        _, unplaced = described_class.call(
          selection(items, must_group: [%w[alpha:tag:alpha.example,2026:a1 alpha:tag:alpha.example,2026:zzz]]),
          page_limit: 5
        )
        expect(unplaced.first.kind).to eq(:unknown_reference)
        expect(unplaced.first.constraints[:missing]).to eq(["alpha:tag:alpha.example,2026:zzz"])
      end

      it "reports a group that exists only as missing references" do
        _, unplaced = described_class.call(
          selection([], must_group: [%w[alpha:missing]]), page_limit: 5
        )
        expect(unplaced.first.kind).to eq(:unknown_reference)
        expect(unplaced.first.entries).to eq([])
      end

      it "co-locates entries that rebind a shared prefix inside the entry" do
        items = [
          item(beta, "tag:beta.example,2026:b3"),
          item(gamma, "tag:gamma.example,2026:g2")
        ]
        pages, unplaced = described_class.call(
          selection(items, must_group: [%w[beta:tag:beta.example,2026:b3 gamma:tag:gamma.example,2026:g2]]),
          page_limit: 5
        )
        expect(unplaced).to eq([])
        expect(pages.length).to eq(1)
        expect(pages.first.namespaces["media"]).to eq("http://search.yahoo.com/mrss/")
      end

      it "reports a shared prefix bound to different namespaces when neither entry rebinds it" do
        items = [
          item(beta, "tag:beta.example,2026:b1"),
          item(gamma, "tag:gamma.example,2026:g1")
        ]
        _, unplaced = described_class.call(
          selection(items, must_group: [%w[beta:tag:beta.example,2026:b1 gamma:tag:gamma.example,2026:g1]]),
          page_limit: 5
        )
        expect(unplaced.first.kind).to eq(:namespace)
        expect(unplaced.first.constraints[:prefix]).to eq("media")
        expect(unplaced.first.constraints[:hrefs])
          .to contain_exactly("http://search.yahoo.com/mrss/", "http://example.org/gamma-media")
      end

      it "reports a group whose relative references need different page bases" do
        items = [
          item(beta, "tag:beta.example,2026:b1"),
          item(gamma, "tag:gamma.example,2026:g1")
        ]
        # Remove the namespace clash to isolate the base rule.
        items.each { |entry| entry[:entry] = entry[:entry].dup.tap { |copy| copy.namespaces = [] } }
        _, unplaced = described_class.call(
          selection(items, must_group: [%w[beta:tag:beta.example,2026:b1 gamma:tag:gamma.example,2026:g1]]),
          page_limit: 5
        )
        expect(unplaced.first.kind).to eq(:base)
      end

      it "does not split on base when entries carry their own absolute xml:base" do
        items = [
          item(alpha, "tag:alpha.example,2026:a1"),
          item(gamma, "tag:gamma.example,2026:g2")
        ]
        items[1][:entry] = items[1][:entry].dup.tap { |copy| copy.namespaces = [] }
        pages, unplaced = described_class.call(selection(items), page_limit: 5)
        expect(unplaced).to eq([])
        expect(pages.length).to eq(1)
      end

      it "reports a signature that covers unselected content" do
        signed = item(
          alpha, "tag:alpha.example,2026:a1",
          signature: {
            "verified" => true,
            "scope" => { "line_ranges" => [[1, 4]], "unselected_paths" => ["entry/content/p[2]"] }
          }
        )
        pages, unplaced = described_class.call(selection([signed]), page_limit: 5)
        expect(pages).to eq([])
        expect(unplaced.first.kind).to eq(:signature_unselected)
        expect(unplaced.first.constraints[:covered][:uncovered_lines]).to eq([[1, 4]])
        expect(unplaced.first.constraints[:covered][:unselected_paths]).to eq(["entry/content/p[2]"])
      end

      it "reports a signature that requires the whole page" do
        signed = item(
          alpha, "tag:alpha.example,2026:a1",
          signature: { "verified" => true, "scope" => { "whole_page" => true } }
        )
        _, unplaced = described_class.call(selection([signed]), page_limit: 5)
        expect(unplaced.first.kind).to eq(:signature_whole_page)
      end

      it "reports an unverified signature" do
        signed = item(alpha, "tag:alpha.example,2026:a1", signature: { "verified" => false })
        _, unplaced = described_class.call(selection([signed]), page_limit: 5)
        expect(unplaced.first.kind).to eq(:signature_unverified)
      end

      it "accepts a signature whose scope is inside the entry" do
        signed = item(
          alpha, "tag:alpha.example,2026:a1",
          signature: { "verified" => true, "scope" => { "line_ranges" => [[8, 12]] } }
        )
        pages, unplaced = described_class.call(selection([signed]), page_limit: 5)
        expect(unplaced).to eq([])
        expect(pages.length).to eq(1)
      end

      it "normalises a non positive page limit to one" do
        items = %w[a1 a2].map { |id| item(alpha, "tag:alpha.example,2026:#{id}") }
        pages, = described_class.call(selection(items), page_limit: 0)
        expect(pages.length).to eq(2)
      end

      it "accepts a hash style must_group reference" do
        items = [item(alpha, "tag:alpha.example,2026:a1")]
        pages, unplaced = described_class.call(
          selection(items, must_group: [[{ "source" => "alpha", "id" => "tag:alpha.example,2026:a1" }]]),
          page_limit: 5
        )
        expect(unplaced).to eq([])
        expect(pages.length).to eq(1)
      end

      it "accepts an array style must_group reference" do
        items = [item(alpha, "tag:alpha.example,2026:a1")]
        pages, unplaced = described_class.call(
          selection(items, must_group: [[%w[alpha tag:alpha.example,2026:a1]]]),
          page_limit: 5
        )
        expect(unplaced).to eq([])
        expect(pages.length).to eq(1)
      end

      it "places a group onto a page that already has room" do
        items = %w[a1 a2 a4].map { |id| item(alpha, "tag:alpha.example,2026:#{id}") }
        pages, unplaced = described_class.call(
          selection(
            items,
            must_group: [
              %w[alpha:tag:alpha.example,2026:a1],
              %w[alpha:tag:alpha.example,2026:a2 alpha:tag:alpha.example,2026:a4]
            ]
          ),
          page_limit: 4
        )
        expect(unplaced).to eq([])
        expect(pages.length).to eq(1)
        expect(pages.first.entries.length).to eq(3)
      end

      it "merges independent pages that can share one" do
        items = %w[a1 a2 a4].map { |id| item(alpha, "tag:alpha.example,2026:#{id}") }
        pages, = described_class.call(selection(items), page_limit: 3)
        expect(pages.length).to eq(1)
      end

      it "uses no page base when no entry depends on it" do
        items = [item(alpha, "tag:alpha.example,2026:a3"), item(alpha, "tag:alpha.example,2026:a4")]
        pages, = described_class.call(selection(items), page_limit: 5)
        expect(pages.first.base).to be_nil
      end

      it "does not put a no-base entry on a page that carries a base" do
        without = item(alpha, "tag:alpha.example,2026:a1")
        without[:entry] = without[:entry].dup
        without[:entry].feed_base = nil
        with_base = item(beta, "tag:beta.example,2026:b1")
        pages, unplaced = described_class.call(selection([without, with_base]), page_limit: 5)
        expect(unplaced).to eq([])
        expect(pages.length).to eq(2)
        expect(pages.map(&:base)).to contain_exactly(nil, "https://beta.example/2026/11/20/")
      end
    end
  end
end
