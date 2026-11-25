"""Finalize (freeze) form pages as read-only and revert a finalization later.

Intake pipelines sometimes have to send out a few pages of a form as a
read-only finalization while the remaining pages keep being filled in the very
same PDF. Reviewers may later send a single finalized page back for edits.

The two operations offered here are designed around that workflow:

* :func:`finalize_form_pages` bakes the current display of every widget of the
  given pages into the page contents, removes those widgets from the editable
  form (the page annotations, the field kids and the AcroForm fields) and
  records an archive entry describing the finalization.
* :func:`revert_form_finalization` undoes one recorded finalization, identified
  by the finalization id issued when the pages were sent out. It removes only
  the read-only drawing produced by that finalization and puts the widgets back
  as editable entries, reconnecting them to the answers currently held by the
  field tree.

The archive is stored below a private catalog key
(``/PyPDFFormFinalizations``) so that it survives writing the PDF to disk and
reopening it. Widgets are identified by their place in the field tree (the
fully qualified field name plus their position among the parent field kids),
not by an object number: two boxes may share a partial name while living under
different parent fields, and one field may own boxes on several pages.
"""

from __future__ import annotations

from typing import Any, cast

from .constants import (
    AnnotationDictionaryAttributes,
    CatalogAttributes,
    FieldDictionaryAttributes,
    InteractiveFormDictEntries,
    PageAttributes,
)
from .errors import PyPdfError
from .generic import (
    ArrayObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NullObject,
    NumberObject,
    RectangleObject,
    StreamObject,
    TextStringObject,
)
from .generic._appearance_stream import TextStreamAppearance

ARCHIVE_KEY = NameObject("/PyPDFFormFinalizations")
ISSUES_KEY = NameObject("/Issues")
SEQUENCE_KEY = NameObject("/NextSequence")
VERSION_KEY = NameObject("/Version")
ARCHIVE_VERSION = 1

BEGIN_MARKER = "% PyPDFFreezeBegin"
END_MARKER = "% PyPDFFreezeEnd"

_WIDGET_SUBTYPE = NameObject("/Widget")
_OFF_STATE = NameObject("/Off")
_STREAM_META_KEYS = (NameObject("/Length"), NameObject("/Filter"), NameObject("/DecodeParms"))


class FormFinalizationError(PyPdfError):
    """A form finalization or revert operation could not be performed."""


def _acroform(writer: Any) -> DictionaryObject:
    acroform = writer.root_object.get(CatalogAttributes.ACRO_FORM)
    if isinstance(acroform, IndirectObject):
        acroform = acroform.get_object()
    if not isinstance(acroform, DictionaryObject):
        raise FormFinalizationError("The document has no interactive form (/AcroForm).")
    return acroform


def _get_archive(writer: Any) -> DictionaryObject | None:
    archive = writer.root_object.get(ARCHIVE_KEY)
    if isinstance(archive, IndirectObject):
        archive = archive.get_object()
    return archive if isinstance(archive, DictionaryObject) else None


def _ensure_archive(writer: Any) -> DictionaryObject:
    """Return the finalization archive dictionary, creating it on first use."""
    archive = _get_archive(writer)
    if archive is not None:
        if not isinstance(archive.get(ISSUES_KEY), ArrayObject):
            archive[ISSUES_KEY] = ArrayObject()
        if SEQUENCE_KEY not in archive:
            archive[SEQUENCE_KEY] = NumberObject(1)
        return archive
    archive = DictionaryObject()
    archive[VERSION_KEY] = NumberObject(ARCHIVE_VERSION)
    archive[ISSUES_KEY] = ArrayObject()
    archive[SEQUENCE_KEY] = NumberObject(1)
    writer.root_object[ARCHIVE_KEY] = writer._add_object(archive)
    return archive


def get_form_finalizations(writer: Any) -> list[dict[str, Any]]:
    """
    Return the recorded finalizations as plain Python dictionaries.

    Each entry exposes the finalization id, the finalized page references and
    the archived widgets. This is read-only information about the state of the
    document; mutating the returned dictionaries does not change the PDF.
    """
    archive = _get_archive(writer)
    if archive is None:
        return []
    issues = archive.get(ISSUES_KEY)
    if not isinstance(issues, ArrayObject):
        return []
    result: list[dict[str, Any]] = []
    for issue in issues:
        issue = issue.get_object()
        widgets = []
        for widget_entry in issue.get(NameObject("/Widgets"), ArrayObject()):
            widget_entry = widget_entry.get_object()
            widgets.append(
                {
                    "field_name": str(widget_entry.get(NameObject("/FieldName"), "")),
                    "kid_index": int(widget_entry.get(NameObject("/KidIndex"), -1)),
                    "page": widget_entry.get(NameObject("/Page")),
                    "rect": list(widget_entry.get(NameObject("/Rect"), ArrayObject())),
                }
            )
        result.append(
            {
                "issue_id": str(issue.get(NameObject("/IssueID"), "")),
                "pages": list(issue.get(NameObject("/Pages"), ArrayObject())),
                "widgets": widgets,
            }
        )
    return result


