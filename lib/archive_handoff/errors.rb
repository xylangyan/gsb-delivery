# frozen_string_literal: true

module ArchiveHandoff
  class Error < StandardError; end

  class NotFound < Error; end
  class AlreadyRunning < Error; end
  class ManifestError < Error; end
  class DigestMismatch < Error; end
  class CommandFailed < Error
    attr_reader :result

    def initialize(message, result = nil)
      super(message)
      @result = result
    end
  end

  class OutcomeUnknown < Error; end
  class SettlementResponseLost < OutcomeUnknown; end
  class SubmissionUncertain < OutcomeUnknown; end
end
