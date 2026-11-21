"""
pyexcel.sections
~~~~~~~~~~~~~~~~

Read several composite-header tables from one worksheet.

A section is a rectangular table inside a worksheet. The caller gives
the section name and the two rows that carry its composite header.
Header labels are looked up by path, for example ``原币/价格``. Each
record carries the physical worksheet row and the physical column of
every projected field.

The reader works on the raw worksheet cells and on the workbook's
merged-cell geometry. A merged header value is inherited only by cells
that belong to the same section and to that section's header band. A
merge that starts in one section and ends in another does not name the
second section and does not replace a data cell.

:copyright: (c) 2014-2026 by Onni Software Ltd. and its contributors
:license: New BSD License, see LICENSE for more details
"""

from io import BytesIO

from pyexcel._compact import OrderedDict


HEADER_ROW_COUNT = 2


class SectionError(Exception):
    """Base class for section reader errors"""


class SectionSpecError(SectionError):
    """A section definition is not valid"""


class FieldNotFoundError(SectionError):
    """A requested field path is absent from a section"""

    def __init__(self, section, field, available):
        self.section = section
        self.field = field
        self.available = tuple(available)
        message = "field %r is not available in section %r" % (
            field,
            section,
        )
        if self.available:
            message += "; available paths: %s" % ", ".join(self.available)
        SectionError.__init__(self, message)


class InvalidSelectionError(SectionError):
    """A physical column selection does not match a candidate"""


class AmbiguousFieldError(SectionError):
    """A field path resolves to more than one physical column"""

    def __init__(self, section, field, candidates):
        self.section = section
        self.field = field
        self.candidates = tuple(candidates)
        SectionError.__init__(
            self,
            "field %r in section %r has %d candidates; select one "
            "physical column" % (field, section, len(self.candidates)),
        )


class HeaderCell(object):
    """A cell that contributed a label to a field path"""

    __slots__ = ("row", "column", "value", "merged_range")

    def __init__(self, row, column, value, merged_range=None):
        self.row = row
        self.column = column
        self.value = value
        self.merged_range = merged_range

    @property
    def excel_row(self):
        return self.row + 1

    @property
    def excel_column(self):
        return self.column + 1

    @property
    def column_letter(self):
        return column_letter(self.column)

    def as_dict(self):
        return OrderedDict(
            [
                ("row", self.row),
                ("excel_row", self.excel_row),
                ("column", self.column),
                ("excel_column", self.excel_column),
                ("column_letter", self.column_letter),
                ("value", self.value),
                ("merged_range", self.merged_range),
            ]
        )

    def __repr__(self):
        return "HeaderCell(%r, %r, %r)" % (
            self.row,
            self.column,
            self.value,
        )


class Candidate(object):
    """One physical column that carries a complete field path"""

    __slots__ = (
        "section",
        "field",
        "column",
        "header_rows",
        "labels",
        "cells",
        "merged_ranges",
    )

    def __init__(
        self,
        section,
        field,
        column,
        header_rows,
        labels,
        cells,
        merged_ranges,
    ):
        self.section = section
        self.field = field
        self.column = column
        self.header_rows = tuple(header_rows)
        self.labels = tuple(labels)
        self.cells = tuple(cells)
        self.merged_ranges = tuple(merged_ranges)

    @property
    def excel_column(self):
        return self.column + 1

    @property
    def column_letter(self):
        return column_letter(self.column)

    def as_dict(self):
        return OrderedDict(
            [
                ("section", self.section),
                ("field", self.field),
                ("column", self.column),
                ("excel_column", self.excel_column),
                ("column_letter", self.column_letter),
                ("header_rows", self.header_rows),
                ("labels", self.labels),
                (
                    "cells",
                    [cell.as_dict() for cell in self.cells],
                ),
                ("merged_ranges", self.merged_ranges),
            ]
        )

    def __repr__(self):
        return "Candidate(section=%r, field=%r, column=%r)" % (
            self.section,
            self.field,
            self.column,
        )