def _snapshot_object(obj: Any, writer: Any, memo: dict[tuple[int, int], IndirectObject]) -> Any:
    """
    Deep-copy a PDF object graph into independent objects owned by *writer*.

    Indirect references are followed and duplicated, so the snapshot never
    shares a mutable dictionary or stream with the live form. Target objects
    are registered in *memo* before their content is copied, which makes
    reference cycles (widget -> parent -> kids -> widget) safe. Scalars are
    immutable and may be reused.
    """
    if isinstance(obj, IndirectObject):
        memo_key = (obj.idnum, obj.generation)
        if memo_key in memo:
            return memo[memo_key]
        resolved = obj.get_object()
        if isinstance(resolved, StreamObject):
            clone = DecodedStreamObject()
            clone.set_data(resolved.get_data())
            ref = writer._add_object(clone)
            memo[memo_key] = ref
            for key, value in resolved.items():
                if key in _STREAM_META_KEYS:
                    continue
                clone[NameObject(str(key))] = _snapshot_object(value, writer, memo)
            return ref
        if isinstance(resolved, DictionaryObject):
            clone = DictionaryObject()
            ref = writer._add_object(clone)
            memo[memo_key] = ref
            for key, value in resolved.items():
                clone[NameObject(str(key))] = _snapshot_object(value, writer, memo)
            return ref
        if isinstance(resolved, ArrayObject):
            clone = ArrayObject()
            ref = writer._add_object(clone)
            memo[memo_key] = ref
            for value in resolved:
                clone.append(_snapshot_object(value, writer, memo))
            return ref
        return _snapshot_object(resolved, writer, memo)
    if isinstance(obj, StreamObject):
        clone = DecodedStreamObject()
        clone.set_data(obj.get_data())
        for key, value in obj.items():
            if key in _STREAM_META_KEYS:
                continue
            clone[NameObject(str(key))] = _snapshot_object(value, writer, memo)
        return clone
    if isinstance(obj, DictionaryObject):
        clone = DictionaryObject()
        for key, value in obj.items():
            clone[NameObject(str(key))] = _snapshot_object(value, writer, memo)
        return clone
    if isinstance(obj, ArrayObject):
        return ArrayObject(_snapshot_object(value, writer, memo) for value in obj)
    return obj


# Widget entries that must not be deep-copied into the widget snapshot: /P is
# reconnected on revert, and /Parent would pull the whole (cyclic) field tree.
_WIDGET_SNAPSHOT_SKIP = frozenset(
    {NameObject("/Parent"), AnnotationDictionaryAttributes.P}
)


def _snapshot_widget(writer: Any, widget: DictionaryObject) -> IndirectObject:
    """Deep-copy one widget without its page and parent links."""
    clone = DictionaryObject()
    for key, value in widget.items():
        if key in _WIDGET_SNAPSHOT_SKIP:
            continue
        clone[NameObject(str(key))] = _snapshot_object(value, writer, {})
    return writer._add_object(clone)


def _snapshot_parent_chain(writer: Any, widget: DictionaryObject) -> ArrayObject:
    """
    Copy the parent field nodes of a widget, root field first.

    Kid/parent links are excluded: they would reintroduce the field tree, and
    the chain is reattached to the live (or rebuilt) structure on revert.
    """
    chain: list[DictionaryObject] = []
    node = _field_parent(widget)
    while node is not None:
        chain.append(node)
        node = _field_parent(node)
    chain.reverse()

    copies = ArrayObject()
    for node in chain:
        copy = DictionaryObject()
        for key, value in node.items():
            if key in (
                FieldDictionaryAttributes.Kids,
                FieldDictionaryAttributes.Parent,
            ):
                continue
            copy[NameObject(str(key))] = _snapshot_object(value, writer, {})
        copies.append(writer._add_object(copy))
    return copies


def _page_widgets(page: Any) -> list[DictionaryObject]:
    annots = page.get(PageAttributes.ANNOTS)
    if not isinstance(annots, ArrayObject):
        return []
    return [
        annot.get_object()
        for annot in annots
        if annot.get_object().get(AnnotationDictionaryAttributes.Subtype) == _WIDGET_SUBTYPE
    ]


def _field_parent(widget: DictionaryObject) -> DictionaryObject | None:
    parent = widget.get(FieldDictionaryAttributes.Parent)
    if isinstance(parent, IndirectObject):
        parent = parent.get_object()
    return parent if isinstance(parent, DictionaryObject) else None


def _qualified_name(writer: Any, widget: DictionaryObject) -> str:
    parent = _field_parent(widget)
    if parent is None:
        return str(widget.get(FieldDictionaryAttributes.T, ""))
    return writer._get_qualified_field_name(parent=parent)


