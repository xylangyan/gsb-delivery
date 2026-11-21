import pytest

from pycldf import Wordlist
from pycldf.alignment import (
    GAP, Correction, AlignmentError, AlignmentCorrectionError)


@pytest.fixture
def ds_wl_alignment(tmp_path):
    """A wordlist with two forms aligned in one cognateset.

    The CognateTable columns are renamed via metadata, and the formReference
    points to a local key of the FormTable which differs from the CLDF id.
    """
    ds = Wordlist.in_dir(tmp_path)
    ds.add_columns('FormTable', 'Local_ID')
    ds['FormTable', 'Segments'].separator = '+'
    ds.add_component('CognateTable')
    ds.rename_column('CognateTable', 'Form_ID', 'Form')
    ds.rename_column('CognateTable', 'Cognateset_ID', 'Set')
    ds.rename_column('CognateTable', 'Alignment', 'Aligned')
    # Let the formReference point to the local key, not the CLDF id:
    for fk in ds['CognateTable'].tableSchema.foreignKeys:
        if fk.columnReference == ['Form']:
            fk.reference.columnReference = ['Local_ID']
    ds.write(
        FormTable=[
            {'ID': 'f1', 'Local_ID': 'A', 'Form': 'xaxa', 'Segments': ['x a', 'x'],
             'Language_ID': 'l', 'Parameter_ID': 'p'},
            {'ID': 'f2', 'Local_ID': 'B', 'Form': 'beko', 'Segments': ['b e', 'k o'],
             'Language_ID': 'l', 'Parameter_ID': 'p'},
        ],
        CognateTable=[
            # Two identical segments "x", aligned in different columns, gap between:
            {'ID': 'c1', 'Form': 'A', 'Set': 's1', 'Aligned': ['x', 'a', '-', 'x', '-']},
            # Same column structure, gap elsewhere:
            {'ID': 'c2', 'Form': 'B', 'Set': 's1', 'Aligned': ['b', 'e', 'k', '-', 'o']},
        ])
    return ds


def run_review(ds, forms=None):
    review = ds.review_alignment('s1', forms=forms)
    # Insert a column into the common alignment and move the second "x" of form A
    # (now in column 4) into the new column; form B keeps a gap in the new column:
    review.insert_column(3)
    review.move_segment(0, 4, 3)
    review.apply_corrections([
        # Old batch, addressed by pre-insertion column number, pointing to a gap of B:
        Correction(row=1, column=3, base='original', value=GAP),
        # Addressed by morpheme cell and segment within the cell of B's form:
        Correction(row=1, source=(1, 1), value='o̯'),
        # Addressed by alignment column in the changed, current numbering:
        Correction(row=1, column=0, value='b̪'),
    ])
    return review


def test_review_alignment_implicit_forms(ds_wl_alignment):
    result = run_review(ds_wl_alignment).verify()
    # Inserting a column did not re-bind old column numbers to another column:
    assert result.columns == (0, 1, 2, None, 3, 4)
    # Both rows still have the same number of alignment columns:
    assert len(result.rows) == 2
    assert all(len(row.cells) == len(result.columns) for row in result.rows)
    row_a, row_b = result.rows
    assert row_a.cells == ('x', 'a', '-', 'x', '-', '-')
    assert row_b.cells == ('b̪', 'e', 'k', '-', '-', 'o̯')
    # Each non-gap cell traces back to morpheme, segment-in-morpheme and flat index.
    # The two identical "x" of form A keep their distinct provenance, the moved one
    # included; the gap of B did not consume a segment number:
    assert row_a.positions == ((0, 0, 0), (0, 1, 1), None, (1, 0, 2), None, None)
    assert row_b.positions == ((0, 0, 0), (0, 1, 1), (1, 0, 2), None, None, (1, 1, 3))


def test_review_alignment_explicit_forms(ds_wl_alignment):
    forms = {row['Local_ID']: row for row in ds_wl_alignment['FormTable']}
    cognates = list(ds_wl_alignment['CognateTable'])
    explicit = run_review(
        ds_wl_alignment, forms=[forms[c['Form']] for c in cognates]).verify()
    implicit = run_review(ds_wl_alignment).verify()
    assert explicit == implicit


