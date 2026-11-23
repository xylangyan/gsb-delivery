import datetime
from io import BytesIO

import fastavro
from fastavro._write_py import Writer
from fastavro.revision import (
    Approval,
    _collect_named,
    _decode_span,
    _parse_container,
    preview,
    revise,
)
from fastavro.schema import parse_schema

LEGACY_PAYLOAD = {
    "type": "record",
    "name": "LegacyPayload",
    "namespace": "com.acme",
    "fields": [{"name": "v", "type": "string"}],
}
MODERN_PAYLOAD = {
    "type": "record",
    "name": "ModernPayload",
    "namespace": "com.acme",
    "fields": [{"name": "v", "type": "string"}],
}

# first writer generation
WRITER_V1 = {
    "type": "record",
    "name": "Account",
    "namespace": "com.acme",
    "fields": [
        {
            "name": "status",
            "type": {
                "type": "enum",
                "name": "Status",
                "symbols": ["NEW", "OPEN", "CLOSED", "ARCHIVED"],
            },
        },
        {"name": "payload", "type": [LEGACY_PAYLOAD, MODERN_PAYLOAD]},
        {"name": "internal_note", "type": "string"},
        {"name": "created", "type": "long"},
    ],
}

# second writer generation: extra enum symbol, logical-typed timestamp,
# internal_note dropped, priority added
WRITER_V2 = {
    "type": "record",
    "name": "Account",
    "namespace": "com.acme",
    "fields": [
        {
            "name": "status",
            "type": {
                "type": "enum",
                "name": "Status",
                "symbols": ["NEW", "OPEN", "CLOSED", "ARCHIVED", "PURGED"],
            },
        },
        {"name": "payload", "type": [LEGACY_PAYLOAD, MODERN_PAYLOAD]},
        {
            "name": "created",
            "type": {"type": "long", "logicalType": "timestamp-millis"},
        },
        {"name": "priority", "type": "int", "default": 0},
    ],
}

# reader schema used for the external preview
READER = {
    "type": "record",
    "name": "Account",
    "namespace": "com.acme",
    "fields": [
        {
            "name": "status",
            "type": {
                "type": "enum",
                "name": "Status",
                "symbols": ["NEW", "OPEN", "CLOSED"],
                "default": "NEW",
            },
        },
        {"name": "payload", "type": [LEGACY_PAYLOAD, MODERN_PAYLOAD]},
        {
            "name": "created",
            "type": {"type": "long", "logicalType": "timestamp-millis"},
        },
        {"name": "reviewed", "type": "boolean", "default": False},
    ],
}

TS = datetime.datetime(2024, 5, 6, 7, 8, 9, tzinfo=datetime.timezone.utc)


def v1_record(status, branch, v, note, created):
    return {
        "status": status,
        "payload": (f"com.acme.{branch}", {"v": v}),
        "internal_note": note,
        "created": created,
    }


def v2_record(status, branch, v, created, priority):
    return {
        "status": status,
        "payload": (f"com.acme.{branch}", {"v": v}),
        "created": created,
        "priority": priority,
    }


def build_file(path, schema, blocks, codec="null"):
    with open(path, "wb") as fo:
        writer = Writer(fo, schema, codec=codec)
        for block in blocks:
            for record in block:
                writer.write(record)
            writer.dump()
        writer.flush()
    return str(path)


def record_spans(container, schema, block_index):
    parsed = parse_schema(schema)
    named = _collect_named(parsed)
    block = container.blocks[block_index]
    spans = []
    pos = 0
    for _ in range(block.count):
        span, pos = _decode_span(parsed, block.decoded, pos, named)
        spans.append(span)
    return spans


def test_enum_fallback_distinguished_from_actual_default_symbol(tmp_path):
    # block 0 writes a symbol the reader enum does not know; block 1 really
    # wrote the old symbol that corresponds to the reader's default item
    path = build_file(
        str(tmp_path / "v1.avro"),
        WRITER_V1,
        [
            [v1_record("ARCHIVED", "LegacyPayload", "a", "n0", 100)],
            [v1_record("NEW", "ModernPayload", "b", "n1", 200)],
        ],
    )
    result = preview(path, READER)
    fallen_back, actually_written = result.at(0, 0), result.at(1, 0)

    # both preview as the reader default symbol ...
    assert fallen_back.data["status"] == "NEW"
    assert actually_written.data["status"] == "NEW"
    # ... but the provenance tells them apart
    prov_fallback = fallen_back.fields["status"]
    prov_actual = actually_written.fields["status"]
    assert prov_fallback.enum_fallback is True
    assert prov_fallback.written_symbol == "ARCHIVED"
    assert prov_actual.enum_fallback is False
    assert prov_actual.written_symbol == "NEW"
    assert prov_fallback.source == prov_actual.source == "written"


