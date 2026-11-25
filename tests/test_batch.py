"""Tests for whole-batch structuring and cross-record references."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Literal

import pytest
from attrs import define, field

from cattrs import Converter
from cattrs.batch import (
    BatchAmbiguityError,
    BatchConflictError,
    BatchError,
    BatchIncompleteError,
    BatchModel,
    BatchSpec,
    Reference,
    TableSpec,
    export_batch,
    structure_batch,
    watch_model,
)


@define
class Part:
    id: str


@define
class Gear(Part):
    ratio: float = 1.0


@define
class Frame(Part):
    length: float = 0.0


@define
class Motor(Gear):
    power: int = 0


@define
class Assembly:
    id: str
    part: Part | None = None


@define
class GearAssembly:
    id: str
    part: Motor


@dataclass
class DPart:
    id: str


@dataclass
class DGear(DPart):
    ratio: float = dc_field(default=1.0)


@dataclass
class DAssembly:
    id: str
    part: DPart | None = None


LEGACY = {"Part": "part", "Gear": "gear", "Frame": "frame", "Motor": "motor"}
MODERN = {"Part": "P", "Gear": "G", "Frame": "F", "Motor": "M"}


def make_spec(**kwargs):
    return BatchSpec(
        tables={
            "legacy": TableSpec(Part, labels=LEGACY),
            "modern": TableSpec(Part, labels=MODERN, version="v2"),
            "assembly": TableSpec(Assembly, labels={"Assembly": "A"}),
        },
        references={"assembly": {"part": Reference("part_id")}},
        id_fields={"legacy": "id", "modern": "id", "assembly": "id"},
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Baseline conversion is untouched
# ---------------------------------------------------------------------------


def test_existing_conversion_unchanged():
    converter = Converter()
    assert converter.structure({"id": "p1"}, Part) == Part("p1")
    assert converter.unstructure(Part("p1")) == {"id": "p1"}


# ---------------------------------------------------------------------------
# Labels, versions, and multiple views
# ---------------------------------------------------------------------------


def test_legacy_and_modern_labels_for_one_subtype():
    spec = make_spec()
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "gear", "ratio": 2.5}],
            "modern": [{"id": "p1", "type": "G"}],
            "assembly": [],
        },
    )
    assert batch.ok
    part = batch.by_registration("p1")
    assert type(part) is Gear
    assert part.ratio == 2.5
    # The occurrence is one object, not two table-specific copies.
    assert batch.tables["legacy"][0] is batch.tables["modern"][0]


def test_version_restricts_available_types():
    spec = BatchSpec(
        tables={
            "modern": TableSpec(
                Part, labels=MODERN, versions={"Motor": "v3"}, version="v2"
            )
        },
        id_fields={"modern": "id"},
    )
    batch = structure_batch(
        Converter(), spec, {"modern": [{"id": "p1", "type": "M", "power": 5}]}
    )
    # Motor is unavailable in v2, so the record has no valid explanation.
    assert not batch.ok
    assert batch.diagnostics.conflicts


def test_blank_does_not_mean_null():
    spec = make_spec()
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "gear"}],
            "modern": [{"id": "p1", "type": "G", "ratio": None}],
            "assembly": [],
        },
    )
    # An explicit null on a non-optional field contradicts the evidence.
    assert not batch.ok
    assert batch.diagnostics.conflicts


# ---------------------------------------------------------------------------
# Global evidence and narrowing
# ---------------------------------------------------------------------------


def test_later_reference_narrows_earlier_base_view():
    spec = BatchSpec(
        tables={
            "legacy": TableSpec(Part, labels=LEGACY),
            "modern": TableSpec(Part, labels=MODERN, version="v2"),
            "assembly": TableSpec(GearAssembly, labels={"GearAssembly": "GA"}),
        },
        references={"assembly": {"part": Reference("part_id")}},
        id_fields={"legacy": "id", "modern": "id", "assembly": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            # A base-only view, seen first.
            "legacy": [{"id": "p1", "type": "part"}],
            # A finer requirement from a relation declared later.
            "assembly": [{"id": "a1", "type": "GA", "part_id": "p1"}],
        },
    )
    assert batch.ok
    assert type(batch.by_registration("p1")) is Motor


def test_sibling_reference_shares_the_subtype():
    spec = BatchSpec(
        tables={
            "legacy": TableSpec(Part, labels=LEGACY),
            "assembly": TableSpec(Assembly, labels={"Assembly": "A"}),
            "gears": TableSpec(GearAssembly, labels={"GearAssembly": "GA"}),
        },
        references={
            "assembly": {"part": Reference("part_id")},
            "gears": {"part": Reference("part_id")},
        },
        id_fields={"legacy": "id", "assembly": "id", "gears": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "part"}],
            "assembly": [{"id": "a1", "type": "A", "part_id": "p1"}],
            "gears": [{"id": "g1", "type": "GA", "part_id": "p1"}],
        },
    )
    assert batch.ok
    part = batch.by_registration("p1")
    assert type(part) is Motor
    assert batch.by_registration("a1").part is part
    assert batch.by_registration("g1").part is part


def test_relation_field_type_rejects_other_interpretation():
    spec = BatchSpec(
        tables={
            "legacy": TableSpec(Part, labels=LEGACY),
            "assembly": TableSpec(GearAssembly, labels={"GearAssembly": "GA"}),
        },
        references={"assembly": {"part": Reference("part_id")}},
        id_fields={"legacy": "id", "assembly": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "frame", "length": 3.0}],
            "assembly": [{"id": "a1", "type": "GA", "part_id": "p1"}],
        },
    )
    assert not batch.ok
    assert batch.diagnostics.conflicts
    conflict = batch.diagnostics.conflicts[0]
    assert "part" in conflict.reason
    assert conflict.references == [("assembly", "part", "p1")]


# ---------------------------------------------------------------------------
# Identity and references
# ---------------------------------------------------------------------------


def test_resolved_reference_is_the_same_object():
    spec = make_spec()
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "gear", "ratio": 2.0}],
            "assembly": [{"id": "a1", "type": "A", "part_id": "p1"}],
        },
    )
    assert batch.by_registration("a1").part is batch.by_registration("p1")
    assert batch.tables["legacy"][0] is batch.by_registration("p1")


def test_forward_references_may_arrive_later():
    spec = make_spec()
    batch = structure_batch(
        Converter(),
        spec,
        {
            "assembly": [{"id": "a1", "type": "A", "part_id": "p9"}],
            "legacy": [{"id": "p9", "type": "gear", "ratio": 1.5}],
        },
    )
    assert batch.ok
    assert batch.by_registration("a1").part is batch.by_registration("p9")


# ---------------------------------------------------------------------------
# Late binding and cycles
# ---------------------------------------------------------------------------


@define
class Node:
    id: str
    peer: Node | None = field(default=None, metadata={"cattrs_late": True})


GuardedGlobal = None


def test_late_bound_cycle_is_legal_and_identity_shared():
    spec = BatchSpec(
        tables={"nodes": TableSpec(Node, labels={"Node": "N"})},
        references={"nodes": {"peer": Reference("peer_id", late=True)}},
        id_fields={"nodes": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "nodes": [
                {"id": "n1", "type": "N", "peer_id": "n2"},
                {"id": "n2", "type": "N", "peer_id": "n1"},
            ]
        },
    )
    assert batch.ok
    n1, n2 = batch.by_registration("n1"), batch.by_registration("n2")
    assert n1.peer is n2
    assert n2.peer is n1


@define
class StrictNode:
    id: str
    peer: StrictNode | None = None


def test_undeclared_cycle_is_reported():
    spec = BatchSpec(
        tables={"nodes": TableSpec(StrictNode, labels={"StrictNode": "N"})},
        references={"nodes": {"peer": Reference("peer_id")}},
        id_fields={"nodes": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "nodes": [
                {"id": "n1", "type": "N", "peer_id": "n2"},
                {"id": "n2", "type": "N", "peer_id": "n1"},
            ]
        },
    )
    assert not batch.ok
    assert batch.diagnostics.cycles
    assert batch.tables["nodes"] == []


@watch_model
@define
class GuardedNode:
    id: str
    peer: GuardedNode | None = field(default=None, metadata={"cattrs_late": True})


@define
class GuardedModel(BatchModel):
    id: str
    peer: GuardedModel | None = field(default=None, metadata={"cattrs_late": True})


def test_partial_access_raises_until_complete():
    spec = BatchSpec(
        tables={"g": TableSpec(GuardedNode, labels={"GuardedNode": "G"})},
        references={"g": {"peer": Reference("peer_id", late=True)}},
        id_fields={"g": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "g": [
                {"id": "g1", "type": "G", "peer_id": "g2"},
                {"id": "g2", "type": "G", "peer_id": "g1"},
            ]
        },
    )
    # Delivered objects are complete, so normal access works.
    assert batch.by_registration("g1").peer is batch.by_registration("g2")


def test_batch_model_guard_blocks_partial_objects():
    spec = BatchSpec(
        tables={"g": TableSpec(GuardedModel, labels={"GuardedModel": "G"})},
        references={"g": {"peer": Reference("peer_id", late=True)}},
        id_fields={"g": "id"},
    )
    batch = structure_batch(Converter(), spec, {"g": [{"id": "g1", "type": "G"}]})
    assert batch.ok


# ---------------------------------------------------------------------------
# Ambiguity and conflicts
# ---------------------------------------------------------------------------


def test_ambiguous_component_lists_distinguishing_evidence():
    spec = BatchSpec(
        tables={
            "legacy": TableSpec(Part, labels=LEGACY),
            "assembly": TableSpec(Assembly, labels={"Assembly": "A"}),
        },
        references={"assembly": {"part": Reference("part_id")}},
        id_fields={"legacy": "id", "assembly": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "part"}],
            "assembly": [{"id": "a1", "type": "A", "part_id": "p1"}],
        },
    )
    assert not batch.ok
    ambiguity = batch.diagnostics.ambiguities[0]
    names = {candidate.name for candidate in ambiguity.candidates}
    assert {"Part", "Gear", "Frame", "Motor"} <= names
    # At least the subtypes offer distinguishing field evidence.
    assert any(candidate.distinguishing_fields for candidate in ambiguity.candidates)
    assert all(candidate.reasons for candidate in ambiguity.candidates)
    assert "ambiguous" in batch.diagnostics.explain()


def test_conflicting_declarations_point_at_both_mentions():
    spec = make_spec()
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "gear", "ratio": 1.0}],
            "modern": [{"id": "p1", "type": "F", "length": 2.0}],
        },
    )
    assert not batch.ok
    conflict = batch.diagnostics.conflicts[0]
    assert {"legacy[0]", "modern[0]"} <= set(conflict.locations)


def test_unrelated_component_still_delivered():
    spec = BatchSpec(
        tables={
            "legacy": TableSpec(Part, labels=LEGACY),
            "assembly": TableSpec(GearAssembly, labels={"GearAssembly": "GA"}),
        },
        references={"assembly": {"part": Reference("part_id")}},
        id_fields={"legacy": "id", "assembly": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [
                {"id": "bad", "type": "frame", "length": 1.0},
                {"id": "good", "type": "motor", "power": 3},
            ],
            "assembly": [{"id": "a1", "type": "GA", "part_id": "bad"}],
        },
    )
    assert not batch.ok
    assert batch.by_registration("good").power == 3
    with pytest.raises(BatchIncompleteError):
        batch.by_registration("bad")


def test_missing_required_field_is_reported():
    spec = BatchSpec(
        tables={"legacy": TableSpec(Part, labels=LEGACY)}, id_fields={"legacy": "id"}
    )
    batch = structure_batch(
        Converter(), spec, {"legacy": [{"id": "p1", "type": "gear"}]}
    )
    # ratio is optional, so this is fine.
    assert batch.ok

    @define
    class Required:
        id: str
        must: int

    spec = BatchSpec(
        tables={"r": TableSpec(Required, labels={"Required": "R"})},
        id_fields={"r": "id"},
    )
    batch = structure_batch(Converter(), spec, {"r": [{"id": "r1", "type": "R"}]})
    assert not batch.ok
    assert batch.diagnostics.missing_fields


# ---------------------------------------------------------------------------
# Strict mode and errors
# ---------------------------------------------------------------------------


def test_strict_raises_conflict_error():
    spec = make_spec(strict=True)
    with pytest.raises(BatchConflictError):
        structure_batch(
            Converter(),
            spec,
            {
                "legacy": [{"id": "p1", "type": "gear", "ratio": 1.0}],
                "modern": [{"id": "p1", "type": "F", "length": 2.0}],
            },
        )


def test_strict_raises_ambiguity_error():
    spec = BatchSpec(
        tables={"legacy": TableSpec(Part, labels=LEGACY)},
        id_fields={"legacy": "id"},
        strict=True,
    )
    with pytest.raises(BatchAmbiguityError):
        structure_batch(Converter(), spec, {"legacy": [{"id": "p1", "type": "part"}]})


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_uses_each_table_labels():
    spec = make_spec()
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "gear", "ratio": 2.0}],
            "modern": [{"id": "p1", "type": "G"}],
            "assembly": [{"id": "a1", "type": "A", "part_id": "p1"}],
        },
    )
    exported = export_batch(batch)
    assert exported["legacy"][0]["type"] == "gear"
    assert exported["modern"][0]["type"] == "G"
    assert exported["assembly"][0]["part_id"] == "p1"


def test_export_round_trips():
    spec = make_spec()
    rows = {
        "legacy": [{"id": "p1", "type": "gear", "ratio": 2.0}],
        "modern": [],
        "assembly": [{"id": "a1", "type": "A", "part_id": "p1"}],
    }
    batch = structure_batch(Converter(), spec, rows)
    exported = export_batch(batch)
    again = structure_batch(Converter(), spec, exported)
    assert again.ok
    assert again.by_registration("a1").part is again.by_registration("p1")
    assert again.by_registration("p1").ratio == 2.0


def test_export_field_renaming():
    @define
    class Renamed:
        id: str
        serial: str = ""

    spec = BatchSpec(
        tables={
            "r": TableSpec(
                Renamed, labels={"Renamed": "R"}, field_map={"serial": "serial_no"}
            )
        },
        id_fields={"r": "id"},
    )
    batch = structure_batch(
        Converter(), spec, {"r": [{"id": "r1", "type": "R", "serial_no": "x"}]}
    )
    assert batch.ok
    assert batch.by_registration("r1").serial == "x"
    assert export_batch(batch)["r"][0]["serial_no"] == "x"


# ---------------------------------------------------------------------------
# Field metadata declarations
# ---------------------------------------------------------------------------


def test_metadata_reference_declaration():
    @define
    class Linked:
        id: str
        other: Part | None = field(
            default=None, metadata={"cattrs_reference": Reference("other_id")}
        )

    spec = BatchSpec(
        tables={
            "legacy": TableSpec(Part, labels=LEGACY),
            "links": TableSpec(Linked, labels={"Linked": "L"}),
        },
        id_fields={"legacy": "id", "links": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "gear", "ratio": 4.0}],
            "links": [{"id": "l1", "type": "L", "other_id": "p1"}],
        },
    )
    assert batch.ok
    assert batch.by_registration("l1").other is batch.by_registration("p1")
    # The declared field type also participated in narrowing.
    assert type(batch.by_registration("p1")) is Gear


def test_literal_tag_is_respected():
    @define
    class Tagged(Part):
        kind: Literal["tagged"] = "tagged"

    spec = BatchSpec(
        tables={"legacy": TableSpec(Part, labels={"Part": "part", "Tagged": "tagged"})},
        id_fields={"legacy": "id"},
        strict=True,
    )
    batch = structure_batch(
        Converter(), spec, {"legacy": [{"id": "p1", "type": "tagged"}]}
    )
    assert type(batch.by_registration("p1")) is Tagged


def test_unknown_table_raises():
    with pytest.raises(BatchError):
        structure_batch(
            Converter(),
            BatchSpec(tables={"a": TableSpec(Part, labels=LEGACY)}),
            {"b": []},
        )


# ---------------------------------------------------------------------------
# Table-specific value mappings and export shape
# ---------------------------------------------------------------------------


@define
class StatusPart(Part):
    status: str = "new"


def test_model_codes_differ_per_table():
    spec = BatchSpec(
        tables={
            "legacy": TableSpec(
                StatusPart,
                labels={"Part": "part", "StatusPart": "status"},
                model_codes={"StatusPart": {"status": {"new": "N", "used": "U"}}},
            ),
            "modern": TableSpec(
                StatusPart,
                labels={"Part": "P", "StatusPart": "S"},
                model_codes={
                    "StatusPart": {"status": {"new": "fresh", "used": "worn"}}
                },
                version="v2",
            ),
        },
        id_fields={"legacy": "id", "modern": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "status", "status": "U"}],
            "modern": [{"id": "p1", "type": "S"}],
        },
    )
    assert batch.ok
    assert batch.by_registration("p1").status == "used"
    exported = export_batch(batch)
    assert exported["legacy"][0]["status"] == "U"
    # The modern row did not report status; blank stays blank by default.
    assert "status" not in exported["modern"][0]
    full = export_batch(batch, reported_only=False)
    assert full["modern"][0]["status"] == "worn"


def test_export_fields_limits_columns_per_table():
    spec = BatchSpec(
        tables={
            "legacy": TableSpec(Part, labels=LEGACY, export_fields=("ratio", "length"))
        },
        id_fields={"legacy": "id"},
    )
    batch = structure_batch(
        Converter(), spec, {"legacy": [{"id": "p1", "type": "gear", "ratio": 2.0}]}
    )
    row = export_batch(batch)["legacy"][0]
    assert row["id"] == "p1"
    assert row["ratio"] == 2.0
    assert "type" in row


def test_references_many():
    @define
    class Kit:
        id: str
        pieces: list[Part] | None = field(default=None, metadata={"cattrs_late": True})

    spec = BatchSpec(
        tables={
            "legacy": TableSpec(Part, labels=LEGACY),
            "kits": TableSpec(Kit, labels={"Kit": "K"}),
        },
        references={"kits": {"pieces": Reference("piece_ids", many=True, late=True)}},
        id_fields={"legacy": "id", "kits": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [
                {"id": "p1", "type": "gear", "ratio": 1.0},
                {"id": "p2", "type": "frame", "length": 2.0},
            ],
            "kits": [{"id": "k1", "type": "K", "piece_ids": ["p1", "p2"]}],
        },
    )
    assert batch.ok
    kit = batch.by_registration("k1")
    assert [piece.id for piece in kit.pieces] == ["p1", "p2"]
    assert kit.pieces[0] is batch.by_registration("p1")


def test_dataclass_models():
    spec = BatchSpec(
        tables={
            "legacy": TableSpec(DPart, labels={"DPart": "part", "DGear": "gear"}),
            "assembly": TableSpec(DAssembly, labels={"DAssembly": "A"}),
        },
        references={"assembly": {"part": Reference("part_id")}},
        id_fields={"legacy": "id", "assembly": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "gear", "ratio": 3.0}],
            "assembly": [{"id": "a1", "type": "A", "part_id": "p1"}],
        },
    )
    assert batch.ok
    assert type(batch.by_registration("p1")) is DGear
    assert batch.by_registration("a1").part is batch.by_registration("p1")


def test_table_order_does_not_change_result():
    spec = make_spec()
    rows = {
        "legacy": [{"id": "p1", "type": "gear", "ratio": 2.0}],
        "modern": [{"id": "p1", "type": "G"}],
        "assembly": [{"id": "a1", "type": "A", "part_id": "p1"}],
    }
    first = structure_batch(Converter(), spec, rows)
    reordered = dict(reversed(list(rows.items())))
    second = structure_batch(Converter(), spec, reordered)
    assert first.ok and second.ok
    assert type(first.by_registration("p1")) is type(second.by_registration("p1"))


def test_occurrence_mentioned_only_by_reference_is_missing():
    spec = make_spec()
    batch = structure_batch(
        Converter(), spec, {"assembly": [{"id": "a1", "type": "A", "part_id": "p404"}]}
    )
    assert not batch.ok
    assert batch.diagnostics.missing_references
    assert batch.tables["assembly"] == []


@define
class JBase:
    id: str


@define
class JGear(JBase):
    pass


@define
class JMotor(JGear):
    pass


@define
class JFrame(JBase):
    pass


@define
class JKit:
    id: str
    a: JGear
    b: JMotor


def test_joint_evidence_from_two_relations():
    spec = BatchSpec(
        tables={
            "parts": TableSpec(
                JBase,
                labels={
                    "JBase": "base",
                    "JGear": "gear",
                    "JMotor": "motor",
                    "JFrame": "frame",
                },
            ),
            "kits": TableSpec(JKit, labels={"JKit": "K"}),
        },
        references={"kits": {"a": Reference("aid"), "b": Reference("bid")}},
        id_fields={"parts": "id", "kits": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "parts": [{"id": "p1", "type": "base"}],
            "kits": [{"id": "k1", "type": "K", "aid": "p1", "bid": "p1"}],
        },
    )
    assert batch.ok
    assert type(batch.by_registration("p1")) is JMotor


def test_explicit_null_reference_is_absence():
    spec = BatchSpec(
        tables={
            "legacy": TableSpec(Part, labels=LEGACY),
            "assembly": TableSpec(Assembly, labels={"Assembly": "A"}),
        },
        references={"assembly": {"part": Reference("part_id")}},
        id_fields={"legacy": "id", "assembly": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "legacy": [{"id": "p1", "type": "gear"}],
            "assembly": [{"id": "a1", "type": "A", "part_id": None}],
        },
    )
    assert batch.ok
    assert batch.by_registration("a1").part is None
    assert export_batch(batch)["assembly"][0]["part_id"] is None


@define
class ReqNodeMeta:
    id: str
    other: ReqNodeMeta = field(metadata={"cattrs_late": True})


def test_required_late_relation_uses_second_phase():
    spec = BatchSpec(
        tables={"nodes": TableSpec(ReqNodeMeta, labels={"ReqNodeMeta": "N"})},
        references={"nodes": {"other": Reference("oid", late=True)}},
        id_fields={"nodes": "id"},
    )
    batch = structure_batch(
        Converter(),
        spec,
        {
            "nodes": [
                {"id": "a", "type": "N", "oid": "b"},
                {"id": "b", "type": "N", "oid": "a"},
            ]
        },
    )
    assert batch.ok
    assert batch.by_registration("a").other is batch.by_registration("b")
