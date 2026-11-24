# frozen_string_literal: true

require "spec_helper"

module Feedjira
  module OfflineArchive
    describe "structured results" do
      it "serialises every result structure" do
        notice = Notice.new(kind: :k, issue: "i", source: "s", id: "e", locations: {}, message: "m", xml: "<entry/>")
        conflict = Conflict.new(issue: "i", entries: [], constraints: { rule: :x })
        retraction = Retraction.new(
          issue: "i", target_source: "s", target_id: "e", target_version: "v",
          published_by: "p", effective_source: "s", published_at: Time.utc(2026, 11, 20),
          forwarded: true, reason: "r", object: "o"
        )
        retained = RetainedSource.new(
          issue: "i", source: "s", id: "e", version: "v", file: "f",
          line_range: [1, 2], byte_range: [0, 9]
        )
        artifact = Archive::PageArtifact.new(
          issue: "i", filename: "x.atom", xml: "<feed/>",
          entries: [{ source: "s", id: "e", version: "v" }], path: "/tmp/x.atom", attachments: {}
        )

        expect(notice.to_h[:kind]).to eq(:k)
        expect(conflict.to_h[:constraints]).to eq(rule: :x)
        expect(retraction.to_h[:published_at]).to eq("2026-11-20T00:00:00Z")
        expect(retraction.to_h[:forwarded]).to be(true)
        expect(retained.to_h[:file]).to eq("f")
        expect(artifact.to_h[:entries].first[:id]).to eq("e")
      end

      it "serialises an archive result" do
        result = build_archive
        hash = result.to_h
        expect(hash[:issues]).to eq(%w[issue-1 issue-2 issue-3])
        expect(hash[:retained_sources]).to be_a(Array)
        expect(hash[:verification]).to include(:ok)
      end

      it "serialises a verifier finding" do
        finding = Verifier::Finding.new(page: "p", kind: :k, detail: { a: 1 })
        expect(finding.to_h).to eq(page: "p", kind: :k, detail: { a: 1 })
      end
    end

    describe "source structures" do
      it "serialises namespace and extension values" do
        source = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
        entry = source.entry_by_id("tag:alpha.example,2026:a1")
        expect(entry.namespaces.map(&:to_h)).to all(include(:prefix, :href, :local))
        expect(entry.extensions.map(&:to_h)).to all(include(:name, :namespace, :type))
      end

      it "lists entry ids" do
        source = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
        expect(source.all_entry_ids).to include("tag:alpha.example,2026:a1")
      end
    end
  end
end