class FieldSource(object):
    """Physical location of a projected field value"""

    __slots__ = ("row", "column", "header_cells", "sheet_name")

    def __init__(self, row, column, header_cells=(), sheet_name=None):
        self.row = row
        self.column = column
        self.header_cells = tuple(header_cells)
        self.sheet_name = sheet_name

    @property
    def excel_row(self):
        return self.row + 1

    @property
    def excel_column(self):
        return self.column + 1

    @property
    def column_letter(self):
        return column_letter(self.column)

    @property
    def cell(self):
        return "%s%s" % (self.column_letter, self.excel_row)

    def as_dict(self):
        return OrderedDict(
            [
                ("sheet_name", self.sheet_name),
                ("row", self.row),
                ("excel_row", self.excel_row),
                ("column", self.column),
                ("excel_column", self.excel_column),
                ("column_letter", self.column_letter),
                ("cell", self.cell),
                (
                    "header_cells",
                    [cell.as_dict() for cell in self.header_cells],
                ),
            ]
        )


class Section(object):
    """Definition of one composite-header table

    :param name: section name used by selections and results
    :param header_rows: two zero-based worksheet row indices
    :param columns: optional ``(first, last)`` zero-based column range;
                    by default the range follows the section's header
                    labels and their merges
    """

    def __init__(self, name, header_rows, columns=None):
        if not name:
            raise SectionSpecError("section name is required")
        try:
            rows = tuple(int(row) for row in header_rows)
        except (TypeError, ValueError):
            raise SectionSpecError(
                "section %r needs two header rows" % name
            )
        if len(rows) != HEADER_ROW_COUNT:
            raise SectionSpecError(
                "section %r needs exactly two header rows" % name
            )
        if rows[0] == rows[1]:
            raise SectionSpecError(
                "section %r needs two different header rows" % name
            )
        if rows[0] > rows[1]:
            rows = (rows[1], rows[0])
        if rows[0] < 0:
            raise SectionSpecError("header rows cannot be negative")
        if columns is not None:
            columns = tuple(int(column) for column in columns)
            if len(columns) != 2 or columns[0] > columns[1]:
                raise SectionSpecError(
                    "section %r needs a (first, last) column range" % name
                )
            if columns[0] < 0:
                raise SectionSpecError("columns cannot be negative")
        self.name = name
        self.header_rows = rows
        self.columns = columns

    @property
    def first_header_row(self):
        return self.header_rows[0]

    @property
    def last_header_row(self):
        return self.header_rows[1]

    def as_dict(self):
        return OrderedDict(
            [
                ("name", self.name),
                ("header_rows", self.header_rows),
                ("columns", self.columns),
            ]
        )

    def __repr__(self):
        return "Section(%r, %r, columns=%r)" % (
            self.name,
            self.header_rows,
            self.columns,
        )


class AmbiguousField(object):
    """Event reporting a field with several real physical candidates"""

    kind = "ambiguous"

    def __init__(self, section, field, candidates):
        self.section = section
        self.field = field
        self.candidates = tuple(candidates)

    def as_dict(self):
        return OrderedDict(
            [
                ("kind", self.kind),
                ("section", self.section),
                ("field", self.field),
                (
                    "candidates",
                    [candidate.as_dict() for candidate in self.candidates],
                ),
            ]
        )

    def __repr__(self):
        return "AmbiguousField(%r, %r, %d candidates)" % (
            self.section,
            self.field,
            len(self.candidates),
        )


class SectionRecord(object):
    """One source row of a section

    ``values`` and ``sources`` hold every field that is resolved when
    the record is yielded. Fields waiting for a section-scoped
    selection are listed in ``pending_fields``. A later selection
    returns a :class:`FieldPatch` for this record instead of yielding
    the record again.
    """

    kind = "record"

    def __init__(
        self,
        section,
        source_row,
        values,
        sources,
        pending_fields=(),
    ):
        self.section = section
        self.source_row = source_row
        self.values = OrderedDict(values)
        self.sources = OrderedDict(sources)
        self.pending_fields = tuple(pending_fields)

    @property
    def excel_row(self):
        return self.source_row + 1

    @property
    def is_complete(self):
        return not self.pending_fields

    def as_dict(self):
        return OrderedDict(
            [
                ("kind", self.kind),
                ("section", self.section),
                ("source_row", self.source_row),
                ("excel_row", self.excel_row),
                ("values", self.values),
                (
                    "sources",
                    OrderedDict(
                        (name, source.as_dict())
                        for name, source in self.sources.items()
                    ),
                ),
                ("pending_fields", self.pending_fields),
            ]
        )

    def __getitem__(self, key):
        if key in self.values:
            return self.values[key]
        return getattr(self, key)

    def __repr__(self):
        return "SectionRecord(%r, row=%r, values=%r)" % (
            self.section,
            self.source_row,
            dict(self.values),
        )


