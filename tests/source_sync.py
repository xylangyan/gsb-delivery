"""
Source-aware many-to-many reconciliation tests.

Covers per-source snapshots with independent revisions, late/redelivered
messages, explicit empty snapshots versus missing blocks, same-revision
content conflicts, in-payload quote conflicts, relationship identity, orphan
pruning, the human curator, and coexistence with the generic ManyToManyField
add/remove/clear queries.
"""
from peewee import *

from .base import ModelTestCase
from .base import TestModel
from playhouse.source_sync import (
    DuplicateQuoteError,
    RevisionConflictError,
    SourceSync,
    StaleRevisionError,
)


JIA = 'jia'
YI = 'yi'
MANUAL = 'manual'


class Warehouse(TestModel):
    code = CharField(primary_key=True)


class Good(TestModel):
    sku = CharField(primary_key=True)


sync = SourceSync(Warehouse, Good, source_field='code', good_field='sku',
                  manual_source=MANUAL)
SourceRevision = sync.revision_model
WarehouseGood = sync.relationship_model
GoodSource = sync.claim_model

# The shared relationship table doubles as a regular M2M through table.
items_field = ManyToManyField(Warehouse, backref='warehouse_items',
                              through_model=WarehouseGood)
items_field.bind(Good, 'warehouses')


ALL_MODELS = [Warehouse, Good, SourceRevision, WarehouseGood, GoodSource]


class SourceSyncTestCase(ModelTestCase):
    requires = ALL_MODELS

    def setUp(self):
        super(SourceSyncTestCase, self).setUp()
        self.warehouse = Warehouse.create(code='W')
        for sku in ('A', 'B', 'C', 'D', 'E'):
            Good.create(sku=sku)

    def goods(self):
        rows = sync.relationships(self.warehouse)
        return sorted(row.good_id for row in rows)

    def quotes(self, sku):
        return sync.get_quotes(self.warehouse, sku)


class TestSourceSyncBasics(SourceSyncTestCase):
    def test_initial_three_way_and_manual(self):
        # A is maintained by all three sources, B by jia, C by yi, D manually.
        sync.apply_snapshot(self.warehouse, JIA, 7,
                            [('A', '1.00'), ('B', '2.00')])
        sync.apply_snapshot(self.warehouse, YI, 7,
                            [('A', '1.10'), ('C', '3.00')])
        sync.manual_upsert(self.warehouse, 'D', quote='9.00', note='curated')

        self.assertEqual(self.goods(), ['A', 'B', 'C', 'D'])
        self.assertEqual(self.quotes('A'),
                         {JIA: '1.00', YI: '1.10'})
        self.assertEqual(self.quotes('B'), {JIA: '2.00'})
        self.assertEqual(self.quotes('C'), {YI: '3.00'})

        manual = sync.get_claim(self.warehouse, 'D', MANUAL)
        self.assertEqual(manual.quote, '9.00')
        self.assertEqual(manual.note, 'curated')

        # Accepted revisions are independently queryable per source.
        self.assertEqual(sync.accepted_revision(self.warehouse, JIA), 7)
        self.assertEqual(sync.accepted_revision(self.warehouse, YI), 7)
        self.assertIsNone(sync.accepted_revision(self.warehouse, 'unknown'))
        rows = {row.source: row for row in sync.revisions(self.warehouse)}
        self.assertEqual(set(rows), {JIA, YI})
        self.assertFalse(rows[JIA].is_empty)


class TestSupplierLifecycle(SourceSyncTestCase):
    def seed(self):
        sync.apply_snapshot(self.warehouse, JIA, 7,
                            [('A', '1.00'), ('B', '2.00')])
        sync.apply_snapshot(self.warehouse, YI, 7,
                            [('A', '1.10'), ('C', '3.00')])
        sync.manual_upsert(self.warehouse, 'D', quote='9.00', note='curated')

    def test_jia_revision_8_keeps_changes_deletes_adds(self):
        self.seed()
        # Keep A with a changed quote, delete B, add E.
        applied = sync.apply_snapshot(
            self.warehouse, JIA, 8, [('A', '1.50'), ('E', '5.00')])
        self.assertTrue(applied)

        self.assertEqual(self.goods(), ['A', 'C', 'D', 'E'])
        self.assertEqual(self.quotes('A'), {JIA: '1.50', YI: '1.10'})
        self.assertEqual(self.quotes('E'), {JIA: '5.00'})
        # B had only jia, so its relationship is removed entirely.
        self.assertIsNone(sync.get_claim(self.warehouse, 'B', JIA))
        self.assertEqual(sync.accepted_revision(self.warehouse, JIA), 8)

    def test_yi_own_revision_updates_c(self):
        self.seed()
        sync.apply_snapshot(self.warehouse, YI, 8,
                            [('A', '1.10'), ('C', '3.30')])
        self.assertEqual(self.quotes('C'), {YI: '3.30'})
        # Jia's state is untouched by yi's revision.
        self.assertEqual(sync.accepted_revision(self.warehouse, JIA), 7)
        self.assertEqual(self.quotes('B'), {JIA: '2.00'})

    def test_explicit_empty_snapshot_clears_only_that_source(self):
        self.seed()
        sync.apply_snapshot(self.warehouse, JIA, 9, [])

        state = sync.get_revision(self.warehouse, JIA)
        self.assertEqual(state.revision, 9)
        self.assertTrue(state.is_empty)
        # yi's C and manual D survive; A survives via yi, B (jia only) dies.
        self.assertEqual(self.goods(), ['A', 'C', 'D'])
        self.assertIsNone(sync.get_claim(self.warehouse, 'A', JIA))
        self.assertEqual(self.quotes('A'), {YI: '1.10'})


