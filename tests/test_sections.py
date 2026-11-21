import os
import tempfile
import unittest

from openpyxl import Workbook, load_workbook

from pyexcel.sections import (
    AmbiguousField,
    AmbiguousFieldError,
    FieldPatch,
    FieldNotFoundError,
    InvalidSelectionError,
    Section,
    SectionRecord,
    SectionSpecError,
    get_section_records,
    iget_section_records,
    save_unmerged_control,
)


def make_price_workbook(path):
    """Build a two-section workbook with the task's header traps.

    Section one has one ``原币/价格`` path. Its ``原币`` group anchor is
    in column D, which is not projected. Column G is an empty separator.
    A vertical merge starts on a real section-one data cell in column F
    and reaches the title rows of section two.

    Section two repeats ``原币/价格`` in two real columns. The second
    one inherits ``原币`` from a horizontal merge, so removing that merge
    in the control copy changes both the discoverable paths and the
    ambiguity.
    """
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Prices"

    sheet["B1"] = "本币"
    sheet.merge_cells("B1:C1")
    sheet["B1"] = "本币"
    sheet["D1"] = "原币"
    sheet.merge_cells("D1:F1")
    sheet["D1"] = "原币"
    sheet["B2"] = "价格"
    sheet["C2"] = "税率"
    sheet["E2"] = "价格"
    sheet["F2"] = "税率"

    sheet["B3"] = "A"
    sheet["C3"] = 0.01
    sheet["E3"] = 10
    sheet["F3"] = 0.02
    sheet["B4"] = "B"
    sheet["C4"] = 0.02
    sheet["E4"] = 20
    sheet["F4"] = 0.03
    sheet["B5"] = "C"
    sheet["C5"] = 0.03
    sheet["E5"] = 30
    sheet["F5"] = "尾值"

    # This merge crosses the section-one tail and touches the title of
    # section two. The anchor is real section-one data at F5.
    sheet.merge_cells("F5:F7")

    sheet["H6"] = "本币"
    sheet.merge_cells("H6:I6")
    sheet["H6"] = "本币"
    sheet["J6"] = "原币"
    sheet.merge_cells("J6:K6")
    sheet["J6"] = "原币"
    sheet["L6"] = "原币"
    sheet["H7"] = "价格"
    sheet["I7"] = "税率"
    sheet["J7"] = "价格"
    sheet["K7"] = "价格"
    sheet["L7"] = "税率"

    sheet["H8"] = "D"
    sheet["I8"] = 0.04
    sheet["J8"] = 40
    sheet["K8"] = 41
    sheet["L8"] = 0.05
    sheet["H9"] = "E"
    sheet["I9"] = 0.05
    sheet["J9"] = 50
    sheet["K9"] = 51
    sheet["L9"] = 0.06

    workbook.save(path)


class SectionFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "prices.xlsx")
        make_price_workbook(self.path)
        self.sections = [
            Section("first", (0, 1)),
            Section("second", (5, 6)),
        ]
        self.fields = [
            "本币/价格",
            "本币/税率",
            "原币/价格",
            "原币/税率",
        ]

    def records(self, selections=None, fields=None):
        return get_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=fields or self.fields,
            selections=selections,
        )


class TestSectionLayout(SectionFixture):
    def test_first_section_paths(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=self.fields,
        )
        paths = stream.paths("first")
        self.assertEqual(
            list(paths),
            [
                "本币/价格",
                "本币/税率",
                "原币/价格",
                "原币/税率",
            ],
        )
        self.assertEqual(len(paths["原币/价格"]), 1)
        self.assertEqual(paths["原币/价格"][0].column, 4)

    def test_first_section_anchor_is_left_of_projection(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["原币/价格"],
        )
        candidate = stream.candidates("first", "原币/价格")[0]
        self.assertEqual(candidate.column_letter, "E")
        self.assertEqual(
            [cell.column_letter for cell in candidate.cells],
            ["D", "E"],
        )
        self.assertEqual(
            candidate.cells[0].merged_range,
            "D1:F1",
        )
        self.assertNotEqual(
            candidate.cells[0].column_letter,
            candidate.column_letter,
        )

    def test_second_section_has_two_real_candidates(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=self.fields,
        )
        candidates = stream.candidates("second", "原币/价格")
        self.assertEqual([item.column for item in candidates], [9, 10])
        self.assertEqual(
            [item.column_letter for item in candidates], ["J", "K"]
        )

    def test_empty_column_separates_sections(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=self.fields,
        )
        self.assertEqual(stream.columns("first"), (1, 5))
        self.assertEqual(stream.columns("second"), (7, 11))

    def test_cross_section_merge_does_not_name_second_header(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=self.fields,
        )
        second_paths = stream.paths("second")
        self.assertTrue(
            all("尾值" not in path for path in second_paths)
        )
        self.assertEqual(stream.columns("second")[0], 7)


