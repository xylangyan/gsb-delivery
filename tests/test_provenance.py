import pathlib
from io import StringIO
from typing import Any

import pytest

from src.json_repair import (
    JSONReturnType,
    ProvenanceRecord,
    from_file_with_provenance,
    load,
    load_with_provenance,
    loads,
    loads_with_provenance,
    repair_json,
)
from src.json_repair.provenance import Provenance, RejectedFragment


def _repair(raw: str, schema: Any | None = None, **kwargs: Any) -> tuple[JSONReturnType, ProvenanceRecord]:
    return loads_with_provenance(raw, schema=schema, **kwargs)


def test_provenance_tracks_duplicate_key_to_last_occurrence() -> None:
    raw = '{"station": 7, "station": 9}'
    value, record = _repair(raw)

    assert value == {"station": 9}
    provenance = record.provenance_for("station")
    assert provenance.origin == "source"
    assert provenance.original_key == "station"
    assert provenance.original_value == 9
    assert record.source_text_for("station") == "9"
    assert record.source_text_for("station") != "7"


def test_normal_reads_keep_duplicate_key_overwrite_behavior() -> None:
    raw = '{"station": 7, "station": 9}'
    assert loads(raw) == {"station": 9}
    assert repair_json(raw) == '{"station": 9}'


def test_salvage_evidence_points_at_selected_fragment_for_duplicate_values() -> None:
    schema = {
        "type": "object",
        "properties": {
            "station": {"type": "integer", "default": 7},
            "contract": {"type": "string"},
        },
        "required": ["contract"],
    }
    raw = '{"station": 7} {"station": 7, "contract": "B"}'
    value, record = _repair(raw, schema=schema, schema_repair_mode="salvage")

    assert value == {"station": 7, "contract": "B"}
    station = record.provenance_for("station")
    second_seven = raw.index("7", raw.index("7") + 1)
    assert station.span is not None
    assert station.span.fragment_index == 1
    assert station.span.start == second_seven
    assert station.span.start != raw.index("7")
    assert record.source_text_for("station") == "7"


def test_rejected_salvage_fragments_record_reason_and_value() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name", "age"],
    }
    raw = '{"foo": 1} {"name": "Alice", "age": 30}'
    value, record = _repair(raw, schema=schema, schema_repair_mode="salvage")

    assert value == {"name": "Alice", "age": 30}
    rejected = record.rejection_reasons()
    assert len(rejected) == 1
    assert isinstance(rejected[0], RejectedFragment)
    assert rejected[0].fragment_index == 0
    assert rejected[0].value == {"foo": 1}
    assert "name" in rejected[0].reason or "required" in rejected[0].reason
    name_span = record.provenance_for("name").span
    assert name_span is not None
    assert name_span.fragment_index == 1


def test_standard_and_salvage_share_the_same_validation_error() -> None:
    schema = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name", "age"],
    }
    raw = '{"foo": 1} {"name": "Alice", "age": 30}'

    with pytest.raises(ValueError) as exc_info:
        loads(raw, schema=schema)
    standard_error = str(exc_info.value)

    _, record = _repair(raw, schema=schema, schema_repair_mode="salvage")
    assert record.rejection_reasons()[0].reason == standard_error


def test_tuple_positions_keep_original_indices_after_tail_drop() -> None:
    schema = {
        "type": "array",
        "prefixItems": [{"type": "integer"}, {"type": "string"}],
        "items": False,
    }
    value, record = _repair('[1, "a", 2.5]', schema=schema)

    assert value == [1, "a"]
    assert record.provenance_for("[0]").original_index == 0
    assert record.provenance_for("[1]").original_index == 1
    assert record.provenance_for("[0]").schema_ref is not None
    dropped = record.dropped_items
    assert len(dropped) == 1
    assert dropped[0].original_index == 2
    dropped_span = dropped[0].span
    assert dropped_span is not None
    assert record.source_text[dropped_span.start : dropped_span.end] == "2.5"


def test_salvage_dropped_array_items_keep_source_indices() -> None:
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
        "required": ["items"],
    }
    value, record = _repair('{"items": [1, "bad", 3]}', schema=schema, schema_repair_mode="salvage")

    assert value == {"items": [1, 3]}
    assert record.provenance_for("items[0]").original_index == 0
    assert record.provenance_for("items[1]").original_index == 2
    assert record.source_text_for("items[1]") == "3"
    assert record.dropped_items[0].original_index == 1


