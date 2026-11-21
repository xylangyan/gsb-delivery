import sqlite3
import unittest

from pypika import Case, Field, JoinException, JoinType, Query, QueryException, Table, Tables, Tuple, functions
from pypika.utils import AliasException
from pypika.dialects import (
    ClickHouseQuery,
    ClickHouseQueryBuilder,
    MSSQLQuery,
    MSSQLQueryBuilder,
    MySQLLoadQueryBuilder,
    MySQLQuery,
    MySQLQueryBuilder,
    OracleQuery,
    OracleQueryBuilder,
    PostgreSQLQuery,
    PostgreSQLQueryBuilder,
    RedshiftQuery,
    RedShiftQueryBuilder,
    SnowflakeQuery,
    SnowflakeQueryBuilder,
    SQLLiteQuery,
    SQLLiteQueryBuilder,
    VerticaCopyQueryBuilder,
    VerticaCreateQueryBuilder,
    VerticaQuery,
    VerticaQueryBuilder,
)
from pypika.queries import CreateQueryBuilder, DropQueryBuilder, JoinOn, QueryBuilder


class QueryTablesTests(unittest.TestCase):
    table_a, table_b, table_c, table_d = Tables("a", "b", "c", "d")

    def test_replace_table(self):
        query = Query.from_(self.table_a).select(self.table_a.time)
        query = query.replace_table(self.table_a, self.table_b)

        self.assertEqual('SELECT "time" FROM "b"', str(query))

    def test_replace_only_specified_table(self):
        query = Query.from_(self.table_a).select(self.table_a.time)
        query = query.replace_table(self.table_b, self.table_c)

        self.assertEqual('SELECT "time" FROM "a"', str(query))

    def test_replace_insert_table(self):
        query = Query.into(self.table_a).insert(1)
        query = query.replace_table(self.table_a, self.table_b)

        self.assertEqual('INSERT INTO "b" VALUES (1)', str(query))

    def test_replace_insert_table_current_table_not_match(self):
        query = Query.into(self.table_a).insert(1)
        query = query.replace_table(self.table_c, self.table_b)

        self.assertEqual('INSERT INTO "a" VALUES (1)', str(query))

    def test_replace_update_table(self):
        query = Query.update(self.table_a).set("foo", "bar")
        query = query.replace_table(self.table_a, self.table_b)

        self.assertEqual('UPDATE "b" SET "foo"=\'bar\'', str(query))

    def test_replace_update_table_current_table_not_match(self):
        query = Query.update(self.table_a).set("foo", "bar")
        query = query.replace_table(self.table_c, self.table_b)

        self.assertEqual('UPDATE "a" SET "foo"=\'bar\'', str(query))

    def test_replace_delete_table(self):
        query = Query.from_(self.table_a).delete()
        query = query.replace_table(self.table_a, self.table_b)

        self.assertEqual('DELETE FROM "b"', str(query))

    def test_replace_join_tables(self):
        query = (
            Query.from_(self.table_a)
            .join(self.table_b)
            .on(self.table_a.customer_id == self.table_b.id)
            .join(self.table_c)
            .on(self.table_b.seller_id == self.table_c.id)
            .select(self.table_a.star)
        )
        query = query.replace_table(self.table_a, self.table_d)

        self.assertEqual(
            'SELECT "d".* '
            'FROM "d" '
            'JOIN "b" ON "d"."customer_id"="b"."id" '
            'JOIN "c" ON "b"."seller_id"="c"."id"',
            str(query),
        )

    def test_replace_filter_tables(self):
        query = Query.from_(self.table_a).select(self.table_a.name).where(self.table_a.name == "Mustermann")
        query = query.replace_table(self.table_a, self.table_b)

        self.assertEqual('SELECT "name" FROM "b" WHERE "name"=\'Mustermann\'', str(query))

    def test_replace_having_table(self):
        query = (
            Query.from_(self.table_a)
            .select(functions.Sum(self.table_a.revenue))
            .groupby(self.table_a.customer)
            .having(functions.Sum(self.table_a.revenue) >= 1000)
        )
        query = query.replace_table(self.table_a, self.table_b)

        self.assertEqual(
            'SELECT SUM("revenue") ' 'FROM "b" ' 'GROUP BY "customer" ' 'HAVING SUM("revenue")>=1000',
            str(query),
        )

    def test_replace_case_table(self):
        query = Query.from_(self.table_a).select(
            Case()
            .when(self.table_a.fname == "Tom", "It was Tom")
            .when(self.table_a.fname == "John", "It was John")
            .else_("It was someone else.")
            .as_("who_was_it")
        )
        query = query.replace_table(self.table_a, self.table_b)

        self.assertEqual(
            "SELECT CASE "
            "WHEN \"fname\"='Tom' THEN 'It was Tom' "
            "WHEN \"fname\"='John' THEN 'It was John' "
            "ELSE 'It was someone else.' END \"who_was_it\" "
            'FROM "b"',
            str(query),
        )

    def test_replace_orderby_table(self):
        query = Query.from_(self.table_a).select(self.table_a.customer).orderby(self.table_a.customer)
        query = query.replace_table(self.table_a, self.table_b)

        self.assertEqual('SELECT "customer" FROM "b" ORDER BY "customer"', str(query))

    def test_replace_tuple_table(self):
        query = (
            Query.from_(self.table_a)
            .select(self.table_a.cost, self.table_a.revenue)
            .where((self.table_a.cost, self.table_a.revenue) == Tuple(1, 2))
        )

        query = query.replace_table(self.table_a, self.table_b)

        # Order is reversed due to lack of right equals method
        self.assertEqual(
            'SELECT "cost","revenue" FROM "b" WHERE (1,2)=("cost","revenue")',
            str(query),
        )

    def test_is_joined(self):
        q = Query.from_(self.table_a).join(self.table_b).on(self.table_a.foo == self.table_b.boo)

        self.assertTrue(q.is_joined(self.table_b))
        self.assertFalse(q.is_joined(self.table_c))


