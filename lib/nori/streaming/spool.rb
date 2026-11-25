class Nori
  module Streaming
    # A local, append-only spool for bytes the receiver has not committed yet.
    #
    # The spool keeps a sliding window of the original transmission. The
    # window's start is an absolute offset, so a record's byte range always
    # refers to the original stream even after earlier bytes have been
    # reclaimed. Appending verifies that the caller continues exactly where the
    # retained data ends; a gap is an error rather than a silent guess.
    class Spool
      attr_reader :path
      attr_accessor :start_offset

      def initialize(path = nil, start_offset = 0, content = nil)
        @path = path
        @buffer = content ? String.new(content, :encoding => Encoding::BINARY) : String.new(encoding: Encoding::BINARY)
        @start_offset = start_offset
        @mutex = Mutex.new
        if @path && content.nil? && File.exist?(@path)
          @buffer = File.binread(@path).force_encoding(Encoding::BINARY)
        end
        persist! if @path && content
      end

      def end_offset
        @start_offset + @buffer.bytesize
      end
      alias eof_offset end_offset

      def bytesize
        @buffer.bytesize
      end

      def empty?
        @buffer.empty?
      end

      def bytes(offset = @start_offset, length = nil)
        return "".b if offset >= end_offset
        relative = offset - @start_offset
        raise OffsetError, "Byte #{offset} is before the retained window (starts at #{@start_offset})" if relative < 0
        length ||= @buffer.bytesize - relative
        @buffer.byteslice(relative, length) || "".b
      end
      alias slice bytes

      def append(bytes, expected_offset = nil)
        bytes = bytes.b
        @mutex.synchronize do
          if expected_offset && expected_offset != end_offset
            raise OffsetError, "Expected bytes at #{end_offset}, got #{expected_offset}"
          end
          @buffer << bytes
          persist!
        end
        self
      end

      def append_at(bytes, offset)
        bytes = bytes.b
        if offset + bytes.bytesize <= @start_offset
          # Already reclaimed and acknowledged. A resend of that prefix cannot
          # affect the current window.
          return self
        end
        if offset < @start_offset
          skip = @start_offset - offset
          bytes = bytes.byteslice(skip, bytes.bytesize - skip)
          offset = @start_offset
        end
        if offset < end_offset
          overlap = [bytes.bytesize, end_offset - offset].min
          unless self.bytes(offset, overlap) == bytes.byteslice(0, overlap)
            raise OffsetError, "Bytes at #{offset} do not match already retained data"
          end
          return self if bytes.bytesize <= overlap
          bytes = bytes.byteslice(overlap, bytes.bytesize - overlap)
          offset = end_offset
        end
        raise OffsetError, "Bytes at #{offset} leave a gap after #{end_offset}" if offset > end_offset
        append(bytes)
      end

      # Drops everything before +offset+ from the retained window. The caller
      # (the session) only does this after a checkpoint has been written, so a
      # crash cannot lose bytes that were never made durable.
      def discard_before(offset)
        return self if offset <= @start_offset
        offset = [offset, end_offset].min
        @mutex.synchronize do
          relative = offset - @start_offset
          @buffer = @buffer.byteslice(relative, @buffer.bytesize - relative) || String.new(encoding: Encoding::BINARY)
          @start_offset = offset
          persist!
        end
        self
      end

      def truncate(offset)
        raise OffsetError, "Truncate point #{offset} is before the retained window" if offset < @start_offset
        @buffer = @buffer.byteslice(0, offset - @start_offset) || String.new(encoding: Encoding::BINARY)
        persist!
        self
      end

      def replace(offset, content)
        content = content.b
        if offset < @start_offset
          raise OffsetError, "Replacement at #{offset} is before the retained window (starts at #{@start_offset})"
        end
        relative = offset - @start_offset
        @buffer = @buffer.byteslice(0, relative).to_s.b + content
        persist!
        self
      end

      def to_s
        @buffer.dup.force_encoding(Encoding::UTF_8)
      end

      def persist!
        return unless @path
        dir = File.dirname(@path)
        FileUtils.mkdir_p(dir) unless dir == "." || Dir.exist?(dir)
        tmp = "#{@path}.tmp"
        File.binwrite(tmp, @buffer)
        File.rename(tmp, @path)
      end

      def self.load(path, start_offset = 0)
        new(path, start_offset)
      end
    end
  end
end
