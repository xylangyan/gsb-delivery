# mypy: disallow_untyped_defs=False
import json
import unittest

import extruct
from extruct.blockextraction import (
    ADDED,
    ANCHOR_DEF,
    PROPERTY,
    REBOUND,
    REFERENCE,
    REMOVED,
    BlockRelationStore,
)

BASE_URL = "http://shop.example.com/product/"

SPECS_HTML = """
<div id="specs" itemscope itemtype="http://schema.org/Product" itemref="unit">
  <span itemprop="name">Widget</span>
  <div itemprop="offers" itemscope itemtype="http://schema.org/Offer">
    <meta itemprop="price" content="10.00" />
  </div>
</div>
"""

RECS_HTML = """
<div id="recs" itemscope itemtype="http://schema.org/Recommendation" itemref="unit">
  <div itemprop="offers" itemscope itemtype="http://schema.org/Offer">
    <meta itemprop="price" content="10.00" />
  </div>
</div>
"""

UNIT_HTML = (
    '<div id="unit" itemscope itemtype="http://schema.org/Unit">'
    '<span itemprop="name">piece</span></div>'
)


def _refs(relations, anchor=None, kind=None):
    out = []
    for rel in relations:
        if kind is not None and rel.kind != kind:
            continue
        if anchor is not None and rel.anchor != anchor:
            continue
        out.append(rel)
    return out


class TestSkeleton(unittest.TestCase):
    def test_pending_block_keeps_skeleton_but_no_relations(self):
        store = BlockRelationStore(base_url=BASE_URL)
        specs = store.add_skeleton("specs", (0,))
        store.add_skeleton(None, (2,))

        self.assertEqual([str(key) for key in store.pending_blocks()],
                         ["block#specs", "block@2"])
        self.assertEqual(store.relations(), [])
        self.assertFalse(store.is_complete())

        state = store.get_block(specs)
        self.assertFalse(state.arrived)
        self.assertEqual(state.base_url, BASE_URL)


class TestBlockRelations(unittest.TestCase):
    def setUp(self):
        self.store = BlockRelationStore(base_url=BASE_URL)
        self.key = self.store.add_skeleton("specs", (0,))
        self.store.ingest_block(SPECS_HTML, "specs", (0,))

    def test_relations_carry_origin_vocabulary_and_base(self):
        by_name = {rel.name: rel for rel in self.store.relations()}
        self.assertEqual(by_name["name"].kind, PROPERTY)
        self.assertEqual(by_name["name"].value, "Widget")
        self.assertEqual(by_name["name"].vocabulary,
                         "http://schema.org/Product")
        self.assertEqual(by_name["name"].base_url, BASE_URL)
        # origin record points back to the exact DOM node of the property
        origin = by_name["name"].origin_node.to_ref()
        self.assertEqual(origin["block"], "block#specs")
        self.assertNotIn("id", origin)  # the <span> itself is anonymous
        self.assertTrue(origin["path"])  # ...but its relative position is kept
        # the owning scope is the public #specs node
        self.assertEqual(by_name["name"].source.element_id, "specs")

    def test_nested_scope_declares_its_own_vocabulary(self):
        # the price lives in the Offer subtree, which keeps its own vocabulary;
        # correcting the parent Product declaration must not rewrite it
        price = next(rel for rel in self.store.relations() if rel.name == "price")
        offers = next(rel for rel in self.store.relations() if rel.name == "offers")
        self.assertEqual(price.vocabulary, "http://schema.org/Offer")
        self.assertEqual(offers.vocabulary, "http://schema.org/Product")
        self.assertEqual(offers.kind, REFERENCE)
        self.assertTrue(offers.target_resolved)

    def test_relations_are_json_serializable(self):
        json.dumps(self.store.relation_dicts())