class TestAmbiguity(SectionFixture):
    def test_ambiguous_event_only_for_second_section(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["原币/价格"],
        )
        events = list(stream)
        ambiguous = [e for e in events if isinstance(e, AmbiguousField)]
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0].section, "second")
        self.assertEqual(
            [item.column_letter for item in ambiguous[0].candidates],
            ["J", "K"],
        )

    def test_first_records_precede_second_ambiguity(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["原币/价格"],
        )
        first = []
        for event in stream:
            if isinstance(event, AmbiguousField):
                break
            first.append(event)
        self.assertEqual([record.section for record in first], ["first"] * 3)
        self.assertEqual(
            [record["原币/价格"] for record in first],
            [10, 20, 30],
        )
        self.assertEqual([record.source_row for record in first], [2, 3, 4])
        self.assertEqual([record.excel_row for record in first], [3, 4, 5])

    def test_ambiguous_second_records_are_delivered_as_pending(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["原币/价格"],
        )
        seen_ambiguous = False
        second = []
        for event in stream:
            if isinstance(event, AmbiguousField):
                seen_ambiguous = True
                continue
            if event.section == "second":
                second.append(event)
                if len(second) == 2:
                    break
        self.assertTrue(seen_ambiguous)
        self.assertEqual(
            [record.pending_fields for record in second],
            [("原币/价格",), ("原币/价格",)],
        )
        self.assertNotIn("原币/价格", second[0].values)

    def test_confirming_patches_only_the_pending_field(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["原币/价格", "本币/价格"],
        )
        delivered = []
        for event in stream:
            if isinstance(event, AmbiguousField):
                continue
            if event.section == "second":
                delivered.append(event)
                if len(delivered) == 2:
                    break
        stream.select("second", "原币/价格", "J")
        patches = []
        for event in stream:
            if isinstance(event, FieldPatch):
                patches.append(event)
                if len(patches) == 2:
                    break
            if isinstance(event, SectionRecord):
                self.fail("records must not be sent again")
        self.assertEqual(
            [(patch.section, patch.source_row) for patch in patches],
            [("second", 7), ("second", 8)],
        )
        self.assertEqual(
            [patch.fields for patch in patches],
            [("原币/价格",), ("原币/价格",)],
        )
        self.assertEqual(
            [patch["原币/价格"] for patch in patches],
            [40, 50],
        )
        self.assertEqual(
            [patch.excel_row for patch in patches], [8, 9]
        )

    def test_get_section_records_requires_selection(self):
        with self.assertRaises(AmbiguousFieldError) as error:
            self.records(fields=["原币/价格"])
        self.assertEqual(error.exception.section, "second")
        self.assertEqual(len(error.exception.candidates), 2)


