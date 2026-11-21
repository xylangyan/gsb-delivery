import sqlite3
import unittest

from pypika import (
    AliasedQuery,
    Dialects,
    JoinType,
    MSSQLQuery,
    NamedParameter,
    QmarkParameter,
    Query,
    SQLLiteQuery,
    SYSTEM_TIME,
    Snapshot,
    Table,
)
from pypika.terms import NullValue
from pypika.utils import QueryException

MONTH_OPEN = "2026-11-01"
MONTH_CLOSE = "2026-11-30"
PREV_DAY = "2026-10-31"
FIXED_TAX_AS_OF = "2026-10-01"


def snapshot_table(name):
    return (
        Table(name)
        .for_(SYSTEM_TIME.as_of(Snapshot()))
        .with_offline_history("{name}_history".format(name=name), "valid_from", "valid_to")
    )


def build_template():
    """
    The month-end report template. The caller builds it once and holds the query object;
    snapshot moments are bound later, per reference, via .as_of().

    - inventory: plain table (the stock list)
    - prices: temporal table read at the reference's snapshot moment
    - tax_codes: temporal table read at a fixed AS OF that must not follow the report moments
    - returns branch: union leg using historical prices, visible only for snapshots on or
      after the return date
    """
    inventory = Table("inventory")
    prices = snapshot_table("prices")
    tax_codes = (
        Table("tax_codes")
        .for_(SYSTEM_TIME.as_of(FIXED_TAX_AS_OF))
        .with_offline_history("tax_codes_history", "valid_from", "valid_to")
    )

    stock = (
        Query.from_(inventory)
        .left_join(prices)
        .on(prices.warehouse == inventory.warehouse)
        .left_join(tax_codes)
        .on(tax_codes.warehouse == inventory.warehouse)
        .select(inventory.warehouse, prices.price, tax_codes.tax_code)
    )

    returns = Table("returns")
    returns_prices = snapshot_table("prices")
    returned = (
        Query.from_(returns)
        .left_join(returns_prices)
        .on(returns_prices.warehouse == returns.warehouse)
        .where(returns.return_date <= Snapshot())
        .select(returns.warehouse, returns_prices.price, NullValue())
    )

    return stock.union_all(returned)


def build_final_report(query_cls, template):
    opening = AliasedQuery("opening")
    closing = AliasedQuery("closing")
    return (
        query_cls.with_(template.as_of(MONTH_OPEN), "opening")
        .with_(template.as_of(MONTH_CLOSE), "closing")
        .from_(opening)
        .join(closing, JoinType.full_outer)
        .on(closing.warehouse == opening.warehouse)
        .select(
            opening.warehouse.as_("open_warehouse"),
            closing.warehouse.as_("close_warehouse"),
            opening.price.as_("open_price"),
            closing.price.as_("close_price"),
            opening.tax_code.as_("open_tax_code"),
            closing.tax_code.as_("close_tax_code"),
        )
    )


def build_prev_day_report(query_cls, template):
    return query_cls.with_(template.as_of(PREV_DAY), "previous").from_(AliasedQuery("previous")).select("*")


