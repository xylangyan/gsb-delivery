"""Tests for read-only page finalization and finalization revert."""

from io import BytesIO

import pytest

from pypdf import (
    FormFinalizationError,
    PdfReader,
    PdfWriter,
    finalize_form_pages,
    get_form_finalizations,
    revert_form_finalization,
)
from pypdf.annotations import Text
from pypdf.constants import FieldDictionaryAttributes
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    RectangleObject,
    TextStringObject,
)

PAGE_WIDTH, PAGE_HEIGHT = 300, 400


def _write(writer: PdfWriter) -> BytesIO:
    out = BytesIO()
    writer.write(out)
    out.seek(0)
    return out


def _reopen(writer: PdfWriter) -> PdfWriter:
    return PdfWriter(clone_from=PdfReader(_write(writer)))


def _form_stream(writer, bbox, data, resources=None):
    stream = DecodedStreamObject()
    stream[NameObject("/Type")] = NameObject("/XObject")
    stream[NameObject("/Subtype")] = NameObject("/Form")
    stream[NameObject("/BBox")] = RectangleObject(bbox)
    if resources is not None:
        stream[NameObject("/Resources")] = resources
    stream.set_data(data)
    return writer._add_object(stream)


def _page_annots(page):
    annots = page.get(NameObject("/Annots"))
    if annots is None:
        annots = ArrayObject()
        page[NameObject("/Annots")] = annots
    return annots


def _build_two_page_form(*, unison=False, same_state=True):
    """
    Page 0: radio group ``Q`` box (on state /X), text field ``Name`` box.
    Page 1: radio group ``Q`` second box, text field ``Name`` second box.

    The two radio boxes intentionally use the same appearance state name /X,
    so only the per-widget /AS can tell which box is selected.
    """
    writer = PdfWriter()
    page1 = writer.add_blank_page(PAGE_WIDTH, PAGE_HEIGHT)
    page2 = writer.add_blank_page(PAGE_WIDTH, PAGE_HEIGHT)

    helv = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    helv_ref = writer._add_object(helv)
    acro = DictionaryObject(
        {
            NameObject("/Fields"): ArrayObject(),
            NameObject("/DR"): DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/Helv"): helv_ref})}
            ),
            NameObject("/DA"): TextStringObject("/Helv 10 Tf 0 g"),
        }
    )
    acro_ref = writer._add_object(acro)
    writer.root_object[NameObject("/AcroForm")] = acro_ref

    # Radio group, one kid per page.
    flags = FieldDictionaryAttributes.FfBits.Radio
    if unison:
        flags |= FieldDictionaryAttributes.FfBits.RadiosInUnison
    radio = DictionaryObject(
        {
            NameObject("/FT"): NameObject("/Btn"),
            NameObject("/T"): TextStringObject("Q"),
            NameObject("/Ff"): NumberObject(int(flags)),
            NameObject("/V"): NameObject("/X"),
            NameObject("/Kids"): ArrayObject(),
        }
    )
    radio_ref = writer._add_object(radio)
    acro["/Fields"].append(radio_ref)

    def radio_widget(page, rect, as_state, on_data):
        on_stream = _form_stream(writer, (0, 0, 10, 10), on_data)
        off_stream = _form_stream(writer, (0, 0, 10, 10), b"/Tx BMC\nEMC\n")
        ap = DictionaryObject(
            {
                NameObject("/N"): DictionaryObject(
                    {NameObject("/Off"): off_stream, NameObject("/X"): on_stream}
                )
            }
        )
        widget = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/Rect"): RectangleObject(rect),
                NameObject("/F"): NumberObject(4),
                NameObject("/P"): page.indirect_reference,
                NameObject("/Parent"): radio_ref,
                NameObject("/AS"): NameObject(as_state),
                NameObject("/AP"): ap,
            }
        )
        widget_ref = writer._add_object(widget)
        _page_annots(page).append(widget_ref)
        radio["/Kids"].append(widget_ref)
        return widget_ref

    on_marker = b"q\n1 0 0 rg\n2 2 6 6 re f\nQ\n"
    widget1 = radio_widget(page1, (50, 300, 60, 310), "/X", on_marker)
    widget2_state = "/X" if same_state else "/Y"
    widget2 = radio_widget(page2, (50, 300, 60, 310), "/Off", on_marker)
    if not same_state:
        # Give the second box its own state name while keeping /X available too.
        widget2.get_object()["/AP"]["/N"][NameObject("/Y")] = widget2.get_object()["/AP"]["/N"]["/X"]
        widget2_state = "/Y"

    # Text field with a box on each page, value lives on the field.
    text_field = DictionaryObject(
        {
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("Name"),
            NameObject("/V"): TextStringObject("Alice"),
            NameObject("/Kids"): ArrayObject(),
        }
    )
    text_ref = writer._add_object(text_field)
    acro["/Fields"].append(text_ref)

    def text_widget(page, rect):
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/Helv"): helv_ref})}
        )
        appearance = _form_stream(
            writer,
            (0, 0, 100, 20),
            b"BT\n/Helv 10 Tf\n2 5 Td\n(Alice) Tj\nET\n",
            resources=resources,
        )
        widget = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/Rect"): RectangleObject(rect),
                NameObject("/F"): NumberObject(4),
                NameObject("/P"): page.indirect_reference,
                NameObject("/Parent"): text_ref,
                NameObject("/DA"): TextStringObject("/Helv 10 Tf 0 g"),
                NameObject("/AP"): DictionaryObject({NameObject("/N"): appearance}),
            }
        )
        widget_ref = writer._add_object(widget)
        _page_annots(page).append(widget_ref)
        text_field["/Kids"].append(widget_ref)
        return widget_ref

    text1 = text_widget(page1, (100, 200, 200, 220))
    text2 = text_widget(page2, (100, 200, 200, 220))

    return writer, {
        "page1": page1,
        "page2": page2,
        "radio": radio,
        "radio_widget1": widget1,
        "radio_widget2": widget2,
        "text": text_field,
        "text_widget1": text1,
        "text_widget2": text2,
        "widget2_state": widget2_state,
    }


