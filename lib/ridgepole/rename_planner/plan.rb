# frozen_string_literal: true

module Ridgepole
  module RenamePlanner
    class Plan
      Step = Struct.new(:sql, :prechecks, :note, keyword_init: true)

      attr_reader :steps, :final_objects, :final_foreign_keys, :temp_renamed_dependents

      def initialize(steps:, final_objects:, final_foreign_keys:, temp_renamed_dependents:)
        @steps = steps
        @final_objects = final_objects
        @final_foreign_keys = final_foreign_keys
        @temp_renamed_dependents = temp_renamed_dependents
      end

      def dependent_temp_rename_count
        @temp_renamed_dependents.length
      end

      def to_sql(guards: true)
        buf = StringIO.new
        buf.puts '-- Ridgepole rename plan (PostgreSQL)'
        buf.puts "-- statements: #{@steps.length}"
        buf.puts "-- attached objects temporarily renamed: #{dependent_temp_rename_count}" \
                  "#{@temp_renamed_dependents.empty? ? '' : " (#{@temp_renamed_dependents.map(&:describe_original).join(', ')})"}"
        @steps.each_with_index do |step, i|
          buf.puts
          buf.puts "-- step #{i + 1}: #{step.note}"
          buf.puts guard_sql(step.prechecks) if guards && !step.prechecks.empty?
          buf.puts step.sql
        end
        buf.puts
        buf.puts '-- final catalog (verify object identities, owning columns and foreign key targets)'
        @final_objects.each do |o|
          moved = [o.schema, o.name] == [o.orig_schema, o.orig_name] ? '' : %( (was "#{o.orig_schema}"."#{o.orig_name}"))
          extra = +''
          extra << %( on table oid=#{o.table_oid}) if o.kind == :index
          extra << %( owned by #{o.table_oid}.#{o.owner_column}) if o.kind == :sequence
          buf.puts %(-- [#{o.kind}] oid=#{o.oid} "#{o.schema}"."#{o.name}"#{moved}#{extra})
        end
        @final_foreign_keys.each do |fk|
          buf.puts %(-- [foreign_key] oid=#{fk[:oid]} "#{fk[:name]}" #{fk[:from_table]} -> #{fk[:to_table]} (target #{fk[:target]}))
        end
        buf.string
      end

      private

      def guard_sql(prechecks)
        lines = ['DO $$', 'BEGIN']
        prechecks.each do |(schema, name)|
          lines << "  IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace " \
                   "WHERE n.nspname = #{literal(schema)} AND c.relname = #{literal(name)}) THEN"
          lines << "    RAISE EXCEPTION 'name % is not free', #{literal(%("#{schema}"."#{name}"))};"
          lines << '  END IF;'
        end
        lines << 'END'
        lines << '$$;'
        lines.join("\n")
      end

      def literal(str)
        "'#{str.gsub("'", "''")}'"
      end
    end
  end
end
