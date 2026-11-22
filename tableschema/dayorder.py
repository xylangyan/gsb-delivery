# -*- coding: utf-8 -*-
"""Reconcile day/month date orders across sites from shared bill numbers.

Each site promises a single day/month order for its whole batch.  When the
same bill number occurs at two sites, both observations must settle to the
same calendar day.  This couples the sites: their orders are either the same
or opposite.  An ISO date, or an unambiguous numeric date, is an absolute
witness for the bill it belongs to.

The ledger is incremental.  Evidence documents can be committed repeatedly
(idempotent), revoked, and committed again under a new identifier.  Only
compact row evidence and declarations are retained, so publishing never
re-reads the original rows and never enumerates site-order combinations.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import io
import re
import json
import six
import datetime
from collections import deque
from collections import OrderedDict


# Module API

class DayOrderLedger(object):
    """Incremental evidence ledger for day/month order reconciliation.

    # Arguments
        date_column (str): business name of the shipment date column
        quantity_columns (str[]): business names treated as numeric quantities
    """

    # Public

    DMY = 'DMY'
    MDY = 'MDY'
    ORDERS = (DMY, MDY)

    def __init__(self, date_column='date', quantity_columns=None):
        self.__date_column = six.text_type(date_column)
        self.__quantity_columns = [
            six.text_type(item)
            for item in (quantity_columns or ['quantity', 'qty', 'count'])]
        self.__explicit = {}
        self.__evidence = OrderedDict()
        self.__revoked = OrderedDict()
        self.__version = 0
        self.__snapshot = None

    @property
    def date_column(self):
        """Business name of the shipment date column."""
        return self.__date_column

    @property
    def descriptor(self):
        """The published shared Table Schema descriptor."""
        return self.publish()['descriptor']

    # Evidence lifecycle

    def declare_site(self, site_id, columns=None, sample=None,
                     missing_values=None):
        """Declare a site's sample limit, business columns and missing marks.

        Declarations are metadata, not evidence.  Re-declaring a site replaces
        its previous declaration.
        """
        self.__explicit[six.text_type(site_id)] = {
            'columns': [six.text_type(item) for item in (columns or [])],
            'sample': int(sample) if sample is not None else None,
            'missingValues': [six.text_type(item)
                              for item in (missing_values or [])],
        }
        self.__changed()

    def commit(self, evidence):
        """Commit one evidence document or a list of documents.

        A document has:

        - `id`: stable evidence identifier
        - `rows`: observations with `site`, `bill` and a date value
        - optional `headers`: maps row arrays to names before use
        - optional `sites`: declarations with `id`, `columns`, `sample` and
          `missingValues`
        - optional `site`, `columns`, `sample` and `missingValues` for a
          single-site document

        Committing a committed identifier again is a no-op.  Distinct
        identifiers are distinct evidence, even with identical content.  A
        revoked identifier is never revived implicitly; use `recommit` with a
        new identifier to submit its content again.
        """
        if isinstance(evidence, (list, tuple)):
            return self.commit_all(evidence)
        document = _load(evidence)
        evidence_id = document.get('id')
        if evidence_id is None:
            raise ValueError('Evidence requires an "id"')
        evidence_id = six.text_type(evidence_id)
        if evidence_id in self.__evidence or evidence_id in self.__revoked:
            return False
        date_column = six.text_type(
            document.get('dateColumn', self.__date_column))
        headers = document.get('headers')
        document_site = document.get('site')
        document_missing = [
            six.text_type(item) for item in document.get(
                'missingValues', document.get('missing_values', []))]
        document_columns = [
            six.text_type(item) for item in document.get(
                'columns', document.get('fields', []))]

        declarations = _declarations(document)
        if document_site is not None:
            site_id = six.text_type(document_site)
            metadata = declarations.setdefault(site_id, {
                'columns': [],
                'sample': _sample(document),
                'missingValues': [],
            })
            if document_columns and not metadata['columns']:
                metadata['columns'] = list(document_columns)
            for item in document_missing:
                if item not in metadata['missingValues']:
                    metadata['missingValues'].append(item)

        fallback_site = None
        if not declarations and len(self.__explicit) == 1:
            # A single-site ledger lets rows omit the redundant `site` key.
            fallback_site = next(iter(self.__explicit))

        rows = []
        counters = {}
        for position, raw in enumerate(document.get('rows', []), start=1):
            if isinstance(raw, list):
                if headers is None:
                    continue
                raw = dict(zip(headers, raw))
            if not isinstance(raw, dict):
                continue
            raw = dict(raw)
            site = raw.get('site', document_site)
            if site is None:
                site = fallback_site
            if site is None:
                continue
            site = six.text_type(site)
            index = raw.get('index', raw.get('row'))
            if index is None:
                counters[site] = counters.get(site, 0) + 1
                index = counters[site]
            else:
                try:
                    index = int(index)
                except (TypeError, ValueError):
                    index = position
            missing = list(document_missing)
            missing.extend(declarations.get(site, {}).get(
                'missingValues', []))
            explicit = self.__explicit.get(site)
            if explicit:
                missing.extend(explicit['missingValues'])

            observation = None
            bill = _first(raw, _BILL_NAMES)
            date_value = _first(raw, _DATE_NAMES + (date_column,))
            if bill is not None and date_value is not None:
                text = six.text_type(date_value)
                if text not in missing:
                    observation = _classify(
                        text, site, six.text_type(bill), evidence_id, index)

            values = []
            for column, value in raw.items():
                column = six.text_type(column)
                if column in _RESERVED or column == date_column:
                    continue
                if value is None or value == '':
                    continue
                if six.text_type(value) in missing:
                    continue
                values.append((column, _value_kind(value)))

            rows.append(_Row(site, index, observation, values))

        self.__evidence[evidence_id] = _Evidence(
            evidence_id, document, rows, declarations)
        self.__changed()
        return True

    def commit_all(self, evidences):
        """Commit several evidence documents, returning changed identifiers."""
        changed = []
        for evidence in evidences:
            document = _load(evidence)
            if self.commit(document):
                changed.append(six.text_type(document.get('id')))
        return changed

    def revoke(self, evidence_id):
        """Revoke one committed evidence document by identifier."""
        evidence_id = six.text_type(evidence_id)
        if evidence_id not in self.__evidence:
            return False
        evidence = self.__evidence.pop(evidence_id)
        self.__revoked[evidence_id] = evidence
        self.__changed()
        return True

    def recommit(self, evidence, new_id=None):
        """Commit revoked evidence again under a fresh identifier.

        `evidence` may be the revoked identifier or the document content.
        """
        if isinstance(evidence, six.string_types) and \
                evidence in self.__revoked:
            record = self.__revoked[evidence]
            document = dict(record.document)
            old_id = six.text_type(evidence)
        else:
            document = dict(_load(evidence))
            old_id = six.text_type(document.get('id', 'evidence'))
        if new_id is None:
            new_id = old_id
            suffix = 1
            while new_id in self.__evidence or new_id in self.__revoked:
                new_id = '%s#%s' % (old_id, suffix)
                suffix += 1
        document['id'] = six.text_type(new_id)
        return self.commit(document)

    # Aliases for callers using lifecycle vocabulary

    submit = commit
    submit_all = commit_all
    withdraw = revoke
    add_evidence = commit
    remove_evidence = revoke
    commit_evidence = commit
    revoke_evidence = revoke

    # Statistics

    def statistics(self):
        """Return per-site declared samples, columns and missing values."""
        snapshot = self.__get_snapshot()
        sites = OrderedDict()
        for site_id in sorted(snapshot.sites):
            declaration = snapshot.sites[site_id]
            sample = declaration['sample']
            if sample is None:
                sample = len(snapshot.rows_by_site.get(site_id, []))
            sites[site_id] = {
                'samples': sample,
                'columns': list(declaration['columns']),
                'missingValues': list(declaration['missingValues']),
            }
        return {
            'sites': sites,
            'evidence': {
                'committed': sorted(self.__evidence),
                'revoked': sorted(self.__revoked),
            },
        }

    # Publication

    def publish(self, columns=None):
        """Publish readable formats and per-site column projections.

        The result is deterministic and independent of evidence or site
        delivery order.  A site whose order is not uniquely determined gets no
        date field.  Independently decidable columns remain readable.
        """
        snapshot = self.__get_snapshot()
        solution, conflicts = _solve(snapshot)
        contract = [six.text_type(item) for item in columns] \
            if columns is not None else list(snapshot.columns)

        sites = OrderedDict()
        formats = OrderedDict()
        for site_id in sorted(snapshot.sites):
            declaration = snapshot.sites[site_id]
            order = solution.get(site_id)
            if order is not None:
                date_format = _ORDER_FORMATS[order]
            elif _iso_only(snapshot, site_id):
                # Every observed value is ISO, so the date column is readable
                # regardless of the site's day/month order.
                date_format = 'default'
            else:
                date_format = None
            formats[site_id] = date_format

            fields = []
            enabled = []
            for name in contract:
                if name not in declaration['columns']:
                    continue
                if name == self.__date_column:
                    if date_format is not None:
                        enabled.append(name)
                        fields.append({
                            'name': name,
                            'type': 'date',
                            'format': date_format,
                        })
                    continue
                enabled.append(name)
                fields.append({
                    'name': name,
                    'type': _column_type(snapshot, name),
                    'format': 'default',
                })
            sites[site_id] = {
                'format': order,
                'dateFormat': date_format,
                'columns': enabled,
                'fields': fields,
            }

        date_formats = set(
            value for value in formats.values() if value is not None)
        fields = []
        for name in contract:
            if name == self.__date_column:
                if len(date_formats) == 1:
                    fields.append({
                        'name': name,
                        'type': 'date',
                        'format': next(iter(date_formats)),
                    })
                continue
            fields.append({
                'name': name,
                'type': _column_type(snapshot, name),
                'format': 'default',
            })

        unresolved = sorted(
            site_id for site_id in snapshot.sites
            if formats.get(site_id) is None)
        return {
            'fields': fields,
            'columns': contract,
            'sites': sites,
            'formats': formats,
            'unresolved': unresolved,
            'missingValues': list(snapshot.missing_values),
            'descriptor': {
                'fields': fields,
                'missingValues': list(snapshot.missing_values),
            },
            'conflicts': conflicts,
        }

    def conflict(self):
        """Return the minimal bill/site certificate for a contradiction.

        `None` is returned when the committed evidence is consistent.  The
        certificate names the fewest distinct bills and sites whose joint
        observations cannot settle to one calendar day.
        """
        snapshot = self.__get_snapshot()
        _, conflicts = _solve(snapshot)
        return conflicts[0] if conflicts else None

    def candidates(self):
        """Return possible orders per site, including unresolved sites."""
        snapshot = self.__get_snapshot()
        solution, conflicts = _solve(snapshot)
        conflicted = set()
        for certificate in conflicts:
            conflicted.update(certificate['sites'])
        result = OrderedDict()
        for site_id in sorted(snapshot.sites):
            if site_id in conflicted:
                result[site_id] = list(self.ORDERS)
            elif site_id in solution:
                result[site_id] = [solution[site_id]]
            elif _iso_only(snapshot, site_id):
                result[site_id] = ['default']
            else:
                result[site_id] = list(self.ORDERS)
        return result

    # Internal

    def __changed(self):
        self.__version += 1
        self.__snapshot = None

    def __get_snapshot(self):
        if self.__snapshot is None or \
                self.__snapshot.version != self.__version:
            self.__snapshot = _snapshot(
                self.__evidence, self.__revoked, self.__explicit,
                self.__date_column, self.__quantity_columns,
                self.__version)
        return self.__snapshot


# Internal

_GROUND = '\x00ground'
_ISO_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$')
_YMD_RE = re.compile(r'^(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})$')
_NUM_RE = re.compile(
    r'^\s*(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4}|\d{2})\s*$')
_BILL_NAMES = ('bill', 'billNumber', 'bill_number', 'billNo', 'bill_no',
               'bl', 'billOfLading', 'bill_of_lading')
_DATE_NAMES = ('date', 'shipDate', 'ship_date', 'shipmentDate',
               'shipment_date', 'shippingDate', 'shipping_date')
_RESERVED = set(_BILL_NAMES + _DATE_NAMES + ('site', 'index', 'row'))
_ORDER_FORMATS = {
    DayOrderLedger.DMY: '%d/%m/%Y',
    DayOrderLedger.MDY: '%m/%d/%Y',
}


class _Row(object):
    __slots__ = ('site', 'index', 'observation', 'values')

    def __init__(self, site, index, observation, values):
        self.site = site
        self.index = index
        self.observation = observation
        self.values = values


class _Evidence(object):
    def __init__(self, evidence_id, document, rows, declarations):
        self.id = evidence_id
        self.document = document
        self.rows = rows
        self.declarations = declarations


class _Observation(object):
    __slots__ = ('site', 'bill', 'kind', 'first', 'second', 'year', 'date',
                 'order', 'evidence', 'index', 'text')

    def __init__(self, site, bill, kind, first, second, year, date, order,
                 evidence, index, text):
        self.site = site
        self.bill = bill
        self.kind = kind
        self.first = first
        self.second = second
        self.year = year
        self.date = date
        self.order = order
        self.evidence = evidence
        self.index = index
        self.text = text

    def read(self, order):
        """Resolve this observation under a site order, or `None`."""
        if self.kind == 'ISO':
            return self.date
        if self.kind == 'CONST':
            return self.date if order == self.order else None
        if order == DayOrderLedger.DMY:
            return _make_date(self.first, self.second, self.year)
        return _make_date(self.second, self.first, self.year)


class _Edge(object):
    __slots__ = ('first', 'second', 'parity', 'bill', 'evidence',
                 'witness_sites', 'observations')

    def __init__(self, first, second, parity, bill, evidence,
                 witness_sites=None, observations=None):
        self.first = first
        self.second = second
        self.parity = parity
        self.bill = bill
        self.evidence = evidence
        self.witness_sites = list(witness_sites or [])
        self.observations = list(observations or [])


class _Snapshot(object):
    def __init__(self):
        self.version = None
        self.sites = OrderedDict()
        self.rows_by_site = {}
        self.observations = []
        self.observations_by_site = {}
        self.values = []
        self.columns = []
        self.missing_values = []
        self.quantity_columns = []


def _load(source):
    if isinstance(source, dict):
        return dict(source)
    if hasattr(source, 'read'):
        return json.load(source)
    with io.open(source, encoding='utf-8') as stream:
        return json.load(stream)


def _declarations(document):
    result = {}
    sites = document.get('sites', [])
    if isinstance(sites, dict):
        items = []
        for site_id in sorted(sites):
            metadata = dict(sites[site_id] or {})
            metadata.setdefault('id', site_id)
            items.append(metadata)
        sites = items
    for metadata in sites:
        if not isinstance(metadata, dict):
            continue
        site_id = metadata.get('id', metadata.get('site'))
        if site_id is None:
            continue
        result[six.text_type(site_id)] = {
            'columns': [six.text_type(item) for item in metadata.get(
                'columns', metadata.get('fields', []))],
            'sample': _sample(metadata),
            'missingValues': [six.text_type(item) for item in metadata.get(
                'missingValues', metadata.get('missing_values', []))],
        }
    return result


def _sample(metadata):
    for name in ('sample', 'samples', 'limit'):
        if metadata.get(name) is not None:
            try:
                return int(metadata[name])
            except (TypeError, ValueError):
                return None
    return None


def _first(row, names):
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _make_date(day, month, year):
    try:
        return datetime.date(int(year), int(month), int(day))
    except (TypeError, ValueError):
        return None


def _classify(text, site, bill, evidence, index):
    match = _ISO_RE.match(text)
    if match:
        value = _make_date(match.group(3), match.group(2), match.group(1))
        if value is None:
            return None
        return _Observation(
            site, bill, 'ISO', None, None, None, value, None,
            evidence, index, text)

    match = _YMD_RE.match(text)
    if match:
        value = _make_date(match.group(3), match.group(2), match.group(1))
        if value is None:
            return None
        return _Observation(
            site, bill, 'ISO', None, None, None, value, None,
            evidence, index, text)

    match = _NUM_RE.match(text)
    if not match:
        return None
    first = int(match.group(1))
    second = int(match.group(2))
    year = int(match.group(3))
    if len(match.group(3)) == 2:
        year += 2000
    if first > 12 and second > 12:
        return None
    if first > 12:
        value = _make_date(first, second, year)
        if value is None:
            return None
        return _Observation(
            site, bill, 'CONST', first, second, year, value,
            DayOrderLedger.DMY, evidence, index, text)
    if second > 12:
        value = _make_date(second, first, year)
        if value is None:
            return None
        return _Observation(
            site, bill, 'CONST', first, second, year, value,
            DayOrderLedger.MDY, evidence, index, text)
    if (_make_date(first, second, year) is None or
            _make_date(second, first, year) is None):
        return None
    return _Observation(
        site, bill, 'AMB', first, second, year, None, None,
        evidence, index, text)


def _value_kind(value):
    if isinstance(value, bool):
        return 'string'
    if isinstance(value, six.integer_types):
        return 'integer'
    if isinstance(value, float):
        return 'number'
    text = six.text_type(value).strip()
    try:
        int(text)
        return 'integer'
    except (TypeError, ValueError):
        pass
    try:
        float(text)
        return 'number'
    except (TypeError, ValueError):
        return 'string'


def _snapshot(evidences, revoked, explicit, date_column, quantity_columns,
              version):
    """Build one order-independent view of declarations and row evidence."""
    snapshot = _Snapshot()
    snapshot.version = version
    snapshot.quantity_columns = list(quantity_columns)

    collected = {}
    for site_id in explicit:
        collected[site_id] = {
            'columns': list(explicit[site_id]['columns']),
            'sample': explicit[site_id]['sample'],
            'missingValues': list(explicit[site_id]['missingValues']),
            'explicit': True,
        }
    for source in (evidences, revoked):
        for evidence_id in sorted(source):
            evidence = source[evidence_id]
            for site_id, metadata in evidence.declarations.items():
                current = collected.setdefault(site_id, {
                    'columns': [], 'sample': None, 'missingValues': [],
                    'explicit': False})
                if current.get('explicit'):
                    continue
                for item in metadata.get('columns', []):
                    item = six.text_type(item)
                    if item not in current['columns']:
                        current['columns'].append(item)
                sample = metadata.get('sample')
                if sample is not None:
                    current['sample'] = max(
                        current['sample'] or 0, int(sample))
                for item in metadata.get('missingValues', []):
                    item = six.text_type(item)
                    if item not in current['missingValues']:
                        current['missingValues'].append(item)
    for evidence_id in sorted(evidences):
        evidence = evidences[evidence_id]
        for row in evidence.rows:
            current = collected.setdefault(row.site, {
                'columns': [], 'sample': None, 'missingValues': [],
                'explicit': False})
            if row.observation is not None and \
                    date_column not in current['columns']:
                current['columns'].insert(0, date_column)
            for column, _ in row.values:
                if column not in current['columns']:
                    current['columns'].append(column)
    for evidence_id in sorted(revoked):
        for row in revoked[evidence_id].rows:
            current = collected.setdefault(row.site, {
                'columns': [], 'sample': None, 'missingValues': [],
                'explicit': False})
            if row.observation is not None and \
                    date_column not in current['columns']:
                current['columns'].insert(0, date_column)
            for column, _ in row.values:
                if column not in current['columns']:
                    current['columns'].append(column)

    for site_id in sorted(collected):
        metadata = collected[site_id]
        snapshot.sites[site_id] = {
            'columns': list(metadata['columns']),
            'sample': metadata['sample'],
            'missingValues': list(metadata['missingValues']),
        }
        for item in metadata['missingValues']:
            if item not in snapshot.missing_values:
                snapshot.missing_values.append(item)

    grouped = {}
    for evidence_id in sorted(evidences):
        evidence = evidences[evidence_id]
        for row in evidence.rows:
            grouped.setdefault(row.site, []).append((evidence_id, row))

    for site_id in sorted(snapshot.sites):
        declaration = snapshot.sites[site_id]
        selected = _select_rows(
            grouped.get(site_id, []), declaration['sample'])
        snapshot.rows_by_site[site_id] = selected
        for row in selected:
            if row.observation is not None:
                snapshot.observations.append(row.observation)
                snapshot.observations_by_site.setdefault(
                    site_id, []).append(row.observation)
            for column, kind in row.values:
                snapshot.values.append((site_id, column, kind, row.index))

    columns = []
    for site_id in sorted(snapshot.sites):
        for name in snapshot.sites[site_id]['columns']:
            if name not in columns:
                columns.append(name)
    snapshot.columns = columns or [date_column]
    snapshot.missing_values = sorted(snapshot.missing_values)
    return snapshot


def _select_rows(rows, limit):
    """Select at most `limit` rows per site, independent of delivery order."""
    ordered = sorted(rows, key=lambda item: (
        item[1].index, item[1].site, _row_key(item[1]), item[0]))
    selected = [row for _, row in ordered]
    if limit is None:
        return selected
    limit = max(0, int(limit))
    kept = {}
    result = []
    for row in selected:
        count = kept.get(row.site, 0)
        if count >= limit:
            continue
        kept[row.site] = count + 1
        result.append(row)
    return result


def _row_key(row):
    observation = row.observation
    values = tuple(sorted(
        (column, kind) for column, kind in row.values))
    return (observation.text if observation else '',
            observation.bill if observation else '',
            values)


def _iso_only(snapshot, site_id):
    observations = snapshot.observations_by_site.get(site_id, [])
    return bool(observations) and all(
        item.kind == 'ISO' for item in observations)


def _column_type(snapshot, name):
    kinds = set(
        kind for site, column, kind, index in snapshot.values
        if column == name)
    if name in snapshot.quantity_columns and \
            (not kinds or kinds <= set(['integer', 'number'])):
        # A quantity holds integers and decimals in one numeric column; an
        # all-missing quantity stays numeric rather than becoming text.
        return 'number'
    if 'number' in kinds:
        return 'number'
    if kinds and kinds <= set(['integer']):
        return 'integer'
    return 'string'


def _same_site_orders(first, second):
    orders = []
    for order in DayOrderLedger.ORDERS:
        left = first.read(order)
        right = second.read(order)
        if left is not None and left == right:
            orders.append(order)
    return orders


def _cross_site_orders(first, second):
    allowed = []
    for left in DayOrderLedger.ORDERS:
        for right in DayOrderLedger.ORDERS:
            first_date = first.read(left)
            second_date = second.read(right)
            if first_date is not None and first_date == second_date:
                allowed.append((left, right))
    return allowed


def _parity(left, right):
    return 0 if left == right else 1


def _observation_key(observation):
    return (observation.site, observation.kind, observation.first or -1,
            observation.second or -1, observation.index,
            observation.text, observation.evidence)


def _add_anchor(anchors, site, order, bill, observation, sites=None,
                observations=None):
    anchors.setdefault(site, [])
    anchors[site].append({
        'site': site,
        'order': order,
        'bill': bill,
        'evidence': observation.evidence,
        'sites': sorted(set(sites or [site])),
        'observations': list(observations or [observation]),
    })


def _add_edge(edges, first, second, parity, bill, left, right):
    key = (min(first, second), max(first, second), parity)
    candidate = _Edge(first, second, parity, bill, left.evidence,
                      observations=[left, right])
    current = edges.get(key)
    if current is None or (candidate.bill, candidate.evidence) < \
            (current.bill, current.evidence):
        edges[key] = candidate


def _solve(snapshot):
    """Derive constraints, solve anchored parity components and certificates."""
    by_bill = OrderedDict()
    for observation in snapshot.observations:
        by_bill.setdefault(observation.bill, {}).setdefault(
            observation.site, []).append(observation)

    anchors = OrderedDict()
    edges = OrderedDict()
    internal = []
    for bill in sorted(by_bill):
        sites = by_bill[bill]
        for site_id in sorted(sites):
            observations = sorted(sites[site_id], key=_observation_key)
            for index, first in enumerate(observations):
                for second in observations[index + 1:]:
                    allowed = _same_site_orders(first, second)
                    if not allowed:
                        internal.append(_internal_certificate(
                            bill, [first, second]))
                    elif len(allowed) == 1:
                        _add_anchor(
                            anchors, site_id, allowed[0], bill, first,
                            [site_id], [first, second])
            # An ISO value alone carries no order information: it is the same
            # calendar day under either order, so it only witnesses an order
            # when it shares a bill with a numeric observation.
            for observation in observations:
                if observation.kind == 'CONST':
                    _add_anchor(
                        anchors, site_id, observation.order, bill,
                        observation, [site_id])

        site_ids = sorted(sites)
        for left_index, left_site in enumerate(site_ids):
            for right_site in site_ids[left_index + 1:]:
                for first in sorted(sites[left_site], key=_observation_key):
                    for second in sorted(
                            sites[right_site], key=_observation_key):
                        allowed = _cross_site_orders(first, second)
                        if not allowed:
                            internal.append(_internal_certificate(
                                bill, [first, second]))
                            continue
                        first_orders = set(item[0] for item in allowed)
                        second_orders = set(item[1] for item in allowed)
                        if len(first_orders) == 1:
                            _add_anchor(
                                anchors, left_site,
                                next(iter(first_orders)), bill, first,
                                [left_site, right_site], [first, second])
                        if len(second_orders) == 1:
                            _add_anchor(
                                anchors, right_site,
                                next(iter(second_orders)), bill, second,
                                [left_site, right_site], [first, second])
                        if len(first_orders) > 1 and \
                                len(second_orders) > 1:
                            parities = set(
                                _parity(item[0], item[1])
                                for item in allowed)
                            if len(parities) == 1:
                                _add_edge(
                                    edges, left_site, right_site,
                                    parities.pop(), bill, first, second)

    anchor_orders = {}
    for site_id in sorted(anchors):
        items = sorted(anchors[site_id], key=_anchor_key)
        values = set(item['order'] for item in items)
        anchor_orders[site_id] = values
        if len(values) > 1:
            internal.append(_anchor_conflict(site_id, items))

    graph_edges = list(edges.values())
    for site_id in sorted(anchor_orders):
        values = anchor_orders[site_id]
        if len(values) != 1:
            continue
        order = next(iter(values))
        witness = sorted(anchors[site_id], key=_anchor_key)[0]
        graph_edges.append(_Edge(
            _GROUND, site_id, _parity(DayOrderLedger.DMY, order),
            witness['bill'], witness['evidence'],
            witness_sites=witness['sites'],
            observations=witness['observations']))

    solution, cycle_conflicts = _solve_graph(
        set(snapshot.sites), graph_edges)
    conflicts = list(internal) + cycle_conflicts
    conflicts.sort(key=_certificate_key)
    if conflicts:
        component_of = _component_roots(graph_edges)
        blocked = set()
        for certificate in conflicts:
            for site in certificate['sites']:
                blocked.add(component_of.get(site, site))
        for site_id in list(solution):
            if component_of.get(site_id, site_id) in blocked:
                solution.pop(site_id)
    return solution, conflicts


def _anchor_key(anchor):
    return (anchor['bill'], anchor['order'], anchor['evidence'])


def _anchor_conflict(site_id, items):
    """Pick the two smallest anchors that fix opposite site orders."""
    chosen = []
    for order in DayOrderLedger.ORDERS:
        candidates = [item for item in items if item['order'] == order]
        if candidates:
            chosen.append(sorted(candidates, key=_anchor_key)[0])
    records = []
    sites = set([site_id])
    evidence = set()
    for item in chosen:
        evidence.add(item['evidence'])
        sites.update(item['sites'])
        for observation in item['observations']:
            record = _record(observation)
            if record not in records:
                records.append(record)
            evidence.add(observation.evidence)
    return {
        'bills': sorted(set(item['bill'] for item in chosen)),
        'sites': sorted(sites),
        'links': [],
        'anchors': [{'site': site_id, 'bill': item['bill'],
                     'order': item['order']} for item in chosen],
        'records': sorted(records, key=_record_key),
        'evidence': sorted(evidence),
    }


def _internal_certificate(bill, observations):
    records = []
    evidence = set()
    for observation in observations:
        record = _record(observation)
        if record not in records:
            records.append(record)
        evidence.add(observation.evidence)
    return {
        'bills': [bill],
        'sites': sorted(set(item.site for item in observations)),
        'links': [],
        'anchors': [],
        'records': sorted(records, key=_record_key),
        'evidence': sorted(evidence),
    }


def _record(observation):
    return {
        'site': observation.site,
        'bill': observation.bill,
        'date': observation.text,
        'evidence': observation.evidence,
    }


def _record_key(record):
    return (record['bill'], record['site'], record['date'],
            record['evidence'])


def _solve_graph(sites, edges):
    """Solve signed components and return a shortest odd-cycle certificate.

    Components are first labelled with one linear pass.  Only a component
    that fails consistency pays for a shortest-cycle search, so long
    consistent chains stay linear in the number of site/bill links.
    """
    adjacency = {}
    for site in sites:
        adjacency.setdefault(site, [])
    for edge in edges:
        adjacency.setdefault(edge.first, []).append(edge)
        adjacency.setdefault(edge.second, []).append(edge)

    solution = {}
    conflicts = []
    visited = set()
    for start in sorted(adjacency):
        if start in visited:
            continue
        component = []
        parity = {start: 0}
        queue = deque([start])
        visited.add(start)
        consistent = True
        while queue:
            node = queue.popleft()
            component.append(node)
            for edge in sorted(adjacency[node], key=_edge_sort_key):
                other = edge.second if edge.first == node else edge.first
                value = parity[node] ^ edge.parity
                if other not in parity:
                    parity[other] = value
                    visited.add(other)
                    queue.append(other)
                elif parity[other] != value:
                    consistent = False
        if not consistent:
            certificate = _shortest_odd_cycle(adjacency, component)
            if certificate is not None:
                conflicts.append(certificate)
            continue
        if _GROUND not in component:
            continue
        for node in component:
            if node == _GROUND:
                continue
            solution[node] = (
                DayOrderLedger.DMY if parity[node] == 0
                else DayOrderLedger.MDY)
    return solution, conflicts


def _component_roots(edges):
    """Map every node to the smallest site name in its connected component."""
    adjacency = {}
    for edge in edges:
        adjacency.setdefault(edge.first, []).append(edge)
        adjacency.setdefault(edge.second, []).append(edge)
    roots = {}
    for start in sorted(adjacency):
        if start in roots:
            continue
        group = []
        queue = deque([start])
        seen = set([start])
        while queue:
            node = queue.popleft()
            group.append(node)
            for edge in adjacency[node]:
                other = edge.second if edge.first == node else edge.first
                if other not in seen:
                    seen.add(other)
                    queue.append(other)
        root = min(group)
        for node in group:
            roots[node] = root
    return roots


def _shortest_odd_cycle(adjacency, component):
    """Return the shortest odd signed cycle in one component.

    For each edge, a parity-aware BFS searches for a path between its
    endpoints whose parity differs from the edge.  The smallest certificate
    by (distinct bills, distinct sites, bills, sites) wins.
    """
    nodes = set(component)
    seen = set()
    edges = []
    for node in sorted(nodes):
        for edge in adjacency[node]:
            key = (edge.first, edge.second, edge.parity, edge.bill,
                   edge.evidence)
            if key in seen:
                continue
            seen.add(key)
            edges.append(edge)

    best = None
    for closing in sorted(edges, key=_edge_sort_key):
        path = _parity_path(
            adjacency, closing.first, closing.second,
            1 - closing.parity, closing)
        if path is None:
            continue
        cycle_edges = _order_cycle(closing.first, path, closing)
        if cycle_edges is None:
            continue
        certificate = _certificate_from_edges(cycle_edges)
        if best is None or _certificate_key(certificate) < \
                _certificate_key(best):
            best = certificate
    return best


def _parity_path(adjacency, first, second, required, closing):
    """Shortest path from `first` to `second` with the required parity."""
    start = (first, 0)
    target = (second, required)
    if start == target:
        return []
    previous = {start: (None, None)}
    queue = deque([start])
    while queue:
        node, parity = queue.popleft()
        for edge in sorted(adjacency[node], key=_edge_sort_key):
            if edge is closing:
                continue
            other = edge.second if edge.first == node else edge.first
            state = (other, parity ^ edge.parity)
            if state in previous:
                continue
            previous[state] = ((node, parity), edge)
            if state == target:
                queue.clear()
                break
            queue.append(state)
    if target not in previous:
        return None
    path = []
    current = target
    while current != start:
        parent, edge = previous[current]
        if parent is None or edge is None:
            return None
        path.append(edge)
        current = parent
    path.reverse()
    return _trim_walk(first, path)


def _order_cycle(start, path, closing):
    """Order a path and its closing edge into one closed cycle."""
    remaining = list(path)
    ordered = []
    current = start
    while remaining:
        for edge in remaining:
            if edge.first == current or edge.second == current:
                remaining.remove(edge)
                ordered.append(edge)
                current = edge.second if edge.first == current else edge.first
                break
        else:
            return None
    if current not in (closing.first, closing.second):
        return None
    ordered.append(closing)
    first = ordered[0]
    last = ordered[-1]
    if not set([first.first, first.second]) & set([last.first, last.second]):
        return None
    return ordered


def _trim_walk(start, edges):
    """Drop even closed sub-walks so the remaining walk is a simple cycle."""
    path = [(start, None)]
    positions = {start: 0}
    for edge in edges:
        first, second = edge.first, edge.second
        current = path[-1][0]
        if current == first:
            nxt = second
        elif current == second:
            nxt = first
        else:
            return edges
        if nxt in positions:
            index = positions[nxt]
            removed = len(path) - index - 1
            for node, _ in path[index + 1:]:
                positions.pop(node, None)
            del path[index + 1:]
            if removed % 2 == 1:
                path.append((nxt, edge))
                positions[nxt] = len(path) - 1
                continue
            path[-1] = (nxt, path[-1][1])
            continue
        positions[nxt] = len(path)
        path.append((nxt, edge))
    return [item[1] for item in path[1:] if item[1] is not None]


def _certificate_from_edges(edges):
    """Express a certificate as replayable bill/site links and raw records."""
    bills = sorted(set(edge.bill for edge in edges))
    nodes = []
    links = []
    anchors = []
    records = []
    evidence = set()
    for edge in edges:
        for node in (edge.first, edge.second):
            if node != _GROUND and node not in nodes:
                nodes.append(node)
        for site in edge.witness_sites:
            if site != _GROUND and site not in nodes:
                nodes.append(site)
        evidence.add(edge.evidence)
        if edge.first != _GROUND and edge.second != _GROUND:
            link = sorted([edge.first, edge.second]) + [edge.bill]
            if link not in links:
                links.append(link)
        else:
            site = edge.second if edge.first == _GROUND else edge.first
            if edge.witness_sites:
                site = edge.witness_sites[0]
            anchors.append({
                'site': site,
                'bill': edge.bill,
                'order': (DayOrderLedger.DMY if edge.parity == 0
                          else DayOrderLedger.MDY),
            })
            if len(edge.witness_sites) > 1:
                pair = sorted(edge.witness_sites)[:2]
                link = pair + [edge.bill]
                if link not in links:
                    links.append(link)
        for observation in edge.observations:
            record = _record(observation)
            if record not in records:
                records.append(record)
            evidence.add(observation.evidence)
    return _minimize({
        'bills': bills,
        'sites': sorted(nodes),
        'links': sorted(links),
        'anchors': anchors,
        'records': sorted(records, key=_record_key),
        'evidence': sorted(evidence),
    })


def _minimize(certificate):
    """Drop bills that do not contribute to the contradiction.

    Shrinks the certificate to a subset-minimal set of bills: a cycle can
    include a ground anchor whose bill is only needed to derive the anchor,
    never as part of the inconsistent core.  Requires the same replay and
    consistency helpers used by the published certificate.
    """
    bills = list(certificate['bills'])
    if len(bills) <= 1:
        return certificate
    improved = True
    while improved:
        improved = False
        for bill in list(bills):
            trial = [item for item in bills if item != bill]
            candidate = _restrict(certificate, set(trial))
            if candidate is None:
                continue
            if _consistent(candidate):
                continue
            bills = trial
            certificate = candidate
            improved = True
            break
    return certificate


def _restrict(certificate, bills):
    records = [item for item in certificate['records']
               if item['bill'] in bills]
    if not records:
        return None
    links = [item for item in certificate['links'] if item[2] in bills]
    anchors = [item for item in certificate['anchors']
               if item['bill'] in bills]
    sites = sorted(set(item['site'] for item in records))
    if any(anchor['site'] not in sites for anchor in anchors):
        return None
    return {
        'bills': sorted(bills),
        'sites': sites,
        'links': links,
        'anchors': anchors,
        'records': records,
        'evidence': certificate['evidence'],
    }


def _consistent(certificate):
    """Brute-force check that a small certificate admits one assignment."""
    sites = certificate['sites']
    assignment = {}
    return _search(certificate, sites, 0, assignment)


def _search(certificate, sites, index, assignment):
    if index == len(sites):
        return not _violates(certificate, assignment)
    site = sites[index]
    for order in (DayOrderLedger.DMY, DayOrderLedger.MDY):
        if any(anchor['site'] == site and anchor['order'] != order
               for anchor in certificate['anchors']):
            continue
        assignment[site] = order
        if _search(certificate, sites, index + 1, assignment):
            return True
    assignment.pop(site, None)
    return False


def _violates(certificate, assignment):
    for site in certificate['sites']:
        texts = [item['date'] for item in certificate['records']
                 if item['site'] == site]
        if len(set(texts)) < 2:
            continue
        values = set(_read_text(text, assignment[site]) for text in texts)
        if len(values) > 1:
            return True
    for left, right, bill in certificate['links']:
        first = [item for item in certificate['records']
                 if item['site'] == left and item['bill'] == bill]
        second = [item for item in certificate['records']
                  if item['site'] == right and item['bill'] == bill]
        if not first or not second:
            return True
        if _read_text(first[0]['date'], assignment[left]) != \
                _read_text(second[0]['date'], assignment[right]):
            return True
    return False


def _read_text(text, order):
    match = re.match(r'^(\d{4})-(\d{2})-(\d{2})', text)
    if match:
        return (match.group(1), match.group(2), match.group(3))
    match = _NUM_RE.match(text)
    if not match:
        return text
    first, second, year = match.group(1), match.group(2), match.group(3)
    if len(year) == 2:
        year = '20%s' % year
    first = int(first)
    second = int(second)
    if order == DayOrderLedger.DMY:
        return (year, '%02d' % second, '%02d' % first)
    return (year, '%02d' % first, '%02d' % second)


def _edge_sort_key(edge):
    return (min(edge.first, edge.second), max(edge.first, edge.second),
            edge.parity, edge.bill, edge.evidence)


def _certificate_key(certificate):
    return (len(certificate['bills']), len(certificate['sites']),
            certificate['bills'], certificate['sites'])


# Convenience API

def reconcile(evidences, columns=None, sites=None, date_column='date',
              quantity_columns=None, ledger=None):
    """Reconcile evidence documents and publish a Table Schema projection."""
    ledger = ledger or DayOrderLedger(
        date_column=date_column, quantity_columns=quantity_columns)
    for site in sites or []:
        ledger.declare_site(
            site.get('id', site.get('site')),
            columns=site.get('columns', site.get('fields')),
            sample=_sample(site),
            missing_values=site.get(
                'missingValues', site.get('missing_values')))
    ledger.commit_all(evidences)
    return ledger.publish(columns=columns)


def infer_day_orders(evidences, columns=None, sites=None, date_column='date',
                     quantity_columns=None):
    """Infer a shared day/month order contract from multiple site samples."""
    return reconcile(
        evidences, columns=columns, sites=sites, date_column=date_column,
        quantity_columns=quantity_columns)


# Discoverable aliases

DayOrderReconciler = DayOrderLedger
SiteDateReconciler = DayOrderLedger
reconcile_day_orders = reconcile
infer_site_day_orders = infer_day_orders
infer_day_order = infer_day_orders
