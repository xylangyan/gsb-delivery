import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from handoff_dispatch import build_schema, plan_dispatch, plan_manifest  # noqa: E402
from pyrsistent import PClass, PRecord, field  # noqa: E402


class StockPClass(PClass):
    location_no = field(type=int)
    loc1 = field(type=int, initial=10)
    loc2 = field(type=int, initial=10)
    scale_cm = field(type=int, factory=int)

    def total_ok(self):
        return self.loc1 + self.loc2 == 20

    __invariant__ = lambda stock: (stock.loc1 + stock.loc2 == 20, "total quantity changed")


class StockPRecord(PRecord):
    location_no = field(type=int)
    loc1 = field(type=int, initial=10)
    loc2 = field(type=int, initial=10)
    scale_cm = field(type=int, factory=int)

    __invariant__ = lambda stock: (stock.loc1 + stock.loc2 == 20, "total quantity changed")


INITIAL = {"location_no": 1, "scale_cm": "4321"}


def plan_kwargs(**overrides):
    kwargs = {
        "initial": INITIAL,
        "pclass_cls": StockPClass,
        "precord_cls": StockPRecord,
    }
    kwargs.update(overrides)
    return kwargs


def test_shared_plan_skips_invalid_temporary_write_and_batches_locations():
    result = plan_dispatch(
        [
            {"id": "R7", "field": "location_no", "value": "TEMP-TEXT", "archivable": True},
            {"id": "R12", "field": "location_no", "value": 4321},
            {"id": "R18", "field": "loc1", "value": 7, "group": "MOVE"},
            {"id": "R19", "field": "loc2", "value": 13, "group": "MOVE"},
        ],
        checkpoints=[
            {
                "id": "APPROVAL",
                "after": "R19",
                "signoffs": {
                    "location_no": {"revision": "R12", "raw_type": "int"},
                    "scale_cm": {"revision": None, "raw_type": "str"},
                },
            }
        ],
        commit_groups=[["R18", "R19"]],
        **plan_kwargs(),
    )

    assert result["feasible"] is True
    assert result["sent_revisions"] == ["R12", "R18", "R19"]
    assert result["write_count"] == 3
    assert result["commit_count"] == 1
    assert result["commits"][0]["revisions"] == ["R12", "R18", "R19"]
    assert [item["revision"] for item in result["archived_only"]] == ["R7"]
    assert result["archived_only"][0]["reason"] == "record_entry_rejects_write"
    assert result["verification"]["performed"] is True
    assert all(item["ok"] for item in result["verification"]["checkpoints"])


def test_mandatory_invalid_write_is_a_common_path_blocker():
    result = plan_dispatch(
        [
            {"id": "R7", "field": "location_no", "value": "TEMP-TEXT"},
            {"id": "R12", "field": "location_no", "value": 4321},
        ],
        **plan_kwargs(),
    )

    assert result["feasible"] is False
    blocker = next(item for item in result["blockers"] if item["kind"] == "mandatory_write_field_error")
    assert blocker["revision"] == "R7"
    assert "PTypeError" in blocker["error"]


def test_precord_sticky_invariant_error_is_avoided_only_by_archiving_write():
    class QuantityPRecord(PRecord):
        quantity = field(invariant=lambda value: (value >= 0, "negative"))

    class QuantityPClass(PClass):
        quantity = field(invariant=lambda value: (value >= 0, "negative"))

    optional = plan_dispatch(
        [
            {"id": "R1", "field": "quantity", "value": -1, "archivable": True},
            {"id": "R2", "field": "quantity", "value": 2},
        ],
        pclass_cls=QuantityPClass,
        precord_cls=QuantityPRecord,
    )
    assert optional["feasible"] is True
    assert optional["sent_revisions"] == ["R2"]
    assert optional["archived_only"][0]["reason"] == "record_entry_rejects_write"

    mandatory = plan_dispatch(
        [
            {"id": "R1", "field": "quantity", "value": -1},
            {"id": "R2", "field": "quantity", "value": 2},
        ],
        pclass_cls=QuantityPClass,
        precord_cls=QuantityPRecord,
    )
    assert mandatory["feasible"] is False
    assert mandatory["blockers"][0]["kind"] == "mandatory_write_field_error"


def test_signoff_requiring_superseded_revision_is_unreachable():
    result = plan_dispatch(
        [
            {"id": "R1", "field": "location_no", "value": 10, "archivable": True},
            {"id": "R2", "field": "location_no", "value": 20},
        ],
        checkpoints=[
            {
                "id": "EARLY",
                "after": "R2",
                "signoffs": {"location_no": {"revision": "R1"}},
            }
        ],
        **plan_kwargs(),
    )

    assert result["feasible"] is False
    blocker = next(item for item in result["blockers"] if item["kind"] == "mandatory_revision_supersedes_signoff")
    assert blocker["revision"] == "R2"
    assert blocker["checkpoint"] == "EARLY"


