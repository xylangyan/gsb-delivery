import sqlite3
import unittest
from datetime import date

from pypika import (
    AliasedQuery,
    Field,
    FormatParameter,
    NamedParameter,
    NumericParameter,
    Parameter,
    PyformatParameter,
    QmarkParameter,
    Query,
    Table,
    Tables,
)
from pypika.dialects import ClickHouseQuery, MySQLQuery
from pypika.terms import ListParameter, ParameterValueWrapper, ValueWrapper


class ParametrizedTests(unittest.TestCase):
    table_abc, table_efg = Tables("abc", "efg")

    def test_param_insert(self):
        q = Query.into(self.table_abc).columns("a", "b", "c").insert(Parameter("?"), Parameter("?"), Parameter("?"))

        self.assertEqual('INSERT INTO "abc" ("a","b","c") VALUES (?,?,?)', q.get_sql())

    def test_param_select_join(self):
        q = (
            Query.from_(self.table_abc)
            .select("*")
            .where(self.table_abc.category == Parameter("%s"))
            .join(self.table_efg)
            .on(self.table_abc.id == self.table_efg.abc_id)
            .where(self.table_efg.date >= Parameter("%s"))
            .limit(10)
        )

        self.assertEqual(
            'SELECT * FROM "abc" JOIN "efg" ON "abc"."id"="efg"."abc_id" WHERE "abc"."category"=%s AND "efg"."date">=%s LIMIT 10',
            q.get_sql(),
        )

    def test_param_select_subquery(self):
        q = (
            Query.from_(self.table_abc)
            .select("*")
            .where(self.table_abc.category == Parameter("&1"))
            .where(
                self.table_abc.id.isin(
                    Query.from_(self.table_efg)
                    .select(self.table_efg.abc_id)
                    .where(self.table_efg.date >= Parameter("&2"))
                )
            )
            .limit(10)
        )

        self.assertEqual(
            'SELECT * FROM "abc" WHERE "category"=&1 AND "id" IN (SELECT "abc_id" FROM "efg" WHERE "date">=&2) LIMIT 10',
            q.get_sql(),
        )

    def test_join(self):
        subquery = (
            Query.from_(self.table_efg)
            .select(self.table_efg.fiz, self.table_efg.buz)
            .where(self.table_efg.buz == Parameter(":buz"))
        )

        q = (
            Query.from_(self.table_abc)
            .join(subquery)
            .on(self.table_abc.bar == subquery.buz)
            .select(self.table_abc.foo, subquery.fiz)
            .where(self.table_abc.bar == Parameter(":bar"))
        )

        self.assertEqual(
            'SELECT "abc"."foo","sq0"."fiz" FROM "abc" JOIN (SELECT "fiz","buz" FROM "efg" WHERE "buz"=:buz)'
            ' "sq0" ON "abc"."bar"="sq0"."buz" WHERE "abc"."bar"=:bar',
            q.get_sql(),
        )

    def test_qmark_parameter(self):
        self.assertEqual('?', QmarkParameter().get_sql())

    def test_numeric_parameter(self):
        self.assertEqual(':14', NumericParameter('14').get_sql())
        self.assertEqual(':15', NumericParameter(15).get_sql())

    def test_named_parameter(self):
        self.assertEqual(':buz', NamedParameter('buz').get_sql())

    def test_format_parameter(self):
        self.assertEqual('%s', FormatParameter().get_sql())

    def test_pyformat_parameter(self):
        self.assertEqual('%(buz)s', PyformatParameter('buz').get_sql())