def _reorder(writer: PdfWriter, new_order):
    pages_root = writer._pages.get_object()
    refs = [writer.flattened_pages[i].indirect_reference for i in new_order]
    pages_root[NameObject("/Kids")] = ArrayObject(refs)
    writer.flattened_pages = [writer.flattened_pages[i] for i in new_order]
    writer._page_id2num = {}


def _widgets(page):
    return [
        annot.get_object()
        for annot in page.get(NameObject("/Annots"), ArrayObject())
        if annot.get_object().get(NameObject("/Subtype")) == NameObject("/Widget")
    ]


def _content_data(page):
    contents = page.get(NameObject("/Contents"))
    assert contents is not None
    return contents.get_object().get_data()


def test_finalize_freezes_first_page_keeps_second_editable():
    writer, parts = _build_two_page_form()
    result = finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")

    assert result == {"issue_id": "issue-1", "frozen_widgets": 2}
    assert _widgets(parts["page1"]) == []
    assert len(_widgets(parts["page2"])) == 2

    # The frozen page keeps painting the appearances via dedicated XObjects.
    data = _content_data(parts["page1"])
    assert b"PyPDFFreezeBegin" in data
    assert b" Do\n" in data
    xobjects = parts["page1"]["/Resources"]["/XObject"]
    assert len(xobjects) == 2

    # The fields still exist through the boxes of page 2 and keep their values.
    fields = writer.get_fields()
    assert set(fields) == {"Q", "Name"}
    assert fields["Q"]["/V"] == "/X"
    assert fields["Name"]["/V"] == "Alice"

    # The archive remembers issue, page and per-widget identity.
    archived = get_form_finalizations(writer)
    assert len(archived) == 1
    entry = archived[0]
    assert entry["issue_id"] == "issue-1"
    assert {w["field_name"] for w in entry["widgets"]} == {"Q", "Name"}
    assert all(w["kid_index"] >= 0 for w in entry["widgets"])


