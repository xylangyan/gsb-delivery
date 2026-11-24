# frozen_string_literal: true

require "spec_helper"

module Feedjira
  module OfflineArchive
    describe Archive do
      subject(:result) { build_archive }

      def page_for(issue)
        result.pages.select { |page| page.issue == issue }
      end

      def ids(issue)
        page_for(issue).flat_map { |page| page.page.entries.map { |entry| entry[:id] } }
      end

      describe "issue 1: a forwarded retraction" do
        it "removes only the retracted entry" do
          expect(ids("issue-1")).to contain_exactly(
            "tag:alpha.example,2026:a1",
            "tag:alpha.example,2026:a2",
            "tag:alpha.example,2026:a4"
          )
        end

        it "keeps the original publication source on the applied retraction" do
          applied = result.retractions.first
          expect(applied.target_id).to eq("tag:alpha.example,2026:a3")
          expect(applied.effective_source).to eq("alpha")
          expect(applied.published_by).to eq("digest")
          expect(applied.forwarded).to be(true)
          expect(applied.published_at).to eq(Time.utc(2026, 11, 20, 15))
        end

        it "does not touch the sibling entries from the same source" do
          expect(ids("issue-1")).not_to include("tag:alpha.example,2026:a3")
          expect(ids("issue-1")).to include("tag:alpha.example,2026:a4")
        end
      end

      describe "issue 2: same prefix, different namespaces" do
        it "keeps a rebound prefix on the same page as another source" do
          grouped = page_for("issue-2").find do |page|
            page.page.entries.any? { |entry| entry[:id] == "tag:beta.example,2026:b3" }
          end
          expect(grouped.page.entries.map { |entry| entry[:id] })
            .to include("tag:beta.example,2026:b3", "tag:gamma.example,2026:g2")
        end

        it "reports an unsatisfiable shared-prefix group with the original bytes" do
          conflict = result.conflicts.find { |item| item.issue == "issue-2" }
          expect(conflict.constraints[:kind]).to eq(:namespace)
          expect(conflict.constraints[:prefix]).to eq("media")
          expect(conflict.entries.map { |entry| entry[:id] })
            .to contain_exactly("tag:beta.example,2026:b1", "tag:gamma.example,2026:g1")
          expect(conflict.entries.map { |entry| entry[:xml] }).to all(include("<entry"))
        end

        it "keeps the remaining page readable" do
          expect(page_for("issue-2").flat_map { |page| page.page.entries.map { |entry| entry[:source] } })
            .to include("beta", "gamma")
        end
      end

      describe "issue 3: versions and signatures" do
        it "archives the version at the cutoff, not the next day release" do
          entry = page_for("issue-3").flat_map { |page| page.page.entries }
                                    .find { |item| item[:id] == "tag:alpha.example,2026:a3" }
          expect(entry[:version]).to eq("2026-11-20T10:00:00Z")
        end

        it "reports the signature that covers unselected content and keeps it off the pages" do
          notice = result.notices.first
          expect(notice.id).to eq("tag:epsilon.example,2026:e1")
          expect(notice.kind).to eq(:signature_unselected)
          expect(notice.locations[:unselected_paths]).to eq(["entry/content/p[2]"])
          expect(ids("issue-3")).not_to include("tag:epsilon.example,2026:e1")
        end

        it "shows the actual covered positions" do
          locations = result.notices.first.locations
          expect(locations[:entry_lines]).to be_a(Array)
          expect(locations[:entry_bytes].length).to eq(2)
          expect(locations[:uncovered_lines]).to eq([[5, 20]])
          expect(locations[:entry_lines]).not_to eq([5, 20])
        end
      end

      describe "page composition" do
        it "uses as few pages as the constraints allow" do
          expect(page_for("issue-1").length).to eq(1)
          expect(page_for("issue-3").length).to eq(1)
        end

        it "never exceeds the page limit" do
          result.pages.each do |page|
            issue = catalog_fixture["issues"].find { |item| item["id"] == page.issue }
            expect(page.page.entries.length).to be <= issue["page_limit"]
          end
        end

        it "keeps a required same-page group together" do
          page = page_for("issue-1").first
          grouped = page.page.entries.map { |entry| entry[:id] }
          expect(grouped).to include("tag:alpha.example,2026:a1", "tag:alpha.example,2026:a2")
        end
      end

      describe "reprint identity" do
        it "treats a reprint with a shared link as a distinct source" do
          sources = result.retained_sources.map(&:source).uniq
          expect(sources).to include("delta", "epsilon")
        end
      end

      describe "output fidelity" do
        it "writes pages Feedjira can open" do
          result.pages.each do |page|
            expect(Feedjira.parse(page.xml)).to be_a(Feedjira::Parser::Atom)
          end
        end

        it "passes the built-in verification" do
          expect(result.verification[:ok]).to be(true)
          expect(result.verification[:findings]).to eq([])
        end

        it "keeps signed entry bytes exact" do
          source = File.binread(fixture_path("sources", "alpha-1120.atom"))
          original = RawDocument.new(source).slice_xml(0).force_encoding("UTF-8")
          expect(page_for("issue-1").first.xml).to include(original)
        end

        it "resolves relative enclosures through the inherited base" do
          page = page_for("issue-2").find do |candidate|
            candidate.page.entries.any? { |entry| entry[:id].end_with?("g2") }
          end
          expect(page.attachments).to include("https://gamma.example/assets/art/g2.jpg")
        end
      end

      describe "writing to disk" do
        it "writes each page and reports the paths" do
          Dir.mktmpdir do |dir|
            catalog = catalog_fixture
            catalog["issues"].each { |issue| issue["sources"].each { |source| source["file"] = fixture_path(source["file"]) } }
            written = described_class.new(catalog, base_dir: dir, write: true).build
            path = written.pages.first.path
            expect(File.exist?(path)).to be(true)
            expect(File.read(path)).to eq(written.pages.first.xml)
          end
        end

        it "serialises written page artifacts" do
          Dir.mktmpdir do |dir|
            catalog = catalog_fixture
            catalog["issues"].each { |issue| issue["sources"].each { |source| source["file"] = fixture_path(source["file"]) } }
            written = described_class.new(catalog, base_dir: dir, write: true).build
            hash = written.to_h
            expect(hash[:pages].first[:file]).to match(/\.atom\z/)
            expect(hash[:pages].first[:entries]).to be_a(Array)
          end
        end

        it "treats a zero page limit as unlimited" do
          Dir.mktmpdir do |dir|
            catalog = catalog_fixture
            catalog["issues"].each do |issue|
              issue["page_limit"] = 0
              issue["sources"].each { |source| source["file"] = fixture_path(source["file"]) }
            end
            written = described_class.new(catalog, base_dir: dir, write: true).build
            expect(written.pages).not_to be_empty
          end
        end
      end

      describe "catalog errors" do
        it "raises for an unknown source" do
          catalog = {
            "issues" => [
              single_issue(
                "sources" => [], "entries" => [alpha_entry("tag:alpha.example,2026:a1")]
              )
            ]
          }
          expect { described_class.new(catalog, base_dir: fixture_path, write: false).build }
            .to raise_error(CatalogError, /unknown source/)
        end
      end
    end
  end
end