class TestCrossBlockItemref(unittest.TestCase):
    def _populate(self, unit_html=UNIT_HTML):
        store = BlockRelationStore(base_url=BASE_URL)
        store.add_skeleton("specs", (0,))
        store.add_skeleton("recs", (1,))
        store.add_skeleton("unit", (2,))
        store.ingest_block(SPECS_HTML, "specs", (0,))
        store.ingest_block(RECS_HTML, "recs", (1,))
        return store

    def test_reference_unresolved_until_anchor_block_arrives(self):
        store = self._populate()
        unit_refs = _refs(store.relations(include_unresolved=True),
                          anchor="unit", kind=REFERENCE)
        self.assertEqual(len(unit_refs), 2)
        self.assertTrue(all(not rel.target_resolved for rel in unit_refs))
        # nothing resolvable is exposed through the default active query
        self.assertEqual(_refs(store.relations(), anchor="unit"), [])

        changes = store.ingest_block(UNIT_HTML, "unit", (2,))
        added = [change for change in changes
                 if change.relation.kind == REFERENCE
                 and change.relation.anchor == "unit"]
        self.assertEqual([change.action for change in added], [ADDED, ADDED])

        unit_refs = _refs(store.relations(), anchor="unit", kind=REFERENCE)
        self.assertEqual(len(unit_refs), 2)
        self.assertTrue(all(rel.target_resolved for rel in unit_refs))
        # both scopes resolve to the very same #unit node
        targets = {json.dumps(rel.target_ref, sort_keys=True) for rel in unit_refs}
        self.assertEqual(len(targets), 1)
        self.assertTrue(store.is_complete())

    def test_anonymous_offers_with_same_content_stay_separated_by_parent(self):
        store = self._populate()
        store.ingest_block(UNIT_HTML, "unit", (2,))

        offers = [rel for rel in store.relations()
                  if rel.kind == REFERENCE and rel.name == "offers"]
        self.assertEqual(len(offers), 2)
        # both are anonymous (no public id) yet their identities differ
        self.assertTrue(all(rel.target.is_anonymous for rel in offers))
        identities = {(rel.target.block.block_id, rel.target.rel_path)
                      for rel in offers}
        self.assertEqual(len(identities), 2)
        # the two sources keep their own parent block
        source_blocks = {rel.source.block.block_id for rel in offers}
        self.assertEqual(source_blocks, {"specs", "recs"})

        # the two independent, identical anonymous prices are records that lead
        # back to their original DOM position
        prices = [rel for rel in store.relations() if rel.name == "price"]
        self.assertEqual(len(prices), 2)
        self.assertEqual({rel.value for rel in prices}, {"10.00"})
        origins = {json.dumps(rel.origin_node.to_ref(), sort_keys=True)
                   for rel in prices}
        self.assertEqual(len(origins), 2)


