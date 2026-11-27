"""Tests for the two-round shipping-slip reconciliation engine."""

import time

import pyarrow as pa
import pytest

from akimbo.reconcile import (
    EMPTY_LIST,
    MISSING_FIELD,
    MISSING_LIST,
    MISSING_PART,
    BackfillEvidence,
    ReconciledRow,
    Round1Slip,
    Round2Slip,
    Shipment,
    display_rows,
    parts_to_arrow,
    reconcile,
)

INT = pa.int64()


def make(shipment_no, parts, slot="BIN-1", types=None):
    return Shipment(
        shipment_no=shipment_no,
        slot_index=slot,
        parts=parts,
        field_types=types or {"v": INT},
    )


# ---------------------------------------------------------------------------
# Round one
# ---------------------------------------------------------------------------


def test_round1_update_insert_ordering():
    shipment = make("S1", [{"v": 1}, {"v": 2}])
    slips = [
        Round1Slip("c1", "S1", "update", [0], fields={"v": 9}),
        Round1Slip("c2", "S1", "insert", [1], side="before", mark="m1",
                   fields={"v": 7}),
    ]
    row = reconcile([shipment], slips)[0]
    assert row.status == "approved"
    assert row.parts == [{"v": 9}, {"v": 7}, {"v": 2}]
    assert row.publishable and not row.unchanged
    assert row.evidence[0].provenance == [
        ("orig", 0),
        ("ins", "m1"),
        ("orig", 1),
    ]


def test_round1_inserts_follow_confirmation_order():
    shipment = make("S1", [{"v": 1}])
    slips = [
        Round1Slip("c1", "S1", "insert", [0], side="after", mark="a",
                   fields={"v": 2}),
        Round1Slip("c2", "S1", "insert", [0], side="after", mark="b",
                   fields={"v": 3}),
        Round1Slip("c3", "S1", "insert", [0], side="before", mark="c",
                   fields={"v": 0}),
    ]
    row = reconcile([shipment], slips)[0]
    assert row.parts == [{"v": 0}, {"v": 1}, {"v": 2}, {"v": 3}]


def test_round1_field_value_conflict_rejects_whole_row():
    shipment = make("S1", [{"v": 1}, {"v": 2}])
    slips = [
        Round1Slip("c1", "S1", "update", [0], fields={"v": 9}),
        Round1Slip("c2", "S1", "update", [0], fields={"v": 3}),
    ]
    row = reconcile([shipment], slips)[0]
    assert row.status == "rejected"
    assert row.parts == [{"v": 1}, {"v": 2}]
    assert row.returned_confirm_ids == ["c1", "c2"]


def test_round1_update_and_delete_same_part_rejects():
    shipment = make("S1", [{"v": 1}, {"v": 2}])
    slips = [
        Round1Slip("c1", "S1", "update", [1], fields={"v": 9}),
        Round1Slip("c2", "S1", "remove", [1]),
    ]
    row = reconcile([shipment], slips)[0]
    assert row.status == "rejected"
    assert row.parts == [{"v": 1}, {"v": 2}]


def test_round1_remove_and_anchor_insert_allowed():
    shipment = make("S1", [{"v": 1}, {"v": 2}])
    slips = [
        Round1Slip("c1", "S1", "remove", [0]),
        Round1Slip("c2", "S1", "insert", [0], side="after", mark="m",
                   fields={"v": 5}),
    ]
    row = reconcile([shipment], slips)[0]
    assert row.status == "approved"
    assert row.parts == [{"v": 5}, {"v": 2}]
    assert row.evidence[0].provenance == [("ins", "m"), ("orig", 1)]


def test_round1_repeated_mark_is_one_physical_part():
    shipment = make("S1", [{"v": 1}, {"v": 2}])
    slips = [
        Round1Slip("c1", "S1", "insert", [0], side="after", mark="m",
                   fields={"v": 7}),
        Round1Slip("c2", "S1", "insert", [0], side="after", mark="m",
                   fields={"w": 4}),
    ]
    row = reconcile([shipment, make("S2", [{"v": 0}])], slips)[0]
    assert row.status == "approved"
    assert row.parts == [{"v": 1}, {"v": 7, "w": 4}, {"v": 2}]


