class Nori
  module Streaming
    # One closed record. The parsed Nori value is available through #value
    # (and through the hash-like readers below). The byte range is absolute:
    # it addresses the original transmission, not the retained spool window.
    class Record
      include Enumerable

      attr_reader :id, :sequence, :name, :path, :byte_start, :byte_end,
                  :closed_at, :options, :status, :context
      attr_accessor :raw_bytes, :raw_path, :value_path

      def initialize(attributes = {})
        attributes = {} unless attributes.is_a?(Hash)
        @id = attributes[:id]
        @sequence = attributes[:sequence]
        @name = attributes[:name]
        @path = attributes[:path]
        @byte_start = attributes[:byte_start]
        @byte_end = attributes[:byte_end]
        @closed_at = attributes[:closed_at]
        @options = attributes[:options] || {}
        @value = attributes[:value]
        @value_loaded = attributes.key?(:value)
        @status = attributes[:status] || :closed
        @delivered = attributes[:delivered] || false
        @acknowledged = attributes[:acknowledged] || false
        @parser = attributes[:parser]
        @context = attributes[:context] || {}
        @raw_bytes = attributes[:raw_bytes]
        @raw_path = attributes[:raw_path]
        @value_path = attributes[:value_path]
        @digest = attributes[:digest]
      end

      # The parsed record (the value Nori would have produced for this one
      # element). Rebuilt from the retained bytes if this record was loaded
      # from a checkpoint and its value was not kept in memory.
      def value
        return @value if @value_loaded || @byte_start.nil?
        @value_loaded = true
        if @value_path && File.exist?(@value_path)
          begin
            @value = Marshal.load(File.binread(@value_path))
            return @value
          rescue StandardError
            @value_path = nil
          end
        end
        @value = @parser ? @parser.call(self) : nil
      end
      alias parsed value
      alias record value
      alias data value

      # The exact original bytes of the record. Kept in memory while the value
      # is live, and on disk while the record is unacknowledged so recovery
      # never has to reread transmission bytes that were already released.
      def raw
        @raw_bytes || (@raw_path && File.exist?(@raw_path) ? File.binread(@raw_path) : nil)
      end
      alias bytes raw
      alias raw_bytes raw
      alias xml raw

      def value=(new_value)
        @value = new_value
        @value_loaded = true
      end

      def delivered?
        @delivered
      end

      def acknowledged?
        @acknowledged
      end

      def pending?
        @delivered && !@acknowledged
      end

      def acknowledge!
        @acknowledged = true
        @status = :acknowledged
        self
      end

      def deliver!
        @delivered = true
        @status = :delivered unless @acknowledged
        self
      end

      def status=(status)
        @status = status
      end

      def id=(id)
        @id = id
      end

      def business_id
        @id
      end
      alias business_number business_id
      alias ticket business_id
      alias number business_id

      def byte_range
        @byte_start..(@byte_end - 1) if @byte_start && @byte_end
      end

      def range
        byte_range
      end

      def start_offset
        @byte_start
      end
      alias start_byte start_offset
      alias byte_offset start_offset

      def end_offset
        @byte_end
      end
      alias end_byte end_offset

      def length
        @byte_end - @byte_start if @byte_start && @byte_end
      end

      def [](key)
        value[key]
      end

      def fetch(*args, &block)
        value.fetch(*args, &block)
      end

      def dig(*args)
        value.dig(*args)
      end

      def key?(*args)
        value.key?(*args)
      end
      alias has_key? key?

      def keys
        value.keys if value.respond_to?(:keys)
      end

      def each(&block)
        value.each(&block)
      end

      def to_h
        value
      end
      alias to_hash to_h

      # The hash-like readers above intentionally delegate to #value. Keep a
      # separate metadata view (credentials and the byte range) available under
      # a name that cannot collide with record data.
      def metadata
        to_h_metadata
      end

      def to_s
        value.to_s
      end

      def ==(other)
        return true if equal?(other)
        value == other || super
      end

      def inspect
        "#<#{self.class.name} id=#{@id.inspect} sequence=#{@sequence.inspect} " \
          "path=#{@path.inspect} bytes=#{@byte_start}...#{@byte_end} value=#{value.inspect}>"
      end

      # A compact credential suitable for a receipt message. It contains
      # everything needed to correlate the record with the original bytes.
      attr_reader :digest

      def credential
        {
          :id => @id,
          :business_id => @id,
          :sequence => @sequence,
          :name => @name,
          :path => @path && @path.dup,
          :byte_start => @byte_start,
          :byte_end => @byte_end,
          :byte_range => byte_range,
          :status => @status.to_s.to_sym,
          :acknowledged => @acknowledged,
          :digest => @digest
        }
      end
      alias receipt credential
      alias to_receipt credential

      def to_h_metadata
        credential.merge(:options => @options)
      end

      def marshal_metadata
        credential.merge(
          :options => Streaming.serializable_options(@options),
          :context => {
            :namespaces => (@context || {})[:namespaces],
            :xml_space => (@context || {})[:xml_space]
          },
          :delivered => @delivered,
          :acknowledged => @acknowledged,
          :status => @status
        )
      end
    end

    Receipt = Record
  end
end
