import sqlite3
import unittest

from pypika import Field, NamedParameter, Order, QmarkParameter, Query, Table
from pypika.dialects import MSSQLQuery, SQLLiteQuery
from pypika.enums import Dialects
from pypika.queries import QueryBuilder


GROSS_ALIAS = 'total "gross"'
TENANT_ID = 7


def shared_tenant_filter():
    # Deliberately unqualified so the same criterion can be rendered in the
    # child query (against the table) and in the parent (against the subquery).
    return Field("tenant_id") == TENANT_ID


def build_report(query_cls):
    invoices = Table("invoices")
    tenant_filter = shared_tenant_filter()

    child = (
        query_cls.from_(invoices)
        .select(invoices.id, invoices.tenant_id, invoices.amount.as_(GROSS_ALIAS))
        .where(tenant_filter)
        .orderby(invoices.id)
        .limit(3)
        .offset(1)
    )

    gross = Field(GROSS_ALIAS, table=child)
    parent = (
        query_cls.from_(child)
        .select(child.id, gross, child.tenant_id)
        .where(tenant_filter)
        .orderby(gross, order=Order.desc)
        .limit(2)
        .offset(1)
    )
    return child, parent


def create_invoices(connection):
    connection.execute("CREATE TABLE invoices (id INTEGER, tenant_id INTEGER, amount INTEGER)")
    connection.executemany(
        "INSERT INTO invoices VALUES (?,?,?)",
        [(row_id, TENANT_ID if row_id % 2 else 8, row_id * 10) for row_id in range(1, 13)],
    )


