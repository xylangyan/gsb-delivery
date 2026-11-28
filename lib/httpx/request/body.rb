# frozen_string_literal: true

require "tempfile"

module HTTPX
  # Implementation of the HTTP Request body as a delegator which iterates (responds to +each+) payload chunks.
  class Request::Body < SimpleDelegator
    # Raised when a request body can't be staged, or when its staged length
    # doesn't match the length declared for the request.
    class StagingError < Error; end

    # Stages the encoded entity bytes produced by a request body exactly once,
    # so that later attempts can replay the same bytes without asking the
    # source enumerable for a second copy.
    #
    # The staging area sits before transfer encoding, therefore chunk framing
    # is never part of the bytes being replayed or digested.
    class Staging
      CHUNK_SIZE = 16_384

      attr_reader :bytesize

      def initialize(source, expected_size: nil)
        @source = source
        @expected_size = expected_size
        @bytesize = 0
        @source_done = false
        @enum = nil
        @string_read = false
        @mode = if source.respond_to?(:read)
          :read
        elsif source.respond_to?(:each)
          :each
        else
          :string
        end

        @tempfile = Tempfile.new("httpx-request-body", binmode: true)
        @reader = File.open(@tempfile.path, "rb")
      rescue SystemCallError, IOError, RuntimeError => e
        raise StagingError, "unable to stage request body: #{e.message}"
      end

      def append(chunk)
        return if chunk.nil?

        chunk = String(chunk).b
        return if chunk.empty?

        if @expected_size && (@bytesize + chunk.bytesize) > @expected_size
          raise StagingError, "request body length mismatch: expected #{@expected_size} bytes, " \
                              "staged more than that"
        end

        @tempfile.seek(@bytesize)
        @tempfile.write(chunk)
        @tempfile.flush
        @bytesize += chunk.bytesize
      rescue SystemCallError, IOError => e
        raise StagingError, "unable to stage request body: #{e.message}"
      end

      # drains the remainder of the source into the staging area, validating
      # the final size when the request declared one.
      def finish!
        return validate! if @source_done

        while (chunk = next_source_chunk)
          append(chunk)
        end

        @source_done = true
        validate!
      rescue StandardError => e
        raise e if e.is_a?(StagingError)

        raise StagingError, "unable to stage request body: #{e.message}"
      end

      def each
        return enum_for(__method__) unless block_given?

        offset = 0
        loop do
          if offset < @bytesize
            chunk = read_at(offset, CHUNK_SIZE)
            offset += chunk.bytesize
            yield chunk
            next
          end

          break if @source_done

          chunk = next_source_chunk

          if chunk.nil?
            @source_done = true
            validate!
            break
          end

          chunk = String(chunk).b
          next if chunk.empty?

          append(chunk)
          offset += chunk.bytesize
          yield chunk
        end
      end

      def rewind
        @reader.seek(0)
        0
      end

      def source_complete?
        @source_done
      end

      def hexdigest(algorithm)
        finish!

        digest = algorithm.new
        each_staged_chunk { |chunk| digest.update(chunk) }
        digest.hexdigest
      end

      def close
        @reader.close
        @tempfile.close!
      rescue IOError
      end

      private

      def each_staged_chunk
        @reader.seek(0)
        while (chunk = @reader.read(CHUNK_SIZE))
          yield chunk
        end
      end

      def read_at(offset, length)
        @reader.seek(offset)
        @reader.read(length) || "".b
      end

      def next_source_chunk
        case @mode
        when :read
          chunk = @source.read(CHUNK_SIZE)
          return if chunk.nil? || chunk.empty?

          chunk
        when :each
          @enum ||= @source.to_enum
          begin
            @enum.next
          rescue StopIteration
            nil
          end
        else
          return if @string_read

          @string_read = true
          @source.to_s
        end
      end

      def validate!
        return unless @expected_size

        return if @bytesize == @expected_size

        raise StagingError, "request body length mismatch: expected #{@expected_size} bytes, " \
                            "staged #{@bytesize} bytes"
      end
    end

    class << self
      def new(h, options, body: nil, **params)
        case body
        when self
          # request derives its options from body
          body.options = options.merge(params)
          body
        when nil
          super(h, options, **params)
        else
          super
        end
      end
    end

    attr_accessor :options

    # inits the instance with the request +headers+, +options+ and +params+, which contain the payload definition.
    # it wraps the given body with the appropriate encoder on initialization.
    #
    #   ..., json: { foo: "bar" }) #=> json encoder
    #   ..., form: { foo: "bar" }) #=> form urlencoded encoder
    #   ..., form: { foo: Pathname.open("path/to/file") }) #=> multipart urlencoded encoder
    #   ..., form: { foo: File.open("path/to/file") }) #=> multipart urlencoded encoder
    #   ..., form: { body: "bla") }) #=> raw data encoder
    def initialize(h, options, **params)
      @headers = h
      @body = self.class.initialize_body(params)
      @options = options.merge(params)
      @unbounded_body = nil
      @staging = nil
      @replayable = false

      if @body
        if @options.compress_request_body && @headers.key?("content-encoding")

          @headers.get("content-encoding").each do |encoding|
            @body = self.class.initialize_deflater_body(@body, encoding)
          end
        end

        @headers["content-type"] ||= @body.content_type
        @headers["content-length"] = @body.bytesize unless unbounded_body?
      end

      super(@body)
    end

    # makes the body replayable: the encoded entity bytes are copied to a
    # staging buffer the first time they're read, and replayed from there on
    # subsequent attempts.
    def enable_replay!
      @replayable = true
    end

    def replayable?
      @replayable
    end

    # returns a hexdigest of the bytes of the encoded entity, without any
    # transfer encoding framing.
    def digest(algorithm)
      staging.hexdigest(algorithm)
    end

    # consumes and yields the request payload in chunks.
    def each(&block)
      return enum_for(__method__) unless block
      return if @body.nil?

      if @replayable
        body = stream(staging.each)
        if body.respond_to?(:each)
          body.each(&block)
        else
          block[body.to_s]
        end
        return
      end

      body = stream(@body)
      if body.respond_to?(:read)
        while (chunk = body.read(16_384))
          block.call(chunk)
        end
        # TODO: use copy_stream once bug is resolved: https://bugs.ruby-lang.org/issues/21131
        # IO.copy_stream(body, ProcIO.new(block))
      elsif body.respond_to?(:each)
        body.each(&block)
      else
        block[body.to_s]
      end
    end

    def close
      @staging&.close
      @staging = nil
      @body.close if @body.respond_to?(:close)
    end

    # if the +@body+ is rewindable, it rewinnds it.
    def rewind
      return if @body.nil?

      if @staging
        @staging.rewind
      elsif @replayable
        # the source will be staged on the next attempt; do not touch it
        # (it may be a one-shot stream which must not be rewound twice).
        nil
      elsif @body.respond_to?(:rewind)
        @body.rewind
      end
    end

    # return +true+ if the +body+ has been fully drained (or does nnot exist).
    def empty?
      return true if @body.nil?
      return false if chunked?

      if @staging
        @staging.bytesize.zero?
      else
        @body.bytesize.zero?
      end
    end

    # returns the +@body+ payload size in bytes.
    def bytesize
      return 0 if @body.nil?
      return @staging.bytesize if @staging

      @body.bytesize
    end

    # sets the body to yield using chunked trannsfer encoding format.
    def stream(body)
      return body unless chunked?

      Transcoder::Chunker.encode(body.enum_for(:each))
    end

    # returns whether the body yields infinitely.
    def unbounded_body?
      return @unbounded_body unless @unbounded_body.nil?

      @unbounded_body = !@body.nil? && (chunked? || @body.bytesize == Float::INFINITY)
    end

    # returns whether the chunked transfer encoding header is set.
    def chunked?
      @headers["transfer-encoding"] == "chunked"
    end

    # sets the chunked transfer encoding header.
    def chunk!
      @headers.add("transfer-encoding", "chunked")
    end

    # simplecov:disable
    def inspect
      "#<#{self.class}:#{object_id} " \
        "#{unbounded_body? ? "stream" : "@bytesize=#{bytesize}"}>"
    end
    # simplecov:enable

    private

    def staging
      @staging ||= begin
        length = @headers["content-length"]
        expected_size = length && Integer(length, exception: false)

        Staging.new(@body, expected_size: expected_size)
      end
    end

    class << self
      def initialize_body(params)
        if (body = params.delete(:body))
          # @type var body: bodyIO
          Transcoder::Body.encode(body)
        elsif (form = params.delete(:form))
          if Transcoder::Multipart.multipart?(form)
            # @type var form: Transcoder::Multipart::multipart_input
            Transcoder::Multipart.encode(form)
          else
            # @type var form: Transcoder::urlencoded_input
            Transcoder::Form.encode(form)
          end
        elsif (json = params.delete(:json))
          # @type var body: _ToJson
          Transcoder::JSON.encode(json)
        end
      end

      # returns the +body+ wrapped with the correct deflater accordinng to the given +encodisng+.
      def initialize_deflater_body(body, encoding)
        case encoding
        when "gzip"
          Transcoder::GZIP.encode(body)
        when "deflate"
          Transcoder::Deflate.encode(body)
        when "identity"
          body
        else
          body
        end
      end
    end
  end
end
