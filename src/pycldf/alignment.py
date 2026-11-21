"""
Support for reviewing and editing alignments of cognate judgements in wordlists.

An alignment (as stored in a `CognateTable`'s `alignment` column) distributes the
segments of a form over the columns of a matrix shared by all judgements in a
cognateset, using a gap symbol for columns where the form has no counterpart.
Reviewing such an alignment - e.g. to reconcile an older batch of morpheme
annotations with new errata for the pronunciation segments - requires tracking

- the identity of alignment columns across structural edits (inserting a column
  must not re-bind column numbers of pre-existing annotations to another column),
- the provenance of each non-gap cell, i.e. which segment of the source form it
  represents (identified by position, never guessed from identical spellings),
- corrections addressed in different coordinate systems: alignment columns in
  pre-edit ("original") or current numbering, or source coordinates given as
  (morpheme, segment within morpheme) of the form.
"""
import dataclasses
from typing import Optional, Union, Any

__all__ = [
    'GAP',
    'Correction',
    'AlignmentError',
    'AlignmentCorrectionError',
    'AlignedRow',
    'AlignmentResult',
    'AlignmentReview',
]

GAP = '-'
"""The symbol marking gaps in alignments."""


class AlignmentError(ValueError):
    """Raised when an alignment review encounters inconsistent data."""


class AlignmentCorrectionError(AlignmentError):
    """
    Raised when a correction cannot be applied to an alignment.

    The failing correction (specified as index into the list of corrections) and
    the kind of coordinate which could not be resolved are available as
    `correction` and `coordinate` attributes. Since corrections are applied
    atomically, no partial merge result is left behind when this error is raised.
    """
    def __init__(self, correction: int, coordinate: str, message: str):
        self.correction = correction
        self.coordinate = coordinate
        super().__init__(
            'correction #{} ({} coordinate): {}'.format(correction, coordinate, message))


@dataclasses.dataclass
class Correction:
    """
    A correction to one cell of one row of an alignment.

    The target cell is addressed either by alignment column - in `current` numbering
    or in `original`, i.e. pre-edit, numbering - or by `source` coordinates, a pair
    (morpheme index, segment index within the morpheme) locating the segment in the
    source form. Exactly one of `column` and `source` must be given.
    """
    row: Union[int, str]
    value: str
    column: Optional[int] = None
    base: str = 'current'
    source: Optional[tuple] = None

    def coordinate_kind(self) -> str:
        if self.source is not None:
            return 'source'
        return '{} column'.format(self.base)


@dataclasses.dataclass(frozen=True)
class AlignedRow:
    """
    One reviewed row of an alignment.

    `cells` holds the segment (or `GAP`) of each alignment column; `positions`
    holds, for each non-gap cell, the provenance of the segment in the source form
    as triple (morpheme index, segment index within the morpheme, flat segment
    index) - and `None` for gaps.
    """
    cognate: Any
    form: Any
    cells: tuple
    positions: tuple


@dataclasses.dataclass(frozen=True)
class AlignmentResult:
    """
    The verified outcome of an alignment review.

    `columns` maps each current alignment column to the column number it had in the
    original annotation - or `None` for columns inserted during the review.
    """
    cognateset: Any
    columns: tuple
    rows: tuple


@dataclasses.dataclass(frozen=True)
class _Cell:
    value: str
    position: tuple  # (morpheme index, segment index within the morpheme)


@dataclasses.dataclass
class _RowState:
    cognate: Any
    form: Any
    morphemes: list  # List of morphemes, each a list of segment strings.
    cells: list  # List of Optional[_Cell], one per alignment column.
    id: Optional[str] = None

    @property
    def flat_positions(self):
        return [(m, i) for m, morpheme in enumerate(self.morphemes)
                for i in range(len(morpheme))]

    def flat_index(self, position):
        m, i = position
        return sum(len(morpheme) for morpheme in self.morphemes[:m]) + i