def _kid_index(widget: DictionaryObject) -> int:
    parent = _field_parent(widget)
    if parent is None:
        return -1
    kids = parent.get(FieldDictionaryAttributes.Kids)
    if not isinstance(kids, ArrayObject):
        return -1
    widget_ref = widget.indirect_reference
    for index, kid in enumerate(kids):
        if (
            isinstance(kid, IndirectObject)
            and widget_ref is not None
            and kid.idnum == widget_ref.idnum
        ) or kid.get_object() is widget:
            return index
    return -1


def _on_state(widget: DictionaryObject) -> NameObject | None:
    """Return the non-/Off appearance state name of a button widget, if any."""
    appearance = widget.get(AnnotationDictionaryAttributes.AP)
    if not isinstance(appearance, DictionaryObject):
        return None
    normal = appearance.get(NameObject("/N"))
    if normal is None:
        return None
    normal = normal.get_object()
    if not isinstance(normal, DictionaryObject):
        return None
    for state in normal.keys():
        if state != _OFF_STATE:
            return NameObject(str(state))
    return None


def _normal_appearance_stream(widget: DictionaryObject) -> StreamObject | None:
    appearance = widget.get(AnnotationDictionaryAttributes.AP)
    if not isinstance(appearance, DictionaryObject):
        return None
    normal = appearance.get(NameObject("/N"))
    if normal is None:
        return None
    normal = normal.get_object()
    if isinstance(normal, StreamObject):
        # Text / choice widgets: /N is the appearance stream directly.
        return normal
    if isinstance(normal, DictionaryObject):
        # Button widgets: /N maps appearance states to streams.
        state = widget.get(AnnotationDictionaryAttributes.AS, _OFF_STATE)
        stream = normal.get(cast(str, state)) or normal.get(str(_OFF_STATE))
        if isinstance(stream, IndirectObject):
            stream = stream.get_object()
        if isinstance(stream, StreamObject):
            return stream
    return None


def _ensure_text_appearance(writer: Any, page: Any, widget: DictionaryObject) -> None:
    """Generate a normal appearance stream for a text/choice widget lacking one."""
    if AnnotationDictionaryAttributes.AP in widget:
        appearance = widget[AnnotationDictionaryAttributes.AP]
        if isinstance(appearance, DictionaryObject) and NameObject("/N") in appearance:
            return
    field = _field_parent(widget) or widget
    if field.get(FieldDictionaryAttributes.FT) not in (NameObject("/Tx"), NameObject("/Ch")):
        return
    stream = TextStreamAppearance.from_text_annotation(
        writer,
        page,
        flatten=False,
        acro_form=_acroform(writer),
        field=field,
        annotation=widget,
    )
    if AnnotationDictionaryAttributes.AP not in widget:
        widget[NameObject(AnnotationDictionaryAttributes.AP)] = DictionaryObject()
    cast(DictionaryObject, widget[AnnotationDictionaryAttributes.AP])[
        NameObject("/N")
    ] = writer._add_object(stream)


def _marker_token(issue_id: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in issue_id)


def _is_null(value: Any) -> bool:
    return value is None or isinstance(value, NullObject)


def _stamp_on_page(
    writer: Any,
    page: Any,
    widget: DictionaryObject,
    issue_id: str,
    sequence: int,
) -> NameObject:
    """
    Draw the widget's current normal appearance on the page as read-only ink.

    Every frozen widget gets its own form XObject and its own marked content
    block, even when two widgets share an identical appearance stream: the
    drawing occurrences stay independently removable.
    """
    appearance_stream = _normal_appearance_stream(widget)
    if appearance_stream is None:
        raise FormFinalizationError(
            f"Widget {_qualified_name(writer, widget)!r} has no appearance stream to freeze."
        )
    rectangle = cast(RectangleObject, widget[AnnotationDictionaryAttributes.Rect])
    bbox = appearance_stream.get(NameObject("/BBox"))
    if bbox is None:
        # Fall back to the widget rectangle without scaling.
        bbox = RectangleObject(rectangle)
    elif isinstance(bbox, IndirectObject):
        bbox = bbox.get_object()

    xobject = DecodedStreamObject()
    xobject.set_data(appearance_stream.get_data())
    for key, value in appearance_stream.items():
        if key in _STREAM_META_KEYS:
            continue
        xobject[NameObject(str(key))] = _snapshot_object(value, writer, {})
    xobject_ref = writer._add_object(xobject)

    resources = page.get(PageAttributes.RESOURCES)
    if not isinstance(resources, DictionaryObject):
        resources = DictionaryObject()
        page[NameObject(PageAttributes.RESOURCES)] = resources
    xobjects = resources.get(NameObject("/XObject"))
    if not isinstance(xobjects, DictionaryObject):
        xobjects = DictionaryObject()
        resources[NameObject("/XObject")] = xobjects
    xobject_name = NameObject(f"/Frz{sequence}_{xobject_ref.idnum}")._sanitize()
    xobjects[xobject_name] = xobject_ref

    width = max(float(bbox[2]) - float(bbox[0]), 1e-6)
    height = max(float(bbox[3]) - float(bbox[1]), 1e-6)
    scale_x = (float(rectangle[2]) - float(rectangle[0])) / width
    scale_y = (float(rectangle[3]) - float(rectangle[1])) / height
    translate_x = float(rectangle[0]) - float(bbox[0]) * scale_x
    translate_y = float(rectangle[1]) - float(bbox[1]) * scale_y
    matrix = (
        f"{FloatObject(scale_x)} 0 0 {FloatObject(scale_y)} "
        f"{FloatObject(translate_x)} {FloatObject(translate_y)} cm"
    )
    token = _marker_token(issue_id)
    block = (
        f"{BEGIN_MARKER} issue={token} widget={sequence}\n"
        f"q\n{matrix}\n{xobject_name} Do\nQ\n"
        f"{END_MARKER} issue={token} widget={sequence}\n"
    ).encode("utf-8")
    writer._merge_content_stream_to_page(page, block)
    return xobject_name


