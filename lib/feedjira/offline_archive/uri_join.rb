# frozen_string_literal: true

require "uri"

module Feedjira
  module OfflineArchive
    # Resolves references against +xml:base+ chains.
    #
    # Ruby's URI.join implements RFC 3986 section 5 for absolute bases, but it
    # raises on some inputs and cannot apply a chain of bases. This wrapper adds
    # those two conveniences and falls back to returning the reference unchanged
    # when it is already absolute or cannot be resolved.
    module UriJoin
      SCHEME = %r{\A[A-Za-z][A-Za-z0-9+\-.]*:}.freeze

      module_function

      def resolve(base, reference)
        reference = normalise(reference)
        return reference if base.nil? || blank?(base)
        return reference if reference.match?(SCHEME)

        URI.join(normalise(base), reference).to_s
      rescue URI::Error
        reference
      end

      # Applies a chain of bases, outermost first. Each element is resolved
      # against the accumulated base; a blank element leaves the base alone.
      def resolve_chain(bases, reference)
        base = Array(bases).reject { |value| blank?(value) }.reduce(nil) do |memo, value|
          memo.nil? ? normalise(value) : resolve(memo, value)
        end
        resolve(base, reference)
      end

      def normalise(value)
        value.to_s.strip
      end

      def blank?(value)
        value.nil? || normalise(value).empty?
      end
    end
  end
end
