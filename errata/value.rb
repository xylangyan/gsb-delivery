# frozen_string_literal: true

module Errata
  # JSON has no symbols: a leading ":" denotes a symbol category, numeric
  # strings become integers and every other string stays a string.
  def self.normalize_value(value)
    return value unless value.is_a?(String)

    return value[1..].to_sym if value.start_with?(':')

    value.match?(/\A-?\d+\z/) ? Integer(value) : value
  end
end