def test_frozen_display_survives_write_and_reopen():
    writer, parts = _build_two_page_form()
    finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")
    reopened = _reopen(writer)

    page1 = reopened.pages[0]
    assert _widgets(page1) == []
    data = _content_data(page1)
    assert b"PyPDFFreezeBegin" in data
    # The frozen text is still rendered on the finalized page.
    assert "Alice" in page1.extract_text()
    assert len(get_form_finalizations(reopened)) == 1


def test_radio_boxes_with_same_state_name_keep_distinct_selection():
    writer, parts = _build_two_page_form(same_state=True)
    finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")

    radio = parts["radio"]
    kid2 = radio["/Kids"][0].get_object()
    # Only the second box remains; field value and its /AS were not touched.
    assert len(radio["/Kids"]) == 1
    assert kid2["/AS"] == "/Off"
    assert radio["/V"] == "/X"

    # The filler selects the still-editable second box.
    radio[NameObject("/V")] = NameObject("/X")
    kid2[NameObject("/AS")] = NameObject("/X")

    revert_form_finalization(writer, "issue-1")
    fields = writer.get_fields()
    assert fields["Q"]["/V"] == "/X"
    restored = parts["radio"]
    states = [kid.get_object()["/AS"] for kid in restored["/Kids"]]
    assert sorted(states) == ["/Off", "/X"]


def test_radios_in_unison_flag_is_preserved():
    writer, parts = _build_two_page_form(unison=True)
    flags = int(parts["radio"]["/Ff"])
    finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")
    assert int(parts["radio"]["/Ff"]) == flags

    revert_form_finalization(writer, "issue-1")
    assert int(writer.get_fields()["Q"]["/Ff"]) == flags
    states = [kid.get_object()["/AS"] for kid in writer.get_fields()["Q"]["/Kids"]]
    assert states == ["/X", "/X"]


def test_revert_joins_the_current_radio_answer():
    writer, parts = _build_two_page_form(same_state=False)
    finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")

    # Before the revert, the filler answers differently on page 2.
    radio = parts["radio"]
    remaining = radio["/Kids"][0].get_object()
    radio[NameObject("/V")] = NameObject("/Y")
    remaining[NameObject("/AS")] = NameObject("/Y")

    revert_form_finalization(writer, "issue-1")
    assert writer.get_fields()["Q"]["/V"] == "/Y"
    by_state = {kid.get_object()["/AS"]: kid.get_object() for kid in radio["/Kids"]}
    assert set(by_state) == {"/Off", "/Y"}
    # The restored box shows the current answer, not its frozen-time selection.
    restored_boxes = [
        kid.get_object() for kid in radio["/Kids"] if kid.get_object()["/AS"] == "/Off"
    ]
    assert len(restored_boxes) == 1
    assert restored_boxes[0]["/P"].get_object() is parts["page1"]


def test_revert_joins_the_current_text_answer():
    writer, parts = _build_two_page_form()
    finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")

    writer.update_page_form_field_values(
        writer.pages[1], {"Name": "Bob"}, auto_regenerate=False
    )
    assert writer.get_fields()["Name"]["/V"] == "Bob"

    revert_form_finalization(writer, "issue-1")
    field = writer.get_fields()["Name"]
    assert field["/V"] == "Bob"
    assert len(field["/Kids"]) == 2
    restored_page = parts["page1"]
    assert len(_widgets(restored_page)) == 2
    # The regenerated appearance of the restored box shows the current value.
    restored_text = next(
        annot.get_object()
        for annot in restored_page["/Annots"]
        if annot.get_object()["/AP"]["/N"].get_object().get_data().find(b"Bob") != -1
    )
    assert restored_text["/Subtype"] == "/Widget"