class TestSelections(SectionFixture):
    def test_selection_by_letter(self):
        records = self.records(
            selections={"second": {"原币/价格": "K"}},
            fields=["原币/价格"],
        )
        self.assertEqual(
            [record["原币/价格"] for record in records],
            [10, 20, 30, 41, 51],
        )

    def test_selection_by_zero_based_column(self):
        records = self.records(
            selections={"second": {"原币/价格": 9}},
            fields=["原币/价格"],
        )
        self.assertEqual(
            [record["原币/价格"] for record in records],
            [10, 20, 30, 40, 50],
        )

    def test_selection_by_candidate(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["原币/价格"],
        )
        candidate = stream.candidates("second", "原币/价格")[1]
        stream.select("second", "原币/价格", candidate)
        values = [
            record["原币/价格"]
            for record in stream
            if isinstance(record, SectionRecord)
        ]
        self.assertEqual(values, [10, 20, 30, 41, 51])

    def test_selection_outside_candidates_is_rejected(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["原币/价格"],
        )
        with self.assertRaises(InvalidSelectionError):
            stream.select("second", "原币/价格", "H")

    def test_changing_a_selection_is_rejected(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["原币/价格"],
            selections={"second": {"原币/价格": "K"}},
        )
        with self.assertRaises(InvalidSelectionError):
            stream.select("second", "原币/价格", "J")

    def test_selection_for_one_section_does_not_leak(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["原币/价格"],
        )
        first = []
        for event in stream:
            if isinstance(event, AmbiguousField):
                break
            first.append(event)
        before = [
            (record.source_row, record["原币/价格"])
            for record in first
        ]
        stream.select("second", "原币/价格", "K")
        after = []
        for event in stream:
            if isinstance(event, FieldPatch):
                continue
            if event.section == "second":
                after.append(event)
                break
        self.assertEqual(
            before, [(2, 10), (3, 20), (4, 30)]
        )
        self.assertEqual(
            [item.column_letter
             for item in stream.candidates("first", "原币/价格")],
            ["E"],
        )
        self.assertEqual(after[0]["原币/价格"], 41)

    def test_unknown_field_reports_available_paths(self):
        with self.assertRaises(FieldNotFoundError) as error:
            iget_section_records(
                file_name=self.path,
                sections=self.sections,
                fields=["原币/单价"],
            )
        self.assertEqual(error.exception.section, "first")
        self.assertIn("原币/价格", error.exception.available)


class TestVerticalMergeOverData(SectionFixture):
    def test_vertical_data_merge_keeps_anchor_value(self):
        records = self.records(
            selections={"second": {"原币/价格": "J"}}
        )
        last_first = [
            record for record in records
            if record.section == "first" and record.source_row == 4
        ][0]
        self.assertEqual(last_first["原币/税率"], "尾值")

    def test_vertical_merge_does_not_create_header_path(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=self.fields,
        )
        for section in ("first", "second"):
            for path in stream.paths(section):
                self.assertNotIn("尾值", path)
                self.assertEqual(path.count("/"), 1)


class TestRecordSources(SectionFixture):
    def test_sources_carry_physical_coordinates(self):
        records = self.records(
            selections={"second": {"原币/价格": "K"}}
        )
        first = [record for record in records if record.section == "first"]
        self.assertEqual([record.source_row for record in first], [2, 3, 4])
        self.assertEqual([record.excel_row for record in first], [3, 4, 5])
        source = first[0].sources["原币/价格"]
        self.assertEqual(source.column, 4)
        self.assertEqual(source.excel_column, 5)
        self.assertEqual(source.row, 2)

    def test_header_anchors_are_reported(self):
        records = self.records(
            selections={"second": {"原币/价格": "K"}}
        )
        first = [record for record in records if record.section == "first"]
        source = first[0].sources["原币/价格"]
        self.assertEqual(
            [cell.excel_column for cell in source.header_cells],
            [4, 5],
        )
        self.assertEqual(source.header_cells[0].merged_range, "D1:F1")

    def test_cross_section_merge_keeps_real_data_cell(self):
        records = self.records(
            selections={"second": {"原币/价格": "K"}}
        )
        last_first = [
            record for record in records
            if record.section == "first" and record.source_row == 4
        ][0]
        self.assertEqual(last_first["原币/税率"], "尾值")
        second = [
            record for record in records if record.section == "second"
        ]
        self.assertEqual(
            [record["本币/价格"] for record in second], ["D", "E"]
        )

    def test_projection_order_does_not_change_sources(self):
        forward = {
            (record.section, record.source_row): record
            for record in self.records(
                selections={"second": {"原币/价格": "K"}}
            )
        }
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=list(reversed(self.fields)),
            selections={"second": {"原币/价格": "K"}},
        )
        for record in stream:
            if not isinstance(record, SectionRecord):
                continue
            original = forward[(record.section, record.source_row)]
            for field in self.fields:
                self.assertEqual(
                    record.sources[field].as_dict(),
                    original.sources[field].as_dict(),
                )
                self.assertEqual(record.values[field], original.values[field])

    def test_record_order_follows_source_rows(self):
        records = self.records(
            selections={"second": {"原币/价格": "K"}},
        )
        self.assertEqual(
            [(record.section, record.source_row) for record in records],
            [
                ("first", 2),
                ("first", 3),
                ("first", 4),
                ("second", 7),
                ("second", 8),
            ],
        )