class SnapshotBindingTests(unittest.TestCase):
    def setUp(self):
        self.template = build_template()

    def test_each_reference_uses_its_own_moment(self):
        sql = build_final_report(Query, self.template).get_sql()

        self.assertEqual(2, sql.count("FOR SYSTEM_TIME AS OF '{}'".format(MONTH_OPEN)))
        self.assertEqual(2, sql.count("FOR SYSTEM_TIME AS OF '{}'".format(MONTH_CLOSE)))
        # the fixed tax-code lookup follows the template, not the report moments
        self.assertEqual(2, sql.count("FOR SYSTEM_TIME AS OF '{}'".format(FIXED_TAX_AS_OF)))
        self.assertIn("'{}'".format(MONTH_OPEN), sql.split("opening")[1])
        self.assertLess(sql.index(MONTH_OPEN), sql.index("closing"))

    def test_template_is_not_mutated_by_binding(self):
        self.template.as_of(MONTH_OPEN)
        self.template.as_of(MONTH_CLOSE)

        sql = self.template.get_sql()
        self.assertNotIn(MONTH_OPEN, sql)
        self.assertNotIn(MONTH_CLOSE, sql)
        # unbound snapshots render as placeholders instead of inheriting a previous moment
        self.assertEqual(2, sql.count("FOR SYSTEM_TIME AS OF ?"))

    def test_generation_order_does_not_matter(self):
        first = (
            build_prev_day_report(Query, self.template).get_sql(),
            build_final_report(Query, self.template).get_sql(),
        )
        second = (
            build_prev_day_report(Query, self.template).get_sql(),
            build_final_report(Query, self.template).get_sql(),
        )

        self.assertEqual(first, second)
        self.assertIn(PREV_DAY, first[0])
        self.assertNotIn(MONTH_CLOSE, first[0])

    def test_snapshot_default_is_used_when_unbound(self):
        table = Table("prices").for_(SYSTEM_TIME.as_of(Snapshot(default=PREV_DAY)))
        sql = Query.from_(table).select("*").get_sql()
        self.assertIn("FOR SYSTEM_TIME AS OF '{}'".format(PREV_DAY), sql)

    def test_moment_may_be_a_parameter_term(self):
        sql = self.template.as_of(NamedParameter("snap")).get_sql()
        self.assertEqual(2, sql.count("FOR SYSTEM_TIME AS OF :snap"))

    def test_fixed_between_and_plain_tables_are_unchanged(self):
        table = Table("prices").for_(SYSTEM_TIME.between("2026-11-01", "2026-12-01"))
        self.assertEqual(
            'SELECT * FROM "prices" FOR SYSTEM_TIME BETWEEN \'2026-11-01\' AND \'2026-12-01\'',
            SQLLiteQuery.from_(table).select("*").get_sql(),
        )
        self.assertEqual('SELECT * FROM "inventory"', SQLLiteQuery.from_("inventory").select("*").get_sql())


class MSSQLReportTests(unittest.TestCase):
    def setUp(self):
        self.template = build_template()

    def test_every_history_read_carries_for_system_time(self):
        sql = build_final_report(MSSQLQuery, self.template).get_sql()

        self.assertEqual(2, sql.count("FOR SYSTEM_TIME AS OF '{}'".format(MONTH_OPEN)))
        self.assertEqual(2, sql.count("FOR SYSTEM_TIME AS OF '{}'".format(MONTH_CLOSE)))
        self.assertEqual(2, sql.count("FOR SYSTEM_TIME AS OF '{}'".format(FIXED_TAX_AS_OF)))
        self.assertNotIn("_history", sql)

    def test_parameter_positions_follow_the_references(self):
        parameter = QmarkParameter()
        sql = build_final_report(MSSQLQuery, self.template).get_sql(parameter=parameter)

        self.assertEqual(6, sql.count("FOR SYSTEM_TIME AS OF ?"))
        self.assertEqual(
            [
                MONTH_OPEN,
                FIXED_TAX_AS_OF,
                MONTH_OPEN,
                MONTH_OPEN,
                MONTH_CLOSE,
                FIXED_TAX_AS_OF,
                MONTH_CLOSE,
                MONTH_CLOSE,
            ],
            parameter.get_parameters(),
        )

    def test_standalone_template_does_not_inherit_report_moments(self):
        build_final_report(MSSQLQuery, self.template).get_sql()

        sql = self.template.get_sql()
        self.assertNotIn(MONTH_OPEN, sql)
        self.assertNotIn(MONTH_CLOSE, sql)
        self.assertIn("FOR SYSTEM_TIME AS OF ?", sql)
        self.assertIn("FOR SYSTEM_TIME AS OF '{}'".format(FIXED_TAX_AS_OF), sql)