class FieldPatch(object):
    """Only the newly resolved fields of an already delivered record"""

    kind = "patch"

    def __init__(self, section, source_row, values, sources):
        self.section = section
        self.source_row = source_row
        self.values = OrderedDict(values)
        self.sources = OrderedDict(sources)

    @property
    def excel_row(self):
        return self.source_row + 1

    @property
    def fields(self):
        return tuple(self.values.keys())

    def as_dict(self):
        return OrderedDict(
            [
                ("kind", self.kind),
                ("section", self.section),
                ("source_row", self.source_row),
                ("excel_row", self.excel_row),
                ("fields", self.fields),
                ("values", self.values),
                (
                    "sources",
                    OrderedDict(
                        (name, source.as_dict())
                        for name, source in self.sources.items()
                    ),
                ),
            ]
        )

    def __getitem__(self, key):
        if key in self.values:
            return self.values[key]
        return getattr(self, key)

    def __repr__(self):
        return "FieldPatch(%r, row=%r, values=%r)" % (
            self.section,
            self.source_row,
            dict(self.values),
        )


class _Rect(object):
    __slots__ = (
        "min_row",
        "min_col",
        "max_row",
        "max_col",
        "range_string",
    )

    def __init__(self, min_row, min_col, max_row, max_col, range_string):
        self.min_row = min_row
        self.min_col = min_col
        self.max_row = max_row
        self.max_col = max_col
        self.range_string = range_string


class _RawSheet(object):
    def __init__(self, worksheet):
        self.worksheet = worksheet
        self.sheet_name = worksheet.title
        self.max_row = max(worksheet.max_row, 1) - 1
        self.max_column = max(worksheet.max_column, 1) - 1
        self.rectangles = []
        self.by_cell = {}
        for merged_range in worksheet.merged_cells.ranges:
            rect = _Rect(
                merged_range.min_row - 1,
                merged_range.min_col - 1,
                merged_range.max_row - 1,
                merged_range.max_col - 1,
                str(merged_range),
            )
            self.rectangles.append(rect)
            if rect.max_row > self.max_row:
                self.max_row = rect.max_row
            if rect.max_col > self.max_column:
                self.max_column = rect.max_col
            for row in range(rect.min_row, rect.max_row + 1):
                for column in range(rect.min_col, rect.max_col + 1):
                    self.by_cell[(row, column)] = rect

    def value(self, row, column):
        if row < 0 or column < 0:
            return None
        if row > self.max_row or column > self.max_column:
            return None
        return self.worksheet.cell(
            row=row + 1, column=column + 1
        ).value

    def rectangle(self, row, column):
        return self.by_cell.get((row, column))


def column_letter(column):
    """Return the Excel letter of a zero-based column index"""
    from openpyxl.utils import get_column_letter

    return get_column_letter(column + 1)


def _column_index(value):
    if isinstance(value, int):
        if value < 0:
            raise InvalidSelectionError("column cannot be negative")
        return value
    from openpyxl.utils import column_index_from_string

    try:
        return column_index_from_string(str(value)) - 1
    except ValueError:
        raise InvalidSelectionError(
            "%r is not a physical column" % (value,)
        )


def _text(value):
    if value is None:
        return ""
    return str(value).strip()


def _is_empty(value):
    return _text(value) == ""


def canonical_path(field):
    """Normalize a field path to ``part/part``"""
    if isinstance(field, (tuple, list)):
        parts = field
    else:
        parts = str(field).split("/")
    return "/".join(part for part in (_text(part) for part in parts) if part)