def test_union_branch_provenance_and_single_branch_update(tmp_path):
    # two records whose union values are equal as dicts but were written on
    # different branches
    path = build_file(
        str(tmp_path / "unions.avro"),
        WRITER_V1,
        [
            [
                v1_record("OPEN", "LegacyPayload", "same", "x", 1),
                v1_record("OPEN", "ModernPayload", "same", "y", 2),
            ]
        ],
    )
    result = preview(path, READER)
    first, second = result.at(0, 0), result.at(0, 1)
    assert first.data["payload"] == second.data["payload"] == {"v": "same"}
    # the actual branch comes from the binary, not from guessing the dict
    assert first.fields["payload"].branch_index == 0
    assert first.fields["payload"].branch_name == "com.acme.LegacyPayload"
    assert second.fields["payload"].branch_index == 1
    assert second.fields["payload"].branch_name == "com.acme.ModernPayload"
    assert first.fields["payload"].reader_branch_index == 0
    assert second.fields["payload"].reader_branch_index == 1

    # later, only the record on the ModernPayload branch is updated
    out = str(tmp_path / "unions_out.avro")
    report = revise(path, out, [Approval(0, 1, ("payload", "v"), "changed")], READER)
    assert [entry.verified for entry in report.applied] == [True]

    read_first = report.readback.at(0, 0)
    read_second = report.readback.at(0, 1)
    assert read_first.data["payload"] == {"v": "same"}
    assert read_first.fields["payload"].branch_name == "com.acme.LegacyPayload"
    assert read_second.data["payload"] == {"v": "changed"}
    assert read_second.fields["payload"].branch_name == "com.acme.ModernPayload"

    # the untouched record kept its exact bytes in the decompressed payload
    original = _parse_container(open(path, "rb").read())
    revised = _parse_container(open(out, "rb").read())
    original_spans = record_spans(original, WRITER_V1, 0)
    record0_end = original_spans[0].end
    assert (
        revised.blocks[0].decoded[:record0_end]
        == original.blocks[0].decoded[:record0_end]
    )


def test_reader_supplied_and_pruned_fields_are_distinguished(tmp_path):
    path = build_file(
        str(tmp_path / "v1.avro"),
        WRITER_V1,
        [[v1_record("OPEN", "LegacyPayload", "a", "secret", 100)]],
    )
    result = preview(path, READER)
    record = result.at(0, 0)
    # reader-supplied field shows up with its default and is marked as such
    assert record.data["reviewed"] is False
    assert record.fields["reviewed"].source == "reader_default"
    # pruned field is not projected but still reported from the source
    assert "internal_note" not in record.data
    assert record.pruned["internal_note"] == "secret"

    out = str(tmp_path / "out.avro")
    report = revise(
        path,
        out,
        [
            Approval(0, 0, ("reviewed",), True),
            Approval(0, 0, ("internal_note",), "redacted"),
            Approval(0, 0, ("no_such_field",), 1),
        ],
        READER,
    )
    reasons = {r.approval.path: r.reason for r in report.rejected}
    assert reasons[("reviewed",)] == "reader_supplied"
    assert reasons[("no_such_field",)] == "unknown_field"

    applied = {entry.approval.path: entry for entry in report.applied}
    assert applied[("internal_note",)].note == "pruned_source"
    assert applied[("internal_note",)].verified is True

    # the reader preview still hides the pruned field but reports its new value
    revised_record = report.readback.at(0, 0)
    assert "internal_note" not in revised_record.data
    assert revised_record.pruned["internal_note"] == "redacted"
    # and the file stays readable under the original writer schema
    with open(out, "rb") as fo:
        records = list(fastavro.reader(fo))
    assert records[0]["internal_note"] == "redacted"


def test_untouched_blocks_copied_byte_for_byte(tmp_path):
    path = build_file(
        str(tmp_path / "deflate.avro"),
        WRITER_V1,
        [
            [v1_record("NEW", "LegacyPayload", "a", "n0", 1)],
            [v1_record("OPEN", "ModernPayload", "b", "n1", 2)],
            [v1_record("CLOSED", "LegacyPayload", "c", "n2", 3)],
        ],
        codec="deflate",
    )
    out = str(tmp_path / "deflate_out.avro")
    report = revise(path, out, [Approval(1, 0, ("status",), "CLOSED")], READER)

    original = _parse_container(open(path, "rb").read())
    revised = _parse_container(open(out, "rb").read())
    # header copied verbatim
    assert original.data[: original.header_end] == revised.data[: revised.header_end]
    # untouched compressed blocks copied byte for byte
    for index in (0, 2):
        before = original.blocks[index]
        after = revised.blocks[index]
        assert (
            original.data[before.start : before.end]
            == revised.data[after.start : after.end]
        )
    assert {b.index: b.action for b in report.blocks} == {
        0: "copied",
        1: "rewritten",
        2: "copied",
    }
    assert report.applied[0].verified is True