class ParametrizedTestsWithValues(unittest.TestCase):
    table_abc, table_efg = Tables("abc", "efg")

    def test_param_insert(self):
        q = Query.into(self.table_abc).columns("a", "b", "c").insert(1, 2.2, 'foo')

        parameter = QmarkParameter()
        sql = q.get_sql(parameter=parameter)
        self.assertEqual('INSERT INTO "abc" ("a","b","c") VALUES (?,?,?)', sql)
        self.assertEqual([1, 2.2, 'foo'], parameter.get_parameters())

    def test_param_select_join(self):
        q = (
            Query.from_(self.table_abc)
            .select("*")
            .where(self.table_abc.category == 'foobar')
            .join(self.table_efg)
            .on(self.table_abc.id == self.table_efg.abc_id)
            .where(self.table_efg.date >= date(2024, 2, 22))
            .limit(10)
        )

        parameter = FormatParameter()
        sql = q.get_sql(parameter=parameter)
        self.assertEqual(
            'SELECT * FROM "abc" JOIN "efg" ON "abc"."id"="efg"."abc_id" WHERE "abc"."category"=%s AND "efg"."date">=%s LIMIT 10',
            sql,
        )
        self.assertEqual(['foobar', '2024-02-22'], parameter.get_parameters())

    def test_param_select_subquery(self):
        q = (
            Query.from_(self.table_abc)
            .select("*")
            .where(self.table_abc.category == 'foobar')
            .where(
                self.table_abc.id.isin(
                    Query.from_(self.table_efg)
                    .select(self.table_efg.abc_id)
                    .where(self.table_efg.date >= date(2024, 2, 22))
                )
            )
            .limit(10)
        )

        parameter = ListParameter(placeholder=lambda idx: f'&{idx+1}')
        sql = q.get_sql(parameter=parameter)
        self.assertEqual(
            'SELECT * FROM "abc" WHERE "category"=&1 AND "id" IN (SELECT "abc_id" FROM "efg" WHERE "date">=&2) LIMIT 10',
            sql,
        )
        self.assertEqual(['foobar', '2024-02-22'], parameter.get_parameters())

    def test_join(self):
        subquery = (
            Query.from_(self.table_efg)
            .select(self.table_efg.fiz, self.table_efg.buz)
            .where(self.table_efg.buz == 'buz')
        )

        q = (
            Query.from_(self.table_abc)
            .join(subquery)
            .on(self.table_abc.bar == subquery.buz)
            .select(self.table_abc.foo, subquery.fiz)
            .where(self.table_abc.bar == 'bar')
        )

        parameter = NamedParameter()
        sql = q.get_sql(parameter=parameter)
        self.assertEqual(
            'SELECT "abc"."foo","sq0"."fiz" FROM "abc" JOIN (SELECT "fiz","buz" FROM "efg" WHERE "buz"=:param1)'
            ' "sq0" ON "abc"."bar"="sq0"."buz" WHERE "abc"."bar"=:param2',
            sql,
        )
        self.assertEqual({'param1': 'buz', 'param2': 'bar'}, parameter.get_parameters())

    def test_join_with_parameter_value_wrapper(self):
        subquery = (
            Query.from_(self.table_efg)
            .select(self.table_efg.fiz, self.table_efg.buz)
            .where(self.table_efg.buz == ParameterValueWrapper(Parameter(':buz'), 'buz'))
        )

        q = (
            Query.from_(self.table_abc)
            .join(subquery)
            .on(self.table_abc.bar == subquery.buz)
            .select(self.table_abc.foo, subquery.fiz)
            .where(self.table_abc.bar == ParameterValueWrapper(NamedParameter('bar'), 'bar'))
        )

        parameter = NamedParameter()
        sql = q.get_sql(parameter=parameter)
        self.assertEqual(
            'SELECT "abc"."foo","sq0"."fiz" FROM "abc" JOIN (SELECT "fiz","buz" FROM "efg" WHERE "buz"=:buz)'
            ' "sq0" ON "abc"."bar"="sq0"."buz" WHERE "abc"."bar"=:bar',
            sql,
        )
        self.assertEqual({':buz': 'buz', 'bar': 'bar'}, parameter.get_parameters())

    def test_pyformat_parameter(self):
        q = Query.into(self.table_abc).columns("a", "b", "c").insert(1, 2.2, 'foo')

        parameter = PyformatParameter()
        sql = q.get_sql(parameter=parameter)
        self.assertEqual('INSERT INTO "abc" ("a","b","c") VALUES (%(param1)s,%(param2)s,%(param3)s)', sql)
        self.assertEqual({"param1": 1, "param2": 2.2, "param3": "foo"}, parameter.get_parameters())