class TestMessageQueueSemantics(SourceSyncTestCase):
    def seed_to_empty_jia_9(self):
        sync.apply_snapshot(self.warehouse, JIA, 7,
                            [('A', '1.00'), ('B', '2.00')])
        sync.apply_snapshot(self.warehouse, YI, 7, [('C', '3.00')])
        sync.manual_upsert(self.warehouse, 'D', quote='9.00', note='curated')
        sync.apply_snapshot(self.warehouse, JIA, 8,
                            [('A', '1.50'), ('E', '5.00')])
        sync.apply_snapshot(self.warehouse, JIA, 9, [])

    def test_late_older_revision_rejected_and_explainable(self):
        self.seed_to_empty_jia_9()
        before = self.goods()

        with self.assertRaises(StaleRevisionError) as ctx:
            sync.apply_snapshot(self.warehouse, JIA, 8,
                                [('A', '1.50'), ('E', '5.00')])
        err = ctx.exception
        # The rejection points at the source, both revisions and the accepted
        # fingerprint -- even though revision 9 was an explicit empty list.
        self.assertEqual(err.source, JIA)
        self.assertEqual(err.revision, 8)
        self.assertEqual(err.current_revision, 9)
        self.assertEqual(len(err.current_fingerprint), 64)

        # Nothing changed: rolling back to an old revision cannot revive B/E.
        self.assertEqual(self.goods(), before)
        self.assertEqual(sync.accepted_revision(self.warehouse, JIA), 9)
        self.assertIsNone(sync.get_claim(self.warehouse, 'E', JIA))
        self.assertIsNone(sync.get_claim(self.warehouse, 'B', JIA))

    def test_missing_jia_block_is_not_an_empty_snapshot(self):
        self.seed_to_empty_jia_9()
        # A new batch only carries yi. Jia's absence must not be treated as
        # an empty jia snapshot: its accepted revision and claims stay put.
        jia_state_before = sync.get_revision(self.warehouse, JIA)
        sync.apply_snapshot(self.warehouse, YI, 9, [('C', '3.99')])

        jia_state_after = sync.get_revision(self.warehouse, JIA)
        self.assertEqual(jia_state_after.revision, jia_state_before.revision)
        self.assertEqual(jia_state_after.fingerprint,
                         jia_state_before.fingerprint)
        self.assertEqual(sync.accepted_revision(self.warehouse, YI), 9)
        self.assertEqual(self.quotes('C'), {YI: '3.99'})

    def test_exact_redelivery_is_idempotent(self):
        self.seed_to_empty_jia_9()
        claims_before = GoodSource.select().count()
        rels_before = WarehouseGood.select().count()

        result = sync.apply_snapshot(self.warehouse, JIA, 9, [])
        self.assertFalse(result)

        self.assertEqual(GoodSource.select().count(), claims_before)
        self.assertEqual(WarehouseGood.select().count(), rels_before)
        # A non-empty redelivery is idempotent too and keeps claim identity.
        sync.apply_snapshot(self.warehouse, YI, 8, [('C', '3.30')])
        claim = sync.get_claim(self.warehouse, 'C', YI)
        identity = (claim.relationship_id, claim.source)
        again = sync.apply_snapshot(self.warehouse, YI, 8, [('C', '3.30')])
        self.assertFalse(again)
        redelivered = sync.get_claim(self.warehouse, 'C', YI)
        self.assertEqual((redelivered.relationship_id, redelivered.source),
                         identity)

    def test_same_revision_different_content_conflicts(self):
        self.seed_to_empty_jia_9()
        before = self.goods()

        with self.assertRaises(RevisionConflictError) as ctx:
            sync.apply_snapshot(self.warehouse, JIA, 9, [('A', '1.50')])
        err = ctx.exception
        self.assertEqual(err.source, JIA)
        self.assertEqual(err.revision, 9)
        self.assertNotEqual(err.incoming_fingerprint,
                            err.current_fingerprint)

        # Rejected wholesale: no A claim reappears, revision still 9/empty.
        self.assertEqual(self.goods(), before)
        state = sync.get_revision(self.warehouse, JIA)
        self.assertEqual(state.revision, 9)
        self.assertTrue(state.is_empty)
        self.assertIsNone(sync.get_claim(self.warehouse, 'A', JIA))

    def test_in_payload_quote_conflict_is_atomic(self):
        self.seed_to_empty_jia_9()
        rels_before = WarehouseGood.select().count()

        with self.assertRaises(DuplicateQuoteError) as ctx:
            # A brand new source sends one good with two different quotes.
            sync.apply_snapshot(self.warehouse, 'bing', 1,
                                [('A', '1.0'), ('A', '2.0')])
        err = ctx.exception
        self.assertEqual(err.good, 'A')
        self.assertEqual(err.quotes, ['1.0', '2.0'])

        # Nothing was written for the rejected source at all.
        self.assertIsNone(sync.get_revision(self.warehouse, 'bing'))
        self.assertIsNone(sync.get_claim(self.warehouse, 'A', 'bing'))
        self.assertEqual(WarehouseGood.select().count(), rels_before)


