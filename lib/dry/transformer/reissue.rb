# frozen_string_literal: true

module Dry
  module Transformer
    # Reissue reconciliation built on top of ArrayTransformations#combine.
    #
    # The workflow mirrors the warehouse assembly process:
    #
    # 1. Ordered ingredient lists are attached to orders with
    #    {Dry::Transformer::ArrayTransformations#combine}.
    # 2. Quality procedures are attached by the exact ordered ingredient
    #    list (its fingerprint), so lists with the same rows in a different
    #    order never resolve to the same procedures.
    # 3. Patch batches (inserts, withdrawals, reorderings) are applied
    #    atomically and the order numbers that must be re-sent are compressed
    #    into contiguous ranges or equal-step arithmetic sequences.
    #
    # @api public
    module Reissue
      Error = Class.new(Dry::Transformer::Error)

      # Raised when a full double-merge verification of the ledger fails.
      #
      # @api public
      VerificationError = Class.new(Error)
    end
  end
end
