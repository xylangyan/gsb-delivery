# frozen_string_literal: true

require 'stringio'

module Ridgepole
  # Plans renames and cross-schema moves of PostgreSQL tables together with
  # the dependent objects that travel with them (indexes and column-owned
  # sequences), and emits an auditable SQL plan.
  #
  # The planner works from a catalog listing (current names and object
  # identities) plus a source -> target identity mapping. It guarantees:
  #
  # * every statement is preceded by a check that the target name is free
  # * rows, foreign keys, indexes and sequences keep their original objects
  #   (objects are only renamed / moved, never dropped and recreated)
  # * objects that do not take part in the migration are never renamed
  # * dependent objects are temporarily renamed only when a rename cycle
  #   cannot be resolved otherwise; the plan minimizes the number of
  #   dependent objects that need a temporary name
  # * the final catalog is verified against the original object identities,
  #   sequence owning columns and foreign key targets
  module RenamePlanner
    class ConflictError < StandardError
      attr_reader :conflicts

      def initialize(message, conflicts = [])
        @conflicts = conflicts
        super(message)
      end
    end

    module_function

    def symbolize_keys(obj)
      case obj
      when Hash
        obj.each_with_object({}) { |(k, v), h| h[k.to_sym] = symbolize_keys(v) }
      when Array
        obj.map { |v| symbolize_keys(v) }
      else
        obj
      end
    end

    def plan(catalog, moves)
      Planner.new(catalog, moves).plan
    end
  end
end

require 'ridgepole/rename_planner/catalog'
require 'ridgepole/rename_planner/plan'
require 'ridgepole/rename_planner/planner'
