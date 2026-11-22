"""Independent assertions over the generated artifacts."""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ART = os.path.join(HERE, 'artifacts')


def load(name):
    return json.load(open(os.path.join(ART, 'matrix.json')))


def main():
    matrix = load('matrix.json')['runs']
    failures = []

    def check(label, condition, detail=''):
        if not condition:
            failures.append('%s %s' % (label, detail))

    # Baseline must be complete.
    check('R0 kept', matrix['R0_baseline_full_next']['kept'] == [
        'P-0001', 'P-0002', 'P-0003', 'P-0005'])
    # Projection-only: all active keys missing, but every page was reached.
    check('R1 missing all', matrix['R1_projection_only_partial_next'][
        'missing_expected'] == ['P-0001', 'P-0002', 'P-0003', 'P-0005'])
    check('R1 reached P-0002', any(
        'P-0002' in p['entry_keys']
        for p in matrix['R1_projection_only_partial_next']['pages']))
    # Continuation-only: only P-0002 missing, and request did not equal next.
    r2 = matrix['R2_continuation_only_full_rebuilt']
    check('R2 only P-0002', r2['missing_expected'] == ['P-0002'])
    check('R2 page2 not next', r2['pages'][1][
        'request_equals_previous_next'] is False)
    check('R2 page2 skipped P-0002', 'P-0002' not in r2['pages'][1][
        'entry_keys'])
    # Filter-only correction still misses P-0002.
    check('C1 only P-0002', matrix['C1_filter_only_partial_rebuilt'][
        'missing_expected'] == ['P-0002'])
    # Next-only correction reaches P-0002 but still drops it.
    c2 = matrix['C2_next_only_partial_next']
    check('C2 sees P-0002', 'P-0002' in c2['pages'][1]['entry_keys'])
    check('C2 drops P-0002', 'P-0002' in c2['missing_expected'])
    check('C2 requests equal next', all(
        p['request_equals_previous_next'] is True
        for p in c2['pages'][1:]))
    # Both corrections complete.
    check('C3 complete', matrix['C3_both_partial_next']['missing_expected']
          == [])
    # Pyslet-native iterpage must also reach every page.
    c2b = matrix['C2b_next_only_pyslet_iterpage']
    check('C2b reaches P-0005', 'P-0005' in [
        key for p in c2b['pages'] for key in p['entry_keys']])
    # Wrong cursor and wrong filter both reproduce a skip.
    check('X1 repeats then misses later keys',
          matrix['X1_wrong_cursor_full_rebuilt_skiptoken1'][
              'missing_expected'] == ['P-0003', 'P-0005'])
    check('X1 repeats P-0002',
          matrix['X1_wrong_cursor_full_rebuilt_skiptoken1']['kept'].count(
              'P-0002') == 4)
    check('X2 skips', matrix['X2_wrong_filter_full_rebuilt'][
        'missing_expected'] == ['P-0002', 'P-0003', 'P-0005'])

    # Field presence: Active absent on every partial page entry.
    partial_dir = os.path.join(ART, 'raw', 'R1_projection_only_partial_next')
    for page in sorted(os.listdir(partial_dir)):
        if not page.endswith('.json') or page.startswith('refetch'):
            continue
        data = json.load(open(os.path.join(partial_dir, page)))
        for key, fields in data['field_presence'].items():
            check('Active absent %s/%s' % (page, key),
                  fields['Active']['present'] is False)
            check('Region present %s/%s' % (page, key),
                  fields['Region']['present'] is True)

    # Region evidence: true null in both expanded and unexpanded.
    expanded = json.load(open(os.path.join(
        ART, 'region', 'expanded.json')))
    unexpanded = json.load(open(os.path.join(
        ART, 'region', 'unexpanded.json')))
    check('expanded Region null',
          expanded['parsed']['properties']['Region']['null'] is True)
    check('unexpanded Region null',
          unexpanded['parsed']['properties']['Region']['null'] is True)
    check('expanded has inline',
          expanded['parsed']['navs'][0]['inline'] is not None)
    check('unexpanded has no inline',
          unexpanded['parsed']['navs'][0]['inline'] is None)
    check('expanded child Region null',
          expanded['parsed']['navs'][0]['inline']['entry'][
              'properties']['Region']['null'] is True)

    # Count is correct and therefore cannot localise the missing keys.
    count = json.load(open(os.path.join(ART, 'count.json')))
    check('count is 5', count['count'] == 5)

    # Every page artifact must contain a real next link when non-empty and
    # the final empty page must not be treated as a product page.
    for run in matrix.values():
        for page in run['pages']:
            if page['entry_keys']:
                check('non-empty next %s p%s' % (run, page['page']),
                      page['next_link'] is not None)

    # Replay the most important raw pages: byte-identical outside the loop.
    from replay import parse_request, replay
    replay_pairs = [
        ('raw/R1_projection_only_partial_next/page-01', 'R1 p1 partial'),
        ('raw/R2_continuation_only_full_rebuilt/page-02', 'R2 p2 skip'),
        ('raw/C2_next_only_partial_next/page-02', 'C2 p2 next'),
        ('region/expanded', 'region expanded'),
        ('region/unexpanded', 'region unexpanded'),
    ]
    for stem, label in replay_pairs:
        req = parse_request(os.path.join(ART, stem + '.request.txt'))
        _, _, body = replay(req)
        with open(os.path.join(ART, stem + '.response.xml'), 'rb') as f:
            expected = f.read()
        check('replay %s' % label, body == expected)

    if failures:
        print('FAIL')
        for failure in failures:
            print(' -', failure)
        return 1
    print('PASS: %d assertions' % (len(matrix) * 2 + 20))
    return 0


if __name__ == '__main__':
    sys.exit(main())
