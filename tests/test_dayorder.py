# -*- coding: utf-8 -*-
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import json
from tableschema.dayorder import (
    DayOrderLedger, reconcile, infer_day_orders)


# Helpers

def evidence(evidence_id, rows, **extra):
    document = {'id': evidence_id, 'rows': rows}
    document.update(extra)
    return document


def declare(ledger, sites, columns=('bill', 'date', 'quantity'), sample=8):
    for site in sites:
        ledger.declare_site(site, columns=list(columns), sample=sample)
    return ledger


def format_date(order, day, month):
    if order == DayOrderLedger.DMY:
        return '%02d/%02d/2026' % (day, month)
    return '%02d/%02d/2026' % (month, day)


_DAYS = [4, 1, 2, 3, 6, 7, 8, 9, 10, 11, 12]


def bill_date(index):
    """A genuinely ambiguous May date for bill `k(index)` (day != month)."""
    return _DAYS[index % len(_DAYS)], 5


def chain(length=3):
    """Alternating chain linked only by adjacent shared bills.

    Ground truth: `s1` is MDY, `s2` is DMY, `s3` is MDY, ...  Every date text
    is individually ambiguous, so only the shared-bill chain decides the tail.
    """
    orders = {}
    for index in range(1, length + 1):
        orders['s%s' % index] = (
            DayOrderLedger.MDY if index % 2 == 1 else DayOrderLedger.DMY)
    documents = []
    documents.append(evidence('w0', [
        {'site': 's1', 'bill': 'k0', 'date': '2026-05-04'},
    ]))
    first = orders['s1']
    documents.append(evidence('w1', [
        {'site': 's1', 'bill': 'k0', 'date': format_date(first, 4, 5)},
        {'site': 's1', 'bill': 'k1', 'date': format_date(
            first, *bill_date(1))},
    ]))
    for index in range(1, length):
        site = 's%s' % (index + 1)
        order = orders[site]
        rows = [{
            'site': site, 'bill': 'k%s' % index,
            'index': 1, 'quantity': '1',
            'date': format_date(order, *bill_date(index)),
        }]
        if index + 1 < length:
            rows.append({
                'site': site, 'bill': 'k%s' % (index + 1),
                'index': 2, 'quantity': '1',
                'date': format_date(order, *bill_date(index + 1)),
            })
        documents.append(evidence('w%s' % (index + 1), rows))
    return documents


def expected_formats(length):
    formats = {}
    for index in range(1, length + 1):
        formats['s%s' % index] = (
            '%m/%d/%Y' if index % 2 == 1 else '%d/%m/%Y')
    return formats


def _replay_orders(certificate):
    from itertools import product
    sites = certificate['sites']
    records = certificate['records']
    links = certificate['links']
    anchors = certificate['anchors']
    bills = certificate['bills']
    for assignment in product(('DMY', 'MDY'), repeat=len(sites)):
        chosen = dict(zip(sites, assignment))
        if any(chosen[anchor['site']] != anchor['order']
               for anchor in anchors):
            continue
        ok = True
        for site in sites:
            texts = [item['date'] for item in records
                     if item['site'] == site]
            values = set(_read(text, chosen[site]) for text in texts)
            if len(values) > 1:
                ok = False
                break
        if not ok:
            continue
        for link in links:
            left, right, bill = link
            first = [item for item in records
                     if item['site'] == left and item['bill'] == bill]
            second = [item for item in records
                      if item['site'] == right and item['bill'] == bill]
            if not first or not second:
                ok = False
                break
            if _read(first[0]['date'], chosen[left]) != \
                    _read(second[0]['date'], chosen[right]):
                ok = False
                break
        if ok:
            return chosen
    return None


def _read(text, order):
    if len(text) >= 10 and text[4] == '-':
        return text[:10]
    parts = text.split('/')
    first, second, year = int(parts[0]), int(parts[1]), parts[2]
    if order == 'DMY':
        return '%s-%02d-%02d' % (year, second, first)
    return '%s-%02d-%02d' % (year, first, second)


def _is_contradiction(certificate):
    return _replay_orders(certificate) is None


def _minimal(certificate, ledger):
    import itertools
    bills = certificate['bills']
    if len(bills) <= 1:
        return True
    for size in range(len(bills)):
        for subset in itertools.combinations(bills, size):
            reduced = _restrict(certificate, set(subset))
            if reduced is not None and _is_contradiction(reduced):
                return False
    return True


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


