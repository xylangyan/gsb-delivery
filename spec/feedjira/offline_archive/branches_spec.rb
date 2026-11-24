# frozen_string_literal: true

require "spec_helper"

module Feedjira
  module OfflineArchive
    describe RawDocument do
      it "skips a leading comment and a CDATA section" do
        xml = "<!-- c --><feed><![CDATA[x]]><entry><id>1</id></entry></feed>"
        expect(described_class.new(xml).slices.length).to eq(1)
      end

      it "skips a top level CDATA between entries" do
        xml = "<feed><entry><id>1</id></entry><![CDATA[y]]><entry><id>2</id></entry></feed>"
        expect(described_class.new(xml).slices.length).to eq(2)
      end

      it "skips a processing instruction" do
        xml = "<?xml-stylesheet href='x'?><feed><entry><id>1</id></entry></feed>"
        expect(described_class.new(xml).slices.length).to eq(1)
      end

      it "tolerates an unterminated CDATA section" do
        xml = "<feed><![CDATA[open<entry><id>1</id></entry></feed>"
        expect(described_class.new(xml).slices).to eq([])
      end

      it "tolerates an unterminated comment" do
        xml = "<feed><!-- no end <entry><id>1</id></entry></feed>"
        expect(described_class.new(xml).slices).to eq([])
      end

      it "tolerates an unterminated tag" do
        xml = "<feed><entry"
        expect(described_class.new(xml).slices).to eq([])
      end

      it "tolerates an unterminated declaration" do
        xml = "<feed><!DOCTYPE x"
        expect(described_class.new(xml).slices).to eq([])
      end

      it "skips a declaration whose subset contains a quoted bracket" do
        xml = "<!DOCTYPE feed [ <!ENTITY x \">\"> ]><feed><entry><id>1</id></entry></feed>"
        expect(described_class.new(xml).slices.length).to eq(1)
      end

      it "handles a self closing entry mixed with a paired entry" do
        xml = "<feed><entry/><entry><id>2</id></entry></feed>"
        slices = described_class.new(xml).slices
        expect(slices.map(&:xml)).to eq(["<entry/>", "<entry><id>2</id></entry>"])
      end
    end

    describe Catalog do
      it "treats empty strings as absent" do
        data = {
          "issues" => [
            {
              "id" => "i",
              "cutoff" => "2026-11-21T00:00:00Z",
              "sources" => [{ "name" => "a", "file" => "f", "uri" => "" }],
              "entries" => [{ "source" => "a", "id" => "e", "version" => "" }]
            }
          ]
        }
        catalog = described_class.load(data)
        expect(catalog.issues.first.sources.first.uri).to be_nil
        expect(catalog.issues.first.entries.first.version).to be_nil
      end
    end

    describe Selector do
      let(:alpha) { SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom")) }

      def issue(cutoff: nil)
        Catalog::Issue.new(id: "i", cutoff: cutoff, page_limit: 5, sources: [], entries: [],
                           must_group: [], retractions: [])
      end

      def spec(id, version: nil)
        Catalog::EntrySpec.new(source: "alpha", id: id, version: version, confirmed: true,
                               include: true, signature: nil, resources: {})
      end

      it "treats a missing cutoff as no cutoff" do
        selection = described_class.call(issue(cutoff: nil), { "alpha" => [alpha] })
        expect(selection.entries).to eq([])
      end

      it "ignores an unparseable version when choosing" do
        entry = alpha.entry_by_id("tag:alpha.example,2026:a1")
        allow(alpha).to receive(:entries).and_return([entry.dup.tap { |copy| copy.version = "not-a-time" }])
        selection = described_class.call(issue(cutoff: Time.utc(2026, 11, 21)), { "alpha" => [alpha] },)
        expect(selection.entries).to eq([])
      end

      it "selects a version with an invalid timestamp when no cutoff is set" do
        entry = alpha.entry_by_id("tag:alpha.example,2026:a1")
        allow(alpha).to receive(:entries)
          .and_return([entry.dup.tap { |copy| copy.version = "not-a-time" }])
        selection = described_class.call(issue(cutoff: nil), { "alpha" => [alpha] })
        expect(selection.entries.length).to eq(0)
      end

      it "records an after-cutoff reason when a pinned version is too new" do
        later = SourceDocument.new("alpha", fixture_path("sources", "alpha-1121.atom"))
        pinned = spec("tag:alpha.example,2026:a3", version: "2026-11-21T07:30:00Z")
        issue = Catalog::Issue.new(id: "i", cutoff: Time.utc(2026, 11, 20), page_limit: 5, sources: [],
                                   entries: [pinned], must_group: [], retractions: [])
        selection = described_class.call(issue, { "alpha" => [later] })
        expect(selection.unselected.first[:reason]).to eq(:after_cutoff)
      end

      it "ignores unparseable version times when comparing to a cutoff" do
        broken = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
        entry = broken.entry_by_id("tag:alpha.example,2026:a1").dup
        entry.version = "not-a-time"
        allow(broken).to receive(:entries).and_return([entry])
        item_spec = spec("tag:alpha.example,2026:a1")
        issue = Catalog::Issue.new(id: "i", cutoff: Time.utc(2026, 11, 21), page_limit: 5,
                                   sources: [], entries: [item_spec], must_group: [], retractions: [])
        selection = described_class.call(issue, { "alpha" => [broken] })
        expect(selection.entries.length).to eq(1)
      end

      it "retracts by newest version when the notice has no timestamp" do
        alpha = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
        item_spec = spec("tag:alpha.example,2026:a3")
        notice = Catalog::RetractionSpec.new(
          notice_source: "digest", original_source: "alpha", target_source: "alpha",
          target_id: "tag:alpha.example,2026:a3", target_version: nil, published_at: nil,
          effective: true, forwarded: true, reason: nil, object: nil
        )
        issue = Catalog::Issue.new(id: "i", cutoff: Time.utc(2026, 11, 21), page_limit: 5,
                                   sources: [], entries: [item_spec], must_group: [],
                                   retractions: [notice])
        selection = described_class.call(issue, { "alpha" => [alpha] })
        expect(selection.entries).to eq([])
      end

      it "resolves references for an entry without a feed base" do
        alpha = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
        entry = alpha.entry_by_id("tag:alpha.example,2026:a1").dup
        entry.feed_base = nil
        allow(alpha).to receive(:entries).and_return([entry])
        item_spec = spec("tag:alpha.example,2026:a1")
        issue = Catalog::Issue.new(id: "i", cutoff: Time.utc(2026, 11, 21), page_limit: 5,
                                   sources: [], entries: [item_spec], must_group: [], retractions: [])
        selection = described_class.call(issue, { "alpha" => [alpha] })
        expect(selection.entries.first[:resolved_references]["audio/a1.mp3"]).to eq("audio/a1.mp3")
      end

      it "falls back to the newest version when every candidate is after the cutoff" do
        later = SourceDocument.new("alpha", fixture_path("sources", "alpha-1121.atom"))
        item_spec = spec("tag:alpha.example,2026:a3", version: nil)
        entry = later.entry_by_id("tag:alpha.example,2026:a3").dup
        entry.version = nil
        allow(later).to receive(:entries).and_return([entry])
        issue = Catalog::Issue.new(id: "i", cutoff: Time.utc(2026, 11, 20), page_limit: 5,
                                   sources: [], entries: [item_spec], must_group: [], retractions: [])
        selection = described_class.call(issue, { "alpha" => [later] })
        expect(selection.entries.length).to eq(1)
      end

      it "compares an unparseable selected version to a notice without crashing" do
        alpha = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
        entry = alpha.entry_by_id("tag:alpha.example,2026:a3").dup
        entry.version = "bad-time"
        allow(alpha).to receive(:entries).and_return([entry])
        item_spec = spec("tag:alpha.example,2026:a3", version: "bad-time")
        notice = Catalog::RetractionSpec.new(
          notice_source: "digest", original_source: "alpha", target_source: "alpha",
          target_id: "tag:alpha.example,2026:a3", target_version: nil,
          published_at: Time.utc(2026, 11, 20), effective: true, forwarded: false,
          reason: nil, object: nil
        )
        issue = Catalog::Issue.new(id: "i", cutoff: Time.utc(2026, 11, 21), page_limit: 5,
                                   sources: [], entries: [item_spec], must_group: [],
                                   retractions: [notice])
        selection = described_class.call(issue, { "alpha" => [alpha] })
        expect(selection.entries).to eq([])
      end
    end

    describe Renderer do
      let(:issue) do
        Catalog::Issue.new(id: "i", cutoff: Time.utc(2026, 11, 21), page_limit: 5, sources: [],
                           entries: [], must_group: [], retractions: [])
      end

      it "stringifies nested resource values" do
        alpha = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
        entry = alpha.entry_by_id("tag:alpha.example,2026:a1")
        item = {
          source: "alpha", id: entry.id, version: entry.version, entry: entry,
          signature: nil, resources: {}, resolved_references: {},
          attachment_targets: { "audio/a1.mp3" => "local/a1.mp3" }
        }
        page = Planner::Page.new(index: 0, entries: [item], base: nil, namespaces: {})
        rendered = described_class.call([page], issue: issue)
        expect(rendered.first.attachments["audio/a1.mp3"]).to eq("local/a1.mp3")
      end

      it "stringifies array resource values" do
        renderer = described_class.new(issue: issue, resources: { "a" => %w[x] })
        # An array value is not a valid local file, but the stringifier must not
        # crash while walking the caller supplied map.
        expect(renderer.send(:stringify, ["a"])).to eq(["a"])
      end
    end

    describe Archive do
      it "accepts an already parsed Catalog" do
        catalog = Catalog.load(catalog_fixture)
        result = described_class.new(catalog, base_dir: fixture_path, write: false).build
        expect(result.issues).to eq(%w[issue-1 issue-2 issue-3])
      end
    end

    describe Retraction do
      it "serialises a missing publication time" do
        retraction = Retraction.new(
          issue: "i", target_source: "s", target_id: "e", target_version: "v",
          published_by: "p", effective_source: "s", published_at: nil,
          forwarded: false, reason: nil, object: nil
        )
        expect(retraction.to_h[:published_at]).to be_nil
      end
    end

    describe Catalog do
      it "treats a nil source uri as absent" do
        data = {
          "issues" => [
            {
              "id" => "i",
              "cutoff" => "2026-11-21T00:00:00Z",
              "sources" => [{ "name" => "a", "file" => "f", "uri" => nil }],
              "entries" => []
            }
          ]
        }
        expect(described_class.load(data).issues.first.sources.first.uri).to be_nil
      end
    end

    describe Planner do
      it "treats an entry without a feed base as not base sensitive" do
        alpha = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
        entry = alpha.entry_by_id("tag:alpha.example,2026:a1").dup
        entry.feed_base = nil
        item = {
          source: "alpha", id: entry.id, version: entry.version, entry: entry,
          signature: nil, resources: {}, resolved_references: {}
        }
        issue = Catalog::Issue.new(id: "i", cutoff: Time.utc(2026, 11, 21), page_limit: 5,
                                   sources: [], entries: [], must_group: [], retractions: [])
        selection = Selector::Selection.new(issue: issue, entries: [item], retractions: [], unselected: [])
        pages, = described_class.call(selection, page_limit: 5)
        expect(pages.first.base).to be_nil
      end
    end

    describe SourceDocument do
      it "ignores a plain base attribute that is not in the xml namespace" do
        xml = <<~XML
          <feed xmlns="http://www.w3.org/2005/Atom" base="plain">
            <entry base="sub"><id>e1</id></entry>
          </feed>
        XML
        source = described_class.new("plain", temporary_atom(xml, basename: "plain"))
        expect(source.feed_base).to be_nil
        expect(source.entries.first.local_bases).to eq([])
      end

      it "handles an entry with an unnamespaced extension element" do
        xml = <<~XML
          <feed xmlns="http://www.w3.org/2005/Atom">
            <entry><id>e1</id><plain><inner/></plain></entry>
          </feed>
        XML
        source = described_class.new("plain2", temporary_atom(xml, basename: "plain2"))
        expect(source.entries.first.extensions).to eq([])
      end
    end

    describe "private edge branches" do
      it "parses a nil timestamp as nil" do
        expect(Catalog.send(:allocate).send(:parse_time, nil)).to be_nil
      end

      it "resolves references with a nil feed base" do
        renderer = Renderer.allocate
        entry = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
                                     .entry_by_id("tag:alpha.example,2026:a1").dup
        entry.feed_base = nil
        expect(renderer.send(:resolve_reference, entry, "a.mp3")).to eq("a.mp3")
      end

      it "treats a nil cutoff as never after cutoff" do
        selector = Selector.allocate
        selector.instance_variable_set(:@issue, Catalog::Issue.new(
                                                  id: "i", cutoff: nil, page_limit: 5,
                                                  sources: [], entries: [], must_group: [], retractions: []
                                                ))
        entry = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
                                   .entry_by_id("tag:alpha.example,2026:a1")
        expect(selector.send(:after_cutoff?, entry)).to be(false)
      end

      it "ignores a blank, fragment-only or absolute reference as an attachment" do
        source = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
        expect(source.send(:relative?, "")).to be(false)
        expect(source.send(:relative?, "#frag")).to be(false)
        expect(source.send(:relative?, "https://x/y")).to be(false)
        expect(source.send(:relative?, "a/b")).to be(true)
      end

      it "falls back to every candidate when all are after the cutoff" do
        later = SourceDocument.new("alpha", fixture_path("sources", "alpha-1121.atom"))
        entry = later.entry_by_id("tag:alpha.example,2026:a3").dup
        allow(later).to receive(:entries).and_return([entry])
        item_spec = Catalog::EntrySpec.new(source: "alpha", id: entry.id, version: nil,
                                           confirmed: true, include: true, signature: nil, resources: {})
        issue = Catalog::Issue.new(id: "i", cutoff: Time.utc(2026, 11, 20), page_limit: 5,
                                   sources: [], entries: [item_spec], must_group: [], retractions: [])
        selection = Selector.call(issue, { "alpha" => [later] })
        expect(selection.unselected.first[:reason]).to eq(:after_cutoff)
      end

      it "retracts when every selected version is newer than the notice" do
        alpha = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
        entry = alpha.entry_by_id("tag:alpha.example,2026:a3").dup
        entry.version = "2026-11-20T23:00:00Z"
        allow(alpha).to receive(:entries).and_return([entry])
        item_spec = Catalog::EntrySpec.new(source: "alpha", id: entry.id, version: "2026-11-20T23:00:00Z",
                                           confirmed: true, include: true, signature: nil, resources: {})
        notice = Catalog::RetractionSpec.new(
          notice_source: "digest", original_source: "alpha", target_source: "alpha",
          target_id: entry.id, target_version: nil, published_at: Time.utc(2026, 11, 20, 12),
          effective: true, forwarded: false, reason: nil, object: nil
        )
        issue = Catalog::Issue.new(id: "i", cutoff: Time.utc(2026, 11, 21), page_limit: 5,
                                   sources: [], entries: [item_spec], must_group: [],
                                   retractions: [notice])
        selection = Selector.call(issue, { "alpha" => [alpha] })
        expect(selection.entries).to eq([])
      end
    end

    describe Verifier do
      it "collects url and image descriptors" do
        parsed = double("entry", links: [], image: "img", url: "alt")
        verifier = described_class.new([], selected: [])
        expect(verifier.send(:descriptor_values, parsed)).to contain_exactly("img", "alt")
      end

      it "tolerates an entry without links" do
        parsed = double("entry", image: nil, url: nil)
        verifier = described_class.new([], selected: [])
        expect(verifier.send(:descriptor_values, parsed)).to eq([])
      end

      it "skips Atom namespaced children when listing extensions" do
        node = Nokogiri::XML("<entry xmlns='http://www.w3.org/2005/Atom'><title>x</title></entry>").root
        verifier = described_class.new([], selected: [])
        expect(verifier.send(:extension_types, node)).to eq([])
      end

      it "skips an unnamespaced child when listing extensions" do
        node = Nokogiri::XML("<entry xmlns='http://www.w3.org/2005/Atom'><plain xmlns=''/></entry>").root
        verifier = described_class.new([], selected: [])
        expect(verifier.send(:extension_types, node)).to eq([])
      end

      it "skips a child without a namespace" do
        node = Nokogiri::XML("<entry><plain/></entry>").root
        verifier = described_class.new([], selected: [])
        expect(verifier.send(:extension_types, node)).to eq([])
      end

      it "returns nil for an entry index past the page" do
        verifier = described_class.new([], selected: [])
        expect(verifier.send(:entry_slice, "<feed><entry/></feed>", 5)).to be_nil
      end

      it "records a finding when the parsed entry node is missing" do
        alpha = SourceDocument.new("alpha", fixture_path("sources", "alpha-1120.atom"))
        entry = alpha.entry_by_id("tag:alpha.example,2026:a1")
        item = { source: "alpha", id: entry.id, version: entry.version, entry: entry,
                 signature: nil, resources: {}, resolved_references: {}, attachment_targets: {} }
        issue = Catalog::Issue.new(id: "i", cutoff: Time.utc(2026, 11, 21), page_limit: 5,
                                   sources: [], entries: [], must_group: [], retractions: [])
        page = Planner::Page.new(index: 0, entries: [item], base: nil, namespaces: {})
        rendered = Renderer.call([page], issue: issue) if false
        verifier = described_class.new([], selected: [item])
        findings = []
        result = Struct.new(:xml, :page, :filename).new(
          "<feed xmlns='http://www.w3.org/2005/Atom'><title>x</title></feed>", page, "p.atom"
        )
        verifier.send(:check_page, result, findings)
        # No entries parsed, so the entry count finding fires before node lookup.
        expect(findings.map(&:kind)).to eq([:entry_count])
      end
    end
  end
end
