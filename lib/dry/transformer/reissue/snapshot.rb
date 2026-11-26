# frozen_string_literal: true

require "set"

module Dry
  module Transformer
    module Reissue
      # One immutable materialization of the ledger.
      #
      # Explicit patch results and rule segments live here; anything not
      # touched by a snapshot is resolved through its parent, so previous
      # versions stay fully readable after new batches arrive and unchanged
      # source keys keep sharing the list frozen by an older snapshot.
      #
      # @api private
      class Snapshot
        EMPTY_LIST = [].freeze

        # @return [Integer] zero-based generation, bumped on every patch batch
        attr_reader :generation

        # @return [Snapshot, nil]
        attr_reader :parent

        # @return [Array<RuleSegment>] rule segments registered by this batch
        attr_reader :rule_segments

        # @api private
        def initialize(generation, parent, base_loader: nil)
          @generation = generation
          @parent = parent
          # source_key => frozen, explicitly materialized ingredient list
          @lists = {}
          @rule_segments = []
          # source keys whose list or procedure provenance changed here
          @changed_keys = Set.new
          # lazily materialized rule results, keyed by [source key, order no]
          @rule_memo = {}
          # lazily materialized master lists on the generation-zero snapshot
          @base_memo = {}
          @base_loader = base_loader
        end

        # @api private
        def store_list(key, rows)
          @lists[key] = rows.freeze
          @rule_memo.clear
        end

        # @api private
        def store_rule_segment(segment)
          @rule_segments << segment
        end

        # @api private
        def mark_changed(key)
          @changed_keys << key
        end

        # @return [Array<Object>] source keys changed by this snapshot only
        #
        # @api private
        def changed_keys
          @changed_keys.to_a
        end

        # Resolves the ordered ingredient list for a source key.
        #
        # Rule segments select by order number, so memoization includes the
        # order number as well; this keeps shared source keys honest when a
        # segment covers only some of the orders carrying one key.
        #
        # @return [Array<Hash>] frozen list of ingredient rows
        #
        # @api private
        def list_for(key, order_number)
          return @lists[key] if @lists.key?(key)

          segment = @rule_segments.find { |item| item.cover?(order_number) }
          if segment
            @rule_memo[[key, order_number]] ||=
              segment.apply(parent_list_for(key, order_number), key).freeze
          elsif @parent
            @parent.list_for(key, order_number)
          elsif @base_loader
            @base_memo[key] ||= @base_loader.call(key).freeze
          else
            EMPTY_LIST
          end
        end

        # @api private
        def rule_name_for(key, order_number)
          segment = @rule_segments.find { |item| item.cover?(order_number) }
          return segment.name if segment

          @parent&.rule_name_for(key, order_number)
        end

        private

        def parent_list_for(key, order_number)
          @parent ? @parent.list_for(key, order_number).map(&:dup) : []
        end
      end
    end
  end
end
