"""Run the forensic matrix against one shared, unmodified Pyslet service."""

import json
import os
import sys

from harness import (
    ForensicClient, EXPECTED_ACTIVE_KEYS, INACTIVE_KEY, OUT_OF_SCOPE_KEY,
    REGION_NULL_KEY, build_service, build_url, configure_collection,
    count_evidence, decode_feed, fetch, parse_feed, refetch_active,
    region_evidence, request_text, write_region, write_run)
from pyslet.odata2 import core

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'artifacts')


def classify(entity, client_filter, client, collection, refetch_log):
    active = entity['Active']
    if client_filter == 'naive':
        return active.value is True, 'naive Active==true; selected=%s' % (
            entity.is_selected('Active'),)
    if client_filter == 'selection_aware':
        if entity.is_selected('Active'):
            return active.value is True, 'Active selected; value=%r' % (
                active.value,)
        refetched = refetch_active(client, collection, entity.key())
        refetch_log.append(refetched)
        value = refetched['decoded']['Active']['value']
        return value is True, (
            'Active not selected in page; targeted refetch value=%r' % (
                value,))
    raise ValueError(client_filter)


def execute(svc, projection, continuation, client_filter, name,
            topmax=1, top=2, continuation_filter=None,
            continuation_skiptoken=None, max_pages=12):
    """Execute one export run and return its auditable trace."""
    client = ForensicClient(svc)
    collection = configure_collection(client, projection)
    collection.set_page(top, 0)
    url = build_url(collection, skip=0)
    pages, refetches, kept, dropped = [], [], [], []
    previous_next = None

    for page_number in range(1, max_pages + 1):
        request, record = fetch(client, url)
        parsed = parse_feed(request.res_body, url)
        doc = core.Document(base_uri=url)
        doc.read(request.res_body)
        entities = []
        for entry in doc.root.Entry:
            entity = core.Entity(collection.entity_set)
            entity.exists = True
            entry.get_value(entity)
            entities.append(entity)
        decoded = decode_feed(collection, request.res_body, url)
        decisions = []
        for entity in entities:
            keep, reason = classify(
                entity, client_filter, client, collection, refetches)
            decisions.append({
                'key': entity.key(),
                'kept': keep,
                'active_in_page': (
                    entity['Active'].value
                    if entity.is_selected('Active') else '<not selected>'),
                'reason': reason,
            })
            if keep:
                kept.append(entity.key())
            else:
                dropped.append({'key': entity.key(), 'reason': reason})
        pages.append({
            'page': page_number,
            'request_url': url,
            'request': record,
            'request_text': request_text(record),
            'response_body': request.res_body,
            'parsed': parsed,
            'decoded': decoded,
            'decisions': decisions,
            'previous_next_link': previous_next,
            'request_equals_previous_next': (
                None if previous_next is None
                else previous_next == record['url']),
            'next_link': parsed['next_link'],
        })
        previous_next = parsed['next_link']
        if not parsed['entry_keys']:
            break
        if continuation == 'next':
            if parsed['next_link'] is None:
                break
            url = parsed['next_link']
        elif continuation == 'rebuilt':
            url = build_url(
                collection,
                skip=None if continuation_skiptoken is not None
                else page_number * top,
                skiptoken=continuation_skiptoken,
                filter_override=continuation_filter)
        else:
            raise ValueError(continuation)

    return {
        'name': name,
        'projection': projection,
        'continuation': continuation,
        'client_filter': client_filter,
        'server_topmax': topmax,
        'requested_top': top,
        'continuation_filter': (str(continuation_filter)
                                if continuation_filter is not None else None),
        'continuation_skiptoken': continuation_skiptoken,
        'pages': pages,
        'refetches': refetches,
        'kept': kept,
        'dropped': dropped,
    }