# Manual small chain

def test_small_chain_publishes_unique_format_for_tail():
    ledger = declare(DayOrderLedger(), ['s1', 's2', 's3'])
    for document in chain():
        assert ledger.commit(document)
    published = ledger.publish()
    assert published['formats'] == expected_formats(3)
    assert ledger.conflict() is None


def test_ambiguous_pair_without_witness_stays_unresolved():
    ledger = declare(DayOrderLedger(), ['a', 'b'])
    ledger.commit(evidence('p1', [
        {'site': 'a', 'bill': 'x', 'date': '04/05/2026'},
        {'site': 'b', 'bill': 'x', 'date': '05/04/2026'},
    ]))
    published = ledger.publish()
    assert published['formats'] == {'a': None, 'b': None}
    assert published['unresolved'] == ['a', 'b']
    assert ledger.candidates() == {'a': ['DMY', 'MDY'],
                                   'b': ['DMY', 'MDY']}


def test_reverse_witness_conflict_and_minimal_certificate():
    ledger = declare(DayOrderLedger(), ['s1', 's2', 's3'])
    for document in chain():
        ledger.commit(document)
    ledger.commit(evidence('rev', [
        {'site': 's3', 'bill': 'k2', 'date': '2026-03-05'},
    ]))
    certificate = ledger.conflict()
    assert certificate is not None
    # The certificate must be a genuine minimal contradiction: replaying its
    # own records under its own links/anchors has no consistent assignment.
    assert _is_contradiction(certificate)
    assert _minimal(certificate, ledger)
    published = ledger.publish()
    assert published['formats']['s2'] is None
    assert published['formats']['s3'] is None


def test_revoke_middle_witness_splits_components():
    ledger = declare(DayOrderLedger(), ['s1', 's2', 's3', 's4', 'other'])
    for document in chain(length=4):
        ledger.commit(document)
    ledger.commit(evidence('free', [
        {'site': 'other', 'bill': 'z', 'date': '07/08/2026'},
    ]))
    assert ledger.publish()['formats']['s4'] == '%d/%m/%Y'

    assert ledger.revoke('w3')
    published = ledger.publish()
    assert published['formats']['s1'] == '%m/%d/%Y'
    assert published['formats']['s2'] == '%d/%m/%Y'
    assert published['formats']['s3'] is None
    assert published['formats']['s4'] is None
    assert published['formats']['other'] is None
    assert published['unresolved'] == ['other', 's3', 's4']

    assert ledger.recommit('w3', 'w3-again')
    assert ledger.publish()['formats']['s4'] == '%d/%m/%Y'


def test_revoke_removes_conflict_and_restores_formats():
    ledger = declare(DayOrderLedger(), ['s1', 's2', 's3'])
    for document in chain():
        ledger.commit(document)
    ledger.commit(evidence('rev', [
        {'site': 's3', 'bill': 'k2', 'date': '2026-03-05'},
    ]))
    assert ledger.conflict() is not None
    ledger.revoke('rev')
    assert ledger.conflict() is None
    assert ledger.publish()['formats']['s3'] == '%m/%d/%Y'


# Identity and lifecycle

def test_commit_is_idempotent_only_for_the_same_identifier():
    ledger = declare(DayOrderLedger(), ['a'])
    document = evidence('e1', [
        {'site': 'a', 'bill': 'x', 'date': '2026-05-04'},
    ])
    assert ledger.commit(document) is True
    assert ledger.commit(document) is False
    assert ledger.commit(evidence('e1', [
        {'site': 'a', 'bill': 'x', 'date': '2026-05-04'},
    ])) is False
    assert ledger.commit(evidence('e2', [
        {'site': 'a', 'bill': 'x', 'date': '2026-05-04'},
    ])) is True
    assert ledger.statistics()['evidence']['committed'] == ['e1', 'e2']


def test_revoked_identifier_requires_a_new_one():
    ledger = declare(DayOrderLedger(), ['a'])
    document = evidence('e1', [
        {'site': 'a', 'bill': 'x', 'date': '2026-05-04'}, ])
    ledger.commit(document)
    ledger.revoke('e1')
    assert ledger.commit(document) is False
    assert ledger.recommit('e1') is True
    assert 'e1#1' in ledger.statistics()['evidence']['committed']


