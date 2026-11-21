import unittest

from pypika import AliasedQuery, Query, Table
from pypika.dialects import MSSQLQuery, PostgreSQLQuery
from pypika.terms import ValueWrapper
from pypika.utils import QueryException


class RecursiveCTETests(unittest.TestCase):
    """
    Warehouse inventory scenario: shelves and their sub-locations are written into an end-of-day snapshot via a
    recursive CTE declaring id/parent_id/depth, whose branch aliases differ from the declared output columns.
    """

    physical_shelf = Table("physical_shelf")
    location = Table("location")
    shelf_ref = AliasedQuery("shelf")

    def root_query(self):
        # Root locations taken from the physical shelf table
        return (
            Query.from_(self.physical_shelf)
            .join(self.location)
            .on(self.location.id == self.physical_shelf.location_id)
            .select(
                self.location.id.as_("loc_id"),
                self.location.parent_id.as_("loc_parent"),
                ValueWrapper(0).as_("lvl"),
            )
        )

    def recursive_branch(self):
        # Next level bound only to the previous level's relation, passing depth along
        return (
            Query.from_(self.location)
            .join(self.shelf_ref)
            .on(self.location.parent_id == self.shelf_ref.id)
            .select(
                self.location.id.as_("loc_id"),
                self.location.parent_id.as_("loc_parent"),
                (self.shelf_ref.depth + 1).as_("lvl"),
            )
        )

    def nested_plain_cte(self):
        # A nested plain CTE, also named "shelf", living in a different query layer
        return (
            Query.with_(Query.from_(self.physical_shelf).select(self.physical_shelf.location_id), "shelf")
            .from_(AliasedQuery("shelf"))
            .select(AliasedQuery("shelf").location_id)
            .as_("sq")
        )

    def snapshot_query(self):
        inner = self.nested_plain_cte()
        return (
            Query.into(Table("daily_snapshot"))
            .with_recursive(self.root_query().union_all(self.recursive_branch()), "shelf", "id", "parent_id", "depth")
            .from_(self.shelf_ref)
            .join(inner)
            .on(inner.location_id == self.shelf_ref.id)
            .select(self.shelf_ref.id, self.shelf_ref.parent_id, self.shelf_ref.depth)
        )

    def test_recursive_cte_sql(self):
        self.assertEqual(
            'WITH RECURSIVE shelf(id,parent_id,depth) AS ('
            '(SELECT "location"."id" "loc_id","location"."parent_id" "loc_parent",0 "lvl" '
            'FROM "physical_shelf" JOIN "location" ON "location"."id"="physical_shelf"."location_id") '
            'UNION ALL '
            '(SELECT "location"."id" "loc_id","location"."parent_id" "loc_parent","shelf"."depth"+1 "lvl" '
            'FROM "location" JOIN shelf ON "location"."parent_id"="shelf"."id")) '
            'INSERT INTO "daily_snapshot" '
            'SELECT "shelf"."id","shelf"."parent_id","shelf"."depth" FROM shelf '
            'JOIN (WITH shelf AS (SELECT "location_id" FROM "physical_shelf") '
            'SELECT "shelf"."location_id" FROM shelf) "sq" ON "sq"."location_id"="shelf"."id"',
            str(self.snapshot_query()),
        )

    def test_inner_plain_cte_with_same_name_is_not_recursive(self):
        inner_sql = str(self.nested_plain_cte())
        self.assertEqual(
            'WITH shelf AS (SELECT "location_id" FROM "physical_shelf") SELECT "shelf"."location_id" FROM shelf',
            inner_sql,
        )
        self.assertNotIn("RECURSIVE", inner_sql)

    def test_plain_template_reusing_root_query_is_unchanged(self):
        # The non-recursive template was built earlier and reuses the same root query
        plain_template = Query.with_(self.root_query(), "shelf_roots").from_(AliasedQuery("shelf_roots")).select("*")
        plain_sql_before = str(plain_template)

        # Building the recursive snapshot afterwards must not leak RECURSIVE or column names into the template
        str(self.snapshot_query())

        self.assertEqual(plain_sql_before, str(plain_template))
        self.assertEqual(
            'WITH shelf_roots AS ('
            'SELECT "location"."id" "loc_id","location"."parent_id" "loc_parent",0 "lvl" '
            'FROM "physical_shelf" JOIN "location" ON "location"."id"="physical_shelf"."location_id") '
            'SELECT * FROM shelf_roots',
            str(plain_template),
        )

    def test_recursive_cte_via_query_entry_point(self):
        q = PostgreSQLQuery.with_recursive(
            self.root_query().union_all(self.recursive_branch()), "shelf", "id", "parent_id", "depth"
        ).from_(self.shelf_ref)
        self.assertTrue(str(q.select(self.shelf_ref.id)).startswith('WITH RECURSIVE shelf(id,parent_id,depth) AS ('))

    def test_missing_column_in_recursive_branch_locates_branch_and_expected_width(self):
        # Only the parent_id column is removed from the recursive branch
        branch = (
            Query.from_(self.location)
            .join(self.shelf_ref)
            .on(self.location.parent_id == self.shelf_ref.id)
            .select(self.location.id.as_("loc_id"), (self.shelf_ref.depth + 1).as_("lvl"))
        )
        with self.assertRaises(QueryException) as ctx:
            Query.with_recursive(self.root_query().union_all(branch), "shelf", "id", "parent_id", "depth")
        message = str(ctx.exception)
        self.assertIn("branch 2", message)
        self.assertIn("declares 3 columns", message)
        self.assertIn("selects 2 columns", message)

    def test_star_with_unknown_width_is_rejected_not_counted_as_one(self):
        branch = (
            Query.from_(self.location)
            .join(self.shelf_ref)
            .on(self.location.parent_id == self.shelf_ref.id)
            .select("*")
        )
        with self.assertRaises(QueryException) as ctx:
            Query.with_recursive(self.root_query().union_all(branch), "shelf", "id", "parent_id", "depth")
        self.assertIn("Cannot confirm", str(ctx.exception))

    def test_dialect_without_recursive_support_fails_at_construction(self):
        with self.assertRaises(QueryException) as ctx:
            MSSQLQuery.with_recursive(
                self.root_query().union_all(self.recursive_branch()), "shelf", "id", "parent_id", "depth"
            )
        message = str(ctx.exception)
        self.assertIn("MSSQLQueryBuilder", message)
        self.assertIn("with_recursive", message)

    def test_plain_with_without_column_declaration_still_works(self):
        q = Query.with_(self.root_query(), "shelf_roots").from_(AliasedQuery("shelf_roots")).select("*")
        self.assertTrue(str(q).startswith("WITH shelf_roots AS ("))
