# frozen_string_literal: true

require "set"
require "sqlite3"

module HandoverAudit
  # Parses and validates a handover-audit specification, and builds the sealed
  # view tables plus the raw material table referenced by original id.
  #
  # A specification is a plain Hash (the YAML/JSON document):
  #
  #   material:
  #     table:  items            # table holding the archived raw rows
  #     id_col: orig_id          # column with the original material number
  #   views:                     # sealed view versions, ordered by version id
  #     - version: v1
  #       sql: "SELECT ..."
  #   statements:                # one prepared statement per shared cursor group
  #     - id: S1
  #       sql: "SELECT * FROM w"
  #   deliveries:                # approval slips, one per delivered shipment
  #     - id: D1
  #       statement: S1
  #       expect_view: v1
  #       columns: { code: orig_id, label: name }
  #       row_map: { method: segment, source: [[1, 1, 100]], step: 1 }
  #   handovers:                 # shift log; entries may use groups of options
  #     - id: h0
  #       actions: [{ op: prepare, statement: S1 }]
  #   groups:                    # named option sets, shared across references
  #     g1: [{ op: step, handle: H1 }, { op: step, handle: H2 }]
  #   ledger:                    # receiving book, one entry per receipt
  #     - delivery: D1
  #       handle: H1
  #       events:
  #         - { type: sample, column: code, value: 7 }
  #         - { type: row, line: 1 }
  #         - { type: end }
  class Case
    class SpecError < StandardError; end

    OPS = %i[prepare execute step columns close swap_view bind reset clear_bindings pass].freeze
    EVENT_TYPES = %i[sample end error row].freeze

    attr_reader :spec, :material_id_col, :view_sql, :view_order, :statement_sql,
      :deliveries, :handovers, :groups, :ledger, :handles, :view_rows,
      :view_columns, :material_by_id, :material_table, :material_columns

    def initialize(spec)
      @spec = symbolize(spec)
      @groups = (@spec[:groups] || {}).to_h do |name, choices|
        [name.to_sym, choices.map { |choice| symbolize(choice) }]
      end
      read_material
      read_views
      read_statements
      read_deliveries
      read_handovers
      read_ledger
      freeze_indexes
    end

    def view_ids
      @view_sql.keys
    end

    # Runs the sealed view SQL in an isolated in-memory database and returns
    # its ordered rows as Hashes keyed by output column name.
    def materialize(view_id)
      @view_rows.fetch(view_id)
    end

    def handle_to_delivery(handle)
      @handle_to_delivery.fetch(handle) do
        raise SpecError, "handle #{handle.inspect} never appears in the ledger"
      end
    end

    def handle_to_statement(handle)
      @deliveries.fetch(handle_to_delivery(handle))[:statement]
    end

    def view_name_for_statement(statement_id)
      info = @statement_info.fetch(statement_id)
      info[:view_name] || @spec.dig(:material, :view_name) || "v"
    end

    # Name a swap_view action installs into when the action omits an explicit
    # name: the material-level view_name, defaulting to "v". Statements that
    # read other view names must be swapped through actions carrying an
    # explicit :name.
    def shared_view_name
      @spec.dig(:material, :view_name) || "v"
    end

    def initial_view_for_statement(statement_id)
      @statement_info.fetch(statement_id)[:initial_view] || @view_order.first
    end

    # View name -> sealed version installed when each statement is prepared.
    def initial_views
      @initial_views ||= begin
        mapping = {}
        @statement_info.each do |sid, _info|
          name = view_name_for_statement(sid)
          version = initial_view_for_statement(sid)
          if mapping.key?(name) && mapping[name] != version
            raise SpecError, "statements sharing view name #{name.inspect} declare different initial views"
          end

          mapping[name] = version
        end
        mapping
      end
    end

    private

    def read_material
      mat = require_hash(:material)
      @material_table = (mat[:table] || "material").to_s
      @material_id_col = (mat[:id_col] || :id).to_sym
      rows = (mat[:rows] || []).map { |r| symbolize(r) }
      raise SpecError, "material rows must be hashes" unless rows.all? { |r| r.is_a?(Hash) }
      raise SpecError, "material id column :#{@material_id_col} missing in some rows" unless
        rows.all? { |r| r.key?(@material_id_col) }

      ids = rows.map { |r| r[@material_id_col] }
      raise SpecError, "material ids must be unique" unless ids.uniq.length == ids.length

      @material_by_id = rows.to_h { |r| [r[@material_id_col], r] }
      @material_rows = rows
      @material_columns = rows.flat_map(&:keys).uniq
      @material_columns = [@material_id_col] if @material_columns.empty?
    end

    def read_views
      @view_sql = {}
      @view_order = []
      list = @spec[:views] || []
      raise SpecError, "at least one sealed view is required" if list.empty?

      list.each do |entry|
        entry = symbolize(entry)
        vid = entry[:version].to_sym
        raise SpecError, "view version missing" if vid.nil?
        raise SpecError, "duplicate view version #{vid.inspect}" if @view_sql.key?(vid)

        @view_sql[vid] = entry[:sql]
        @view_order << vid
      end
      build_views
    end

    def read_statements
      @statement_sql = {}
      @statement_info = {}
      (@spec[:statements] || []).each do |entry|
        entry = symbolize(entry)
        sid = entry[:id].to_sym
        raise SpecError, "statement id missing" if sid.nil?
        raise SpecError, "duplicate statement #{sid.inspect}" if @statement_sql.key?(sid)

        info = entry.dup
        info[:id] = sid
        info[:view_name] = info[:view_name]&.to_s
        info[:initial_view] = info[:initial_view]&.to_sym
        @statement_sql[sid] = entry[:sql]
        @statement_info[sid] = info
      end
      raise SpecError, "at least one statement is required" if @statement_sql.empty?
    end

    def read_deliveries
      @deliveries = {}
      (@spec[:deliveries] || []).each do |entry|
        entry = symbolize(entry)
        did = entry[:id].to_sym
        raise SpecError, "delivery id missing" if did.nil?
        raise SpecError, "duplicate delivery #{did.inspect}" if @deliveries.key?(did)

        sid = entry[:statement].to_sym
        raise SpecError, "delivery #{did} has no statement" unless @statement_sql.key?(sid)
        raise SpecError, "delivery #{did} expects unknown view #{entry[:expect_view].inspect}" unless
          @view_sql.key?(entry[:expect_view].to_sym)

        cols = (entry[:columns] || {}).to_h { |name, field| [name.to_sym, field.to_sym] }
        raise SpecError, "delivery #{did} columns must be a hash" unless cols.is_a?(Hash)

        row_map = parse_row_map(did, entry[:row_map])
        @deliveries[did] = {
          id: did, statement: sid, expect_view: entry[:expect_view].to_sym,
          columns: cols, row_map: row_map, id_column: entry[:id_column]&.to_sym
        }
      end
      raise SpecError, "at least one delivery is required" if @deliveries.empty?
    end

    def parse_row_map(did, raw)
      raw = symbolize(raw || { method: :sequence })
      method = (raw[:method] || :sequence).to_sym
      case method
      when :sequence
        { method: :sequence, step: (raw[:step] || 1).to_i, start: (raw[:start] || 1).to_i }
      when :segment
        segs = (raw[:source] || []).map do |seg|
          pub_first, pub_last, mat_first = seg.map { |n| Integer(n) }
          raise SpecError, "delivery #{did} has an empty publish segment" if pub_last < pub_first

          { pub_first: pub_first, pub_last: pub_last, mat_first: mat_first }
        end
        { method: :segment, source: segs }
      when :fixed
        { method: :fixed, value: Integer(raw[:value]) }
      else
        raise SpecError, "delivery #{did} unknown row_map method #{method.inspect}"
      end
    end

    def read_handovers
      @handovers = []
      (@spec[:handovers] || []).each_with_index do |entry, idx|
        entry = symbolize(entry)
        hid = (entry[:id] || "h#{idx}").to_sym
        raw_actions = entry[:actions] || [entry]
        choices = expand_choices(raw_actions)
        choices.each { |choice| validate_choice(hid, choice) }
        @handovers << { id: hid, choices: choices }
      end
      raise SpecError, "at least one handover entry is required" if @handovers.empty?
    end

    # One handover entry models one shift cell. A plain action hash is the only
    # permitted choice; { group: name } or { any: [...] } turn the cell into an
    # uncertain one, with every option reusing the same shared choice wherever
    # the same group is referenced.
    def expand_choices(raw_actions)
      raise SpecError, "a handover entry must describe one cell (a single action or one group/any slot)" unless
        raw_actions.length == 1

      slot = raw_actions.first
      if slot.key?(:group)
        group = slot[:group].to_sym
        @groups.fetch(group) { raise SpecError, "unknown group #{group.inspect}" }
      elsif slot.key?(:any)
        slot[:any].map { |choice| symbolize(choice) }
      else
        [slot]
      end
    end

    def validate_choice(hid, choice)
      op = choice[:op]
      raise SpecError, "handover #{hid} action missing op" if op.nil?
      raise SpecError, "handover #{hid} unknown op #{op.inspect}" unless OPS.include?(op.to_sym)

      case op.to_sym
      when :prepare, :bind, :reset, :clear_bindings
        choice[:statement] = choice[:statement].to_sym
        raise SpecError, "handover #{hid} op #{op} needs a statement" unless
          @statement_sql.key?(choice[:statement])
      when :execute
        choice[:statement] = choice[:statement].to_sym
        raise SpecError, "handover #{hid} op execute needs a statement" unless
          @statement_sql.key?(choice[:statement])
        raise SpecError, "handover #{hid} op execute needs a handle" if choice[:handle].nil?

        choice[:handle] = choice[:handle].to_sym
        choice[:mode] = choice[:mode].to_sym if choice[:mode]
      when :step, :columns, :close
        raise SpecError, "handover #{hid} op #{op} needs a handle" if choice[:handle].nil?

        choice[:handle] = choice[:handle].to_sym
      when :swap_view
        choice[:view] = choice[:view].to_sym
        raise SpecError, "handover #{hid} swap_view needs a known view" unless
          @view_sql.key?(choice[:view])
      when :pass
        nil
      end
    end

    def read_ledger
      @ledger = []
      @handles = Set.new
      (@spec[:ledger] || []).each do |entry|
        entry = symbolize(entry)
        did = entry[:delivery].to_sym
        raise SpecError, "ledger entry references unknown delivery #{did.inspect}" unless
          @deliveries.key?(did)

        handle = entry[:handle]
        raise SpecError, "ledger entry for #{did} has no handle" if handle.nil?

        handle = handle.to_sym
        events = (entry[:events] || []).map { |ev| parse_event(did, symbolize(ev)) }
        validate_continuous_lines(did, events)
        @handles << handle
        @ledger << { delivery: did, handle: handle, events: events }
      end
      raise SpecError, "at least one ledger receipt is required" if @ledger.empty?
    end

    # A receipt only records the continuous line numbers that were written.
    # Within one receipt those numbers form an increasing contiguous range
    # (a delivery can be spread over several handles, so the first receipt does
    # not have to start at line 1).
    def validate_continuous_lines(did, events)
      lines = events.select { |event| event[:type] == :row }.map { |event| event[:line] }
      lines.each_cons(2) do |previous, current|
        raise SpecError, "ledger for #{did} has a gap: lines #{previous} and #{current} are not consecutive" unless
          current == previous + 1
      end
    end

    def parse_event(did, ev)
      type = ev[:type]
      raise SpecError, "ledger entry for #{did} event missing type" if type.nil?
      raise SpecError, "ledger entry for #{did} unknown event #{type.inspect}" unless
        EVENT_TYPES.include?(type.to_sym)

      case type.to_sym
      when :sample
        raise SpecError, "ledger sample for #{did} needs column and value" unless
          ev.key?(:column) && ev.key?(:value)
        { type: :sample, column: ev[:column].to_sym, value: ev[:value] }
      when :error
        { type: :error, message: ev[:message] }
      when :end
        { type: :end }
      when :row
        line = ev[:line] || ev[:no]
        raise SpecError, "ledger row event for #{did} needs a line number" if line.nil?

        { type: :row, line: Integer(line) }
      end
    end

    def freeze_indexes
      @handle_to_delivery = @ledger.to_h { |entry| [entry[:handle], entry[:delivery]] }
      @handle_to_statement = @handle_to_delivery.transform_values { |did| @deliveries[did][:statement] }
      @material_table.freeze
      @material_id_col.freeze
    end

    # Builds every sealed view in one isolated database so the audit never
    # touches live data; view body selection, ordering and templating are taken
    # verbatim from the sealed handover material.
    def build_views
      db = SQLite3::Database.new(":memory:")
      cols = @material_rows.first&.keys || [@material_id_col]
      col_decl = cols.map { |c| "\"#{c}\"" }.join(", ")
      db.execute("CREATE TABLE \"#{@material_table}\" (#{col_decl})")
      @material_rows.each do |row|
        values = cols.map { |c| row[c] }
        placeholders = (["?"] * cols.length).join(", ")
        db.execute("INSERT INTO \"#{@material_table}\" (#{col_decl}) VALUES (#{placeholders})", values)
      end

      @view_rows = {}
      @view_columns = {}
      @view_order.each_with_index do |vid, index|
        name = "sealed_view_#{index}"
        db.execute("CREATE VIEW \"#{name}\" AS #{view_body(vid)}")
        stmt = db.prepare("SELECT * FROM \"#{name}\"")
        names = stmt.columns.map(&:to_sym)
        hashed = stmt.to_a.map { |row| names.zip(row).to_h }
        stmt.close
        @view_rows[vid] = hashed
        @view_columns[vid] = names
      end
    ensure
      db&.close
    end

    # Strips a CREATE VIEW wrapper down to the SELECT body so a sealed view can
    # be reinstalled under any name during replay.
    public def view_body(version)
      sql = @view_sql.fetch(version)
      match = sql.match(/\ACREATE\s+(?:TEMP(?:ORARY)?\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?[\w.]+["`]?\s+AS\s+(.*)\z/m)
      match ? match[1].strip : sql
    end

    # Name a CREATE VIEW statement installs under; bare SELECT bodies use the
    # explicit name or the version id.
    def view_name(vid)
      @spec[:views].each do |entry|
        entry = symbolize(entry)
        next unless entry[:version] == vid

        return entry[:name].to_s if entry[:name]

        match = entry[:sql].to_s.match(/\ACREATE\s+(?:TEMP(?:ORARY)?\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?([\w.]+)["`]?/)
        return match[1] if match
      end
      vid.to_s
    end

    def require_hash(key)
      value = @spec[key]
      raise SpecError, "missing #{key} section" unless value.is_a?(Hash)

      value
    end

    def symbolize(value)
      case value
      when Hash
        value.to_h { |k, v| [k.to_sym, symbolize(v)] }
      when Array
        value.map { |item| symbolize(item) }
      else
        value
      end
    end
  end
end