def test_round1_repeated_mark_disagreement_conflicts():
    shipment = make("S1", [{"v": 1}])
    slips = [
        Round1Slip("c1", "S1", "insert", [0], side="after", mark="m",
                   fields={"v": 7}),
        Round1Slip("c2", "S1", "insert", [0], side="after", mark="m",
                   fields={"v": 8}),
    ]
    row = reconcile([shipment], slips)[0]
    assert row.status == "rejected"
    assert row.parts == [{"v": 1}]


def test_round1_copied_lost_slot_ambiguous_when_outcomes_differ():
    # Same observed value sits at two originals; inserting before either
    # gives two distinct orders, so the row cannot be pinned.
    shipment = make("S1", [{"v": 5}, {"v": 5}])
    slips = [
        Round1Slip("c1", "S1", "insert", [0, 1], side="before", mark="m",
                   fields={"v": 9}),
    ]
    row = reconcile([shipment], slips)[0]
    assert row.status == "ambiguous"
    assert row.parts == [{"v": 5}, {"v": 5}]
    orders = [ev.final_order for ev in row.evidence]
    assert {"v": 9} in orders[0] and {"v": 9} in orders[1]
    provs = [ev.provenance for ev in row.evidence]
    assert provs[0] != provs[1]
    # The two witnesses must resolve the same confirm to different slots.
    assert row.evidence[0].round1_resolution != row.evidence[1].round1_resolution


def test_round1_equal_values_but_different_provenance_stays_ambiguous():
    # Removing either identical part gives the same rendered order and
    # fields, but a different original provenance, so it cannot be published.
    shipment = make("S1", [{"v": 5}, {"v": 5}])
    slips = [Round1Slip("c1", "S1", "remove", [0, 1])]
    row = reconcile([shipment], slips)[0]
    assert row.status == "ambiguous"
    rendered = [ev.final_order for ev in row.evidence]
    assert rendered[0] == rendered[1] == [{"v": 5}]
    provs = [tuple(ev.provenance) for ev in row.evidence]
    assert set(provs) == {(("orig", 0),), (("orig", 1),)}


def test_round1_noop_correction_converges_despite_lost_slot():
    # Writing back the existing value at either identical slot is a no-op:
    # both backfits share order, fields and provenance, so publish.
    shipment = make("S1", [{"v": 5}, {"v": 5}])
    slips = [Round1Slip("c1", "S1", "update", [0, 1], fields={"v": 5})]
    row = reconcile([shipment], slips)[0]
    assert row.status == "approved" and row.unchanged
    assert row.parts == [{"v": 5}, {"v": 5}]


def test_round1_dedupe_resends_by_confirm_id():
    shipment = make("S1", [{"v": 1}])
    slips = [
        Round1Slip("c1", "S1", "update", [0], fields={"v": 9}),
        Round1Slip("c1", "S1", "update", [0], fields={"v": 3}),
    ]
    row = reconcile([shipment], slips)[0]
    assert row.status == "approved"
    assert row.parts == [{"v": 9}]


def test_round1_missing_list_has_no_locatable_slot():
    shipment = make("S1", None)
    slips = [Round1Slip("c1", "S1", "update", [0], fields={"v": 1})]
    row = reconcile([shipment], slips)[0]
    assert row.status == "unsat"
    assert row.parts is None
    assert row.first_fatal == {
        "round": 1,
        "confirm_id": "c1",
        "round_sequence_position": 0,
        "sequence_position": 0,
    }


def test_round1_missing_part_update_impossible_but_anchor_insert_works():
    shipment = make("S1", [{"v": 1}, MISSING_PART])
    update = [Round1Slip("c1", "S1", "update", [1], fields={"v": 2})]
    assert reconcile([shipment], update)[0].status == "unsat"

    slips = [
        Round1Slip("c2", "S1", "remove", [1]),
        Round1Slip("c3", "S1", "insert", [1], side="before", mark="m",
                   fields={"v": 8}),
    ]
    row = reconcile([shipment], slips)[0]
    assert row.status == "approved"
    assert row.parts == [{"v": 1}, {"v": 8}]


# ---------------------------------------------------------------------------
# Round two
# ---------------------------------------------------------------------------


