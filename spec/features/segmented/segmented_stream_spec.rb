# frozen_string_literal: true

require 'stringio'

# A read-once IO that hands bytes out in ragged, caller-sized chunks and can be
# paused between deliveries. It only supports the surface SmarterCSV uses.
class RaggedChunkIO
  def initialize(bytes, chunk_sizes)
    @buffer = bytes.b
    @chunk_sizes = chunk_sizes.cycle
  end

  def gets(sep)
    return nil if @buffer.empty?

    index = @buffer.index(sep.b)
    if index
      line = @buffer.byteslice(0, index + sep.bytesize)
      @buffer = @buffer.byteslice((index + sep.bytesize)..) || ''.b
      line
    else
      line = @buffer
      @buffer = ''.b
      line
    end
  end

  def read(length)
    return nil if @buffer.empty?

    length = [length, @chunk_sizes.next].min
    chunk = @buffer.byteslice(0, length)
    @buffer = @buffer.byteslice(length..) || ''.b
    chunk
  end

  def eof?
    @buffer.empty?
  end
end

RSpec.describe SmarterCSV::SegmentedStream do
  let(:required) { [:id] }
  let(:rules) { %i[double_quotes backslash] }

  def single_pass(bytes, options = {})
    SmarterCSV.process(StringIO.new(bytes), options)
  end

  describe '.process' do
    context 'with an exact, marker-free join' do
      let(:bytes) { "id,name\n1,Alice\n2,Bob\n3,Carol\n" }

      it 'returns the same records as one sequential read' do
        cut = "id,name\n1,Alice\n".bytesize
        result = described_class.process(
          segments: [bytes.byteslice(0, cut), bytes.byteslice(cut..)],
          rules: rules, required_keys: required
        )

        expect(result.records).to eq single_pass(bytes)
        expect(result.ambiguous).to be_empty
      end

      it 'survives under every allowed rule when the data carries no escapes' do
        cut = "id,name\n1,Alice\n".bytesize
        result = described_class.process(
          segments: [bytes.byteslice(0, cut), bytes.byteslice(cut..)],
          rules: rules, required_keys: required
        )

        expect(result.surviving_interpretations.map { |i| i.rules }.uniq)
          .to contain_exactly([:double_quotes, :double_quotes],
                              [:double_quotes, :backslash],
                              [:backslash, :double_quotes],
                              [:backslash, :backslash])
      end

      it 'forwards regular reader options' do
        cut = "id,name\n1,Alice\n".bytesize
        result = described_class.process(
          segments: [bytes.byteslice(0, cut), bytes.byteslice(cut..)],
          rules: rules, required_keys: required,
          options: { convert_values_to_numeric: false }
        )

        expect(result.records.first[:id]).to eq '1'
      end
    end

    context 'when the escaping rule switches at the seam' do
      let(:segment_one) { "id,note\n\"he said \"\"hi\"\"\",x\n" }
      let(:segment_two) { "\"he said \\\"yo\\\"\",y\n" }

      it 'keeps only interpretations that parse both sources completely' do
        result = described_class.process(
          segments: [segment_one, segment_two],
          rules: rules, required_keys: [:id]
        )

        expect(result.success?).to be true
        expect(result.surviving_interpretations).not_to be_empty
        expect(result.surviving_interpretations.all? { |i| i.rules.last == :backslash }).to be true
        expect(result.interpretations.select(&:ok?).map { |i| i.rules.last }).not_to include(:double_quotes)
      end

      it 'commits the jointly determined records' do
        result = described_class.process(
          segments: [segment_one, segment_two],
          rules: rules, required_keys: [:id]
        )

        expect(result.records).to contain_exactly(
          { id: 'he said "hi"', note: 'x' },
          { id: 'he said \"yo\"', note: 'y' }
        )
      end

      it 'records the rejected interpretations with their error rather than raising' do
        result = described_class.process(
          segments: [segment_one, segment_two],
          rules: rules, required_keys: [:id]
        )

        rejected = result.interpretations.reject(&:ok?)
        expect(rejected).not_to be_empty
        expect(rejected.map { |i| i.error.class }.uniq).to include(SmarterCSV::MalformedCSV)
      end
    end

    context 'with a finite seam window and decoy boundaries' do
      let(:header) { "id,note\n" }
      let(:multiline_row) { "1,\"line one\nSEGMENT_TWO_MARKER\n﻿id,note\nline two\"\n" }
      let(:tail) { "2,plain\n" }
      let(:bytes) { header + multiline_row + tail }
      let(:true_cut) { (header + multiline_row).bytesize }

      it 'only admits cuts proven on the raw bytes, ignoring in-field line starts and BOM homoglyphs' do
        result = described_class.process(
          segments: [bytes.byteslice(0, true_cut), bytes.byteslice(true_cut..)],
          rules: rules, required_keys: required,
          seam: { first: ((true_cut - 40)..true_cut), second: (0..0) }
        )

        expect(result.surviving_interpretations.map { |i| i.seam.first.first }.uniq).to eq [true_cut]
        expect(result.records).to eq single_pass(bytes)
      end

      it 'withholds records when the window contains two genuine boundaries yielding different complete reads' do
        # Two byte-proven record boundaries lie in the window: end of header and
        # end of the multiline row. Both parse cleanly but keep different rows,
        # so nothing is jointly determined.
        result = described_class.process(
          segments: [bytes.byteslice(0, true_cut), bytes.byteslice(true_cut..)],
          rules: rules, required_keys: required,
          seam: { first: (header.bytesize..true_cut), second: (0..0) }
        )

        surviving = result.surviving_interpretations
        expect(surviving.map { |i| i.seam.first.first }.uniq).to include(header.bytesize, true_cut)
        expect(result.records).to be_empty
        expect(result.ambiguous).not_to be_empty
      end

      it 'rejects seam offsets outside a proven boundary when a context split is required' do
        # repeated_headers forces two independent readers, so every cut must be a
        # byte-proven record boundary; the window below contains none.
        result = described_class.process(
          segments: [bytes.byteslice(0, true_cut), bytes.byteslice(true_cut..)],
          rules: rules, required_keys: required, repeated_headers: true,
          seam: { first: (0...header.bytesize), second: (0..0) }
        )

        expect(result.success?).to be false
      end

      it 'preserves a BOM homoglyph at the start of segment two as field content' do
        segment_one = "id,note\n1,\"open line\n"
        segment_two = "\xEF\xBB\xBFcontinued text\"\n2,plain\n"

        result = described_class.process(
          segments: [segment_one, segment_two],
          rules: rules, required_keys: required,
          seam: { first: (segment_one.bytesize..segment_one.bytesize), second: (0..3) }
        )

        second_cuts = result.surviving_interpretations.map { |i| i.seam[1][0] }.uniq
        expect(second_cuts).to eq [0]
        expect(result.records).to eq single_pass(segment_one + segment_two)
        expect(result.records.first[:note]).to include("﻿")
      end

      it 'never considers cuts beyond the declared window' do
        result = described_class.process(
          segments: [bytes.byteslice(0, true_cut), bytes.byteslice(true_cut..)],
          rules: rules, required_keys: required,
          seam: { first: ((true_cut - 40)...true_cut), second: (0..0) }
        )

        first_cuts = result.interpretations.map { |i| i.seam.first.first }
        expect(first_cuts).to all(be < true_cut)
        expect(first_cuts).to all(be >= true_cut - 40)
      end

      it 'keeps multibyte UTF-8 fields intact across the seam' do
        segment_one = "id,name\n1,\"München\n".b.force_encoding('UTF-8')
        segment_two = "😀\"\n2,Zürich\n".b.force_encoding('UTF-8')

        result = described_class.process(
          segments: [segment_one, segment_two],
          rules: rules, required_keys: required,
          seam: { first: (segment_one.bytesize..segment_one.bytesize), second: (0..0) }
        )

        expect(result.records).to eq single_pass(segment_one + segment_two)
        expect(result.records.first[:name]).to eq "München\n😀"
        expect(result.records.first[:name].encoding).to eq Encoding::UTF_8
      end
    end

    context 'when delivery pauses inside a record' do
      let(:bytes) { "id,note\n1,\"multi\nline\"\n2,ok\n" }

      it 'stitches the multiline record and matches a single sequential read' do
        cut = "id,note\n1,\"multi\n".bytesize
        result = described_class.process(
          segments: [bytes.byteslice(0, cut), bytes.byteslice(cut..)],
          rules: [:double_quotes], required_keys: required,
          seam: { first: (cut..cut), second: (0..0) }
        )

        expect(result.records).to eq single_pass(bytes, quote_escaping: :double_quotes)
      end

      it 'produces identical output when the same bytes arrive in ragged read-once chunks' do
        cut = "id,note\n1,\"multi\n".bytesize
        result = described_class.process(
          segments: [bytes.byteslice(0, cut), bytes.byteslice(cut..)],
          rules: [:double_quotes], required_keys: required,
          seam: { first: (cut..cut), second: (0..0) }
        )
        streamed = SmarterCSV.process(RaggedChunkIO.new(bytes, [1, 3, 7, 2, 13]),
                                      quote_escaping: :double_quotes)

        expect(streamed).to eq result.records
      end
    end

    context 'when interpretations disagree' do
      it 'withholds the divergent row and reports every variant' do
        # "a\",b",c : RFC 4180 and backslash escaping split the fields differently.
        segment_two = "\"a\\\",b\",c\n"
        result = described_class.process(
          segments: ["id,note\n", segment_two],
          rules: rules, required_keys: [:id]
        )

        expect(result.records).to be_empty
        expect(result.ambiguous.size).to eq 1
        expect(result.ambiguous.first.row_index).to eq 0
        distinct = result.ambiguous.first.variants.map { |variant| variant[:record] }.uniq
        expect(distinct.size).to eq 2
      end

      it 'still commits the rows all surviving interpretations agree on' do
        segment_two = "\"a\\\",b\",c\n"
        result = described_class.process(
          segments: ["id,note\n0,stable\n", segment_two],
          rules: rules, required_keys: [:id]
        )

        expect(result.records).to eq [{ id: 0, note: 'stable' }]
        expect(result.ambiguous.map(&:row_index)).to eq [1]
      end
    end

    context 'required columns' do
      it 'rejects interpretations whose rows lack a required key' do
        result = described_class.process(
          segments: ["id,note\n", "1,\n"],
          rules: [:double_quotes], required_keys: [:note]
        )

        expect(result.success?).to be false
        expect(result.surviving_interpretations).to be_empty
      end

      it 'rejects interpretations whose header omits a required key' do
        result = described_class.process(
          segments: ["id\n", "1\n"],
          rules: [:double_quotes], required_keys: [:note]
        )

        expect(result.success?).to be false
        expect(result.surviving_interpretations).to be_empty
        expect(result.errors.map(&:class)).to include(SmarterCSV::MissingKeys)
      end
    end

    context 'with a repeated header in the second source' do
      it 'skips the repeated header row' do
        result = described_class.process(
          segments: ["id,name\n1,Alice\n", "id,name\n2,Bob\n"],
          rules: [:double_quotes], required_keys: [:id],
          repeated_headers: true
        )

        expect(result.records).to eq [{ id: 1, name: 'Alice' }, { id: 2, name: 'Bob' }]
      end

      it 'treats the repeated header as data when not declared' do
        result = described_class.process(
          segments: ["id,name\n1,Alice\n", "id,name\n2,Bob\n"],
          rules: [:double_quotes], required_keys: [:id]
        )

        expect(result.records.first).to eq({ id: 1, name: 'Alice' })
        expect(result.records.any? { |row| row[:id] == 'id' }).to be true
      end
    end

    context 'without any complete interpretation' do
      let(:segments) { ["id\n", "\"never closed\n"] }

      it 'returns a result with no records and the collected errors' do
        result = described_class.process(segments: segments, rules: rules, required_keys: [:id])

        expect(result.success?).to be false
        expect(result.records).to be_empty
        expect(result.errors).not_to be_empty
      end

      it '.process! raises NoConsensusError' do
        expect do
          described_class.process!(segments: segments, rules: rules, required_keys: [:id])
        end.to raise_error(described_class::NoConsensusError)
      end
    end

    context 'argument validation' do
      it 'requires exactly two segments' do
        expect do
          described_class.process(segments: ["id\n"], rules: rules, required_keys: [:id])
        end.to raise_error(described_class::InvalidSeamError)
      end

      it 'rejects unknown escaping rules' do
        expect do
          described_class.process(segments: ["id\n", "1\n"], rules: [:nope], required_keys: [:id])
        end.to raise_error(described_class::InvalidSeamError)
      end

      it 'rejects seam ranges outside the segment sizes' do
        expect do
          described_class.process(
            segments: ["id\n", "1\n"], rules: rules, required_keys: [:id],
            seam: { first: (0..99), second: (0..0) }
          )
        end.to raise_error(described_class::InvalidSeamError)
      end

      it 'rejects malformed seam declarations' do
        expect do
          described_class.process(
            segments: ["id\n", "1\n"], rules: rules, required_keys: [:id],
            seam: { first: 4, second: 0 }
          )
        end.to raise_error(described_class::InvalidSeamError)
      end

      it 'caps the number of candidate seam pairs' do
        stub_const("#{described_class}::MAX_CANDIDATE_PAIRS", 2)
        expect do
          described_class.process(
            segments: ["id\n12\n", "34\n"], rules: rules, required_keys: [:id],
            seam: { first: (0..6), second: (0..3) }
          )
        end.to raise_error(described_class::InvalidSeamError, /exceeding the limit/)
      end
    end

    it 'exposes seam and rule metadata on every interpretation' do
      bytes = "id,name\n1,Alice\n2,Bob\n"
      cut = "id,name\n1,Alice\n".bytesize
      result = described_class.process(
        segments: [bytes.byteslice(0, cut), bytes.byteslice(cut..)],
        rules: rules, required_keys: required
      )

      result.interpretations.each do |interpretation|
        expect(interpretation.seam).to be_an(Array)
        expect(interpretation.seam.first).to contain_exactly(an_instance_of(Integer), an_instance_of(Symbol))
        expect(interpretation.rules.size).to eq 2
        expect(%i[ok error rejected]).to include(interpretation.status)
      end
    end

    it 'yields each committed record to the block' do
      bytes = "id,name\n1,Alice\n2,Bob\n"
      cut = "id,name\n1,Alice\n".bytesize
      yielded = []
      result = described_class.process(
        segments: [bytes.byteslice(0, cut), bytes.byteslice(cut..)],
        rules: rules, required_keys: required
      ) { |record| yielded << record }

      expect(yielded).to eq result.records
    end

    it 'accepts :auto as an allowed rule' do
      bytes = "id,name\n1,Alice\n"
      result = described_class.process(
        segments: [bytes, String.new],
        rules: [:auto], required_keys: required
      )

      expect(result.success?).to be true
      expect(result.records).to eq single_pass(bytes)
    end
  end
end