def _detach_widget(writer: Any, page: Any, widget: DictionaryObject) -> None:
    """Remove a live widget from the page and the field/AcroForm structure."""
    annots = page.get(PageAttributes.ANNOTS)
    widget_ref = widget.indirect_reference
    if isinstance(annots, ArrayObject):
        for index, annot in enumerate(list(annots)):
            if (
                isinstance(annot, IndirectObject)
                and widget_ref is not None
                and annot.idnum == widget_ref.idnum
            ) or annot.get_object() is widget:
                del annots[index]
                break

    parent = _field_parent(widget)
    if parent is not None:
        kids = parent.get(FieldDictionaryAttributes.Kids)
        if isinstance(kids, ArrayObject):
            for index, kid in enumerate(list(kids)):
                if (
                    isinstance(kid, IndirectObject)
                    and widget_ref is not None
                    and kid.idnum == widget_ref.idnum
                ) or kid.get_object() is widget:
                    del kids[index]
                    break
        _prune_empty_fields(writer, parent)
        return

    acroform = _acroform(writer)
    fields = acroform.get(InteractiveFormDictEntries.Fields)
    if isinstance(fields, ArrayObject):
        for index, field_ref in enumerate(list(fields)):
            if (
                isinstance(field_ref, IndirectObject)
                and widget_ref is not None
                and field_ref.idnum == widget_ref.idnum
            ) or field_ref.get_object() is widget:
                del fields[index]
                break


def _field_has_widgets(field: DictionaryObject) -> bool:
    """Whether the field subtree still contains any widget dictionary."""
    if field.get(AnnotationDictionaryAttributes.Subtype) == _WIDGET_SUBTYPE:
        return True
    kids = field.get(FieldDictionaryAttributes.Kids)
    if isinstance(kids, ArrayObject):
        return any(
            isinstance(kid.get_object(), DictionaryObject)
            and _field_has_widgets(kid.get_object())
            for kid in kids
        )
    return False


def _prune_empty_fields(writer: Any, field: DictionaryObject) -> None:
    """
    Detach field nodes that no longer lead to any widget.

    The values stay reachable through sibling fields that still own boxes; a
    field whose boxes were all frozen disappears from the data-entry tree until
    a revert rebuilds it from the archive snapshot.
    """
    current: DictionaryObject | None = field
    while isinstance(current, DictionaryObject):
        if _field_has_widgets(current):
            return
        current_ref = current.indirect_reference
        parent = _field_parent(current)
        if parent is not None:
            kids = parent.get(FieldDictionaryAttributes.Kids)
            if isinstance(kids, ArrayObject):
                for index, kid in enumerate(list(kids)):
                    if (
                        isinstance(kid, IndirectObject)
                        and current_ref is not None
                        and kid.idnum == current_ref.idnum
                    ) or kid.get_object() is current:
                        del kids[index]
                        break
            current = parent
            continue
        fields = _acroform(writer).get(InteractiveFormDictEntries.Fields)
        if isinstance(fields, ArrayObject):
            for index, field_ref in enumerate(list(fields)):
                if (
                    isinstance(field_ref, IndirectObject)
                    and current_ref is not None
                    and field_ref.idnum == current_ref.idnum
                ) or field_ref.get_object() is current:
                    del fields[index]
                    break
        return


