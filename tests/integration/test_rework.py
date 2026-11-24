from dataclasses import dataclass, field
from decimal import Decimal
from typing import Generic, List, Literal, Optional, TypeVar

import pytest

from apischema import ValidationError, alias, deserialize
from apischema.metadata import conversion, flatten
from apischema.types import Undefined

from apischema_rework import (
    ImportEntry,
    Missing,
    Null,
    Remove,
    SourceChange,
    TargetExpectation,
    build_influence,
    build_rework,
)


# --- legacy header: one flattened JSON key feeds two inner objects


@dataclass
class AmountPart:
    amount: float = field(metadata=alias("amt"))


@dataclass
class TaxBasePart:
    tax_base: float = field(metadata=alias("amt"))


@dataclass
class LegacyEntry:
    amount_part: AmountPart = field(metadata=flatten)
    tax_part: TaxBasePart = field(metadata=flatten)


# --- modern branch: distinct aliases, swappable marker, null vs missing


T = TypeVar("T")


@dataclass
class LocationRecord(Generic[T]):
    code: str = field(metadata=alias("locCode"))
    marker: T


def parse_money(raw: str) -> Decimal:
    return Decimal(raw)


@dataclass
class MoneyLocation:
    code: str = field(metadata=alias("locCode"))
    money: Decimal = field(metadata=conversion(deserialization=parse_money))


@dataclass
class DetailLine:
    line_id: int = field(metadata=alias("lineId"))
    label: str = "auto"


@dataclass
class ModernEntry:
    category: Literal["goods", "service"] = field(metadata=alias("docCategory"))
    generic_loc: LocationRecord[Literal["in", "out"]]
    money_loc: MoneyLocation
    remark: Optional[str] = None
    note: Optional[str] = field(default=Undefined)
    details: List[DetailLine] = field(default_factory=list)


MODERN_ORIGINAL = {
    "docCategory": "goods",
    "generic_loc": {"locCode": "G1", "marker": "in"},
    "money_loc": {"locCode": "M1", "money": "12.50"},
    "remark": None,
}


def test_shared_flat_key_change_drags_both_warehouse_fields():
    result = build_rework(
        ImportEntry(LegacyEntry),
        {"amt": 100.0},
        [TargetExpectation(("amt",), 200.0)],
        [SourceChange(("amt",), (200.0, Remove))],
    )
    assert result.feasible
    patch = result.patches[0]
    assert patch.size == 1
    assert patch.changed_paths == (("amt",),)
    reimported = deserialize(LegacyEntry, patch.document)
    assert reimported.amount_part.amount == 200.0
    assert reimported.tax_part.tax_base == 200.0


def test_divergent_requirements_on_shared_key_become_conflict():
    result = build_rework(
        ImportEntry(LegacyEntry),
        {"amt": 100.0},
        [
            TargetExpectation(("amount_part", "amount"), 200.0),
            TargetExpectation(("tax_part", "tax_base"), 100.0),
        ],
        [SourceChange(("amt",), (200.0, 100.0))],
    )
    assert not result.feasible
    assert len(result.report.conflicts) == 1
    pair = result.report.conflicts[0]
    assert pair.source_paths == (("amt",),)
    assert pair.first.path == ("amount_part", "amount")
    assert pair.second.path == ("tax_part", "tax_base")
    assert pair.options == {"('amt',)": (200.0, 100.0)}
    assert result.report.blocked == ()


def test_minimal_patch_keeps_other_source_items_and_applies_on_reimport():
    result = build_rework(
        ImportEntry(ModernEntry),
        MODERN_ORIGINAL,
        [TargetExpectation(("docCategory",), "service")],
        [
            SourceChange(("docCategory",), ("service", "goods")),
            SourceChange(("generic_loc", "marker"), ("in", "out")),
        ],
    )
    patch = result.patches[0]
    assert patch.size == 1
    assert patch.changed_paths == (("docCategory",),)
    assert patch.document["generic_loc"] == MODERN_ORIGINAL["generic_loc"]
    assert deserialize(ModernEntry, patch.document).category == "service"