class ScopedTableReplacementTests(unittest.TestCase):
    """Self-joins of one physical table, plus a nested query that reuses an outer alias.

    The order warehouse (A) and delivery warehouse (B) are two bindings of ``warehouse`` with different
    addresses and validity.  The nested query looks up warehouse settings but is scoped to B only.  Its
    display alias intentionally equals the ordering warehouse's alias to prove inner references are not
    captured by the outer binding.
    """

    def setUp(self):
        self.fact = Table("orders")
        self.order_warehouse = Table("warehouse", alias="order_wh")
        self.delivery_warehouse = Table("warehouse", alias="delivery_wh")
        self.settings = Table("warehouse", alias="order_wh")
        self.lookup = (
            Query.from_(self.settings)
            .select(self.settings.config)
            .where(self.settings.id == self.delivery_warehouse.id)
            .where(self.settings.enabled == 1)
        )
        self.query = (
            Query.from_(self.fact)
            .join(self.order_warehouse)
            .on(self.fact.order_wh_id == self.order_warehouse.id)
            .join(self.delivery_warehouse)
            .on(self.fact.delivery_wh_id == self.delivery_warehouse.id)
            .select(
                self.order_warehouse.address.as_("order_address"),
                self.delivery_warehouse.address.as_("delivery_address"),
                self.lookup.as_("delivery_config"),
            )
        )

    def execute(self, query):
        with sqlite3.connect(":memory:") as connection:
            connection.executescript(
                "CREATE TABLE orders (id INTEGER, order_wh_id INTEGER, delivery_wh_id INTEGER);"
                "CREATE TABLE warehouse (id INTEGER, address TEXT, enabled INTEGER, config TEXT);"
                "INSERT INTO orders VALUES (1, 10, 20), (2, 10, 30);"
                "INSERT INTO warehouse VALUES "
                "(10, 'A-address', 1, 'A-config'),"
                "(20, 'B-address', 0, 'B-config'),"
                "(30, 'C-address', 1, 'C-config');"
            )
            return connection.execute(str(query)).fetchall()

    def test_original_query_uses_distinct_warehouse_addresses(self):
        sql = str(self.query)

        self.assertIn('"order_wh"."address" "order_address"', sql)
        self.assertIn('"delivery_wh"."address" "delivery_address"', sql)
        self.assertEqual(
            self.execute(self.query),
            [('A-address', 'B-address', None), ('A-address', 'C-address', 'C-config')],
        )

    def test_copied_delivery_binding_rebinds_its_projection_join_and_correlation(self):
        copy = self.delivery_warehouse.as_("delivery_wh_copy")
        copied = self.query.replace_table(self.delivery_warehouse, copy)
        sql = str(copied)

        self.assertIn('"delivery_wh_copy"."address" "delivery_address"', sql)
        self.assertIn('JOIN "warehouse" "delivery_wh_copy"', sql)
        self.assertIn('"order_wh"."id"="delivery_wh_copy"."id"', sql)
        # The ordering warehouse binding is a different binding and must be untouched.
        self.assertIn('JOIN "warehouse" "order_wh"', sql)
        self.assertNotIn('"delivery_wh"."', sql)
        self.assertEqual(self.execute(copied), self.execute(self.query))

    def test_render_order_does_not_change_aliases_or_criteria(self):
        copy = self.delivery_warehouse.as_("delivery_wh_copy")
        original_sql = (
            'SELECT "order_wh"."address" "order_address",'
            '"delivery_wh"."address" "delivery_address",'
            '(SELECT "order_wh"."config" FROM "warehouse" "order_wh" '
            'WHERE "order_wh"."id"="delivery_wh"."id" AND "order_wh"."enabled"=1) "delivery_config" '
            'FROM "orders" JOIN "warehouse" "order_wh" ON "orders"."order_wh_id"="order_wh"."id" '
            'JOIN "warehouse" "delivery_wh" ON "orders"."delivery_wh_id"="delivery_wh"."id"'
        )
        self.assertEqual(str(self.query), original_sql)

        copied = self.query.replace_table(self.delivery_warehouse, copy)
        copied_sql = (
            'SELECT "order_wh"."address" "order_address",'
            '"delivery_wh_copy"."address" "delivery_address",'
            '(SELECT "order_wh"."config" FROM "warehouse" "order_wh" '
            'WHERE "order_wh"."id"="delivery_wh_copy"."id" AND "order_wh"."enabled"=1) "delivery_config" '
            'FROM "orders" JOIN "warehouse" "order_wh" ON "orders"."order_wh_id"="order_wh"."id" '
            'JOIN "warehouse" "delivery_wh_copy" ON "orders"."delivery_wh_id"="delivery_wh_copy"."id"'
        )
        self.assertEqual(str(copied), copied_sql)
        self.assertEqual(self.execute(copied), self.execute(self.query))
        # Rendering the copy first must not rewrite the original's aliases or criteria.
        self.assertEqual(str(self.query), original_sql)

    def test_renaming_the_inner_alias_keeps_outer_bindings(self):
        renamed_settings = self.settings.as_("replenish_wh")
        renamed = self.query.replace_table(self.settings, renamed_settings)
        sql = str(renamed)

        self.assertIn('FROM "warehouse" "replenish_wh"', sql)
        self.assertIn('"replenish_wh"."id"="delivery_wh"."id"', sql)
        # Outer ordering warehouse and delivery warehouse keep their own aliases.
        self.assertIn('"order_wh"."address" "order_address"', sql)
        self.assertIn('JOIN "warehouse" "order_wh"', sql)
        self.assertIn('JOIN "warehouse" "delivery_wh"', sql)
        self.assertNotIn('"replenish_wh"."address"', sql)
        self.assertEqual(self.execute(renamed), self.execute(self.query))

    def test_renaming_the_outer_alias_leaves_inner_shadow_alias_alone(self):
        renamed = self.query.replace_table(self.order_warehouse, self.order_warehouse.as_("buy_wh"))
        sql = str(renamed)

        self.assertIn('"buy_wh"."address" "order_address"', sql)
        self.assertIn('JOIN "warehouse" "buy_wh"', sql)
        self.assertIn('FROM "warehouse" "order_wh"', sql)
        self.assertIn('"order_wh"."id"="delivery_wh"."id"', sql)
        self.assertEqual(self.execute(renamed), self.execute(self.query))

    def test_duplicate_alias_in_one_scope_is_reported_before_generating_sql(self):
        first = Table("warehouse", alias="wh")
        second = Table("warehouse", alias="wh")

        with self.assertRaises(AliasException) as context:
            (
                Query.from_(self.fact)
                .join(first)
                .on(self.fact.order_wh_id == first.id)
                .join(second)
                .on(self.fact.delivery_wh_id == second.id)
                .select(first.address, second.address)
            )
        self.assertIn('Duplicate table alias "wh"', str(context.exception))
        # It is also a join/query error so existing handlers keep working.
        self.assertIsInstance(context.exception, JoinException)
        self.assertIsInstance(context.exception, QueryException)

    def test_duplicate_alias_is_also_reported_when_only_rendering(self):
        first = Table("warehouse", alias="wh")
        second = Table("warehouse", alias="wh")
        query = Query.from_(self.fact).select(first.address)
        query._joins = [JoinOn(first, JoinType.inner, self.fact.order_wh_id == first.id)]
        query._joins.append(JoinOn(second, JoinType.inner, self.fact.delivery_wh_id == second.id))

        with self.assertRaises(AliasException):
            str(query)

    def test_duplicate_alias_is_detected_per_scope_not_globally(self):
        outer = Table("warehouse", alias="wh")
        inner_one, inner_two = Table("warehouse", alias="wh"), Table("warehouse", alias="wh")
        first = Query.from_(inner_one).select(inner_one.id).where(inner_one.region == outer.region)
        second = Query.from_(inner_two).select(inner_two.id).where(inner_two.region == outer.region)

        query = Query.from_(outer).select(outer.address, first.as_("a"), second.as_("b"))

        self.assertEqual(
            str(query),
            'SELECT "wh"."address",'
            '(SELECT "wh"."id" FROM "warehouse" "wh" WHERE "wh"."region"="wh"."region") "a",'
            '(SELECT "wh"."id" FROM "warehouse" "wh" WHERE "wh"."region"="wh"."region") "b" '
            'FROM "warehouse" "wh"',
        )