class TestRelationshipIdentityAndPruning(SourceSyncTestCase):
    def test_identity_stable_while_any_source_remains(self):
        sync.apply_snapshot(self.warehouse, JIA, 1, [('A', '1.00')])
        relationship_id = sync.get_claim(
            self.warehouse, 'A', JIA).relationship_id

        # Quote updates and a second source joining keep the same row.
        sync.apply_snapshot(self.warehouse, JIA, 2, [('A', '1.50')])
        sync.apply_snapshot(self.warehouse, YI, 1, [('A', '1.10')])
        self.assertEqual(
            sync.get_claim(self.warehouse, 'A', JIA).relationship_id,
            relationship_id)
        self.assertEqual(
            sync.get_claim(self.warehouse, 'A', YI).relationship_id,
            relationship_id)

        # Jia leaves; yi still maintains A, so the relationship survives.
        sync.apply_snapshot(self.warehouse, JIA, 3, [])
        self.assertEqual(
            sync.get_claim(self.warehouse, 'A', YI).relationship_id,
            relationship_id)

        # Last source gone: relationship deleted.
        sync.apply_snapshot(self.warehouse, YI, 2, [])
        self.assertEqual(self.goods(), [])
        self.assertFalse(
            WarehouseGood.select()
            .where(WarehouseGood.id == relationship_id)
            .exists())

        # Re-adding later creates a fresh row rather than reviving the old one.
        sync.apply_snapshot(self.warehouse, YI, 3, [('A', '1.20')])
        new_claim = sync.get_claim(self.warehouse, 'A', YI)
        self.assertEqual(new_claim.quote, '1.20')
        self.assertEqual(self.goods(), ['A'])

    def test_manual_note_keeps_identity_and_survives_suppliers(self):
        sync.manual_upsert(self.warehouse, 'D', quote='9.00', note='note-1')
        rel_id = sync.get_claim(self.warehouse, 'D', MANUAL).relationship_id

        sync.apply_snapshot(self.warehouse, JIA, 1, [('D', '8.00')])
        sync.manual_upsert(self.warehouse, 'D', quote='9.50', note='note-2')
        claim = sync.get_claim(self.warehouse, 'D', MANUAL)
        self.assertEqual(claim.relationship_id, rel_id)
        self.assertEqual(claim.note, 'note-2')
        self.assertEqual(self.quotes('D'),
                         {MANUAL: '9.50', JIA: '8.00'})

        # Supplier drops D: manual claim and note remain, relationship remains.
        sync.apply_snapshot(self.warehouse, JIA, 2, [])
        self.assertEqual(
            sync.get_claim(self.warehouse, 'D', MANUAL).relationship_id,
            rel_id)

        # Removing the last (manual) claim prunes the relationship.
        self.assertTrue(sync.manual_remove(self.warehouse, 'D'))
        self.assertEqual(self.goods(), [])
        # Removing again is a safe no-op.
        self.assertFalse(sync.manual_remove(self.warehouse, 'D'))

    def test_failed_write_leaves_no_partial_changes(self):
        from unittest import mock

        sync.apply_snapshot(self.warehouse, JIA, 1, [('A', '1.00')])
        rels_before = WarehouseGood.select().count()
        original_insert = sync.claim_model.insert
        calls = {'n': 0}

        def flaky_insert(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 2:
                raise RuntimeError('simulated mid-import failure')
            return original_insert(*args, **kwargs)

        # The first claim would update A, the second one (E) blows up.
        with mock.patch.object(sync.claim_model, 'insert',
                               side_effect=flaky_insert):
            with self.assertRaises(RuntimeError):
                sync.apply_snapshot(self.warehouse, JIA, 2,
                                    [('A', '1.50'), ('E', '5.00')])

        # The whole transaction rolled back: revision, quotes and rows.
        self.assertEqual(sync.accepted_revision(self.warehouse, JIA), 1)
        self.assertEqual(self.quotes('A'), {JIA: '1.00'})
        self.assertIsNone(sync.get_claim(self.warehouse, 'E', JIA))
        self.assertEqual(WarehouseGood.select().count(), rels_before)


class TestGenericManyToManyQueries(SourceSyncTestCase):
    def test_add_remove_clear_against_managed_through_table(self):
        shop = self.warehouse
        a, b, c = Good.get(Good.sku == 'A'), Good.get(Good.sku == 'B'), \
            Good.get(Good.sku == 'C')

        shop.warehouse_items = [a, b]
        self.assertEqual(self.goods(), ['A', 'B'])

        shop.warehouse_items.add(c)
        self.assertEqual(self.goods(), ['A', 'B', 'C'])

        shop.warehouse_items.remove(a)
        self.assertEqual(self.goods(), ['B', 'C'])

        shop.warehouse_items.clear()
        self.assertEqual(self.goods(), [])

    def test_manual_claims_visible_through_m2m_query(self):
        sync.manual_upsert(self.warehouse, 'D', quote='9.00', note='n')
        sync.apply_snapshot(self.warehouse, JIA, 1, [('A', '1.00')])
        skus = sorted(g.sku for g in self.warehouse.warehouse_items)
        self.assertEqual(skus, ['A', 'D'])


class TestPayloadForms(SourceSyncTestCase):
    def test_dict_and_model_entries(self):
        good_a = Good.get(Good.sku == 'A')

        sync.apply_snapshot(self.warehouse, JIA, 1, [
            {'good': good_a, 'quote': 1.0},
            ('B', 2),
        ])
        self.assertEqual(self.quotes('A'), {JIA: '1.0'})
        self.assertEqual(self.quotes('B'), {JIA: '2'})

        class QuotedGood(object):
            def __init__(self, sku, quote):
                self.sku = sku
                self.quote = quote

        sync.apply_snapshot(self.warehouse, YI, 1, [
            QuotedGood('A', 1.25), QuotedGood('C', 3)])
        self.assertEqual(self.quotes('A'), {JIA: '1.0', YI: '1.25'})
        self.assertEqual(self.quotes('C'), {YI: '3'})

    def test_duplicate_good_with_same_quote_is_coalesced(self):
        sync.apply_snapshot(self.warehouse, JIA, 1,
                            [('A', '1.0'), ('A', '1.0')])
        self.assertEqual(self.quotes('A'), {JIA: '1.0'})

    def test_custom_fingerprint_drives_dedup_and_conflict(self):
        custom = SourceSync(
            Warehouse, Good, source_field='code', good_field='sku',
            fingerprint_func=lambda payload: 'fp-%d' % len(payload),
            database=sync.database)
        custom.create_tables(safe=True)
        # Same length, different quotes -> same custom fingerprint -> no-op.
        self.assertTrue(custom.apply_snapshot(
            self.warehouse, JIA, 1, [('A', '1.0'), ('B', '2.0')]))
        self.assertFalse(custom.apply_snapshot(
            self.warehouse, JIA, 1, [('A', '9.0'), ('B', '8.0')]))
        # Different length -> different fingerprint -> conflict on same rev.
        with self.assertRaises(RevisionConflictError):
            custom.apply_snapshot(self.warehouse, JIA, 1, [('A', '1.0')])


class TestWarehouseIsolation(SourceSyncTestCase):
    def test_revisions_and_relationships_per_warehouse(self):
        other = Warehouse.create(code='W2')
        sync.apply_snapshot(self.warehouse, JIA, 8, [('A', '1.00')])
        sync.apply_snapshot(other, JIA, 3, [('B', '2.00')])

        self.assertEqual(sync.accepted_revision(self.warehouse, JIA), 8)
        self.assertEqual(sync.accepted_revision(other, JIA), 3)
        self.assertEqual(self.goods(), ['A'])
        self.assertEqual(
            sorted(r.good_id for r in sync.relationships(other)), ['B'])

        # A stale revision for one warehouse does not affect the other.
        with self.assertRaises(StaleRevisionError):
            sync.apply_snapshot(other, JIA, 2, [('A', '1.00')])
        self.assertEqual(
            sorted(r.good_id for r in sync.relationships(other)), ['B'])