def _archive_widget_entry(
    writer: Any,
    page: Any,
    widget: DictionaryObject,
    field_value: Any,
    xobject_name: NameObject,
    sequence: int,
) -> DictionaryObject:
    parent = _field_parent(widget) or widget
    entry = DictionaryObject()
    entry[NameObject("/FieldName")] = TextStringObject(_qualified_name(writer, widget))
    entry[NameObject("/KidIndex")] = NumberObject(_kid_index(widget))
    entry[NameObject("/Page")] = page.indirect_reference
    entry[NameObject("/Rect")] = RectangleObject(widget[AnnotationDictionaryAttributes.Rect])
    entry[NameObject("/Snapshot")] = _snapshot_widget(writer, widget)
    entry[NameObject("/ParentChain")] = _snapshot_parent_chain(writer, widget)
    entry[NameObject("/FieldValue")] = _snapshot_object(field_value, writer, {})
    entry[NameObject("/FieldType")] = NameObject(
        str(parent.get(FieldDictionaryAttributes.FT, "/Unknown"))
    )
    on_state = _on_state(widget)
    if on_state is not None:
        entry[NameObject("/OnState")] = on_state
    entry[NameObject("/XObjectName")] = xobject_name
    entry[NameObject("/Sequence")] = NumberObject(sequence)
    flags = parent.get(FieldDictionaryAttributes.Ff, 0)
    entry[NameObject("/FieldFlags")] = NumberObject(int(flags) if flags is not None else 0)
    return entry


def _resolve_pages(writer: Any, pages: Any) -> list[Any]:
    if pages is None:
        return list(writer.pages)
    if not isinstance(pages, (list, tuple)):
        pages = [pages]
    return [writer.pages[page] if isinstance(page, int) else page for page in pages]


def finalize_form_pages(writer: Any, pages: Any, issue_id: str) -> dict[str, Any]:
    """
    Freeze the widgets of the given pages into read-only finalization ink.

    Args:
        writer: The :class:`~pypdf.PdfWriter` holding the live form.
        pages: A page, a list of pages or page indices, or ``None`` for all
            pages.
        issue_id: A caller-chosen identifier of this outgoing finalization.
            Later reverts address the finalization through this same id.

    Returns:
        A summary dictionary with the ``issue_id`` and the number of frozen
        widgets.

    Raises:
        FormFinalizationError: If the id is already used, the document has no
            form, or a widget cannot be frozen.
    """
    if not isinstance(issue_id, str) or not issue_id:
        raise FormFinalizationError("issue_id must be a non-empty string.")
    _acroform(writer)  # Reject documents without an interactive form up front.
    archive = _ensure_archive(writer)
    issues = cast(ArrayObject, archive[ISSUES_KEY])
    for existing in issues:
        if str(existing.get_object().get(NameObject("/IssueID"), "")) == issue_id:
            raise FormFinalizationError(f"Finalization id {issue_id!r} is already in use.")

    target_pages = _resolve_pages(writer, pages)
    issue_entry = DictionaryObject()
    issue_entry[NameObject("/IssueID")] = TextStringObject(issue_id)
    issue_entry[NameObject("/Pages")] = ArrayObject(page.indirect_reference for page in target_pages)
    widget_entries = ArrayObject()
    issue_entry[NameObject("/Widgets")] = widget_entries

    for page in target_pages:
        for widget in list(_page_widgets(page)):
            parent = _field_parent(widget) or widget
            field_value = parent.get(FieldDictionaryAttributes.V, NullObject())
            if parent.get(FieldDictionaryAttributes.FT) in (NameObject("/Tx"), NameObject("/Ch")):
                _ensure_text_appearance(writer, page, widget)
            sequence = int(archive[SEQUENCE_KEY])
            archive[SEQUENCE_KEY] = NumberObject(sequence + 1)
            xobject_name = _stamp_on_page(writer, page, widget, issue_id, sequence)
            widget_entries.append(
                writer._add_object(
                    _archive_widget_entry(
                        writer, page, widget, field_value, xobject_name, sequence
                    )
                )
            )
            _detach_widget(writer, page, widget)

    issues.append(writer._add_object(issue_entry))
    return {"issue_id": issue_id, "frozen_widgets": len(widget_entries)}


# ---------------------------------------------------------------------------
# Revert
# ---------------------------------------------------------------------------


def _find_issue(writer: Any, issue_id: str) -> tuple[DictionaryObject, int] | None:
    archive = _get_archive(writer)
    if archive is None:
        return None
    issues = archive.get(ISSUES_KEY)
    if not isinstance(issues, ArrayObject):
        return None
    for index, issue in enumerate(issues):
        if str(issue.get_object().get(NameObject("/IssueID"), "")) == issue_id:
            return issue.get_object(), index
    return None


def _rewrite_page_contents(page: Any, data: bytes) -> None:
    """Replace the whole page contents with one stream holding *data*."""
    replacement = DecodedStreamObject()
    replacement.set_data(data)
    # PageObject.replace_contents() attaches the stream through the writer's
    # object table and detaches the previous content stream(s).
    page.replace_contents(replacement)