class QueryBuilderTests(unittest.TestCase):
    def test_query_builders_have_reference_to_correct_query_class(self):
        with self.subTest('QueryBuilder'):
            self.assertEqual(Query, QueryBuilder.QUERY_CLS)

        with self.subTest('DropQueryBuilder'):
            self.assertEqual(Query, DropQueryBuilder.QUERY_CLS)

        with self.subTest('CreateQueryBuilder'):
            self.assertEqual(Query, CreateQueryBuilder.QUERY_CLS)

        with self.subTest('MySQLQueryBuilder'):
            self.assertEqual(MySQLQuery, MySQLQueryBuilder.QUERY_CLS)

        with self.subTest('MySQLLoadQueryBuilder'):
            self.assertEqual(MySQLQuery, MySQLLoadQueryBuilder.QUERY_CLS)

        with self.subTest('VerticaQueryBuilder'):
            self.assertEqual(VerticaQuery, VerticaQueryBuilder.QUERY_CLS)

        with self.subTest('VerticaCreateQueryBuilder'):
            self.assertEqual(VerticaQuery, VerticaCreateQueryBuilder.QUERY_CLS)

        with self.subTest('VerticaCopyQueryBuilder'):
            self.assertEqual(VerticaQuery, VerticaCopyQueryBuilder.QUERY_CLS)

        with self.subTest('PostgreSQLQueryBuilder'):
            self.assertEqual(PostgreSQLQuery, PostgreSQLQueryBuilder.QUERY_CLS)

        with self.subTest('MSSQLQueryBuilder'):
            self.assertEqual(MSSQLQuery, MSSQLQueryBuilder.QUERY_CLS)

        with self.subTest('SnowflakeQueryBuilder'):
            self.assertEqual(SnowflakeQuery, SnowflakeQueryBuilder.QUERY_CLS)

        with self.subTest('ClickHouseQueryBuilder'):
            self.assertEqual(ClickHouseQuery, ClickHouseQueryBuilder.QUERY_CLS)

        with self.subTest('RedShiftQueryBuilder'):
            self.assertEqual(RedshiftQuery, RedShiftQueryBuilder.QUERY_CLS)

        with self.subTest('SQLLiteQueryBuilder'):
            self.assertEqual(SQLLiteQuery, SQLLiteQueryBuilder.QUERY_CLS)

        with self.subTest('OracleQueryBuilder'):
            self.assertEqual(OracleQuery, OracleQueryBuilder.QUERY_CLS)

    def test_pipe(self) -> None:
        base_query = Query.from_("test")

        def select(query: QueryBuilder) -> QueryBuilder:
            return query.select("test1", "test2")

        def count_group(query: QueryBuilder, *groups) -> QueryBuilder:
            return query.groupby(*groups).select(*groups, functions.Count("*"))

        for func, args, kwargs, expected_str in [
            (select, [], {}, 'SELECT "test1","test2" FROM "test"'),
            (
                count_group,
                ["test1", "test2"],
                {},
                'SELECT "test1","test2",COUNT(*) FROM "test" GROUP BY "test1","test2"',
            ),
            (count_group, ["test1"], {}, 'SELECT "test1",COUNT(*) FROM "test" GROUP BY "test1"'),
        ]:
            result_str = str(base_query.pipe(func, *args, **kwargs))
            self.assertEqual(result_str, str(func(base_query, *args, **kwargs)))
            self.assertEqual(result_str, expected_str)

        def where_clause(query: QueryBuilder, num_days: int) -> QueryBuilder:
            return query.where(Field("date") > functions.Now() - num_days)

        result_str = str(base_query.pipe(select).pipe(where_clause, num_days=1))
        self.assertEqual(
            result_str,
            str(select(where_clause(base_query, num_days=1))),
        )
        self.assertEqual(
            result_str,
            'SELECT "test1","test2" FROM "test" WHERE "date">NOW()-1',
        )