def test_round2_points_at_round_one_inserted_display_row():
    shipment = make("S1", [{"v": 1}, {"v": 2}])
    r1 = [
        Round1Slip("a", "S1", "insert", [0], side="after", mark="m1",
                   fields={"v": 7}),
    ]
    # display rows after round one: 0 -> {1}, 1 -> {7} (new), 2 -> {2}
    r2 = [
        Round2Slip("b", "S1", "update", [1], observed={"v": 7},
                   fields={"v": 70}),
    ]
    row = reconcile([shipment], r1, r2)[0]
    assert row.status == "approved"
    assert row.parts == [{"v": 1}, {"v": 70}, {"v": 2}]
    assert row.evidence[0].round2_resolution == {"b": 1}


def test_round2_display_observation_partial_fields():
    shipment = make("S1", [{"v": 1, "w": 2}], types={"v": INT, "w": INT})
    r2 = [
        Round2Slip("o", "S1", "update", [0],
                   observed={"v": 1, "w": MISSING_FIELD}, fields={"w": 3}),
    ]
    row = reconcile([shipment], [], r2)[0]
    assert row.status == "approved"
    assert row.parts == [{"v": 1, "w": 3}]


def test_round2_observation_rules_out_wrong_display_row():
    shipment = make("S1", [{"v": 1}, {"v": 2}])
    r2 = [
        Round2Slip("o", "S1", "update", [0, 1], observed={"v": 2},
                   fields={"v": 9}),
    ]
    row = reconcile([shipment], [], r2)[0]
    assert row.status == "approved"
    assert row.parts == [{"v": 1}, {"v": 9}]
    assert row.evidence[0].round2_resolution == {"o": 1}


def test_round2_insert_beside_anchor_in_confirmation_order():
    shipment = make("S1", [{"v": 1}])
    r2 = [
        Round2Slip("o1", "S1", "insert", [0], observed={"v": 1},
                   side="after", mark="x", fields={"v": 2}),
        Round2Slip("o2", "S1", "insert", [0], observed={"v": 1},
                   side="after", mark="y", fields={"v": 3}),
    ]
    row = reconcile([shipment], [], r2)[0]
    assert row.parts == [{"v": 1}, {"v": 2}, {"v": 3}]


def test_round2_conflict_rejects_round_two_only():
    shipment = make("S1", [{"v": 1}, {"v": 2}])
    r1 = [Round1Slip("a", "S1", "update", [0], fields={"v": 10})]
    r2 = [
        Round2Slip("b1", "S1", "update", [1], observed={"v": 2},
                   fields={"v": 20}),
        Round2Slip("b2", "S1", "remove", [1], observed={"v": 2}),
    ]
    row = reconcile([shipment], r1, r2)[0]
    assert row.status == "rejected"
    # Round one stays applied; the round-two row reverts to its pre-round shape.
    assert row.parts == [{"v": 10}, {"v": 2}]
    assert row.returned_confirm_ids == ["b1", "b2"]
    assert row.evidence[0].round2_rejected is True
    assert row.evidence[0].round1_rejected is False


def test_round2_first_fatal_observation():
    shipment = make("S1", [{"v": 1}])
    r2 = [
        Round2Slip("o1", "S1", "update", [0], observed={"v": 1},
                   fields={"v": 2}),
        Round2Slip("o2", "S1", "update", [0], observed={"v": 99},
                   fields={"v": 3}),
    ]
    row = reconcile([shipment], [], r2)[0]
    assert row.status == "unsat"
    assert row.first_fatal["round"] == 2
    assert row.first_fatal["confirm_id"] == "o2"
    assert row.first_fatal["round_sequence_position"] == 1


# ---------------------------------------------------------------------------
# The three raw list states
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parts, sign",
    [
        (None, MISSING_LIST),
        ([], EMPTY_LIST),
        ([MISSING_PART], MISSING_PART),
    ],
)
def test_display_rows_keeps_three_states_distinct(parts, sign):
    assert display_rows(parts) == [sign]