class ParametrizedBackslashTests(unittest.TestCase):
    # MySQL and ClickHouse escape backslashes in inline SQL string literals, but a
    # value collected for a bound parameter is not an SQL literal: the bound value
    # must be the original, unescaped value.

    def test_mysql_format_parameter_keeps_original_value(self):
        parameter = FormatParameter()
        q = MySQLQuery.update(Table("t")).set("col", "a\\b")

        self.assertEqual("UPDATE `t` SET `col`=%s", q.get_sql(parameter=parameter))
        self.assertEqual(["a\\b"], parameter.get_parameters())

    def test_mysql_qmark_parameter_keeps_original_value(self):
        parameter = QmarkParameter()
        q = MySQLQuery.update(Table("t")).set("col", "a\\b")

        self.assertEqual("UPDATE `t` SET `col`=?", q.get_sql(parameter=parameter))
        self.assertEqual(["a\\b"], parameter.get_parameters())

    def test_mysql_named_parameter_keeps_original_value(self):
        parameter = NamedParameter("param")
        q = MySQLQuery.update(Table("t")).set("col", "a\\b")

        self.assertEqual("UPDATE `t` SET `col`=:param", q.get_sql(parameter=parameter))
        self.assertEqual({"param": "a\\b"}, parameter.get_parameters())

    def test_clickhouse_format_parameter_keeps_original_value(self):
        parameter = FormatParameter()
        q = ClickHouseQuery.update(Table("t")).set("col", "a\\b")

        self.assertEqual('ALTER TABLE "t" UPDATE "col"=%s', q.get_sql(parameter=parameter))
        self.assertEqual(["a\\b"], parameter.get_parameters())

    def test_mysql_inline_literal_is_still_escaped(self):
        # Side by side with the parameter tests above: the inline path still escapes
        q = MySQLQuery.update(Table("t")).set("col", "a\\b")

        self.assertEqual("UPDATE `t` SET `col`='a\\\\b'", q.get_sql())


class ExplicitPlaceholderTests(unittest.TestCase):
    table_abc = Table("abc")

    def test_explicit_placeholder_is_rendered_without_a_collector(self):
        # Callers that only want a SQL script keep getting the literal placeholder.
        q = Query.from_(self.table_abc).select("*").where(self.table_abc.foo == Parameter(":site"))

        self.assertEqual('SELECT * FROM "abc" WHERE "foo"=:site', q.get_sql())

    def test_explicit_placeholder_value_is_not_collected(self):
        # An explicit placeholder is supplied by the caller, so no value may be
        # collected for it: the value must be bound externally.
        parameter = NamedParameter()
        q = Query.from_(self.table_abc).select("*").where(self.table_abc.foo == Parameter(":site"))

        self.assertEqual('SELECT * FROM "abc" WHERE "foo"=:site', q.get_sql(parameter=parameter))
        self.assertEqual({}, parameter.get_parameters())

    def test_named_autogenerated_names_avoid_explicit_placeholder(self):
        # The explicit :param2 must keep the generated names from reusing "param2".
        parameter = NamedParameter()
        q = (
            Query.from_(self.table_abc)
            .select("*")
            .where(self.table_abc.foo == Parameter(":param2"))
            .where(self.table_abc.bar == "buz")
            .where(self.table_abc.baz == "fiz")
        )

        self.assertEqual(
            'SELECT * FROM "abc" WHERE "foo"=:param2 AND "bar"=:param1 AND "baz"=:param3',
            q.get_sql(parameter=parameter),
        )
        self.assertEqual({"param1": "buz", "param3": "fiz"}, parameter.get_parameters())

    def test_pyformat_autogenerated_names_avoid_explicit_placeholder(self):
        parameter = PyformatParameter()
        q = (
            Query.from_(self.table_abc)
            .select("*")
            .where(self.table_abc.foo == Parameter("%(param2)s"))
            .where(self.table_abc.bar == "buz")
            .where(self.table_abc.baz == "fiz")
        )

        self.assertEqual(
            'SELECT * FROM "abc" WHERE "foo"=%(param2)s AND "bar"=%(param1)s AND "baz"=%(param3)s',
            q.get_sql(parameter=parameter),
        )
        self.assertEqual({"param1": "buz", "param3": "fiz"}, parameter.get_parameters())

    def test_parameter_value_wrapper_name_is_avoided_by_autogenerated_names(self):
        parameter = NamedParameter()
        q = (
            Query.from_(self.table_abc)
            .select("*")
            .where(self.table_abc.foo == ParameterValueWrapper(Parameter(":param1"), "ext"))
            .where(self.table_abc.bar == "buz")
            .where(self.table_abc.baz == "fiz")
        )

        self.assertEqual(
            'SELECT * FROM "abc" WHERE "foo"=:param1 AND "bar"=:param2 AND "baz"=:param3',
            q.get_sql(parameter=parameter),
        )
        self.assertEqual({":param1": "ext", "param2": "buz", "param3": "fiz"}, parameter.get_parameters())

    def test_multiple_explicit_placeholders_are_all_avoided(self):
        parameter = NamedParameter()
        q = (
            Query.from_(self.table_abc)
            .select("*")
            .where(self.table_abc.foo == Parameter(":param1"))
            .where(self.table_abc.bar == "buz")
            .where(self.table_abc.baz == Parameter(":param3"))
            .where(self.table_abc.fiz == "fiz")
        )

        self.assertEqual(
            'SELECT * FROM "abc" WHERE "foo"=:param1 AND "bar"=:param2 AND "baz"=:param3 AND "fiz"=:param4',
            q.get_sql(parameter=parameter),
        )
        self.assertEqual({"param2": "buz", "param4": "fiz"}, parameter.get_parameters())

    def test_explicit_placeholder_rendering_after_collected_values_is_reserved(self):
        # Generated names are emitted before the explicit placeholder is rendered,
        # so its name has to be reserved up front rather than discovered in passing.
        parameter = NamedParameter()
        q = (
            Query.from_(self.table_abc)
            .select("*")
            .where(self.table_abc.bar == "buz")
            .where(self.table_abc.baz == "fiz")
            .where(self.table_abc.foo == Parameter(":param2"))
        )

        self.assertEqual(
            'SELECT * FROM "abc" WHERE "bar"=:param1 AND "baz"=:param3 AND "foo"=:param2',
            q.get_sql(parameter=parameter),
        )
        self.assertEqual({"param1": "buz", "param3": "fiz"}, parameter.get_parameters())

    def test_explicit_placeholder_in_subquery_is_reserved(self):
        # The pre-render walk must descend into subqueries to find their placeholders.
        table_efg = Table("efg")
        parameter = NamedParameter()
        subquery = Query.from_(table_efg).select("fiz").where(table_efg.buz == Parameter(":param1"))
        q = Query.from_(self.table_abc).select("*").where(self.table_abc.id.isin(subquery)).where(
            self.table_abc.bar == "buz"
        )

        self.assertEqual(
            'SELECT * FROM "abc" WHERE "id" IN (SELECT "fiz" FROM "efg" WHERE "buz"=:param1) AND "bar"=:param2',
            q.get_sql(parameter=parameter),
        )
        self.assertEqual({"param2": "buz"}, parameter.get_parameters())

    def test_collectors_are_independent_between_renders(self):
        q = Query.from_(self.table_abc).select("*").where(self.table_abc.foo == Parameter(":param1")).where(
            self.table_abc.bar == "x"
        )

        first = NamedParameter()
        self.assertEqual(
            'SELECT * FROM "abc" WHERE "foo"=:param1 AND "bar"=:param2',
            q.get_sql(parameter=first),
        )
        self.assertEqual({"param2": "x"}, first.get_parameters())

        second = NamedParameter()
        self.assertEqual(
            'SELECT * FROM "abc" WHERE "foo"=:param1 AND "bar"=:param2',
            q.get_sql(parameter=second),
        )
        self.assertEqual({"param2": "x"}, second.get_parameters())


