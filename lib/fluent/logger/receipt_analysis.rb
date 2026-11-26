# frozen_string_literal: true

require_relative 'receipt_analysis/model'
require_relative 'receipt_analysis/fixture'
require_relative 'receipt_analysis/receipt'
require_relative 'receipt_analysis/simulator'
require_relative 'receipt_analysis/archive'
require_relative 'receipt_analysis/analysis'
module Fluent
  module Logger
    # Receipt analysis for FluentLogger archives.
    #
    # Selects the integer buffer_limit values under which a sender, driven by a
    # recorded message/clock/connection/write archive and a local fixture rule
    # table, satisfies a signed-off receipt (签收单). See
    # lib/fluent/logger/receipt_analysis/README.md for the archive format.
    module ReceiptAnalysis
      module_function

      def load(path)
        Archive.from_json_file(path)
      end

      def analyze(archive_or_hash)
        archive = archive_or_hash.is_a?(Archive) ? archive_or_hash : Archive.from_h(archive_or_hash)
        Analysis.new(archive)
      end
    end
  end
end
