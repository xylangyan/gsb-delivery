# frozen_string_literal: true

require "spec_helper"

module Feedjira
  module OfflineArchive
    describe Selector do
      def docs_from(*names)
        names.to_h do |name|
          [name, [SourceDocument.new(name, fixture_path("sources", "#{name}-1120.atom"))]]
        end
      end

      let(:alpha) { SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom")) }
      let(:alpha_next_day) { SourceDocument.new("alpha", fixture_path("sources", "alpha-1121.atom")) }
      let(:digest) { SourceDocument.new("digest", fixture_path("sources", "digest-1120.atom")) }

      def issue(overrides = {})
        base = {
          id: "issue-1", cutoff: Time.utc(2026, 11, 21), page_limit: 5,
          sources: [], entries: [], must_group: [], retractions: []
        }
        Catalog::Issue.new(**base.merge(overrides))
      end

      def entry_spec(id, **extra)
        base = {
          source: "alpha", id: id, version: nil, confirmed: true,
          include: true, signature: nil, resources: {}
        }
        Catalog::EntrySpec.new(**base.merge(extra))
      end

      def selection_for(issue, sources, verification: {}, resources: {})
        described_class.call(issue, sources, verification: verification, resources: resources)
      end

      def retraction(**extra)
        base = {
          notice_source: "digest", original_source: "alpha", target_source: "alpha",
          target_id: "tag:alpha.example,2026:a3", target_version: nil,
          published_at: Time.utc(2026, 11, 20, 15), effective: true, forwarded: true,
          reason: nil, object: nil
        }
        Catalog::RetractionSpec.new(**base.merge(extra))
      end

      it "selects the entries listed in the issue" do
        spec = entry_spec("tag:alpha.example,2026:a1")
        selection = selection_for(issue(entries: [spec]), { "alpha" => [alpha] })
        expect(selection.entries.map { |item| item[:id] }).to eq(["tag:alpha.example,2026:a1"])
      end

      it "records why an unconfirmed entry was skipped" do
        spec = entry_spec("tag:alpha.example,2026:a1", confirmed: false)
        selection = selection_for(issue(entries: [spec]), { "alpha" => [alpha] })
        expect(selection.unselected.first[:reason]).to eq(:not_confirmed)
      end

      it "records explicitly excluded entries" do
        spec = entry_spec("tag:alpha.example,2026:a1", include: false)
        selection = selection_for(issue(entries: [spec]), { "alpha" => [alpha] })
        expect(selection.unselected.first[:reason]).to eq(:excluded)
      end

      it "records missing source entries" do
        spec = entry_spec("tag:alpha.example,2026:nope")
        selection = selection_for(issue(entries: [spec]), { "alpha" => [alpha] })
        expect(selection.unselected.first[:reason]).to eq(:source_entry_missing)
      end

      it "skips entries newer than the cutoff" do
        spec = entry_spec("tag:alpha.example,2026:a3")
        selection = selection_for(issue(entries: [spec]), { "alpha" => [alpha, alpha_next_day] })
        expect(selection.entries.map { |item| item[:version] }).to eq(["2026-11-20T10:00:00Z"])
      end

      it "honours a pinned version" do
        spec = entry_spec("tag:alpha.example,2026:a3", version: "2026-11-20T10:00:00Z")
        selection = selection_for(issue(entries: [spec]), { "alpha" => [alpha, alpha_next_day] })
        expect(selection.entries.first[:version]).to eq("2026-11-20T10:00:00Z")
      end

      it "falls back to the newest version when a pinned one is absent" do
        spec = entry_spec("tag:alpha.example,2026:a3", version: "2025-01-01T00:00:00Z")
        selection = selection_for(issue(entries: [spec]), { "alpha" => [alpha, alpha_next_day] })
        expect(selection.entries.first[:version]).to eq("2026-11-20T10:00:00Z")
      end

      it "raises for an unknown source" do
        spec = entry_spec("tag:alpha.example,2026:a1")
        expect { selection_for(issue(entries: [spec]), {}) }
          .to raise_error(CatalogError, /unknown source/)
      end

      it "removes only the retracted entry" do
        specs = %w[a1 a3 a4].map { |suffix| entry_spec("tag:alpha.example,2026:#{suffix}") }
        selection = selection_for(
          issue(entries: specs, retractions: [retraction]), { "alpha" => [alpha], "digest" => [digest] }
        )
        expect(selection.entries.map { |item| item[:id] })
          .to eq(%w[tag:alpha.example,2026:a1 tag:alpha.example,2026:a4])
      end

      it "keeps the original publication source as the effective source" do
        specs = [entry_spec("tag:alpha.example,2026:a3")]
        selection = selection_for(
          issue(entries: specs, retractions: [retraction]), { "alpha" => [alpha], "digest" => [digest] }
        )
        applied = selection.retractions.first
        expect(applied.effective_source).to eq("alpha")
        expect(applied.published_by).to eq("digest")
        expect(applied.forwarded).to be(true)
      end

      it "leaves an ineffective notice unapplied" do
        specs = [entry_spec("tag:alpha.example,2026:a3")]
        selection = selection_for(
          issue(entries: specs, retractions: [retraction(effective: false)]),
          { "alpha" => [alpha], "digest" => [digest] }
        )
        expect(selection.entries.length).to eq(1)
        expect(selection.retractions).to eq([])
      end

      it "does not retract a newer version when the notice pins the old one" do
        specs = [
          entry_spec("tag:alpha.example,2026:a3", version: "2026-11-21T07:30:00Z")
        ]
        newer = retraction(target_version: "2026-11-20T10:00:00Z")
        selection = selection_for(
          issue(entries: specs, cutoff: Time.utc(2026, 11, 22), retractions: [newer]),
          { "alpha" => [alpha, alpha_next_day], "digest" => [digest] }
        )
        expect(selection.entries.length).to eq(1)
        expect(selection.retractions).to eq([])
      end

      it "retracts the newest version that existed when the notice was published" do
        specs = [entry_spec("tag:alpha.example,2026:a3", version: "2026-11-20T10:00:00Z")]
        unpinned = retraction
        selection = selection_for(
          issue(entries: specs, retractions: [unpinned]),
          { "alpha" => [alpha, alpha_next_day], "digest" => [digest] }
        )
        expect(selection.entries).to eq([])
      end

      it "records nothing when the retraction targets an unselected release" do
        specs = [entry_spec("tag:alpha.example,2026:a1")]
        selection = selection_for(
          issue(entries: specs, retractions: [retraction]), { "alpha" => [alpha], "digest" => [digest] }
        )
        expect(selection.retractions).to eq([])
      end

      it "prefers the verifier supplied signature over the catalog copy" do
        spec = entry_spec("tag:alpha.example,2026:a1", signature: { "verified" => false })
        selection = selection_for(
          issue(entries: [spec]), { "alpha" => [alpha] },
          verification: { "entries" => { "alpha|tag:alpha.example,2026:a1" => { "verified" => true } } }
        )
        expect(selection.entries.first[:signature]["verified"]).to be(true)
      end

      it "resolves each relative reference through its base chain" do
        spec = entry_spec("tag:alpha.example,2026:a1")
        selection = selection_for(issue(entries: [spec]), { "alpha" => [alpha] })
        resolved = selection.entries.first[:resolved_references]
        expect(resolved["audio/a1.mp3"]).to eq("https://alpha.example/editions/2026-11-20/audio/a1.mp3")
      end
    end
  end
end