def test_fully_frozen_field_keeps_frozen_value_until_revert():
    writer, parts = _build_two_page_form()
    finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")
    finalize_form_pages(writer, writer.pages[1], issue_id="issue-2")

    # No editable entry remains, so the field leaves the data-entry tree.
    assert writer.get_fields() == {}

    revert_form_finalization(writer, "issue-2")
    field = writer.get_fields()["Q"]
    assert field["/V"] == "/X"
    assert len(field["/Kids"]) == 1
    assert field["/Kids"][0].get_object()["/AS"] == "/Off"

    revert_form_finalization(writer, "issue-1")
    field = writer.get_fields()["Q"]
    assert field["/V"] == "/X"
    assert sorted(kid.get_object()["/AS"] for kid in field["/Kids"]) == ["/Off", "/X"]
    assert get_form_finalizations(writer) == []


def test_revert_removes_only_its_own_stamp_and_keeps_other_content():
    writer, parts = _build_two_page_form()
    page1 = parts["page1"]
    # Artwork originally drawn on the page.
    original = DecodedStreamObject()
    original.set_data(b"0.2 0.2 0.2 rg\n10 10 5 5 re f\n")
    page1[NameObject("/Contents")] = writer._add_object(original)

    # A reviewer annotation existing before the finalization.
    note_before = Text(rect=(10, 380, 30, 395), text="before")
    writer.add_annotation(0, note_before)

    finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")
    # Another annotation added after sending the finalized page out.
    note_after = Text(rect=(40, 380, 60, 395), text="after")
    writer.add_annotation(0, note_after)
    # A second, still-effective finalization on page 2 sharing appearance data.
    finalize_form_pages(writer, writer.pages[1], issue_id="issue-2")

    revert_form_finalization(writer, "issue-1")
    data = _content_data(page1)
    assert b"PyPDFFreezeBegin" not in data
    assert b"0.2 0.2 0.2 rg" in data  # original artwork stays
    note_texts = {
        annot.get_object().get("/Contents") for annot in page1["/Annots"]
    }
    assert "before" in note_texts and "after" in note_texts
    # The other finalization's stamp is still in place.
    assert b"PyPDFFreezeBegin" in _content_data(parts["page2"])
    assert len(get_form_finalizations(writer)) == 1


def test_shared_appearance_gets_independent_drawings():
    writer, parts = _build_two_page_form()
    finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")
    finalize_form_pages(writer, writer.pages[1], issue_id="issue-2")

    names1 = list(parts["page1"]["/Resources"]["/XObject"].keys())
    names2 = list(parts["page2"]["/Resources"]["/XObject"].keys())
    assert set(names1).isdisjoint(names2)

    revert_form_finalization(writer, "issue-1")
    assert b"PyPDFFreezeBegin" not in _content_data(parts["page1"])
    assert b"PyPDFFreezeBegin" in _content_data(parts["page2"])
    assert parts["page2"]["/Resources"]["/XObject"]  # second stamp resource intact


def test_reopen_reorder_then_revert_finds_page_by_identity():
    writer, parts = _build_two_page_form()
    finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")
    writer.update_page_form_field_values(
        writer.pages[1], {"Name": "Carol"}, auto_regenerate=False
    )
    reopened = _reopen(writer)
    # Move the finalized page to the end; the archive must follow the page.
    _reorder(reopened, [1, 0])
    assert _widgets(reopened.pages[1]) == []

    revert_form_finalization(reopened, "issue-1")
    moved_page = reopened.pages[1]
    assert b"PyPDFFreezeBegin" not in _content_data(moved_page)
    widgets = _widgets(moved_page)
    assert len(widgets) == 2
    field = reopened.get_fields()["Name"]
    assert field["/V"] == "Carol"
    pages_with_box = {
        kid.get_object()["/P"].get_object().indirect_reference.idnum
        for kid in field["/Kids"]
    }
    assert pages_with_box == {page.indirect_reference.idnum for page in reopened.pages}


