require "spec_helper"

describe ChunkyPNG::Canvas::PartialPNGDecoding do
  include ChunkyPNG::Canvas::PartialPNGDecoding

  let(:opaque_colors) do
    [
      ChunkyPNG::Color.rgb(10, 20, 30),
      ChunkyPNG::Color.rgb(40, 50, 60),
      ChunkyPNG::Color.rgb(70, 80, 90),
      ChunkyPNG::Color.rgb(100, 110, 120),
    ]
  end

  let(:transparent_colors) do
    [ChunkyPNG::Color.rgba(0, 0, 0, 0), ChunkyPNG::Color.rgb(5, 6, 7)]
  end

  def decoding_palette(colors)
    palette = encoding_palette(colors)
    ChunkyPNG::Palette.from_chunks(palette.to_plte_chunk, palette.opaque? ? nil : palette.to_trns_chunk)
  end

  def encoding_palette(colors)
    palette = ChunkyPNG::Palette.new(colors, colors)
    palette.to_plte_chunk
    palette
  end

  def encode_stream(canvas, palette, depth, interlace, filter)
    canvas.encoding_palette = palette
    canvas.send(:encode_png_pixelstream, ChunkyPNG::COLOR_INDEXED, depth, interlace, filter)
  end

  def decode_partial(records, width, height, depth, interlace, palette, rect, confirmed = [])
    ChunkyPNG::Canvas.decode_partial_pixelstream(
      records, width, height, ChunkyPNG::COLOR_INDEXED, depth, interlace, palette, rect, confirmed: confirmed
    )
  end

  def full_record(stream, id = "full")
    transfer_record(id, [{start_pos: 0, bytes: stream}])
  end

  def transfer_record(id, fragments, group: nil)
    ChunkyPNG::Canvas::PartialPNGDecoding::TransferRecord.new(id, group: group, fragments: fragments)
  end

  def fragment(stream, start_pos, length = 1)
    {start_pos: start_pos, bytes: stream.byteslice(start_pos, length)}
  end

  [1, 2, 4, 8].each do |depth|
    count = {1 => 2, 2 => 4, 4 => 16, 8 => 16}[depth]
    context "with #{depth}-bit indexed images" do
      let(:colors) { Array.new(count) { |i| ChunkyPNG::Color.rgba(i * 4, i * 8, i * 16, i.zero? ? 0 : 255) } }

      [ChunkyPNG::INTERLACING_NONE, ChunkyPNG::INTERLACING_ADAM7].each do |interlace|
        [ChunkyPNG::FILTER_NONE, ChunkyPNG::FILTER_SUB, ChunkyPNG::FILTER_UP,
         ChunkyPNG::FILTER_AVERAGE, ChunkyPNG::FILTER_PAETH].each do |filter|
          it "reconstructs every pixel from a complete fragment (interlace=#{interlace}, filter=#{filter})" do
            palette = decoding_palette(colors)
            canvas = ChunkyPNG::Canvas.new(9, 7, Array.new(63) { colors.sample })
            stream = encode_stream(canvas, encoding_palette(colors), depth, interlace, filter)

            result = decode_partial(
              [full_record(stream)], 9, 7, depth, interlace, palette, [0, 0, 8, 6]
            )

            expect(result.legal?).to be true
            expect(result.proven_pixels.length).to eql 63
            7.times do |y|
              9.times do |x|
                expect(result.color_at(x, y)).to eql canvas[x, y]
              end
            end
          end
        end
      end
    end
  end

  describe "sparse fragments" do
    let(:depth) { 8 }
    let(:palette) { decoding_palette(opaque_colors) }
    let(:canvas) { ChunkyPNG::Canvas.new(6, 4, Array.new(24) { opaque_colors.sample }) }
    let(:stream) do
  encode_stream(canvas, encoding_palette(opaque_colors), depth, ChunkyPNG::INTERLACING_NONE, ChunkyPNG::FILTER_UP)
