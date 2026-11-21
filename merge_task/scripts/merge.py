"""Merge two dialect CLDF Wordlist batches with full provenance tracking.

Stable ID rules (independent of input loading order):

* every per-batch record keeps its identity under the output ID
  ``"<batch>-<original ID>"`` (e.g. ``jia-F1``, ``yi-C1``);
* only concepts listed in the explicit mapping are merged, and their
  output ID is the fixed ``output_id`` from that mapping;
* same-spelling IDs from different batches never collapse on their own.

The merge resolves every reference column by its CLDF ``propertyUrl``
(not its local header name), so batch-local column names such as
``Beleg`` or ``Verwandtschaftsgruppe`` are handled correctly.

Dangling references are reported with their owning batch and original ID;
the affected row is skipped.  A missing row is never repaired with data
from the other batch.
"""
import argparse
import csv
import json
import pathlib

from pycldf import Wordlist

TERMS = {
    'id': 'http://cldf.clld.org/v1.0/terms.rdf#id',
    'name': 'http://cldf.clld.org/v1.0/terms.rdf#name',
    'description': 'http://cldf.clld.org/v1.0/terms.rdf#description',
    'form': 'http://cldf.clld.org/v1.0/terms.rdf#form',
    'languageReference':
        'http://cldf.clld.org/v1.0/terms.rdf#languageReference',
    'parameterReference':
        'http://cldf.clld.org/v1.0/terms.rdf#parameterReference',
    'formReference':
        'http://cldf.clld.org/v1.0/terms.rdf#formReference',
    'cognatesetReference':
        'http://cldf.clld.org/v1.0/terms.rdf#cognatesetReference',
    'segments': 'http://cldf.clld.org/v1.0/terms.rdf#segments',
    'comment': 'http://cldf.clld.org/v1.0/terms.rdf#comment',
    'source': 'http://cldf.clld.org/v1.0/terms.rdf#source',
    'segmentSlice':
        'http://cldf.clld.org/v1.0/terms.rdf#segmentSlice',
    'alignment': 'http://cldf.clld.org/v1.0/terms.rdf#alignment',
    'columnSpec': 'http://cldf.clld.org/v1.0/terms.rdf#columnSpec',
}

TABLES = ['ParameterTable', 'FormTable', 'CognatesetTable', 'CognateTable']


def colname(ds, table, term):
    """Local header name of the column carrying a CLDF term."""
    return ds[(table, TERMS[term])].name


def read_rows(ds, table):
    """All rows of a table as plain dicts."""
    return list(ds[table].iterdicts())


def stable_id(batch, original_id):
    """Order-independent output ID for a batch-local record."""
    return f'{batch}-{original_id}'


def load_mapping(path):
    """Return (shared output id -> sorted list of (batch, orig id))."""
    data = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    shared = {}
    for entry in data['shared_parameters']:
        members = sorted((m['batch'], m['id']) for m in entry['members'])
        shared[entry['output_id']] = members
    return shared


