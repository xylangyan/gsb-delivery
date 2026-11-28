from __future__ import annotations

import base64

import pytest

from python_multipart.exceptions import ProvenanceDecodeError, ProvenanceError
from python_multipart.provenance import (
    AUDITABLE,
    BASE64,
    BINARY,
    FAILED,
    INNER,
    OUTER,
    PROVISIONAL,
    BitSource,
    NestedFormParser,
    PartStatus,
    QUOTED_PRINTABLE,
    SourceWindow,
)


def _part(name: bytes, data: bytes, *, filename: bytes | None = None, encoding: str | None = None) -> bytes:
    disposition = b'Content-Disposition: form-data; name="' + name + b'"'
    if filename is not None:
        disposition += b'; filename="' + filename + b'"'
    headers = [disposition]
    if encoding is not None:
        headers.append(b"Content-Transfer-Encoding: " + encoding.encode("latin-1"))
    if filename is not None:
        headers.append(b"Content-Type: application/octet-stream")
    return b"--innerb\r\n" + b"\r\n".join(headers) + b"\r\n\r\n" + data + b"\r\n"


def _inner_document(*parts: bytes) -> bytes:
    return b"".join(parts) + b"--innerb--\r\n"


def _outer_message(*fields: bytes) -> bytes:
    return b"".join(fields) + b"--outerb--\r\n"


def _outer_field(name: bytes, value: bytes, *, filename: bytes | None = None, cte: bytes | None = None) -> bytes:
    disposition = b'Content-Disposition: form-data; name="' + name + b'"'
    if filename is not None:
        disposition += b'; filename="' + filename + b'"'
    headers = [disposition]
    if cte is not None:
        headers.append(b"Content-Transfer-Encoding: " + cte)
    return b"--outerb\r\n" + b"\r\n".join(headers) + b"\r\n\r\n" + value + b"\r\n"


def _parse(
    body: bytes,
    windows: list[SourceWindow],
    *,
    chunks: int | None = None,
    detailed: tuple[int, ...] = (0,),
):
    outer_fields: list = []
    outer_files: list = []
    inner_fields: list = []
    inner_files: list = []
    parser = NestedFormParser(
        b"outerb",
        windows,
        on_field=outer_fields.append,
        on_file=outer_files.append,
        on_inner_field=inner_fields.append,
        on_inner_file=inner_files.append,
        detailed_windows=detailed,
    )
    if chunks is None:
        parser.write(body)
    else:
        for start in range(0, len(body), chunks):
            parser.write(body[start : start + chunks])
    return parser, outer_fields, outer_files, inner_fields, inner_files


def test_plain_nested_binary_delivery_and_identities() -> None:
    document = _inner_document(
        _part(b"a", b"foo"),
        _part(b"doc", b"bar", filename=b"f.txt"),
    )
    body = _outer_message(
        _outer_field(b"plain", b"hello"),
        _outer_field(b"nested", document),
    )
    parser, outer_fields, _outer_files, inner_fields, inner_files = _parse(
        body, [SourceWindow(b"nested", b"innerb")]
    )
    parser.finalize()

    assert [(f.field_name, f.value) for f in outer_fields] == [
        (b"plain", b"hello"),
        (b"nested", document),
    ]
    assert [(f.field_name, f.value) for f in inner_fields] == [(b"a", b"foo")]
    assert [(f.field_name, f.file_name) for f in inner_files] == [(b"doc", b"f.txt")]
    assert inner_files[0].file_object.getvalue() == b"bar"
    assert not parser.failures


def test_duplicate_names_and_filenames_disambiguated_by_ordinal() -> None:
    document = _inner_document(
        _part(b"same", b"one"),
        _part(b"same", b"two", filename=b"dup.bin"),
        _part(b"same", b"three", filename=b"dup.bin"),
    )
    body = _outer_message(_outer_field(b"nested", document))
    parser, _of, _ofl, inner_fields, inner_files = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()

    assert inner_fields[0].value == b"one"
    assert [f.file_object.getvalue() for f in inner_files] == [b"two", b"three"]
    ledger = parser.ledger
    assert ledger.part_info(INNER, 0, 1).name == b"same"
    assert ledger.part_info(INNER, 0, 2).filename == b"dup.bin"
    assert ledger.part_info(INNER, 0, 3).filename == b"dup.bin"
    assert ledger.part_length(INNER, 0, 2) == 3
    assert ledger.part_length(INNER, 0, 3) == 5