end

    it "leaves unobserved pixels pending and keeps their candidates sound" do
      line = ChunkyPNG::Color.scanline_bytesize(ChunkyPNG::COLOR_INDEXED, depth, 6) + 1
      record = ChunkyPNG::Canvas::PartialPNGDecoding::TransferRecord.new(
        "partial", fragments: [{start_pos: 0, bytes: stream.byteslice(0, line)}]
      )
      result = decode_partial([record], 6, 4, depth, ChunkyPNG::INTERLACING_NONE, palette, [0, 0, 5, 3])

      expect(result.unknown?(0, 1)).to be true
      (0...4).each do |y|
        (0...6).each do |x|
          expect(result.candidates_at(x, y)).to include(canvas[x, y])
        end
      end
    end

    it "combines several separated fragments of one record" do
      line = ChunkyPNG::Color.scanline_bytesize(ChunkyPNG::COLOR_INDEXED, depth, 6) + 1
      fragments = [
        {start_pos: 0, bytes: stream.byteslice(0, line)},
        {start_pos: line, bytes: stream.byteslice(line, line)},
      ]
      record = ChunkyPNG::Canvas::PartialPNGDecoding::TransferRecord.new("split", fragments: fragments)
      result = decode_partial([record], 6, 4, depth, ChunkyPNG::INTERLACING_NONE, palette, [0, 1, 5, 1])

      6.times { |x| expect(result.color_at(x, 1)).to eql canvas[x, 1] }
    end

    it "lets a fragment outside the rectangle constrain the row below through up filtering" do
      line = ChunkyPNG::Color.scanline_bytesize(ChunkyPNG::COLOR_INDEXED, depth, 6) + 1
      fragments = [
        {start_pos: 0, bytes: stream.byteslice(0, line)},
        {start_pos: line, bytes: stream.byteslice(line, line)},
      ]
      record = ChunkyPNG::Canvas::PartialPNGDecoding::TransferRecord.new("rows", fragments: fragments)
      result = decode_partial([record], 6, 4, depth, ChunkyPNG::INTERLACING_NONE, palette, [0, 1, 5, 1])

      6.times { |x| expect(result.color_at(x, 1)).to eql canvas[x, 1] }
    end
  end

  describe "alternative transfer records" do
    let(:depth) { 8 }
    let(:palette) { decoding_palette(opaque_colors) }
    let(:canvas) { ChunkyPNG::Canvas.new(5, 2, Array.new(10) { opaque_colors.sample }) }
    let(:stream) do
  encode_stream(canvas, encoding_palette(opaque_colors), depth, ChunkyPNG::INTERLACING_NONE, ChunkyPNG::FILTER_NONE)
