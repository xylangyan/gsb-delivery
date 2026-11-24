# frozen_string_literal: true

require "spec_helper"

module Feedjira
  module OfflineArchive
    describe SourceDocument do
      def document(name, xml)
        path = temporary_atom(xml, basename: name)
        described_class.new(name, path, uri: "https://#{name}.example/")
      end

      let(:xml) do
        <<~XML
          <feed xmlns="http://www.w3.org/2005/Atom"
                xmlns:media="http://search.yahoo.com/mrss/"
                xml:base="https://example.org/edition/">
            <title>Feed</title>
            <entry xml:base="sub/">
              <id>e1</id>
              <title>One</title>
              <updated>2026-11-20T08:00:00Z</updated>
              <link rel="enclosure" href="audio/a.mp3"/>
              <media:content url="pics/a.jpg" type="image/jpeg"/>
            </entry>
            <entry xmlns:media="http://example.org/other">
              <id>e2</id>
              <updated>2026-11-20T09:00:00Z</updated>
              <media:content url="pics/b.jpg"/>
            </entry>
            <wrapper><entry><id>nested</id></entry></wrapper>
          </feed>
        XML
      end

      subject(:source) { document("alpha", xml) }

      it "only exposes direct entry children" do
        expect(source.entries.map(&:id)).to eq(%w[e1 e2])
      end

      it "records the feed base" do
        expect(source.feed_base).to eq("https://example.org/edition/")
      end

      it "records the entry frame version from updated" do
        expect(source.entries.first.version).to eq("2026-11-20T08:00:00Z")
      end

      it "marks a feed level prefix as inherited rather than local" do
        binding = source.entries.first.namespaces.find { |namespace| namespace.prefix == "media" }
        expect(binding.local).to be(false)
      end

      it "marks a prefix rebound inside the entry as local" do
        binding = source.entries.last.namespaces.find { |namespace| namespace.prefix == "media" }
        expect(binding.local).to be(true)
        expect(binding.href).to eq("http://example.org/other")
      end

      it "collects the xml:base chain declared on the entry" do
        expect(source.entries.first.local_bases).to eq(["sub/"])
      end

      it "lists relative references without resolving them" do
        expect(source.entries.first.relative_references).to eq(["audio/a.mp3", "pics/a.jpg"])
      end

      it "records the expanded name and type of extension elements" do
        extension = source.entries.first.extensions.first
        expect(extension.name).to eq("content")
        expect(extension.namespace).to eq("http://search.yahoo.com/mrss/")
        expect(extension.type).to eq("image/jpeg")
      end

      it "gives a one based line range" do
        first, last = source.entries.first.line_range
        expect(first).to eq(5)
        expect(last).to be >= first
      end

      it "preserves the original entry bytes" do
        expect(source.entries.first.xml).to include("<entry xml:base=\"sub/\">")
      end

      it "raises when parsed and sliced entry counts diverge" do
        allow(RawDocument).to receive(:new).and_wrap_original do |original, bytes|
          raw = original.call(bytes)
          allow(raw).to receive(:slices).and_return([])
          raw
        end
        expect { source }.to raise_error(SlicingError, /entry count mismatch/)
      end

      it "falls back to the document uri when no xml:base is present" do
        plain = document("plain", "<feed xmlns=\"http://www.w3.org/2005/Atom\"><entry><id>x</id></entry></feed>")
        expect(plain.feed_base).to eq("https://plain.example/")
      end
    end
  end
end
