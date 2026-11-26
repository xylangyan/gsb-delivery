require 'bindata/io'

module BinData
  # Raised when a preview can not be layed out.  This happens when a delayed
  # write would target a negative absolute offset.
  #
  # The offending DelayedIO object and the offset it attempted to use are
  # available via #field and #offset.
  class PreviewError < IOError
    def initialize(field, offset)
      @field  = field
      @offset = offset
      super("invalid abs_offset #{offset.inspect} for #{field.debug_name}")
    end

    # The DelayedIO object that could not be layed out.
    attr_reader :field

    # The absolute offset that #field attempted to write to.
    attr_reader :offset
    alias abs_offset offset
  end

  # A sparse byte store used as the destination of a preview write.
  #
  # Bytes that are never written read as zero.  Only non-zero bytes are kept,
  # so a very large logical volume can be layed out without materialising the
  # empty space.
  class PreviewVolume
    def initialize
      @bytes  = {}
      @pos    = 0
      @length = 0
    end

    def seekable?
      true
    end

    def pos
      @pos
    end
    alias tell pos
    alias offset pos

    def seek(amount, whence = ::IO::SEEK_SET)
      new_pos =
        case whence
        when ::IO::SEEK_SET then amount
        when ::IO::SEEK_CUR then @pos + amount
        when ::IO::SEEK_END then @length + amount
        else
          raise ArgumentError, "unknown whence #{whence}"
        end

      if new_pos.negative?
        raise IOError, "can not seek to negative offset #{new_pos}"
      end

      @pos = new_pos
    end

    # Writes +data+ at the current position.  A zero byte removes any
    # previously written value, so that a later write of zero correctly
    # overwrites an earlier non-zero byte.
    def write(data)
      data = data.to_s
      data.each_byte.with_index do |byte, i|
        pos = @pos + i
        if byte.zero?
          @bytes.delete(pos)
        else
          @bytes[pos] = byte
        end
      end

      @pos += data.bytesize
      @length = @pos if @pos > @length
      data.bytesize
    end

    # The logical length of the volume.
    def logical_length
      @length
    end

    # A frozen snapshot of the non-zero bytes, keyed by absolute offset.
    def snapshot
      @bytes.dup.freeze
    end
  end

  # An immutable preview of what an AutoCallDelayedIO record would write.
  #
  #   preview = record.preview
  #   preview.num_bytes        #=> logical length of the previewed volume
  #   preview.read(3, 2)       #=> "\x00\x31"
  #   preview.read(3, 2)       #=> "\x00\x31"  (repeatable)
  #
  # Regions that were never written read as zero.  A preview is a snapshot, so
  # later changes to the record, new previews, or real writes do not affect it.
  class Preview
    def initialize(bytes, length)
      @bytes  = bytes
      @length = length
    end

    # The logical length of the previewed volume.
    def num_bytes
      @length
    end
    alias length num_bytes
    alias size num_bytes
    alias logical_length num_bytes
    alias logical_size num_bytes
    alias byte_length num_bytes

    # Reads +length+ bytes starting at +abs_offset+.  Unwritten bytes are
    # returned as zero.  Reading consumes nothing, so the same window may be
    # read repeatedly.
    #
    # +abs_offset+ may also be a Range, in which case +length+ is ignored.
    def read(abs_offset, length = nil)
      if abs_offset.is_a?(Range)
        range       = abs_offset
        abs_offset  = range.begin
        range_end   = range.end
        range_end  -= 1 if range.exclude_end?
        length      = range_end - range.begin + 1
      end

      length ||= @length - abs_offset
      return binary_string("") if length.nil? || length <= 0

      str = binary_string("")
      length.times { |i| str << @bytes.fetch(abs_offset + i) { 0 }.chr }
      str
    end
    alias readbytes read
    alias read_bytes read
    alias read_at read
    alias read_window read
    alias byte_window read
    alias window read
    alias bytes read
    alias byteslice read

    def [](abs_offset, length = nil)
      read(abs_offset, length)
    end

    # Returns the whole volume as a binary string.  This materialises the
    # empty space, so it refuses to build very large volumes.
    def to_binary_s
      if @length > MAX_MATERIALIZED_LENGTH
        raise RangeError,
              "preview volume of #{@length} bytes is too large to materialise"
      end
      read(0, @length)
    end
    alias to_s to_binary_s

    MAX_MATERIALIZED_LENGTH = 16 * 1024 * 1024

    private

    def binary_string(str)
      str.dup.force_encoding(Encoding::BINARY)
    end
  end

  # The mutable state used while laying out a single preview.
  class PreviewSession # :nodoc:
    attr_reader :volume, :io

    def initialize
      @volume = PreviewVolume.new
      @io     = BinData::IO::Write.new(@volume)
      @io.preview_session = self
      @seen   = {}.compare_by_identity
    end

    # Marks +obj+ as already handled by this preview.
    def visit(obj)
      @seen[obj] = true
    end

    def visited?(obj)
      @seen.key?(obj)
    end
  end
end
