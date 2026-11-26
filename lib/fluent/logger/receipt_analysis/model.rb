# frozen_string_literal: true

require 'msgpack'
require 'time'
require 'json'

module Fluent
  module Logger
    module ReceiptAnalysis
      DEFAULT_IDENTIFIER_WIDTH = 12

      class Error < StandardError; end

      # Packs and unpacks fluent forward frames ([tag, time, record]).
      module Packing
        module_function

        def pack(tuple)
          MessagePack::Factory.new.packer.pack(tuple).to_s
        end

        # Returns the list of tuples whose bytes are fully present in +bytes+.
        def complete_messages(bytes)
          return [] if bytes.nil? || bytes.empty?
          unpacker = MessagePack::Unpacker.new
          unpacker.feed(bytes)
          tuples = []
          unpacker.each { |tuple| tuples << tuple }
          tuples
        end
      end

      class Message
        attr_reader :number, :tag, :time, :record

        def initialize(number:, tag:, time: 0, record: {}, identifier_format: nil)
          @number = number
          @tag = tag
          @time = Integer(time)
          @record = record || {}
          @identifier_format = identifier_format
        end

        def identifier(width = DEFAULT_IDENTIFIER_WIDTH)
          if @identifier_format
            @identifier_format.call(@number)
          else
            @number.to_s.rjust(width, '0')
          end
        end

        def tuple
          [@tag, @time, @record]
        end

        def packed(width = DEFAULT_IDENTIFIER_WIDTH)
          Packing.pack(tuple)
        end

        def bytesize(width = DEFAULT_IDENTIFIER_WIDTH)
          packed(width).bytesize
        end

        def ==(other)
          other.is_a?(Message) && other.number == @number && other.tag == @tag &&
            other.time == @time && other.record == @record
        end
        alias eql? ==

        def hash
          [@number, @tag, @time, @record].hash
        end
      end

      # A single call the driver made against the native sender.
      class Operation
        attr_reader :kind, :time, :number, :connection, :outcome
        attr_accessor :message

        def initialize(kind:, time:, number: nil, connection: nil, outcome: :ok, message: nil)
          @kind = kind
          @time = time
          @number = number
          @connection = connection
          @outcome = outcome
          @message = message
        end

        def resolved_message
          @message
        end

        def self.post(time, number, message: nil)
          new(kind: :post, time: time, number: number, message: message)
        end

        def self.close(time)
          new(kind: :close, time: time)
        end

        def self.connect(time, connection)
          new(kind: :connect, time: time, connection: connection)
        end

        def self.write(time, connection: nil, outcome: :ok)
          new(kind: :write, time: time, connection: connection, outcome: outcome)
        end

        def self.disconnect(time, connection: nil, outcome: :drop)
          new(kind: :disconnect, time: time, connection: connection, outcome: outcome)
        end

        def post?
          kind == :post
        end

        def to_h
          { 'kind' => kind.to_s, 'time' => time, 'number' => number,
            'connection' => connection, 'outcome' => outcome.to_s }
        end
      end
    end
  end
end