def test_recommit_accepts_document_content():
    ledger = declare(DayOrderLedger(), ['a'])
    document = evidence('e1', [
        {'site': 'a', 'bill': 'x', 'date': '2026-05-04'}, ])
    ledger.commit(document)
    ledger.revoke('e1')
    assert ledger.recommit(document, 'e1-new') is True
    assert ledger.statistics()['evidence']['committed'] == ['e1-new']


# Declarations and readable projections

def test_statistics_follow_declared_samples_columns_and_missing_values():
    ledger = DayOrderLedger()
    ledger.commit(evidence('e1', [
        {'site': 'a', 'bill': 'x', 'date': '2026-05-04',
         'quantity': '1', 'status': 'NA'}], sites=[
        {'id': 'a', 'columns': ['date', 'quantity', 'status'],
         'sample': 7, 'missingValues': ['NA']},
    ]))
    statistics = ledger.statistics()
    assert statistics['sites']['a'] == {
        'samples': 7,
        'columns': ['date', 'quantity', 'status'],
        'missingValues': ['NA'],
    }


def test_sample_limit_excludes_rows_beyond_the_limit():
    ledger = DayOrderLedger()
    ledger.declare_site('a', columns=['bill', 'date'], sample=2)
    ledger.commit(evidence('e1', [
        {'site': 'a', 'bill': 'x1', 'date': '2026-05-04'},
        {'site': 'a', 'bill': 'x2', 'date': '2026-05-05'},
        {'site': 'a', 'bill': 'x3', 'date': '13/05/2026'},
    ]))
    published = ledger.publish()
    assert published['formats']['a'] == 'default'
    assert ledger.candidates()['a'] == ['default']


def test_quantity_is_numeric_while_all_missing_site_keeps_date_disabled():
    ledger = DayOrderLedger()
    ledger.declare_site('a', columns=['bill', 'date', 'quantity'], sample=5)
    ledger.declare_site('b', columns=['bill', 'date', 'quantity'], sample=5)
    ledger.declare_site('c', columns=['bill', 'date', 'quantity'], sample=5)
    ledger.commit(evidence('ea', [
        {'site': 'a', 'bill': 'a1', 'date': '2026-01-02', 'quantity': '1'},
    ]))
    ledger.commit(evidence('eb', [
        {'site': 'b', 'bill': 'b1', 'date': '2026-01-03', 'quantity': '2.5'},
    ]))
    ledger.commit(evidence('ec', [
        {'site': 'c', 'bill': 'c1', 'date': '', 'quantity': ''},
    ]))
    published = ledger.publish(columns=['bill', 'date', 'quantity'])
    fields = dict((item['name'], item) for item in published['fields'])
    assert fields['quantity']['type'] == 'number'
    assert published['sites']['c']['dateFormat'] is None
    assert 'date' not in published['sites']['c']['columns']
    assert 'quantity' in published['sites']['c']['columns']


def test_projection_follows_business_column_order_and_headers():
    ledger = DayOrderLedger()
    ledger.declare_site('a', columns=['quantity', 'date', 'bill'], sample=5)
    ledger.commit(evidence(
        'e1',
        [['x', '1', '2026-05-04']],
        headers=['bill', 'quantity', 'ship_date']))
    published = ledger.publish()
    assert [item['name'] for item in published['fields']] == [
        'quantity', 'date', 'bill']
    assert published['sites']['a']['columns'] == [
        'quantity', 'date', 'bill']


# Determinism and scale

def test_output_is_independent_of_delivery_order():
    documents = chain(length=4)
    outputs = set()
    import itertools
    for permutation in itertools.permutations(documents):
        ledger = declare(DayOrderLedger(), ['s1', 's2', 's3', 's4'])
        ledger.commit_all(list(permutation))
        published = ledger.publish()
        outputs.add(json.dumps({
            'formats': published['formats'],
            'fields': published['fields'],
        }, sort_keys=True))
    assert len(outputs) == 1


def test_long_chain_is_linear_not_combinatorial():
    length = 1200
    ledger = declare(DayOrderLedger(), ['s%s' % i
                                        for i in range(1, length + 1)],
                     sample=4)
    ledger.commit_all(chain(length))
    published = ledger.publish()
    expected = expected_formats(length)
    assert published['formats']['s%s' % length] == expected[
        's%s' % length]
    assert ledger.conflict() is None


