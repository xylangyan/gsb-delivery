# frozen_string_literal: true

require "spec_helper"

module Feedjira
  module OfflineArchive
    describe Renderer do
      let(:alpha) { SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom")) }
      let(:issue) do
        Catalog::Issue.new(
          id: "issue-1", cutoff: Time.utc(2026, 11, 21), page_limit: 5,
          sources: [], entries: [], must_group: [], retractions: []
        )
      end

      def item(document, id, **extra)
        entry = document.entry_by_id(id)
        {
          source: document.name, id: entry.id, version: entry.version,
          entry: entry, signature: nil, resources: {}, resolved_references: {}
        }.merge(extra)
      end

      def page(entries, base: nil, namespaces: {})
        Planner::Page.new(index: 0, entries: entries, base: base, namespaces: namespaces)
      end

      it "writes a plain Atom document that Feedjira can parse" do
        rendered = described_class.call([page([item(alpha, "tag:alpha.example,2026:a1")])], issue: issue)
        feed = Feedjira.parse(rendered.first.xml)
        expect(feed).to be_a(Feedjira::Parser::Atom)
        expect(feed.entries.length).to eq(1)
      end

      it "carries the entry bytes verbatim" do
        entry = alpha.entry_by_id("tag:alpha.example,2026:a1")
        rendered = described_class.call([page([item(alpha, entry.id)])], issue: issue)
        expect(rendered.first.xml).to include(entry.xml)
      end

      it "declares the page base and inherited namespaces" do
        rendered = described_class.call(
          [page([item(alpha, "tag:alpha.example,2026:a1")],
                base: "https://alpha.example/editions/2026-11-20/",
                namespaces: { "media" => "http://search.yahoo.com/mrss/" })],
          issue: issue
        )
        expect(rendered.first.xml).to include('xml:base="https://alpha.example/editions/2026-11-20/"')
        expect(rendered.first.xml).to include('xmlns:media="http://search.yahoo.com/mrss/"')
      end

      it "omits the base when the page has none" do
        rendered = described_class.call([page([item(alpha, "tag:alpha.example,2026:a4")])], issue: issue)
        expect(rendered.first.xml).not_to include("xml:base")
      end

      it "names the file from the issue and page number" do
        rendered = described_class.call([page([item(alpha, "tag:alpha.example,2026:a1")])], issue: issue)
        expect(rendered.first.filename).to eq("issue-issue-1-01.atom")
      end

      it "maps relative references to local files" do
        resources = { "audio/a1.mp3" => "attachments/audio/a1.mp3" }
        item = item(alpha, "tag:alpha.example,2026:a1")
        item[:resolved_references] = {
          "audio/a1.mp3" => "https://alpha.example/editions/2026-11-20/audio/a1.mp3"
        }
        rendered = described_class.call([page([item])], issue: issue, resources: resources)
        expect(rendered.first.attachments["audio/a1.mp3"]).to eq("attachments/audio/a1.mp3")
        expect(rendered.first.attachments["https://alpha.example/editions/2026-11-20/audio/a1.mp3"])
          .to eq("attachments/audio/a1.mp3")
      end

      it "skips references the resource map does not know" do
        item = item(alpha, "tag:alpha.example,2026:a1")
        item[:resolved_references] = { "audio/a1.mp3" => "https://alpha.example/a.mp3" }
        rendered = described_class.call([page([item])], issue: issue, resources: {})
        expect(rendered.first.attachments).to eq({})
      end

      it "accepts an entry supplied attachment target map" do
        item = item(alpha, "tag:alpha.example,2026:a1")
        item[:attachment_targets] = { "audio/a1.mp3" => "local/a1.mp3" }
        item[:resolved_references] = { "audio/a1.mp3" => "https://alpha.example/a.mp3" }
        rendered = described_class.call([page([item])], issue: issue)
        expect(rendered.first.attachments["audio/a1.mp3"]).to eq("local/a1.mp3")
      end

      it "escapes the issue title" do
        special = issue.dup
        special.id = "a & <b>"
        rendered = described_class.call([page([item(alpha, "tag:alpha.example,2026:a1")])], issue: special)
        expect(rendered.first.xml).to include("a &amp; &lt;b&gt; archive")
      end
    end
  end
end
