# frozen_string_literal: true
# rubocop:todo all

require 'tempfile'

module BSON
  # Streaming cleaner for collector BSON frame streams.
  #
  # Reads a sequence of BSON documents ("frames") from a forward-only
  # input, filters each station's +samples+ document using the station's
  # last +keep+ packed_bit vector as the quality mask, and appends the
  # cleaned frames to an append-only output. Frames that fail validation
  # are dropped wholesale and reported to the caller; cleaning resumes
  # at the next frame.
  #
  # Frames may be up to 16 MiB. Each frame is spooled to a temporary
  # file while it is cleaned, so the input, intermediate, and pending
  # output data held in memory stays small regardless of frame size.
  class StreamCleaner
    # Maximum accepted frame size, in bytes.
    MAX_FRAME_SIZE = 16 * 1024 * 1024

    # Buffer size used when streaming data to and from disk.
    CHUNK_SIZE = 64 * 1024

    # Number of samples filtered per iteration when rewriting a channel.
    BLOCK_SAMPLES = 8192

    DOCUMENT_TYPE = 0x03
    ARRAY_TYPE = 0x04
    BINARY_TYPE = 0x05
    VECTOR_SUBTYPE = 0x09

    STATIONS_FIELD = 'stations'
    SAMPLES_FIELD = 'samples'
    TEMPERATURE_FIELD = 'temperature'
    CODE_FIELD = 'code'
    KEEP_FIELD = 'keep'
    TARGET_FIELDS = [ TEMPERATURE_FIELD, CODE_FIELD, KEEP_FIELD ].freeze

    FLOAT32_DTYPE = Binary::VECTOR_DATA_TYPES[:float32]
    INT8_DTYPE = Binary::VECTOR_DATA_TYPES[:int8]
    PACKED_BIT_DTYPE = Binary::VECTOR_DATA_TYPES[:packed_bit]

    ZERO_INT32 = [ 0 ].pack('l<').freeze
    VECTOR_SUBTYPE_BYTE = VECTOR_SUBTYPE.chr.freeze
    FF_BLOCK = ("\xFF".b * CHUNK_SIZE).freeze
    POPCOUNT = (0..255).map { |byte| byte.to_s(2).count('1') }.freeze

    # Value sizes for element types with a fixed-size value.
    FIXED_VALUE_SIZES = {
      0x01 => 8, 0x07 => 12, 0x08 => 1, 0x09 => 8, 0x10 => 4,
      0x11 => 8, 0x12 => 8, 0x13 => 16,
    }.freeze

    # Element types with no value bytes.
    EMPTY_VALUE_TYPES = [ 0x06, 0x0A, 0xFF, 0x7F ].freeze

    # Describes a frame that was rejected or could not be read.
    #
    # @!attribute offset
    #   @return [ Integer ] Byte offset of the frame start in the input.
    # @!attribute station_index
    #   @return [ Integer | nil ] Position of the offending station in
    #     the +stations+ array, if known.
    # @!attribute field
    #   @return [ String | nil ] Name of the offending field, if known.
    # @!attribute reason
    #   @return [ Symbol ] Why the frame was rejected.
    FrameError = Struct.new(:offset, :station_index, :field, :reason, keyword_init: true) do
      # @return [ true | false ] Whether the input ended mid-frame.
      def truncated?
        reason == :truncated
      end
    end

    # Raised internally when a frame fails validation; converted to a
    # FrameError for the caller.
    #
    # @api private
    class FrameCleaningError < StandardError
      attr_accessor :station_index, :field, :reason

      def initialize(message, station_index: nil, field: nil, reason: :malformed)
        super(message)
        @station_index = station_index
        @field = field
        @reason = reason
      end
    end

    # Cleaning plan for one samples document.
    #
    # @api private
    SamplePlan = Struct.new(:targets, :vectors, :kept)

    # Cleans a stream of BSON frames.
    #
    # @param [ #read ] input Forward-only source of concatenated frames.
    # @param [ #write ] output Append-only sink for cleaned frames.
    # @yieldparam error [ FrameError ] Reports each rejected frame.
    #
    # @return [ Array<FrameError> ] The rejected frames, in input order.
    def self.clean(input, output, &on_error)
      new.clean(input, output, &on_error)
    end

    # Cleans a stream of BSON frames.
    #
    # @param [ #read ] input Forward-only source of concatenated frames.
    # @param [ #write ] output Append-only sink for cleaned frames.
    # @yieldparam error [ FrameError ] Reports each rejected frame.
    #
    # @return [ Array<FrameError> ] The rejected frames, in input order.
    def clean(input, output, &on_error)
      errors = []
      offset = 0
      loop do
        header = read_partial(input, 4)
        if header.bytesize < 4
          report(errors, FrameError.new(offset: offset, reason: :truncated), on_error) unless header.empty?
          break
        end
        length = header.unpack1('l<')
        break unless report(errors, check_frame_length(length, offset), on_error)

        report(errors, process_frame(input, length, offset, output), on_error)
        offset += length
      end
      errors
    end

    private

    def check_frame_length(length, offset)
      return nil if length >= 5 && length <= MAX_FRAME_SIZE

      FrameError.new(offset: offset, reason: :invalid_frame_length)
    end

    def report(errors, error, on_error)
      return true unless error

      errors << error
      on_error&.call(error)
      false
    end

    def process_frame(input, length, offset, output)
      spool = open_spool('bson-stream-cleaner-in')
      out = open_spool('bson-stream-cleaner-out')
      unless spool_frame(input, length, spool)
        return FrameError.new(offset: offset, reason: :truncated)
      end

      clean_spooled_frame(spool, out, offset) || stream_out(out, output)
    ensure
      spool&.close!
      out&.close!
    end

    def open_spool(prefix)
      Tempfile.new(prefix).tap(&:binmode)
    end

    def spool_frame(input, length, spool)
      spool.write([ length ].pack('l<'))
      remaining = length - 4
      while remaining.positive?
        chunk = input.read([ CHUNK_SIZE, remaining ].min)
        break if chunk.nil? || chunk.empty?

        spool.write(chunk)
        remaining -= chunk.bytesize
      end
      remaining.zero?
    end

    def clean_spooled_frame(spool, out, offset)
      @in = spool
      @out = out
      @frame_size = spool.size
      spool.rewind
      clean_frame
      nil
    rescue FrameCleaningError => e
      FrameError.new(offset: offset, station_index: e.station_index, field: e.field, reason: e.reason)
    ensure
      @in = @out = @frame_size = nil
    end

    def stream_out(out, output)
      out.rewind
      while (chunk = out.read(CHUNK_SIZE))
        output.write(chunk)
      end
      nil
    end

    def clean_frame
      transform_document do |type, name, _value_pos, _index|
        if name == STATIONS_FIELD && type == ARRAY_TYPE
          transform_stations
          true
        else
          false
        end
      end
    end

    def transform_stations
      transform_document do |type, _name, _value_pos, index|
        clean_station_element(type, index)
      end
    end

    def clean_station_element(type, index)
      unless type == DOCUMENT_TYPE
        raise FrameCleaningError.new('station element is not a document',
                                     station_index: index, reason: :invalid_station)
      end

      begin
        transform_station(index)
      rescue FrameCleaningError => e
        e.station_index ||= index
        raise
      end
      true
    end

    def transform_station(station_index)
      samples_seen = false
      transform_document do |type, name, _value_pos, _index|
        if name == SAMPLES_FIELD
          samples_seen = true
          transform_samples_field(type, station_index)
        else
          false
        end
      end
      return if samples_seen

      raise FrameCleaningError.new('station has no samples document',
                                   station_index: station_index, field: SAMPLES_FIELD, reason: :missing_field)
    end

    def transform_samples_field(type, station_index)
      unless type == DOCUMENT_TYPE
        raise FrameCleaningError.new('samples field is not a document',
                                     station_index: station_index, field: SAMPLES_FIELD, reason: :invalid_type)
      end

      transform_samples(station_index)
      true
    end

    def transform_samples(station_index)
      doc_start = @in.pos
      doc_end = doc_start + checked_doc_length(doc_start)
      targets = scan_target_fields(doc_end)
      vectors = validate_vectors(targets, station_index)
      kept = count_kept_bits(vectors[:keep][:payload_pos], vectors[:temperature][:count])
      plan = SamplePlan.new(targets, vectors, kept)
      @in.seek(doc_start)
      transform_document do |_type, name, value_pos, _index|
        rewrite_sample_element(name, value_pos, plan)
      end
    end

    def scan_target_fields(doc_end)
      targets = {}
      each_element(doc_end) do |type, name, value_pos, _element_start, _element_end, _index|
        targets[name] = [ type, value_pos ] if TARGET_FIELDS.include?(name)
      end
      targets
    end

    def validate_vectors(targets, station_index)
      vectors = {
        temperature: validate_vector(targets, TEMPERATURE_FIELD, FLOAT32_DTYPE, station_index),
        code: validate_vector(targets, CODE_FIELD, INT8_DTYPE, station_index),
        keep: validate_vector(targets, KEEP_FIELD, PACKED_BIT_DTYPE, station_index),
      }
      counts = vectors.transform_values { |vector| vector[:count] }
      return vectors if counts.values.uniq.length == 1

      raise FrameCleaningError.new("sample count mismatch: #{counts}",
                                   station_index: station_index, field: mismatch_field(counts),
                                   reason: :count_mismatch)
    end

    def mismatch_field(counts)
      return TEMPERATURE_FIELD if counts[:code] == counts[:keep]
      return CODE_FIELD if counts[:temperature] == counts[:keep]

      KEEP_FIELD
    end

    def validate_vector(targets, field, dtype, station_index)
      entry = targets[field]
      unless entry
        raise FrameCleaningError.new("missing #{field} vector",
                                     station_index: station_index, field: field, reason: :missing_field)
      end

      type, value_pos = entry
      unless type == BINARY_TYPE
        raise FrameCleaningError.new("#{field} is not a binary field",
                                     station_index: station_index, field: field, reason: :invalid_type)
      end

      header = read_vector_header(value_pos, field, station_index)
      count = vector_sample_count(header, dtype, field, station_index)
      { count: count, payload_pos: value_pos + 7, payload_len: header[:payload_len] }
    end

    def read_vector_header(value_pos, field, station_index)
      if value_pos + 7 > @frame_size
        raise FrameCleaningError.new('malformed frame: truncated binary', station_index: station_index, field: field)
      end

      @in.seek(value_pos)
      blen = read_int32
      subtype = read_byte
      unless subtype == VECTOR_SUBTYPE && blen >= 2 && value_pos + 5 + blen <= @frame_size
        raise FrameCleaningError.new("#{field} is not a well-formed vector",
                                     station_index: station_index, field: field, reason: :invalid_vector)
      end

      { dtype: read_byte, padding: read_byte, payload_len: blen - 2 }
    end

    def vector_sample_count(header, dtype, field, station_index)
      valid = header[:dtype] == dtype && vector_padding_valid?(header, dtype)
      unless valid
        raise FrameCleaningError.new("invalid #{field} vector payload",
                                     station_index: station_index, field: field, reason: :invalid_vector)
      end

      payload_len = header[:payload_len]
      case dtype
      when FLOAT32_DTYPE then payload_len / 4
      when INT8_DTYPE then payload_len
      else payload_len * 8 - header[:padding]
      end
    end

    def vector_padding_valid?(header, dtype)
      padding = header[:padding]
      payload_len = header[:payload_len]
      if dtype == PACKED_BIT_DTYPE
        padding <= 7 && (payload_len.positive? || padding.zero?)
      else
        padding.zero? && (dtype != FLOAT32_DTYPE || (payload_len % 4).zero?)
      end
    end

    def rewrite_sample_element(name, value_pos, plan)
      entry = plan.targets[name]
      return false unless entry && entry[1] == value_pos

      case name
      when TEMPERATURE_FIELD
        write_filtered_channel(plan.vectors[:keep][:payload_pos], plan.vectors[:temperature], plan.kept, 4, FLOAT32_DTYPE)
      when CODE_FIELD
        write_filtered_channel(plan.vectors[:keep][:payload_pos], plan.vectors[:code], plan.kept, 1, INT8_DTYPE)
      else
        write_ones_mask(plan.kept)
      end
      true
    end

    def write_element_header(type, name)
      @out.write(type.chr)
      @out.write(name)
      @out.write(NULL_BYTE)
    end

    def write_filtered_channel(mask_pos, channel, kept, sample_size, dtype)
      @out.write([ 2 + kept * sample_size ].pack('l<'))
      @out.write(VECTOR_SUBTYPE_BYTE)
      @out.write(dtype.chr)
      @out.write(NULL_BYTE)
      offset = 0
      count = channel[:count]
      while offset < count
        n = [ BLOCK_SAMPLES, count - offset ].min
        write_filtered_block(mask_pos + offset / 8, channel[:payload_pos] + offset * sample_size, n, sample_size)
        offset += n
      end
    end

    def write_filtered_block(mask_pos, channel_pos, n, sample_size)
      mask = read_at(mask_pos, (n + 7) / 8)
      channel = read_at(channel_pos, n * sample_size)
      piece = +''.b
      i = 0
      while i < n
        piece << channel.byteslice(i * sample_size, sample_size) if (mask.getbyte(i >> 3) & (0x80 >> (i & 7))) != 0
        i += 1
      end
      @out.write(piece)
    end

    def write_ones_mask(kept)
      padding = (8 - kept % 8) % 8
      @out.write([ 2 + (kept + 7) / 8 ].pack('l<'))
      @out.write(VECTOR_SUBTYPE_BYTE)
      @out.write(PACKED_BIT_DTYPE.chr)
      @out.write(padding.chr)
      full = kept / 8
      while full.positive?
        n = [ full, CHUNK_SIZE ].min
        @out.write(FF_BLOCK.byteslice(0, n))
        full -= n
      end
      @out.write(((0xFF << padding) & 0xFF).chr) if padding.positive?
    end

    def count_kept_bits(payload_pos, count)
      kept = 0
      pos = payload_pos
      remaining = count
      while remaining.positive?
        nbytes = [ (remaining + 7) / 8, CHUNK_SIZE ].min
        kept += count_block_bits(read_at(pos, nbytes), remaining)
        pos += nbytes
        remaining -= nbytes * 8
      end
      kept
    end

    def count_block_bits(bytes, valid_bits)
      bits = [ valid_bits, bytes.bytesize * 8 ].min
      kept = 0
      bytes.byteslice(0, bits / 8).each_byte { |byte| kept += POPCOUNT[byte] }
      if bits % 8 != 0
        kept += POPCOUNT[bytes.getbyte(bits / 8) & (0xFF << (8 - bits % 8)) & 0xFF]
      end
      kept
    end

    def transform_document
      doc_start = @in.pos
      doc_end = doc_start + checked_doc_length(doc_start)
      out_start = @out.pos
      @out.write(ZERO_INT32)
      each_element(doc_end) do |type, name, value_pos, _element_start, element_end, index|
        write_element_header(type, name)
        copy_bytes(value_pos, element_end - value_pos) unless yield(type, name, value_pos, index)
      end
      @out.write(NULL_BYTE)
      patch_length(out_start)
    end

    def each_element(doc_end)
      index = 0
      loop do
        raise FrameCleaningError.new('malformed frame: document overruns its length') if @in.pos >= doc_end

        element_start = @in.pos
        type = read_byte
        return if check_document_end(type, doc_end)

        name = read_cstring(doc_end)
        value_pos = @in.pos
        skip_value(type, doc_end)
        element_end = @in.pos
        @in.seek(value_pos)
        yield type, name, value_pos, element_start, element_end, index
        @in.seek(element_end)
        index += 1
      end
    end

    def check_document_end(type, doc_end)
      return false unless type.zero?

      unless @in.pos == doc_end
        raise FrameCleaningError.new('malformed frame: document length mismatch')
      end

      true
    end

    def copy_bytes(from, length)
      @in.seek(from)
      remaining = length
      while remaining.positive?
        chunk = read_exact([ CHUNK_SIZE, remaining ].min)
        @out.write(chunk)
        remaining -= chunk.bytesize
      end
    end

    def patch_length(out_start)
      out_end = @out.pos
      @out.seek(out_start)
      @out.write([ out_end - out_start ].pack('l<'))
      @out.seek(out_end)
    end

    def checked_doc_length(doc_start)
      length = read_int32
      if length < 5 || doc_start + length > @frame_size
        raise FrameCleaningError.new('malformed frame: invalid document length')
      end

      length
    end

    def skip_value(type, limit)
      if (size = FIXED_VALUE_SIZES[type])
        skip_bytes(size, limit)
      elsif EMPTY_VALUE_TYPES.include?(type)
        nil
      else
        skip_variable_value(type, limit)
      end
    end

    def skip_variable_value(type, limit)
      case type
      when 0x02, 0x0D, 0x0E then skip_bytes(checked_string_length, limit)
      when 0x03, 0x04 then skip_bytes(checked_doc_length(@in.pos) - 4, limit)
      when 0x05 then skip_binary(limit)
      when 0x0B then skip_regex(limit)
      when 0x0C then skip_bytes(checked_string_length + 12, limit)
      when 0x0F then skip_bytes(checked_code_w_scope_length - 4, limit)
      else raise FrameCleaningError.new("malformed frame: unknown element type #{type}")
      end
    end

    def skip_bytes(count, limit)
      if @in.pos + count > limit
        raise FrameCleaningError.new('malformed frame: value overruns document')
      end

      @in.seek(@in.pos + count)
    end

    def checked_string_length
      length = read_int32
      raise FrameCleaningError.new('malformed frame: invalid string length') if length < 1

      length
    end

    def skip_binary(limit)
      length = read_int32
      raise FrameCleaningError.new('malformed frame: invalid binary length') if length.negative?

      skip_bytes(1 + length, limit)
    end

    def skip_regex(limit)
      read_cstring(limit)
      read_cstring(limit)
    end

    def checked_code_w_scope_length
      length = read_int32
      raise FrameCleaningError.new('malformed frame: invalid code_w_s length') if length < 14

      length
    end

    def read_cstring(limit)
      result = +''.b
      loop do
        remaining = limit - @in.pos
        raise FrameCleaningError.new('malformed frame: unterminated cstring') if remaining <= 0

        chunk = read_exact([ CHUNK_SIZE, remaining ].min)
        nul = chunk.index(NULL_BYTE)
        if nul
          result << chunk.byteslice(0, nul)
          @in.seek(@in.pos - (chunk.bytesize - nul - 1))
          return result
        end
        result << chunk
      end
    end

    def read_exact(length)
      data = @in.read(length)
      if data.nil? || data.bytesize != length
        raise FrameCleaningError.new('malformed frame: unexpected end of data')
      end

      data
    end

    def read_at(pos, length)
      @in.seek(pos)
      read_exact(length)
    end

    def read_int32
      read_exact(4).unpack1('l<')
    end

    def read_byte
      read_exact(1).getbyte(0)
    end

    def read_partial(input, length)
      result = +''.b
      while result.bytesize < length
        chunk = input.read(length - result.bytesize)
        break if chunk.nil? || chunk.empty?

        result << chunk
      end
      result
    end
  end
end