def run_pyslet_iterpage(svc, projection, client_filter, name, topmax=1,
                        top=2, max_pages=12):
    """Use Pyslet's own iterpage(set_next=True), i.e. the built-in next path."""
    client = ForensicClient(svc)
    collection = configure_collection(client, projection)
    collection.set_page(top, 0)
    pages, refetches, kept, dropped = [], [], [], []
    for page_number in range(1, max_pages + 1):
        entities = list(collection.iterpage(set_next=True))
        record = client.transport.log[-1]
        parsed = parse_feed(record['response_body'], record['url'])
        decoded = decode_feed(
            collection, record['response_body'], record['url'])
        decisions = []
        for entity in entities:
            keep, reason = classify(
                entity, client_filter, client, collection, refetches)
            decisions.append({
                'key': entity.key(),
                'kept': keep,
                'active_in_page': (
                    entity['Active'].value
                    if entity.is_selected('Active') else '<not selected>'),
                'reason': reason,
            })
            if keep:
                kept.append(entity.key())
            else:
                dropped.append({'key': entity.key(), 'reason': reason})
        pages.append({
            'page': page_number,
            'request_url': record['url'],
            'request': record,
            'request_text': request_text(record),
            'response_body': record['response_body'],
            'parsed': parsed,
            'decoded': decoded,
            'decisions': decisions,
            'previous_next_link': None,
            'request_equals_previous_next': None,
            'next_link': parsed['next_link'],
        })
        if not parsed['entry_keys']:
            break
    return {
        'name': name,
        'projection': projection,
        'continuation': 'pyslet_iterpage',
        'client_filter': client_filter,
        'server_topmax': topmax,
        'requested_top': top,
        'continuation_filter': None,
        'continuation_skiptoken': None,
        'pages': pages,
        'refetches': refetches,
        'kept': kept,
        'dropped': dropped,
    }


def build_ledger(results, baseline_name='R0_baseline_full_next'):
    """Per-key first-loss ledger with the page boundary that caused it."""
    baseline = results[baseline_name]
    baseline_pages = {}
    for page in baseline['pages']:
        for key in page['parsed']['entry_keys']:
            baseline_pages.setdefault(key, page['page'])
    ledger = {}
    for key in EXPECTED_ACTIVE_KEYS + [INACTIVE_KEY, OUT_OF_SCOPE_KEY]:
        entry = {
            'key': key,
            'expected_in_export': key in EXPECTED_ACTIVE_KEYS,
            'baseline_page': baseline_pages.get(key),
            'runs': {},
        }
        for name, data in results.items():
            appearing = [p['page'] for p in data['pages']
                         if key in p['parsed']['entry_keys']]
            kept = key in data['kept']
            if key == OUT_OF_SCOPE_KEY:
                boundary = 'scope_filter'
                page = None
                detail = 'never in W1 result by design'
            elif key == INACTIVE_KEY:
                if appearing:
                    page = appearing[0]
                    boundary = ('postprocess' if not kept
                                else 'correct_exclusion')
                    detail = (
                        'non-key property Active present=false in page; '
                        'client value is None, so this is a false exclusion'
                        if not kept else
                        'Active=false is a legitimate exclusion')
                else:
                    page = baseline_pages.get(key)
                    boundary = 'continuation_skip'
                    detail = 'not reached'
            elif kept:
                page = appearing[0] if appearing else None
                boundary = 'kept'
                detail = 'present and kept'
            elif appearing:
                page = appearing[0]
                boundary = 'postprocess'
                detail = 'present in page response but dropped by client'
            else:
                page = baseline_pages.get(key)
                boundary = 'continuation_skip'
                detail = 'never present in any page response'
            page_record = None
            for p in data['pages']:
                if p['page'] == page:
                    page_record = {
                        'page': p['page'],
                        'request_url': p['request_url'],
                        'request_equals_previous_next':
                            p['request_equals_previous_next'],
                        'previous_next_link': p['previous_next_link'],
                        'entry_keys': p['parsed']['entry_keys'],
                    }
                    break
            entry['runs'][name] = {
                'kept': kept,
                'appearing_pages': appearing,
                'first_boundary': boundary,
                'first_boundary_page': page,
                'first_boundary_detail': detail,
                'page_record': page_record,
            }
        ledger[key] = entry
    return ledger


def summarize(data):
    missing = [k for k in EXPECTED_ACTIVE_KEYS if k not in data['kept']]
    print('\n### %s' % data['name'])
    print('projection=%s continuation=%s client_filter=%s topmax=%s top=%s' % (
        data['projection'], data['continuation'], data['client_filter'],
        data['server_topmax'], data['requested_top']))
    for page in data['pages']:
        print(' page %d keys=%s next=%s request=prev_next? %s' % (
            page['page'], page['parsed']['entry_keys'],
            page['next_link'], page['request_equals_previous_next']))
        for d in page['decisions']:
            print('   %-7s kept=%-5s active=%-13r %s' % (
                d['key'], d['kept'], d['active_in_page'], d['reason']))
    print(' KEPT=%s' % data['kept'])
    print(' MISSING_EXPECTED=%s' % missing)
    print(' REFETCHES=%s' % [
        (r['key'], r['decoded']['Active']['value'])
        for r in data['refetches']])
    return missing