class TestIncrementalUpdates(unittest.TestCase):
    def test_archived_block_is_not_recomputed(self):
        store = BlockRelationStore(base_url=BASE_URL)
        specs = store.add_skeleton("specs", (0,))
        store.add_skeleton("unit", (1,))
        store.ingest_block(SPECS_HTML, "specs", (0,))
        archived = {
            rel.key(): rel
            for rel in store.relations(specs, include_unresolved=True)
        }

        changes = store.ingest_block(UNIT_HTML, "unit", (1,))
        # every change belongs to the block that just arrived or is a binding;
        # no relation owned by the archived specs block is added/removed
        owned_changes = [
            change for change in changes
            if change.relation.origin_block == specs
            and change.relation.kind != REFERENCE
        ]
        self.assertEqual(owned_changes, [])
        current = {
            rel.key(): rel
            for rel in store.relations(specs, include_unresolved=True)
        }
        self.assertEqual(set(archived), set(current))

    def test_indirect_reference_rebinds_only_when_its_anchor_changes(self):
        store = BlockRelationStore(base_url=BASE_URL)
        store.add_skeleton("specs", (0,))
        store.add_skeleton("unit", (1,))
        store.ingest_block(SPECS_HTML, "specs", (0,))
        store.ingest_block(UNIT_HTML, "unit", (1,))
        ref = _refs(store.relations(), anchor="unit", kind=REFERENCE)[0]
        first_target = dict(ref.target_ref)

        # an unrelated block does not touch the binding
        store.add_skeleton("other", (2,))
        unrelated = store.ingest_block(
            '<div id="other" itemscope itemtype="http://schema.org/Thing"></div>',
            "other", (2,),
        )
        self.assertNotIn(
            REBOUND, [change.action for change in unrelated]
        )
        ref = _refs(store.relations(), anchor="unit", kind=REFERENCE)[0]
        self.assertEqual(ref.target_ref, first_target)

        # redefining #unit behind a wrapper changes the resolved node identity
        wrapped = ('<section><div id="unit" itemscope '
                   'itemtype="http://schema.org/Unit"></div></section>')
        changes = store.ingest_block(wrapped, "unit", (1,))
        rebind = [change for change in changes
                  if change.relation.anchor == "unit"
                  and change.relation.kind == REFERENCE]
        self.assertEqual([change.action for change in rebind], [REBOUND])
        ref = _refs(store.relations(), anchor="unit", kind=REFERENCE)[0]
        self.assertTrue(ref.target_resolved)
        self.assertNotEqual(ref.target_ref, first_target)
        self.assertEqual(ref.target_ref["path"], [0])

    def test_anchor_removal_hides_dangling_link_but_keeps_old_readable(self):
        store = BlockRelationStore(base_url=BASE_URL)
        store.add_skeleton("specs", (0,))
        store.add_skeleton("unit", (1,))
        store.ingest_block(SPECS_HTML, "specs", (0,))
        store.ingest_block(UNIT_HTML, "unit", (1,))
        old_ref = dict(
            _refs(store.relations(), anchor="unit", kind=REFERENCE)[0].target_ref
        )

        # new version of the unit block no longer exposes the #unit anchor
        replacement = ('<div id="unit-v2" itemscope '
                       'itemtype="http://schema.org/Unit"></div>')
        changes = store.ingest_block(replacement, "unit", (1,))
        removed = [change for change in changes
                   if change.relation.anchor == "unit"
                   and change.relation.kind == REFERENCE]
        self.assertEqual([change.action for change in removed], [REMOVED])

        # active query only keeps associations that have a real target
        self.assertEqual(_refs(store.relations(), anchor="unit"), [])
        # the old version is still readable, retaining its former target
        legacy = _refs(store.relations(include_unresolved=True),
                       anchor="unit", kind=REFERENCE)
        self.assertEqual(len(legacy), 1)
        self.assertFalse(legacy[0].target_resolved)
        self.assertEqual(legacy[0].target_ref, old_ref)
        self.assertIsNone(store.anchor("unit"))
        self.assertIsNotNone(store.anchor("unit-v2"))

        # restoring the anchor brings the association back
        changes = store.ingest_block(UNIT_HTML, "unit", (1,))
        added = [change for change in changes
                 if change.relation.anchor == "unit"
                 and change.relation.kind == REFERENCE]
        self.assertEqual([change.action for change in added], [ADDED])
        self.assertTrue(
            _refs(store.relations(), anchor="unit", kind=REFERENCE)[0]
            .target_resolved
        )

    def test_cross_block_cycle_keeps_real_edges_regardless_of_order(self):
        first = ('<div id="x" itemscope itemtype="http://schema.org/Thing" '
                 'itemref="y"></div>')
        second = ('<div id="y" itemscope itemtype="http://schema.org/Thing" '
                  'itemref="x"></div>')

        def edges(order):
            store = BlockRelationStore(base_url=BASE_URL)
            store.add_skeleton("b1", (0,))
            store.add_skeleton("b2", (1,))
            for block_id, html, path in order:
                store.ingest_block(html, block_id, path)
            return sorted(
                (rel.source.element_id, rel.anchor)
                for rel in store.relations()
                if rel.kind == REFERENCE and rel.anchor in {"x", "y"}
            )

        forward = edges([("b1", first, (0,)), ("b2", second, (1,))])
        reverse = edges([("b2", second, (1,)), ("b1", first, (0,))])
        self.assertEqual(forward, reverse)
        self.assertEqual(forward, [("x", "y"), ("y", "x")])


class TestFullVerification(unittest.TestCase):
    def test_assembled_document_matches_existing_extract_path(self):
        store = BlockRelationStore(base_url=BASE_URL)
        store.add_skeleton("specs", (0,))
        store.add_skeleton("recs", (1,))
        store.add_skeleton("unit", (2,))
        store.ingest_block(SPECS_HTML, "specs", (0,))
        store.ingest_block(RECS_HTML, "recs", (1,))
        store.ingest_block(UNIT_HTML, "unit", (2,))

        incremental = store.verify_with_extract(syntaxes=["microdata"])
        reference = extruct.extract(
            store.assemble_html(), base_url=BASE_URL, syntaxes=["microdata"]
        )
        self.assertEqual(incremental, reference)
        # the assembled document genuinely contains every block
        for anchor in ("specs", "recs", "unit"):
            self.assertIn(f'id="{anchor}"', store.assemble_html())

    def test_verify_requires_all_blocks(self):
        store = BlockRelationStore(base_url=BASE_URL)
        store.add_skeleton("specs", (0,))
        with self.assertRaises(RuntimeError):
            store.verify_with_extract()


class TestAnchorDefinitions(unittest.TestCase):
    def test_anchor_definition_relation_and_lookup(self):
        store = BlockRelationStore(base_url=BASE_URL)
        store.add_skeleton("unit", (0,))
        store.ingest_block(UNIT_HTML, "unit", (0,))
        definition = _refs(store.relations(), kind=ANCHOR_DEF)
        self.assertEqual(len(definition), 1)
        self.assertEqual(definition[0].anchor, "unit")
        node = store.anchor("unit")
        self.assertIsNotNone(node)
        self.assertEqual(node.element_id, "unit")
        self.assertEqual(node.block.block_id, "unit")


if __name__ == "__main__":
    unittest.main()
