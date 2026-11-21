#!/usr/bin/env python
"""
Source-aware snapshot reconciliation example.

Two suppliers (jia, yi) and a human curator each publish a complete list of
the goods a warehouse may stock, tagged with their own increasing revision
number. Messages may be redelivered, arrive late, go missing, or reuse a
revision number with conflicting content.

Run with: python examples/source_sync.py
"""
from peewee import *
from playhouse.source_sync import (
    RevisionConflictError,
    SourceSync,
    StaleRevisionError,
)

db = SqliteDatabase(':memory:')


class Warehouse(Model):
    code = CharField(primary_key=True)

    class Meta:
        database = db


class Good(Model):
    sku = CharField(primary_key=True)

    class Meta:
        database = db


sync = SourceSync(Warehouse, Good, source_field='code', good_field='sku')


def show(title):
    goods = sorted(r.good_id for r in sync.relationships(warehouse))
    print('%-44s %s' % (title, goods))


db.connect()
db.create_tables([Warehouse, Good])
sync.create_tables()
warehouse = Warehouse.create(code='W')
for sku in ('A', 'B', 'C', 'D', 'E'):
    Good.create(sku=sku)

# Initial state: A is held by both suppliers, B by jia, C by yi, D manually.
sync.apply_snapshot(warehouse, 'jia', 7, [('A', '1.00'), ('B', '2.00')])
sync.apply_snapshot(warehouse, 'yi', 7, [('A', '1.10'), ('C', '3.00')])
sync.manual_upsert(warehouse, 'D', quote='9.00', note='curated')
show('initial (A jia+yi, B jia, C yi, D manual)')

# Jia rev 8: keep A with a new quote, drop B, add E. Yi stays on its own rev.
sync.apply_snapshot(warehouse, 'jia', 8, [('A', '1.50'), ('E', '5.00')])
sync.apply_snapshot(warehouse, 'yi', 8, [('A', '1.10'), ('C', '3.30')])
show('jia 8 (A repriced, B dropped, E added), yi 8')

# Jia rev 9 is an explicit empty snapshot: all jia claims disappear.
sync.apply_snapshot(warehouse, 'jia', 9, [])
show('jia 9 explicit empty list')

# Late delivery of jia rev 8: rejected, B and E are not resurrected.
try:
    sync.apply_snapshot(warehouse, 'jia', 8,
                        [('A', '1.50'), ('E', '5.00')])
except StaleRevisionError as exc:
    print('rejected late jia 8: accepted=%s fingerprint=%s...'
          % (exc.current_revision, exc.current_fingerprint[:12]))
show('after late jia 8 (unchanged)')

# A batch that only contains yi: a missing jia block is NOT an empty list.
sync.apply_snapshot(warehouse, 'yi', 9, [('A', '1.10'), ('C', '3.99')])
show("batch with yi only (jia's absence changes nothing)")

# Exact redelivery of jia 9 is an idempotent no-op.
print('redeliver identical jia 9 -> applied=%s'
      % sync.apply_snapshot(warehouse, 'jia', 9, []))

# Same revision number, different content: conflict, rejected wholesale.
try:
    sync.apply_snapshot(warehouse, 'jia', 9, [('A', '1.50')])
except RevisionConflictError as exc:
    print('conflict on jia 9: incoming %s... != accepted %s...'
          % (exc.incoming_fingerprint[:12],
             exc.current_fingerprint[:12]))
show('after conflicting jia 9 (unchanged)')

# The shared relationship and the manual note keep their identity throughout.
claim = sync.get_claim(warehouse, 'D', 'manual')
print('manual D: relationship=%s quote=%s note=%s'
      % (claim.relationship_id, claim.quote, claim.note))
print('per-source quotes for A:', sync.get_quotes(warehouse, 'A'))

db.close()