def test_signoff_inside_atomic_batch_is_rejected():
    result = plan_dispatch(
        [
            {"id": "R18", "field": "loc1", "value": 7, "group": "MOVE"},
            {"id": "R19", "field": "loc2", "value": 13, "group": "MOVE"},
        ],
        checkpoints=[{"id": "MID", "after": "R18", "signoffs": {}}],
        **plan_kwargs(),
    )

    assert result["feasible"] is False
    blocker = next(item for item in result["blockers"] if item["kind"] == "commit_group_split_by_signoff")
    assert blocker["checkpoint"] == "MID"


def test_equal_value_does_not_change_provenance():
    result = plan_dispatch(
        [{"id": "R12", "field": "location_no", "value": 4321}],
        checkpoints=[
            {
                "id": "APPROVAL",
                "after": "R12",
                "signoffs": {
                    "location_no": {"revision": "R12", "raw_type": "int"},
                    "scale_cm": {"revision": None, "raw_type": "str"},
                },
            }
        ],
        **plan_kwargs(),
    )

    assert result["feasible"] is True
    scale = next(item for item in result["verification"]["checkpoints"] if item["field"] == "scale_cm")
    assert scale["actual_revision"] is None
    assert scale["actual_raw_type"] == "str"
    assert scale["missing"] is False


def test_long_sheet_compresses_repeated_hot_field_overwrites():
    field_count = 10
    cycles_per_checkpoint = 10
    checkpoint_count = 5
    revisions = []
    checkpoints = []

    for block in range(checkpoint_count):
        for cycle in range(cycles_per_checkpoint):
            for field_index in range(field_count):
                rid = f"R{len(revisions) + 1}"
                revisions.append(
                    {
                        "id": rid,
                        "field": f"f{field_index}",
                        "value": block * cycles_per_checkpoint + cycle,
                        "archivable": True,
                    }
                )
        after = revisions[-1]["id"]
        signoffs = {}
        for field_index in range(field_count):
            adopted_index = len(revisions) - field_count + field_index
            signoffs[f"f{field_index}"] = {"revision": revisions[adopted_index]["id"], "raw_type": "int"}
        checkpoints.append({"id": f"C{block + 1}", "after": after, "signoffs": signoffs})

    field_specs = {f"f{index}": {"type": "int", "initial": 0} for index in range(field_count)}
    long_pclass = build_schema({"name": "LongPClass", "fields": field_specs}, PClass)
    long_precord = build_schema({"name": "LongPRecord", "fields": field_specs}, PRecord)

    result = plan_dispatch(
        revisions,
        checkpoints=checkpoints,
        initial={},
        pclass_cls=long_pclass,
        precord_cls=long_precord,
    )

    assert result["feasible"] is True, result["blockers"]
    assert len(revisions) == 500
    assert result["write_count"] == checkpoint_count * field_count
    assert result["commit_count"] == checkpoint_count


def test_small_sheet_exhaustive_search_recovers_required_balancing_write():
    class BalancePClass(PClass):
        a = field(type=int, initial=0)
        b = field(type=int, initial=0)

        __invariant__ = lambda stock: (stock.a + stock.b == 0, "total")

    class BalancePRecord(PRecord):
        a = field(type=int, initial=0)
        b = field(type=int, initial=0)

        __invariant__ = lambda stock: (stock.a + stock.b == 0, "total")

    result = plan_dispatch(
        [
            {"id": "R1", "field": "a", "value": -3},
            {"id": "R2", "field": "b", "value": 3, "archivable": True},
        ],
        pclass_cls=BalancePClass,
        precord_cls=BalancePRecord,
    )

    assert result["feasible"] is True, result["blockers"]
    assert result["sent_revisions"] == ["R1", "R2"]
    assert result["write_count"] == 2
    assert result["optimization"]["used"] is True


def test_json_manifest_builds_both_schemas():
    result = plan_manifest(
        {
            "initial": {"code": "7", "loc1": 10, "loc2": 10},
            "schemas": {
                "shared": {
                    "name": "JsonTicket",
                    "fields": {
                        "code": {"type": "int", "factory": "int"},
                        "loc1": {"type": "int"},
                        "loc2": {"type": "int"},
                    },
                    "invariants": [{"kind": "sum_equals", "fields": ["loc1", "loc2"], "value": 20}],
                }
            },
            "revisions": [
                {"id": "R1", "field": "code", "value": "bad", "archivable": True},
                {"id": "R2", "field": "code", "value": "8"},
                {"id": "R3", "field": "loc1", "value": 7, "group": "move"},
                {"id": "R4", "field": "loc2", "value": 13, "group": "move"},
            ],
            "checkpoints": [{"id": "DONE", "after": "R4", "signoffs": {"code": {"revision": "R2", "raw_type": "str"}}}],
        }
    )

    assert result["feasible"] is True, result["blockers"]
    assert result["sent_revisions"] == ["R2", "R3", "R4"]
    assert result["archived_only"][0]["revision"] == "R1"