def test_origins_round_trip_through_binary_layers() -> None:
    document = _inner_document(_part(b"a", b"abc"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    ledger = parser.ledger

    for offset, byte in enumerate(b"abc"):
        origins = ledger.origins_for_target(INNER, 0, 1, offset)
        assert origins
        assert {body[position] for position in origins} == {byte}
        for position in origins:
            refs = ledger.targets_for_origin(position)
            assert any(ref.layer == INNER and ref.part == 1 and ref.offset == offset for ref in refs)


def test_reverse_lookup_lists_every_relation() -> None:
    # An outer base64 symbol contributes bits to up to two decoded outer bytes,
    # so one original upload byte can feed several delivered targets.
    document = _inner_document(_part(b"a", b"abcdef"))
    encoded = base64.b64encode(document)
    body = _outer_message(_outer_field(b"nested", encoded, cte=b"base64"))
    parser, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    ledger = parser.ledger
    outer_part = next(key.part for key, _s, info in ledger.parts() if key.layer == OUTER)

    symbol_origin = body.index(encoded) + 1  # second symbol of the first quad
    refs = ledger.targets_for_origin(symbol_origin)
    outer_offsets = {ref.offset for ref in refs if ref.layer == OUTER and ref.part == outer_part}
    # Quad index 1 feeds both decoded byte 0 and decoded byte 1.
    assert {0, 1} <= outer_offsets
    # Round trip: each listed target traces back to the same original symbol.
    for offset in outer_offsets:
        assert symbol_origin in ledger.origins_for_target(OUTER, 0, outer_part, offset)


def test_outer_base64_part_records_bit_masks() -> None:
    document = _inner_document(_part(b"a", b"hello"))
    encoded = base64.b64encode(document)
    body = _outer_message(_outer_field(b"nested", encoded, cte=b"base64"))
    parser, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    ledger = parser.ledger
    outer_part = next(key.part for key, _s, info in ledger.parts() if key.layer == OUTER and info.name == b"nested")

    first_sources = ledger.target_sources(OUTER, 0, outer_part, 0)
    # A decoded base64 byte is always assembled from at most two symbols.
    assert 1 <= len(first_sources) <= 2
    assert all(isinstance(source, BitSource) for source in first_sources)
    assert {source.mask for source in first_sources} <= {0xFC, 0x03, 0xF0, 0x0F, 0xC0, 0x3F, 0xFF}
    # Padding symbols never produce output and therefore never appear.
    padding_positions = [index for index, char in enumerate(encoded) if char == ord("=")]
    if padding_positions:
        stream_origin = body.index(encoded)
        for relative in padding_positions:
            assert ledger.targets_for_origin(stream_origin + relative) == ()


def test_inner_base64_part_decodes_and_tracks_bits() -> None:
    document = _inner_document(
        _part(b"a", b"aGVsbG8=", encoding="base64"),
    )
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_of, inner_fields, _ifl = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    assert inner_fields[0].value == b"hello"
    ledger = parser.ledger
    # The first decoded byte combines two base64 symbols with split masks.
    sources = ledger.target_sources(INNER, 0, 1, 0)
    assert len(sources) == 2
    assert sources[0].mask & sources[1].mask == 0


def test_document_level_base64_encoding() -> None:
    document = _inner_document(_part(b"a", b"wxyz"))
    encoded = base64.b64encode(document)
    body = _outer_message(_outer_field(b"nested", encoded))
    parser, *_of, inner_fields, _ifl = _parse(
        body, [SourceWindow(b"nested", b"innerb", encoding=BASE64)]
    )
    parser.finalize()
    assert inner_fields[0].value == b"wxyz"
    ledger = parser.ledger
    for offset in range(4):
        assert ledger.origins_for_target(INNER, 0, 1, offset)


def test_quoted_printable_soft_break_and_hex_masks() -> None:
    qp_payload = b"a=\r\nb=41"
    document = _inner_document(_part(b"q", qp_payload, encoding="quoted-printable"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_of, inner_fields, _ifl = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    assert inner_fields[0].value == b"abA"
    ledger = parser.ledger
    hex_sources = ledger.target_sources(INNER, 0, 1, 2)
    assert len(hex_sources) == 2
    assert hex_sources[0].mask == 0xF0
    assert hex_sources[1].mask == 0x0F


def test_quoted_printable_document_encoding() -> None:
    import binascii

    document = _inner_document(_part(b"q", b"x=3Dy"))
    encoded = binascii.b2a_qp(document)
    body = _outer_message(_outer_field(b"nested", encoded))
    parser, *_of, inner_fields, _ifl = _parse(
        body, [SourceWindow(b"nested", b"innerb", encoding=QUOTED_PRINTABLE)]
    )
    parser.finalize()
    assert not parser.failures, parser.failures
    # The inner payload itself is still literal quoted-printable text here.
    assert inner_fields[0].value == b"x=3Dy"


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 13, 64])
def test_provenance_is_chunk_invariant(chunk_size: int) -> None:
    document = _inner_document(
        _part(b"a", b"xx\r\n--innerXyy\r\nzzz"),
        _part(b"b", b"second", filename=b"f.bin"),
    )
    body = _outer_message(_outer_field(b"nested", document))

    whole, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")], chunks=None)
    whole.finalize()
    split, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")], chunks=chunk_size)
    split.finalize()

    assert split.ledger is not whole.ledger
    for key, status, info in whole.ledger.parts():
        assert split.ledger.status(key.layer, key.window, key.part) is status
        assert split.ledger.part_info(key.layer, key.window, key.part) == info
    for key, _status, _info in whole.ledger.parts():
        length = whole.ledger.part_length(key.layer, key.window, key.part)
        for offset in range(length):
            assert split.ledger.target_sources(key.layer, key.window, key.part, offset) == whole.ledger.target_sources(
                key.layer, key.window, key.part, offset
            )
            assert split.ledger.origins_for_target(
                key.layer, key.window, key.part, offset
            ) == whole.ledger.origins_for_target(key.layer, key.window, key.part, offset)
    for position in range(len(body)):
        assert split.ledger.targets_for_origin(position) == whole.ledger.targets_for_origin(position)


def test_provisional_parts_remain_queryable_when_outer_never_closes() -> None:
    document = _inner_document(_part(b"a", b"foo"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, _of, _ofl, inner_fields, _ifl = _parse(body[:-8], [SourceWindow(b"nested", b"innerb")])

    # The complete inner part was already delivered provisionally during write.
    assert [field.value for field in inner_fields] == [b"foo"]
    with pytest.raises(ProvenanceError):
        parser.finalize()

    ledger = parser.ledger
    assert ledger.status(INNER, 0, 1) is PROVISIONAL
    assert ledger.closed is False
    # Staged provenance is still inspectable.
    assert ledger.origins_for_target(INNER, 0, 1, 0)


def test_successful_run_settles_to_auditable() -> None:
    document = _inner_document(_part(b"a", b"foo"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")])
    assert parser.ledger.status(INNER, 0, 1) is PROVISIONAL
    parser.finalize()
    assert parser.ledger.status(INNER, 0, 1) is AUDITABLE
    assert parser.ledger.closed is True


def test_inner_encoding_failure_is_contained_and_sibling_auditable() -> None:
    document = _inner_document(
        _part(b"good", b"ok"),
        _part(b"bad", b"@@@", encoding="base64"),
    )
    body = _outer_message(_outer_field(b"nested", document))
    parser, _of, _ofl, inner_fields, _inner_files = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()

    assert parser.failures
    assert parser.ledger.status(INNER, 0, 1) is AUDITABLE
    assert parser.ledger.status(INNER, 0, 2) is FAILED
    # The failed part is not delivered; the good sibling still is.
    assert [field.field_name for field in inner_fields] == [b"good"]


def test_failed_window_keeps_outer_field() -> None:
    document = _inner_document(_part(b"bad", b"=zz", encoding="quoted-printable"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, outer_fields, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    assert parser.ledger.window_failed(0) is True
    assert outer_fields[0].value == document


def test_window_offsets_select_a_subrange() -> None:
    prefix, suffix = b"PREFIX-", b"-SUFFIX"
    document = _inner_document(_part(b"a", b"zzz"))
    value = prefix + document + suffix
    body = _outer_message(_outer_field(b"nested", value))
    window = SourceWindow(
        b"nested",
        b"innerb",
        start=len(prefix),
        end=len(prefix) + len(document),
    )
    parser, outer_fields, _ofl, inner_fields, _ifl = _parse(body, [window])
    parser.finalize()
    assert inner_fields[0].value == b"zzz"
    assert outer_fields[0].value == value


def test_content_window_further_narrows_relations() -> None:
    document = _inner_document(_part(b"a", b"0123456789"))
    body = _outer_message(_outer_field(b"nested", document))
    window = SourceWindow(b"nested", b"innerb", content_start=2, content_end=8)
    parser, *_ = _parse(body, [window])
    parser.finalize()
    # Only a truncated slice reached the nested parser, so the window fails.
    assert parser.failures
    assert parser.ledger.window_failed(0)


def test_unselected_window_parses_but_keeps_no_detail() -> None:
    document = _inner_document(_part(b"a", b"foo"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_of, inner_fields, _ifl = _parse(
        body, [SourceWindow(b"nested", b"innerb")], detailed=()
    )
    parser.finalize()
    assert inner_fields[0].value == b"foo"
    with pytest.raises(KeyError):
        parser.ledger.target_sources(INNER, 0, 1, 0)


def test_two_windows_get_distinct_records_and_ordinals() -> None:
    first = _inner_document(_part(b"x", b"one"))
    second = _inner_document(_part(b"y", b"two"))
    body = _outer_message(
        _outer_field(b"first", first),
        _outer_field(b"gap", b"ignored"),
        _outer_field(b"second", second),
    )
    windows = [
        SourceWindow(b"first", b"innerb"),
        SourceWindow(b"second", b"innerb"),
    ]
    parser, outer_fields, _ofl, inner_fields, _ifl = _parse(body, windows, detailed=(0, 1))
    parser.finalize()

    assert [field.field_name for field in inner_fields] == [b"x", b"y"]
    ledger = parser.ledger
    assert ledger.part_length(INNER, 0, 1) == 3
    assert ledger.part_length(INNER, 1, 1) == 3
    outer_ordinals = {
        info.name: key.part for key, _s, info in ledger.parts() if key.layer == OUTER
    }
    assert outer_ordinals == {b"first": 1, b"second": 3}
    # The ordinary "gap" field has no ledger record.
    assert all(info.name != b"gap" for _key, _s, info in ledger.parts())


def test_shared_base64_symbol_uses_disjoint_masks_for_content_and_newline() -> None:
    data = b"A"
    document = _inner_document(_part(b"a", data))
    encoded = base64.b64encode(document)
    body = _outer_message(_outer_field(b"nested", encoded, cte=b"base64"))
    parser, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    ledger = parser.ledger
    outer_part = next(key.part for key, _s, info in ledger.parts() if key.layer == OUTER)

    data_end = document.index(data) + 1
    data_sources = ledger.target_sources(OUTER, 0, outer_part, data_end - 1)
    cr_sources = ledger.target_sources(OUTER, 0, outer_part, data_end)
    shared = {s.position for s in data_sources} & {s.position for s in cr_sources}
    assert shared
    for position in shared:
        data_mask = next(s.mask for s in data_sources if s.position == position)
        cr_mask = next(s.mask for s in cr_sources if s.position == position)
        assert data_mask & cr_mask == 0


def test_strict_base64_rejects_invalid_symbol() -> None:
    document = _inner_document(_part(b"a", b"!!!!", encoding="base64"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    assert parser.failures
    assert parser.ledger.status(INNER, 0, 1) is FAILED


def test_strict_base64_rejects_bad_padding() -> None:
    document = _inner_document(_part(b"a", b"Q===", encoding="base64"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    assert parser.failures


def test_strict_base64_rejects_incomplete_quad() -> None:
    document = _inner_document(_part(b"a", b"QQ", encoding="base64"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    assert parser.failures


def test_base64_whitespace_is_skipped() -> None:
    payload = b"Q\tQ = \n=\r\n"
    document = _inner_document(_part(b"a", payload, encoding="base64"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_of, inner_fields, _ifl = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    assert not parser.failures
    assert inner_fields[0].value == b"A"
    # Ignored whitespace produced no inner output; it is still outer content.
    whitespace_origin = body.index(b"\t")
    refs = parser.ledger.targets_for_origin(whitespace_origin)
    assert all(ref.layer != INNER for ref in refs)


def test_strict_qp_rejects_bare_equals() -> None:
    document = _inner_document(_part(b"a", b"a=zz", encoding="quoted-printable"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    assert parser.failures


def test_strict_qp_rejects_truncated_escape_at_end() -> None:
    document = _inner_document(_part(b"a", b"a=4", encoding="quoted-printable"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_ = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    assert parser.failures


def test_strict_qp_accepts_soft_break_split_across_chunks() -> None:
    document = _inner_document(_part(b"a", b"ab=\r\ncd", encoding="quoted-printable"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_of, inner_fields, _ifl = _parse(body, [SourceWindow(b"nested", b"innerb")], chunks=1)
    parser.finalize()
    assert not parser.failures
    assert inner_fields[0].value == b"abcd"


def test_source_window_validation() -> None:
    with pytest.raises(ValueError):
        SourceWindow(b"", b"innerb")
    with pytest.raises(ValueError):
        SourceWindow(b"nested", b"")
    with pytest.raises(ValueError):
        SourceWindow(b"nested", b"innerb", encoding="rot13")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SourceWindow(b"nested", b"innerb", start=-1)
    with pytest.raises(ValueError):
        SourceWindow(b"nested", b"innerb", start=5, end=2)
    with pytest.raises(ValueError):
        SourceWindow(b"nested", b"innerb", content_end=1, content_start=4)


def test_duplicate_window_field_names_rejected() -> None:
    with pytest.raises(ValueError):
        NestedFormParser(
            b"outerb",
            [SourceWindow(b"nested", b"innerb"), SourceWindow(b"nested", b"otherb")],
        )


def test_unknown_detailed_window_rejected() -> None:
    with pytest.raises(ValueError):
        NestedFormParser(
            b"outerb",
            [SourceWindow(b"nested", b"innerb")],
            detailed_windows=(5,),
        )


def test_repeated_window_field_occurrences_keep_independent_provenance() -> None:
    document = _inner_document(_part(b"a", b"foo"))
    body = _outer_message(
        _outer_field(b"nested", document),
        _outer_field(b"nested", document),
    )
    parser, *_of, inner_fields, _ifl = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()

    assert [field.value for field in inner_fields] == [b"foo", b"foo"]
    ledger = parser.ledger
    # Each outer occurrence and inner occurrence gets its own ordinal.
    outer_parts = sorted(key.part for key, _s, info in ledger.parts() if key.layer == OUTER)
    inner_parts = sorted(key.part for key, _s, info in ledger.parts() if key.layer == INNER)
    assert outer_parts == [1, 2]
    assert inner_parts == [1, 2]
    # The two inner parts trace to disjoint original bytes.
    first = ledger.origins_for_target(INNER, 0, 1, 0)
    second = ledger.origins_for_target(INNER, 0, 2, 0)
    assert set(first).isdisjoint(second)
    # Reverse lookup never crosses the occurrence boundary.
    assert all(ref.part != 1 for ref in ledger.targets_for_origin(second[0]) if ref.layer == INNER)


def test_dict_window_spec_is_coerced() -> None:
    document = _inner_document(_part(b"a", b"foo"))
    body = _outer_message(_outer_field(b"nested", document))
    parser, *_of, inner_fields, _ifl = _parse(
        body, [{"field_name": b"nested", "boundary": b"innerb", "encoding": "binary"}]
    )
    parser.finalize()
    assert inner_fields[0].value == b"foo"
    assert parser.ledger.windows[0].encoding == BINARY


def test_direct_decoder_units_raise_provenance_decode_error() -> None:
    from python_multipart.provenance import _Base64Mapper, _QPMapper

    def noop(_data: bytes, _sources: list) -> None:
        raise AssertionError("sink must not run on invalid input")

    with pytest.raises(ProvenanceDecodeError):
        mapper = _Base64Mapper(noop)
        mapper.write(b"Q===", 0)

    with pytest.raises(ProvenanceDecodeError):
        mapper = _QPMapper(noop)
        mapper.write(b"=z", 0)


def test_origins_are_deduplicated_when_routes_converge() -> None:
    # An inner base64 part carried inside an outer base64 field: adjacent
    # document bytes can both be assembled from the same original symbol.
    document = _inner_document(_part(b"a", b"QQ==", encoding="base64"))
    encoded = base64.b64encode(document)
    body = _outer_message(_outer_field(b"nested", encoded, cte=b"base64"))
    parser, *_of, inner_fields, _ifl = _parse(body, [SourceWindow(b"nested", b"innerb")])
    parser.finalize()
    assert inner_fields[0].value == b"A"
    ledger = parser.ledger

    origins = ledger.origins_for_target(INNER, 0, 1, 0)
    # The original byte is reported once even when several routes reach it.
    assert len(origins) == len(set(origins)) and origins
    # The layer-local direct relations are still all available separately.
    assert ledger.target_sources(INNER, 0, 1, 0)


def test_status_enum_values() -> None:
    assert PartStatus.PROVISIONAL is PROVISIONAL
    assert PartStatus.AUDITABLE is AUDITABLE
    assert PartStatus.FAILED is FAILED
    assert OUTER != INNER