def test_explicit_null_and_absent_property_stay_distinct():
    result = build_rework(
        ImportEntry(ModernEntry),
        MODERN_ORIGINAL,
        [
            TargetExpectation(("remark",), Null),
            TargetExpectation(("note",), Missing),
        ],
        [
            SourceChange(("remark",), (None, Remove)),
            SourceChange(("note",), (None, Remove)),
        ],
    )
    assert result.feasible
    # remark is already an explicit null and note already absent: no edit needed
    assert result.patches[0].size == 0
    reimported = deserialize(ModernEntry, result.patches[0].document)
    assert reimported.remark is None
    assert reimported.note is Undefined

    forced_null = build_rework(
        ImportEntry(ModernEntry),
        MODERN_ORIGINAL,
        [TargetExpectation(("note",), Null)],
        [SourceChange(("note",), (None, Remove))],
    )
    assert forced_null.patches[0].document["note"] is None
    assert deserialize(ModernEntry, forced_null.patches[0].document).note is None

    forced_missing = build_rework(
        ImportEntry(ModernEntry),
        {**MODERN_ORIGINAL, "note": "x"},
        [TargetExpectation(("note",), Missing)],
        [SourceChange(("note",), (None, Remove))],
    )
    assert "note" not in forced_missing.patches[0].document
    assert (
        deserialize(ModernEntry, forced_missing.patches[0].document).note is Undefined
    )


def test_source_value_swap_inside_modern_branch():
    result = build_rework(
        ImportEntry(ModernEntry),
        MODERN_ORIGINAL,
        [
            TargetExpectation(("docCategory",), "service"),
            TargetExpectation(("generic_loc", "marker"), "out"),
        ],
        [
            SourceChange(("docCategory",), ("service", "goods")),
            SourceChange(("generic_loc", "marker"), ("out", "in")),
        ],
    )
    patch = result.patches[0]
    assert patch.size == 2
    document = patch.document
    assert document["docCategory"] == "service"
    assert document["generic_loc"]["marker"] == "out"
    reimported = deserialize(ModernEntry, document)
    assert reimported.category == "service"
    assert reimported.generic_loc.marker == "out"


@dataclass
class SwapRecord(Generic[T]):
    first: str = field(metadata=alias("a"))
    second: T


@dataclass
class SwapDoc:
    pair: SwapRecord[str]


def test_two_source_positions_exchange_their_values():
    original = {"pair": {"a": "A", "second": "B"}}
    result = build_rework(
        ImportEntry(SwapDoc),
        original,
        [
            TargetExpectation(("pair", "first"), "B"),
            TargetExpectation(("pair", "second"), "A"),
        ],
        [
            SourceChange(("pair", "a"), ("B",)),
            SourceChange(("pair", "second"), ("A",)),
        ],
    )
    patch = result.patches[0]
    assert patch.size == 2
    assert deserialize(SwapDoc, patch.document) == SwapDoc(SwapRecord("B", "A"))


def test_two_locations_share_record_shape_but_keep_own_conversion_and_alias():
    result = build_rework(
        ImportEntry(ModernEntry),
        MODERN_ORIGINAL,
        [TargetExpectation(("money_loc", "money"), Decimal("99.99"))],
        [SourceChange(("money_loc", "money"), ("99.99",))],
    )
    assert result.feasible
    reimported = deserialize(ModernEntry, result.patches[0].document)
    assert reimported.money_loc.money == Decimal("99.99")

    influence = build_influence(ModernEntry)
    assert influence.influencers(("money_loc", "money")) == (
        ("money_loc", "money"),
    )
    with pytest.raises(ValidationError):
        deserialize(
            ModernEntry,
            {
                **MODERN_ORIGINAL,
                "money_loc": {"locCode": "M1", "marker": "12.50"},
            },
        )


def test_equal_length_patches_on_different_source_paths_are_both_returned():
    replacement = {"locCode": "G1", "marker": "out"}
    result = build_rework(
        ImportEntry(ModernEntry),
        MODERN_ORIGINAL,
        [TargetExpectation(("generic_loc", "marker"), "out")],
        [
            SourceChange(("generic_loc", "marker"), ("out",)),
            SourceChange(("generic_loc",), (replacement,)),
        ],
    )
    assert len(result.patches) == 2
    assert {patch.size for patch in result.patches} == {1}
    changed = {patch.changed_paths for patch in result.patches}
    assert changed == {(("generic_loc", "marker"),), (("generic_loc",),)}
    for patch in result.patches:
        assert deserialize(ModernEntry, patch.document).generic_loc.marker == "out"


def test_remove_candidate_deletes_property_and_uses_default():
    result = build_rework(
        ImportEntry(ModernEntry),
        {**MODERN_ORIGINAL, "note": "present"},
        [TargetExpectation(("note",), Missing)],
        [SourceChange(("note",), (Remove, None))],
    )
    assert result.patches[0].operations[0].action == "remove"
    assert "note" not in result.patches[0].document