def test_live_part_with_unrecorded_field_is_not_missing_part():
    shipment = make("S1", [{"v": 1}], types={"v": INT, "w": INT})
    # The part exists; only field "w" was not copied.
    r2 = [
        Round2Slip("o", "S1", "update", [0],
                   observed={"v": 1, "w": MISSING_FIELD}, fields={"w": 7}),
    ]
    row = reconcile([shipment], [], r2)[0]
    assert row.status == "approved"
    assert row.parts == [{"v": 1, "w": 7}]


def test_round2_inserts_materialise_missing_and_empty_lists():
    for parts in (None, []):
        shipment = make("S1", parts)
        r2 = [
            Round2Slip("o1", "S1", "insert", [0],
                       observed=MISSING_LIST if parts is None else EMPTY_LIST,
                       side="after", mark="x", fields={"v": 1}),
            Round2Slip("o2", "S1", "insert", [0],
                       observed=MISSING_LIST if parts is None else EMPTY_LIST,
                       side="after", mark="y", fields={"v": 2}),
        ]
        row = reconcile([shipment], [], r2)[0]
        assert row.status == "approved"
        assert row.parts == [{"v": 1}, {"v": 2}]


def test_round2_update_on_list_state_row_is_unsat():
    shipment = make("S1", None)
    r2 = [
        Round2Slip("o1", "S1", "update", [0], observed=MISSING_LIST,
                   fields={"v": 1}),
    ]
    row = reconcile([shipment], [], r2)[0]
    assert row.status == "unsat"
    assert row.first_fatal["confirm_id"] == "o1"


def test_missing_part_sign_matches_null_element_only():
    shipment = make("S1", [{"v": 1}, MISSING_PART])
    r2 = [Round2Slip("o", "S1", "remove", [1], observed=MISSING_PART)]
    row = reconcile([shipment], [], r2)[0]
    assert row.status == "approved"
    assert row.parts == [{"v": 1}]


# ---------------------------------------------------------------------------
# The prompt's small example
# ---------------------------------------------------------------------------


def test_small_example_same_slot_cross_sorted_returns():
    # Two tickets share one bin slot and identical first-part values, but
    # their missing-state signatures differ.
    ticket_a = make("A1", [{"v": 1}, {"v": 2}], slot="BIN-9")
    ticket_b = make("A2", [{"v": 1}, MISSING_PART], slot="BIN-9")

    # Round one inserts a new part into ticket A after its first slot.
    r1_a = [
        Round1Slip("r1-a", "A1", "insert", [0], side="after", mark="X",
                   fields={"v": 2}),
    ]
    # Round two signs display row 1, which is the part inserted in round one.
    r2_a = [
        Round2Slip("r2-a", "A1", "update", [1], observed={"v": 2},
                   fields={"v": 4}),
    ]

    # Cross-sort the returned slips so the two tickets interleave; nothing may
    # be merged by bin slot.
    r1_slips = list(r1_a)
    r2_slips = list(r2_a)
    rows = {row.shipment_no: row for row in
            reconcile([ticket_a, ticket_b], r1_slips, r2_slips)}

    row_a, row_b = rows["A1"], rows["A2"]
    assert row_a.status == "approved"
    assert row_a.parts == [{"v": 1}, {"v": 4}, {"v": 2}]
    assert row_a.evidence[0].provenance == [
        ("orig", 0),
        ("ins", "X"),
        ("orig", 1),
    ]
    # Ticket B is untouched and keeps its null part element.
    assert row_b.status == "approved" and row_b.unchanged
    assert row_b.parts == [{"v": 1}, MISSING_PART]
    assert row_a.slot_index == row_b.slot_index == "BIN-9"


def test_equal_values_from_opposite_insert_directions_cannot_be_pinned():
    # Two equal-value parts arrive from different-direction inserts beside
    # different originals; a round-two sign matching either must stay open.
    shipment = make("S1", [{"v": 1}, {"v": 2}])
    r1 = [
        Round1Slip("i1", "S1", "insert", [0], side="after", mark="X",
                   fields={"v": 9}),
        Round1Slip("i2", "S1", "insert", [1], side="before", mark="Y",
                   fields={"v": 9}),
    ]
    # post-round-one display: 0:{1}, 1:{9}=X, 2:{9}=Y, 3:{2}
    r2 = [
        Round2Slip("u", "S1", "update", [1, 2], observed={"v": 9},
                   fields={"v": 5}),
    ]
    row = reconcile([shipment], r1, r2)[0]
    assert row.status == "ambiguous"
    finals = {
        ev.round2_resolution["u"]: ev.final_order for ev in row.evidence
    }
    assert finals[1] == [{"v": 1}, {"v": 5}, {"v": 9}, {"v": 2}]
    assert finals[2] == [{"v": 1}, {"v": 9}, {"v": 5}, {"v": 2}]