class SQLiteEmulationTests(unittest.TestCase):
    def setUp(self):
        self.template = build_template()
        self.db = sqlite3.connect(":memory:")
        self.db.executescript(
            """
            CREATE TABLE inventory (warehouse TEXT);
            INSERT INTO inventory VALUES ('W1'), ('W2'), ('W3');

            CREATE TABLE prices_history (warehouse TEXT, price REAL, valid_from TEXT, valid_to TEXT);
            -- W1: the old version ends exactly where the next one starts
            INSERT INTO prices_history VALUES
                ('W1', 10, '2026-10-01', '2026-11-01'),
                ('W1', 12, '2026-11-01', '2026-12-01'),
                -- W2: priced at the month open only
                ('W2', 20, '2026-11-01', '2026-11-02');
            -- W3: never priced

            CREATE TABLE tax_codes_history (warehouse TEXT, tax_code TEXT, valid_from TEXT, valid_to TEXT);
            INSERT INTO tax_codes_history VALUES
                ('W1', 'TAX-A', '2026-01-01', '2026-11-15'),
                ('W1', 'TAX-B', '2026-11-15', '2027-01-01'),
                ('W2', 'TAX-C', '2026-01-01', '2027-01-01');

            CREATE TABLE returns (warehouse TEXT, return_date TEXT, qty INT);
            -- only visible for snapshots on or after 2026-11-15
            INSERT INTO returns VALUES ('W1', '2026-11-15', 3);
            """
        )

    def tearDown(self):
        self.db.close()

    def test_offline_read_uses_history_table_with_half_open_periods(self):
        bound = Query.from_(snapshot_table("prices")).select("*").as_of(MONTH_OPEN)
        sql = bound.get_sql(dialect=Dialects.SQLLITE)
        self.assertIn(
            '(SELECT * FROM "prices_history" WHERE "valid_from"<=\'2026-11-01\' AND "valid_to">\'2026-11-01\')',
            sql,
        )

    def test_missing_offline_history_declaration_reports_the_read(self):
        prices = Table("prices").for_(SYSTEM_TIME.as_of(Snapshot()))
        query = SQLLiteQuery.from_(Table("inventory")).select("*").join(prices).on(prices.warehouse == "W1")

        with self.assertRaises(QueryException) as ctx:
            query.as_of(MONTH_OPEN).get_sql()

        self.assertIn('"prices"', str(ctx.exception))

    def test_final_report_rows_follow_each_references_moment(self):
        sql = build_final_report(SQLLiteQuery, self.template).get_sql()
        rows = {tuple(row) for row in self.db.execute(sql)}

        self.assertEqual(
            {
                # W1: boundary at month open belongs to the new version (12, not 10);
                # the returns leg of the closing reference also matches here
                ("W1", "W1", 12.0, 12.0, "TAX-A", "TAX-A"),
                ("W1", "W1", 12.0, 12.0, "TAX-A", None),
                # W2: priced at the month open only
                ("W2", "W2", 20.0, None, "TAX-C", "TAX-C"),
                # W3: never priced, still delivered
                ("W3", "W3", None, None, None, None),
            },
            rows,
        )

    def test_returns_record_only_lands_in_the_month_close_reference(self):
        opening = AliasedQuery("opening")
        closing = AliasedQuery("closing")
        report = (
            SQLLiteQuery.with_(self.template.as_of(MONTH_OPEN), "opening")
            .with_(self.template.as_of(MONTH_CLOSE), "closing")
            .from_(opening)
            .select("*")
        )
        opening_rows = list(self.db.execute(report.get_sql()))

        report2 = (
            SQLLiteQuery.with_(self.template.as_of(MONTH_OPEN), "opening")
            .with_(self.template.as_of(MONTH_CLOSE), "closing")
            .from_(closing)
            .select("*")
        )
        closing_rows = list(self.db.execute(report2.get_sql()))

        self.assertEqual(3, len(opening_rows))
        self.assertEqual(4, len(closing_rows))
        self.assertIn(("W1", 12.0, None), [tuple(row) for row in closing_rows])
        self.assertNotIn(("W1", 12.0, None), [tuple(row) for row in opening_rows])

    def test_prev_day_report_then_final_report_then_swapped(self):
        prev_sql = build_prev_day_report(SQLLiteQuery, self.template).get_sql()
        final_sql = build_final_report(SQLLiteQuery, self.template).get_sql()

        prev_rows = {tuple(row) for row in self.db.execute(prev_sql)}
        self.assertEqual(
            {
                ("W1", 10.0, "TAX-A"),
                ("W2", None, "TAX-C"),
                ("W3", None, None),
            },
            prev_rows,
        )

        # regenerating in the opposite order yields identical SQL
        self.assertEqual(final_sql, build_final_report(SQLLiteQuery, self.template).get_sql())
        self.assertEqual(prev_sql, build_prev_day_report(SQLLiteQuery, self.template).get_sql())


if __name__ == "__main__":
    unittest.main()
