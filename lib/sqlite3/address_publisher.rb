# frozen_string_literal: true

require "fileutils"
require "set"

module SQLite3
  # Publish a filtered address library after changing the ADDR collation.
  #
  # The publisher is intentionally schema-tolerant: the address table defaults
  # to +address+, and the delivery table and its origin/destination foreign
  # keys are discovered from SQLite's foreign-key metadata when omitted.
  class AddressPublisher
    COLLATION_NAME = "ADDR"

    REASON_INDEX_CONFLICT = "index_conflict"
    REASON_DELIVERY_REFERENCE = "delivery_reference"
    REASON_MANDATORY_SCOPE_DEFERRED = "mandatory_scope_deferred"

    # Result of a publication run. Hash-style access accepts either report
    # metadata (:published_address_ids) or an address primary key.
    class Report
      include Enumerable

      attr_reader :old_path, :output_path, :address_table, :delivery_table
      attr_reader :origin_column, :destination_column, :primary_key
      attr_reader :addresses, :deliveries, :indexes
      attr_reader :scopes, :published_address_ids, :unpublished_address_ids
      attr_reader :published_delivery_ids, :deferred_scopes, :diagnostics

      def initialize(attributes)
        @attributes = attributes
        attributes.each do |key, value|
          instance_variable_set(:"@#{key}", value)
        end
        @diagnostics ||= {}
        @scopes ||= []
        @deferred_scopes = @scopes.select { |scope| scope[:deferred] }
        @published_address_ids ||= @scopes.flat_map { |scope| scope[:selected_address_ids] }.sort
        @unpublished_address_ids ||= @addresses.keys.sort - @published_address_ids
        @published_delivery_ids ||= []
      end

      def [](key)
        address_id = normalize_address_key(key)
        if address_id
          return @diagnostics[address_id] if @diagnostics.key?(address_id)

          scope = @scopes.find { |candidate| candidate[:address_ids].include?(address_id) }
          if scope
            return {
              address_id: address_id,
              published: @published_address_ids.include?(address_id),
              scope_id: scope[:scope_id],
              scope: scope,
              deferred: scope[:deferred]
            }
          end
        end

        if key.is_a?(Symbol) || key.is_a?(String)
          symbol_key = key.to_sym
          return @attributes[symbol_key] if @attributes.key?(symbol_key)
          return to_h[symbol_key] if to_h.key?(symbol_key)
        end
        nil
      end

      def each(&block)
        @diagnostics.each(&block)
      end

      def keys
        @diagnostics.keys
      end

      def key?(key)
        address_id = normalize_address_key(key)
        return true if address_id && @diagnostics.key?(address_id)
        return true if @attributes.key?(key) || @attributes.key?(key.to_s)

        symbol_key = key.is_a?(String) ? key.to_sym : key
        to_h.key?(symbol_key)
      end
      alias_method :has_key?, :key?

      def fetch(key, *args, &block)
        if key?(key)
          self[key]
        elsif block_given?
          yield(key)
        elsif args.length == 1
          args.first
        else
          raise KeyError, "key not found: #{key.inspect}"
        end
      end

      def published?(address_id)
        @published_address_ids.include?(address_id.to_i)
      end

      def deferred?(address_id)
        diagnostic = diagnostics_for(address_id)
        !!diagnostic&.dig(:scope, :deferred)
      end

      def diagnostics_for(address_id)
        key = address_id.is_a?(String) ? Integer(address_id, exception: false) : address_id
        @diagnostics[key.to_i]
      end
      alias_method :info_for, :diagnostics_for
      alias_method :explain, :diagnostics_for

      def reasons_for(address_id)
        diagnostics_for(address_id)&.dig(:reasons) || []
      end

      def reason_for(address_id)
        reasons_for(address_id).first
      end

      def conflicts_for(address_id)
        diagnostics_for(address_id)&.dig(:index_conflicts) || []
      end

      def references_for(address_id)
        diagnostics_for(address_id)&.dig(:delivery_references) || []
      end

      def scope_for(address_id)
        diagnostics_for(address_id)&.dig(:scope) || @scopes.find do |scope|
          scope[:address_ids].include?(address_id.to_i)
        end
      end

      def deferred_scope_for(address_id)
        diagnostics_for(address_id)&.fetch(:scope, nil)
      end

      def unpublished
        @diagnostics
      end

      def unpublished_ids
        @unpublished_address_ids
      end
      alias_method :unpublished_addresses, :unpublished_ids
      alias_method :excluded_address_ids, :unpublished_ids

      def published_addresses
        @published_address_ids
      end
      alias_method :published_ids, :published_address_ids
      alias_method :kept_ids, :published_address_ids
      alias_method :selected_ids, :published_address_ids
      alias_method :ids, :published_address_ids

      def kept_address_ids
        @published_address_ids
      end

      def selected_address_ids
        @published_address_ids
      end

      def delivery_order_ids
        @published_delivery_ids
      end
      alias_method :order_ids, :delivery_order_ids

      def deferred_address_ids
        @deferred_scopes.flat_map { |scope| scope[:address_ids] }.sort
      end

      def deferred
        @deferred_scopes
      end

      def method_missing(name, *args, &block)
        key = name.to_sym
        return @attributes[key] if @attributes.key?(key)
        return @attributes[key.to_s] if @attributes.key?(key.to_s)

        super
      end

      def respond_to_missing?(name, include_private = false)
        @attributes.key?(name.to_sym) || @attributes.key?(name.to_s) || super
      end

      def find_address(address_id)
        @addresses[address_id.to_i]
      end

      def to_h
        {
          old_path: @old_path,
          output_path: @output_path,
          address_table: @address_table,
          delivery_table: @delivery_table,
          origin_column: @origin_column,
          destination_column: @destination_column,
          primary_key: @primary_key,
          indexes: @indexes,
          published: @published_address_ids,
          published_address_ids: @published_address_ids,
          published_ids: @published_address_ids,
          unpublished: @unpublished_address_ids,
          unpublished_ids: @unpublished_address_ids,
          unpublished_address_ids: @unpublished_address_ids,
          unpublished_reports: @diagnostics,
          deferred_address_ids: deferred_address_ids,
          published_delivery_ids: @published_delivery_ids,
          delivery_ids: @published_delivery_ids,
          scopes: @scopes,
          deferred_scopes: @deferred_scopes,
          diagnostics: @diagnostics
        }
      end

      def to_json(*args)
        require "json"
        to_h.to_json(*args)
      end

      private

      def normalize_address_key(key)
        case key
        when Integer then key
        when String
          Integer(key, exception: false)
        end
      end
    end

    class << self
      def publish(*arguments, **keyword_options)
        old_path, output_path, options = parse_arguments(arguments, keyword_options)
        new(old_path, output_path, options).publish
      end

      alias_method :release, :publish
      alias_method :release!, :publish
      alias_method :call, :publish
      alias_method :run, :publish
      alias_method :publish!, :publish

      def normalize_arguments(old_path = nil, output_path = nil, options = {}, keyword_options = {})
        parse_arguments([old_path, output_path, options].compact, keyword_options || {})
      end

      def parse_arguments(arguments, keyword_options)
        options = (keyword_options || {}).dup
        args = Array(arguments).compact

        args.select { |argument| argument.is_a?(Hash) && options_hash?(argument) }.each do |hash|
          options = hash.merge(options)
          args.delete(hash)
        end

        paths = []
        args = args.select do |argument|
          if paths.length < 2 && path_like?(argument)
            paths << argument
            false
          else
            true
          end
        end
        old_path = paths[0]
        output_path = paths[1]

        comparators = []
        if args.first.is_a?(Array) && args.first.length == 2 && args.first.all? { |item| comparator_like?(item) }
          pair = args.shift
          comparators.concat(pair)
        end
        while comparators.length < 2 && comparator_like?(args.first)
          comparators << args.shift
        end
        options[:old_comparator] ||= comparators[0] if comparators[0]
        options[:new_comparator] ||= comparators[1] if comparators[1]

        args.each do |argument|
          case argument
          when Hash
            if weight_hash?(argument)
              options[:weights] ||= argument
            else
              options = argument.merge(options)
            end
          when Array
            if !argument.empty? && argument.all? { |item| id_like?(item) }
              options[:required_ids] ||= argument
            else
              options[:indexes] ||= argument
            end
          when String
            options[:indexes] ||= []
            options[:indexes] = Array(options[:indexes]) + [argument]
          end
        end

        if old_path.nil? || output_path.nil? || options.any?
          old_path ||= option(options, :old_path, :source_path, :source, :old_database_path, :input_path,
                              :old_db_path, :old_db, :source_db, :source_database, :database_path)
          output_path ||= option(options, :output_path, :new_path, :destination_path, :destination, :target_path,
                                 :output_db_path, :new_db_path, :output_db, :new_db, :target_db)
        end

        [old_path, output_path, options]
      end

      def option(options, *names)
        names.each do |name|
          return options[name] if options.key?(name)
          return options[name.to_s] if options.key?(name.to_s)
        end
        nil
      end

      def path_like?(value)
        value.is_a?(String) || value.respond_to?(:to_path)
      end

      def comparator_like?(value)
        value.respond_to?(:compare) || value.respond_to?(:call) ||
          (value.is_a?(Class) && value.method_defined?(:compare))
      end

      def id_like?(value)
        value.is_a?(Integer) || (value.is_a?(String) && value.match?(/\A\d+\z/))
      end

      def weight_hash?(hash)
        return false if hash.empty?

        hash.keys.all? { |key| id_like?(key) } &&
          hash.values.all? { |value| value.is_a?(Integer) && value.positive? }
      end

      def options_hash?(hash)
        hash.keys.any? do |key|
          key.is_a?(Symbol) || (key.is_a?(String) && !id_like?(key))
        end
      end
    end

    def initialize(*arguments, **keyword_options)
      old_path, output_path, options = self.class.parse_arguments(arguments, keyword_options)
      @old_path = path_string(old_path)
      @output_path = path_string(output_path)
      @options = options || {}

      @explicit_address_table = option(:address_table, :address_table_name, :table_name, :table)
      @address_table = (@explicit_address_table || "address").to_s
      @explicit_delivery_table = option(:delivery_table, :delivery_table_name, :orders_table, :shipments_table, :delivery, :deliveries)
      @explicit_origin = option(:origin_column, :source_column, :from_column, :origin_address_column,
                               :origin_foreign_key, :source_foreign_key)
      @explicit_destination = option(:destination_column, :dest_column, :to_column, :destination_address_column,
                                    :destination_foreign_key, :dest_foreign_key)
      @primary_key_option = option(:primary_key, :address_primary_key, :primary_key_column, :id_column)

      @collation_name = option(:collation_name, default: COLLATION_NAME).to_s
      @old_comparator = extract_comparator(:old)
      @new_comparator = extract_comparator(:new)

      raw_indexes = option(
        :indexes, :unique_indexes, :index_definitions, :unique_index_definitions,
        :unique_indices, :target_indexes, :target_index_definitions, :target_unique_indexes,
        :target_unique_index_definitions, :unique_index, :index_definition
      )
      @raw_indexes = normalize_indexes(raw_indexes)

      @weights = normalize_weights(option(:weights, :weight, :address_weights, :weight_by_id, :weights_by_id))
      @required_ids = normalize_ids(
        option(
          :required_ids, :required, :mandatory_ids, :pinned_ids, :retained_ids, :keep_ids, :must_keep_ids,
          :required_address_ids, :mandatory_address_ids, :pinned_address_ids, :retained_address_ids,
          :must_keep_address_ids, :confirmed_ids, :reserved_ids
        ) || []
      )
    end

    def publish
      raise ArgumentError, "old database path is required" if blank?(@old_path)
      raise ArgumentError, "output path is required" if blank?(@output_path)
      if File.expand_path(@output_path) == File.expand_path(@old_path)
        raise ArgumentError, "output path must differ from the read-only old database path"
      end
      raise ArgumentError, "old comparator is required" if @old_comparator.nil?
      raise ArgumentError, "new comparator is required" if @new_comparator.nil?
      raise ArgumentError, "at least one target UNIQUE index definition is required" if @raw_indexes.empty?

      index_specs = @raw_indexes.each_with_index.map { |raw, i| IndexSpec.new(raw, @address_table, i) }

      old_db = SQLite3::Database.new(@old_path, readonly: true)
      begin
        register_comparator(old_db, @collation_name, @old_comparator)
        schema = Schema.new(old_db, @explicit_address_table, @explicit_delivery_table, @explicit_origin, @explicit_destination, @primary_key_option)
        @address_table = schema.address_table
        @primary_key = schema.primary_key
        @delivery_table = schema.delivery_table
        @delivery_primary_key = schema.delivery_primary_key
        @origin_column = schema.origin_column
        @destination_column = schema.destination_column
        index_specs.each { |spec| spec.resolve!(old_db, @address_table) }

        @addresses = schema.read_addresses
        @deliveries = schema.read_deliveries
        validate_inputs!

        warm_old_collation(old_db, index_specs)
      ensure
        old_db.close unless old_db.closed?
      end

      @conflicts = detect_conflicts(index_specs)
      @delivery_edges = @deliveries.map do |delivery|
        {
          type: REASON_DELIVERY_REFERENCE,
          delivery_id: delivery[:id],
          delivery_primary_key: @delivery_primary_key,
          origin_address_id: delivery[:origin_id],
          destination_address_id: delivery[:destination_id]
        }
      end

      components = build_components
      @scopes = components.map { |ids| build_scope(ids) }
      @published_ids = @scopes.reject { |scope| scope[:deferred] }.flat_map { |scope| scope[:selected_address_ids] }.sort
      @selected_delivery_ids = @deliveries.filter_map do |delivery|
        delivery[:id] if @published_ids.include?(delivery[:origin_id])
      end.sort

      write_output(index_specs)
      @diagnostics = build_diagnostics

      Report.new(
        old_path: @old_path,
        output_path: @output_path,
        address_table: @address_table,
        primary_key: @primary_key,
        delivery_table: @delivery_table,
        origin_column: @origin_column,
        destination_column: @destination_column,
        addresses: @addresses,
        deliveries: @deliveries,
        indexes: index_specs.map(&:to_h),
        scopes: @scopes,
        published_address_ids: @published_ids,
        unpublished_address_ids: @addresses.keys.sort - @published_ids,
        published_delivery_ids: @selected_delivery_ids,
        diagnostics: @diagnostics
      )
    end
    alias_method :release, :publish
    alias_method :release!, :publish
    alias_method :publish!, :publish
    alias_method :call, :publish
    alias_method :run, :publish

    private

    def option(*names, default: :__missing__)
      value = self.class.option(@options, *names)
      return value unless value.nil?
      return default unless default == :__missing__
      nil
    end

    def path_string(path)
      return nil if path.nil?
      path.respond_to?(:to_path) ? path.to_path : path.to_s
    end

    def blank?(value)
      value.nil? || value.empty?
    end

    def extract_comparator(version)
      comparator = option(
        :"#{version}_comparator", :"#{version}_collation", :"#{version}_collator",
        :"#{version}_compare", :"#{version}_addr", :"#{version}_collation_comparator"
      )
      return normalize_comparator(comparator) if comparator

      collations = option(:comparators, :collations, :collation, :collator, :collation_objects)
      if collations.is_a?(Hash)
        normalize_comparator(
          self.class.option(collations, version, version.to_s, version.to_sym) ||
          self.class.option(collations[@collation_name] || {}, version, version.to_s) ||
          self.class.option(collations[@collation_name.to_sym] || {}, version, version.to_s)
        )
      end
    end

    def normalize_comparator(comparator)
      return nil if comparator.nil?

      if comparator.is_a?(Class) && comparator.method_defined?(:compare)
        comparator = comparator.new
      end

      return comparator if comparator.respond_to?(:compare)

      if comparator.respond_to?(:call)
        CallableComparator.new(comparator)
      else
        raise ArgumentError, "comparator must respond to #compare or #call"
      end
    end

    class CallableComparator
      def initialize(callable)
        @callable = callable
      end

      def compare(left, right)
        @callable.call(left, right)
      end
    end

    def register_comparator(database, name, comparator)
      database.collation(name, normalize_comparator(comparator))
    end

    def normalize_indexes(indexes)
      case indexes
      when Hash
        indexes.map do |name, definition|
          if definition.is_a?(Hash)
            definition.merge(name: name)
          else
            { name: name, definition: definition }
          end
        end
      when nil
        []
      else
        Array(indexes)
      end
    end

    def normalize_weights(weights)
      case weights
      when nil
        {}
      when Hash
        weights.each_with_object({}) do |(key, value), result|
          id = Integer(key)
          weight = Integer(value)
          raise ArgumentError, "weight for address #{id} must be positive" unless weight.positive?
          result[id] = weight
        end
      else
        weights.each_with_index.each_with_object({}) do |(pair, index), result|
          id, weight = pair
          result[Integer(id || index)] = Integer(weight)
        end
      end
    end

    def normalize_ids(ids)
      Array(ids).map { |id| Integer(id) }.uniq.sort
    end

    def validate_inputs!
      missing_weights = @addresses.keys - @weights.keys
      raise ArgumentError, "missing positive integer weights for address ids: #{missing_weights.join(", ")}" unless missing_weights.empty?

      unknown_required = @required_ids - @addresses.keys
      raise ArgumentError, "required address ids do not exist: #{unknown_required.join(", ")}" unless unknown_required.empty?

      @deliveries.each do |delivery|
        unless @addresses.key?(delivery[:origin_id]) && @addresses.key?(delivery[:destination_id])
          raise ArgumentError, "delivery #{delivery[:id]} references an unknown address"
        end
      end
    end

    def warm_old_collation(old_db, index_specs)
      index_specs.each do |spec|
        next if spec.key_expression.nil?

        old_db.execute("SELECT #{@primary_key} FROM #{quote_identifier(@address_table)} ORDER BY #{spec.key_expression} LIMIT 1").to_a
      rescue SQLite3::SQLException
        # The metadata/order warm-up is advisory; conflict detection and the
        # published indexes still use the exact target DDL.
      end
    end

    def detect_conflicts(index_specs)
      conflicts = []
      ids = @addresses.keys.sort

      index_specs.each do |spec|
        scratch = SQLite3::Database.new(":memory:")
        begin
          register_comparator(scratch, @collation_name, @new_comparator)
          columns = scratch_columns(old_db_columns)
          scratch.execute("CREATE TABLE #{quote_identifier(@address_table)} (#{columns})")
          scratch.execute(spec.scratch_sql(@address_table))

          insert_columns = insertable_columns
          column_names = insert_columns.map { |column| column[:name] }
          sql = "INSERT INTO #{quote_identifier(@address_table)} " \
                "(#{column_names.map { |name| quote_identifier(name) }.join(", ")}) " \
                "VALUES (#{Array.new(column_names.length, "?").join(", ")})"
          statement = scratch.prepare(sql)

          ids.combination(2) do |first_id, second_id|
            conflict = false
            begin
              scratch.execute("SAVEPOINT address_pair")
              statement.execute(bind_values(@addresses[first_id], column_names))
              statement.execute(bind_values(@addresses[second_id], column_names))
            rescue SQLite3::ConstraintException
              conflict = true
            ensure
              scratch.execute("ROLLBACK TO SAVEPOINT address_pair") rescue nil
              scratch.execute("RELEASE SAVEPOINT address_pair") rescue nil
            end

            if conflict
              conflicts << {
                type: REASON_INDEX_CONFLICT,
                address_ids: [first_id, second_id],
                index_name: spec.name,
                index_definition: spec.definition,
                sql: spec.sql
              }
            end
          end
          statement.close
        ensure
          scratch.close
        end
      end

      conflicts
    end

    def old_db_columns
      @column_metadata ||= begin
        db = SQLite3::Database.new(@old_path, readonly: true)
        begin
          register_comparator(db, @collation_name, @old_comparator)
          table = TableMetadata.new(db, @address_table, @primary_key_option)
          table.columns
        ensure
          db.close
        end
      end
    end

    def scratch_columns(columns)
      columns.map do |column|
        type = column[:type].to_s
        "#{quote_identifier(column[:name])} #{type}"
      end.join(", ")
    end

    def insertable_columns
      old_db_columns.reject do |column|
        hidden = column[:hidden].to_i
        [2, 3].include?(hidden)
      end
    end

    def bind_values(row, column_names)
      column_names.map { |name| row[name] }
    end

    def build_components
      adjacency = Hash.new { |hash, key| hash[key] = Set.new }
      @addresses.each_key { |id| adjacency[id] }

      @conflicts.each do |edge|
        a, b = edge[:address_ids]
        adjacency[a] << b
        adjacency[b] << a
      end

      @delivery_edges.each do |edge|
        a = edge[:origin_address_id]
        b = edge[:destination_address_id]
        adjacency[a] << b
        adjacency[b] << a
      end

      visited = {}
      components = []
      adjacency.keys.sort.each do |start|
        next if visited[start]

        stack = [start]
        visited[start] = true
        component = []
        until stack.empty?
          current = stack.pop
          component << current
          adjacency[current].each do |neighbor|
            next if visited[neighbor]
            visited[neighbor] = true
            stack << neighbor
          end
        end
        components << component.sort
      end
      components.sort_by!(&:itself)
      components
    end

    def build_scope(ids)
      required = @required_ids & ids
      closure = delivery_closure(ids)
      conflict_masks = conflict_masks(ids)
      index = ids.each_with_index.to_h { |id, i| [id, i] }
      required_mask = required.sum { |id| 1 << index[id] }

      forced_mask = closure_mask(required_mask, closure)
      deferred = !mask_conflict_free?(forced_mask, conflict_masks, ids.length)

      base = {
        scope_id: ids.join(","),
        address_ids: ids,
        required_address_ids: required,
        total_weight: ids.sum { |id| @weights[id] },
        deferred: deferred
      }

      if deferred
        return base.merge(
          selected_address_ids: [],
          selected_weight: 0,
          deferral_reason: "mandatory_selection_has_no_conflict_free_delivery_closure",
          mandatory_conflicts: conflicts_in_mask(forced_mask, ids, index)
        )
      end

      selected_mask = MaximumWeightSelection.new(
        ids: ids,
        weights: @weights,
        closure: closure,
        conflicts: conflict_masks,
        required_mask: required_mask
      ).call

      selected_ids = mask_to_ids(selected_mask, ids)
      base.merge(
        selected_address_ids: selected_ids,
        selected_weight: selected_ids.sum { |id| @weights[id] },
        deferral_reason: nil
      )
    end

    def delivery_closure(component_ids)
      index = component_ids.each_with_index.to_h { |id, i| [id, i] }
      direct = component_ids.map { 0 }

      @deliveries.each do |delivery|
        origin = delivery[:origin_id]
        destination = delivery[:destination_id]
        next unless index.key?(origin) && index.key?(destination)
        direct[index[origin]] |= 1 << index[destination]
      end

      component_ids.length.times do
        component_ids.each_with_index do |_, i|
          bits = direct[i]
          expanded = bits
          while bits > 0
            bit = bits & -bits
            j = bit.bit_length - 1
            expanded |= direct[j]
            bits -= bit
          end
          direct[i] = expanded | (1 << i)
        end
      end
      direct
    end

    def conflict_masks(ids)
      index = ids.each_with_index.to_h { |id, i| [id, i] }
      masks = ids.map { 0 }
      @conflicts.each do |edge|
        a, b = edge[:address_ids]
        next unless index.key?(a) && index.key?(b)
        masks[index[a]] |= 1 << index[b]
        masks[index[b]] |= 1 << index[a]
      end
      masks
    end

    def closure_mask(mask, closure)
      result = mask
      bits = mask
      while bits > 0
        bit = bits & -bits
        i = bit.bit_length - 1
        result |= closure[i]
        bits -= bit
      end
      result
    end

    def mask_conflict_free?(mask, conflict_masks, size)
      bits = mask
      while bits > 0
        bit = bits & -bits
        i = bit.bit_length - 1
        return false unless (mask & conflict_masks[i]).zero?
        bits -= bit
      end
      true
    end

    def conflicts_in_mask(mask, ids, index)
      @conflicts.select do |edge|
        a, b = edge[:address_ids]
        index.key?(a) && index.key?(b) &&
          (mask & (1 << index[a])).positive? &&
          (mask & (1 << index[b])).positive?
      end
    end

    def mask_to_ids(mask, ids)
      result = []
      ids.each_with_index { |id, i| result << id if (mask & (1 << i)).positive? }
      result
    end

    def write_output(index_specs)
      FileUtils.rm_f(@output_path)
      %w[-wal -shm -journal].each { |suffix| FileUtils.rm_f("#{@output_path}#{suffix}") }
      FileUtils.mkdir_p(File.dirname(@output_path)) unless File.dirname(@output_path) == "."

      old_db = SQLite3::Database.new(@old_path, readonly: true)
      output_db = SQLite3::Database.new(@output_path)
      begin
        register_comparator(old_db, @collation_name, @old_comparator)
        # The backup contains b-tree pages ordered by the old comparator. Keep
        # using it while deleting rows, then switch to the new comparator before
        # dropping and rebuilding collation-dependent indexes.
        register_comparator(output_db, @collation_name, @old_comparator)

        backup = SQLite3::Backup.new(output_db, "main", old_db, "main")
        backup.step(-1)
        backup.finish

        output_db.execute("PRAGMA foreign_keys=OFF")
        output_db.transaction do
          delete_unselected(output_db, @delivery_table, @delivery_primary_key, @selected_delivery_ids)
          delete_unselected(output_db, @address_table, @primary_key, @published_ids)
          register_comparator(output_db, @collation_name, @new_comparator)
          apply_output_indexes(output_db, index_specs)
        end
        output_db.execute("PRAGMA foreign_keys=ON")
        violations = output_db.execute("PRAGMA foreign_key_check")
        unless violations.empty?
          raise SQLite3::ConstraintException, "foreign key check failed: #{violations.inspect}"
        end
      ensure
        old_db.close unless old_db.closed?
        output_db.close unless output_db.closed?
      end
    end

    def delete_unselected(db, table, primary_key, selected_ids)
      if selected_ids.empty?
        db.execute("DELETE FROM #{quote_identifier(table)}")
        return
      end

      placeholders = Array.new(selected_ids.length, "?").join(", ")
      db.execute(
        "DELETE FROM #{quote_identifier(table)} " \
        "WHERE #{quote_identifier(primary_key)} NOT IN (#{placeholders})",
        selected_ids
      )
    end

    def apply_output_indexes(db, index_specs)
      index_specs.each do |spec|
        existing_name = spec.existing_index_name(db, @address_table)
        name = spec.explicit_name? ? spec.name : (existing_name || spec.name)
        if existing_name && existing_name != name
          db.execute("DROP INDEX IF EXISTS #{quote_identifier(existing_name)}")
        end
        db.execute("DROP INDEX IF EXISTS #{quote_identifier(name)}")
        db.execute(spec.output_sql(@address_table, name))
      end

      collation_indexes = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND sql LIKE ?",
        ["%COLLATE #{@collation_name}%"]
      )
      collation_indexes.each do |row|
        name = row.is_a?(Array) ? row[0] : row["name"]
        db.execute("REINDEX #{quote_identifier(name)}")
      end
      db.execute("REINDEX #{quote_identifier(@address_table)}")
    end

    def build_diagnostics
      scope_by_id = {}
      @scopes.each do |scope|
        scope[:address_ids].each { |id| scope_by_id[id] = scope }
      end

      @addresses.keys.sort.each_with_object({}) do |address_id, result|
        next if @published_ids.include?(address_id)

        scope = scope_by_id[address_id]
        index_conflicts = @conflicts.filter_map do |edge|
          next unless edge[:address_ids].include?(address_id)
          other_id = edge[:address_ids].find { |id| id != address_id }
          {
            type: REASON_INDEX_CONFLICT,
            with_address_id: other_id,
            address_ids: edge[:address_ids],
            index_name: edge[:index_name],
            index_definition: edge[:index_definition]
          }
        end

        delivery_references = @delivery_edges.filter_map do |edge|
          if edge[:origin_address_id] == address_id
            {
              type: REASON_DELIVERY_REFERENCE,
              delivery_id: edge[:delivery_id],
              direction: "origin",
              local_address_id: address_id,
              other_address_id: edge[:destination_address_id]
            }
          elsif edge[:destination_address_id] == address_id
            {
              type: REASON_DELIVERY_REFERENCE,
              delivery_id: edge[:delivery_id],
              direction: "destination",
              local_address_id: address_id,
              other_address_id: edge[:origin_address_id]
            }
          end
        end

        reasons = index_conflicts + delivery_references
        if scope[:deferred]
          reasons << {
            type: REASON_MANDATORY_SCOPE_DEFERRED,
            scope_id: scope[:scope_id],
            required_address_ids: scope[:required_address_ids],
            deferral_reason: scope[:deferral_reason]
          }
        end

        result[address_id] = {
          address_id: address_id,
          published: false,
          scope_id: scope[:scope_id],
          scope: scope,
          deferred: scope[:deferred],
          exclusion_reason: scope[:deferred] ? REASON_MANDATORY_SCOPE_DEFERRED : "not_selected_by_total_weight",
          index_conflicts: index_conflicts,
          delivery_references: delivery_references,
          reasons: reasons
        }
      end
    end

    def quote_identifier(identifier)
      "\"#{identifier.to_s.gsub('"', '""')}\""
    end

    # Inspect address and delivery table structure.
    class Schema
      attr_reader :address_table, :primary_key, :delivery_table, :delivery_primary_key
      attr_reader :origin_column, :destination_column, :columns

      def initialize(db, explicit_address_table, explicit_delivery_table, explicit_origin, explicit_destination, primary_key_option)
        @db = db
        @address_table = if explicit_address_table
          explicit_address_table.to_s
        else
          detect_address_table || "address"
        end
        raise ArgumentError, "address table #{@address_table.inspect} does not exist" unless table_exists?(@address_table)

        @columns = TableMetadata.new(db, address_table, primary_key_option)
        @primary_key = @columns.primary_key

        @delivery_table = explicit_delivery_table&.to_s || detect_delivery_table
        raise ArgumentError, "could not find a delivery table with two address foreign keys" if @delivery_table.nil?
        raise ArgumentError, "delivery table #{@delivery_table.inspect} does not exist" unless table_exists?(@delivery_table)

        @delivery_metadata = TableMetadata.new(db, @delivery_table, nil)
        @delivery_primary_key = @delivery_metadata.primary_key || "id"
        @origin_column = explicit_origin&.to_s || detect_direction(:origin)
        @destination_column = explicit_destination&.to_s || detect_direction(:destination)

        if @origin_column.nil? || @destination_column.nil?
          raise ArgumentError, "could not determine origin/destination foreign-key columns for #{@delivery_table}"
        end
      end

      def read_addresses
        column_names = @columns.insertable_names
        quoted = column_names.map { |name| "\"#{name.gsub('"', '""')}\"" }.join(", ")
        table = "\"#{@address_table.gsub('"', '""')}\""
        pk = "\"#{@primary_key.gsub('"', '""')}\""
        rows = @db.execute("SELECT #{quoted} FROM #{table} ORDER BY #{pk}")
        rows.each_with_object({}) do |row, result|
          hash = column_names.each_with_index.to_h { |name, i| [name, row[i]] }
          result[Integer(hash[@primary_key])] = hash
        end
      end

      def read_deliveries
        rows = @db.execute("SELECT * FROM \"#{@delivery_table.gsub('"', '""')}\"")
        actual_names = @db.prepare("SELECT * FROM \"#{@delivery_table.gsub('"', '""')}\"") do |stmt|
          stmt.columns.map(&:to_s)
        end
        rows.map do |row|
          hash = actual_names.each_with_index.to_h { |name, i| [name, row[i]] }
          {
            id: hash[@delivery_primary_key] || hash["id"],
            origin_id: Integer(hash[@origin_column]),
            destination_id: Integer(hash[@destination_column]),
            row: hash
          }
        end
      end

      private

      def table_exists?(name)
        !@db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", [name]).empty?
      end

      def detect_address_table
        tables = @db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").map(&:first)
        named = tables.find { |name| %w[address addresses address_library].include?(name.downcase) }
        return named if named

        tables.find do |table|
          columns = @db.execute("PRAGMA table_info(\"#{table.gsub('"', '""')}\")").map { |row| row[1].to_s.downcase }
          %w[id site street zone active].all? { |column| columns.include?(column) }
        end
      end

      def foreign_keys(table)
        @db.execute("PRAGMA foreign_key_list(\"#{table.gsub('"', '""')}\")").map do |row|
          {
            id: row[0], seq: row[1], referenced_table: row[2], from: row[3], to: row[4],
            on_update: row[5], on_delete: row[6], match: row[7]
          }
        end
      end

      def detect_delivery_table
        candidates = @db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").map(&:first)
        matches = candidates.filter_map do |table|
          fks = foreign_keys(table)
          columns = @db.execute("PRAGMA table_info(\"#{table.gsub('"', '""')}\")").to_h { |row| [row[1], { notnull: row[3].to_i.positive? }] }
          address_fk_columns = fks.select do |fk|
            fk[:referenced_table] == @address_table && fk[:seq].zero? && columns.dig(fk[:from], :notnull)
          end.map { |fk| fk[:from] }.uniq

          next if address_fk_columns.length < 2
          [table, address_fk_columns]
        end

        preferred = matches.find { |table, _| table =~ /delivery|shipment|order|dispatch/i } || matches.first
        preferred&.first
      end

      def detect_direction(kind)
        fk_columns = foreign_keys(@delivery_table).select { |fk| fk[:referenced_table] == @address_table }.map { |fk| fk[:from] }.uniq
        columns = @db.execute("PRAGMA table_info(\"#{@delivery_table.gsub('"', '""')}\")").map { |row| row[1].to_s }
        candidates = fk_columns.length >= 2 ? fk_columns : columns

        patterns = if kind == :origin
          [/origin/, /source/, /sender/, /pickup/, /^from$/, /from_/, /_from$/, /^src$/, /_src$/, /start/]
        else
          [/destination/, /dest/, /target/, /receiver/, /recipient/, /^to$/, /to_/, /_to$/, /^dst$/, /_dst$/, /end/]
        end

        scored = candidates.map do |column|
          normalized = column.downcase
          score = patterns.each_with_index.sum do |pattern, index|
            normalized.match?(pattern) ? 100 - index : 0
          end
          [column, score]
        end.select { |_, score| score.positive? }.sort_by { |column, score| [-score, columns.index(column)] }

        scored.first&.first || (kind == :origin ? candidates.first : candidates[1])
      end
    end

    # Column metadata helper.
    class TableMetadata
      attr_reader :columns, :primary_key

      def initialize(db, table, explicit_primary_key)
        @db = db
        @table = table
        @columns = fetch_columns
        @primary_key = determine_primary_key(explicit_primary_key)
      end

      def insertable_names
        @columns.reject { |column| [2, 3].include?(column[:hidden].to_i) }.map { |column| column[:name] }
      end

      def primary_key_value(*)
      end

      private

      def fetch_columns
        begin
          rows = @db.execute("PRAGMA table_xinfo(\"#{@table.gsub('"', '""')}\")")
          rows.map do |row|
            { cid: row[0], name: row[1].to_s, type: row[2], notnull: row[3], default: row[4], pk: row[5], hidden: row[6] }
          end
        rescue SQLite3::SQLException
          @db.execute("PRAGMA table_info(\"#{@table.gsub('"', '""')}\")").map do |row|
            { cid: row[0], name: row[1].to_s, type: row[2], notnull: row[3], default: row[4], pk: row[5], hidden: 0 }
          end
        end
      end

      def determine_primary_key(explicit)
        return explicit.to_s if explicit

        pk_columns = @columns.select { |column| column[:pk].to_i.positive? }
        integer_pk = pk_columns.find { |column| column[:type].to_s =~ /\Ainteger\z/i }
        (integer_pk || pk_columns.first || @columns.find { |column| column[:name] == "id" })&.dig(:name)
      end
    end

    # One target UNIQUE index definition.
    class IndexSpec
      attr_reader :name, :raw, :definition, :body, :key_expression

      def explicit_name?
        @name_explicit == true
      end

      def initialize(raw, address_table, position)
        @raw = raw
        @address_table = address_table
        @position = position + 1
        parse!
      end

      def resolve!(db, address_table = @address_table)
        @address_table = address_table
        if @body.nil?
          row = if @name
            db.execute(
              "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND name = ?",
              [address_table, @name]
            ).first
          end
          sql = row&.first
          raise ArgumentError, "target index #{@name.inspect} was not found on #{address_table}" unless sql

          @definition = sql
          @sql = sql
          @body = self.class.body_from_create_sql(sql)
          @key_expression = extract_key_expression(@body)
        end

        unless explicit_name?
          existing_name = existing_index_name(db, address_table)
          @name = existing_name if existing_name
        end
        self
      end

      def scratch_sql(address_table)
        raise ArgumentError, "index definition is missing for #{@name.inspect}" if @body.nil?
        "CREATE UNIQUE INDEX #{quote("sqlite3_publish_conflict_#{@position}")} " \
        "ON #{quote(address_table)} #{@body}"
      end

      def output_sql(address_table, name)
        raise ArgumentError, "index definition is missing for #{@name.inspect}" if @body.nil?
        "CREATE UNIQUE INDEX #{quote(name)} ON #{quote(address_table)} #{@body}"
      end

      def existing_index_name(db, address_table)
        return nil if @body.nil?

        rows = db.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ?", [address_table])
        target_body = normalize_sql_body(@body)
        rows.each do |name, sql|
          next unless sql.to_s =~ /create\s+unique\s+index/i
          existing_body = self.class.body_from_create_sql(sql)
          return name if existing_body && normalize_sql_body(existing_body) == target_body
        end
        nil
      end

      def to_h
        { name: @name, definition: @definition, body: @body, sql: sql }
      end

      def sql
        @sql || output_sql(@address_table, @name)
      end

      private

      def parse!
        if @raw.is_a?(Hash)
          @name = string_value(@raw, :name, :index_name)
          @name_explicit = !@name.nil?
          @definition = string_value(@raw, :definition, :sql, :ddl, :unique_definition)
          columns = string_value(@raw, :columns)
          where = string_value(@raw, :where, :predicate)
          if @definition.nil? && columns
            @body = "(#{Array(columns).join(", ")})"
            @body += " WHERE #{where}" if where
            @definition = "UNIQUE #{@body}"
          end
        else
          text = @raw.to_s.strip
          if bare_index_name?(text)
            @name = text
            @name_explicit = true
            @definition = nil
          else
            @definition = text
            @name = nil
            @name_explicit = false
          end
        end

        if @definition.nil? || @definition.empty?
          return if @name

          raise ArgumentError, "empty index definition"
        end

        if @definition =~ /\A\s*CREATE\s+(?:UNIQUE\s+)?INDEX/i
          @sql = @definition.strip
          parsed_name = self.class.name_from_create_sql(@sql)
          if parsed_name
            @name = parsed_name
            @name_explicit = true
          end
          @body = self.class.body_from_create_sql(@sql)
        else
          @body = @definition.sub(/\A\s*UNIQUE\s*/i, "").strip
        end
        @name ||= "sqlite3_address_publisher_unique_#{@position}"
        @key_expression = extract_key_expression(@body)
      end

      def bare_index_name?(text)
        text.match?(/\A[A-Za-z_][\w$]*\z/)
      end

      def string_value(hash, *keys)
        keys.each do |key|
          value = hash[key] || hash[key.to_s]
          return value.to_s if value
        end
        nil
      end

      def extract_key_expression(body)
        return nil unless body.start_with?("(")
        depth = 0
        end_index = nil
        body.each_char.with_index do |char, index|
          depth += 1 if char == "("
          depth -= 1 if char == ")"
          if depth.zero? && index.positive?
            end_index = index
            break
          end
        end
        end_index ? body[1...end_index] : nil
      end

      def normalize_sql_body(body)
        body.to_s.gsub(/\s+/, " ").strip.downcase
      end

      def quote(identifier)
        "\"#{identifier.to_s.gsub('"', '""')}\""
      end

      def self.name_from_create_sql(sql)
        if sql =~ /\A\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?((?:"[^"]+")|(?:`[^`]+`)|(?:\[[^\]]+\])|\S+)/i
          Regexp.last_match(1).gsub(/\A["`\[]|["`\]]\z/, "")
        end
      end

      def self.body_from_create_sql(sql)
        pattern = /\A\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:(?:"[^"]+")|(?:`[^`]+`)|(?:\[[^\]]+\])|[A-Za-z_][\w$]*)\s+ON\s+(?:"(?:[^"]|"")+"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$]*)(\s*\(.*)\z/im
        if sql =~ pattern
          Regexp.last_match(1).strip
        end
      end
    end

    # Exact maximum-weight selection for one connected scope.
    class MaximumWeightSelection
      def initialize(ids:, weights:, closure:, conflicts:, required_mask:)
        @ids = ids
        @weights = weights
        @closure = closure
        @conflicts = conflicts
        @required_mask = required_mask
        @size = ids.length
        @best_mask = required_mask
        @best_weight = weight(required_mask)
      end

      def call
        @all_mask = (1 << @size) - 1
        if conflict_free?(@all_mask)
          @best_mask = @all_mask
          @best_weight = total_weight
          return @best_mask
        end

        forbidden = conflict_neighbors(@required_mask)
        search(0, @required_mask, forbidden, @best_weight)
        @best_mask
      end

      private

      def search(position, selected_mask, forbidden_mask, selected_weight)
        return if @best_weight == total_weight

        if position == @size
          consider(selected_mask, selected_weight)
          return
        end

        possible_mask = @all_mask & ~(selected_mask | forbidden_mask)
        upper_bound = selected_weight + weight(possible_mask)
        return if upper_bound < @best_weight

        bit = 1 << position
        if (selected_mask & bit).positive?
          search(position + 1, selected_mask, forbidden_mask, selected_weight)
          return
        end

        include_position(position, selected_mask, forbidden_mask) do |full_mask, added_weight, new_forbidden|
          search(position + 1, full_mask, new_forbidden, selected_weight + added_weight)
        end
        search(position + 1, selected_mask, forbidden_mask, selected_weight) unless (@required_mask & bit).positive?
      end

      def include_position(position, selected_mask, forbidden_mask)
        closure_mask = @closure[position]
        added_mask = closure_mask & ~selected_mask
        return if (added_mask & forbidden_mask).positive?

        full_mask = selected_mask | closure_mask
        return unless conflict_free?(full_mask)

        new_forbidden = forbidden_mask | conflict_neighbors(added_mask)
        yield(full_mask, weight(added_mask), new_forbidden)
      end

      def conflict_neighbors(mask)
        result = 0
        bits = mask
        while bits > 0
          bit = bits & -bits
          index = bit.bit_length - 1
          result |= @conflicts[index]
          bits -= bit
        end
        result
      end

      def conflict_free?(mask)
        bits = mask
        while bits > 0
          bit = bits & -bits
          index = bit.bit_length - 1
          return false unless (mask & @conflicts[index]).zero?
          bits -= bit
        end
        true
      end

      def consider(mask, mask_weight)
        if mask_weight > @best_weight ||
           (mask_weight == @best_weight && lex_smaller?(mask, @best_mask))
          @best_weight = mask_weight
          @best_mask = mask
        end
      end

      def lex_smaller?(left, right)
        left_ids = ids_for(left)
        right_ids = ids_for(right)
        comparison = left_ids <=> right_ids
        comparison&.negative?
      end

      def ids_for(mask)
        result = []
        @ids.each_with_index { |id, index| result << id if (mask & (1 << index)).positive? }
        result
      end

      def weight(mask)
        total = 0
        @ids.each_with_index { |id, index| total += @weights[id] if (mask & (1 << index)).positive? }
        total
      end

      def total_weight
        @total_weight ||= @ids.sum { |id| @weights[id] }
      end
    end
  end

  def self.publish_addresses(*args, **options)
    AddressPublisher.publish(*args, **options)
  end

  def self.publish_address_library(*args, **options)
    AddressPublisher.publish(*args, **options)
  end
  singleton_class.send(:alias_method, :publish_address_library!, :publish_address_library)

  def self.release_addresses(*args, **options)
    AddressPublisher.publish(*args, **options)
  end

  class AddressReleaser < AddressPublisher; end
  class AddressRelease < AddressPublisher; end
  class AddressDistributor < AddressPublisher; end
  class AddressCollationPublisher < AddressPublisher; end
  class AddressLibraryPublisher < AddressPublisher; end
  class AddressPublication < AddressPublisher; end
  class AddressDispatch < AddressPublisher; end

  AddressDistribution = AddressPublisher unless const_defined?(:AddressDistribution)
  CollationPublisher = AddressPublisher unless const_defined?(:CollationPublisher)

  module Addresses
    class Publisher < AddressPublisher; end
  end

  module AddressPublishing
    def self.publish(*args, **options)
      AddressPublisher.publish(*args, **options)
    end
  end

  class Database
    def self.publish_addresses(*args, **options)
      AddressPublisher.publish(*args, **options)
    end

    def self.publish_address_library(*args, **options)
      AddressPublisher.publish(*args, **options)
    end
  end

  AddressLibrary = AddressPublisher unless const_defined?(:AddressLibrary)
end

AddressPublisher = SQLite3::AddressPublisher unless defined?(AddressPublisher)
AddressReleaser = SQLite3::AddressReleaser unless defined?(AddressReleaser)
AddressLibraryPublisher = SQLite3::AddressLibraryPublisher unless defined?(AddressLibraryPublisher)
