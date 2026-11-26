# frozen_string_literal: true

module HandoverAudit
  # Replays one concrete choice of every handover slot against a real, isolated
  # SQLite database containing the sealed views. The gem itself supplies the
  # tricky semantics (schema-change reprepare, cached column names, shared
  # statement close, sticky bindings); the engine layers the cursor bookkeeping
  # and provenance capture on top.
  class Engine
    class Halt < StandardError
      attr_reader :observation

      def initialize(observation)
        super(observation[:message].to_s)
        @observation = observation
      end
    end

    Row = Struct.new(:handle, :delivery, :mode, :raw, :snapshot_names, :live_names,
      :view, :ordinal, :view_ordinal, :pub_no, :material_id, keyword_init: true) do
      # Value observed when the receiving book spot-checks a named column.
      # Hash cursors key rows with the names cached when their statement first
      # ran (the gem emits string keys); array cursors are positioned by live
      # column order.
      def value(column)
        key = column.to_sym
        if mode == :hash
          return raw[key.to_s] if raw.key?(key.to_s)

          position = snapshot_names.index(key)
        else
          position = live_names.index(key) || snapshot_names.index(key)
        end
        return nil if position.nil?

        ordered_values[position]
      end

      def ordered_values
        @ordered_values ||=
          if mode == :hash
            live_names.each_index.map do |i|
              i < snapshot_names.length ? raw[snapshot_names[i].to_s] : nil
            end
          else
            raw
          end
      end
    end

    Terminal = Struct.new(:handle, :kind, :message, keyword_init: true)

    attr_reader :rows, :terminals, :choice_by_slot

    def initialize(audit_case, provenance, choice_by_slot, shared_db = nil)
      @audit_case = audit_case
      @provenance = provenance
      @choice_by_slot = choice_by_slot
      @shared_db = shared_db
      @rows = []
      @terminal_by_handle = {}
      @cursors = {}
      @view_version_by_name = {}
      @row_counters = Hash.new(0)
      @statement_row_counters = Hash.new(0)
      @delivery_counters = Hash.new(0)
    end

    def run
      owns_database = @shared_db.nil?
      @db = @shared_db || build_database
      @view_version_by_name = @audit_case.initial_views.transform_values(&:itself)
      @savepoint = "ha_#{object_id.abs}"
      @db.execute("SAVEPOINT #{@savepoint}") unless owns_database
      @statements = {}

      @audit_case.handovers.each_with_index do |handover, slot_index|
        choice = @choice_by_slot.fetch(slot_index)
        apply(handover[:id], choice)
      end
      self
    ensure
      @statements.each_value { |stmt| stmt.close unless stmt.closed? }
      if owns_database
        @db&.close
      elsif @db
        @db.execute("ROLLBACK TO SAVEPOINT #{@savepoint}")
        @db.execute("RELEASE SAVEPOINT #{@savepoint}")
      end
    end

    # Ordered rows actually consumed through one handle.
    def rows_for(handle)
      @rows.select { |row| row.handle == handle }
    end

    def terminals
      @terminal_by_handle.values
    end

    def terminal_for(handle)
      @terminal_by_handle[handle]
    end

    private

    def mark_terminal(handle, kind, message)
      @terminal_by_handle[handle] ||= Terminal.new(handle: handle, kind: kind, message: message)
    end

    def apply(handover_id, action)
      op = action[:op].to_sym
      case op
      when :prepare then prepare(action)
      when :swap_view then swap_view(action)
      when :execute then execute_cursor(handover_id, action)
      when :step then step(handover_id, action)
      when :columns then record_columns(handover_id, action)
      when :close then close_cursor(handover_id, action)
      when :bind then statement_for(action).bind_params(*bind_args(action[:vars]))
      when :reset then statement_for(action).reset!
      when :clear_bindings then statement_for(action).clear_bindings!
      when :pass then nil
      else
        raise Halt, observation(handover_id, action, "unknown op #{action[:op]}")
      end
    rescue SQLite3::Exception => e
      raise Halt, observation(handover_id, action, "SQLite3 #{e.class.name.split("::").last}: #{e.message}")
    end

    def prepare(action)
      sid = action[:statement]
      @statements[sid] = @db.prepare(@audit_case.statement_sql.fetch(sid))
    end

    def swap_view(action)
      name = action[:name] || @audit_case.shared_view_name
      version = action[:view]
      @db.execute("DROP VIEW IF EXISTS \"#{name}\"")
      @db.execute("CREATE VIEW \"#{name}\" AS #{view_body(version)}")
      @view_version_by_name[name] = version
    end

    def execute_cursor(handover_id, action)
      handle = require_handle(handover_id, action)
      sid = action[:statement]
      check_handle_statement(handover_id, action, handle, sid)
      stmt = @statements.fetch(sid)
      mode = (action[:mode] || :array).to_sym

      @db.results_as_hash = (mode == :hash)
      result_set = stmt.execute(*bind_args(action[:vars]))
      snapshot = stmt.columns.map(&:to_sym)
      # A fresh execute resets the VM, so the view-row position of this run
      # starts again at the first row even if an older cursor is still open.
      @statement_row_counters[sid] = 0
      @cursors[handle] = {
        statement: sid, mode: mode, result_set: result_set,
        snapshot: snapshot, delivery: delivery_of(handle), closed: false
      }
    end

    def step(handover_id, action)
      handle = require_handle(handover_id, action)
      cursor = @cursors.fetch(handle) { raise Halt, observation(handover_id, action, "step on unknown handle #{handle}") }
      stmt = @statements.fetch(cursor[:statement])

      if cursor[:closed] || stmt.closed?
        mark_terminal(handle, :error, "cannot use a closed statement")
        return
      end
      return if @terminal_by_handle.key?(handle)

      raw =
        begin
          cursor[:result_set].next
        rescue SQLite3::Exception => e
          mark_terminal(handle, :error, "SQLite3 #{e.class.name.split("::").last}: #{e.message}")
          return
        end

      if raw.nil?
        mark_terminal(handle, :end, nil)
        return
      end

      live_names = (0...stmt.column_count).map { |i| stmt.column_name(i).to_sym }
      version = @view_version_by_name.fetch(@audit_case.view_name_for_statement(cursor[:statement]))
      ordinal = @row_counters[handle]
      @row_counters[handle] += 1
      view_ordinal = @statement_row_counters[cursor[:statement]]
      @statement_row_counters[cursor[:statement]] += 1
      delivery = cursor[:delivery]
      pub_no = @delivery_counters[delivery] + 1
      @delivery_counters[delivery] += 1
      row = Row.new(
        handle: handle, delivery: delivery, mode: cursor[:mode], raw: raw,
        snapshot_names: cursor[:snapshot], live_names: live_names,
        view: version, ordinal: ordinal, view_ordinal: view_ordinal,
        pub_no: pub_no, material_id: nil
      )
      id_position = live_names.index(@provenance.id_column(delivery))
      row[:material_id] = row.ordered_values[id_position] if id_position
      @rows << row
    end

    def record_columns(handover_id, action)
      handle = require_handle(handover_id, action)
      cursor = @cursors.fetch(handle) { raise Halt, observation(handover_id, action, "columns on unknown handle") }
      cursor[:snapshot] = @statements.fetch(cursor[:statement]).columns.map(&:to_sym)
    end

    def close_cursor(handover_id, action)
      handle = require_handle(handover_id, action)
      cursor = @cursors.fetch(handle) { raise Halt, observation(handover_id, action, "close on unknown handle") }
      sid = cursor[:statement]
      statement = @statements.fetch(sid)
      if statement.closed?
        mark_terminal(handle, :error, "cannot use a closed statement")
        return
      end
      statement.close
      @cursors.each_value { |other| other[:closed] = true if other[:statement] == sid }
    end

    def build_database
      @db = SQLite3::Database.new(":memory:")
      @db.results_as_hash = false
      columns = @audit_case.material_columns
      declaration = columns.map { |column| "\"#{column}\"" }.join(", ")
      @db.execute("CREATE TABLE \"#{@audit_case.material_table}\" (#{declaration})")
      @audit_case.material_by_id.each_value do |row|
        placeholders = (["?"] * columns.length).join(", ")
        @db.execute(
          "INSERT INTO \"#{@audit_case.material_table}\" (#{declaration}) VALUES (#{placeholders})",
          columns.map { |column| row[column] }
        )
      end

      @audit_case.initial_views.each do |name, version|
        @db.execute("CREATE VIEW \"#{name}\" AS #{view_body(version)}")
        @view_version_by_name[name] = version
      end
    end

    def view_body(version)
      sql = @audit_case.view_sql.fetch(version)
      match = sql.match(/\ACREATE\s+(?:TEMP(?:ORARY)?\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?[\w.]+["`]?\s+AS\s+(.*)\z/m)
      match ? match[1].strip : sql
    end

    def statement_for(action)
      @statements.fetch(action[:statement])
    end

    def bind_args(vars)
      return [] if vars.nil?

      vars.is_a?(Hash) ? [vars] : Array(vars)
    end

    def require_handle(handover_id, action)
      handle = action[:handle]
      raise Halt, observation(handover_id, action, "action is missing a handle") if handle.nil?

      handle
    end

    def check_handle_statement(handover_id, action, handle, sid)
      expected = @audit_case.handle_to_statement(handle)
      return if expected == sid

      raise Halt, observation(handover_id, action,
        "handle #{handle} belongs to statement #{expected}, not #{sid}")
    end

    def delivery_of(handle)
      @audit_case.handle_to_delivery(handle)
    end

    def observation(handover_id, action, message)
      { handover: handover_id, action: action, message: message }
    end
  end
end