def test_default_value_records_effective_schema_and_no_span() -> None:
    schema = {
        "type": "object",
        "properties": {
            "note": {"type": "string", "default": "n/a"},
            "count": {"type": "integer"},
        },
        "required": ["count"],
    }
    value, record = _repair('{"count": "5"}', schema=schema)

    assert value == {"note": "n/a", "count": 5}
    note = record.provenance_for("note")
    assert note.origin == "default"
    assert note.span is None
    assert note.original_value == "n/a"
    assert note.schema_ref is not None and note.schema_ref.path == "$.note"


def test_coercion_records_type_change_original_value_and_action() -> None:
    schema = {
        "type": "object",
        "properties": {"count": {"type": "integer"}},
        "required": ["count"],
    }
    value, record = _repair('{"count": "5"}', schema=schema)

    assert value == {"count": 5}
    count = record.provenance_for("count")
    assert count.origin == "source"
    assert count.type_changed is True
    assert count.original_value == "5"
    assert count.actions == ("Coerced string to integer",)
    assert record.source_text_for("count") == '"5"'


def test_const_and_enum_fills_are_labeled() -> None:
    schema = {
        "type": "object",
        "properties": {"k": {"const": "X"}, "e": {"enum": ["a", "b"]}},
        "required": ["k", "e"],
    }
    value, record = _repair("{}", schema=schema, schema_repair_mode="salvage")

    assert value == {"k": "X", "e": "a"}
    assert record.provenance_for("k").origin == "const"
    assert record.provenance_for("e").origin == "enum"


def test_string_and_descriptor_inputs_produce_matching_evidence() -> None:
    body = '{"a": 1, "b": [10, 20]}'
    string_value, string_record = _repair(body)

    prefix = "STALE PREFIX\n"
    descriptor = StringIO(prefix + body)
    descriptor.seek(len(prefix))
    descriptor_value, descriptor_record = load_with_provenance(descriptor)

    assert string_value == descriptor_value == {"a": 1, "b": [10, 20]}
    assert descriptor_record.source_text == body
    assert string_record.source_text_for("b[1]") == descriptor_record.source_text_for("b[1]") == "20"
    string_span = string_record.provenance_for("a").span
    descriptor_span = descriptor_record.provenance_for("a").span
    assert string_span is not None and descriptor_span is not None
    assert string_span.start == descriptor_span.start


def test_provenance_for_normalizes_bare_and_pathed_keys() -> None:
    _, record = _repair('{"a": {"b": 1}, "arr": [10]}')
    assert record.provenance_for("a.b").path == "$.a.b"
    assert record.provenance_for("$.arr[0]").path == "$.arr[0]"
    with pytest.raises(KeyError):
        record.provenance_for("missing")


def test_bare_scalars_keep_their_source_span() -> None:
    for raw, expected in [("42", 42), ('"hello"', "hello"), ("true", True), ("null", None)]:
        value, record = _repair(raw)
        assert value == expected
        provenance = record.provenance_for("$")
        assert provenance.origin == "source"
        assert record.source_text_for("$") == raw


def test_schema_coercion_of_bare_scalar() -> None:
    value, record = _repair('"7"', schema={"type": "integer"})
    assert value == 7
    provenance = record.provenance_for("$")
    assert provenance.type_changed is True
    assert provenance.actions == ("Coerced string to integer",)
    assert record.source_text_for("$") == '"7"'


def test_malformed_object_still_carries_source_evidence() -> None:
    value, record = _repair('{key: value, "n": 5}')
    assert value == {"key": "value", "n": 5}
    assert record.source_text_for("key") == "value"
    assert record.source_text_for("n") == "5"


def test_pydantic_model_provenance() -> None:
    pydantic = pytest.importorskip("pydantic", minversion="2")

    class Payload(pydantic.BaseModel):
        value: int
        tags: list[str] = pydantic.Field(default_factory=list)

    value, record = _repair('{"value": "42", "tags": }', schema=Payload)
    assert value == {"value": 42, "tags": []}
    assert record.provenance_for("value").type_changed is True
    assert record.provenance_for("tags").origin == "default"


