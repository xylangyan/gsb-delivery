class Nori
  module Streaming
    class Error < StandardError; end

    # Raised when the bytes received so far are not a well-formed XML
    # document and cannot become one by appending more data.
    class ParseError < Error
      attr_reader :byte_offset

      def initialize(message, byte_offset = nil)
        @byte_offset = byte_offset
        suffix = byte_offset ? " at byte #{byte_offset}" : ""
        super("#{message}#{suffix}")
      end
    end

    # Raised when a caller acknowledges a receipt that the ledger does not
    # know. Late acknowledgements for known receipts are idempotent.
    class UnknownReceipt < Error; end

    # Raised when a caller tries to feed bytes that do not continue the
    # retained stream. The receiver never guesses across a gap.
    class OffsetError < Error; end
  end
end