def test_rewritten_block_preserves_unmodified_bytes(tmp_path):
    path = build_file(
        str(tmp_path / "deflate.avro"),
        WRITER_V1,
        [
            [
                v1_record("OPEN", "LegacyPayload", "keep-me", "unprojected", 42),
                v1_record("NEW", "ModernPayload", "other", "note", 43),
            ]
        ],
        codec="deflate",
    )
    out = str(tmp_path / "deflate_out.avro")
    revise(path, out, [Approval(0, 0, ("status",), "CLOSED")], READER)

    original = _parse_container(open(path, "rb").read())
    revised = _parse_container(open(out, "rb").read())
    before = original.blocks[0].decoded
    after = revised.blocks[0].decoded
    status_span = record_spans(original, WRITER_V1, 0)[0].fields["status"]
    delta = len(after) - len(before)
    # every byte before the edited field ...
    assert after[: status_span.start] == before[: status_span.start]
    # ... and every byte after it (untouched fields incl. the unprojected
    # internal_note, and the whole second record) is preserved exactly
    assert after[status_span.end + delta :] == before[status_span.end :]
    # the edited field now encodes the approved value
    named = _collect_named(parse_schema(WRITER_V1))
    new_span, _ = _decode_span(
        parse_schema(WRITER_V1)["fields"][0]["type"],
        after,
        status_span.start,
        named,
    )
    assert new_span.value == "CLOSED"


def test_unencodable_logical_value_rejects_whole_block(tmp_path):
    path = build_file(
        str(tmp_path / "v1.avro"),
        WRITER_V1,
        [
            [
                v1_record("OPEN", "LegacyPayload", "a", "n0", 1),
                v1_record("OPEN", "ModernPayload", "b", "n1", 2),
            ],
            [v1_record("NEW", "LegacyPayload", "c", "n2", 3)],
        ],
        codec="deflate",
    )
    out = str(tmp_path / "out.avro")
    report = revise(
        path,
        out,
        [
            Approval(0, 0, ("status",), "CLOSED"),  # valid on its own ...
            Approval(0, 1, ("created",), TS),  # ... but the v1 writer schema
            # has a plain long here and cannot encode a datetime
            Approval(1, 0, ("status",), "OPEN"),
        ],
        READER,
    )
    # the whole block 0 change is rejected, including the otherwise valid edit
    rejected = {r.approval.path: r.reason for r in report.rejected}
    assert rejected[("created",)] == "unencodable_value"
    assert rejected[("status",)] == "unencodable_value"
    assert {b.index: b.action for b in report.blocks} == {
        0: "rejected",
        1: "rewritten",
    }
    # block 0 is copied verbatim; block 1 was still revised
    original = _parse_container(open(path, "rb").read())
    revised = _parse_container(open(out, "rb").read())
    assert (
        original.data[original.blocks[0].start : original.blocks[0].end]
        == revised.data[revised.blocks[0].start : revised.blocks[0].end]
    )
    assert [entry.approval.block for entry in report.applied] == [1]
    assert report.applied[0].verified is True
    with open(out, "rb") as fo:
        records = list(fastavro.reader(fo))
    assert [r["status"] for r in records] == ["OPEN", "OPEN", "OPEN"]
    assert records[2]["created"] == 3


def test_two_writer_generations_share_one_reader_preview(tmp_path):
    v1_path = build_file(
        str(tmp_path / "gen1.avro"),
        WRITER_V1,
        [[v1_record("ARCHIVED", "LegacyPayload", "a", "note", 1_700_000_000_000)]],
    )
    v2_path = build_file(
        str(tmp_path / "gen2.avro"),
        WRITER_V2,
        [[v2_record("PURGED", "ModernPayload", "b", TS, 7)]],
    )

    gen1 = preview(v1_path, READER).at(0, 0)
    gen2 = preview(v2_path, READER).at(0, 0)
    # both generations fall back to the reader enum default, with the actual
    # written symbol reported
    assert gen1.data["status"] == gen2.data["status"] == "NEW"
    assert gen1.fields["status"].written_symbol == "ARCHIVED"
    assert gen2.fields["status"].written_symbol == "PURGED"
    # logical-typed preview follows what each writer schema can express
    assert isinstance(gen1.data["created"], int)
    assert gen2.data["created"] == TS
    # generation-specific source fields show up as pruned
    assert gen1.pruned["internal_note"] == "note"
    assert gen2.pruned["priority"] == 7

    # the logical-typed revision is encodable by the second generation ...
    new_ts = datetime.datetime(2025, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc)
    v2_out = str(tmp_path / "gen2_out.avro")
    report = revise(v2_path, v2_out, [Approval(0, 0, ("created",), new_ts)], READER)
    assert report.applied[0].verified is True
    assert report.readback.at(0, 0).data["created"] == new_ts

    # ... but rejected as a whole-block change for the first generation
    v1_out = str(tmp_path / "gen1_out.avro")
    report = revise(v1_path, v1_out, [Approval(0, 0, ("created",), new_ts)], READER)
    assert report.blocks[0].action == "rejected"
    assert report.rejected[0].reason == "unencodable_value"