end
    let(:line) { ChunkyPNG::Color.scanline_bytesize(ChunkyPNG::COLOR_INDEXED, depth, 5) + 1 }
    let(:none) { ChunkyPNG::INTERLACING_NONE }

    it "picks whole version records and intersects the agreeing bytes" do
      bad = stream.dup
      bad.setbyte(line + 1, (bad.getbyte(line + 1) + 1) % opaque_colors.length)
      records = [
        transfer_record("a", [fragment(stream, 0, line)], group: "read"),
        transfer_record("b", [fragment(bad, 0, line)], group: "read"),
      ]
      result = decode_partial(records, 5, 2, depth, none, palette, [0, 0, 4, 1])

      5.times { |x| expect(result.color_at(x, 0)).to eql canvas[x, 0] }
      expect(result.unknown?(0, 1)).to be true
    end

    it "eliminates a version record that conflicts on an overlap" do
      bad = stream.dup
      bad.setbyte(line + 1, (bad.getbyte(line + 1) + 1) % opaque_colors.length)
      records = [
        transfer_record(
          "a",
          [fragment(stream, 0, line), fragment(stream, line), fragment(stream, line + 1)],
          group: "read",
        ),
        transfer_record("b", [fragment(bad, 0, line), fragment(bad, line), fragment(bad, line + 1)], group: "read"),
        transfer_record("anchor", [fragment(stream, line + 1)]),
      ]
      result = decode_partial(records, 5, 2, depth, none, palette, [0, 0, 4, 1])

      expect(result.legal?).to be true
      expect(result.color_at(0, 1)).to eql canvas[0, 1]
    end

    it "reports a contradiction when no version survives" do
      worse = ((stream.getbyte(line + 1) + 2) % opaque_colors.length).chr
      other = ((stream.getbyte(line + 1) + 1) % opaque_colors.length).chr
      records = [
        transfer_record("a", [{start_pos: line + 1, bytes: stream.byteslice(line + 1, 1)}], group: "read"),
        transfer_record("b", [{start_pos: line + 1, bytes: other}], group: "read"),
        transfer_record("anchor", [{start_pos: line + 1, bytes: worse}]),
      ]
      result = decode_partial(records, 5, 2, depth, none, palette, [0, 0, 4, 1])

      expect(result.legal?).to be false
      expect(result.contradictions).not_to be_empty
    end

    it "unfilters every complete record separately instead of mixing readings" do
      # UP filtering, two versions: row 0 col 0 is index 1 vs 2, both carry
      # filtered delta 1 in row 1, yielding index 2 vs 3 there.
      stream_a = [ChunkyPNG::FILTER_UP, 1, 0, 0, 0, ChunkyPNG::FILTER_UP, 1, 0, 0, 0].pack("C*")
      stream_b = [ChunkyPNG::FILTER_UP, 2, 0, 0, 0, ChunkyPNG::FILTER_UP, 1, 0, 0, 0].pack("C*")
      positions = [0, 1, 2, 5, 6, 7]
      build = lambda do |id, partial_stream|
        transfer_record(id, positions.map { |pos| fragment(partial_stream, pos) }, group: "read")
      end
      result = decode_partial(
        [build.call("a", stream_a), build.call("b", stream_b)],
        4, 2, depth, none, palette, [0, 0, 3, 1]
      )

      expect(result.unknown?(0, 1)).to be true
      expect(result.candidates_at(0, 1)).to contain_exactly(opaque_colors[2], opaque_colors[3])
      expect(result.color_at(1, 1)).to eql opaque_colors[0]
    end

    it "isolates an illegal assembly while keeping the legal one authoritative" do
      valid = [ChunkyPNG::FILTER_NONE, 1, 2, 3].pack("C*")
      invalid = [9, 1, 2, 3].pack("C*")
      records = [
        transfer_record("a", [fragment(valid, 0, 4)], group: "read"),
        transfer_record("b", [fragment(invalid, 0, 4)], group: "read"),
      ]
      result = decode_partial(records, 3, 1, depth, ChunkyPNG::INTERLACING_NONE, palette, [0, 0, 2, 0])

      expect(result.legal?).to be true
      expect(result.assembly_contradictions.first.message).to include("Illegal scanline filter")
      expect(result.color_at(0, 0)).to eql opaque_colors[1]
      expect(result.color_at(2, 0)).to eql opaque_colors[3]
    end
  end

  describe "confirmed fragments" do
    let(:depth) { 8 }
    let(:palette) { decoding_palette(opaque_colors) }
    let(:canvas) { ChunkyPNG::Canvas.new(4, 2, Array.new(8) { opaque_colors.sample }) }
    let(:stream) do
  encode_stream(canvas, encoding_palette(opaque_colors), depth, ChunkyPNG::INTERLACING_NONE, ChunkyPNG::FILTER_NONE)