def _remove_stamp(writer: Any, page: Any, issue_id: str, entry: DictionaryObject) -> None:
    """Remove exactly the marked content block and XObject of one frozen box."""
    sequence = int(entry[NameObject("/Sequence")])
    token = _marker_token(issue_id)
    begin = f"{BEGIN_MARKER} issue={token} widget={sequence}".encode("utf-8")
    end = f"{END_MARKER} issue={token} widget={sequence}".encode("utf-8")

    contents = page.get(PageAttributes.CONTENTS)
    data = ContentStream(contents, writer).get_data() if contents is not None else b""
    begin_index = data.find(begin)
    if begin_index != -1:
        end_index = data.find(end, begin_index)
        if end_index != -1:
            end_index += len(end)
            while end_index < len(data) and data[end_index : end_index + 1] in (b"\n", b"\r"):
                end_index += 1
            data = data[:begin_index] + data[end_index:]
    _rewrite_page_contents(page, data)

    resources = page.get(PageAttributes.RESOURCES)
    if isinstance(resources, DictionaryObject):
        xobjects = resources.get(NameObject("/XObject"))
        xobject_name = entry.get(NameObject("/XObjectName"))
        if isinstance(xobjects, DictionaryObject) and xobject_name is not None:
            xobjects.pop(cast(str, xobject_name), None)


def _field_chain_names(field_name: str) -> list[str]:
    return [part for part in field_name.split(".") if part]


def _live_field_for_path(writer: Any, field_name: str) -> DictionaryObject | None:
    fields = _acroform(writer).get(InteractiveFormDictEntries.Fields)
    if not isinstance(fields, ArrayObject):
        return None
    current_children: ArrayObject = fields
    current_field: DictionaryObject | None = None
    for part in _field_chain_names(field_name):
        match = None
        for kid in current_children:
            kid_obj = kid.get_object()
            if isinstance(kid_obj, DictionaryObject) and str(kid_obj.get(NameObject("/T"), "")) == part:
                match = kid_obj
                break
        if match is None:
            return None
        current_field = match
        kids = match.get(FieldDictionaryAttributes.Kids)
        current_children = kids if isinstance(kids, ArrayObject) else ArrayObject()
    return current_field


def _archived_parent_chain(entry: DictionaryObject) -> list[DictionaryObject]:
    """Archived parent field nodes of a frozen widget, root field first."""
    chain = entry.get(NameObject("/ParentChain"))
    if not isinstance(chain, ArrayObject):
        return []
    return [node.get_object() for node in chain if isinstance(node, IndirectObject)]


def _rebuild_field_node(
    writer: Any,
    template: DictionaryObject | None,
    name: str,
    parent: DictionaryObject | None,
) -> DictionaryObject:
    node = DictionaryObject()
    if template is not None:
        for key, value in template.items():
            if key in (
                FieldDictionaryAttributes.Kids,
                FieldDictionaryAttributes.Parent,
                FieldDictionaryAttributes.V,
            ):
                continue
            node[NameObject(str(key))] = _snapshot_object(value, writer, {})
    node[NameObject(FieldDictionaryAttributes.T)] = TextStringObject(name)
    node[NameObject(FieldDictionaryAttributes.Kids)] = ArrayObject()
    writer._add_object(node)
    if parent is not None:
        node[NameObject(FieldDictionaryAttributes.Parent)] = parent.indirect_reference
    return node


def _rebuild_field_path(
    writer: Any, field_name: str, entry: DictionaryObject
) -> DictionaryObject:
    """
    Reconnect (creating missing nodes) the parent field chain of a frozen box.

    Existing live nodes are reused; missing nodes are recreated from the
    archived parent chain so a field whose boxes were all frozen comes back
    intact. Returns the immediate parent field of the restored widget.
    """
    acroform = _acroform(writer)
    fields = acroform.setdefault(NameObject(InteractiveFormDictEntries.Fields), ArrayObject())
    parts = _field_chain_names(field_name)
    archived_chain = _archived_parent_chain(entry)

    current_children = fields
    live_parent: DictionaryObject | None = None
    for depth, part in enumerate(parts):
        match = None
        for kid in current_children:
            kid_obj = kid.get_object()
            if isinstance(kid_obj, DictionaryObject) and str(kid_obj.get(NameObject("/T"), "")) == part:
                match = kid_obj
                break
        if match is None:
            template = archived_chain[depth] if depth < len(archived_chain) else None
            match = _rebuild_field_node(writer, template, part, live_parent)
            if live_parent is None:
                fields.append(match.indirect_reference)
            else:
                current_children.append(match.indirect_reference)
        live_parent = match
        kids = match.get(FieldDictionaryAttributes.Kids)
        if not isinstance(kids, ArrayObject):
            kids = ArrayObject()
            match[NameObject(FieldDictionaryAttributes.Kids)] = kids
        current_children = kids
    assert live_parent is not None
    return live_parent


def _attach_kid(writer: Any, field: DictionaryObject, widget: DictionaryObject) -> None:
    kids = field.get(FieldDictionaryAttributes.Kids)
    if not isinstance(kids, ArrayObject):
        kids = ArrayObject()
        field[NameObject(FieldDictionaryAttributes.Kids)] = kids
    widget[NameObject(FieldDictionaryAttributes.Parent)] = field.indirect_reference
    if widget.indirect_reference not in kids:
        kids.append(widget.indirect_reference)


