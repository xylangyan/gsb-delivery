require "dotenv/missing_keys"

module Dotenv
  module Handover
    # Base error for handover rehearsal problems.
    class Error < Dotenv::Error; end

    # Raised when the manifest itself cannot be understood.
    class ManifestError < Error; end

    # Raised when a file named by the manifest cannot be resolved.
    class FileError < Error; end

    # Raised when a route loops or otherwise cannot be replayed.
    class ReplayError < Error; end
  end
end
