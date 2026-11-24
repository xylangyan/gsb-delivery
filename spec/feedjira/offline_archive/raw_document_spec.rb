# frozen_string_literal: true

require "spec_helper"

module Feedjira
  module OfflineArchive
    describe RawDocument do
      let(:xml) do
        <<~XML
          <?xml version="1.0" encoding="UTF-8"?>
          <!DOCTYPE feed [ <!ENTITY x "y"> ]>
          <!-- top level comment -->
          <feed xmlns="http://www.w3.org/2005/Atom" data="q>z">
            <title>F</title>
            <entry  b='1' >
              <title> A &amp; B </title>
              <!-- inner -->
              <content><![CDATA[ <x> ]]></content>
            </entry>
            <entry/>
            <other><entry>nested</entry></other>
            <entry><title>three</title></entry>
          </feed>
        XML
      end

      it "slices only the direct children of the feed" do
        expect(described_class.new(xml).slices.map(&:index)).to eq([0, 1, 2])
      end

      it "preserves the original bytes including quoting and whitespace" do
        first = described_class.new(xml).slice_xml(0)
        expect(first).to include("<entry  b='1' >")
        expect(first).to include("<!-- inner -->")
        expect(first).to include("<![CDATA[ <x> ]]>")
        expect(first).to end_with("</entry>")
      end

      it "records byte ranges that address the original document" do
        slice = described_class.new(xml).slice(0)
        expect(xml.byteslice(slice.start_byte, slice.length)).to eq(slice.xml)
      end

      it "supports self closing entries" do
        expect(described_class.new(xml).slice_xml(1)).to eq("<entry/>")
      end

      it "exposes a helpful length and bytes" do
        slice = described_class.new(xml).slice(0)
        expect(slice.bytes).to eq(slice.xml.b)
        expect(slice.length).to eq(slice.xml.bytesize)
      end

      it "keeps non UTF-8 bytes instead of transcoding them" do
        latin = "<feed>&#233;</feed>".b
        expect(described_class.new(latin).xml.encoding).to eq(Encoding::BINARY)
      end

      it "tolerates a document without any entry" do
        expect(described_class.new("<feed><title>x</title></feed>").slices).to eq([])
      end

      it "raises when an index is missing" do
        expect { described_class.new(xml).slice(9) }.to raise_error(IndexError)
      end
    end
  end
end
