# frozen_string_literal: true

require "spec_helper"

describe "Roo::Excelx image placements" do
  subject(:xlsx) { Roo::Excelx.new("test/files/image_placements.xlsx") }

  describe "#image_placements" do
    it "keeps every placement of a shared media with its own coordinates" do
      quotes = xlsx.image_placements("Quotes")
      summary = xlsx.image_placements("Summary")

      shared_quotes = quotes.find { |p| p.name == "Merged span" }
      shared_summary = summary.find { |p| p.name == "Shared logo" }

      expect(shared_quotes.media).to eq("roo_media_image1.png")
      expect(shared_summary.media).to eq("roo_media_image1.png")
      expect(shared_quotes.path).to eq(shared_summary.path)
      expect([shared_quotes.row, shared_quotes.col]).to eq([3, 2])
      expect([shared_summary.row, shared_summary.col]).to eq([1, 1])
    end

    it "reports original anchor coordinates and offsets" do
      placement = xlsx.image_placements("Quotes").find { |p| p.name == "Merged span" }

      expect(placement.anchor).to eq(:two_cell)
      expect(placement.row).to eq(3)
      expect(placement.col).to eq(2)
      expect(placement.to_row).to eq(6)
      expect(placement.to_col).to eq(2)
      expect(placement.to_row_offset).to eq(9525)
      expect(placement.to_col_offset).to eq(114300)
    end

    it "covers the merged range in original coordinates" do
      placement = xlsx.image_placements("Quotes").find { |p| p.name == "Merged span" }

      expect(placement.covers?(3, 2)).to be(true)
      expect(placement.covers?(6, 2)).to be(true)
      expect(placement.covers?(7, 2)).to be(false)
      expect(placement.covers?(4, 3)).to be(false)
    end

    it "keeps dimensions of one-cell anchors" do
      placement = xlsx.image_placements("Quotes").find { |p| p.name == "External ref" }

      expect(placement.anchor).to eq(:one_cell)
      expect(placement.width).to eq(952500)
      expect(placement.height).to eq(476250)
    end

    it "treats external pictures as declarations only" do
      placement = xlsx.image_placements("Quotes").find { |p| p.name == "External ref" }

      expect(placement.external?).to be(true)
      expect(placement.missing?).to be(false)
      expect(placement.path).to be_nil
      expect(placement.target).to eq("https://example.com/quotes/photo.png")
    end

    it "distinguishes a missing media entry from a sheet without images" do
      placement = xlsx.image_placements("Quotes").find { |p| p.name == "Missing media" }

      expect(placement.missing?).to be(true)
      expect(placement.external?).to be(false)
      expect(placement.path).to be_nil
      expect(placement.media).to eq("roo_media_image9.png")

      expect(xlsx.image_placements("Empty")).to eq([])
    end

    it "marks cropped pictures" do
      placement = xlsx.image_placements("Quotes").find { |p| p.name == "Cropped" }

      expect(placement.cropped?).to be(true)
      expect(placement.src_rect).to eq("l" => "10000", "t" => "20000", "r" => "30000", "b" => "40000")
    end
  end

  describe "#visible_image_placements" do
    it "collapses hidden rows when attributing placements" do
      visible = xlsx.visible_image_placements("Quotes")
      merged = visible.find { |p| p.name == "Merged span" }

      expect(merged.row).to eq(3)
      expect(merged.to_row).to eq(4)
    end

    it "does not lend a cropped picture to a neighbouring row when its top-left cell is hidden" do
      visible = xlsx.visible_image_placements("Quotes")

      expect(visible.map(&:name)).not_to include("Cropped")
      expect(visible.map(&:name)).to eq(["Merged span", "External ref", "Missing media"])
    end

    it "returns placements of sheets without hidden rows unchanged" do
      visible = xlsx.visible_image_placements("Summary")

      expect(visible.map(&:name)).to eq(["Shared logo"])
      expect(visible.first.row).to eq(1)
      expect(visible.first.to_row).to eq(2)
    end

    it "returns an empty array for sheets without images" do
      expect(xlsx.visible_image_placements("Empty")).to eq([])
    end
  end

  describe "#images" do
    it "keeps the legacy behaviour" do
      names = xlsx.images("Quotes").map { |path| path && File.basename(path) }

      expect(names).to eq(["roo_media_image1.png", "roo_media_image2.png", nil, nil])
      expect(xlsx.images("Empty")).to eq([])
    end
  end
end
