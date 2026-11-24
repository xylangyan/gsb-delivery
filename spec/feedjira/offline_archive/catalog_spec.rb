# frozen_string_literal: true

require "spec_helper"

module Feedjira
  module OfflineArchive
    describe Catalog do
      let(:minimal) do
        {
          "issues" => [
            {
              "id" => "issue-1",
              "cutoff" => "2026-11-21T00:00:00Z",
              "sources" => [{ "name" => "alpha", "file" => "alpha.atom" }],
              "entries" => [{ "source" => "alpha", "id" => "e1" }]
            }
          ]
        }
      end

      it "normalises string keys" do
        catalog = described_class.load(minimal)
        expect(catalog.issue("issue-1").id).to eq("issue-1")
      end

      it "parses the cutoff as UTC" do
        expect(described_class.load(minimal).issues.first.cutoff).to eq(Time.utc(2026, 11, 21))
      end

      it "defaults page limit to zero and entries to confirmed and included" do
        issue = described_class.load(minimal).issues.first
        expect(issue.page_limit).to eq(0)
        expect(issue.entries.first.confirmed).to be(true)
        expect(issue.entries.first.include).to be(true)
      end

      it "accepts an explicit Time cutoff" do
        data = minimal.merge("issues" => [minimal["issues"].first.merge("cutoff" => Time.utc(2026, 11, 21))])
        expect(described_class.load(data).issues.first.cutoff).to eq(Time.utc(2026, 11, 21))
      end

      it "raises for a missing required field" do
        data = { "issues" => [{ "id" => "x" }] }
        expect { described_class.load(data) }.to raise_error(CatalogError, /cutoff/)
      end

      it "raises for an invalid timestamp" do
        data = minimal.merge("issues" => [minimal["issues"].first.merge("cutoff" => "not-a-time")])
        expect { described_class.load(data) }.to raise_error(CatalogError, /invalid timestamp/)
      end

      it "normalises verification and resources" do
        catalog = described_class.load(
          minimal,
          verification: { entries: { "a|b" => { "verified" => true } } },
          resources: { "a" => "b" }
        )
        expect(catalog.verification.dig("entries", "a|b", "verified")).to be(true)
        expect(catalog.resources).to eq("a" => "b")
      end

      it "records retraction forwarding and effective source" do
        data = minimal.merge(
          "issues" => [
            minimal["issues"].first.merge(
              "retractions" => [
                {
                  "notice_source" => "digest",
                  "original_source" => "alpha",
                  "target_id" => "e1",
                  "published_at" => "2026-11-20T15:00:00Z",
                  "forwarded" => true
                }
              ]
            )
          ]
        )
        notice = described_class.load(data).issues.first.retractions.first
        expect(notice.forwarded).to be(true)
        expect(notice.effective_source).to eq("alpha")
        expect(notice.published_at).to eq(Time.utc(2026, 11, 20, 15))
      end

      it "treats an explicitly ineffective notice as not applied" do
        data = minimal.merge(
          "issues" => [
            minimal["issues"].first.merge(
              "retractions" => [{ "target_id" => "e1", "effective" => false }]
            )
          ]
        )
        expect(described_class.load(data).issues.first.retractions.first.applies?).to be(false)
      end

      it "defaults an unspecified effective flag to applied" do
        data = minimal.merge(
          "issues" => [minimal["issues"].first.merge("retractions" => [{ "target_id" => "e1" }])]
        )
        expect(described_class.load(data).issues.first.retractions.first.applies?).to be(true)
      end
    end
  end
end