class AlignmentReview:
    """
    A review session for the alignment of one cognateset.

    Typically instantiated via :meth:`pycldf.dataset.Wordlist.review_alignment`.
    """
    def __init__(self, rows, cognateset=None):
        """
        :param rows: Iterable of tuples `(cognate, form, morphemes, alignment, row_id)`, \
        where `morphemes` is the list of morphemes of the form (each a list of segment \
        strings) and `alignment` the list of alignment cells (`GAP` for gaps).
        """
        self.cognateset = cognateset
        self._origins = None
        self._rows = []
        for cognate, form, morphemes, alignment, row_id in alignment_rows(rows):
            cells = []
            flat = [(m, i) for m, morpheme in enumerate(morphemes)
                    for i in range(len(morpheme))]
            nseg = 0
            for value in alignment:
                if value == GAP:
                    cells.append(None)
                else:
                    if nseg >= len(flat):
                        raise AlignmentError(
                            'alignment of form {!r} has more segments than the form'.format(
                                row_id))
                    # Provenance is assigned by position in the segment sequence,
                    # never guessed from (possibly identical) spellings.
                    cells.append(_Cell(value, flat[nseg]))
                    nseg += 1
            if self._origins is None:
                self._origins = list(range(len(cells)))
            elif len(cells) != len(self._origins):
                raise AlignmentError(
                    'alignment rows differ in column count: {} != {}'.format(
                        len(cells), len(self._origins)))
            self._rows.append(_RowState(cognate, form, morphemes, cells, row_id))
        if self._origins is None:
            self._origins = []

    @property
    def columns(self) -> tuple:
        """The original column number of each current column (`None` if inserted)."""
        return tuple(self._origins)

    def __len__(self):
        return len(self._rows)

    def _row_index(self, row) -> int:
        if isinstance(row, int):
            if 0 <= row < len(self._rows):
                return row
            raise AlignmentError('no row #{}'.format(row))
        for i, state in enumerate(self._rows):
            if state.id == row:
                return i
        raise AlignmentError('no row with ID {!r}'.format(row))

    def resolve_column(self, column: int, base: str = 'current') -> int:
        """
        Resolve a column number to a current column index.

        :param column: The column number to resolve.
        :param base: `current` for a number in the current alignment, `original` \
        for a number referring to the alignment as it was before the review \
        (e.g. from an older batch of annotations).
        """
        if base == 'current':
            if 0 <= column < len(self._origins):
                return column
            raise AlignmentError('column {} out of bounds'.format(column))
        if base == 'original':
            if column in self._origins:
                return self._origins.index(column)
            raise AlignmentError('no column with original number {}'.format(column))
        raise AlignmentError('invalid column numbering base: {!r}'.format(base))

    def insert_column(self, at: int) -> None:
        """
        Insert a gap column into the common alignment.

        The new column has no original column number; the identity of all
        pre-existing columns (and thus the meaning of column numbers used by
        older annotations) is preserved.
        """
        if not 0 <= at <= len(self._origins):
            raise AlignmentError('cannot insert column at {}'.format(at))
        self._origins.insert(at, None)
        for row in self._rows:
            row.cells.insert(at, None)

    def move_segment(self, row, column: int, to_column: int) -> None:
        """
        Move the segment of a row from one alignment column to another.

        The segment keeps its provenance; column identities are not affected.
        """
        state = self._rows[self._row_index(row)]
        column = self.resolve_column(column, 'current')
        to_column = self.resolve_column(to_column, 'current')
        cell = state.cells[column]
        if cell is None:
            raise AlignmentError('no segment in column {}'.format(column))
        if state.cells[to_column] is not None:
            raise AlignmentError('column {} is occupied'.format(to_column))
        state.cells[to_column] = cell
        state.cells[column] = None

    def apply_corrections(self, corrections) -> None:
        """
        Apply a batch of corrections to the alignment.

        Corrections are applied atomically: if any correction cannot be applied,
        an :class:`AlignmentCorrectionError` is raised identifying the correction
        and the kind of coordinate that failed, and the alignment is left unchanged.
        """
        corrections = [
            c if isinstance(c, Correction) else Correction(**c) for c in corrections]
        new_cells = [list(row.cells) for row in self._rows]
        targets = {}
        for n, correction in enumerate(corrections):
            kind = correction.coordinate_kind()
            try:
                ridx = self._row_index(correction.row)
            except AlignmentError as e:
                raise AlignmentCorrectionError(n, 'row', str(e)) from e
            state = self._rows[ridx]
            cells = new_cells[ridx]
            if correction.source is not None:
                if correction.column is not None:
                    raise AlignmentCorrectionError(
                        n, kind, 'ambiguous address: column and source coordinates given')
                col = self._resolve_source(correction.source, state, cells, n)
            elif correction.column is not None:
                try:
                    col = self.resolve_column(correction.column, correction.base)
                except AlignmentError as e:
                    raise AlignmentCorrectionError(n, kind, str(e)) from e
            else:
                raise AlignmentCorrectionError(n, kind, 'no address given')
            if (ridx, col) in targets:
                raise AlignmentCorrectionError(
                    n, kind,
                    'conflicts with correction #{} targeting the same cell'.format(
                        targets[(ridx, col)]))
            targets[(ridx, col)] = n
            cell = cells[col]
            if correction.value == GAP:
                cells[col] = None
            elif cell is None:
                # A gap can only be filled through source coordinates, which carry
                # the provenance of the new segment. Filling a gap addressed by
                # column number would require guessing the source segment.
                raise AlignmentCorrectionError(
                    n, kind,
                    'cannot assign a segment to gap column {} without source '
                    'coordinates'.format(col))
            else:
                cells[col] = dataclasses.replace(cell, value=correction.value)
        for state, cells in zip(self._rows, new_cells):
            state.cells = cells

    def _resolve_source(self, source, state, cells, n) -> int:
        try:
            m, i = source
        except (TypeError, ValueError):
            raise AlignmentCorrectionError(
                n, 'source', 'invalid source coordinates: {!r}'.format(source)) from None
        if not (0 <= m < len(state.morphemes)) \
                or not (0 <= i < len(state.morphemes[m])):
            raise AlignmentCorrectionError(
                n, 'source',
                'source coordinates {} out of bounds for form {!r}'.format(
                    (m, i), state.id))
        for j, cell in enumerate(cells):
            if cell is not None and cell.position == (m, i):
                return j
        raise AlignmentCorrectionError(
            n, 'source',
            'segment {} of form {!r} is not aligned'.format((m, i), state.id))

    def verify(self) -> AlignmentResult:
        """
        Verify the reviewed alignment and return the result.

        Checks that all rows still have the same number of alignment columns and
        that each non-gap cell can be traced back to a unique segment of the source
        form, identified by morpheme index, segment index within the morpheme and
        flat segment index.
        """
        rows = []
        for state in self._rows:
            if len(state.cells) != len(self._origins):
                raise AlignmentError(
                    'row {!r} has {} columns, expected {}'.format(
                        state.id, len(state.cells), len(self._origins)))
            seen, values, positions = set(), [], []
            for cell in state.cells:
                if cell is None:
                    values.append(GAP)
                    positions.append(None)
                    continue
                if cell.position in seen:
                    raise AlignmentError(
                        'segment {} of form {!r} is aligned twice'.format(
                            cell.position, state.id))
                seen.add(cell.position)
                m, i = cell.position
                if m >= len(state.morphemes) or i >= len(state.morphemes[m]):
                    raise AlignmentError(
                        'segment {} out of bounds for form {!r}'.format(
                            cell.position, state.id))
                values.append(cell.value)
                positions.append((m, i, state.flat_index(cell.position)))
            rows.append(AlignedRow(state.cognate, state.form, tuple(values), tuple(positions)))
        return AlignmentResult(self.cognateset, tuple(self._origins), tuple(rows))


def alignment_rows(rows):
    """Normalize row specifications passed to `AlignmentReview`."""
    for row in rows:
        cognate, form, morphemes, alignment, row_id = row
        yield cognate, form, [list(m) for m in morphemes], list(alignment), row_id