def _load_raw_sheet(
    file_name=None,
    file_stream=None,
    file_content=None,
    sheet_name=None,
    data_only=True,
):
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise SectionError(
            "the section reader needs openpyxl to read xlsx files"
        )

    if file_name is not None:
        source = file_name
    elif file_stream is not None:
        source = file_stream
    elif file_content is not None:
        source = BytesIO(file_content)
    else:
        raise SectionSpecError(
            "provide file_name, file_stream or file_content"
        )

    workbook = load_workbook(
        filename=source,
        data_only=data_only,
        read_only=False,
    )
    if sheet_name is None:
        worksheet = workbook.active
    else:
        if sheet_name not in workbook.sheetnames:
            raise SectionSpecError(
                "worksheet %r is not present" % sheet_name
            )
        worksheet = workbook[sheet_name]
    return _RawSheet(worksheet)


def _ordered_sections(sections):
    ordered = OrderedDict()
    for section in sections:
        if not isinstance(section, Section):
            raise SectionSpecError(
                "sections must contain Section instances"
            )
        if section.name in ordered:
            raise SectionSpecError(
                "section %r is defined twice" % section.name
            )
        ordered[section.name] = section
    if not ordered:
        raise SectionSpecError("at least one section is required")
    return ordered


def _ranges_overlap(left, right):
    return left[0] <= right[1] and right[0] <= left[1]


def _header_bands_overlap(left, right):
    return not (
        left.last_header_row < right.first_header_row
        or right.last_header_row < left.first_header_row
    )


def _split_columns(columns):
    clusters = []
    current = [min(columns)]
    for column in sorted(columns):
        if column == current[-1]:
            continue
        if column == current[-1] + 1:
            current.append(column)
        else:
            clusters.append(current)
            current = [column]
    clusters.append(current)
    return clusters