class TestUnmergedControl(SectionFixture):
    def control(self):
        path = os.path.join(self.directory, "control.xlsx")
        save_unmerged_control(self.path, path, [self.sections[1]])
        return path

    def test_unmerged_copy_removes_only_second_header_merges(self):
        path = self.control()
        sheet = load_workbook(path)["Prices"]
        ranges = [str(item) for item in sheet.merged_cells.ranges]
        self.assertIn("B1:C1", ranges)
        self.assertIn("D1:F1", ranges)
        self.assertNotIn("H6:I6", ranges)
        self.assertNotIn("J6:K6", ranges)
        self.assertIn("F5:F7", ranges)

    def test_unmerged_copy_keeps_anchor_text_without_copying_it(self):
        path = self.control()
        sheet = load_workbook(path)["Prices"]
        self.assertEqual(sheet["B1"].value, "本币")
        self.assertEqual(sheet["D1"].value, "原币")
        self.assertEqual(sheet["H6"].value, "本币")
        self.assertEqual(sheet["J6"].value, "原币")
        self.assertIsNone(sheet["I6"].value)
        self.assertIsNone(sheet["K6"].value)

    def test_unmerged_copy_keeps_values_and_rows(self):
        fields = ["原币/价格"]
        merged = self.records(
            selections={"second": {"原币/价格": "J"}},
            fields=fields,
        )
        unmerged = get_section_records(
            file_name=self.control(),
            sections=self.sections,
            fields=fields,
            selections={"second": {"原币/价格": "J"}},
        )
        self.assertEqual(
            [(record.section, record.source_row) for record in merged],
            [(record.section, record.source_row) for record in unmerged],
        )
        for left, right in zip(merged, unmerged):
            self.assertEqual(left.values, right.values)

    def test_unmerged_copy_changes_anchor_provenance(self):
        stream = iget_section_records(
            file_name=self.control(),
            sections=self.sections,
            fields=["原币/价格"],
        )
        candidates = stream.candidates("second", "原币/价格")
        self.assertEqual([item.column for item in candidates], [9])
        for candidate in candidates:
            self.assertTrue(
                all(cell.merged_range is None for cell in candidate.cells)
            )

    def test_unmerged_copy_changes_discoverable_paths(self):
        fields = ["原币/价格"]
        merged = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=fields,
        )
        unmerged = iget_section_records(
            file_name=self.control(),
            sections=self.sections,
            fields=fields,
        )
        self.assertNotIn("本币/税率", unmerged.paths("second"))
        self.assertEqual(
            len(merged.candidates("second", "原币/价格")), 2
        )
        self.assertEqual(
            len(unmerged.candidates("second", "原币/价格")), 1
        )
        self.assertNotIn(
            "原币/价格",
            {
                path
                for path, candidates in unmerged.paths("second").items()
                if any(item.column == 10 for item in candidates)
            },
        )

    def test_unmerged_second_section_no_longer_ambiguous(self):
        records = get_section_records(
            file_name=self.control(),
            sections=self.sections,
            fields=["原币/价格"],
        )
        self.assertEqual(
            [record["原币/价格"] for record in records[-2:]],
            [40, 50],
        )

    def test_filled_control_keeps_every_visible_path(self):
        path = os.path.join(self.directory, "filled.xlsx")
        save_unmerged_control(
            self.path,
            path,
            [self.sections[1]],
            fill_merged_cells=True,
        )
        sheet = load_workbook(path)["Prices"]
        self.assertEqual(sheet["I6"].value, "本币")
        self.assertEqual(sheet["K6"].value, "原币")
        stream = iget_section_records(
            file_name=path,
            sections=self.sections,
            fields=self.fields,
        )
        self.assertEqual(
            list(stream.paths("second")),
            [
                "本币/价格",
                "本币/税率",
                "原币/价格",
                "原币/税率",
            ],
        )
        self.assertEqual(
            [item.column_letter
             for item in stream.candidates("second", "原币/价格")],
            ["J", "K"],
        )