def main():
    svc, container, model = build_service(topmax=1)
    runs = []

    # Baseline: all fields, strict next link.
    runs.append(execute(
        svc, 'full', 'next', 'naive', 'R0_baseline_full_next'))
    # One-at-a-time swaps from the baseline.
    runs.append(execute(
        svc, 'partial', 'next', 'naive', 'R1_projection_only_partial_next'))
    runs.append(execute(
        svc, 'full', 'rebuilt', 'naive', 'R2_continuation_only_full_rebuilt'))
    runs.append(execute(
        svc, 'partial', 'rebuilt', 'naive', 'R3_both_partial_rebuilt'))

    # Three corrections of R3.
    runs.append(execute(
        svc, 'partial', 'rebuilt', 'selection_aware',
        'C1_filter_only_partial_rebuilt'))
    runs.append(execute(
        svc, 'partial', 'next', 'naive', 'C2_next_only_partial_next'))
    runs.append(execute(
        svc, 'partial', 'next', 'selection_aware',
        'C3_both_partial_next'))
    # Built-in Pyslet strict-next path as an independent control.
    runs.append(run_pyslet_iterpage(
        svc, 'partial', 'naive', 'C2b_next_only_pyslet_iterpage'))

    # Continuation defect variants: wrong page cursor and wrong filter.
    runs.append(execute(
        svc, 'full', 'rebuilt', 'naive',
        'X1_wrong_cursor_full_rebuilt_skiptoken1',
        continuation_skiptoken='1', max_pages=5))
    runs.append(execute(
        svc, 'full', 'rebuilt', 'naive', 'X2_wrong_filter_full_rebuilt',
        continuation_filter=core.CommonExpression.from_str(
            "Warehouse eq 'W2'")))

    results = {}
    for data in runs:
        write_run(data, OUT)
        results[data['name']] = data
        summarize(data)

    # Total-only comparison: the filtered count is correct, yet the export
    # still misses keys.  This isolates "not a filter/count problem".
    count_client = ForensicClient(svc)
    count_collection = configure_collection(count_client, 'partial')
    counts = count_evidence(count_client, count_collection)
    with open(os.path.join(OUT, 'count.json'), 'w') as f:
        json.dump({
            'url': counts['url'],
            'count': counts['count'],
            'request': counts['request_text'],
            'response_body': counts['response_body'].decode('utf-8'),
        }, f, indent=2)
        f.write('\n')
    print('\n### FILTERED COUNT')
    print(counts['url'])
    print('count=%r' % counts['response_body'])
    print('note: a correct count does not identify which keys are missing')

    # Region evidence: true NULL Region plus a navigation property.
    region_client = ForensicClient(svc)
    region_collection = configure_collection(region_client, 'partial')
    region_cases = region_evidence(region_client, region_collection)
    write_region(region_cases, OUT)
    for case in region_cases:
        print('\n### REGION %s' % case['name'])
        print('URL', case['url'])
        print(case['response_body'].decode('utf-8'))
        print('DECODED', json.dumps(case['decoded'], indent=2))

    ledger = build_ledger(results)
    with open(os.path.join(OUT, 'ledger.json'), 'w') as f:
        json.dump(ledger, f, indent=2)
        f.write('\n')

    manifest = {
        'service_root': 'http://host/service/',
        'model': 'forensics/model.xml',
        'server_topmax': 1,
        'requested_top': 2,
        'filter': "Warehouse eq 'W1'",
        'orderby': 'BinSequence asc,ProductID asc',
        'expected_active_w1': EXPECTED_ACTIVE_KEYS,
        'expected_inactive_w1': [INACTIVE_KEY],
        'out_of_scope': [OUT_OF_SCOPE_KEY],
        'region_null_key': REGION_NULL_KEY,
        'filtered_count': counts['count'],
        'ledger': 'forensics/artifacts/ledger.json',
        'runs': {
            name: {
                'projection': data['projection'],
                'continuation': data['continuation'],
                'client_filter': data['client_filter'],
                'continuation_filter': str(data['continuation_filter'])
                if data['continuation_filter'] is not None else None,
                'continuation_skiptoken': data['continuation_skiptoken'],
                'kept': data['kept'],
                'missing_expected': [
                    k for k in EXPECTED_ACTIVE_KEYS
                    if k not in data['kept']],
                'pages': [
                    {
                        'page': p['page'],
                        'request_url': p['request_url'],
                        'request_equals_previous_next':
                            p['request_equals_previous_next'],
                        'entry_keys': p['parsed']['entry_keys'],
                        'next_link': p['next_link'],
                    } for p in data['pages']],
            } for name, data in results.items()
        },
    }
    with open(os.path.join(OUT, 'matrix.json'), 'w') as f:
        json.dump(manifest, f, indent=2)
        f.write('\n')


if __name__ == '__main__':
    main()