class SectionLayout(object):
    """Header geometry and field candidates of the defined sections"""

    def __init__(self, raw_sheet, sections):
        self.raw_sheet = raw_sheet
        self.sections = _ordered_sections(sections)
        self._definition_order = {
            name: index
            for index, name in enumerate(self.sections.keys())
        }
        self._columns = self._resolve_columns()
        self._ordered = sorted(
            self.sections.values(),
            key=lambda section: (
                section.first_header_row,
                self._columns[section.name][0],
                self._definition_order[section.name],
            ),
        )
        self._bounds = {}
        self._paths = {}
        self._column_paths = {}
        self._validate_bounds()
        self._build()

    def _anchor_columns(self, section):
        columns = set()
        for row in section.header_rows:
            for column in range(self.raw_sheet.max_column + 1):
                rectangle = self.raw_sheet.rectangle(row, column)
                if rectangle is not None:
                    if rectangle.min_row != row:
                        continue
                    if rectangle.min_col != column:
                        continue
                value = self.raw_sheet.value(row, column)
                if not _is_empty(value):
                    columns.add(column)
        return columns

    def _header_groups(self):
        sections = list(self.sections.values())
        parent = list(range(len(sections)))

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left, right):
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for left in range(len(sections)):
            for right in range(left + 1, len(sections)):
                if _header_bands_overlap(sections[left], sections[right]):
                    union(left, right)
        groups = OrderedDict()
        for index, section in enumerate(sections):
            root = find(index)
            groups.setdefault(root, []).append(section)
        return list(groups.values())

    def _resolve_columns(self):
        resolved = {}
        for section in self.sections.values():
            if section.columns is not None:
                resolved[section.name] = section.columns
        for group in self._header_groups():
            pending = [
                section
                for section in group
                if section.name not in resolved
            ]
            if not pending:
                continue
            taken = [
                resolved[section.name]
                for section in group
                if section.name in resolved
            ]
            anchors = set()
            for section in pending:
                anchors.update(self._anchor_columns(section))
            for first, last in taken:
                anchors = {
                    column
                    for column in anchors
                    if column < first or column > last
                }
            if not anchors:
                raise SectionSpecError(
                    "sections %s have no header labels"
                    % ", ".join(section.name for section in pending)
                )
            if len(pending) == 1:
                section = pending[0]
                resolved[section.name] = (min(anchors), max(anchors))
                continue
            clusters = _split_columns(anchors)
            if len(clusters) != len(pending):
                raise SectionSpecError(
                    "cannot assign columns to sections %s; give an "
                    "explicit column range for each section"
                    % ", ".join(section.name for section in pending)
                )
            for section, cluster in zip(pending, clusters):
                resolved[section.name] = (cluster[0], cluster[-1])
        return resolved

    def _validate_bounds(self):
        ordered = self._ordered
        for left_index, left in enumerate(ordered):
            left_columns = self._columns[left.name]
            for right in ordered[left_index + 1:]:
                right_columns = self._columns[right.name]
                if not _ranges_overlap(left_columns, right_columns):
                    continue
                if _header_bands_overlap(left, right):
                    raise SectionSpecError(
                        "sections %r and %r overlap"
                        % (left.name, right.name)
                    )
        for section in ordered:
            start = section.first_header_row
            end = self.raw_sheet.max_row
            for other in ordered:
                if other is section:
                    continue
                if other.first_header_row <= section.first_header_row:
                    continue
                if not _ranges_overlap(
                    self._columns[section.name],
                    self._columns[other.name],
                ):
                    continue
                end = min(end, other.first_header_row - 1)
            if end < section.last_header_row:
                raise SectionSpecError(
                    "section %r has no room for its header" % section.name
                )
            self._bounds[section.name] = (start, end)

    def _header_label_cell(self, section, row, column):
        rectangle = self.raw_sheet.rectangle(row, column)
        if rectangle is not None:
            if rectangle.min_row not in section.header_rows:
                return None
            anchor_column = rectangle.min_col
            if not self._anchor_belongs_to(
                section, anchor_column, rectangle
            ):
                return None
            value = self.raw_sheet.value(
                rectangle.min_row, anchor_column
            )
            if _is_empty(value):
                return None
            return HeaderCell(
                rectangle.min_row,
                anchor_column,
                value,
                rectangle.range_string,
            )
        if row not in section.header_rows:
            return None
        value = self.raw_sheet.value(row, column)
        if _is_empty(value):
            return None
        return HeaderCell(row, column, value)

    def _anchor_belongs_to(self, section, anchor_column, rectangle=None):
        first, last = self._columns[section.name]
        if first <= anchor_column <= last:
            return True
        if rectangle is None:
            return False
        if rectangle.max_col < first or rectangle.min_col > last:
            return False
        for other in self.sections.values():
            if other.name == section.name:
                continue
            if not _header_bands_overlap(section, other):
                continue
            other_first, other_last = self._columns[other.name]
            if (
                rectangle.min_col <= other_last
                and rectangle.max_col >= other_first
            ):
                return False
        return True

    def _coverage(self, section):
        return self._columns[section.name]

    def _column_header(self, section, column):
        labels = []
        cells = []
        merged_ranges = []
        seen = set()
        complete = True
        for row in section.header_rows:
            cell = self._header_label_cell(section, row, column)
            if cell is None:
                complete = False
                continue
            key = (cell.row, cell.column)
            if key in seen:
                complete = False
                continue
            seen.add(key)
            labels.append(_text(cell.value))
            cells.append(cell)
            if cell.merged_range:
                merged_ranges.append(cell.merged_range)
        path = "/".join(labels)
        owned = bool(cells) and cells[-1].column == column
        return (
            path,
            labels,
            cells,
            tuple(merged_ranges),
            complete,
            owned,
        )

    def _build(self):
        for section in self._ordered:
            first, last = self._coverage(section)
            self._columns[section.name] = (first, last)
            paths = OrderedDict()
            column_paths = OrderedDict()
            for column in range(first, last + 1):
                (
                    path,
                    labels,
                    cells,
                    ranges,
                    complete,
                    owned,
                ) = self._column_header(section, column)
                column_paths[column] = {
                    "path": path,
                    "labels": tuple(labels),
                    "owned": owned,
                    "complete": complete,
                }
                if not path or not owned or not complete:
                    continue
                candidates = paths.get(path)
                if candidates is None:
                    candidates = []
                    paths[path] = candidates
                candidates.append(
                    Candidate(
                        section.name,
                        path,
                        column,
                        section.header_rows,
                        labels,
                        cells,
                        ranges,
                    )
                )
            self._paths[section.name] = OrderedDict(
                (path, tuple(candidates))
                for path, candidates in paths.items()
            )
            self._column_paths[section.name] = column_paths

    def section_names(self):
        return tuple(self.sections.keys())

    @property
    def sheet_name(self):
        return self.raw_sheet.sheet_name

    def physical_sections(self):
        """Sections in worksheet order"""
        return tuple(self._ordered)

    def header_rows(self, section):
        return self.sections[section].header_rows

    def columns(self, section):
        return self._columns[section]

    def paths(self, section):
        """Map every discoverable field path to its real candidates"""
        return self._paths[section]

    def column_paths(self, section):
        """Describe the header path of each column in the section"""
        return self._column_paths[section]

    def candidates(self, section, field):
        return self._paths[section].get(canonical_path(field), ())

    def bounds(self, section):
        return self._bounds[section]

    def value(self, row, column):
        return self.raw_sheet.value(row, column)