def _restore_text_appearance(
    writer: Any, page: Any, widget: DictionaryObject, field: DictionaryObject
) -> None:
    stream = TextStreamAppearance.from_text_annotation(
        writer,
        page,
        flatten=False,
        acro_form=_acroform(writer),
        field=field,
        annotation=widget,
    )
    if AnnotationDictionaryAttributes.AP not in widget:
        widget[NameObject(AnnotationDictionaryAttributes.AP)] = DictionaryObject()
    cast(DictionaryObject, widget[AnnotationDictionaryAttributes.AP])[
        NameObject("/N")
    ] = writer._add_object(stream)


def _frozen_field_value(entry: DictionaryObject) -> Any:
    """Value captured on the immediate parent field at finalization time."""
    chain = _archived_parent_chain(entry)
    if chain:
        parent = chain[-1]
        if FieldDictionaryAttributes.V in parent:
            return parent[FieldDictionaryAttributes.V]
    snapshot = entry[NameObject("/Snapshot")].get_object()
    return snapshot.get(FieldDictionaryAttributes.V, NullObject())


def _newest_archived_field_value(writer: Any, field_name: str) -> Any | None:
    """
    Newest archived value of *field_name* across every recorded finalization.

    Finalizations are stored chronologically and every box of a field shares
    its field value, so the last matching entry captured the latest answer that
    was reachable before the field lost all of its editable entries.
    """
    archive = _get_archive(writer)
    if archive is None:
        return None
    issues = archive.get(ISSUES_KEY)
    if not isinstance(issues, ArrayObject):
        return None
    newest: Any | None = None
    for issue in issues:
        for widget_ref in issue.get_object().get(NameObject("/Widgets"), ArrayObject()):
            entry = widget_ref.get_object()
            if str(entry.get(NameObject("/FieldName"), "")) == field_name:
                value = entry.get(NameObject("/FieldValue"))
                if not _is_null(value):
                    newest = value
    return newest


def _sibling_is_selected(field: DictionaryObject, widget_ref: IndirectObject) -> bool:
    """Whether another kid of a button field currently shows an on state."""
    kids = field.get(FieldDictionaryAttributes.Kids)
    if not isinstance(kids, ArrayObject):
        return False
    for kid in kids:
        if not isinstance(kid, IndirectObject) or kid.idnum == widget_ref.idnum:
            continue
        kid_obj = kid.get_object()
        if isinstance(kid_obj, DictionaryObject) and kid_obj.get(
            AnnotationDictionaryAttributes.AS, _OFF_STATE
        ) != _OFF_STATE:
            return True
    return False


def _button_state_for_restore(
    writer: Any,
    field: DictionaryObject,
    widget: DictionaryObject,
    widget_ref: IndirectObject,
    entry: DictionaryObject,
) -> NameObject:
    """
    Compute the /AS of a restored button box.

    Normal radio groups allow only one selected box at a time. A restored box
    stays off when a surviving sibling already carries the selection. With no
    live sibling selected, the field value may still name a box that remains
    frozen on another page ("the recorded selection stays until the filler
    reselects"), so the box's own frozen-time /AS is used as the baseline.
    RadiosInUnison groups and checkboxes follow the field value directly.
    """
    on_state = entry.get(NameObject("/OnState"))
    if on_state is None:
        return _OFF_STATE
    current_value = field.get(FieldDictionaryAttributes.V, NullObject())
    if current_value != on_state:
        return _OFF_STATE
    flags = int(entry.get(NameObject("/FieldFlags"), 0))
    is_radio = bool(flags & FieldDictionaryAttributes.FfBits.Radio)
    in_unison = bool(flags & FieldDictionaryAttributes.FfBits.RadiosInUnison)
    if is_radio and not in_unison:
        if _sibling_is_selected(field, widget_ref):
            return _OFF_STATE
        archived_state = widget.get(AnnotationDictionaryAttributes.AS)
        if isinstance(archived_state, NameObject):
            return archived_state
    return NameObject(str(on_state))


def _normalize_unison_states(field: DictionaryObject) -> None:
    """
    Apply RadiosInUnison semantics: every kid whose on state equals the field
    value shows that state together; all other kids are off.
    """
    kids = field.get(FieldDictionaryAttributes.Kids)
    if not isinstance(kids, ArrayObject):
        return
    current_value = field.get(FieldDictionaryAttributes.V, _OFF_STATE)
    for kid in kids:
        kid_obj = kid.get_object()
        if not isinstance(kid_obj, DictionaryObject):
            continue
        appearance = kid_obj.get(AnnotationDictionaryAttributes.AP)
        on_state = None
        if isinstance(appearance, DictionaryObject):
            normal = appearance.get(NameObject("/N"))
            if isinstance(normal, IndirectObject):
                normal = normal.get_object()
            if isinstance(normal, DictionaryObject):
                on_state = next(
                    (NameObject(str(state)) for state in normal if state != _OFF_STATE),
                    None,
                )
        kid_obj[NameObject(AnnotationDictionaryAttributes.AS)] = (
            NameObject(str(on_state))
            if on_state is not None and on_state == current_value
            else _OFF_STATE
        )


