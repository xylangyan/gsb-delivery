"""
Tests for the common intake of public worksheets and binary supplements.

These tests use only the pre-generated datasets (no ``Rscript`` is required)
and exercise the intake rules around row names, factor codes, the NA/NaN
distinction, registered corrections, segment moves and open tickets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

import rdata
from rdata.intake import (
    AttachmentError,
    CellRef,
    Correction,
    DecisionStatus,
    FieldChange,
    IntakeError,
    MissingClass,
    Origin,
    OriginCell,
    RowNameRegistry,
    SegmentMove,
    StripSnapshot,
    accept,
    classify_missing,
    compare_releases,
    element_fingerprint,
    list_repeated_segments,
    origin_from_bytes,
    reconcile_drafts,
    request_minimal_supplement,
    transcode_to_ascii,
)
from rdata.missing import R_FLOAT_NA
from rdata.parser import (
    RData,
    RExtraInfo,
    RObject,
    RObjectInfo,
    RObjectType,
    RVersions,
)
from rdata.unparser import unparse_data

if TYPE_CHECKING:
    from rdata.intake import Origin

GENERATED = rdata.TESTDATA_PATH / "generated"


def _load(
    base_name: str,
    serialization: str,
    suffix: str,
    *,
    version: int = 3,
) -> tuple[bytes, "Origin"]:
    path = (
        GENERATED
        / f"{base_name}__{serialization}__version_{version}.{suffix}"
    )
    data = path.read_bytes()
    return data, origin_from_bytes(data, expected_kind=suffix)


@pytest.fixture
def float_worksheet() -> Origin:
    """ASCII RDA worksheet containing NA, NaN and infinities."""
    _, origin = _load(
        "test_dataframe_float_with_na_nan", "ascii", "rda",
    )
    return origin


@pytest.fixture
def float_supplement() -> tuple[bytes, Origin]:
    """Binary RDS supplement of the same floating-point data."""
    return _load(
        "test_dataframe_float_with_na_nan", "binary", "rds",
    )


def test_na_and_computed_nan_are_classified_differently(
    float_supplement: tuple[bytes, Origin],
) -> None:
    """Reported NA and computed NaN look alike but differ in place."""
    _, supplement = float_supplement
    assert (
        supplement.find(CellRef(4, "float")).missing_class
        is MissingClass.R_NA
    )
    assert (
        supplement.find(CellRef(5, "float")).missing_class
        is MissingClass.COMPUTED_NAN
    )
    assert (
        supplement.find(CellRef(4, "float")).raw_bytes
        != supplement.find(CellRef(5, "float")).raw_bytes
    )
    assert (
        supplement.find(CellRef(4, "float")).origin_id
        != supplement.find(CellRef(5, "float")).origin_id
    )


def test_classify_missing_on_raw_arrays() -> None:
    """The classifier works directly on parsed arrays."""
    array = np.array([1.0, R_FLOAT_NA, np.nan], dtype=np.float64)
    assert classify_missing(array, 0) is MissingClass.NOT_MISSING
    assert classify_missing(array, 1) is MissingClass.R_NA
    assert classify_missing(array, 2) is MissingClass.COMPUTED_NAN


def test_binary_to_ascii_keeps_the_same_strips(
    float_supplement: tuple[bytes, Origin],
) -> None:
    """Short supplements are read as binary and written back as ASCII."""
    data, supplement = float_supplement
    ascii_data = transcode_to_ascii(data)
    assert ascii_data[:2] == b"A\n"

    reloaded = origin_from_bytes(ascii_data, expected_kind="rds")
    for ref, cell in supplement.cells.items():
        assert reloaded.cells[ref].origin_id == cell.origin_id
        assert reloaded.cells[ref].missing_class is cell.missing_class


def test_factor_codes_survive_identical_labels() -> None:
    """Different original factor codes can carry the same public label."""
    _, factor = _load("test_factor", "binary", "rds")
    labels_and_codes = [
        (cell.public_label, cell.factor_code)
        for cell in factor.cells.values()
    ]
    assert labels_and_codes == [("a", 1), ("b", 2), ("b", 2)]
    second = factor.find(CellRef(2, "value"))
    third = factor.find(CellRef(3, "value"))
    assert second.public_label == third.public_label
    assert second.origin_id != third.origin_id


def test_automatic_and_manual_row_names_both_display_as_one() -> None:
    """Automatic row names and a manual identifier 1 stay distinguishable."""
    _, automatic = _load("test_dataframe", "binary", "rds")
    _, manual = _load(
        "test_dataframe_int_rownames", "binary", "rds",
    )
    # test_dataframe uses R's compact automatic row names: label 1 is row 0.
    assert automatic.row_registry.resolve(1) == (0,)
    assert automatic.row_registry.is_automatic(0)
    # Manual names are 3,6,9: the displayed 3 is the first row.
    assert manual.row_registry.resolve(3) == (0,)
    assert not manual.row_registry.is_automatic(0)
    # Both registries can display a row as 1, but only the manual one carries
    # an explicit identifier at that position.
    assert automatic.row_registry.display_label(0) == 1
    assert manual.row_registry.display_label(0) == 3


def test_documents_attached_to_the_wrong_slot_are_rejected(
    float_supplement: tuple[bytes, Origin],
) -> None:
    """A binary RDS cannot be admitted through the public RDA slot."""
    data, _ = float_supplement
    with pytest.raises(AttachmentError, match="rda"):
        origin_from_bytes(data, expected_kind="rda")


def test_misattached_slip_is_flagged_before_landing(
    float_worksheet: Origin,
) -> None:
    """A correction written against another cell is rejected."""
    foreign_snapshot = float_worksheet.find(
        CellRef(1, "float"),
    ).snapshot()
    correction = Correction(
        round=1,
        ref=CellRef(4, "float"),
        expected_snapshot=foreign_snapshot,
    )
    result = accept(float_worksheet, corrections=[correction])
    assert result.status is DecisionStatus.REJECTED
    assert result.issues[0].code == "MISATTACHED_SLIP"


def test_grouped_fields_must_arrive_together(
    float_worksheet: Origin,
) -> None:
    """A factor code without its label is not an effective correction."""
    correction = Correction(
        round=1,
        ref=CellRef(1, "float"),
        changes=(FieldChange("factor_code", 2),),
    )
    result = accept(float_worksheet, corrections=[correction])
    assert result.issues[0].code == "UNGROUPED_FIELDS"


def test_a_value_correction_lands_and_stays_readable(
    float_worksheet: Origin,
) -> None:
    """Accepted corrections are persisted and remain readable by rdata."""
    correction = Correction(
        round=1,
        ref=CellRef(1, "float"),
        changes=(
            FieldChange("value", 9.9),
            FieldChange("missing_class", "not_missing"),
        ),
    )
    result = accept(float_worksheet, corrections=[correction])
    assert result.accepted
    assert result.corrected is not None
    assert (
        result.corrected.find(CellRef(1, "float")).public_value == 9.9
    )

    ascii_rda = unparse_data(
        result.corrected.rdata,
        file_format="ascii",
        file_type="rda",
    )
    reloaded = origin_from_bytes(ascii_rda, expected_kind="rda")
    assert reloaded.find(CellRef(1, "float")).public_value == 9.9


def test_open_ticket_drift_is_detected(float_worksheet: Origin) -> None:
    """An open ticket must still verify against its original reading."""
    ref = CellRef(4, "float")
    snapshot = float_worksheet.find(ref).snapshot()
    assert accept(
        float_worksheet,
        open_tickets={"T-1": (ref, snapshot)},
    ).accepted

    correction = Correction(
        round=1,
        ref=ref,
        changes=(
            FieldChange("value", 1.0),
            FieldChange("missing_class", "not_missing"),
        ),
    )
    result = accept(
        float_worksheet,
        corrections=[correction],
        open_tickets={"T-1": (ref, snapshot)},
    )
    assert any(issue.code == "OPEN_TICKET_DRIFT" for issue in result.issues)


def test_minimal_supplement_uses_registered_fees() -> None:
    """The cheapest fragment set closes every indeterminate decision."""
    issue = rdata.intake.missing_fragment_issue(CellRef("frag-7", "float"))
    requests = request_minimal_supplement(
        [issue],
        {"frag-7": 4.25},
    )
    assert requests[0].fragment_id == "frag-7"
    assert requests[0].fee == 4.25


def test_repeated_segments_use_occupied_values_interval_and_step() -> None:
    """Compact sequences are described without expanding a big table."""
    parsed = rdata.parser.parse_file(
        GENERATED
        / "test_altrep_compact_intseq_asymmetric__binary__version_3.rds",
        expand_altrep=False,
    )
    segments = list_repeated_segments(parsed)
    assert len(segments) == 1
    segment = segments[0]
    assert segment.start == -5
    assert segment.stop == 5
    assert segment.step == 1
    assert segment.occupied_values == (-5.0, -4.0, -3.0)


def test_segment_move_relocates_a_whole_row_segment(
    float_worksheet: Origin,
) -> None:
    """Moving a segment splices the rows to the new landing position."""
    correction = Correction(
        round=1,
        ref=CellRef(1, "float"),
        move=SegmentMove(CellRef(6, "float"), length=2),
    )
    result = accept(float_worksheet, corrections=[correction])
    assert result.accepted
    assert result.corrected is not None
    assert (
        result.corrected.find(CellRef(6, "float")).public_value == 1.1
    )
    assert (
        result.corrected.find(CellRef(7, "float")).public_value == 2.2
    )
    # The displaced row keeps the same strip identity.
    assert (
        result.corrected.find(CellRef(1, "float")).origin_id
        == float_worksheet.find(CellRef(3, "float")).origin_id
    )


def test_judgment_slip_after_move_reads_the_current_occupant(
    float_worksheet: Origin,
) -> None:
    """A later slip at an old position matches the strip now sitting there."""
    move = Correction(
        round=1,
        ref=CellRef(1, "float"),
        move=SegmentMove(CellRef(6, "float"), length=2),
    )
    moved = accept(float_worksheet, corrections=[move])
    current = moved.corrected.find(CellRef(1, "float"))

    good_slip = Correction(
        round=2,
        ref=CellRef(1, "float"),
        expected_snapshot=current.snapshot(),
        changes=(
            FieldChange("value", 33.3),
            FieldChange("missing_class", "not_missing"),
        ),
    )
    assert accept(
        float_worksheet, corrections=[move, good_slip],
    ).accepted

    stale_slip = Correction(
        round=2,
        ref=CellRef(1, "float"),
        expected_snapshot=float_worksheet.find(
            CellRef(1, "float"),
        ).snapshot(),
        changes=(
            FieldChange("value", 33.3),
            FieldChange("missing_class", "not_missing"),
        ),
    )
    rejected = accept(
        float_worksheet, corrections=[move, stale_slip],
    )
    assert any(
        issue.code == "MISATTACHED_SLIP" for issue in rejected.issues
    )


def test_corrections_execute_in_approval_round_order(
    float_worksheet: Origin,
) -> None:
    """Rounds are applied by their registered order, not list order."""
    first = Correction(
        round=1,
        ref=CellRef(1, "float"),
        changes=(
            FieldChange("value", 9.9),
            FieldChange("missing_class", "not_missing"),
        ),
    )
    second_snapshot = float_worksheet.find(
        CellRef(2, "float"),
    ).snapshot()
    second = Correction(
        round=2,
        ref=CellRef(2, "float"),
        expected_snapshot=second_snapshot,
        changes=(
            FieldChange("value", 7.7),
            FieldChange("missing_class", "not_missing"),
        ),
    )
    result = accept(float_worksheet, corrections=[second, first])
    assert result.accepted
    assert result.corrected is not None
    assert (
        result.corrected.find(CellRef(2, "float")).public_value == 7.7
    )


def test_mixed_provenance_is_rejected(float_worksheet: Origin) -> None:
    """One value cannot combine a factor code and missing bit from strips."""
    correction = Correction(
        round=1,
        ref=CellRef(1, "float"),
        changes=(
            FieldChange("factor_code", 2, source_origin_id="strip-A"),
            FieldChange("label", "b", source_origin_id="strip-B"),
        ),
    )
    result = accept(float_worksheet, corrections=[correction])
    assert any(
        issue.code == "MIXED_PROVENANCE" for issue in result.issues
    )


def test_na_to_nan_keeps_display_but_changes_underlying_bytes(
    float_worksheet: Origin,
) -> None:
    """Some reports draw NA and NaN the same while the in-place bytes differ."""
    correction = Correction(
        round=1,
        ref=CellRef(4, "float"),
        changes=(
            FieldChange("value", float("nan")),
            FieldChange("missing_class", "computed_nan"),
        ),
    )
    result = accept(float_worksheet, corrections=[correction])
    assert result.accepted
    assert result.corrected is not None
    comparison = {
        item.ref: item
        for item in compare_releases(
            float_worksheet, result.corrected,
        )
    }[CellRef(4, "float")]
    assert not comparison.display_changed
    assert comparison.underlying_changed
    assert (
        result.corrected.find(CellRef(4, "float")).missing_class
        is MissingClass.COMPUTED_NAN
    )


def test_draft_reconciliation_requires_identical_corrections(
    float_worksheet: Origin,
) -> None:
    """Drafts only release together with the same corrections and delivery."""
    correction = Correction(
        round=1,
        ref=CellRef(1, "float"),
        changes=(
            FieldChange("value", 9.9),
            FieldChange("missing_class", "not_missing"),
        ),
    )
    matching = reconcile_drafts(
        [
            (float_worksheet, [correction], None),
            (float_worksheet, [correction], None),
        ],
    )
    assert matching.accepted

    different = Correction(
        round=1,
        ref=CellRef(2, "float"),
        changes=(
            FieldChange("value", 5.5),
            FieldChange("missing_class", "not_missing"),
        ),
    )
    mismatched = reconcile_drafts(
        [
            (float_worksheet, [correction], None),
            (float_worksheet, [different], None),
        ],
    )
    assert not mismatched.accepted
    assert mismatched.issues[0].code == "DRAFT_MISMATCH"
    assert len(mismatched.readable_results) == 2


def test_common_entry_accepts_bytes_for_both_slots(
    float_worksheet: Origin,
    float_supplement: tuple[bytes, Origin],
) -> None:
    """The common entry accepts bytes and keeps both documents readable."""
    supplement_bytes, _ = float_supplement
    worksheet_bytes = float_worksheet.source_bytes
    result = accept(worksheet_bytes, supplement_bytes)
    assert result.worksheet.file_type == "rda"
    assert result.supplement is not None
    assert result.supplement.file_type == "rds"
    assert result.accepted


def test_element_fingerprint_distinguishes_equal_values() -> None:
    """Two physical entries with equal values keep distinct serials."""
    array = np.array([2.2, 2.2], dtype=np.float64)
    first = element_fingerprint(0, array, 0)
    second = element_fingerprint(0, array, 1)
    assert first != second


def test_unknown_field_is_rejected() -> None:
    """Only registered fields can appear on a correction slip."""
    with pytest.raises(IntakeError):
        FieldChange("unregistered_field", 1)


def _shared_strip_origin() -> Origin:
    """Build two worksheet cells that genuinely use the same strip."""
    node = RObject(
        info=RObjectInfo(
            RObjectType.REAL, False, False, False, 0, 0,
        ),
        value=np.array([1.1, 2.2], dtype=np.float64),
        attributes=None,
        tag=None,
        referenced_object=None,
    )
    r_data = RData(
        RVersions(3, 0x30601, 0x30500),
        RExtraInfo("UTF-8"),
        node,
    )
    first = OriginCell(
        ref=CellRef(1, "x"), public_value=1.1, raw=node,
        index=0, node_ordinal=0,
    )
    second = OriginCell(
        ref=CellRef(2, "x"), public_value=1.1, raw=node,
        index=0, node_ordinal=0,
    )
    registry = RowNameRegistry(n_rows=2, manual={})
    return Origin(
        name="shared-strip",
        cells={first.ref: first, second.ref: second},
        row_registry=registry,
        columns=("x",),
        rdata=r_data,
        source_bytes=b"",
        file_type="rda",
        version=3,
    )


def test_two_slips_on_one_origin_must_share_one_interpretation() -> None:
    """Two cells using one strip cannot receive different rework meanings."""
    origin = _shared_strip_origin()
    first = Correction(
        round=1,
        ref=CellRef(1, "x"),
        changes=(
            FieldChange("value", 9.9),
            FieldChange("missing_class", "not_missing"),
        ),
    )
    differing = Correction(
        round=1,
        ref=CellRef(2, "x"),
        changes=(
            FieldChange("value", 8.8),
            FieldChange("missing_class", "not_missing"),
        ),
    )
    rejected = accept(origin, corrections=[first, differing])
    assert any(
        issue.code == "INCONSISTENT_INTERPRETATION"
        for issue in rejected.issues
    )

    matching = Correction(
        round=1,
        ref=CellRef(2, "x"),
        changes=(
            FieldChange("value", 9.9),
            FieldChange("missing_class", "not_missing"),
        ),
    )
    assert accept(origin, corrections=[first, matching]).accepted
