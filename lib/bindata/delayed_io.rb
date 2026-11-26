require 'bindata/base'
require 'bindata/dsl'
require 'bindata/preview'

module BinData
  # BinData declarations are evaluated in a single pass.
  # However, some binary formats require multi pass processing.  A common
  # reason is seeking backwards in the input stream.
  #
  # DelayedIO supports multi pass processing.  It works by ignoring the normal
  # #read or #write calls.  The user must explicitly call the #read_now! or
  # #write_now! methods to process an additional pass.  This additional pass
  # must specify the abs_offset of the I/O operation.
  #
  #   require 'bindata'
  #
  #   obj = BinData::DelayedIO.new(read_abs_offset: 3, type: :uint16be)
  #   obj.read("\x00\x00\x00\x11\x12")
  #   obj #=> 0
  #
  #   obj.read_now!
  #   obj #=> 0x1112
  #
  #   - OR -
  #
  #   obj.read("\x00\x00\x00\x11\x12") { obj.read_now! } #=> 0x1122
  #
  #   obj.to_binary_s { obj.write_now! } #=> "\x00\x00\x00\x11\x12"
  #
  # You can use the +auto_call_delayed_io+ keyword to cause #read and #write to
  # automatically perform the extra passes.
  #
  #   class ReversePascalString < BinData::Record
  #     auto_call_delayed_io
  #
  #     delayed_io :str, read_abs_offset: 0 do
  #       string read_length: :len
  #     end
  #     count_bytes_remaining :total_size
  #     skip to_abs_offset: -> { total_size - 1 }
  #     uint8  :len, value: -> { str.length }
  #   end
  #
  #   s = ReversePascalString.read("hello\x05")
  #   s.to_binary_s #=> "hello\x05"
  #
  # == Previewing
  #
  # A record that uses +auto_call_delayed_io+ can lay out a preview of what it
  # would write without touching the real output.  This is useful for checking
  # the bytes that the delayed passes would place in the stream.
  #
  #   preview = record.preview
  #   preview.num_bytes     #=> logical length of the previewed volume
  #   preview.read(10, 4)   #=> bytes at offsets 10..13 (repeatable)
  #
  # A preview is a snapshot.  Changing the record, laying out another preview,
  # or patching the real volume with #write_now! does not change an existing
  # preview.  Unwritten regions read as zero and are kept sparse, so a volume
  # with a very large offset can be previewed without materialising the gap.
  #
  # The preview follows the +choice+ branch that is actually written and only
  # back-fills delayed_io objects that are connected to the output and not yet
  # written.  Each object is evaluated with its +onlyif+ and current
  # +abs_offset+ at back-fill time.  Inner delayed_io objects that only become
  # connected while back-filling a struct take part in the same back-fill.
  #
  #
  # == Parameters
  #
  # Parameters may be provided at initialisation to control the behaviour of
  # an object.  These params are:
  #
  # <tt>:read_abs_offset</tt>::   The abs_offset to start reading at.
  # <tt>:type</tt>::              The single type inside the delayed io.  Use
  #                               a struct if multiple fields are required.
  class DelayedIO < BinData::Base
    extend DSLMixin

    dsl_parser    :delayed_io
    arg_processor :delayed_io

    mandatory_parameters :read_abs_offset, :type

    def initialize_instance
      @type            = get_parameter(:type).instantiate(nil, self)
      @abs_offset      = nil
      @read_io         = nil
      @write_io        = nil
      @has_write       = false
      @preview_session = nil
    end

    def clear?
      @type.clear?
    end

    def assign(val)
      @type.assign(val)
    end

    def snapshot
      @type.snapshot
    end

    def num_bytes
      @type.num_bytes
    end

    def respond_to_missing?(symbol, include_all = false) # :nodoc:
      @type.respond_to?(symbol, include_all) || super
    end

    def method_missing(...) # :nodoc:
      @type.__send__(...)
    end

    def abs_offset
      @abs_offset || eval_parameter(:read_abs_offset)
    end

    # Sets the +abs_offset+ to use when writing this object.
    def abs_offset=(offset)
      @abs_offset = offset
    end

    def rel_offset
      abs_offset
    end

    def do_read(io) # :nodoc:
      @read_io = io
    end

    def do_write(io) # :nodoc:
      if io.respond_to?(:preview_session) && io.preview_session
        # A preview only connects this object if :onlyif allows it.  It must
        # never disturb the real output, so the real write state is untouched.
        if include_obj?
          @preview_session = io.preview_session
        end
      else
        @write_io  = io
        @has_write = true
      end
    end

    def do_num_bytes # :nodoc:
      0
    end

    def include_obj?
      return true unless has_parameter?(:onlyif)

      condition = eval_parameter(:onlyif)
      condition = condition.snapshot if BinData::Base === condition
      condition ? true : false
    end

    # DelayedIO objects aren't read when #read is called.
    # The reading is delayed until this method is called.
    def read_now!
      return unless include_obj?
      raise IOError, "read from where?" unless @read_io

      @read_io.seek_to_abs_offset(abs_offset)
      start_read do
        @type.do_read(@read_io)
      end
    end

    # DelayedIO objects aren't written when #write is called.
    # The writing is delayed until this method is called.
    def write_now!
      return unless include_obj?
      raise IOError, "write to where?" unless @has_write

      @write_io.seek_to_abs_offset(abs_offset)
      @type.do_write(@write_io)
    end

    # Was this object connected to the output of +session+'s preview?
    def preview_connected_to?(session)
      @preview_session.equal?(session)
    end

    # Back-fills this object into +session+'s output.  Writing the inner type
    # connects any inner DelayedIO objects so that they can participate in the
    # same back-fill.
    def preview_write!(session)
      return unless include_obj?

      offset = abs_offset
      raise PreviewError.new(self, offset) if offset.negative?

      session.io.seek_to_abs_offset(offset)
      @type.do_write(session.io)
      session.io.flush
    end
  end

  class DelayedIoArgProcessor < BaseArgProcessor
    include MultiFieldArgSeparator

    def sanitize_parameters!(obj_class, params)
      params.merge_dsl_params
      params.must_be_integer(:read_abs_offset)
      params.sanitize_object_prototype(:type)
    end
  end

  class Base
    # Add +auto_call_delayed_io+ keyword to BinData::Base.
    class << self
      # The +auto_call_delayed_io+ keyword sets a data object tree to perform
      # multi pass I/O automatically.
      def auto_call_delayed_io
        include AutoCallDelayedIO

        return if DelayedIO.method_defined? :initialize_instance_without_record_io

        DelayedIO.send(:alias_method, :initialize_instance_without_record_io, :initialize_instance)
        DelayedIO.send(:define_method, :initialize_instance) do
          if @parent && !defined? @delayed_io_recorded
            @delayed_io_recorded = true
            list = top_level_get(:delayed_ios)
            list << self if list
          end

          initialize_instance_without_record_io
        end
      end
    end

    module AutoCallDelayedIO
      def initialize_shared_instance
        top_level_set(:delayed_ios, [])
        super
      end

      def read(io)
        super(io) { top_level_get(:delayed_ios).each(&:read_now!) }
      end

      def write(io, *_)
        super(io) { top_level_get(:delayed_ios).each(&:write_now!) }
      end

      # Lays out a preview of what this record would write without writing
      # anything to the real output.
      #
      # Returns a BinData::Preview, which knows the logical length of the
      # previewed volume and can repeatedly read byte windows from it.
      #
      # A preview follows the choice branch that is actually written this
      # time and only back-fills delayed_io objects that are connected to the
      # output and not yet written.  It never modifies the real output, so an
      # existing DelayedIO object can still patch the real volume with
      # #write_now!.
      def preview
        session = PreviewSession.new

        session.visit(self)
        do_write(session.io)
        session.io.flush
        preview_flush_delayed_ios(session)

        BinData::Preview.new(session.volume.snapshot,
                             session.volume.logical_length)
      end
      alias to_preview preview
      alias preview_write preview

      def num_bytes
        to_binary_s.size
      end

      private

      def preview_flush_delayed_ios(session)
        loop do
          progressed = false

          top_level_get(:delayed_ios).dup.each do |delayed_io|
            next if session.visited?(delayed_io)
            next unless delayed_io.preview_connected_to?(session)

            session.visit(delayed_io)
            delayed_io.preview_write!(session)
            progressed = true
          end

          break unless progressed
        end
      end
    end
  end
end
