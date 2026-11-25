require "active_support"
require "active_support/core_ext/time/zones"

module Groupdate
  # Versioned minute-ledger and display-order audit entry point.
  #
  # A ledger keeps immutable versions of UTC minute buckets (integer totals).
  # Every version is a full materialized table, so older versions stay
  # queryable after newer ones are created. A ticket takes an existing version
  # as its base and applies at most MAX_TICKET_ENTRIES minute changes, each an
  # integer total or a revocation.
  #
  # Display keys are New York time labels ("%Y-%m-%d %H:%M"). Real zero totals
  # count as data. Sparse displays order labels by the first UTC minute at
  # which a label appears, ascending, and show the total of the last UTC minute
  # contributing to that label. Filled displays enumerate every minute between
  # a version's first and last data minute, carrying the nearest prior total
  # forward into empty slots.
  class MinuteLedger
    TIME_ZONE_NAME = "America/New_York"
    LABEL_FORMAT = "%Y-%m-%d %H:%M"
    MAX_TICKET_ENTRIES = 8
    MAX_WINDOW_ROWS = 16
    INSPECTION_BUDGET = 1024

    class Error < Groupdate::Error; end

    attr_reader :inspected_count

    def self.time_zone
      ActiveSupport::TimeZone[TIME_ZONE_NAME]
    end

    # epoch minute (UTC) for a Time-like value or integer
    def self.minute_of(value)
      case value
      when Integer
        value
      when Time, ActiveSupport::TimeWithZone, DateTime
        value.utc.to_i / 60
      else
        if value.respond_to?(:to_time)
          value.to_time.utc.to_i / 60
        else
          raise ArgumentError, "cannot convert #{value.inspect} to a UTC minute"
        end
      end
    end

    def self.label_for(minute)
      Time.at(minute * 60).utc.in_time_zone(time_zone).strftime(LABEL_FORMAT)
    end

    def initialize(connection: nil)
      @connection = connection
      @inspected_count = 0
    end

    def connection
      @connection || ActiveRecord::Base.connection
    end

    def install!
      connection.create_table :gd_minute_versions, force: false do |t|
        t.integer :base_id
        t.string :note
        t.timestamp :created_at
      end unless connection.table_exists?(:gd_minute_versions)

      unless connection.table_exists?(:gd_minute_buckets)
        connection.create_table :gd_minute_buckets, id: false, force: false do |t|
          t.integer :version_id, null: false
          t.integer :minute, null: false
          t.integer :total, null: false
          t.string :label, null: false
        end
        connection.execute "CREATE UNIQUE INDEX idx_gd_buckets_version_minute ON gd_minute_buckets(version_id, minute)"
        connection.execute "CREATE INDEX idx_gd_buckets_version_label ON gd_minute_buckets(version_id, label)"
      end

      unless connection.table_exists?(:gd_minute_tickets)
        connection.create_table :gd_minute_tickets, id: false, force: false do |t|
          t.integer :version_id, null: false
          t.integer :position, null: false
          t.integer :minute, null: false
          t.integer :total
          t.boolean :revoked, null: false, default: false
        end
      end

      unless connection.table_exists?(:gd_minute_index)
        connection.create_table :gd_minute_index, id: false, force: false do |t|
          t.integer :minute, null: false
          t.integer :total, null: false
          t.string :label, null: false
        end
        connection.execute "CREATE UNIQUE INDEX idx_gd_index_minute ON gd_minute_index(minute)"
      end

      unless connection.table_exists?(:gd_minute_meta)
        connection.create_table :gd_minute_meta, id: false, force: false do |t|
          t.string :key, null: false
          t.string :value
        end
        connection.execute "CREATE UNIQUE INDEX idx_gd_meta_key ON gd_minute_meta(key)"
      end
    end

    # One-time pre-build of the archive index. Runs the real
    # group_by_minute(...).sum(...) query (without the formatting series) and
    # stores the raw UTC minute buckets and integer totals wholesale.
    def build_index!(relation, time_column, amount_column)
      install!
      if meta_value("index_built")
        raise Error, "minute index has already been built"
      end

      raw = relation.group_by_minute(time_column, series: false).sum(amount_column)
      now = Time.now.utc
      rows = raw.map do |key, total|
        minute = key.utc.to_i / 60
        quote_row([minute, total.to_i, label_for(minute)])
      end
      bulk_insert("gd_minute_index", %w[minute total label], rows) unless rows.empty?
      set_meta("index_built", now.iso8601)
      rows.size
    end

    # Materializes version 0 from the archive index in one whole-table copy.
    def seed_version!(note: "archive seed")
      install!
      unless meta_value("index_built")
        raise Error, "minute index has not been built"
      end
      if connection.select_value("SELECT COUNT(*) FROM gd_minute_versions").to_i > 0
        raise Error, "a seeded version already exists"
      end

      version_id = insert_version(nil, note)

      connection.execute(
        "INSERT INTO gd_minute_buckets(version_id, minute, total, label) " \
        "SELECT #{version_id}, minute, total, label FROM gd_minute_index"
      )
      version_id
    end

    # Applies a ticket on top of a base version and returns the new version id.
    # +changes+ maps UTC minutes (integers or Time-like) to integer totals or
    # +:revoke+. At most MAX_TICKET_ENTRIES entries. The base version is never
    # mutated.
    def submit_ticket!(base_version_id:, changes:, note: nil)
      install!
      entries = normalize_changes(changes)

      base_version_id = base_version_id.to_i
      unless version_exists?(base_version_id)
        raise Error, "base version #{base_version_id} does not exist"
      end

      entries.each do |minute, kind, total|
        if kind == :revoke
          unless bucket_minute?(base_version_id, minute)
            raise Error, "cannot revoke missing minute #{minute}"
          end
        else
          index_bucket(minute) # cached single-item lookup, metered
        end
      end

      version_id = insert_version(base_version_id, note || "ticket on #{base_version_id}")
      connection.execute(
        "INSERT INTO gd_minute_buckets(version_id, minute, total, label) " \
        "SELECT #{version_id}, minute, total, label FROM gd_minute_buckets WHERE version_id = #{base_version_id}"
      )

      entries.each_with_index do |(minute, kind, total), position|
        if kind == :revoke
          connection.execute(
            "DELETE FROM gd_minute_buckets WHERE version_id = #{version_id} AND minute = #{minute}"
          )
        else
          upsert_bucket(version_id, minute, total)
        end
        connection.execute(
          "INSERT INTO gd_minute_tickets(version_id, position, minute, total, revoked) " \
          "VALUES (#{version_id}, #{position}, #{minute}, #{total.nil? ? "NULL" : total}, #{kind == :revoke ? 1 : 0})"
        )
      end

      version_id
    end

    def versions
      connection.select_all(
        "SELECT id, base_id, note, created_at FROM gd_minute_versions ORDER BY id"
      ).to_a
    end

    def ticket_entries(version_id)
      connection.select_all(
        "SELECT position, minute, total, revoked FROM gd_minute_tickets " \
        "WHERE version_id = #{version_id.to_i} ORDER BY position"
      ).to_a
    end

    # Display rows for a version, restricted to a half-open sequence window
    # (at most MAX_WINDOW_ROWS rows).
    #
    # Sparse mode keys rows by New York label. Filled mode keys rows by UTC
    # minute across the version span and carries totals forward.
    def display(version_id, fill: false, window: 0...MAX_WINDOW_ROWS)
      window = normalize_window(window)
      rows =
        if fill
          filled_rows(version_id.to_i, window)
        else
          sparse_rows(version_id.to_i, window)
        end
      rows.each { |row| @inspected_count += 1 }
      rows
    end

    # Audit of two versions over the same half-open window. A display key is
    # listed once when either side adds/deletes it, reorders it, changes its
    # total, or changes the real minute that produced the total.
    def diff(left_version_id, right_version_id, fill: false, window: 0...MAX_WINDOW_ROWS)
      window = normalize_window(window)
      left = display_without_reset(left_version_id, fill: fill, window: window)
      right = display_without_reset(right_version_id, fill: fill, window: window)

      keys = (left.map { |row| row[:key] } + right.map { |row| row[:key] }).uniq
      changes = []
      keys.each do |key|
        lhs = left.find { |row| row[:key] == key }
        rhs = right.find { |row| row[:key] == key }
        next if lhs && rhs && lhs[:position] == rhs[:position] &&
                lhs[:total] == rhs[:total] &&
                lhs[:source_minute] == rhs[:source_minute]

        changes << {
          key: key,
          label: (lhs || rhs)[:label],
          left: lhs && compact_row(lhs),
          right: rhs && compact_row(rhs)
        }
      end
      order_changes(changes)
    end

    # Whole-table recomputation in Ruby. Single segment read of every bucket of
    # the version, then the display rules are rerun independently of the SQL
    # window path, so the small ledger can be checked against a full rerun.
    def replay(version_id, fill: false)
      version_id = version_id.to_i
      buckets =
        connection.select_all(
          "SELECT minute, total FROM gd_minute_buckets WHERE version_id = #{version_id} ORDER BY minute"
        ).to_a.map { |row| [row["minute"].to_i, row["total"].to_i] }

      Groupdate::MinuteLedger::Display.build(buckets, fill: fill)
    end

    # Full-table rerun compared against the SQL window path, page by page.
    def audit!(version_id, fill: false)
      expected = replay(version_id, fill: fill)
      return true if expected.empty?

      pages = (expected.size.to_f / MAX_WINDOW_ROWS).ceil
      pages.times do |page|
        start_row = page * MAX_WINDOW_ROWS
        window = start_row...(start_row + MAX_WINDOW_ROWS)
        actual = display(version_id, fill: fill, window: window)
        expected_slice = expected[window].map { |row| row.merge(key: row[:key]) }
        unless equivalent?(expected_slice, actual)
          raise Error, "ledger audit failed for version #{version_id} at window #{window}"
        end
      end
      true
    end

    def reset_meter!
      @inspected_count = 0
    end

    def within_budget?
      @inspected_count <= INSPECTION_BUDGET
    end

    private

    def display_without_reset(version_id, fill:, window:)
      rows = fill ? filled_rows(version_id.to_i, window) : sparse_rows(version_id.to_i, window)
      rows.each { |row| @inspected_count += 1 }
      rows
    end

    # Sparse display, set-based SQL. The engine scans the version's buckets as
    # one segment; only the window's rows materialize into Ruby.
    def sparse_rows(version_id, window)
      sql = <<~SQL
        WITH ranked AS (
          SELECT minute, total, label,
                 row_number() OVER (PARTITION BY label ORDER BY minute DESC) AS dup_rank,
                 MIN(minute) OVER (PARTITION BY label) AS first_minute,
                 MAX(minute) OVER (PARTITION BY label) AS contributed_minute
          FROM gd_minute_buckets
          WHERE version_id = #{version_id}
        ), kept AS (
          SELECT minute, total, label, first_minute, contributed_minute
          FROM ranked WHERE dup_rank = 1
        ), ordered AS (
          SELECT label, total, minute AS source_minute, contributed_minute, first_minute,
                 row_number() OVER (ORDER BY first_minute ASC) - 1 AS position
          FROM kept
        )
        SELECT label, total, source_minute, contributed_minute, position
        FROM ordered
        WHERE position BETWEEN #{window.begin} AND #{window.end - 1}
        ORDER BY position
      SQL
      connection.select_all(sql).to_a.map do |row|
        minute = row["source_minute"].to_i
        {
          key: row["label"],
          label: row["label"],
          position: row["position"].to_i,
          total: row["total"].to_i,
          contributed_minute: row["contributed_minute"].to_i,
          source_minute: minute
        }
      end
    end

    # Filled display. The recursive sequence and carry-forward subqueries run
    # inside SQLite; only window rows return to Ruby.
    def filled_rows(version_id, window)
      bounds = connection.select_one(
        "SELECT MIN(minute) AS lo, MAX(minute) AS hi FROM gd_minute_buckets WHERE version_id = #{version_id}"
      )
      return [] if bounds.nil? || bounds["lo"].nil?

      lo = bounds["lo"].to_i
      hi = bounds["hi"].to_i
      first_minute = lo + window.begin
      last_minute = [lo + window.end - 1, hi].min
      return [] if first_minute > hi

      sql = <<~SQL
        WITH recursive seq(minute) AS (
          SELECT #{first_minute}
          UNION ALL
          SELECT minute + 1 FROM seq WHERE minute < #{last_minute}
        )
        SELECT s.minute AS minute,
               b.minute AS data_minute,
               (SELECT total FROM gd_minute_buckets
                 WHERE version_id = #{version_id} AND minute <= s.minute
                 ORDER BY minute DESC LIMIT 1) AS total,
               (SELECT minute FROM gd_minute_buckets
                 WHERE version_id = #{version_id} AND minute <= s.minute
                 ORDER BY minute DESC LIMIT 1) AS source_minute
        FROM seq s
        LEFT JOIN gd_minute_buckets b
          ON b.version_id = #{version_id} AND b.minute = s.minute
        ORDER BY s.minute
      SQL
      connection.select_all(sql).to_a.map do |row|
        minute = row["minute"].to_i
        source = row["source_minute"].to_i
        {
          key: minute,
          label: label_for(minute),
          position: minute - lo,
          total: row["total"].to_i,
          contributed_minute: row["data_minute"] ? minute : source,
          source_minute: source
        }
      end
    end

    def normalize_changes(changes)
      raise ArgumentError, "ticket changes cannot be empty" if changes.empty?
      raise ArgumentError, "ticket changes cannot exceed #{MAX_TICKET_ENTRIES} minutes" if changes.size > MAX_TICKET_ENTRIES

      entries = []
      changes.each do |raw_minute, value|
        minute = self.class.minute_of(raw_minute)
        if value == :revoke
          entries << [minute, :revoke, nil]
        elsif value.is_a?(Integer)
          entries << [minute, :set, value]
        else
          raise ArgumentError, "change for #{minute} must be an integer total or :revoke"
        end
      end
      minutes = entries.map(&:first)
      if minutes.uniq.size != minutes.size
        raise ArgumentError, "ticket contains duplicate minutes"
      end
      entries.sort_by(&:first)
    end

    def normalize_window(window)
      raise ArgumentError, "window must be a Range" unless window.is_a?(Range)
      raise ArgumentError, "window must be half-open" unless window.exclude_end?
      raise ArgumentError, "window must start at or after zero" if window.begin < 0
      size = window.end - window.begin
      raise ArgumentError, "window can contain at most #{MAX_WINDOW_ROWS} rows" if size > MAX_WINDOW_ROWS
      raise ArgumentError, "window cannot be empty" if size <= 0
      window
    end

    def compact_row(row)
      {
        position: row[:position],
        total: row[:total],
        contributed_minute: row[:contributed_minute],
        source_minute: row[:source_minute]
      }
    end

    def order_changes(changes)
      changes.sort_by { |change| [change[:left]&.dig(:position) || Float::INFINITY, change[:right]&.dig(:position) || Float::INFINITY, change[:key].to_s] }
    end

    def equivalent?(expected, actual)
      expected == actual
    end

    def version_exists?(version_id)
      connection.select_value("SELECT COUNT(*) FROM gd_minute_versions WHERE id = #{version_id}").to_i > 0
    end

    def bucket_minute?(version_id, minute)
      @inspected_count += 1
      connection.select_value(
        "SELECT COUNT(*) FROM gd_minute_buckets WHERE version_id = #{version_id} AND minute = #{minute}"
      ).to_i > 0
    end

    def index_bucket(minute)
      @inspected_count += 1
      connection.select_all(
        "SELECT minute, total FROM gd_minute_index WHERE minute = #{minute}"
      ).to_a.first
    end

    def upsert_bucket(version_id, minute, total)
      label = label_for(minute)
      connection.execute(
        "INSERT INTO gd_minute_buckets(version_id, minute, total, label) " \
        "VALUES (#{version_id}, #{minute}, #{total}, #{connection.quote(label)}) " \
        "ON CONFLICT(version_id, minute) DO UPDATE SET total = excluded.total, label = excluded.label"
      )
    end

    def insert_version(base_id, note)
      quoted_base = base_id.nil? ? "NULL" : base_id.to_i
      connection.execute(
        "INSERT INTO gd_minute_versions(base_id, note, created_at) " \
        "VALUES (#{quoted_base}, #{connection.quote(note)}, #{connection.quote(Time.now.utc.iso8601)})"
      )
      connection.select_value("SELECT last_insert_rowid()").to_i
    end

    def meta_value(key)
      row = connection.select_all("SELECT value FROM gd_minute_meta WHERE key = #{connection.quote(key)}").to_a.first
      row && row["value"]
    end

    def set_meta(key, value)
      connection.execute(
        "INSERT INTO gd_minute_meta(key, value) VALUES (#{connection.quote(key)}, #{connection.quote(value)}) " \
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
      )
    end

    def bulk_insert(table, columns, quoted_rows)
      quoted_rows.each_slice(1000) do |slice|
        connection.execute(
          "INSERT INTO #{table}(#{columns.join(", ")}) VALUES #{slice.join(", ")}"
        )
      end
    end

    def quote_row(values)
      "(#{values.map { |value| value.is_a?(String) ? connection.quote(value) : value.to_s }.join(", ")})"
    end

    def label_for(minute)
      self.class.label_for(minute)
    end
  end
end

require_relative "minute_ledger/display"