def test_same_partial_name_under_different_parents_stays_distinct():
    writer = PdfWriter()
    page = writer.add_blank_page(PAGE_WIDTH, PAGE_HEIGHT)
    acro = DictionaryObject(
        {NameObject("/Fields"): ArrayObject(), NameObject("/DA"): TextStringObject("/Helv 10 Tf")}
    )
    writer.root_object[NameObject("/AcroForm")] = writer._add_object(acro)

    roots = []
    for root_name in ("A", "B"):
        root = DictionaryObject(
            {
                NameObject("/FT"): NameObject("/Tx"),
                NameObject("/T"): TextStringObject(root_name),
                NameObject("/V"): TextStringObject(root_name.lower()),
                NameObject("/Kids"): ArrayObject(),
            }
        )
        root_ref = writer._add_object(root)
        acro["/Fields"].append(root_ref)
        roots.append(root)
        appearance = _form_stream(writer, (0, 0, 40, 12), b"BT /Helv 10 Tf 0 0 Td (x) Tj ET")
        widget = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Widget"),
                NameObject("/Rect"): RectangleObject((10, 100, 50, 112)),
                NameObject("/P"): page.indirect_reference,
                NameObject("/Parent"): root_ref,
                NameObject("/AP"): DictionaryObject({NameObject("/N"): appearance}),
            }
        )
        widget_ref = writer._add_object(widget)
        _page_annots(page).append(widget_ref)
        root["/Kids"].append(widget_ref)

    finalize_form_pages(writer, page, issue_id="issue-1")
    # Both boxes were the only entries of their fields, so the fields leave the
    # data-entry tree until the revert rebuilds them.
    assert writer.get_fields() == {}
    revert_form_finalization(writer, "issue-1")
    fields = writer.get_fields()
    assert set(fields) == {"A", "B"}
    assert fields["A"]["/V"] == "a"
    assert fields["B"]["/V"] == "b"
    parents = {kid.get_object()["/Parent"].get_object()["/T"] for kid in fields["A"]["/Kids"]}
    assert parents == {"A"}


def test_duplicate_issue_id_and_unknown_revert_are_rejected():
    writer, _ = _build_two_page_form()
    finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")
    with pytest.raises(FormFinalizationError, match="already in use"):
        finalize_form_pages(writer, writer.pages[1], issue_id="issue-1")
    with pytest.raises(FormFinalizationError, match="Unknown finalization"):
        revert_form_finalization(writer, "missing")


def test_finalize_without_form_raises():
    writer = PdfWriter()
    writer.add_blank_page(PAGE_WIDTH, PAGE_HEIGHT)
    with pytest.raises(FormFinalizationError, match="no interactive form"):
        finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")


def test_nested_field_chain_is_rebuilt_after_full_freeze():
    writer = PdfWriter()
    page = writer.add_blank_page(PAGE_WIDTH, PAGE_HEIGHT)
    acro = DictionaryObject(
        {NameObject("/Fields"): ArrayObject(), NameObject("/DA"): TextStringObject("/Helv 10 Tf")}
    )
    writer.root_object[NameObject("/AcroForm")] = writer._add_object(acro)

    # Group -> Child -> widget, a three-level qualified name "Group.Child".
    group = DictionaryObject(
        {NameObject("/T"): TextStringObject("Group"), NameObject("/Kids"): ArrayObject()}
    )
    group_ref = writer._add_object(group)
    acro["/Fields"].append(group_ref)
    child = DictionaryObject(
        {
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject("Child"),
            NameObject("/Parent"): group_ref,
            NameObject("/V"): TextStringObject("nested"),
            NameObject("/Kids"): ArrayObject(),
        }
    )
    child_ref = writer._add_object(child)
    group["/Kids"].append(child_ref)
    appearance = _form_stream(writer, (0, 0, 40, 12), b"BT /Helv 10 Tf 0 0 Td (x) Tj ET")
    widget = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/Rect"): RectangleObject((10, 100, 50, 112)),
            NameObject("/P"): page.indirect_reference,
            NameObject("/Parent"): child_ref,
            NameObject("/AP"): DictionaryObject({NameObject("/N"): appearance}),
        }
    )
    widget_ref = writer._add_object(widget)
    _page_annots(page).append(widget_ref)
    child["/Kids"].append(widget_ref)

    finalize_form_pages(writer, page, issue_id="issue-1")
    assert writer.get_fields() == {}

    revert_form_finalization(writer, "issue-1")
    fields = writer.get_fields()
    # The intermediate node and the terminal field are both reported, just as
    # on the original document.
    assert set(fields) == {"Group", "Group.Child"}
    assert fields["Group.Child"]["/V"] == "nested"
    restored = fields["Group.Child"].indirect_reference.get_object()
    assert restored["/Parent"].get_object()["/T"] == "Group"
    assert restored["/Kids"][0].get_object()["/P"].get_object() is page


