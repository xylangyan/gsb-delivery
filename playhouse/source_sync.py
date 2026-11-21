"""
Source-aware reconciliation for custom many-to-many through tables.

Several suppliers (and a human curator) each publish, every day, the complete
list of goods that may be stocked in a given warehouse. Each publication is a
snapshot carrying the source's own monotonically increasing revision number.

This module keeps one shared warehouse-to-good relationship per pair, while
remembering every source that currently maintains that relationship together
with that source's quote. It handles the realities of a message queue:

* files may arrive late or be redelivered (out-of-order / duplicate revisions);
* a source's data block may be missing for a given day (missing is *not* an
  empty list);
* a revision number may be reused with conflicting content.

The design stores, per (warehouse, source), the last *accepted* revision plus a
fingerprint of its payload. Rejected and redelivered files therefore never
mutate any relationship, and the previous payload is always available as the
reference that makes a rejection explainable -- even when that accepted
revision was an explicit empty snapshot.

Only the three tables below are managed here. They are ordinary peewee models,
so the usual many-to-many ``add()`` / ``remove()`` / ``clear()`` queries keep
working against the relationship table.
"""
import hashlib
import json
import operator
from functools import reduce

from peewee import (
    AutoField,
    BigIntegerField,
    BooleanField,
    CharField,
    CompositeKey,
    ForeignKeyField,
    Model,
    TextField,
)


class SourceSyncError(Exception):
    """Base class for source-sync failures; nothing was written."""


class StaleRevisionError(SourceSyncError):
    """A revision older than the accepted one was delivered."""

    def __init__(self, source, revision, current, current_fingerprint=None):
        self.source = source
        self.revision = revision
        self.current_revision = current
        self.current_fingerprint = current_fingerprint
        super(StaleRevisionError, self).__init__(
            'source %r: incoming revision %s is older than accepted '
            'revision %s' % (source, revision, current))


class RevisionConflictError(SourceSyncError):
    """The same revision number was redelivered with different content."""

    def __init__(self, source, revision, incoming_fingerprint,
                 current_fingerprint):
        self.source = source
        self.revision = revision
        self.incoming_fingerprint = incoming_fingerprint
        self.current_fingerprint = current_fingerprint
        super(RevisionConflictError, self).__init__(
            'source %r: revision %s content mismatch: incoming %s vs '
            'accepted %s' % (source, revision, incoming_fingerprint,
                             current_fingerprint))


class DuplicateQuoteError(SourceSyncError):
    """A single payload lists the same good twice with different quotes."""

    def __init__(self, source, revision, good, quotes):
        self.source = source
        self.revision = revision
        self.good = good
        self.quotes = quotes
        super(DuplicateQuoteError, self).__init__(
            'source %r: revision %s lists good %r with conflicting '
            'quotes %r' % (source, revision, good, quotes))


class SourceRevision(Model):
    """
    The last accepted snapshot of one source for one warehouse.

    A row only exists once a snapshot has been accepted. Its fingerprint lets
    an exact redelivery be detected (idempotency) and lets a same-revision
    payload with different content be rejected with a verifiable reason. An
    accepted empty snapshot is stored exactly like any other revision, so it
    keeps serving as the rejection reference for late older files.
    """
    warehouse = None  # Bound to the warehouse model by SourceSync.bind().
    source = CharField()
    revision = BigIntegerField()
    fingerprint = CharField(max_length=64)
    is_empty = BooleanField()

    class Meta:
        table_name = 'source_revision'
        primary_key = CompositeKey('warehouse', 'source')


class WarehouseGood(Model):
    """
    The shared warehouse-to-good relationship: one row per pair.

    The surrogate ``id`` is the stable relationship identity. It never changes
    as sources come and go: rows are created when the first source maintains
    the pair and deleted only when the last source (including the human
    curator) drops it. The ``(warehouse, good)`` pair stays unique.
    """
    id = AutoField()
    warehouse = None  # Bound by SourceSync.bind().
    good = None       # Bound by SourceSync.bind().

    class Meta:
        table_name = 'warehouse_good'