class TestSpecificationErrors(unittest.TestCase):
    def test_header_rows_must_be_two(self):
        with self.assertRaises(SectionSpecError):
            Section("bad", (0, 0))
        with self.assertRaises(SectionSpecError):
            Section("bad", (0, 1, 2))
        with self.assertRaises(SectionSpecError):
            Section("bad", None)
        with self.assertRaises(SectionSpecError):
            Section("bad", ("a", "b"))

    def test_columns_must_be_ordered(self):
        with self.assertRaises(SectionSpecError):
            Section("bad", (0, 1), columns=(3, 1))

    def test_sections_must_not_overlap(self):
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as handle:
            make_price_workbook(handle.name)
            with self.assertRaises(SectionSpecError):
                iget_section_records(
                    file_name=handle.name,
                    sections=[
                        Section("one", (0, 1)),
                        Section("two", (1, 2)),
                    ],
                    fields=["本币/价格"],
                )

    def test_sections_with_same_rows_need_separate_columns(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet["B1"] = "本币"
        sheet.merge_cells("B1:G1")
        sheet["B1"] = "本币"
        sheet["B2"] = "价格"
        sheet["C2"] = "价格"
        sheet["D2"] = "价格"
        sheet["E2"] = "价格"
        sheet["F2"] = "价格"
        sheet["G2"] = "价格"
        with tempfile.NamedTemporaryFile(suffix=".xlsx") as handle:
            workbook.save(handle.name)
            with self.assertRaises(SectionSpecError):
                iget_section_records(
                    file_name=handle.name,
                    sections=[
                        Section("one", (0, 1)),
                        Section("two", (0, 1)),
                    ],
                    fields=["本币/价格"],
                )


class TestSideBySideSections(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.path = os.path.join(self.directory, "side.xlsx")
        workbook = Workbook()
        sheet = workbook.active
        sheet["B1"] = "本币"
        sheet.merge_cells("B1:C1")
        sheet["B1"] = "本币"
        sheet["D1"] = "原币"
        sheet["E1"] = "原币"
        sheet["B2"] = "价格"
        sheet["C2"] = "税率"
        sheet["D2"] = "价格"
        sheet["E2"] = "价格"
        sheet["B3"] = "A"
        sheet["C3"] = 1
        sheet["D3"] = 2
        sheet["E3"] = 3

        sheet["G1"] = "本币"
        sheet["H1"] = "原币"
        sheet["G2"] = "价格"
        sheet["H2"] = "价格"
        sheet["G3"] = "B"
        sheet["H3"] = 4
        workbook.save(self.path)
        self.sections = [
            Section("left", (0, 1)),
            Section("right", (0, 1)),
        ]

    def test_columns_stop_at_the_empty_column(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["本币/价格", "原币/价格"],
        )
        self.assertEqual(stream.columns("left"), (1, 4))
        self.assertEqual(stream.columns("right"), (6, 7))

    def test_ambiguity_is_per_side(self):
        stream = iget_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["原币/价格"],
        )
        events = list(stream)
        ambiguous = [event for event in events if event.kind == "ambiguous"]
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0].section, "left")
        self.assertEqual(
            [item.column_letter for item in ambiguous[0].candidates],
            ["D", "E"],
        )
        right = [
            record for record in events
            if record.kind == "record" and record.section == "right"
        ]
        self.assertEqual([record["原币/价格"] for record in right], [4])

    def test_selecting_left_keeps_right_records(self):
        records = get_section_records(
            file_name=self.path,
            sections=self.sections,
            fields=["本币/价格", "原币/价格"],
            selections={"left": {"原币/价格": "E"}},
        )
        self.assertEqual(
            [(record.section, record["原币/价格"]) for record in records],
            [("left", 3), ("right", 4)],
        )


if __name__ == "__main__":
    unittest.main()
