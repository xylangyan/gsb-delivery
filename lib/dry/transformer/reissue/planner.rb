# frozen_string_literal: true

require "set"

module Dry
  module Transformer
    module Reissue
      # Reconciliation engine for assembly orders.
      #
      # Ingredient lists are attached first with
      # {Dry::Transformer::ArrayTransformations#combine}; quality procedures are
      # then attached in a second combine pass, keyed by the ordered
      # fingerprint of the materialized ingredient list. Every patch batch
      # (inserts, withdrawals, reorderings and uniform rule segments) produces
      # one new immutable snapshot, so previous versions stay fully readable
      # and unchanged source keys keep sharing their previously frozen list.
      #
      # @api public
      class Planner
        # @return [Array<Snapshot>]
        attr_reader :snapshots

        # @return [BatchReport, nil] result of the latest patch batch
        attr_reader :last_report

        # @api private
        DEFAULTS = {
          order_number_key: :order_no,
          source_key: :source_key,
          code_key: :code,
          seq_key: :seq,
          list_key: :ingredients,
          procedure_key: :procedures,
          fingerprint_key: :fingerprint,
          source_id_key: :source_id
        }.freeze

        # @param orders [Array<Hash>] the order table
        # @param ingredients [Array<Hash>] master ingredient rows
        # @param procedures [Array<Hash>] procedure rows, each carrying the
        #   ordered fingerprint it binds to and a provenance source id
        # @param config [Hash] column-name overrides, see {DEFAULTS}
        #
        # @api public
        def initialize(orders:, ingredients:, procedures:, **config)
          @config = DEFAULTS.merge(config)
          @orders = orders.map(&:dup)
          @master_ingredients = ingredients.map(&:dup)
          @procedure_rows = procedures.map(&:dup)
          @snapshots = []
          @batch_log = []
          @key_index = build_key_index
          @order_index = @orders.to_h { |order| [order.fetch(cfg(:order_number_key)), order] }
          @order_numbers = @order_index.keys.sort
          @procedure_catalog = build_procedure_catalog
          bootstrap
        end

        # Applies one atomic patch batch and reconciles the ledger.
        #
        # @param inserts [Array<Hash>] ingredient rows appended to their key
        # @param withdraws [Array<Hash>] ingredient rows identified by code
        # @param reorders [Hash] source key => ordered codes or explicit rows
        # @param rule [Hash, nil] +{name:, sequences:, transform:}+ describing
        #   a uniform rule over order-number ranges/arithmetic sequences
        #
        # @return [BatchReport]
        #
        # @api public
        def apply_batch(inserts: [], withdraws: [], reorders: {}, rule: nil)
          @batch_log << {
            inserts: deep_dup(inserts),
            withdraws: deep_dup(withdraws),
            reorders: deep_dup(reorders),
            rule: rule
          }
          snapshot = Snapshot.new(@snapshots.size, current_snapshot)

          apply_explicit_patches(snapshot, inserts, withdraws, reorders)
          register_rule(snapshot, rule)

          @snapshots << snapshot
          @last_report = reconcile(snapshot)
        end

        # Full reissue ranges of the latest batch.
        #
        # @return [Array<Range, Enumerator::ArithmeticSequence>]
        #
        # @api public
        def reissue_ranges
          @last_report&.ranges || []
        end

        # Order numbers that must be re-sent, in sorted order. Rule-only
        # segments are expanded here on demand; the report keeps them
        # compressed internally.
        #
        # @return [Array<Integer>]
        #
        # @api public
        def changed_order_numbers
          @last_report&.all_numbers || []
        end

        # Fetches the actual old/new ingredient lists and procedure provenance
        # for one order number across the last two snapshots.
        #
        # @param order_number [Integer]
        #
        # @return [Hash]
        #
        # @api public
        def diff_for(order_number)
          order = fetch_order(order_number)
          key = order[cfg(:source_key)]
          new_snapshot = current_snapshot
          old_snapshot = new_snapshot.parent

          build_diff(order_number, key, old_snapshot, new_snapshot)
        end

        # Streams diff records for the orders inside a subrange without
        # expanding the whole ledger.
        #
        # @param sequence [Range, Enumerator::ArithmeticSequence]
        #
        # @return [Enumerator<Hash>]
        #
        # @api public
        def each_diff(sequence)
          return enum_for(:each_diff, sequence) unless block_given?

          new_snapshot = current_snapshot
          old_snapshot = new_snapshot.parent
          each_order_in(sequence) do |order|
            number = order.fetch(cfg(:order_number_key))
            yield build_diff(number, order[cfg(:source_key)], old_snapshot, new_snapshot)
          end
        end

        # Frozen ordered ingredient list attached to one order at a
        # generation. Orders sharing a source key share the same frozen
        # object, including across snapshots that did not touch that key.
        #
        # @param order_number [Integer]
        # @param generation [Integer] snapshot generation, defaults to latest
        #
        # @return [Array<Hash>]
        #
        # @api public
        def ingredient_list_for(order_number, generation: nil)
          order = fetch_order(order_number)
          snapshot_at(generation).list_for(order[cfg(:source_key)], order_number)
        end

        # Materializes a single order as attached by both combine passes at a
        # given snapshot generation.
        #
        # @param order_number [Integer]
        # @param generation [Integer] snapshot generation, defaults to latest
        #
        # @return [Hash]
        #
        # @api public
        def record_for(order_number, generation: nil)
          attach_record(fetch_order(order_number), snapshot_at(generation))
        end

        # @return [Array<Hash>] the fully attached order table at a generation
        #
        # @api public
        def materialize(generation: nil)
          snapshot = snapshot_at(generation)
          @orders.map { |order| attach_record(order, snapshot) }
        end

        # Rebuilds the ledger from the original master data by replaying every
        # batch, then double-merges the whole table and compares it against the
        # snapshot chain. Intended for small ledgers.
        #
        # @raise [VerificationError]
        #
        # @return [Symbol] +:verified+
        #
        # @api public
        def verify!
          replay = self.class.new(
            orders: deep_dup(@orders),
            ingredients: deep_dup(@master_ingredients),
            procedures: deep_dup(@procedure_rows),
            **@config
          )
          @batch_log.each { |batch| replay.apply_batch(**batch) }

          @snapshots.each_with_index do |_, generation|
            @orders.each do |order|
              number = order.fetch(cfg(:order_number_key))
              expected = record_for(number, generation: generation)
              actual = replay.record_for(number, generation: generation)
              next if expected == actual

              raise Reissue::VerificationError,
                    "double-merge mismatch for order #{number} at generation #{generation}"
            end
          end
          :verified
        end

        # @api private
        def cfg(name)
          @config.fetch(name)
        end

        # @api private
        def fingerprint(list)
          list.map { |row| row[cfg(:code_key)] }
        end

        # @api private
        def normalize(rows, key = nil)
          rows.map(&:dup).each_with_index do |row, index|
            row[cfg(:source_key)] = key if key
            row[cfg(:seq_key)] = index
          end
        end

        # @api private
        def procedures_for(list)
          @procedure_catalog
            .fetch(fingerprint(list), ArrayTransformations::Combine::EMPTY_ARRAY.dup)
            .map(&:dup)
        end

        private

        def bootstrap
          grouped = @master_ingredients.group_by { |row| row.fetch(cfg(:source_key)) }
          base_loader = ->(key) { normalize(grouped.fetch(key, []), key) }
          @snapshots << Snapshot.new(0, nil, base_loader: base_loader)
          @last_report = nil
        end

        def current_snapshot
          @snapshots.last
        end

        def snapshot_at(generation)
          return current_snapshot if generation.nil?

          @snapshots.fetch(generation)
        end

        def apply_explicit_patches(snapshot, inserts, withdraws, reorders)
          inserts.group_by { |row| row.fetch(cfg(:source_key)) }.each do |key, rows|
            parent = snapshot.parent.list_for(key, representative_number(key)).map(&:dup)
            snapshot.store_list(key, normalize(parent + rows.map(&:dup), key))
            snapshot.mark_changed(key)
          end

          withdraws.group_by { |row| row.fetch(cfg(:source_key)) }.each do |key, rows|
            parent = snapshot.parent.list_for(key, representative_number(key)).map(&:dup)
            removed = rows.map { |row| row.fetch(cfg(:code_key)) }
            ensure_codes_present!(key, parent, removed)
            remaining = parent.reject { |row| removed.include?(row[cfg(:code_key)]) }
            snapshot.store_list(key, normalize(remaining, key))
            snapshot.mark_changed(key)
          end

          reorders.each do |key, new_order|
            parent = snapshot.parent.list_for(key, representative_number(key)).map(&:dup)
            snapshot.store_list(key, normalize(reorder_rows(parent, key, new_order), key))
            snapshot.mark_changed(key)
          end
        end

        # @api private
        def representative_number(key)
          @key_index.fetch(key, []).min
        end

        def reorder_rows(parent, key, new_order)
          return new_order.map(&:dup) if new_order.all? { |item| item.is_a?(Hash) }

          by_code = parent.to_h { |row| [row[cfg(:code_key)], row] }
          new_order.map do |code|
            by_code.fetch(code) do
              raise Reissue::Error,
                    "cannot reorder key #{key.inspect}: unknown code #{code.inspect}"
            end
          end
        end

        def ensure_codes_present!(key, parent, removed)
          missing = removed - parent.map { |row| row[cfg(:code_key)] }
          return if missing.empty?

          raise Reissue::Error,
                "cannot withdraw from key #{key.inspect}: missing codes #{missing.inspect}"
        end

        def register_rule(snapshot, rule)
          return if rule.nil?

          snapshot.store_rule_segment(
            RuleSegment.new(
              rule.fetch(:name),
              Array(rule.fetch(:sequences)),
              rule.fetch(:transform)
            )
          )
        end

        def reconcile(snapshot)
          explicit_numbers = Set.new

          # Explicit patches are checked once per source key; a shared key
          # expands to every order carrying it.
          snapshot.changed_keys.each do |key|
            numbers = @key_index.fetch(key, [])
            next if numbers.empty?

            sample_number = numbers.first
            old_list = snapshot.parent.list_for(key, sample_number)
            new_list = snapshot.list_for(key, sample_number)
            next if fingerprint(old_list) == fingerprint(new_list) &&
                    provenance(old_list) == provenance(new_list)

            explicit_numbers.merge(numbers)
          end

          # Rule segments already name the exact order sequences their patch
          # applies to, so they continue in bulk and stay unexpanded; actual
          # lists remain lazy and are only built for queried orders.
          rule_sequences = snapshot.rule_segments.flat_map(&:number_sequences)

          ordered_numbers = explicit_numbers.to_a.sort
          ranges = RangeCompressor.compress(ordered_numbers) +
                   rule_sequences.sort_by(&:begin)

          BatchReport.new(
            generation: snapshot.generation,
            changed_numbers: ordered_numbers,
            ranges: ranges
          )
        end

        def provenance(list)
          procedures_for(list).map { |row| row[cfg(:source_id_key)] }
        end

        def build_diff(order_number, key, old_snapshot, new_snapshot)
          old_list = old_snapshot ? old_snapshot.list_for(key, order_number) : Snapshot::EMPTY_LIST
          new_list = new_snapshot.list_for(key, order_number)
          {
            order_number: order_number,
            source_key: key,
            rule: new_snapshot.rule_name_for(key, order_number),
            old_list: old_list.map(&:dup),
            new_list: new_list.map(&:dup),
            old_procedures: procedures_for(old_list),
            new_procedures: procedures_for(new_list),
            changed: fingerprint(old_list) != fingerprint(new_list) ||
                       provenance(old_list) != provenance(new_list)
          }
        end

        # Two-pass combine: ingredients first, procedures second, the second
        # pass keyed by the ordered fingerprint materialized by the first.
        def attach_record(order, snapshot)
          number = order.fetch(cfg(:order_number_key))
          key = order[cfg(:source_key)]
          list = snapshot.list_for(key, number)

          ingredient_rows = list.map { |row| row.dup.merge(cfg(:source_key) => key) }
          ingredient_mapping = [[cfg(:list_key), {cfg(:source_key) => cfg(:source_key)}]]
          first_pass = ArrayTransformations.combine(
            [[order.dup], [ingredient_rows]], ingredient_mapping
          ).first

          first_pass[cfg(:fingerprint_key)] = fingerprint(list)

          procedure_mapping = [[cfg(:procedure_key),
                                {cfg(:fingerprint_key) => cfg(:fingerprint_key)}]]
          ArrayTransformations.combine(
            [[first_pass], [@procedure_rows.map(&:dup)]], procedure_mapping
          ).first
        end

        def each_order_in(sequence)
          start = @order_numbers.bsearch_index { |number| number >= sequence.begin }
          return if start.nil?

          index = start
          while index < @order_numbers.size && @order_numbers[index] <= sequence.end
            number = @order_numbers[index]
            yield @order_index.fetch(number) if sequence.include?(number)
            index += 1
          end
        end

        def fetch_order(order_number)
          @order_index.fetch(order_number) do
            raise Reissue::Error, "unknown order number #{order_number.inspect}"
          end
        end

        def build_key_index
          index = Hash.new { |hash, key| hash[key] = [] }
          @orders.each do |order|
            index[order.fetch(cfg(:source_key))] << order.fetch(cfg(:order_number_key))
          end
          index
        end

        def build_procedure_catalog
          @procedure_rows.group_by { |row| row.fetch(cfg(:fingerprint_key)) }
        end

        def deep_dup(value)
          case value
          when Hash then value.transform_values { |item| deep_dup(item) }
          when Array then value.map { |item| deep_dup(item) }
          else value
          end
        end
      end
    end
  end
end
