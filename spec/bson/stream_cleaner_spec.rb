# frozen_string_literal: true
# rubocop:todo all

require 'spec_helper'

describe BSON::StreamCleaner do
  # IO wrapper that returns at most chunk_size bytes per read call, to
  # prove that cleaning does not depend on how the input is chunked.
  class ChunkedIO
    def initialize(data, chunk_size)
      @data = data
      @chunk_size = chunk_size
      @pos = 0
    end

    def read(length)
      return nil if @pos >= @data.bytesize

      chunk = @data.byteslice(@pos, [ length, @chunk_size ].min)
      @pos += chunk.bytesize
      chunk
    end
  end

  let(:float32) { BSON::Binary::VECTOR_DATA_TYPES[:float32] }
  let(:int8) { BSON::Binary::VECTOR_DATA_TYPES[:int8] }
  let(:packed_bit) { BSON::Binary::VECTOR_DATA_TYPES[:packed_bit] }

  def binary_element(name, payload)
    "\x05#{name}\x00".b + [ payload.bytesize ].pack('l<') + "\x09".b + payload
  end

  def vector_element(name, dtype, padding, data)
    binary_element(name, [ dtype, padding ].pack('CC') + data)
  end

  def document_element(name, content, type = 0x03)
    type.chr + "#{name}\x00".b + [ content.bytesize + 5 ].pack('l<') + content + "\x00".b
  end

  def string_element(name, value)
    "\x02#{name}\x00".b + [ value.bytesize + 1 ].pack('l<') + value + "\x00".b
  end

  def int32_element(name, value)
    "\x10#{name}\x00".b + [ value ].pack('l<')
  end

  def frame(*elements)
    content = elements.join
    [ content.bytesize + 5 ].pack('l<') + content + "\x00".b
  end

  def station_frame(*station_contents)
    stations = station_contents.each_with_index.map do |content, index|
      document_element(index.to_s, content)
    end.join
    frame(document_element('stations', stations, 0x04))
  end

  def samples_document(*elements)
    document_element('samples', elements.join)
  end

  def clean(data, chunk_size: nil)
    input = chunk_size ? ChunkedIO.new(data, chunk_size) : StringIO.new(data)
    output = StringIO.new(+''.b)
    errors = described_class.clean(input, output)
    [ output.string, errors ]
  end

  def parse_frames(data)
    buffer = BSON::ByteBuffer.new(data)
    frames = []
    frames << BSON::Document.from_bson(buffer) while buffer.length.positive?
    frames
  end

  let(:temperatures) do
    (0..9).map { |i| [ i.to_f ].pack('e') }.join.tap do |packed|
      packed[4, 4] = "\x00\x00\x00\x80".b
    end
  end
  let(:codes) { (-5..4).to_a.pack('c*') }

  # The example from the collector documentation: ten samples, an
  # archival keep (C0 3F) before the channels, archival fields and the
  # effective keep (51 BF) after them, both padded by six bits.
  let(:archived_samples) do
    vector_element('keep', packed_bit, 6, "\xC0\x3F".b) +
      vector_element('temperature', float32, 0, temperatures) +
      vector_element('code', int8, 0, codes) +
      string_element('note', 'archive') +
      vector_element('keep', packed_bit, 6, "\x51\xBF".b)
  end

  let(:frame1) { station_frame(int32_element('id', 1) + samples_document(archived_samples)) }

  let(:valid_samples) do
    vector_element('temperature', float32, 0, [ 1.0, 2.0, 3.0 ].pack('e*')) +
      vector_element('code', int8, 0, [ 1, 2, 3 ].pack('c*')) +
      vector_element('keep', packed_bit, 5, "\xE0".b)
  end

  # Same samples as frame1 but the effective keep declares padding 7,
  # which makes its sample count disagree with the channels.
  let(:bad_samples) do
    vector_element('keep', packed_bit, 6, "\xC0\x3F".b) +
      vector_element('temperature', float32, 0, temperatures) +
      vector_element('code', int8, 0, codes) +
      vector_element('keep', packed_bit, 7, "\x51\xBF".b)
  end

  let(:frame2) { station_frame(samples_document(valid_samples), samples_document(bad_samples)) }

  let(:zeroed_samples) do
    vector_element('temperature', float32, 0, [ 5.0, 6.0, 7.0, 8.0 ].pack('e*')) +
      vector_element('code', int8, 0, [ 4, 5, 6, 7 ].pack('c*')) +
      vector_element('keep', packed_bit, 4, "\x00".b)
  end

  let(:frame3) { station_frame(samples_document(zeroed_samples)) }

  let(:stream) { frame1 + frame2 + frame3 }

  shared_examples 'a cleaned three-frame stream' do
    it 'drops the invalid frame and reports it' do
      expect(errors.length).to eq(1)
      error = errors.first
      expect(error.offset).to eq(frame1.bytesize)
      expect(error.station_index).to eq(1)
      expect(error.field).to eq('keep')
      expect(error.reason).to eq(:count_mismatch)
    end

    it 'writes only the valid frames' do
      expect(frames.length).to eq(2)
    end

    it 'filters the code channel by the mask' do
      expect(frames[0]['stations'][0]['samples']['code'].as_vector.to_a).to eq([ -4, -2, 2, 3 ])
    end

    it 'copies the kept temperature groups verbatim' do
      data = frames[0]['stations'][0]['samples']['temperature'].data
      expect(data.byteslice(2, 4)).to eq("\x00\x00\x00\x80".b)
      expect(data.byteslice(2, 16)).to eq("\x00\x00\x00\x80".b + [ 3.0, 7.0, 8.0 ].pack('e*'))
    end

    it 'rewrites the effective keep as an all-ones mask' do
      expect(frames[0]['stations'][0]['samples']['keep'].data).to eq([ packed_bit, 4, 0xF0 ].pack('CCC'))
    end

    it 'preserves the archival keep and other fields in place' do
      archival = binary_element('keep', [ packed_bit, 6, 0xC0, 0x3F ].pack('CCCC'))
      temperature = frames[0]['stations'][0]['samples']['temperature']
      expect(output).to include(archival)
      expect(output.index(archival)).to be < output.index(temperature.data.byteslice(2, 16))
      expect(frames[0]['stations'][0]['samples']['note']).to eq('archive')
      expect(frames[0]['stations'][0]['id']).to eq(1)
    end

    it 'empties all three vectors when the mask keeps nothing' do
      samples = frames[1]['stations'][0]['samples']
      expect(samples['temperature'].data).to eq([ float32, 0 ].pack('CC'))
      expect(samples['code'].data).to eq([ int8, 0 ].pack('CC'))
      expect(samples['keep'].data).to eq([ packed_bit, 0 ].pack('CC'))
    end
  end

  context 'when frames arrive in a single read' do
    let(:output_and_errors) { clean(stream) }
    let(:output) { output_and_errors.first }
    let(:errors) { output_and_errors.last }
    let(:frames) { parse_frames(output) }

    it_behaves_like 'a cleaned three-frame stream'
  end

  context 'when frame boundaries fall inside input chunks' do
    [ 1, 7, 64, 137, 4096 ].each do |chunk_size|
      context "with #{chunk_size}-byte reads" do
        let(:output_and_errors) { clean(stream, chunk_size: chunk_size) }
        let(:output) { output_and_errors.first }
        let(:errors) { output_and_errors.last }
        let(:frames) { parse_frames(output) }

        it_behaves_like 'a cleaned three-frame stream'
      end
    end
  end

  context 'when the input is empty' do
    it 'produces no output and no errors' do
      output, errors = clean('')
      expect(output).to eq('')
      expect(errors).to be_empty
    end
  end

  context 'when the stream ends mid-frame' do
    it 'reports truncation and keeps previously written frames readable' do
      output, errors = clean(frame1 + frame3.byteslice(0, 10))
      expect(errors.length).to eq(1)
      expect(errors.first.reason).to eq(:truncated)
      expect(errors.first.offset).to eq(frame1.bytesize)
      expect(errors.first).to be_truncated
      expect(parse_frames(output).length).to eq(1)
    end
  end

  context 'when the stream ends inside a frame header' do
    it 'reports truncation' do
      _output, errors = clean(frame1 + "\x20\x00".b)
      expect(errors.last.reason).to eq(:truncated)
    end
  end

  context 'when a frame length is out of range' do
    it 'reports the frame and stops' do
      output, errors = clean([ 4 ].pack('l<') + frame1)
      expect(errors.length).to eq(1)
      expect(errors.first.reason).to eq(:invalid_frame_length)
      expect(errors.first.offset).to eq(0)
      expect(output).to eq('')
    end

    it 'rejects frames larger than 16 MiB' do
      _output, errors = clean([ 16 * 1024 * 1024 + 1 ].pack('l<'))
      expect(errors.first.reason).to eq(:invalid_frame_length)
    end
  end

  context 'when a record has no stations field' do
    it 'passes the frame through byte-identical' do
      record = frame(string_element('kind', 'meta'), int32_element('seq', 7))
      output, errors = clean(record)
      expect(errors).to be_empty
      expect(output).to eq(record)
    end
  end

  context 'when a required vector is missing' do
    let(:samples) do
      vector_element('code', int8, 0, "\x01".b) +
        vector_element('keep', packed_bit, 7, "\x80".b)
    end

    it 'reports the missing field' do
      _output, errors = clean(station_frame(samples_document(samples)))
      expect(errors.length).to eq(1)
      expect(errors.first.field).to eq('temperature')
      expect(errors.first.reason).to eq(:missing_field)
      expect(errors.first.station_index).to eq(0)
    end
  end

  context 'when a vector has the wrong dtype' do
    let(:samples) do
      vector_element('temperature', int8, 0, "\x01".b) +
        vector_element('code', int8, 0, "\x01".b) +
        vector_element('keep', packed_bit, 7, "\x80".b)
    end

    it 'reports an invalid vector' do
      _output, errors = clean(station_frame(samples_document(samples)))
      expect(errors.first.field).to eq('temperature')
      expect(errors.first.reason).to eq(:invalid_vector)
    end
  end

  context 'when a field is not a binary vector' do
    let(:samples) do
      vector_element('temperature', float32, 0, [ 1.0 ].pack('e')) +
        vector_element('code', int8, 0, "\x01".b) +
        string_element('keep', 'not-a-vector')
    end

    it 'reports an invalid type' do
      _output, errors = clean(station_frame(samples_document(samples)))
      expect(errors.first.field).to eq('keep')
      expect(errors.first.reason).to eq(:invalid_type)
    end
  end

  context 'when an archival keep is malformed but the effective one is valid' do
    let(:samples) do
      vector_element('keep', packed_bit, 7, "\xFF".b) +
        vector_element('temperature', float32, 0, [ 1.0 ].pack('e')) +
        vector_element('code', int8, 0, "\x2A".b) +
        vector_element('keep', packed_bit, 7, "\x80".b)
    end

    it 'cleans using the last keep only' do
      output, errors = clean(station_frame(samples_document(samples)))
      expect(errors).to be_empty
      samples_doc = parse_frames(output)[0]['stations'][0]['samples']
      expect(samples_doc['code'].as_vector.to_a).to eq([ 42 ])
      expect(output).to include(binary_element('keep', [ packed_bit, 7, 0xFF ].pack('CCC')))
    end
  end

  context 'when the effective keep precedes the channels' do
    let(:samples) do
      vector_element('keep', packed_bit, 5, "\xA8".b) +
        vector_element('temperature', float32, 0, [ 1.0, 2.0, 3.0 ].pack('e*')) +
        vector_element('code', int8, 0, [ 10, 20, 30 ].pack('c*'))
    end

    it 'still filters both channels' do
      output, errors = clean(station_frame(samples_document(samples)))
      expect(errors).to be_empty
      samples_doc = parse_frames(output)[0]['stations'][0]['samples']
      expect(samples_doc['code'].as_vector.to_a).to eq([ 10, 30 ])
      expect(samples_doc['temperature'].as_vector.to_a).to eq([ 1.0, 3.0 ])
      expect(samples_doc['keep'].data).to eq([ packed_bit, 6, 0xC0 ].pack('CCC'))
    end
  end

  context 'with a frame larger than the in-memory budget' do
    let(:sample_count) { 600_000 }
    let(:kept_indexes) { (0...sample_count).select { |i| (i % 3).zero? } }
    let(:mask) do
      bits = (0...sample_count).map { |i| (i % 3).zero? ? '1' : '0' }.join
      [ bits ].pack('B*')
    end
    let(:big_samples) do
      vector_element('temperature', float32, 0, (0...sample_count).map { |i| [ i.to_f ].pack('e') }.join) +
        vector_element('code', int8, 0, (0...sample_count).map { |i| i % 128 }.pack('c*')) +
        vector_element('keep', packed_bit, 0, mask)
    end
    let(:big_frame) { station_frame(samples_document(big_samples)) }

    it 'cleans via temporary files and stays readable' do
      output, errors = clean(big_frame + frame3, chunk_size: 65_537)
      expect(errors).to be_empty
      frames = parse_frames(output)
      expect(frames.length).to eq(2)
      samples_doc = frames[0]['stations'][0]['samples']
      expect(samples_doc['code'].as_vector.to_a).to eq(kept_indexes.map { |i| i % 128 })
      expect(samples_doc['temperature'].data.bytesize).to eq(2 + kept_indexes.length * 4)
      expect(samples_doc['keep'].data).to eq([ packed_bit, 0 ].pack('CC') + "\xFF".b * (kept_indexes.length / 8))
    end
  end

  it 'yields errors to a block as they happen' do
    yielded = []
    described_class.clean(StringIO.new(stream), StringIO.new(+''.b)) { |error| yielded << error }
    expect(yielded.length).to eq(1)
    expect(yielded.first.field).to eq('keep')
  end
end
