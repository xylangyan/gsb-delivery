# frozen_string_literal: true

require "spec_helper"

module Feedjira
  module OfflineArchive
    describe Verifier do
      let(:alpha) { SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom")) }
      let(:issue) do
        Catalog::Issue.new(
          id: "issue", cutoff: Time.utc(2026, 11, 21), page_limit: 5,
          sources: [], entries: [], must_group: [], retractions: []
        )
      end

      def item(id)
        entry = alpha.entry_by_id(id)
        {
          source: "alpha", id: entry.id, version: entry.version, entry: entry,
          signature: nil, resources: {}, resolved_references: {},
          attachment_targets: {}
        }
      end

      def render(entries)
        namespaces = {}
        entries.each do |item|
          item[:entry].inherited_bindings.each { |binding| namespaces[binding.prefix] ||= binding.href }
        end
        page = Planner::Page.new(index: 0, entries: entries, base: nil, namespaces: namespaces)
        Renderer.call([page], issue: issue)
      end

      it "passes for a faithful page" do
        rendered = render([item("tag:alpha.example,2026:a1")])
        expect(described_class.call(rendered, selected: rendered.first.page.entries)).to be_ok
      end

      it "flags a changed entry id" do
        selected = [item("tag:alpha.example,2026:a1")]
        rendered = render(selected)
        rendered.first.xml = rendered.first.xml.sub("tag:alpha.example,2026:a1", "changed")
        result = described_class.call(rendered, selected: selected)
        expect(result.findings.map(&:kind)).to include(:id_changed)
      end

      it "flags a missing entry" do
        selected = [item("tag:alpha.example,2026:a1"), item("tag:alpha.example,2026:a2")]
        rendered = render(selected)
        first = rendered.first.page.entries.first
        rendered.first.xml = rendered.first.xml.sub(first[:entry].xml, "")
        result = described_class.call(rendered, selected: selected)
        expect(result.findings.map(&:kind)).to include(:entry_count)
      end

      it "flags a changed extension type" do
        selected = [item("tag:alpha.example,2026:a1")]
        rendered = render(selected)
        rendered.first.xml = rendered.first.xml.sub('type="image/jpeg"', 'type="image/png"')
        result = described_class.call(rendered, selected: selected)
        expect(result.findings.map(&:kind)).to include(:extension_type_changed)
      end

      it "flags changed signed bytes" do
        selected = [item("tag:alpha.example,2026:a1")]
        selected.first[:entry].digest = "deadbeef"
        rendered = render(selected)
        rendered.first.xml = rendered.first.xml.sub("Alpha one", "Changed one")
        result = described_class.call(rendered, selected: selected)
        expect(result.findings.map(&:kind)).to include(:signed_bytes_changed)
      end

      it "accepts unchanged signed bytes" do
        selected = [item("tag:alpha.example,2026:a1")]
        selected.first[:entry].digest = "deadbeef"
        rendered = render(selected)
        result = described_class.call(rendered, selected: selected)
        expect(result.findings.map(&:kind)).not_to include(:signed_bytes_changed)
      end

      it "flags a lost attachment reference" do
        selected = [item("tag:alpha.example,2026:a1")]
        selected.first[:attachment_targets] = {
          "https://alpha.example/editions/2026-11-20/audio/a1.mp3" => "local/a1.mp3"
        }
        selected.first[:resolved_references] = {
          "audio/a1.mp3" => "https://alpha.example/editions/2026-11-20/audio/a1.mp3"
        }
        rendered = render(selected)
        rendered.first.xml = rendered.first.xml.sub('href="audio/a1.mp3"', 'href="other.mp3"')
        result = described_class.call(rendered, selected: selected)
        expect(result.findings.map(&:kind)).to include(:attachment_changed)
      end

      it "exposes a structured hash" do
        rendered = render([item("tag:alpha.example,2026:a1")])
        hash = described_class.call(rendered, selected: rendered.first.page.entries).to_h
        expect(hash).to include(:ok, :findings)
      end
    end
  end
end
