# frozen_string_literal: true

require 'minitest/autorun'
require 'ridgepole/rename_planner'

class RenamePlannerTest < Minitest::Test
  def plan(catalog, moves)
    Ridgepole::RenamePlanner.plan(catalog, moves)
  end

  def sql_statements(result)
    result.steps.map(&:sql)
  end

  def test_simple_rename_without_conflict
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'orders' },
      ],
      indexes: [
        { oid: 102, schema: 'public', name: 'idx_orders_date', table: 101 },
      ],
      sequences: [
        { oid: 103, schema: 'public', name: 'orders_id_seq', owned_by: { table: 101, column: 'id' } },
      ],
      temp_names: ['tmp_1'],
    }
    result = plan(catalog, [{ from: 101, to: { schema: 'public', name: 'orders_archive' } }])

    assert_equal ['ALTER TABLE "public"."orders" RENAME TO "orders_archive";'], sql_statements(result)
    assert_equal 0, result.dependent_temp_rename_count

    table = result.final_objects.find { |o| o.oid == '101' }
    assert_equal 'orders_archive', table.name
    assert_equal 'public', table.schema
    sequence = result.final_objects.find { |o| o.oid == '103' }
    assert_equal '101', sequence.table_oid
    assert_equal 'id', sequence.owner_column
  end

  def test_swap_two_tables_in_same_schema
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'a' },
        { oid: 102, kind: 'table', schema: 'public', name: 'b' },
      ],
      temp_names: ['tmp_1'],
    }
    result = plan(catalog, [
                    { from: 101, to: { schema: 'public', name: 'b' } },
                    { from: 102, to: { schema: 'public', name: 'a' } },
                  ])

    assert_equal [
      'ALTER TABLE "public"."a" RENAME TO "tmp_1";',
      'ALTER TABLE "public"."b" RENAME TO "a";',
      'ALTER TABLE "public"."tmp_1" RENAME TO "b";',
    ], sql_statements(result)
    assert_equal 0, result.dependent_temp_rename_count
  end

  def test_move_table_to_other_schema_keeps_attached_objects
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'orders_2024' },
        { oid: 201, kind: 'table', schema: 'public', name: 'customers' },
      ],
      indexes: [
        { oid: 102, schema: 'public', name: 'idx_orders_date', table: 101 },
      ],
      sequences: [
        { oid: 103, schema: 'public', name: 'orders_2024_id_seq', owned_by: { table: 101, column: 'id' } },
      ],
      foreign_keys: [
        { oid: 104, name: 'fk_orders_customers', from_table: 101, to_table: 201 },
      ],
      temp_names: [],
    }
    result = plan(catalog, [{ from: 101, to: { schema: 'archive', name: 'orders_2024' } }])

    assert_equal ['ALTER TABLE "public"."orders_2024" SET SCHEMA "archive";'], sql_statements(result)

    step = result.steps.first
    assert_includes step.prechecks, ['archive', 'orders_2024']
    assert_includes step.prechecks, ['archive', 'idx_orders_date']
    assert_includes step.prechecks, ['archive', 'orders_2024_id_seq']

    by_oid = result.final_objects.to_h { |o| [o.oid, o] }
    assert_equal 'archive', by_oid['102'].schema
    assert_equal 'idx_orders_date', by_oid['102'].name
    assert_equal 'archive', by_oid['103'].schema
    assert_equal '101', by_oid['103'].table_oid

    fk = result.final_foreign_keys.first
    assert_equal '101', fk[:from_table]
    assert_equal '201', fk[:to_table]
    assert_equal 'public.customers', fk[:target]
  end

  def test_cross_schema_swap_with_colliding_index_names
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'ta' },
        { oid: 102, kind: 'table', schema: 'archive', name: 'tb' },
      ],
      indexes: [
        { oid: 111, schema: 'public', name: 'idx_value', table: 101 },
        { oid: 112, schema: 'archive', name: 'idx_value', table: 102 },
      ],
      temp_names: ['tmp_1'],
    }
    result = plan(catalog, [
                    { from: 101, to: { schema: 'archive', name: 'ta' } },
                    { from: 102, to: { schema: 'public', name: 'tb' } },
                  ])

    assert_equal 1, result.dependent_temp_rename_count
    statements = sql_statements(result)
    assert_equal 'ALTER INDEX "public"."idx_value" RENAME TO "tmp_1";', statements[0]
    assert_equal 'ALTER TABLE "archive"."tb" SET SCHEMA "public";', statements[1]
    assert_equal 'ALTER TABLE "public"."ta" SET SCHEMA "archive";', statements[2]
    assert_equal 'ALTER INDEX "archive"."tmp_1" RENAME TO "idx_value";', statements[3]

    by_oid = result.final_objects.to_h { |o| [o.oid, o] }
    assert_equal %w[archive idx_value], [by_oid['111'].schema, by_oid['111'].name]
    assert_equal %w[public idx_value], [by_oid['112'].schema, by_oid['112'].name]
  end

  def test_cross_schema_swap_of_tables_with_same_name
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'orders' },
        { oid: 102, kind: 'table', schema: 'archive', name: 'orders' },
      ],
      temp_names: ['tmp_1'],
    }
    result = plan(catalog, [
                    { from: 101, to: { schema: 'archive', name: 'orders' } },
                    { from: 102, to: { schema: 'public', name: 'orders' } },
                  ])

    assert_equal 0, result.dependent_temp_rename_count
    statements = sql_statements(result)
    assert_equal 4, statements.length
    assert_equal 'ALTER TABLE "public"."orders" RENAME TO "tmp_1";', statements[0]
    assert_equal 'ALTER TABLE "archive"."orders" SET SCHEMA "public";', statements[1]
    assert_equal ['ALTER TABLE "public"."tmp_1" SET SCHEMA "archive";',
                  'ALTER TABLE "archive"."tmp_1" RENAME TO "orders";'], statements[2..]
  end

  def test_move_and_rename_table
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'orders' },
      ],
      temp_names: [],
    }
    result = plan(catalog, [{ from: 101, to: { schema: 'archive', name: 'orders_2024' } }])

    assert_equal [
      'ALTER TABLE "public"."orders" RENAME TO "orders_2024";',
      'ALTER TABLE "public"."orders_2024" SET SCHEMA "archive";',
    ], sql_statements(result)
  end

  def test_move_and_rename_uses_temp_name_when_both_direct_names_are_taken
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'orders' },
        { oid: 102, kind: 'view', schema: 'public', name: 'orders_2024' },
        { oid: 103, kind: 'view', schema: 'archive', name: 'orders' },
      ],
      temp_names: ['tmp_1'],
    }
    result = plan(catalog, [{ from: 101, to: { schema: 'archive', name: 'orders_2024' } }])

    assert_equal [
      'ALTER TABLE "public"."orders" RENAME TO "tmp_1";',
      'ALTER TABLE "public"."tmp_1" SET SCHEMA "archive";',
      'ALTER TABLE "archive"."tmp_1" RENAME TO "orders_2024";',
    ], sql_statements(result)
    assert_equal 0, result.dependent_temp_rename_count
  end

  def test_chained_moves_fall_back_to_temp_path_after_deadlock_is_broken
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'a' },
        { oid: 102, kind: 'table', schema: 'public', name: 'b' },
        { oid: 103, kind: 'table', schema: 'archive', name: 'a' },
      ],
      temp_names: %w[tmp_1 tmp_2],
    }
    result = plan(catalog, [
                    { from: 101, to: { schema: 'archive', name: 'b' } },
                    { from: 102, to: { schema: 'archive', name: 'a' } },
                    { from: 103, to: { schema: 'public', name: 'b' } },
                  ])

    assert_equal [
      'ALTER TABLE "public"."b" RENAME TO "tmp_1";',
      'ALTER TABLE "public"."a" RENAME TO "b";',
      'ALTER TABLE "public"."b" SET SCHEMA "archive";',
      'ALTER TABLE "archive"."a" SET SCHEMA "public";',
      'ALTER TABLE "public"."a" RENAME TO "b";',
      'ALTER TABLE "public"."tmp_1" RENAME TO "a";',
      'ALTER TABLE "public"."a" SET SCHEMA "archive";',
    ], sql_statements(result)
    assert_equal 0, result.dependent_temp_rename_count
  end

  def test_view_blocking_moved_index_name_is_reported_as_conflict
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'orders' },
        { oid: 102, kind: 'view', schema: 'archive', name: 'idx_orders_date' },
      ],
      indexes: [
        { oid: 111, schema: 'public', name: 'idx_orders_date', table: 101 },
      ],
      temp_names: %w[tmp_1 tmp_2],
    }

    error = assert_raises(Ridgepole::RenamePlanner::ConflictError) do
      plan(catalog, [{ from: 101, to: { schema: 'archive', name: 'orders' } }])
    end
    details = error.conflicts.join("\n")
    assert_match(/idx_orders_date/, details)
    assert(error.conflicts.any? { |c| c.include?('view') && c.include?('102') })
    assert(error.conflicts.any? { |c| c.include?('index') && c.include?('111') })
  end

  def test_duplicate_target_is_reported_as_conflict
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'a' },
        { oid: 102, kind: 'table', schema: 'archive', name: 'b' },
      ],
      temp_names: ['tmp_1'],
    }

    error = assert_raises(Ridgepole::RenamePlanner::ConflictError) do
      plan(catalog, [
             { from: 101, to: { schema: 'archive', name: 'b' } },
             { from: 102, to: { schema: 'archive', name: 'b' } },
           ])
    end
    assert(error.conflicts.any? { |c| c.include?('101') && c.include?('102') })
  end

  def test_target_occupied_by_object_outside_migration_is_reported
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'orders' },
        { oid: 102, kind: 'table', schema: 'archive', name: 'orders_2024' },
      ],
      temp_names: ['tmp_1'],
    }

    error = assert_raises(Ridgepole::RenamePlanner::ConflictError) do
      plan(catalog, [{ from: 101, to: { schema: 'archive', name: 'orders_2024' } }])
    end
    assert(error.conflicts.any? { |c| c.include?('102') })
  end

  def test_exhausted_temp_names_report_conflicting_objects
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'a' },
        { oid: 102, kind: 'table', schema: 'public', name: 'b' },
      ],
      temp_names: [],
    }

    error = assert_raises(Ridgepole::RenamePlanner::ConflictError) do
      plan(catalog, [
             { from: 101, to: { schema: 'public', name: 'b' } },
             { from: 102, to: { schema: 'public', name: 'a' } },
           ])
    end
    assert_match(/temporary/, error.message)
    assert(!error.conflicts.empty?)
  end

  def test_every_statement_carries_a_name_free_precheck
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'a' },
        { oid: 102, kind: 'table', schema: 'public', name: 'b' },
      ],
      temp_names: ['tmp_1'],
    }
    result = plan(catalog, [
                    { from: 101, to: { schema: 'public', name: 'b' } },
                    { from: 102, to: { schema: 'public', name: 'a' } },
                  ])

    assert(result.steps.all? { |s| !s.prechecks.empty? })
    sql = result.to_sql
    assert_match(/DO \$\$/, sql)
    assert_match(/pg_class/, sql)
    assert_match(/ALTER TABLE/, sql)
    assert_match(/final catalog/, sql)
    assert_match(/oid=101/, sql)
    refute_match(/DO \$\$/, result.to_sql(guards: false))
  end

  def test_move_mapping_accepts_hash_form_and_string_keys
    catalog = {
      'relations' => [
        { 'oid' => 101, 'kind' => 'table', 'schema' => 'public', 'name' => 'orders' },
      ],
      'temp_names' => [],
    }
    result = plan(catalog, { 101 => { 'schema' => 'archive', 'name' => 'orders' } })

    assert_equal ['ALTER TABLE "public"."orders" SET SCHEMA "archive";'], sql_statements(result)
  end

  def test_move_source_must_be_a_table
    catalog = {
      relations: [
        { oid: 101, kind: 'view', schema: 'public', name: 'v_orders' },
      ],
      temp_names: [],
    }

    error = assert_raises(Ridgepole::RenamePlanner::ConflictError) do
      plan(catalog, [{ from: 101, to: { schema: 'archive', name: 'v_orders' } }])
    end
    assert_match(/not a table/, error.conflicts.first)
  end

  def test_cycle_prefers_temp_renaming_a_table_over_a_dependent
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'a' },
        { oid: 102, kind: 'table', schema: 'public', name: 'b' },
      ],
      indexes: [
        { oid: 111, schema: 'public', name: 'idx_a', table: 101 },
      ],
      temp_names: ['tmp_1'],
    }
    result = plan(catalog, [
                    { from: 101, to: { schema: 'public', name: 'b' } },
                    { from: 102, to: { schema: 'public', name: 'a' } },
                  ])

    assert_equal 0, result.dependent_temp_rename_count
    assert_equal 'b', result.final_objects.find { |o| o.oid == '101' }.name
    assert_equal 'a', result.final_objects.find { |o| o.oid == '102' }.name
  end

  def test_full_swap_with_sequence_needs_one_dependent_temp_rename
    catalog = {
      relations: [
        { oid: 101, kind: 'table', schema: 'public', name: 'orders' },
        { oid: 102, kind: 'table', schema: 'archive', name: 'orders' },
        { oid: 103, kind: 'table', schema: 'public', name: 'customers' },
      ],
      indexes: [
        { oid: 111, schema: 'public', name: 'idx_orders_date', table: 101 },
        { oid: 112, schema: 'archive', name: 'idx_orders_date', table: 102 },
      ],
      sequences: [
        { oid: 113, schema: 'public', name: 'orders_id_seq', owned_by: { table: 101, column: 'id' } },
      ],
      foreign_keys: [
        { oid: 114, name: 'fk_orders_customers', from_table: 101, to_table: 103 },
      ],
      temp_names: %w[tmp_1 tmp_2],
    }
    moves = [
      { from: 101, to: { schema: 'archive', name: 'orders' } },
      { from: 102, to: { schema: 'public', name: 'orders' } },
    ]
    result = plan(catalog, moves)

    assert_equal 1, result.dependent_temp_rename_count
    assert_equal ['111'], result.temp_renamed_dependents.map(&:oid)

    by_oid = result.final_objects.to_h { |o| [o.oid, o] }
    assert_equal %w[archive orders], [by_oid['101'].schema, by_oid['101'].name]
    assert_equal %w[public orders], [by_oid['102'].schema, by_oid['102'].name]
    assert_equal %w[archive idx_orders_date], [by_oid['111'].schema, by_oid['111'].name]
    assert_equal %w[public idx_orders_date], [by_oid['112'].schema, by_oid['112'].name]
    assert_equal %w[archive orders_id_seq], [by_oid['113'].schema, by_oid['113'].name]
    assert_equal '101', by_oid['113'].table_oid
    assert_equal 'id', by_oid['113'].owner_column
    assert_equal 'public.customers', result.final_foreign_keys.first[:target]

    # one reserved name is not enough for this swap
    error = assert_raises(Ridgepole::RenamePlanner::ConflictError) do
      plan(catalog.merge(temp_names: ['tmp_1']), moves)
    end
    assert_match(/temporary/, error.message)
  end
end