class GoodSource(Model):
    """
    One source's current claim on a warehouse/good pair, with its quote.

    The human curator is modelled as just another source, additionally allowed
    to store a free-text note on the claim.
    """
    relationship = None  # Bound by SourceSync.bind().
    source = CharField()
    quote = TextField(null=True)
    note = TextField(null=True)
    revision = BigIntegerField()

    class Meta:
        table_name = 'good_source'
        primary_key = CompositeKey('relationship', 'source')


def _default_fingerprint(payload):
    """
    Stable content hash of a snapshot payload.

    The payload is normalised first so that dict ordering does not produce
    different fingerprints for the same logical content.
    """
    normalised = sorted((str(key), str(quote))
                        for key, quote in payload.items())
    blob = json.dumps(normalised, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


class SourceSync(object):
    """
    Reconcile per-source complete snapshots onto shared M2M relationships.

    :param Model warehouse_model: the "left" model, e.g. a warehouse.
    :param Model good_model: the "right" model, e.g. an inbound good.
    :param str source_field: field on ``warehouse_model`` holding its key.
    :param str good_field: field on ``good_model`` holding its key.
    :param str manual_source: source name reserved for the human curator.
    :param callable fingerprint_func: maps a payload dict to a stable string.
    :param Database database: database the generated tables are bound to.

    The three managed tables are exposed as :attr:`revision_model`,
    :attr:`relationship_model` and :attr:`claim_model`.
    """

    def __init__(self, warehouse_model, good_model, source_field='id',
                 good_field='id', manual_source='manual',
                 fingerprint_func=None, database=None):
        self.warehouse_model = warehouse_model
        self.good_model = good_model
        self.source_field = source_field
        self.good_field = good_field
        self.manual_source = manual_source
        self.fingerprint = fingerprint_func or _default_fingerprint
        self.database = database or warehouse_model._meta.database

        self.revision_model = self._build_revision_model()
        self.relationship_model = self._build_relationship_model()
        self.claim_model = self._build_claim_model()

    # ------------------------------------------------------------------ models

    def _build_revision_model(self):
        wm = self.warehouse_model
        sync = self

        class SourceRevisionBound(SourceRevision):
            warehouse = ForeignKeyField(
                wm, field=getattr(wm, sync.source_field),
                column_name='warehouse', backref='+')

            class Meta:
                database = sync.database
                table_name = SourceRevision._meta.table_name
                primary_key = CompositeKey('warehouse', 'source')

        SourceRevisionBound.__name__ = 'SourceRevision'
        return SourceRevisionBound

    def _build_relationship_model(self):
        wm = self.warehouse_model
        gm = self.good_model
        sync = self

        class WarehouseGoodBound(WarehouseGood):
            warehouse = ForeignKeyField(
                wm, field=getattr(wm, sync.source_field),
                column_name='warehouse', backref='+')
            good = ForeignKeyField(
                gm, field=getattr(gm, sync.good_field),
                column_name='good', backref='+')

            class Meta:
                database = sync.database
                table_name = WarehouseGood._meta.table_name
                indexes = ((('warehouse', 'good'), True),)

        WarehouseGoodBound.__name__ = 'WarehouseGood'
        return WarehouseGoodBound

    def _build_claim_model(self):
        rm = self.relationship_model
        sync = self

        class GoodSourceBound(GoodSource):
            relationship = ForeignKeyField(
                rm, column_name='relationship_id', backref='claims')

            class Meta:
                database = sync.database
                table_name = GoodSource._meta.table_name
                primary_key = CompositeKey('relationship', 'source')

        GoodSourceBound.__name__ = 'GoodSource'
        return GoodSourceBound

    @property
    def models(self):
        """All managed tables, in creation order."""
        return [self.revision_model, self.relationship_model,
                self.claim_model]

    def create_tables(self, safe=True):
        self.database.create_tables(self.models, safe=safe)

    def drop_tables(self, safe=True):
        self.database.drop_tables(self.models, safe=safe)

    # ------------------------------------------------------------- normalising

    def _normalise_payload(self, items, source=None, revision=None):
        """
        Turn accepted payload forms into an ordered ``{good_key: quote}`` dict.

        Each entry may be a ``(good, quote)`` tuple, a model instance (its key
        field and ``quote`` attribute are used), or a mapping with
        ``good``/``quote`` keys. Duplicate goods carrying different quotes are
        rejected up front so an import can never apply partially.
        """
        payload = {}
        for item in items or ():
            if isinstance(item, dict):
                good_key = item['good']
                quote = item['quote']
            elif isinstance(item, (tuple, list)):
                good_key, quote = item
            elif hasattr(item, self.good_field) and hasattr(item, 'quote'):
                good_key = getattr(item, self.good_field)
                quote = item.quote
            else:
                raise TypeError(
                    'unsupported payload entry %r; expected (good, quote), '
                    'dict or object with %r and "quote" attributes'
                    % (item, self.good_field))

            good_key = getattr(good_key, self.good_field, good_key)
            quote = str(quote)
            if good_key in payload and payload[good_key] != quote:
                raise DuplicateQuoteError(
                    source, revision, good_key, [payload[good_key], quote])
            payload[good_key] = quote
        return payload

    def _key(self, obj, field):
        return getattr(obj, field, obj)

    # --------------------------------------------------------------- revisions

    def accepted_revision(self, warehouse, source):
        """Return the accepted revision number, or ``None`` if never seen."""
        state = self.get_revision(warehouse, source)
        return None if state is None else state.revision

    def get_revision(self, warehouse, source):
        """Return the full accepted revision row, or ``None``."""
        warehouse_key = self._key(warehouse, self.source_field)
        return self.revision_model.get_or_none(
            self.revision_model.warehouse == warehouse_key,
            self.revision_model.source == source)

    def revisions(self, warehouse=None):
        """Select accepted revision rows, optionally for one warehouse."""
        query = self.revision_model.select()
        if warehouse is not None:
            warehouse_key = self._key(warehouse, self.source_field)
            query = query.where(self.revision_model.warehouse ==
                                warehouse_key)
        return query.order_by(self.revision_model.source)

    # ------------------------------------------------------------------ claims

    def get_claim(self, warehouse, good, source):
        """Fetch one source claim for a pair, or ``None``."""
        good_key = self._key(good, self.good_field)
        warehouse_key = self._key(warehouse, self.source_field)
        rm = self.relationship_model
        return (self.claim_model
                .select()
                .join(rm)
                .where(
                    rm.warehouse == warehouse_key,
                    rm.good == good_key,
                    self.claim_model.source == source)
                .first())

    def get_quotes(self, warehouse, good):
        """Return ``{source: quote}`` for every source maintaining a pair."""
        good_key = self._key(good, self.good_field)
        warehouse_key = self._key(warehouse, self.source_field)
        rm = self.relationship_model
        rows = (self.claim_model
                .select(self.claim_model.source, self.claim_model.quote)
                .join(rm)
                .where(
                    rm.warehouse == warehouse_key,
                    rm.good == good_key))
        return {row.source: row.quote for row in rows}

    def relationships(self, warehouse=None):
        """Select shared relationships, optionally for one warehouse."""
        query = self.relationship_model.select()
        if warehouse is not None:
            warehouse_key = self._key(warehouse, self.source_field)
            query = query.where(self.relationship_model.warehouse ==
                                warehouse_key)
        return query

    # -------------------------------------------------------------- manual API

    def _get_or_create_relationship(self, warehouse_key, good_key):
        """Fetch the shared relationship, creating it without racing retries."""
        rm = self.relationship_model
        (rm.insert(warehouse=warehouse_key, good=good_key)
         .on_conflict_ignore().execute())
        return rm.get(rm.warehouse == warehouse_key, rm.good == good_key)

    def manual_upsert(self, warehouse, good, quote=None, note=None):
        """
        Create or refresh the human curator's claim on a pair.

        The shared relationship is created on first use and keeps its identity
        afterwards. Repeated calls update the quote / note in place.
        """
        good_key = self._key(good, self.good_field)
        warehouse_key = self._key(warehouse, self.source_field)
        cm = self.claim_model
        with self.database.atomic():
            rel = self._get_or_create_relationship(warehouse_key, good_key)
            cm.insert(
                relationship=rel,
                source=self.manual_source,
                quote=None if quote is None else str(quote),
                note=note,
                revision=0,
            ).on_conflict(
                conflict_target=[cm.relationship, cm.source],
                update={
                    cm.quote: None if quote is None else str(quote),
                    cm.note: note,
                }).execute()

    def manual_remove(self, warehouse, good):
        """Drop the curator's claim; the relationship is pruned if orphaned."""
        with self.database.atomic():
            claim = self.get_claim(warehouse, good, self.manual_source)
            if claim is None:
                return False
            return self._delete_claims([claim])

    # --------------------------------------------------------------- main sync

    def apply_snapshot(self, warehouse, source, revision, items):
        """
        Apply one complete snapshot from ``source`` for a warehouse.

        :returns: ``True`` if the snapshot was applied, ``False`` if it was an
            exact redelivery of the already-accepted revision (no writes).
        :raises StaleRevisionError: ``revision`` is below the accepted one.
        :raises RevisionConflictError: same revision, different payload.
        :raises DuplicateQuoteError: the payload itself is internally
            inconsistent.

        Validation runs before any write and the whole reconciliation is one
        transaction, so failures never leave partial adds or removals behind.
        """
        payload = self._normalise_payload(items, source, revision)
        fingerprint = self.fingerprint(payload)
        warehouse_key = self._key(warehouse, self.source_field)

        with self.database.atomic():
            state = self.revision_model.get_or_none(
                self.revision_model.warehouse == warehouse_key,
                self.revision_model.source == source)

            if state is not None:
                if revision < state.revision:
                    raise StaleRevisionError(
                        source, revision, state.revision, state.fingerprint)
                if revision == state.revision:
                    if fingerprint == state.fingerprint:
                        # Exact redelivery: idempotent no-op.
                        return False
                    raise RevisionConflictError(
                        source, revision, fingerprint, state.fingerprint)

            self._reconcile(warehouse_key, source, revision, payload)

            self.revision_model.insert(
                warehouse=warehouse_key,
                source=source,
                revision=revision,
                fingerprint=fingerprint,
                is_empty=not payload,
            ).on_conflict(
                conflict_target=[self.revision_model.warehouse,
                                 self.revision_model.source],
                update={
                    self.revision_model.revision: revision,
                    self.revision_model.fingerprint: fingerprint,
                    self.revision_model.is_empty: not payload,
                }).execute()
            return True

    def _reconcile(self, warehouse_key, source, revision, payload):
        rm = self.relationship_model
        cm = self.claim_model

        existing = {row.good_id: row
                    for row in rm.select().where(rm.warehouse ==
                                                 warehouse_key)}

        for good_key, quote in payload.items():
            rel = existing.get(good_key)
            if rel is None:
                rel = self._get_or_create_relationship(
                    warehouse_key, good_key)
            cm.insert(
                relationship=rel,
                source=source,
                quote=quote,
                note=None,
                revision=revision,
            ).on_conflict(
                conflict_target=[cm.relationship, cm.source],
                update={cm.quote: quote, cm.revision: revision}
            ).execute()

        stale_query = (cm.select()
                       .join(rm)
                       .where(rm.warehouse == warehouse_key,
                              cm.source == source))
        if payload:
            stale_query = stale_query.where(rm.good.not_in(list(payload)))
        stale = list(stale_query)
        if stale:
            self._delete_claims(stale)

    def _delete_claims(self, claims):
        """Delete claims and prune any relationship left without a source."""
        cm = self.claim_model
        rm = self.relationship_model

        conditions = [
            (cm.relationship == claim.relationship_id) &
            (cm.source == claim.source)
            for claim in claims]
        deleted = (cm.delete()
                   .where(reduce(operator.or_, conditions))
                   .execute())

        relationship_ids = {claim.relationship_id for claim in claims}
        orphaned = [rel_id for rel_id in relationship_ids
                    if not cm.select().where(
                        cm.relationship == rel_id).exists()]
        if orphaned:
            rm.delete().where(rm.id << orphaned).execute()
        return bool(deleted)