def test_root_widget_field_without_parent_round_trips():
    writer = PdfWriter()
    page = writer.add_blank_page(PAGE_WIDTH, PAGE_HEIGHT)
    acro = DictionaryObject({NameObject("/Fields"): ArrayObject()})
    writer.root_object[NameObject("/AcroForm")] = writer._add_object(acro)

    on_stream = _form_stream(writer, (0, 0, 10, 10), b"q 1 0 0 rg 2 2 6 6 re f Q")
    off_stream = _form_stream(writer, (0, 0, 10, 10), b"/Tx BMC\nEMC\n")
    checkbox = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Btn"),
            NameObject("/T"): TextStringObject("Agree"),
            NameObject("/Rect"): RectangleObject((10, 100, 20, 110)),
            NameObject("/P"): page.indirect_reference,
            NameObject("/V"): NameObject("/Yes"),
            NameObject("/AS"): NameObject("/Yes"),
            NameObject(
                "/AP"
            ): DictionaryObject(
                {
                    NameObject("/N"): DictionaryObject(
                        {NameObject("/Off"): off_stream, NameObject("/Yes"): on_stream}
                    )
                }
            ),
        }
    )
    checkbox_ref = writer._add_object(checkbox)
    acro["/Fields"].append(checkbox_ref)
    _page_annots(page).append(checkbox_ref)

    finalize_form_pages(writer, page, issue_id="issue-1")
    assert writer.get_fields() == {}
    revert_form_finalization(writer, "issue-1")

    field = writer.get_fields()["Agree"].indirect_reference.get_object()
    assert field["/V"] == "/Yes"
    assert field["/AS"] == "/Yes"
    assert field["/Subtype"] == "/Widget"
    assert field["/P"].get_object() is page


def test_each_issue_keeps_its_own_archived_value():
    writer, parts = _build_two_page_form()
    finalize_form_pages(writer, writer.pages[0], issue_id="issue-1")
    # The still-editable second page receives a different answer, then is also
    # finalized in a second outgoing issue.
    writer.update_page_form_field_values(
        writer.pages[1], {"Name": "Bob"}, auto_regenerate=False
    )
    finalize_form_pages(writer, writer.pages[1], issue_id="issue-2")

    archived = {item["issue_id"]: item for item in get_form_finalizations(writer)}
    value_1 = archived["issue-1"]["widgets"]
    value_2 = archived["issue-2"]["widgets"]
    name_1 = next(w for w in value_1 if w["field_name"] == "Name")
    name_2 = next(w for w in value_2 if w["field_name"] == "Name")
    # The two boxes of one field are told apart by the page they sat on and
    # their rectangle, not by the (mutable) position in the kids array.
    assert name_1["page"] != name_2["page"]
    assert name_1["rect"] == name_2["rect"]
    assert name_1["kid_index"] == name_2["kid_index"] == 0

    # Revert the first issue; the second, still-finalized issue keeps its own
    # recorded value rather than being rewritten.
    revert_form_finalization(writer, "issue-1")
    remaining = {item["issue_id"]: item for item in get_form_finalizations(writer)}
    assert set(remaining) == {"issue-2"}
    # The reverted box reconnects to the newest answer reachable through issue-2.
    assert writer.get_fields()["Name"]["/V"] == "Bob"
    assert len(writer.get_fields()["Name"]["/Kids"]) == 1
