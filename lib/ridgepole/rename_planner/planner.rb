# frozen_string_literal: true

module Ridgepole
  module RenamePlanner
    class Planner
      def initialize(catalog, moves)
        @catalog = catalog.is_a?(Catalog) ? catalog : Catalog.load(catalog)
        moves = moves.map { |from, to| { from: from, to: to } } if moves.is_a?(Hash)
        @moves = Array(moves).map { |m| m.is_a?(Move) ? m : Move.new(**RenamePlanner.symbolize_keys(m)) }
        @goals = {}
        @moves.each { |m| @goals[m.from] = [m.schema, m.name] }
        @by_oid = @catalog.objects.to_h { |o| [o.oid, o] }
        @dependents_of = Hash.new { |h, k| h[k] = [] }
        @catalog.dependents.each { |d| @dependents_of[d.table_oid] << d }
        @occupied = @catalog.objects.to_h { |o| [[o.schema, o.name], o] }
        @steps = []
        @temp_dependents = []
        @temp_renamed_dependents = []
      end

      def plan
        validate_moves!
        validate_final_state!
        @remaining = @catalog.tables.select { |t| @goals.key?(t.oid) && @goals[t.oid] != [t.schema, t.name] }
        settle!
        restore_dependents
        verify_final_state!
        build_plan
      end

      private

      def validate_moves!
        problems = []
        @goals.each do |oid, (schema, name)|
          obj = @by_oid[oid]
          if obj.nil?
            problems << %(move source oid=#{oid} does not exist in the catalog)
          elsif obj.kind != :table
            problems << %(move source #{obj.describe} is not a table)
          end
          problems << %(move target for oid=#{oid} is missing a schema or name) if schema.empty? || name.empty?
        end
        raise ConflictError.new('invalid move mapping', problems) unless problems.empty?
      end

      def validate_final_state!
        final = {}
        collisions = []
        @catalog.objects.each do |o|
          slot =
            if @goals.key?(o.oid)
              @goals[o.oid]
            elsif participating_dependent?(o)
              [@goals[o.table_oid][0], o.name]
            else
              [o.schema, o.name]
            end
          if (other = final[slot])
            collisions << %(#{o.describe} and #{other.describe} would both end up at "#{slot[0]}"."#{slot[1]}")
          else
            final[slot] = o
          end
        end
        raise ConflictError.new('name collision in the final state', collisions) unless collisions.empty?
      end

      def settle!
        until @remaining.empty?
          progressed = false
          @remaining.dup.each do |t|
            next unless blockers_for(t).empty?

            execute_placement(t)
            @remaining.delete(t)
            progressed = true
          end
          break_deadlock unless progressed
        end
      end

      def blockers_for(t)
        goal_schema, goal_name = @goals[t.oid]
        blockers = []
        add_blocker(blockers, occupant(goal_schema, goal_name), t)
        @dependents_of[t.oid].each do |d|
          add_blocker(blockers, occupant(goal_schema, d.orig_name), t, ignore: d)
        end
        if blockers.empty? && goal_schema != t.schema && goal_name != t.name
          rename_first = occupant(t.schema, goal_name)
          move_first = occupant(goal_schema, t.name)
          if rename_first && move_first
            [rename_first, move_first].each do |o|
              blockers << o if @remaining.include?(owner_of(o))
            end
          end
        end
        blockers.uniq
      end

      def add_blocker(blockers, occupant_obj, table, ignore: nil)
        return if occupant_obj.nil? || occupant_obj == table || occupant_obj == ignore
        return if occupant_obj.dependent? && occupant_obj.table_oid == table.oid

        blockers << occupant_obj
      end

      def execute_placement(t)
        goal_schema, goal_name = @goals[t.oid]
        if goal_schema == t.schema
          rename_object(t, goal_name)
        elsif goal_name == t.name
          set_schema(t, goal_schema)
        elsif !occupant(t.schema, goal_name)
          rename_object(t, goal_name)
          set_schema(t, goal_schema)
        elsif !occupant(goal_schema, t.name)
          set_schema(t, goal_schema)
          rename_object(t, goal_name)
        else
          temp = allocate_temp(t, t.schema, goal_schema)
          rename_object(t, temp)
          set_schema(t, goal_schema)
          rename_object(t, goal_name)
        end
      end

      def break_deadlock
        edges = {}
        stranded = []
        @remaining.each do |t|
          blockers = blockers_for(t)
          blockers.each do |o|
            owner = owner_of(o)
            if @remaining.include?(owner)
              (edges[t] ||= []) << owner
            else
              stranded << %(#{o.describe} blocks #{t.describe} and cannot be renamed)
            end
          end
        end
        raise ConflictError.new('unresolvable name conflict', stranded) unless stranded.empty?

        cycle = find_cycle(edges)
        raise ConflictError.new('unresolvable name conflict', @remaining.map(&:describe)) unless cycle

        victim = cycle.uniq.min_by do |t|
          victims = family_victims(t)
          [victims.count(&:dependent?), victims.size]
        end
        family_victims(victim).each { |o| temp_rename(o) }
      end

      def find_cycle(edges)
        state = {}
        stack = []
        @remaining.each do |root|
          next if state[root]

          visit = nil
          visit = lambda do |node|
            state[node] = :visiting
            stack << node
            (edges[node] || []).each do |nxt|
              if state[nxt] == :visiting
                return stack[stack.index(nxt)..]
              elsif !state[nxt]
                result = visit.call(nxt)
                return result if result
              end
            end
            stack.pop
            state[node] = :done
            nil
          end
          result = visit.call(root)
          return result if result
        end
        nil
      end

      def family_victims(t)
        family = [t] + @dependents_of[t.oid]
        victims = []
        family.each do |o|
          next unless o.dependent?

          occupant_obj = occupant(@goals[t.oid][0], o.orig_name)
          victims << o if occupant_obj && !family.include?(occupant_obj)
        end
        family.each do |o|
          next if victims.include?(o) || o.name != o.orig_name

          victims << o if @remaining.any? { |u| u != t && needed_slots_of(u).include?([o.schema, o.name]) }
        end
        victims
      end

      def needed_slots_of(t)
        goal_schema, goal_name = @goals[t.oid]
        slots = final_slots_of(t)
        if goal_schema != t.schema && goal_name != t.name
          slots << [t.schema, goal_name] << [goal_schema, t.name]
        end
        slots
      end

      def temp_rename(obj)
        owner = owner_of(obj)
        schemas = [obj.schema]
        schemas << @goals[owner.oid][0] if @goals.key?(owner.oid)
        temp = allocate_temp(obj, *schemas.uniq)
        rename_object(obj, temp)
        return unless obj.dependent?

        @temp_dependents << obj
        @temp_renamed_dependents << obj unless @temp_renamed_dependents.include?(obj)
      end

      def allocate_temp(obj, *schemas)
        name = @catalog.temp_names.find do |candidate|
          schemas.all? { |s| temp_usable?(s, candidate) }
        end
        return name if name

        raise ConflictError.new(
          'reserved temporary names are exhausted',
          [%(#{obj.describe} needs a temporary name in schema(s) #{schemas.uniq.join(', ')})]
        )
      end

      def temp_usable?(schema, name)
        return false if @occupied[[schema, name]]
        return false if @remaining.any? { |t| final_slots_of(t).include?([schema, name]) }
        return false if @temp_dependents.any? { |d| d.schema == schema && d.orig_name == name }

        true
      end

      def final_slots_of(t)
        goal_schema, goal_name = @goals[t.oid]
        [[goal_schema, goal_name]] + @dependents_of[t.oid].map { |d| [goal_schema, d.orig_name] }
      end

      def restore_dependents
        @temp_dependents.each do |d|
          rename_object(d, d.orig_name) unless d.name == d.orig_name
        end
      end

      def rename_object(obj, new_name)
        return if obj.name == new_name

        key = [obj.schema, new_name]
        raise "name #{key.inspect} is not free (planning error)" if @occupied[key]

        keyword = { table: 'TABLE', index: 'INDEX', sequence: 'SEQUENCE' }.fetch(obj.kind)
        add_step(
          sql: %(ALTER #{keyword} #{quote(obj.schema)}.#{quote(obj.name)} RENAME TO #{quote(new_name)};),
          prechecks: [[obj.schema, new_name]],
          note: %(rename #{obj.kind} "#{obj.schema}"."#{obj.name}" to "#{obj.schema}"."#{new_name}")
        )
        @occupied.delete([obj.schema, obj.name])
        obj.name = new_name
        @occupied[key] = obj
      end

      def set_schema(table, new_schema)
        moving = [table] + @dependents_of[table.oid]
        moving.each do |o|
          raise "name #{[new_schema, o.name].inspect} is not free (planning error)" if @occupied[[new_schema, o.name]]
        end

        add_step(
          sql: %(ALTER TABLE #{quote(table.schema)}.#{quote(table.name)} SET SCHEMA #{quote(new_schema)};),
          prechecks: moving.map { |o| [new_schema, o.name] },
          note: %(move table "#{table.schema}"."#{table.name}" to schema "#{new_schema}" (with #{moving.length - 1} attached object(s)))
        )
        moving.each do |o|
          @occupied.delete([o.schema, o.name])
          o.schema = new_schema
          @occupied[[o.schema, o.name]] = o
        end
      end

      def add_step(sql:, prechecks:, note:)
        @steps << Plan::Step.new(sql: sql, prechecks: prechecks, note: note)
      end

      def verify_final_state!
        problems = []
        @catalog.objects.each do |o|
          if @goals.key?(o.oid)
            problems << %(#{o.describe} did not reach its target "#{@goals[o.oid][0]}"."#{@goals[o.oid][1]}") if [o.schema, o.name] != @goals[o.oid]
          elsif participating_dependent?(o)
            owner = @by_oid[o.table_oid]
            problems << %(#{o.describe} does not follow its table #{owner.describe}) if o.schema != owner.schema
            problems << %(#{o.describe} did not get its original name "#{o.orig_name}" back) if o.name != o.orig_name
          elsif [o.schema, o.name] != [o.orig_schema, o.orig_name]
            problems << %(#{o.describe} is not part of the migration but was moved)
          end
          next unless o.kind == :sequence

          owner = @by_oid[o.table_oid]
          problems << %(sequence oid=#{o.oid} owner table oid=#{o.table_oid} is missing from the catalog) unless owner
        end
        @catalog.foreign_keys.each do |fk|
          problems << %(foreign key oid=#{fk.oid} source table oid=#{fk.from_table} is missing from the catalog) unless @by_oid[fk.from_table]
          problems << %(foreign key oid=#{fk.oid} target table oid=#{fk.to_table} is missing from the catalog) unless @by_oid[fk.to_table]
        end
        raise ConflictError.new('final verification failed', problems) unless problems.empty?
      end

      def build_plan
        Plan.new(
          steps: @steps,
          final_objects: @catalog.objects.sort_by(&:oid),
          final_foreign_keys: @catalog.foreign_keys.map do |fk|
            target = @by_oid[fk.to_table]
            {
              oid: fk.oid,
              name: fk.name,
              from_table: fk.from_table,
              to_table: fk.to_table,
              target: target ? target.qualified : '(unknown)',
            }
          end,
          temp_renamed_dependents: @temp_renamed_dependents
        )
      end

      def participating_dependent?(obj)
        obj.dependent? && @goals.key?(obj.table_oid)
      end

      def owner_of(obj)
        obj.dependent? ? @by_oid.fetch(obj.table_oid) : obj
      end

      def occupant(schema, name)
        @occupied[[schema, name]]
      end

      def quote(ident)
        %("#{ident.gsub('"', '""')}")
      end
    end
  end
end