class ReportTemplateTests(unittest.TestCase):
    def test_sqlite_mssql_sqlite_sequence_keeps_each_dialect_pagination(self):
        sqlite_child_1, sqlite_parent_1 = build_report(SQLLiteQuery)
        mssql_child, mssql_parent = build_report(MSSQLQuery)
        sqlite_child_2, sqlite_parent_2 = build_report(SQLLiteQuery)

        self.assertEqual(sqlite_child_1.get_sql(), sqlite_child_2.get_sql())
        self.assertEqual(sqlite_parent_1.get_sql(), sqlite_parent_2.get_sql())

        self.assertIn('ORDER BY "id" LIMIT 3 OFFSET 1', sqlite_child_1.get_sql())
        self.assertIn('ORDER BY "sq0"."total ""gross""" DESC LIMIT 2 OFFSET 1', sqlite_parent_1.get_sql())

        self.assertIn('ORDER BY "id" OFFSET 1 ROWS FETCH NEXT 3 ROWS ONLY', mssql_child.get_sql())
        self.assertIn(
            'ORDER BY "sq0"."total ""gross""" DESC OFFSET 1 ROWS FETCH NEXT 2 ROWS ONLY',
            mssql_parent.get_sql(),
        )

    def test_child_and_parent_keep_their_own_pagination_values(self):
        child, parent = build_report(SQLLiteQuery)

        child_sql = child.get_sql()
        parent_sql = parent.get_sql()

        self.assertIn("LIMIT 3 OFFSET 1", child_sql)
        self.assertIn("LIMIT 2 OFFSET 1", parent_sql)
        self.assertNotIn("LIMIT 2 OFFSET 1", child_sql)

    def test_mssql_subquery_reused_in_sqlite_report_renders_sqlite_pagination(self):
        native_child, native_parent = build_report(SQLLiteQuery)
        mssql_child, mssql_parent = build_report(MSSQLQuery)

        gross = Field(GROSS_ALIAS, table=mssql_child)
        reused_parent = (
            SQLLiteQuery.from_(mssql_child)
            .select(mssql_child.id, gross, mssql_child.tenant_id)
            .where(shared_tenant_filter())
            .orderby(gross, order=Order.desc)
            .limit(2)
            .offset(1)
        )

        self.assertIn("FETCH NEXT", mssql_child.get_sql())
        self.assertIn(native_child.get_sql(), reused_parent.get_sql())
        self.assertEqual(native_parent.get_sql(), reused_parent.get_sql())
        self.assertNotIn("FETCH NEXT", reused_parent.get_sql())
        self.assertIn('ORDER BY "id" LIMIT 3 OFFSET 1', reused_parent.get_sql())

        # Rendering the reused subquery in a SQLite report must not change how
        # the original MSSQL queries render on their own.
        self.assertIn("OFFSET 1 ROWS FETCH NEXT 3 ROWS ONLY", mssql_child.get_sql())
        self.assertIn("OFFSET 1 ROWS FETCH NEXT 2 ROWS ONLY", mssql_parent.get_sql())

    def test_explicit_quote_char_only_affects_that_rendering(self):
        _, sqlite_parent = build_report(SQLLiteQuery)
        _, mssql_parent = build_report(MSSQLQuery)

        default_sqlite_sql = sqlite_parent.get_sql()
        explicit_sqlite_sql = sqlite_parent.get_sql(quote_char="`")
        default_mssql_sql = mssql_parent.get_sql()
        explicit_mssql_sql = mssql_parent.get_sql(quote_char="`")

        self.assertIn('"sq0"."total ""gross"""', default_sqlite_sql)
        self.assertIn("`sq0`.`total \"gross\"`", explicit_sqlite_sql)
        self.assertIn("`amount` `total \"gross\"`", explicit_sqlite_sql)
        self.assertNotIn("`", default_sqlite_sql)
        self.assertEqual(default_sqlite_sql, sqlite_parent.get_sql())

        self.assertIn('ORDER BY "sq0"."total ""gross""" DESC OFFSET 1 ROWS FETCH NEXT 2 ROWS ONLY', default_mssql_sql)
        self.assertIn("ORDER BY `sq0`.`total \"gross\"` DESC OFFSET 1 ROWS FETCH NEXT 2 ROWS ONLY", explicit_mssql_sql)
        self.assertEqual(default_mssql_sql, mssql_parent.get_sql())

    def test_both_sequential_sqlite_versions_execute_identically(self):
        qmark = QmarkParameter()
        sqlite_child_1, sqlite_parent_1 = build_report(SQLLiteQuery)
        sqlite_sql_1 = sqlite_parent_1.get_sql(parameter=qmark)
        sqlite_params_1 = list(qmark.get_parameters())

        mssql_child, mssql_parent = build_report(MSSQLQuery)
        mssql_sql = mssql_parent.get_sql(parameter=QmarkParameter())
        self.assertIn("FETCH NEXT", mssql_sql)

        qmark_2 = QmarkParameter()
        sqlite_child_2, sqlite_parent_2 = build_report(SQLLiteQuery)
        sqlite_sql_2 = sqlite_parent_2.get_sql(parameter=qmark_2)
        sqlite_params_2 = list(qmark_2.get_parameters())

        self.assertEqual(sqlite_sql_1, sqlite_sql_2)
        self.assertEqual(sqlite_params_1, sqlite_params_2)
        self.assertEqual([TENANT_ID, TENANT_ID], sqlite_params_1)

        with sqlite3.connect(":memory:") as connection:
            create_invoices(connection)
            cursor_1 = connection.execute(sqlite_sql_1, sqlite_params_1)
            headers_1, rows_1 = [column[0] for column in cursor_1.description], cursor_1.fetchall()
            cursor_2 = connection.execute(sqlite_sql_2, sqlite_params_2)
            headers_2, rows_2 = [column[0] for column in cursor_2.description], cursor_2.fetchall()

        self.assertEqual(headers_1, headers_2)
        self.assertEqual(rows_1, rows_2)
        self.assertEqual(['id', GROSS_ALIAS, 'tenant_id'], headers_1)
        self.assertEqual([(5, 50, TENANT_ID), (3, 30, TENANT_ID)], rows_1)

    def test_mssql_subquery_reused_in_sqlite_report_executes_like_native_sqlite(self):
        native_child, native_parent = build_report(SQLLiteQuery)
        mssql_child, _ = build_report(MSSQLQuery)

        gross = Field(GROSS_ALIAS, table=mssql_child)
        reused_parent = (
            SQLLiteQuery.from_(mssql_child)
            .select(mssql_child.id, gross, mssql_child.tenant_id)
            .where(shared_tenant_filter())
            .orderby(gross, order=Order.desc)
            .limit(2)
            .offset(1)
        )

        native_param = QmarkParameter()
        native_sql = native_parent.get_sql(parameter=native_param)
        reused_param = QmarkParameter()
        reused_sql = reused_parent.get_sql(parameter=reused_param)

        self.assertEqual(native_sql, reused_sql)
        self.assertEqual(native_param.get_parameters(), reused_param.get_parameters())

        with sqlite3.connect(":memory:") as connection:
            create_invoices(connection)
            native_cursor = connection.execute(native_sql, native_param.get_parameters())
            native_headers, native_rows = [c[0] for c in native_cursor.description], native_cursor.fetchall()
            reused_cursor = connection.execute(reused_sql, reused_param.get_parameters())
            reused_headers, reused_rows = [c[0] for c in reused_cursor.description], reused_cursor.fetchall()

        self.assertEqual(native_headers, reused_headers)
        self.assertEqual(native_rows, reused_rows)

    def test_parameter_style_is_fixed_per_call(self):
        _, sqlite_parent = build_report(SQLLiteQuery)

        qmark = QmarkParameter()
        qmark_sql = sqlite_parent.get_sql(parameter=qmark)
        self.assertEqual([TENANT_ID, TENANT_ID], qmark.get_parameters())

        named = NamedParameter()
        named_sql = sqlite_parent.get_sql(parameter=named)
        self.assertEqual({'param1': TENANT_ID, 'param2': TENANT_ID}, named.get_parameters())

        self.assertIn("WHERE \"tenant_id\"=?", qmark_sql)
        self.assertIn("WHERE \"tenant_id\"=:param1", named_sql)
        self.assertIn("WHERE \"tenant_id\"=:param2", named_sql)

        # Each call starts a fresh parameter collection; no values leak between calls.
        qmark_again = QmarkParameter()
        self.assertEqual(qmark_sql, sqlite_parent.get_sql(parameter=qmark_again))
        self.assertEqual([TENANT_ID, TENANT_ID], qmark_again.get_parameters())

    def test_parameter_object_is_not_retained_by_the_builder(self):
        _, sqlite_parent = build_report(SQLLiteQuery)
        literal_sql = sqlite_parent.get_sql()

        qmark = QmarkParameter()
        parameterized_sql = sqlite_parent.get_sql(parameter=qmark)

        self.assertEqual([TENANT_ID, TENANT_ID], qmark.get_parameters())
        self.assertNotEqual(literal_sql, parameterized_sql)
        self.assertEqual(literal_sql, sqlite_parent.get_sql())
        self.assertIn('WHERE "tenant_id"=7', sqlite_parent.get_sql())

    def test_rendering_one_dialect_does_not_mutate_the_template(self):
        sqlite_child, sqlite_parent = build_report(SQLLiteQuery)
        before = (
            sqlite_child.get_sql(),
            sqlite_parent.get_sql(),
            sqlite_child._limit,
            sqlite_child._offset,
            sqlite_parent._limit,
            sqlite_parent._offset,
            sqlite_child.dialect,
            sqlite_parent.dialect,
        )

        sqlite_parent.get_sql(dialect=Dialects.MSSQL)
        mssql_parent = build_report(MSSQLQuery)[1]
        mssql_parent.get_sql(dialect=Dialects.SQLLITE)

        after = (
            sqlite_child.get_sql(),
            sqlite_parent.get_sql(),
            sqlite_child._limit,
            sqlite_child._offset,
            sqlite_parent._limit,
            sqlite_parent._offset,
            sqlite_child.dialect,
            sqlite_parent.dialect,
        )
        self.assertEqual(before, after)

    def test_alternating_dialect_renders_do_not_leak_pagination_syntax(self):
        sqlite_child, sqlite_parent = build_report(SQLLiteQuery)
        mssql_child, mssql_parent = build_report(MSSQLQuery)

        sqlite_first = sqlite_parent.get_sql()
        mssql_first = mssql_parent.get_sql()
        sqlite_second = sqlite_parent.get_sql()
        mssql_second = mssql_parent.get_sql()

        self.assertEqual(sqlite_first, sqlite_second)
        self.assertEqual(mssql_first, mssql_second)
        self.assertEqual(sqlite_child.get_sql().count("LIMIT"), 1)
        self.assertEqual(mssql_child.get_sql().count("FETCH NEXT"), 1)
        self.assertNotIn("FETCH NEXT", sqlite_parent.get_sql())
        self.assertNotIn("LIMIT", mssql_parent.get_sql())

    def test_child_pagination_stays_inside_and_parent_pagination_outside(self):
        _, mssql_parent = build_report(MSSQLQuery)
        sql = mssql_parent.get_sql()

        subquery_start = sql.index("FROM (")
        subquery_end = sql.index(') "sq0"')
        subquery_sql = sql[subquery_start:subquery_end]

        self.assertIn("OFFSET 1 ROWS FETCH NEXT 3 ROWS ONLY", subquery_sql)
        self.assertIn("OFFSET 1 ROWS FETCH NEXT 2 ROWS ONLY", sql[subquery_end:])

    def test_single_base_template_rendered_with_explicit_dialects(self):
        _, base_parent = build_report(Query)
        sqlite_sql = base_parent.get_sql(dialect=Dialects.SQLLITE)
        mssql_sql = base_parent.get_sql(dialect=Dialects.MSSQL)
        sqlite_again_sql = base_parent.get_sql(dialect=Dialects.SQLLITE)

        self.assertEqual(sqlite_sql, sqlite_again_sql)
        self.assertIn('ORDER BY "id" LIMIT 3 OFFSET 1', sqlite_sql)
        self.assertIn('ORDER BY "sq0"."total ""gross""" DESC LIMIT 2 OFFSET 1', sqlite_sql)
        self.assertNotIn("FETCH NEXT", sqlite_sql)

        self.assertIn('ORDER BY "id" OFFSET 1 ROWS FETCH NEXT 3 ROWS ONLY', mssql_sql)
        self.assertIn('ORDER BY "sq0"."total ""gross""" DESC OFFSET 1 ROWS FETCH NEXT 2 ROWS ONLY', mssql_sql)
        self.assertNotIn("LIMIT", mssql_sql)

        self.assertIn('ORDER BY "id" LIMIT 3 OFFSET 1', base_parent.get_sql())