def test_publish_does_not_touch_original_rows():
    ledger = declare(DayOrderLedger(), ['s1', 's2', 's3'])
    documents = chain()
    for document in documents:
        ledger.commit(document)
    # Destroy the source documents; publication uses retained compact rows.
    for document in documents:
        document['rows'] = None
    ledger.publish()
    assert ledger.conflict() is None


# Convenience API

def test_reconcile_convenience_api():
    documents = chain()
    sites = [
        {'id': 's1', 'columns': ['bill', 'date', 'quantity'], 'sample': 5},
        {'id': 's2', 'columns': ['bill', 'date', 'quantity'], 'sample': 5},
        {'id': 's3', 'columns': ['bill', 'date', 'quantity'], 'sample': 5},
    ]
    published = reconcile(
        documents, columns=['bill', 'date', 'quantity'], sites=sites)
    assert published['formats']['s3'] == expected_formats(3)['s3']
    alias = infer_day_orders(documents, columns=['bill', 'date'],
                             sites=sites)
    assert alias['formats'] == published['formats']


def test_multibill_certificate_is_minimal_and_replayable():
    ledger = DayOrderLedger()
    for site in ('x', 'y', 'z'):
        ledger.declare_site(site, columns=['bill', 'date'], sample=6)
    ledger.commit(evidence('x0', [
        {'site': 'x', 'bill': 'k0', 'date': '2026-05-04'}]))
    ledger.commit(evidence('x1', [
        {'site': 'x', 'bill': 'k0', 'date': '05/04/2026'},
        {'site': 'x', 'bill': 'k1', 'date': '05/04/2026'}]))
    ledger.commit(evidence('y1', [
        {'site': 'y', 'bill': 'k1', 'date': '04/05/2026'},
        {'site': 'y', 'bill': 'k2', 'date': '03/05/2026'}]))
    ledger.commit(evidence('z2', [
        {'site': 'z', 'bill': 'k2', 'date': '05/03/2026'}]))
    ledger.commit(evidence('z3', [
        {'site': 'z', 'bill': 'k2', 'date': '2026-03-05'}]))
    certificate = ledger.conflict()
    assert certificate is not None
    assert _is_contradiction(certificate)
    assert _minimal(certificate, ledger)
    # The irrelevant ISO witness bill `k0` must not inflate the certificate.
    assert 'k0' not in certificate['bills']


def test_odd_cycle_certificate_without_anchor_is_minimal():
    ledger = DayOrderLedger()
    for site in ('s1', 's2', 's3'):
        ledger.declare_site(site, columns=['bill', 'date'], sample=8)
    ledger.commit(evidence('b', [
        {'site': 's1', 'bill': 'b3', 'date': '04/05/2026'},
        {'site': 's2', 'bill': 'b3', 'date': '05/04/2026'}]))
    ledger.commit(evidence('c', [
        {'site': 's2', 'bill': 'b4', 'date': '04/05/2026'},
        {'site': 's3', 'bill': 'b4', 'date': '05/04/2026'}]))
    ledger.commit(evidence('bad', [
        {'site': 's3', 'bill': 'b5', 'date': '04/05/2026'},
        {'site': 's1', 'bill': 'b5', 'date': '05/04/2026'}]))
    certificate = ledger.conflict()
    assert certificate is not None
    assert certificate['bills'] == ['b4', 'b5']
    assert certificate['sites'] == ['s1', 's2', 's3']
    assert _is_contradiction(certificate)
    assert _minimal(certificate, ledger)
    published = ledger.publish()
    assert published['formats'] == {'s1': None, 's2': None, 's3': None}


def test_long_chain_shuffle_and_middle_revocation():
    import random
    length = 120
    documents = chain(length)
    reference = DayOrderLedger()
    reference.commit_all(documents)
    expected = reference.publish()['formats']

    shuffled = list(documents)
    random.Random(7).shuffle(shuffled)
    ledger = DayOrderLedger()
    for document in shuffled:
        ledger.commit(document)
    assert ledger.publish()['formats'] == expected

    middle = 'w%d' % (length // 2)
    ledger.revoke(middle)
    published = ledger.publish()
    for index in range(1, length + 1):
        site = 's%s' % index
        if index < length // 2:
            assert published['formats'][site] == expected[site]
        else:
            # `s(middle)` lost its only evidence; the detached right side no
            # longer has an anchor and falls back to both candidates.
            assert published['formats'][site] is None
    assert ledger.conflict() is None