def test_unsatisfiable_target_without_adjustable_source_is_blocked():
    result = build_rework(
        ImportEntry(ModernEntry),
        MODERN_ORIGINAL,
        [TargetExpectation(("docCategory",), "service")],
        [SourceChange(("generic_loc", "marker"), ("out",))],
    )
    assert not result.feasible
    blocked_paths = [expectation.path for expectation in result.report.blocked]
    assert ("docCategory",) in blocked_paths


def test_unknown_warehouse_field_is_reported_back():
    result = build_rework(
        ImportEntry(ModernEntry),
        MODERN_ORIGINAL,
        [TargetExpectation(("ghost",), "x")],
        [SourceChange(("docCategory",), ("service",))],
    )
    assert not result.feasible
    assert result.report.blocked[0].path == ("ghost",)


def test_volume_with_many_details_is_not_fully_enumerated():
    original = {
        **MODERN_ORIGINAL,
        "details": [{"lineId": i, "label": f"L{i}"} for i in range(500)],
    }
    changes = [
        SourceChange(("docCategory",), ("service",)),
        *[
            SourceChange(("details", i, "label"), (f"X{i}",))
            for i in range(500)
        ],
    ]
    result = build_rework(
        ImportEntry(ModernEntry),
        original,
        [TargetExpectation(("docCategory",), "service")],
        changes,
    )
    assert result.patches[0].size == 1
    assert result.patches[0].document["details"] == original["details"]


def test_caller_validator_rejects_invalid_amendment():
    def reject_service(entry: ModernEntry) -> None:
        if entry.category == "service":
            raise ValidationError([{"loc": ["docCategory"], "err": "forbidden"}])

    entry = ImportEntry(ModernEntry, validators=(reject_service,))
    result = build_rework(
        entry,
        MODERN_ORIGINAL,
        [TargetExpectation(("docCategory",), "service")],
        [SourceChange(("docCategory",), ("service",))],
    )
    assert not result.feasible
    blocked_paths = [expectation.path for expectation in result.report.blocked]
    assert ("docCategory",) in blocked_paths
    assert result.report.conflicts == ()


def test_detail_line_is_targeted_by_index_without_enumerating_siblings():
    original = {
        "docCategory": "goods",
        "generic_loc": {"locCode": "G1", "marker": "in"},
        "money_loc": {"locCode": "M1", "money": "12.50"},
        "details": [{"lineId": i, "label": f"L{i}"} for i in range(500)],
    }
    changes = [
        SourceChange(("details", i, "label"), (f"X{i}",)) for i in range(500)
    ]
    result = build_rework(
        ImportEntry(ModernEntry),
        original,
        [TargetExpectation(("details", 7, "label"), "X7")],
        changes,
    )
    patch = result.patches[0]
    assert patch.changed_paths == (("details", 7, "label"),)
    reimported = deserialize(ModernEntry, patch.document)
    assert reimported.details[7].label == "X7"
    assert reimported.details[8].label == "L8"
    assert len(patch.document["details"]) == 500


def test_whole_list_replacement_is_one_equally_short_alternative():
    original = {
        "docCategory": "goods",
        "generic_loc": {"locCode": "G1", "marker": "in"},
        "money_loc": {"locCode": "M1", "money": "12.50"},
        "details": [{"lineId": 0, "label": "L0"}, {"lineId": 1, "label": "L1"}],
    }
    replacement = [{"lineId": 0, "label": "X0"}, {"lineId": 1, "label": "L1"}]
    result = build_rework(
        ImportEntry(ModernEntry),
        original,
        [TargetExpectation(("details", 0, "label"), "X0")],
        [
            SourceChange(("details", 0, "label"), ("X0",)),
            SourceChange(("details",), (replacement,)),
        ],
    )
    changed = {patch.changed_paths for patch in result.patches}
    assert changed == {(("details", 0, "label"),), (("details",),)}
    assert {patch.size for patch in result.patches} == {1}


def test_duplicate_specs_are_rejected():
    with pytest.raises(ValueError):
        build_rework(
            ImportEntry(ModernEntry),
            MODERN_ORIGINAL,
            [TargetExpectation(("docCategory",), "service")],
            [
                SourceChange(("docCategory",), ("service",)),
                SourceChange(("docCategory",), ("goods",)),
            ],
        )