def _restore_widget(writer: Any, page: Any, entry: DictionaryObject) -> None:
    snapshot = cast(DictionaryObject, entry[NameObject("/Snapshot")].get_object())
    field_name = str(entry[NameObject("/FieldName")])
    field_type = str(entry[NameObject("/FieldType")])
    kid_index = int(entry[NameObject("/KidIndex")])
    has_parent = bool(_archived_parent_chain(entry))

    widget = cast(DictionaryObject, _snapshot_object(snapshot, writer, {}))
    # The widget is reattached to the live (or rebuilt) field below.
    widget.pop(NameObject(FieldDictionaryAttributes.Parent), None)
    widget_ref = writer._add_object(widget)
    widget[NameObject(AnnotationDictionaryAttributes.P)] = page.indirect_reference

    if not has_parent:
        # Root widget field: reattach to AcroForm /Fields and refresh its state.
        fields = _acroform(writer).setdefault(NameObject(InteractiveFormDictEntries.Fields), ArrayObject())
        if widget_ref not in fields:
            fields.append(widget_ref)
        if field_type == "/Btn":
            on_state = entry.get(NameObject("/OnState"))
            current_value = widget.get(FieldDictionaryAttributes.V, NullObject())
            state = (
                NameObject(str(on_state))
                if on_state is not None and current_value == on_state
                else _OFF_STATE
            )
            widget[NameObject(AnnotationDictionaryAttributes.AS)] = state
            widget[NameObject(FieldDictionaryAttributes.V)] = state
        elif field_type in ("/Tx", "/Ch"):
            _restore_text_appearance(writer, page, widget, widget)
    elif field_type == "/Btn":
        live = _live_field_for_path(writer, field_name)
        if live is None:
            # No editable entry survived: reuse the newest archived answer, or
            # the value captured by this finalization.
            live = _rebuild_field_path(writer, field_name, entry)
            archived = _newest_archived_field_value(writer, field_name)
            value = archived if archived is not None else _frozen_field_value(entry)
            live[NameObject(FieldDictionaryAttributes.V)] = _snapshot_object(value, writer, {})
        _attach_kid(writer, live, widget)
        widget.pop(NameObject(FieldDictionaryAttributes.V), None)
        widget[NameObject(AnnotationDictionaryAttributes.AS)] = _button_state_for_restore(
            writer, live, widget, widget_ref, entry
        )
        flags = int(entry.get(NameObject("/FieldFlags"), 0))
        if flags & FieldDictionaryAttributes.FfBits.RadiosInUnison:
            _normalize_unison_states(live)
    else:
        live = _live_field_for_path(writer, field_name)
        if live is None:
            live = _rebuild_field_path(writer, field_name, entry)
            archived = _newest_archived_field_value(writer, field_name)
            value = archived if archived is not None else _frozen_field_value(entry)
            live[NameObject(FieldDictionaryAttributes.V)] = _snapshot_object(value, writer, {})
        _attach_kid(writer, live, widget)
        # The value belongs to the field, never to the kid widget.
        widget.pop(NameObject(FieldDictionaryAttributes.V), None)
        _restore_text_appearance(writer, page, widget, live)

    annots = page.get(PageAttributes.ANNOTS)
    if not isinstance(annots, ArrayObject):
        annots = ArrayObject()
        page[NameObject(PageAttributes.ANNOTS)] = annots
    insert_at = min(kid_index, len(annots)) if kid_index >= 0 else len(annots)
    annots.insert(insert_at, widget_ref)


def revert_form_finalization(writer: Any, issue_id: str) -> dict[str, Any]:
    """
    Undo one finalization previously created with :func:`finalize_form_pages`.

    Only the read-only ink and the structural changes of the addressed
    finalization id are reverted. Annotations added afterwards, artwork
    originally drawn on the pages and the stamps of other finalizations remain
    untouched, even when they reference the same graphical appearance.

    Reverted boxes reconnect to the answers currently held by the field tree.
    When a field had no remaining editable entry after the finalization, its
    value could not have changed and the frozen-time answer is reused.
    """
    found = _find_issue(writer, issue_id)
    if found is None:
        raise FormFinalizationError(f"Unknown finalization id {issue_id!r}.")
    issue_entry, issue_index = found
    archive = _get_archive(writer)
    assert archive is not None
    issues = cast(ArrayObject, archive[ISSUES_KEY])

    widget_entries = cast(ArrayObject, issue_entry[NameObject("/Widgets")])
    restored = 0
    for widget_ref in list(widget_entries):
        entry = widget_ref.get_object()
        page = entry[NameObject("/Page")].get_object()
        _remove_stamp(writer, page, issue_id, entry)
        _restore_widget(writer, page, entry)
        restored += 1

    del issues[issue_index]
    return {"issue_id": issue_id, "restored_widgets": restored}