def test_from_file_with_provenance(tmp_path: pathlib.Path) -> None:
    input_path = tmp_path / "input.json"
    input_path.write_text('{"a": [1, 2, 3]}', encoding="utf-8")
    value, record = from_file_with_provenance(input_path)
    assert value == {"a": [1, 2, 3]}
    assert record.source_text_for("a[1]") == "2"


def test_returns_provenance_record_type() -> None:
    _, record = _repair('{"a": 1}')
    assert isinstance(record, ProvenanceRecord)
    assert all(isinstance(entry, Provenance) for entry in record.entries.values())


def test_normal_load_api_is_unchanged() -> None:
    assert load(StringIO('{"a": 1}')) == {"a": 1}
    assert loads("[1, 2]") == [1, 2]
    assert repair_json('{"a": 1}') == '{"a": 1}'


def test_default_and_supplier_identical_values_keep_distinct_evidence() -> None:
    # First fragment carries a supplier-written 7 but fails the contract; the
    # selected fragment omits the field and gets a default 7 from its schema.
    schema = {
        "type": "object",
        "properties": {
            "station": {"type": "integer", "default": 7},
            "contract": {"type": "string"},
        },
        "required": ["contract"],
    }
    raw = '{"station": 7} {"contract": "B"}'
    value, record = _repair(raw, schema=schema, schema_repair_mode="salvage")

    assert value == {"station": 7, "contract": "B"}
    station = record.provenance_for("station")
    assert station.origin == "default"
    assert station.span is None
    assert station.schema_ref is not None and station.schema_ref.path == "$.station"
    # The supplier 7 survives only as part of the rejected first fragment.
    assert record.rejection_reasons()[0].value == {"station": 7}


def test_repaired_string_origin_for_unwrapped_containers() -> None:
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "string"}}},
        "required": ["items"],
    }
    value, record = _repair('{"items": "[\\"a\\", \\"b\\"]"}', schema=schema)
    assert value == {"items": ["a", "b"]}
    assert record.provenance_for("items").origin == "repaired_string"


def test_salvage_repaired_string_origin_for_malformed_container() -> None:
    schema = {
        "type": "object",
        "properties": {
            "summary": {
                "type": "object",
                "properties": {"verdict": {"type": "string"}, "confidence": {"type": "string"}},
                "required": ["verdict", "confidence"],
            }
        },
        "required": ["summary"],
    }
    value, record = _repair(
        '{"summary": "{verdict: malicious, confidence: high}"}',
        schema=schema,
        schema_repair_mode="salvage",
    )
    assert value == {"summary": {"verdict": "malicious", "confidence": "high"}}
    assert record.provenance_for("summary").origin == "repaired_string"


def test_multiple_dropped_items_keep_all_source_indices() -> None:
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "integer"}}},
        "required": ["items"],
    }
    value, record = _repair('{"items": [1, "bad", 3, "x", 5]}', schema=schema, schema_repair_mode="salvage")
    assert value == {"items": [1, 3, 5]}
    assert [record.provenance_for(f"items[{i}]").original_index for i in range(3)] == [0, 2, 4]
    assert [item.original_index for item in record.dropped_items] == [1, 3]


def test_nested_object_in_dropped_array_resolves_to_surviving_source() -> None:
    schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
            }
        },
        "required": ["rows"],
    }
    value, record = _repair(
        '{"rows": [{"id": 1}, {"id": "bad"}, {"id": 3}]}',
        schema=schema,
        schema_repair_mode="salvage",
    )
    assert value == {"rows": [{"id": 1}, {"id": 3}]}
    assert record.provenance_for("rows[1]").original_index == 2
    assert record.source_text_for("rows[1].id") == "3"
    assert [item.original_index for item in record.dropped_items] == [1]


def test_nested_object_preserves_original_key() -> None:
    value, record = _repair('{"outer": {"inner": 1}}')
    provenance = record.provenance_for("outer.inner")
    assert provenance.original_key == "inner"
    assert record.source_text_for("outer.inner") == "1"
    assert record.provenance_for("outer").origin == "source"


def test_paths_cover_every_output_node() -> None:
    value, record = _repair('{"a": 1, "b": [2, 3]}')
    assert value == {"a": 1, "b": [2, 3]}
    assert set(record.paths()) == {"$", "$.a", "$.b", "$.b[0]", "$.b[1]"}