end

    it "holds in every assembly and reports an overlapping conflict" do
      conflicting = (stream.getbyte(1) + 1) % opaque_colors.length
      confirmed = [{start_pos: 1, bytes: conflicting.chr}]
      result = decode_partial(
  [full_record(stream)], 4, 2, depth, ChunkyPNG::INTERLACING_NONE, palette, [0, 0, 3, 1], confirmed
)

      expect(result.legal?).to be false
      expect(result.contradictions.first.message).to include("Conflicting observed byte")
    end
  end

  describe "palette handling" do
    it "unifies two palette indices that map to the same RGBA color" do
      colors = [ChunkyPNG::Color.rgb(7, 7, 7), ChunkyPNG::Color.rgb(7, 7, 7), ChunkyPNG::Color.rgb(9, 9, 9)]
      palette = decoding_palette(colors)
      canvas = ChunkyPNG::Canvas.new(4, 1, [colors[0], colors[1], colors[2], colors[0]])
      stream = encode_stream(canvas, encoding_palette(colors), 2, ChunkyPNG::INTERLACING_NONE, ChunkyPNG::FILTER_NONE)
      record = ChunkyPNG::Canvas::PartialPNGDecoding::TransferRecord.new(
        "r", fragments: [{start_pos: 1, bytes: stream.byteslice(1, 1)}]
      )
      result = decode_partial([record], 4, 1, 2, ChunkyPNG::INTERLACING_NONE, palette, [0, 0, 2, 0])

      expect(result.color_at(0, 0)).to eql ChunkyPNG::Color.rgb(7, 7, 7)
      expect(result.color_at(1, 0)).to eql ChunkyPNG::Color.rgb(7, 7, 7)
      expect(result.color_at(2, 0)).to eql ChunkyPNG::Color.rgb(9, 9, 9)
    end

    it "treats full transparency as an actual RGBA color" do
      palette = decoding_palette(transparent_colors)
      canvas = ChunkyPNG::Canvas.new(2, 1, transparent_colors)
      stream = encode_stream(
  canvas, encoding_palette(transparent_colors), 1, ChunkyPNG::INTERLACING_NONE, ChunkyPNG::FILTER_NONE
)
      result = decode_partial([full_record(stream)], 2, 1, 1, ChunkyPNG::INTERLACING_NONE, palette, [0, 0, 1, 0])

      expect(result.color_at(0, 0)).to eql ChunkyPNG::Color.rgba(0, 0, 0, 0)
      expect(result.color_at(1, 0)).to eql ChunkyPNG::Color.rgb(5, 6, 7)
    end

    it "contradicts bytes whose indices fall outside the palette boundary" do
      palette = decoding_palette([ChunkyPNG::Color.rgb(10, 20, 30)])
      stream = [ChunkyPNG::FILTER_NONE, 0xff].pack("C*")
      result = decode_partial([full_record(stream)], 2, 1, 1, ChunkyPNG::INTERLACING_NONE, palette, [0, 0, 1, 0])

      expect(result.legal?).to be false
      expect(result.contradictions).not_to be_empty
    end
  end

  describe "missing or invalid filter bytes" do
    let(:depth) { 8 }
    let(:palette) { decoding_palette(opaque_colors) }
    let(:canvas) { ChunkyPNG::Canvas.new(4, 2, Array.new(8) { opaque_colors.sample }) }
    let(:stream) do
  encode_stream(canvas, encoding_palette(opaque_colors), depth, ChunkyPNG::INTERLACING_NONE, ChunkyPNG::FILTER_UP)
end

    it "keeps every legal filter possible when the filter byte is missing" do
      line = ChunkyPNG::Color.scanline_bytesize(ChunkyPNG::COLOR_INDEXED, depth, 4) + 1
      record = ChunkyPNG::Canvas::PartialPNGDecoding::TransferRecord.new(
        "r", fragments: [{start_pos: 1, bytes: stream.byteslice(1, line - 1)}]
      )
      result = decode_partial([record], 4, 2, depth, ChunkyPNG::INTERLACING_NONE, palette, [0, 0, 3, 0])

      expect(result.legal?).to be true
      expect(result.candidates_at(0, 0)).to include(canvas[0, 0])
    end

    it "reports an illegal filter type" do
      bad = stream.dup
      bad.setbyte(0, 9)
      line = ChunkyPNG::Color.scanline_bytesize(ChunkyPNG::COLOR_INDEXED, depth, 4) + 1
      record = ChunkyPNG::Canvas::PartialPNGDecoding::TransferRecord.new(
        "r", fragments: [{start_pos: 0, bytes: bad.byteslice(0, line)}]
      )
      result = decode_partial([record], 4, 2, depth, ChunkyPNG::INTERLACING_NONE, palette, [0, 0, 3, 0])

      expect(result.legal?).to be false
      expect(result.contradictions.first.message).to include("Illegal scanline filter")
    end
  end

  describe "Adam7 window queries" do
    let(:depth) { 2 }
    let(:colors) { opaque_colors }
    let(:palette) { decoding_palette(colors) }
    let(:canvas) { ChunkyPNG::Canvas.new(12, 9, Array.new(108) { colors.sample }) }
    let(:stream) do
  encode_stream(canvas, encoding_palette(colors), depth, ChunkyPNG::INTERLACING_ADAM7, ChunkyPNG::FILTER_PAETH)