class SectionRecordStream(object):
    """Incremental records for several sections

    Iterate the stream to receive :class:`AmbiguousField`,
    :class:`SectionRecord` and :class:`FieldPatch` events. When a field
    has several real candidates, call :meth:`select` with the section
    name, the field path and one physical column (or a
    :class:`Candidate`). The selection only applies to that section.

    A record is yielded even while one of its fields is ambiguous; that
    field is listed in ``pending_fields``. Selecting the field later
    queues :class:`FieldPatch` events for the records that were already
    delivered. A patch carries only the newly resolved fields and the
    original source row, and the record is not yielded again.
    """

    def __init__(
        self,
        file_name=None,
        sections=None,
        fields=None,
        selections=None,
        sheet_name=None,
        file_stream=None,
        file_content=None,
        data_only=True,
    ):
        if not sections:
            raise SectionSpecError("at least one section is required")
        if not fields:
            raise SectionSpecError("at least one field is required")
        self.fields = tuple(canonical_path(field) for field in fields)
        if len(set(self.fields)) != len(self.fields):
            raise SectionSpecError("field paths must be unique")
        self.layout = SectionLayout(
            _load_raw_sheet(
                file_name=file_name,
                file_stream=file_stream,
                file_content=file_content,
                sheet_name=sheet_name,
                data_only=data_only,
            ),
            sections,
        )
        self._selected = {}
        self._delivered = OrderedDict(
            (name, OrderedDict()) for name in self.layout.section_names()
        )
        self._patches = OrderedDict()
        self._events = []
        self._announced = set()
        self._pending_row = None
        self._rows = self._make_row_iterator()
        self._exhausted = False
        self._check_fields()
        for section_name, section_selections in (selections or {}).items():
            if section_name not in self.layout.sections:
                raise InvalidSelectionError(
                    "unknown section %r" % (section_name,)
                )
            for field, column in section_selections.items():
                self.select(section_name, field, column)

    def _check_fields(self):
        for section_name in self.layout.section_names():
            available = self.layout.paths(section_name)
            for field in self.fields:
                if not available.get(field):
                    raise FieldNotFoundError(
                        section_name,
                        field,
                        available.keys(),
                    )

    def _make_row_iterator(self):
        for section in self.layout.physical_sections():
            start, end = self.layout.bounds(section.name)
            yield section.name, None
            for row in range(section.last_header_row + 1, end + 1):
                yield section.name, row

    def _resolution(self, section, field):
        selected = self._selected.get((section, field))
        if selected is not None:
            return selected
        candidates = self.layout.candidates(section, field)
        if len(candidates) == 1:
            return candidates[0]
        return None

    def select(self, section, field, column):
        """Select one physical column for a section's ambiguous field"""
        if section not in self.layout.sections:
            raise InvalidSelectionError(
                "unknown section %r" % (section,)
            )
        field = canonical_path(field)
        candidates = self.layout.candidates(section, field)
        if not candidates:
            raise FieldNotFoundError(
                section,
                field,
                self.layout.paths(section).keys(),
            )

        selected = None
        if isinstance(column, Candidate):
            if column.section != section or column.field != field:
                raise InvalidSelectionError(
                    "candidate belongs to %r/%r"
                    % (column.section, column.field)
                )
            selected = column
        else:
            index = _column_index(column)
            for candidate in candidates:
                if candidate.column == index:
                    selected = candidate
                    break
        if selected is None:
            raise InvalidSelectionError(
                "column %r is not a candidate for %r in section %r; "
                "choose one of %s"
                % (
                    column,
                    field,
                    section,
                    ", ".join(
                        candidate.column_letter
                        for candidate in candidates
                    ),
                )
            )

        current = self._selected.get((section, field))
        if current is not None:
            if current.column != selected.column:
                raise InvalidSelectionError(
                    "%r in section %r is already selected as column %s"
                    % (field, section, current.column_letter)
                )
            return current
        self._selected[(section, field)] = selected
        if len(candidates) > 1:
            self._queue_patches(section, field, selected)
        return selected

    def _queue_patches(self, section, field, candidate):
        for source_row, record in self._delivered[section].items():
            if field not in record.pending_fields:
                continue
            key = (section, source_row)
            entry = self._patches.get(key)
            if entry is None:
                entry = {
                    "values": OrderedDict(),
                    "sources": OrderedDict(),
                    "fields": set(),
                }
                self._patches[key] = entry
            if field in entry["fields"]:
                continue
            entry["fields"].add(field)
            entry["values"][field] = self._value(
                source_row, candidate.column
            )
            entry["sources"][field] = FieldSource(
                source_row,
                candidate.column,
                candidate.cells,
                self.layout.sheet_name,
            )

    def _value(self, row, column):
        value = self.layout.value(row, column)
        if value is None:
            return ""
        return value

    def _row_has_field_content(self, section, row):
        for field in self.fields:
            selected = self._selected.get((section, field))
            if selected is not None:
                columns = (selected.column,)
            else:
                columns = tuple(
                    candidate.column
                    for candidate in self.layout.candidates(
                        section, field
                    )
                )
            for column in columns:
                if not _is_empty(self.layout.value(row, column)):
                    return True
        return False

    def _pending_fields(self, section):
        pending = []
        for field in self.fields:
            if self._resolution(section, field) is None:
                pending.append(field)
        return pending

    def _make_record(self, section, row):
        pending = self._pending_fields(section)
        pending_set = set(pending)
        values = OrderedDict()
        sources = OrderedDict()
        for field in self.fields:
            if field in pending_set:
                continue
            candidate = self._resolution(section, field)
            values[field] = self._value(row, candidate.column)
            sources[field] = FieldSource(
                row,
                candidate.column,
                candidate.cells,
                self.layout.sheet_name,
            )
        return SectionRecord(
            section,
            row,
            values,
            sources,
            pending,
        )

    def _ambiguities(self, section):
        events = []
        for field in self.fields:
            if self._selected.get((section, field)) is not None:
                continue
            candidates = self.layout.candidates(section, field)
            if len(candidates) > 1:
                events.append(
                    AmbiguousField(section, field, candidates)
                )
        return events

    def _next_patch(self):
        (section, source_row), entry = self._patches.popitem(last=False)
        return FieldPatch(
            section,
            source_row,
            entry["values"],
            entry["sources"],
        )

    def _next_event(self):
        while self._events:
            event = self._events.pop(0)
            if self._selected.get((event.section, event.field)) is None:
                return event
        return None

    def __iter__(self):
        return self

    def __next__(self):
        if self._patches:
            return self._next_patch()
        event = self._next_event()
        if event is not None:
            return event
        if self._exhausted:
            raise StopIteration
        row_item = self._pending_row
        while True:
            if row_item is None:
                try:
                    row_item = next(self._rows)
                except StopIteration:
                    self._exhausted = True
                    raise
            section, row = row_item
            self._pending_row = row_item
            if row is None:
                if section not in self._announced:
                    self._announced.add(section)
                    self._events.extend(self._ambiguities(section))
                self._pending_row = None
                row_item = None
                event = self._next_event()
                if event is not None:
                    return event
                continue
            if not self._row_has_field_content(section, row):
                row_item = None
                self._pending_row = None
                continue
            record = self._make_record(section, row)
            self._delivered[section][row] = record
            self._pending_row = None
            return record

    next = __next__

    def candidates(self, section, field):
        """Return the real candidates of a section field"""
        return self.layout.candidates(section, field)

    def paths(self, section):
        """Return the discoverable paths of a section"""
        return self.layout.paths(section)

    def columns(self, section):
        """Return the covered ``(first, last)`` physical columns"""
        return self.layout.columns(section)