# ---------------------------------------------------------------------------
# Whole-checklist behaviour and performance
# ---------------------------------------------------------------------------


def test_rows_without_slips_are_approved_unchanged():
    shipments = [
        make("S1", [{"v": 1}]),
        make("S2", None),
        make("S3", []),
    ]
    rows = reconcile(shipments)
    assert [r.status for r in rows] == ["approved"] * 3
    assert all(r.unchanged for r in rows)
    assert rows[1].parts is None and rows[2].parts == []


def test_unknown_shipment_raises():
    with pytest.raises(KeyError):
        reconcile([make("S1", [{"v": 1}])],
                  [Round1Slip("c1", "S9", "update", [0], fields={"v": 2})])


def test_validation_errors():
    s = make("S1", [{"v": 1}])
    # Insert without side/mark is rejected when the slips are processed.
    with pytest.raises(ValueError):
        reconcile([s], [Round1Slip("c", "S1", "insert", [0],
                                   fields={"v": 1})])
    with pytest.raises(ValueError):
        reconcile([s], [Round1Slip("c", "S1", "bogus", [0])])
    with pytest.raises(ValueError):
        reconcile([s], [],
                  [Round2Slip("c", "S1", "insert", [0], observed={"v": 1})])


def test_long_ledger_is_local_not_warehouse_wide():
    # Long run of consecutive shipment numbers, each with repeated short
    # slips and copied-lost two-choice anchors.
    n = 400
    shipments = [
        make(f"S{i:04d}", [{"v": 1}, {"v": 1}, {"v": 1}]) for i in range(n)
    ]
    r1 = []
    r2 = []
    for i in range(n):
        no = f"S{i:04d}"
        r1.append(Round1Slip(f"a{i}", no, "insert", [0, 1], side="before",
                             mark=f"m{i}", fields={"v": 9}))
        r2.append(Round2Slip(f"b{i}", no, "update", [0, 1, 2],
                             observed={"v": 1}, fields={"v": 2}))
    start = time.perf_counter()
    rows = reconcile(shipments, r1, r2)
    elapsed = time.perf_counter() - start
    assert len(rows) == n
    assert all(isinstance(row, ReconciledRow) for row in rows)
    # Every row stays ambiguous locally; never a cartesian over 400 tickets.
    assert all(row.status in {"ambiguous", "approved"} for row in rows)
    assert elapsed < 10.0


def test_arrow_field_types_and_null_states_preserved():
    types = {"v": pa.int64(), "w": pa.string()}
    shipment = Shipment("S1", "BIN", [{"v": 1, "w": "x"}, MISSING_PART], types)
    r1 = [Round1Slip("a", "S1", "update", [0], fields={"v": 5})]
    row = reconcile([shipment], r1)[0]
    scalar = parts_to_arrow(row.parts, row.field_types, row.field_order)
    assert scalar.type.value_type.field("v").type == pa.int64()
    assert scalar.type.value_type.field("w").type == pa.string()
    assert scalar.as_py() == [{"v": 5, "w": "x"}, None]

    null_list = parts_to_arrow(None, types, ["v", "w"])
    assert null_list.as_py() is None
    empty = parts_to_arrow([], types, ["v", "w"])
    assert empty.as_py() == []


def test_evidence_dataclass_carries_full_witness():
    shipment = make("S1", [{"v": 1}])
    r1 = [Round1Slip("a", "S1", "update", [0], fields={"v": 2})]
    row = reconcile([shipment], r1)[0]
    ev = row.evidence[0]
    assert isinstance(ev, BackfillEvidence)
    assert ev.round1_resolution == {"a": 0}
    assert ev.round2_resolution == {}
    assert ev.provenance == [("orig", 0)]
    assert not ev.round1_rejected and not ev.round2_rejected
