from textwrap import dedent

from pyexcel import Sheet
from pyexcel.sheet import Change

from .nose_tools import eq_


def make_sheet():
    # "amount" appears twice as a raw column header, hence the real
    # column names become "amount" and "amount-1". The second row is
    # named "amount" as well, exactly like a column name. Cell values
    # are duplicated on purpose, so no value based lookup is possible.
    data = [
        ["id", "amount", "amount", "note"],
        ["row_a", 1, 1, "x"],
        ["amount", 2, 2, "y"],
        ["row_c", 3, 3, "z"],
    ]
    return Sheet(data, name_columns_by_row=0, name_rows_by_column=0)


def make_tracked_sheet():
    sheet = make_sheet()
    sheet.track_changes()
    return sheet


def project_and_transpose(sheet):
    excluded = sheet.project(["note"], exclusion=True)
    projected = excluded.project(list(reversed(excluded.colnames)))
    projected.transpose()
    return excluded, projected


def test_project_keeps_row_names():
    sheet = make_sheet()
    projected = sheet.project(["amount-1", "amount"], exclusion=False)
    eq_(projected.colnames, ["amount-1", "amount"])
    eq_(projected.rownames, ["row_a", "amount", "row_c"])


def test_project_exclusion_keeps_row_names():
    sheet = make_sheet()
    projected = sheet.project(["note"], exclusion=True)
    eq_(projected.colnames, ["amount", "amount-1"])
    eq_(projected.rownames, ["row_a", "amount", "row_c"])


def test_transpose_display_uses_current_axis_names_and_order():
    sheet = make_tracked_sheet()
    _, projected = project_and_transpose(sheet)
    eq_(projected.rownames, ["amount-1", "amount"])
    eq_(projected.colnames, ["row_a", "amount", "row_c"])
    expected = dedent(
        """
    pyexcel sheet:
    +----------+-------+--------+-------+
    |          | row_a | amount | row_c |
    +==========+=======+========+=======+
    | amount-1 | 1     | 2      | 3     |
    +----------+-------+--------+-------+
    | amount   | 1     | 2      | 3     |
    +----------+-------+--------+-------+""",
    ).strip()
    eq_(expected, str(projected))


def test_backfill_reports_surviving_changes_in_initial_orientation():
    sheet = make_tracked_sheet()
    _, projected = project_and_transpose(sheet)

    projected["amount", "amount"] = 99  # by name
    projected[1, 0] = 77  # by index
    projected["amount", "row_c"] = 555  # a change of mind ...
    projected["amount", "row_c"] = 3  # ... reverted to the original
    projected["amount-1", "row_a"] = 1000  # lost with its row below
    del projected.row["amount-1"]

    changes = projected.get_changes()
    eq_(
        changes,
        [
            Change(
                row=0,
                column=0,
                row_name="row_a",
                column_name="amount",
                value=77,
            ),
            Change(
                row=1,
                column=0,
                row_name="amount",
                column_name="amount",
                value=99,
            ),
        ],
    )

    # the caller backfills into the original format
    for change in changes:
        sheet[change.row, change.column] = change.value
    eq_(sheet[0, 0], 77)
    eq_(sheet[1, 0], 99)
    eq_(sheet[2, 0], 3)
    eq_(sheet["amount", "amount-1"], 2)


def test_edits_do_not_leak_to_source_or_intermediate_sheets():
    sheet = make_tracked_sheet()
    excluded, projected = project_and_transpose(sheet)
    projected["amount", "amount"] = 99
    projected[0, 0] = 1000

    eq_(sheet[0, 0], 1)
    eq_(sheet[1, 0], 2)
    eq_(sheet.get_changes(), [])
    eq_(
        excluded.to_array(),
        [
            ["", "amount", "amount-1"],
            ["row_a", 1, 1],
            ["amount", 2, 2],
            ["row_c", 3, 3],
        ],
    )
    eq_(excluded.get_changes(), [])


def test_reverted_change_disappears_without_deletion():
    sheet = make_tracked_sheet()
    sheet["row_a", "amount"] = 42
    eq_(len(sheet.get_changes()), 1)
    sheet["row_a", "amount"] = 1
    eq_(sheet.get_changes(), [])


def test_deleted_column_takes_its_changes():
    sheet = make_tracked_sheet()
    sheet["row_a", "amount-1"] = 42
    sheet["row_c", "note"] = "new"
    sheet.delete_named_column_at("amount-1")
    eq_(
        sheet.get_changes(),
        [
            Change(
                row=2,
                column=2,
                row_name="row_c",
                column_name="note",
                value="new",
            ),
        ],
    )


def test_get_changes_requires_tracking():
    sheet = make_sheet()
    try:
        sheet.get_changes()
        raise AssertionError("ValueError was not raised")
    except ValueError:
        pass


def test_untracked_projection_and_transpose_work_as_before():
    sheet = make_sheet()
    excluded, projected = project_and_transpose(sheet)
    projected["amount", "amount"] = 99
    eq_(projected["amount", "amount"], 99)
    eq_(sheet[1, 0], 2)
    eq_(excluded.rownames, ["row_a", "amount", "row_c"])