def iget_section_records(
    file_name=None,
    sections=None,
    fields=None,
    selections=None,
    sheet_name=None,
    file_stream=None,
    file_content=None,
    data_only=True,
):
    """Return an incremental multi-section record stream

    :param file_name: an xlsx file name
    :param sections: a list of :class:`Section` definitions
    :param fields: field paths such as ``原币/价格``
    :param selections: optional ``{section: {field: column}}`` map
    :param sheet_name: worksheet name; the active sheet by default
    """
    return SectionRecordStream(
        file_name=file_name,
        sections=sections,
        fields=fields,
        selections=selections,
        sheet_name=sheet_name,
        file_stream=file_stream,
        file_content=file_content,
        data_only=data_only,
    )


def get_section_records(
    file_name=None,
    sections=None,
    fields=None,
    selections=None,
    sheet_name=None,
    file_stream=None,
    file_content=None,
    data_only=True,
):
    """Extract all section records

    Every requested field must be resolved, either because it has one
    real candidate or because ``selections`` names a physical column.
    Otherwise :class:`AmbiguousFieldError` is raised with the candidates.
    Use :func:`iget_section_records` to inspect candidates and select
    them while records are being delivered.
    """
    stream = iget_section_records(
        file_name=file_name,
        sections=sections,
        fields=fields,
        selections=selections,
        sheet_name=sheet_name,
        file_stream=file_stream,
        file_content=file_content,
        data_only=data_only,
    )
    records = []
    for event in stream:
        if event.kind == "ambiguous":
            raise AmbiguousFieldError(
                event.section,
                event.field,
                event.candidates,
            )
        if event.kind == "record":
            records.append(event)
    return records