def test_readback_anchors_new_coordinates_and_approval_scope(tmp_path):
    path = build_file(
        str(tmp_path / "v1.avro"),
        WRITER_V1,
        [
            [v1_record("NEW", "LegacyPayload", "a", "n0", 1)],
            [v1_record("OPEN", "ModernPayload", "b", "n1", 2)],
            [v1_record("CLOSED", "LegacyPayload", "c", "n2", 3)],
        ],
    )
    out = str(tmp_path / "out.avro")
    approvals = [
        Approval(0, 0, ("status",), "OPEN"),
        Approval(2, 0, ("payload", "v"), "revised"),
    ]
    report = revise(path, out, approvals, READER)

    assert len(report.applied) == 2
    for entry in report.applied:
        # each applied revision points back to its approval scope ...
        assert entry.approval in approvals
        # ... and to the coordinates in the new file
        assert (entry.new_block, entry.new_record) == (
            entry.approval.block,
            entry.approval.record,
        )
        assert entry.verified is True
        # the read-back preview at those coordinates shows the approved value
        preview_record = report.readback.at(entry.new_block, entry.new_record)
        value = preview_record.data
        for segment in entry.approval.path:
            value = value[segment]
        assert value == entry.approval.value

    by_coordinate = report.applied_at(2, 0)
    assert len(by_coordinate) == 1
    assert by_coordinate[0].approval.path == ("payload", "v")


def test_output_file_is_readable_with_original_writer_schema(tmp_path):
    path = build_file(
        str(tmp_path / "v1.avro"),
        WRITER_V1,
        [
            [v1_record("ARCHIVED", "LegacyPayload", "a", "n0", 10)],
            [v1_record("NEW", "ModernPayload", "b", "n1", 20)],
        ],
        codec="deflate",
    )
    out = str(tmp_path / "out.avro")
    revise(
        path,
        out,
        [
            Approval(0, 0, ("status",), "ARCHIVED"),  # unknown to the reader,
            # but a valid source symbol
            Approval(1, 0, ("internal_note",), "updated"),
        ],
        READER,
    )
    with open(out, "rb") as fo:
        records = list(fastavro.reader(fo))  # no reader schema: writer schema
    assert [r["status"] for r in records] == ["ARCHIVED", "NEW"]
    assert records[1]["internal_note"] == "updated"
    # the preview still flags the first record as a fallback to the default
    result = preview(out, READER)
    assert result.at(0, 0).fields["status"].enum_fallback is True
    assert result.at(0, 0).fields["status"].written_symbol == "ARCHIVED"


def test_approval_targets_are_selected_by_block_and_record(tmp_path):
    path = build_file(
        str(tmp_path / "v1.avro"),
        WRITER_V1,
        [
            [v1_record("NEW", "LegacyPayload", "a", "n0", 1)],
            [
                v1_record("NEW", "LegacyPayload", "b", "n1", 2),
                v1_record("NEW", "LegacyPayload", "c", "n2", 3),
            ],
        ],
    )
    out = str(tmp_path / "out.avro")
    # same field, same value approved for two different coordinates
    report = revise(
        path,
        out,
        [
            Approval(0, 0, ("status",), "CLOSED"),
            Approval(1, 1, ("status",), "CLOSED"),
        ],
        READER,
    )
    assert {(e.new_block, e.new_record) for e in report.applied} == {(0, 0), (1, 1)}
    with open(out, "rb") as fo:
        records = list(fastavro.reader(fo))
    assert [r["status"] for r in records] == ["CLOSED", "NEW", "CLOSED"]
    # out-of-range coordinates are rejected, not silently applied elsewhere
    report = revise(
        path,
        str(tmp_path / "out2.avro"),
        [Approval(5, 0, ("status",), "CLOSED"), Approval(1, 9, ("status",), "CLOSED")],
        READER,
    )
    assert {r.reason for r in report.rejected} == {"no_such_block", "no_such_record"}