def test_review_alignment_column_identity(ds_wl_alignment):
    review = ds_wl_alignment.review_alignment('s1')
    assert review.resolve_column(3, 'original') == 3
    review.insert_column(3)
    # Old column numbers still identify the same columns after the insertion:
    assert review.resolve_column(3, 'original') == 4
    assert review.resolve_column(3, 'current') == 3
    with pytest.raises(AlignmentError):
        review.resolve_column(5, 'original')
    with pytest.raises(AlignmentError):
        review.resolve_column(6, 'current')


def test_review_alignment_correction_out_of_bounds(ds_wl_alignment):
    review = ds_wl_alignment.review_alignment('s1')
    review.insert_column(3)
    with pytest.raises(AlignmentCorrectionError) as e:
        review.apply_corrections([Correction(row=1, column=9, value='z')])
    assert e.value.correction == 0
    assert e.value.coordinate == 'current column'
    with pytest.raises(AlignmentCorrectionError) as e:
        review.apply_corrections([Correction(row=1, column=9, base='original', value='z')])
    assert e.value.coordinate == 'original column'
    with pytest.raises(AlignmentCorrectionError) as e:
        review.apply_corrections([Correction(row=1, source=(5, 0), value='z')])
    assert e.value.coordinate == 'source'


def test_review_alignment_correction_conflict(ds_wl_alignment):
    review = ds_wl_alignment.review_alignment('s1')
    review.insert_column(3)
    review.move_segment(0, 4, 3)
    # An old annotation still binding "x" to the original column 3 conflicts with
    # the moved segment, because a gap cannot be filled by column address:
    with pytest.raises(AlignmentCorrectionError) as e:
        review.apply_corrections([Correction(row=0, column=3, base='original', value='x')])
    assert e.value.correction == 0
    assert e.value.coordinate == 'original column'
    # Two corrections targeting the same cell conflict:
    with pytest.raises(AlignmentCorrectionError) as e:
        review.apply_corrections([
            Correction(row=1, column=0, value='b'),
            Correction(row=1, column=0, value='p'),
        ])
    assert e.value.correction == 1
    # Ambiguous addresses are rejected:
    with pytest.raises(AlignmentCorrectionError):
        review.apply_corrections([Correction(row=1, column=0, source=(0, 0), value='z')])


def test_review_alignment_corrections_are_atomic(ds_wl_alignment):
    review = ds_wl_alignment.review_alignment('s1')
    before = review.verify()
    with pytest.raises(AlignmentCorrectionError):
        review.apply_corrections([
            Correction(row=1, column=0, value='b̪'),
            Correction(row=1, column=99, value='z'),
        ])
    # No partial merge result is delivered:
    assert review.verify() == before


def test_review_alignment_row_and_column_errors(ds_wl_alignment):
    review = ds_wl_alignment.review_alignment('s1')
    with pytest.raises(AlignmentError):
        review.insert_column(9)
    with pytest.raises(AlignmentError):
        review.move_segment(0, 2, 3)  # no segment in column 2
    with pytest.raises(AlignmentError):
        review.move_segment(0, 0, 1)  # column 1 is occupied
    with pytest.raises(AlignmentError):
        review.move_segment('cx', 0, 1)
    # Rows can also be addressed by cognate ID:
    review.move_segment('c1', 3, 4)
    assert review.verify().rows[0].cells == ('x', 'a', '-', '-', 'x')


def test_review_alignment_source_coordinate_not_aligned(ds_wl_alignment):
    review = ds_wl_alignment.review_alignment('s1')
    review.apply_corrections([Correction(row=0, column=0, value=GAP)])
    with pytest.raises(AlignmentCorrectionError) as e:
        review.apply_corrections([Correction(row=0, source=(0, 0), value='y')])
    assert e.value.coordinate == 'source'


def test_review_alignment_mismatched_forms(ds_wl_alignment):
    with pytest.raises(ValueError):
        ds_wl_alignment.review_alignment('s1', forms=[])