def save_unmerged_control(
    file_name,
    dest_file_name,
    sections,
    sheet_name=None,
    fill_merged_cells=False,
):
    """Save a copy with the given sections' header merges removed

    Only merges that lie completely inside one of the given sections'
    header bands are removed. By default the anchor cell keeps its value
    and the covered cells stay empty, so the merged label is no longer
    inherited by the covered columns. Paths and ambiguity that depended
    on that inheritance can then be compared with the merged original.

    Set ``fill_merged_cells=True`` to copy the anchor's displayed value
    into every covered cell before unmerging. The control then shows the
    same visible header text everywhere, and the only difference is that
    each column carries a literal label instead of inheriting one.

    Merges outside those header bands, including a merge that crosses
    from one section into the next, are left untouched.
    """
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise SectionError(
            "the section reader needs openpyxl to read xlsx files"
        )

    workbook = load_workbook(filename=file_name, data_only=True)
    if sheet_name is None:
        worksheet = workbook.active
    else:
        if sheet_name not in workbook.sheetnames:
            raise SectionSpecError(
                "worksheet %r is not present" % sheet_name
            )
        worksheet = workbook[sheet_name]

    header_bands = []
    for section in sections:
        if not isinstance(section, Section):
            raise SectionSpecError(
                "sections must contain Section instances"
            )
        header_bands.append(
            (section.first_header_row + 1, section.last_header_row + 1)
        )

    for merged_range in list(worksheet.merged_cells.ranges):
        for first, last in header_bands:
            if (
                merged_range.min_row >= first
                and merged_range.max_row <= last
            ):
                value = None
                if fill_merged_cells:
                    value = worksheet.cell(
                        row=merged_range.min_row,
                        column=merged_range.min_col,
                    ).value
                worksheet.unmerge_cells(str(merged_range))
                if fill_merged_cells:
                    for row in range(
                        merged_range.min_row,
                        merged_range.max_row + 1,
                    ):
                        for column in range(
                            merged_range.min_col,
                            merged_range.max_col + 1,
                        ):
                            worksheet.cell(
                                row=row,
                                column=column,
                            ).value = value
                break

    workbook.save(dest_file_name)
