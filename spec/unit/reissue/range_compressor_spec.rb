# frozen_string_literal: true

RSpec.describe Dry::Transformer::Reissue::RangeCompressor do
  describe ".compress" do
    it "returns an empty array for no numbers" do
      expect(described_class.compress([])).to eq([])
    end

    it "compresses contiguous numbers into ranges" do
      expect(described_class.compress([3, 1, 2, 4])).to eq([1..4])
    end

    it "keeps equal-step runs as arithmetic sequences" do
      expect(described_class.compress([7, 9, 11])).to eq([(7..11).step(2)])
    end

    it "prefers contiguous runs over wider arithmetic runs" do
      ranges = described_class.compress([1, 2, 3, 7, 9, 11, 20])

      expect(ranges).to eq([1..3, (7..11).step(2), 20..20])
      expect(described_class.expand(ranges)).to eq([1, 2, 3, 7, 9, 11, 20])
    end

    it "deduplicates and round-trips through expand" do
      numbers = [2, 4, 4, 6, 10, 11, 12, 30]
      ranges = described_class.compress(numbers)

      expect(described_class.expand(ranges)).to eq([2, 4, 6, 10, 11, 12, 30])
    end

    it "understands step membership without confusing plain range cover" do
      sequence = (7..11).step(2)

      expect(described_class.member?(sequence, 9)).to be(true)
      expect(described_class.member?(sequence, 8)).to be(false)
      expect(described_class.member?(1..3, 2)).to be(true)
    end
  end
end
