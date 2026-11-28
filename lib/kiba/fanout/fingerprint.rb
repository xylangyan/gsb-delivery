require "digest"

module Kiba
  module Fanout
    # Content fingerprint of a logical detail node.
    #
    # The copy ledger pairs this with object identity. Re-delivering the same
    # registered object unchanged therefore reuses a version; mutating it
    # upstream produces a new fingerprint and a new version.
    module Fingerprint
      module_function

      def for(value)
        Digest::SHA256.hexdigest(canonical(value))
      end

      def canonical(value)
        case value
        when Hash
          "h{" + value.map { |key, child| "#{canonical(key)}=#{canonical(child)}" }.join(",") + "}"
        when Array
          "a[" + value.map { |child| canonical(child) }.join(",") + "]"
        when String
          "s#{value.bytesize}:#{value}"
        when Symbol
          "y:#{value}"
        when Integer, Float, Rational
          "n:#{value.class}:#{value}"
        when NilClass, TrueClass, FalseClass
          value.inspect
        else
          "o:#{value.class}:#{value.object_id}"
        end
      end
    end
  end
end
