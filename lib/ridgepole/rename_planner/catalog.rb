# frozen_string_literal: true

module Ridgepole
  module RenamePlanner
    class DbObject
      attr_reader :oid, :kind, :orig_schema, :orig_name, :table_oid, :owner_column
      attr_accessor :schema, :name

      def initialize(oid:, kind:, schema:, name:, table_oid: nil, owner_column: nil)
        @oid = oid.to_s
        @kind = kind.to_sym
        @schema = schema.to_s
        @name = name.to_s
        @orig_schema = @schema
        @orig_name = @name
        @table_oid = table_oid&.to_s
        @owner_column = owner_column&.to_s
      end

      def dependent?
        @kind == :index || @kind == :sequence
      end

      def qualified(schema = @schema, name = @name)
        "#{schema}.#{name}"
      end

      def describe
        %(#{@kind} oid=#{@oid} "#{@schema}"."#{@name}")
      end

      def describe_original
        %(#{@kind} oid=#{@oid} "#{@orig_schema}"."#{@orig_name}")
      end
    end

    ForeignKey = Struct.new(:oid, :name, :from_table, :to_table) do
      def initialize(oid:, name:, from_table:, to_table:)
        super(oid.to_s, name.to_s, from_table.to_s, to_table.to_s)
      end
    end

    Move = Struct.new(:from, :schema, :name) do
      def initialize(from:, to:)
        super(from.to_s, to.fetch(:schema).to_s, to.fetch(:name).to_s)
      end
    end

    class Catalog
      attr_reader :objects, :foreign_keys, :temp_names

      def self.load(hash)
        attrs = RenamePlanner.symbolize_keys(hash)
        new(**attrs.slice(:relations, :indexes, :sequences, :foreign_keys, :temp_names))
      end

      def initialize(relations: [], indexes: [], sequences: [], foreign_keys: [], temp_names: [])
        @objects = []
        relations.each do |attrs|
          @objects << DbObject.new(**RenamePlanner.symbolize_keys(attrs).slice(:oid, :kind, :schema, :name).tap { |a| a[:kind] ||= :table })
        end
        indexes.each do |attrs|
          a = RenamePlanner.symbolize_keys(attrs)
          @objects << DbObject.new(oid: a.fetch(:oid), kind: :index, schema: a.fetch(:schema), name: a.fetch(:name), table_oid: a.fetch(:table))
        end
        sequences.each do |attrs|
          a = RenamePlanner.symbolize_keys(attrs)
          owned_by = a.fetch(:owned_by)
          @objects << DbObject.new(oid: a.fetch(:oid), kind: :sequence, schema: a.fetch(:schema), name: a.fetch(:name),
                                   table_oid: owned_by.fetch(:table), owner_column: owned_by.fetch(:column))
        end
        @foreign_keys = foreign_keys.map { |attrs| ForeignKey.new(**RenamePlanner.symbolize_keys(attrs)) }
        @temp_names = temp_names.map(&:to_s)

        validate_catalog!
      end

      def tables
        @objects.select { |o| o.kind == :table }
      end

      def dependents
        @objects.select(&:dependent?)
      end

      private

      def validate_catalog!
        seen = {}
        @objects.each do |o|
          key = [o.schema, o.name]
          raise ConflictError.new("duplicate object in catalog: #{o.describe}", [o.describe, seen[key].describe]) if seen[key]

          seen[key] = o
        end
      end
    end
  end
end