end

    it "only reports coordinates inside the queried rectangle" do
      result = decode_partial([full_record(stream)], 12, 9, depth, ChunkyPNG::INTERLACING_ADAM7, palette, [3, 3, 5, 5])

      expect(result.legal?).to be true
      result.proven_pixels.each_key do |x, y|
        expect(x).to be_between(3, 5)
        expect(y).to be_between(3, 5)
      end
    end

    it "matches the complete decoder at every sampled point in the window" do
      result = decode_partial([full_record(stream)], 12, 9, depth, ChunkyPNG::INTERLACING_ADAM7, palette, [0, 0, 11, 8])

      9.times do |y|
        12.times do |x|
          expect(result.color_at(x, y)).to eql canvas[x, y]
        end
      end
    end
  end

  describe ".from_partial_datastream" do
    let(:depth) { 8 }
    let(:palette) { decoding_palette(opaque_colors) }
    let(:canvas) { ChunkyPNG::Canvas.new(4, 3, Array.new(12) { opaque_colors.sample }) }
    let(:stream) do
  encode_stream(canvas, encoding_palette(opaque_colors), depth, ChunkyPNG::INTERLACING_NONE, ChunkyPNG::FILTER_NONE)
end

    it "uses the reliable header and palette chunks" do
      encoding = encoding_palette(opaque_colors)
      ds = ChunkyPNG::Datastream.new
      ds.header_chunk = ChunkyPNG::Chunk::Header.new(
        width: 4, height: 3, depth: depth, color: ChunkyPNG::COLOR_INDEXED, interlace: ChunkyPNG::INTERLACING_NONE
      )
      ds.palette_chunk = encoding.to_plte_chunk
      ds.end_chunk = ChunkyPNG::Chunk::End.new

      result = ChunkyPNG::Canvas.from_partial_datastream(
        ds, transfers: [full_record(stream)], rect: [0, 0, 3, 2]
      )

      expect(result.proven_pixels.length).to eql 12
      expect(result.color_at(2, 1)).to eql canvas[2, 1]
    end
  end

  describe "unsupported input" do
    let(:palette) { decoding_palette(opaque_colors) }

    it "rejects non-indexed color modes" do
      expect do
        ChunkyPNG::Canvas.decode_partial_pixelstream(
          [], 2, 2, ChunkyPNG::COLOR_TRUECOLOR, 8, ChunkyPNG::INTERLACING_NONE, palette, [0, 0, 1, 1]
        )
      end.to raise_error(ChunkyPNG::NotSupported)
    end

    it "rejects out of bounds rectangles" do
      expect do
        decode_partial([], 2, 2, 8, ChunkyPNG::INTERLACING_NONE, palette, [0, 0, 2, 1])
      end.to raise_error(ChunkyPNG::OutOfBounds)
    end

    it "reports fragments beyond the header-implied stream size" do
      # A 2x1 8-bit indexed stream holds one filter byte and two data bytes.
      record = transfer_record("r", [{start_pos: 3, bytes: "\x01".b}])
      result = decode_partial([record], 2, 1, 8, ChunkyPNG::INTERLACING_NONE, palette, [0, 0, 1, 0])

      expect(result.legal?).to be false
      expect(result.contradictions.first.message).to include("outside the")
    end
  end

  describe "complete PNGs" do
    it "still decode through the regular path" do
      canvas = reference_canvas("adam7")
      expect(canvas.width).to be > 0
      expect(canvas[0, 0]).to be_a(Integer)
    end
  end
end
