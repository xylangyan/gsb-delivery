module Geocoder
  module Replay
    class Error < StandardError; end
    class InputError < Error; end

    ##
    # Raised when a replay reaches a point that the recorded material cannot
    # explain: a cache miss with no recorded response, a response for another
    # query, a body that does not contain the requested candidate, and so on.
    #
    class Violation < Error
      attr_reader :kind, :details

      def initialize(kind, message, details = {})
        @kind = kind.to_sym
        @details = details
        super(message)
      end

      def to_h
        { kind: kind, message: message }.merge(details)
      end
    end
  end
end
