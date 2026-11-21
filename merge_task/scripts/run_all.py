"""End-to-end driver: build inputs, run all three merges, verify them.

Deliverables (under ``deliverables/``):

* ``merge_jia_yi/``   - merge with 甲 submitted first, then 乙
* ``merge_yi_jia/``   - same batches with the loading order swapped
* ``merge_damaged/``  - merge where 甲's F1 form row was deleted while the
  cognate judgement C1 still references it

Every delivery carries a ``provenance.tsv`` (output ID -> source batch +
original ID) and a ``missing.tsv`` (dangling references with owning batch
and original ID).
"""
import csv
import filecmp
import json
import pathlib
import shutil
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from build_inputs import build_jia, build_yi  # noqa: E402
from merge import merge  # noqa: E402
from pycldf import Wordlist  # noqa: E402

MAPPING = ROOT / 'inputs' / 'concept_mapping.json'


def read_tsv(path):
    with pathlib.Path(path).open(encoding='utf-8') as handle:
        return list(csv.DictReader(handle, delimiter='\t'))


def build_damaged_jia(dest):
    """Copy batch 甲, then drop the F1 form row but keep cognate C1."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(ROOT / 'inputs' / 'jia' / 'cldf', dest)
    forms = dest / 'forms.csv'
    lines = forms.read_text(encoding='utf-8').splitlines()
    header, rows = lines[0], lines[1:]
    kept = [line for line in rows if not line.startswith('F1,')]
    forms.write_text('\n'.join([header, *kept]) + '\n', encoding='utf-8')


def run_merge(batches, out_name, report_name, prov_name):
    out_dir = ROOT / 'deliverables' / out_name
    missing_report = ROOT / 'reports' / report_name
    provenance = ROOT / 'deliverables' / out_name / 'provenance.tsv'
    missing, _ = merge(batches, MAPPING, out_dir, missing_report,
                       provenance)
    shutil.copy(provenance, ROOT / 'reports' / prov_name)
    return out_dir, missing_report, provenance, missing


def verify(delivery_dir, provenance_path, expected_missing, label,
           baseline_provenance=None, damaged=False):
    """Structural + provenance + foreign-key walk checks for one delivery."""
    results = []

    def check(name, condition, detail=''):
        results.append((name, bool(condition), detail))

    ds = Wordlist.from_metadata(
        delivery_dir / 'Wordlist-metadata.json')
    check('cldf validate() passes', ds.validate())

    forms = {r['ID']: r for r in ds['FormTable'].iterdicts()}
    params = {r['ID']: r for r in ds['ParameterTable'].iterdicts()}
    cogsets = {r['ID']: r for r in ds['CognatesetTable'].iterdicts()}
    cognates = list(ds['CognateTable'].iterdicts())

    # Two F1 / two C1 stay independent even though they spell alike.
    if not damaged:
        check('jia-F1 and yi-F1 are distinct output forms',
              'jia-F1' in forms and 'yi-F1' in forms)
        if 'jia-F1' in forms and 'yi-F1' in forms:
            check('both F1 forms keep their own spelled form',
                  forms['jia-F1']['Form'] ==
                  forms['yi-F1']['Form'] == 'hant')
        check('jia-C1 and yi-C1 are distinct cognate judgements',
              {r['ID'] for r in cognates} >= {'jia-C1', 'yi-C1'})
    else:
        check('jia-F1 is absent from the damaged delivery',
              'jia-F1' not in forms)
        check('yi-F1 still present and never fills the jia gap',
              'yi-F1' in forms)
        check('jia-C1 judgement skipped, yi-C1 intact',
              'jia-C1' not in {r['ID'] for r in cognates} and
              'yi-C1' in {r['ID'] for r in cognates})

    # Only the explicitly mapped P1 collapses to one shared concept.
    shared_id = 'param-shared-hand-P1'
    check('P1 merges into the single mapped concept', shared_id in params)
    check('yi P3 keeps its own concept identity', 'yi-P3' in params)
    if 'yi-P3' in params:
        check('yi-P3 stays named "hand" but separate',
              params['yi-P3']['Name'] == 'hand')
    check('jia P2 keeps its own concept identity', 'jia-P2' in params)
    if shared_id in params:
        check('yi-F1 points at the shared P1 concept',
              forms.get('yi-F1', {}).get('Parameter_ID') == shared_id)
        if not damaged:
            check('jia-F1 points at the shared P1 concept',
                  forms['jia-F1']['Parameter_ID'] == shared_id)
    if 'yi-F3' in forms:
        check('yi-F3 points at the unconfirmed separate concept',
              forms['yi-F3']['Parameter_ID'] == 'yi-P3')

    # Cognate sets never merge across batches.
    check('cognate sets stay batch-specific',
          {'jia-S1', 'jia-S2', 'yi-S1', 'yi-S3'} <= set(cogsets))

    # Walk every cognate judgement through its foreign keys (re-open path).
    orm_ds = Wordlist.from_metadata(
        delivery_dir / 'Wordlist-metadata.json')
    for cog in orm_ds.objects('CognateTable'):
        form = cog.form
        cogset = cog.cognateset
        parameter = form.parameter
        check(
            f'FK walk {cog.id}: cognate -> form -> parameter',
            form is not None and parameter is not None,
            f'{cog.id} -> {form.id if form else None} -> '
            f'{parameter.id if parameter else None}')
        check(f'FK walk {cog.id}: cognate -> cognateset',
              cogset is not None,
              f'{cog.id} -> {cogset.id if cogset else None}')
        # A judgement may only link same-batch form and cognateset.
        batch = cog.id.split('-', 1)[0]
        check(
            f'{cog.id} stays inside its source batch {batch}',
            form.id.startswith(batch + '-') and
            cogset.id.startswith(batch + '-'))

    # Reverse lookup: output ID -> source batch + original ID, and the
    # original record must exist in the named input with the same form.
    prov_rows = read_tsv(provenance_path)
    # The shared concept is one output row with two provenance rows; every
    # other output row has exactly one provenance row.
    output_row_count = sum(
        len(list(ds[t].iterdicts()))
        for t in ('FormTable', 'ParameterTable',
                  'CognateTable', 'CognatesetTable'))
    check('provenance covers every output row (shared concept twice)',
          len(prov_rows) == output_row_count + 1)
    input_sets = {
        'jia': Wordlist.from_metadata(
            ROOT / 'inputs' / 'jia' / 'cldf' / 'Wordlist-metadata.json'),
        'yi': Wordlist.from_metadata(
            ROOT / 'inputs' / 'yi' / 'cldf' / 'Wordlist-metadata.json'),
    }
    for row in prov_rows:
        if row['table'] != 'FormTable':
            continue
        src = input_sets[row['source_batch']]
        id_col = src[src['FormTable'],
                     'http://cldf.clld.org/v1.0/terms.rdf#id'].name
        form_col = src[src['FormTable'],
                       'http://cldf.clld.org/v1.0/terms.rdf#form'].name
        orig = src.get_row('FormTable', row['source_id'])
        check(
            f"traceback {row['output_id']} -> "
            f"{row['source_batch']}:{row['source_id']}",
            orig[id_col] == row['source_id'] and
            forms[row['output_id']]['Form'] == orig[form_col])

    # Dangling-reference report.
    missing_rows = read_tsv(
        ROOT / 'reports' / f'missing_{label}.tsv')
    check('missing-reference count matches expectation',
          len(missing_rows) == len(expected_missing))
    for expected in expected_missing:
        check(
            f"missing report contains {expected['batch']} "
            f"{expected['table']} {expected['row_id']} -> "
            f"{expected['missing_table']} {expected['missing_id']}",
            any(all(row[k] == v for k, v in expected.items())
                for row in missing_rows))

    if baseline_provenance is not None:
        base = read_tsv(baseline_provenance)
        check('provenance identical after loading-order swap',
              base == prov_rows)

    return results


def main():
    build_jia(ROOT / 'inputs' / 'jia' / 'cldf')
    build_yi(ROOT / 'inputs' / 'yi' / 'cldf')

    dir_a, _, prov_a, _ = run_merge(
        [('jia', ROOT / 'inputs' / 'jia' / 'cldf'),
         ('yi', ROOT / 'inputs' / 'yi' / 'cldf')],
        'merge_jia_yi', 'missing_jia_yi.tsv',
        'provenance_jia_yi.tsv')
    dir_b, _, prov_b, _ = run_merge(
        [('yi', ROOT / 'inputs' / 'yi' / 'cldf'),
         ('jia', ROOT / 'inputs' / 'jia' / 'cldf')],
        'merge_yi_jia', 'missing_yi_jia.tsv',
        'provenance_yi_jia.tsv')

    # The two full deliveries must be byte-identical apart from dir name.
    same = True
    for fname in ('forms.csv', 'parameters.csv', 'cognates.csv',
                  'cognatesets.csv', 'provenance.tsv'):
        if not filecmp.cmp(dir_a / fname, dir_b / fname, shallow=False):
            same = False
    print(f'order-swap deliveries identical: {same}')

    damaged = ROOT / 'inputs' / 'jia_no_f1' / 'cldf'
    build_damaged_jia(damaged)
    dir_c, _, _, missing_c = run_merge(
        [('jia', damaged),
         ('yi', ROOT / 'inputs' / 'yi' / 'cldf')],
        'merge_damaged', 'missing_damaged.tsv',
        'provenance_damaged.tsv')

    expected_missing_damaged = [{
        'batch': 'jia', 'table': 'CognateTable', 'row_id': 'C1',
        'missing_table': 'FormTable', 'missing_id': 'F1',
    }]

    report_lines = []
    for label, directory, prov, expected, damaged_flag in (
            ('jia_yi', dir_a, prov_a, [], False),
            ('yi_jia', dir_b, prov_b, [], False),
            ('damaged', dir_c,
             ROOT / 'reports' / 'provenance_damaged.tsv',
             expected_missing_damaged, True)):
        baseline = prov_a if label == 'yi_jia' else None
        results = verify(directory, prov, expected, label, baseline,
                         damaged=damaged_flag)
        report_lines.append(f'## delivery {label}')
        for name, ok, detail in results:
            mark = 'PASS' if ok else 'FAIL'
            report_lines.append(f'[{mark}] {name}' +
                                (f' ({detail})' if detail else ''))
        report_lines.append('')

    # Extra damaged-delivery assertions: yi-F1 must not fill jia's gap.
    ds_c = Wordlist.from_metadata(
        dir_c / 'Wordlist-metadata.json')
    forms_c = {r['ID']: r for r in ds_c['FormTable'].iterdicts()}
    cognates_c = {r['ID']: r for r in
                  ds_c['CognateTable'].iterdicts()}
    extra = [
        ('jia-F1 absent from damaged delivery', 'jia-F1' not in forms_c),
        ('yi-F1 still present, not used as replacement',
         'yi-F1' in forms_c),
        ('jia-C1 judgement skipped, not rewired to yi-F1',
         'jia-C1' not in cognates_c),
        ('yi-C1 judgement intact', 'yi-C1' in cognates_c),
    ]
    report_lines.append('## damaged-delivery gap handling')
    for name, ok in extra:
        report_lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}")
    report_lines.append('')

    # Source -> output ID correspondence must be consistent across the
    # full and the damaged delivery for every record still delivered.
    prov_full = {
        (r['table'], r['output_id'], r['source_batch'], r['source_id'])
        for r in read_tsv(prov_a)}
    prov_damaged = {
        (r['table'], r['output_id'], r['source_batch'], r['source_id'])
        for r in read_tsv(
            ROOT / 'reports' / 'provenance_damaged.tsv')}
    consistent = prov_damaged.issubset(prov_full)
    report_lines.append('## cross-delivery stability')
    report_lines.append(
        f"[{'PASS' if consistent else 'FAIL'}] every damaged-delivery "
        'provenance row uses the same source->output ID mapping')

    report_path = ROOT / 'reports' / 'verification.txt'
    report_path.write_text('\n'.join(report_lines) + '\n',
                           encoding='utf-8')
    failed = [line for line in report_lines if '[FAIL]' in line]
    print(f'verification report: {report_path}')
    print(f'checks failed: {len(failed)}')
    if failed or not same:
        sys.exit(1)


if __name__ == '__main__':
    main()