def merge(batch_specs, mapping_path, out_dir, missing_report_path,
          provenance_path):
    """Merge the batches given as ``[(batch_id, cldf_dir), ...]``.

    The output only depends on the *set* of batches, not on their order.
    """
    shared = load_mapping(mapping_path)

    # Reverse lookup: (batch, orig parameter id) -> shared output id.
    param_to_shared = {}
    for out_id, members in shared.items():
        for member in members:
            param_to_shared[member] = out_id

    # Always process batches in a fixed (alphabetical) order so that any
    # field-level conflict resolution is loading-order independent.
    batch_specs = sorted(batch_specs, key=lambda spec: spec[0])
    datasets = {
        batch: Wordlist.from_metadata(
            pathlib.Path(directory) / 'Wordlist-metadata.json')
        for batch, directory in batch_specs
    }

    missing = []          # dangling-reference report rows
    provenance = []       # output id -> source batch / original id

    def record_provenance(table, out_id, batch, orig_id):
        provenance.append({
            'table': table, 'output_id': out_id,
            'source_batch': batch, 'source_id': orig_id,
        })

    # ---- collect per-batch rows, keyed by batch -----------------------
    raw = {batch: {table: read_rows(ds, table)
                   for table in TABLES}
           for batch, ds in datasets.items()}

    # ---- ParameterTable ----------------------------------------------
    param_rows_out = []
    seen_params = set()
    for batch in sorted(raw):
        ds = datasets[batch]
        id_col = colname(ds, 'ParameterTable', 'id')
        name_col = colname(ds, 'ParameterTable', 'name')
        desc_col = colname(ds, 'ParameterTable', 'description')
        spec_col = colname(ds, 'ParameterTable', 'columnSpec')
        for row in raw[batch]['ParameterTable']:
            orig_id = row[id_col]
            shared_id = param_to_shared.get((batch, orig_id))
            if shared_id:
                if shared_id in seen_params:
                    # Provenance for all members was recorded when the
                    # shared row was first emitted.
                    continue
                seen_params.add(shared_id)
                out_id = shared_id
                # Multi-valued provenance columns, order fixed by batch.
                members = shared[shared_id]
                src_batches = [m[0] for m in members]
                src_ids = [m[1] for m in members]
            else:
                out_id = stable_id(batch, orig_id)
                seen_params.add(out_id)
                src_batches, src_ids = [batch], [orig_id]
            param_rows_out.append({
                'ID': out_id,
                'Name': row[name_col],
                'Description': row[desc_col],
                'ColumnSpec': row.get(spec_col),
                'Source_Batch': src_batches,
                'Source_ID': src_ids,
            })
            if shared_id:
                for member_batch, member_id in shared[shared_id]:
                    record_provenance(
                        'ParameterTable', shared_id,
                        member_batch, member_id)
            else:
                record_provenance('ParameterTable', out_id, batch, orig_id)

    # ---- CognatesetTable (never merged across batches) ---------------
    cogset_rows_out = []
    available_cogsets = {}   # (batch, orig id) -> output id
    for batch in sorted(raw):
        ds = datasets[batch]
        id_col = colname(ds, 'CognatesetTable', 'id')
        desc_col = colname(ds, 'CognatesetTable', 'description')
        for row in raw[batch]['CognatesetTable']:
            orig_id = row[id_col]
            out_id = stable_id(batch, orig_id)
            available_cogsets[(batch, orig_id)] = out_id
            cogset_rows_out.append({
                'ID': out_id,
                'Description': row[desc_col],
                'Source_Batch': [batch],
                'Source_ID': [orig_id],
            })
            record_provenance('CognatesetTable', out_id, batch, orig_id)

    # ---- FormTable ----------------------------------------------------
    form_rows_out = []
    available_forms = {}      # (batch, orig id) -> output id
    form_param_ref = {}       # output form id -> output parameter id
    for batch in sorted(raw):
        ds = datasets[batch]
        id_col = colname(ds, 'FormTable', 'id')
        lang_col = colname(ds, 'FormTable', 'languageReference')
        param_col = colname(ds, 'FormTable', 'parameterReference')
        form_col = colname(ds, 'FormTable', 'form')
        seg_col = colname(ds, 'FormTable', 'segments')
        comment_col = colname(ds, 'FormTable', 'comment')
        source_col = colname(ds, 'FormTable', 'source')
        batch_param_ids = {
            row[colname(ds, 'ParameterTable', 'id')]
            for row in raw[batch]['ParameterTable']}
        for row in raw[batch]['FormTable']:
            orig_id = row[id_col]
            orig_param = row[param_col]
            if orig_param not in batch_param_ids:
                missing.append({
                    'batch': batch, 'table': 'FormTable',
                    'row_id': orig_id,
                    'missing_table': 'ParameterTable',
                    'missing_id': orig_param,
                    'problem': f'missing parameter {orig_param} in batch {batch}',
                })
                continue
            out_id = stable_id(batch, orig_id)
            out_param = param_to_shared.get(
                (batch, orig_param), stable_id(batch, orig_param))
            available_forms[(batch, orig_id)] = out_id
            form_param_ref[out_id] = out_param
            form_rows_out.append({
                'ID': out_id,
                'Language_ID': row[lang_col],
                'Parameter_ID': out_param,
                'Form': row[form_col],
                'Segments': row[seg_col],
                'Comment': row[comment_col],
                'Source': row[source_col],
                'Source_Batch': [batch],
                'Source_ID': [orig_id],
            })
            record_provenance('FormTable', out_id, batch, orig_id)

    # ---- CognateTable -------------------------------------------------
    cognate_rows_out = []
    for batch in sorted(raw):
        ds = datasets[batch]
        id_col = colname(ds, 'CognateTable', 'id')
        form_col = colname(ds, 'CognateTable', 'formReference')
        cogset_col = colname(ds, 'CognateTable', 'cognatesetReference')
        slice_col = colname(ds, 'CognateTable', 'segmentSlice')
        align_col = colname(ds, 'CognateTable', 'alignment')
        source_col = colname(ds, 'CognateTable', 'source')
        for row in raw[batch]['CognateTable']:
            orig_id = row[id_col]
            orig_form = row[form_col]
            orig_cogset = row[cogset_col]
            # References must resolve *within the same batch*.
            if (batch, orig_form) not in available_forms:
                missing.append({
                    'batch': batch, 'table': 'CognateTable',
                    'row_id': orig_id,
                    'missing_table': 'FormTable',
                    'missing_id': orig_form,
                    'problem':
                        f'form {orig_form} missing in batch {batch}; '
                        f'cognate judgement {orig_id} skipped',
                })
                continue
            if (batch, orig_cogset) not in available_cogsets:
                missing.append({
                    'batch': batch, 'table': 'CognateTable',
                    'row_id': orig_id,
                    'missing_table': 'CognatesetTable',
                    'missing_id': orig_cogset,
                    'problem':
                        f'cognateset {orig_cogset} missing in batch {batch}; '
                        f'cognate judgement {orig_id} skipped',
                })
                continue
            out_id = stable_id(batch, orig_id)
            cognate_rows_out.append({
                'ID': out_id,
                'Form_ID': available_forms[(batch, orig_form)],
                'Cognateset_ID': available_cogsets[(batch, orig_cogset)],
                'Segment_Slice': row[slice_col],
                'Alignment': row[align_col],
                'Source': row[source_col],
                'Source_Batch': [batch],
                'Source_ID': [orig_id],
            })
            record_provenance('CognateTable', out_id, batch, orig_id)

    # ---- deterministic ordering --------------------------------------
    param_rows_out.sort(key=lambda r: r['ID'])
    cogset_rows_out.sort(key=lambda r: r['ID'])
    form_rows_out.sort(key=lambda r: r['ID'])
    cognate_rows_out.sort(key=lambda r: r['ID'])
    provenance.sort(key=lambda r: (r['table'], r['output_id'],
                                   r['source_batch'], r['source_id']))
    missing.sort(key=lambda r: (r['batch'], r['table'], r['row_id']))

    _write_output(out_dir, param_rows_out, form_rows_out,
                  cogset_rows_out, cognate_rows_out)
    _write_tsv(missing_report_path, missing,
               ['batch', 'table', 'row_id', 'missing_table',
                'missing_id', 'problem'])
    _write_tsv(provenance_path, provenance,
               ['table', 'output_id', 'source_batch', 'source_id'])
    return missing, provenance


def _write_output(out_dir, params, forms, cogsets, cognates):
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ds = Wordlist.in_dir(out_dir)
    ds.add_component('ParameterTable')
    ds.add_component('CognatesetTable')
    ds.add_component('CognateTable')
    for table in TABLES:
        ds.add_columns(table, {
            'name': 'Source_Batch', 'datatype': 'string',
            'separator': ';', 'required': True})
        ds.add_columns(table, {
            'name': 'Source_ID', 'datatype': 'string',
            'separator': ';', 'required': True})
    ds.write(
        ParameterTable=params,
        FormTable=forms,
        CognatesetTable=cogsets,
        CognateTable=cognates,
    )


def _write_tsv(path, rows, fieldnames):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames,
                                delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--batch', action='append', nargs=2,
                        metavar=('BATCH_ID', 'CLDF_DIR'), required=True,
                        help='input batch, repeatable; order is ignored')
    parser.add_argument('--mapping', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--missing-report', required=True)
    parser.add_argument('--provenance', required=True)
    args = parser.parse_args()
    missing, _ = merge(
        args.batch, args.mapping, args.out,
        args.missing_report, args.provenance)
    print(f'merged into {args.out}; {len(missing)} dangling reference(s)')


if __name__ == '__main__':
    main()