class ParameterizedUnionCTETests(unittest.TestCase):
    # A warehouse-style statement: a constant parameter lives in a CTE defined once
    # and referenced by both UNION branches, a range filter appears physically in
    # each branch, and an outer filter takes the site parameter.
    readings = Table("readings")
    cte_name = "bounds"
    tenant_id = 777
    low_limit = 10
    high_limit = 90

    def _builder(self):
        # SQLite cannot parenthesise the branches of a compound SELECT.
        return Query._builder(wrap_set_operation_queries=False)

    def build_query(self, site):
        cte = Query.select(ValueWrapper(self.tenant_id).as_("tenant_id"))
        cte_ref = AliasedQuery(self.cte_name)

        low_branch = (
            self._builder()
            .from_(self.readings)
            .cross_join(cte_ref)
            .cross()
            .select(
                self.readings.station,
                self.readings.measure,
                self.readings.value,
                Field("tenant_id", table=cte_ref),
            )
            .where(self.readings.measure >= self.low_limit)
        )
        high_branch = (
            self._builder()
            .from_(self.readings)
            .cross_join(cte_ref)
            .cross()
            .select(
                self.readings.station,
                self.readings.measure,
                self.readings.value,
                Field("tenant_id", table=cte_ref),
            )
            .where(self.readings.measure <= self.high_limit)
        )

        union = low_branch.union(high_branch)
        return (
            Query.with_(cte, self.cte_name)
            .from_(union)
            .select(Field("station"), Field("measure"), Field("value"), Field("tenant_id"))
            .where(Field("station") == site)
        )

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.execute("CREATE TABLE readings (station TEXT, measure INTEGER, value TEXT)")
        self.connection.executemany(
            "INSERT INTO readings VALUES (?, ?, ?)",
            [
                ("ST-1", 5, "a-low"),
                ("ST-1", 50, "b-mid"),
                ("ST-1", 95, "c-high"),
                ("ST-2", 50, "d-mid"),
            ],
        )
        self.connection.commit()

    def test_archived_sql_inlines_values(self):
        sql = self.build_query("ST-1").get_sql()

        self.assertEqual(
            'WITH bounds AS (SELECT 777 "tenant_id") '
            'SELECT "station","measure","value","tenant_id" FROM ('
            'SELECT "readings"."station","readings"."measure","readings"."value","bounds"."tenant_id" '
            'FROM "readings" CROSS JOIN bounds WHERE "readings"."measure">=10 '
            "UNION "
            'SELECT "readings"."station","readings"."measure","readings"."value","bounds"."tenant_id" '
            'FROM "readings" CROSS JOIN bounds WHERE "readings"."measure"<=90'
            ') "sq0" WHERE "station"=\'ST-1\'',
            sql,
        )

    def test_qmark_bind_order_and_replay(self):
        query = self.build_query("ST-1")
        parameter = QmarkParameter()
        sql = query.get_sql(parameter=parameter)

        # One bind position in the CTE, one per physical UNION branch, then the site.
        self.assertEqual(4, sql.count("?"))
        self.assertEqual(1, sql.count("SELECT ? "))
        self.assertEqual([self.tenant_id, self.low_limit, self.high_limit, "ST-1"], parameter.get_parameters())

        rows = self.connection.execute(sql, parameter.get_parameters()).fetchall()
        self.assertEqual(
            [
                ("ST-1", 5, "a-low", 777),
                ("ST-1", 50, "b-mid", 777),
                ("ST-1", 95, "c-high", 777),
            ],
            sorted(rows),
        )

    def test_named_bind_keys_and_replay(self):
        query = self.build_query("ST-1")
        parameter = NamedParameter()
        sql = query.get_sql(parameter=parameter)

        self.assertEqual(
            {
                "param1": self.tenant_id,
                "param2": self.low_limit,
                "param3": self.high_limit,
                "param4": "ST-1",
            },
            parameter.get_parameters(),
        )
        self.assertEqual(
            'WITH bounds AS (SELECT :param1 "tenant_id") '
            'SELECT "station","measure","value","tenant_id" FROM ('
            'SELECT "readings"."station","readings"."measure","readings"."value","bounds"."tenant_id" '
            'FROM "readings" CROSS JOIN bounds WHERE "readings"."measure">=:param2 '
            "UNION "
            'SELECT "readings"."station","readings"."measure","readings"."value","bounds"."tenant_id" '
            'FROM "readings" CROSS JOIN bounds WHERE "readings"."measure"<=:param3'
            ') "sq0" WHERE "station"=:param4',
            sql,
        )

        rows = self.connection.execute(sql, parameter.get_parameters()).fetchall()
        self.assertEqual(
            [
                ("ST-1", 5, "a-low", 777),
                ("ST-1", 50, "b-mid", 777),
                ("ST-1", 95, "c-high", 777),
            ],
            sorted(rows),
        )

    def test_each_generation_owns_its_parameters(self):
        first_query = self.build_query("ST-1")
        first_parameter = QmarkParameter()
        first_sql = first_query.get_sql(parameter=first_parameter)
        first_values = list(first_parameter.get_parameters())

        # Reuse the same constructor for another outer site, producing a fresh run.
        second_parameter = QmarkParameter()
        second_sql = self.build_query("ST-2").get_sql(parameter=second_parameter)

        self.assertEqual([self.tenant_id, self.low_limit, self.high_limit, "ST-2"], second_parameter.get_parameters())
        second_rows = self.connection.execute(second_sql, second_parameter.get_parameters()).fetchall()
        self.assertEqual([("ST-2", 50, "d-mid", 777)], sorted(second_rows))

        # The first generation's parameters still replay the original site and are
        # not overwritten by the values generated for the second site.
        self.assertEqual([self.tenant_id, self.low_limit, self.high_limit, "ST-1"], first_values)
        first_rows = self.connection.execute(first_sql, first_values).fetchall()
        self.assertEqual(
            [
                ("ST-1", 5, "a-low", 777),
                ("ST-1", 50, "b-mid", 777),
                ("ST-1", 95, "c-high", 777),
            ],
            sorted(first_rows),
        )