class LegacyApiTests(unittest.TestCase):
    def test_plain_query_builder_keeps_limit_offset(self):
        invoices = Table("invoices")
        query = QueryBuilder().from_(invoices).select(invoices.id).orderby(invoices.id).limit(2).offset(1)

        self.assertEqual('SELECT "id" FROM "invoices" ORDER BY "id" LIMIT 2 OFFSET 1', query.get_sql())

    def test_plain_query_keeps_limit_offset(self):
        invoices = Table("invoices")
        query = Query.from_(invoices).select(invoices.id).orderby(invoices.id).limit(2).offset(1)

        self.assertEqual('SELECT "id" FROM "invoices" ORDER BY "id" LIMIT 2 OFFSET 1', str(query))

    def test_direct_dialect_queries_keep_their_own_pagination(self):
        invoices = Table("invoices")
        sqlite_query = SQLLiteQuery.from_(invoices).select(invoices.id).orderby(invoices.id).limit(2).offset(1)
        mssql_query = MSSQLQuery.from_(invoices).select(invoices.id).orderby(invoices.id).limit(2).offset(1)

        self.assertEqual('SELECT "id" FROM "invoices" ORDER BY "id" LIMIT 2 OFFSET 1', sqlite_query.get_sql())
        self.assertEqual(
            'SELECT "id" FROM "invoices" ORDER BY "id" OFFSET 1 ROWS FETCH NEXT 2 ROWS ONLY',
            mssql_query.get_sql(),
        )

    def test_custom_builder_pagination_overrides_still_apply(self):
        class CustomPaginationBuilder(QueryBuilder):
            def _limit_sql(self):
                return " TOPX {}".format(self._limit)

            def _offset_sql(self):
                return " SKIPX {}".format(self._offset)

        invoices = Table("invoices")
        query = CustomPaginationBuilder().from_(invoices).select(invoices.id).limit(2).offset(1)

        self.assertEqual('SELECT "id" FROM "invoices" TOPX 2 SKIPX 1', query.get_sql())
