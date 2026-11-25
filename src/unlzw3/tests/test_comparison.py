"""Tests for the index based archive comparison entry point."""

from __future__ import annotations

import importlib.resources as pkgr
import time

import pytest

from unlzw3 import unlzw
from unlzw3.comparison import (
    ByteAnchor,
    CodeAnchor,
    ComparisonResult,
    InvalidArchive,
    RangeSpec,
    _WordWalker,
    build_index,
    compare,
    compare_archives,
    load_index,
    make_selection,
    save_index,
)
from unlzw3.compare_cli import main as cli_main
from unlzw3.tests.lzw_helpers import lzw_compress


# ---------------------------------------------------------------------------
# Helpers and shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sample_text() -> bytes:
    return (
        b"the quick brown fox jumps over the lazy dog. " * 400
        + bytes(range(256)) * 8
        + b"lzw dictionary reset boundaries matter here. " * 300
    )


def _archive(data: bytes, maxbits: int, block: bool) -> bytes:
    return lzw_compress(data, maxbits=maxbits, block=block)


def _full_selection(index):
    return make_selection(index, [RangeSpec(ByteAnchor(0), ByteAnchor(index.total_out))])


def _walk(selection) -> bytes:
    walker = _WordWalker(selection)
    parts = []
    while True:
        item = walker.next_chunk()
        if item is None:
            break
        parts.append(item[0])
    return b"".join(parts)


# ---------------------------------------------------------------------------
# Index fidelity against the reference reader
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("maxbits", [9, 10, 12, 14, 16])
@pytest.mark.parametrize("block", [True, False])
def test_index_matches_reference_reader(sample_text, maxbits, block):
    archive = _archive(sample_text, maxbits, block)
    assert unlzw(archive) == sample_text

    index = build_index(archive)
    assert index.total_out == len(sample_text)

    emitted_length = sum(
        index.out_len[k] for k in range(index.n_codes) if not index.is_clear(k)
    )
    assert emitted_length == len(sample_text)

    # out_start is monotonic and consistent with the recorded word lengths.
    for k in range(index.n_codes):
        if not index.is_clear(k):
            assert index.out_len[k] >= 1
            assert index.out_start[k] + index.out_len[k] <= index.total_out
        assert index.bit_end[k] > index.bit_start[k]
        if k:
            # Padding bits may create gaps but code ranges never overlap.
            assert index.bit_start[k] >= index.bit_end[k - 1]


def test_index_on_shipped_fixtures():
    for name in ("hello.Z", "lipsum.com.Z"):
        with pkgr.as_file(pkgr.files(__package__).joinpath(name)) as path:
            data = path.read_bytes()
        index = build_index(data)
        assert index.total_out == len(unlzw(data))
        assert _walk(_full_selection(index)) == unlzw(data)


def test_walker_reconstruction_across_clears(sample_text):
    archive = _archive(sample_text, 10, True)
    index = build_index(archive)
    assert len(index.segment_start) > 1
    assert _walk(_full_selection(index)) == sample_text


def test_width_transitions_record_padding_separately(sample_text):
    archive = _archive(sample_text, 12, True)
    index = build_index(archive)
    gaps = [
        k for k in range(1, index.n_codes) if index.bit_start[k] > index.bit_end[k - 1]
    ]
    assert gaps
    # No recorded code range covers a padding gap.
    for k in gaps:
        assert index.bit_start[k] - index.bit_end[k - 1] > 0
    # The widths actually used include more than one code size.
    widths = {index.bit_end[k] - index.bit_start[k] for k in range(index.n_codes)}
    assert len(widths) > 1


# ---------------------------------------------------------------------------
# Equality across different code boundaries, widths and clear timing
# ---------------------------------------------------------------------------


def test_different_widths_and_modes_compare_equal(sample_text):
    left = build_index(_archive(sample_text, 9, True))
    right = build_index(_archive(sample_text, 16, False))
    assert left.n_codes != right.n_codes
    result = compare(_full_selection(left), _full_selection(right))
    assert result.equal


def test_one_code_vs_two_codes_same_bytes(sample_text):
    # Different compression settings guarantee different code boundaries for
    # the same text; equality must still hold at the byte level.
    left = build_index(_archive(sample_text, 10, True))
    right = build_index(_archive(sample_text, 14, True))
    result = compare(_full_selection(left), _full_selection(right))
    assert isinstance(result, ComparisonResult)
    assert result.equal
    assert result.output_offset is None


def test_mid_word_starts_at_different_absolute_offsets(sample_text):
    left = build_index(_archive(sample_text, 10, True))
    right = build_index(_archive(sample_text, 12, True))

    left_specs = [
        RangeSpec(ByteAnchor(137), length=900),
        RangeSpec(ByteAnchor(5000), length=1234),
    ]
    # The right side starts inside an emitted word at an absolute offset that
    # differs from the left side's start.
    right_start_code = _code_covering(right, 211)
    right_specs = [
        RangeSpec(CodeAnchor(right_start_code, 2), length=900),
        RangeSpec(ByteAnchor(5074), length=1234),
    ]
    left_selection = make_selection(left, left_specs)
    right_selection = make_selection(right, right_specs)

    left_expected = sample_text[137 : 137 + 900] + sample_text[5000 : 5000 + 1234]
    assert _walk(left_selection) == left_expected

    right_start = right.out_start[right_start_code] + 2
    right_expected = sample_text[right_start : right_start + 900] + sample_text[
        5074 : 5074 + 1234
    ]
    assert _walk(right_selection) == right_expected

    # When both handoff sheets name the same retained byte window, the
    # concatenated selections compare equal despite unrelated anchors.
    aligned_right = make_selection(
        right,
        [RangeSpec(ByteAnchor(137), length=900), RangeSpec(ByteAnchor(5000), length=1234)],
    )
    assert compare(left_selection, aligned_right).equal


def _code_covering(index, absolute_offset):
    import bisect

    ordinal = bisect.bisect_right(index.out_start, absolute_offset) - 1
    while index.is_clear(ordinal):
        ordinal += 1
    return ordinal


def test_ranges_cross_clear_and_start_inside_word(sample_text):
    archive = _archive(sample_text, 10, True)
    index = build_index(archive)
    assert len(index.segment_start) >= 3

    clear_boundary = index.out_start[index.segment_start[2]]
    specs = [
        RangeSpec(ByteAnchor(clear_boundary - 40), length=120),
        RangeSpec(CodeAnchor(index.segment_start[3] + 1, 1), length=500),
    ]
    selection = make_selection(index, specs)
    expected = (
        sample_text[clear_boundary - 40 : clear_boundary + 80]
        + sample_text[
            index.out_start[index.segment_start[3] + 1]
            + 1 : index.out_start[index.segment_start[3] + 1]
            + 1
            + 500
        ]
    )
    assert _walk(selection) == expected
    assert compare(selection, make_selection(build_index(archive), specs)).equal


# ---------------------------------------------------------------------------
# Difference reporting and provenance
# ---------------------------------------------------------------------------


def test_byte_difference_reports_offset_and_provenance(sample_text):
    mutated = bytearray(sample_text)
    mutated[2500] ^= 0x01
    left = build_index(_archive(sample_text, 12, True))
    right = build_index(_archive(bytes(mutated), 14, True))

    result = compare(_full_selection(left), _full_selection(right))
    assert not result.equal
    assert result.output_offset == 2500
    assert result.left_byte == sample_text[2500]
    assert result.right_byte == mutated[2500]
    assert result.short_by is None

    for provenance, index in (
        (result.left_provenance, left),
        (result.right_provenance, right),
    ):
        assert provenance.range_index == 0
        assert 1 <= provenance.segment <= len(index.segment_start)
        width = provenance.code_bit_end - provenance.code_bit_start
        assert 9 <= width <= index.max_bits
        assert 0 <= provenance.word_position < index.out_len[provenance.code_index]
        assert (
            provenance.decompressed_offset
            == index.out_start[provenance.code_index] + provenance.word_position
        )
        assert index.bit_start[provenance.code_index] == provenance.code_bit_start


def test_same_code_value_different_dictionary_words(sample_text):
    # A mutation early in the text makes the post-clear dictionaries diverge, so
    # the same compressed code value can denote different words.  Comparison
    # must rely on emitted bytes, never on code identity.
    mutated = bytearray(sample_text)
    mutated[10] ^= 0x01
    left = build_index(_archive(sample_text, 10, True))
    right = build_index(_archive(bytes(mutated), 10, True))

    left_words = _word_map(left)
    right_words = right
    right_map = _word_map(right)
    coinciding = [
        ordinal
        for ordinal, word in left_words.items()
        if ordinal in right_map
        and left.code_value[ordinal] == right.code_value[ordinal]
        and word != right_map[ordinal]
        and left.code_value[ordinal] >= 256
    ]
    assert coinciding

    result = compare(_full_selection(left), _full_selection(right))
    assert not result.equal
    assert result.output_offset == 10
    text = str(result)
    assert "compressed bits" in text


def _word_map(index):
    walker = _WordWalker(_full_selection(index))
    words = {}
    while True:
        item = walker.next_chunk()
        if item is None:
            break
        chunk, ordinal, word_begin, _ = item
        words.setdefault(ordinal, bytearray()).extend(chunk)
    return {ordinal: bytes(value) for ordinal, value in words.items()}


def test_length_mismatch_names_short_side(sample_text):
    left = build_index(_archive(sample_text, 12, True))
    right = build_index(_archive(sample_text, 12, True))

    left_selection = make_selection(left, [RangeSpec(ByteAnchor(0), length=4000)])
    right_selection = make_selection(right, [RangeSpec(ByteAnchor(0), length=3987)])

    result = compare(left_selection, right_selection)
    assert not result.equal
    assert result.output_offset == 3987
    assert result.short_by == 13
    assert result.right_byte is None
    assert result.right_provenance is None
    assert result.left_provenance is not None
    assert result.left_byte == sample_text[3987]

    reversed_result = compare(right_selection, left_selection)
    assert reversed_result.short_by == 13
    assert reversed_result.left_byte is None
    assert reversed_result.right_provenance is not None


def test_difference_reports_which_handoff_range(sample_text):
    left = build_index(_archive(sample_text, 12, True))
    right_data = bytearray(sample_text)
    right_data[6000] ^= 0x01
    right = build_index(_archive(bytes(right_data), 12, True))

    specs = [
        RangeSpec(ByteAnchor(0), length=3000),
        RangeSpec(ByteAnchor(3000), length=4000),
    ]
    result = compare(make_selection(left, specs), make_selection(right, specs))
    assert not result.equal
    assert result.left_provenance.range_index == 1
    assert result.right_provenance.range_index == 1


# ---------------------------------------------------------------------------
# Invalid input must identify the offending archive
# ---------------------------------------------------------------------------


def test_invalid_archive_raises_and_is_named():
    good = _archive(b"valid archive contents " * 20, 12, True)
    with pytest.raises(InvalidArchive):
        build_index(b"not an lzw stream")

    result = compare_archives(b"bad magic", good, [], [])
    assert result.invalid_side == "left"
    assert not result.equal

    result = compare_archives(good, b"\x1f\x9d\x90x", [], [])
    assert result.invalid_side == "right"
    assert "Invalid" in result.invalid_reason


def test_truncated_stream_is_invalid():
    archive = _archive(b"truncation case " * 100, 12, True)
    with pytest.raises(InvalidArchive):
        build_index(archive[:-2])


def test_empty_archive_and_range_lookup():
    index = build_index(b"\x1f\x9d\x90")
    assert index.total_out == 0
    assert index.n_codes == 0
    assert _walk(_full_selection(index)) == b""

    other = build_index(_archive(b"non empty contents " * 10, 12, True))
    result = compare(_full_selection(index), _full_selection(other))
    assert result.short_by == other.total_out

    selection = make_selection(
        other,
        [RangeSpec(ByteAnchor(0), length=10), RangeSpec(ByteAnchor(20), length=15)],
    )
    assert selection.range_index_at(0) == 0
    assert selection.range_index_at(9) == 0
    assert selection.range_index_at(10) == 1
    assert selection.range_index_at(24) == 1
    with pytest.raises(ValueError):
        selection.range_index_at(selection.output_length)


# ---------------------------------------------------------------------------
# Index reuse: one scan, many handoff sheets
# ---------------------------------------------------------------------------


def test_index_reused_for_multiple_range_sheets(sample_text):
    left = build_index(_archive(sample_text, 10, True))
    right = build_index(_archive(sample_text, 14, True))

    sheets = [
        [RangeSpec(ByteAnchor(0), length=1000)],
        [RangeSpec(ByteAnchor(2000), length=1500), RangeSpec(ByteAnchor(8000), length=777)],
        [RangeSpec(ByteAnchor(100), ByteAnchor(12345))],
    ]
    for sheet in sheets:
        assert compare(make_selection(left, sheet), make_selection(right, sheet)).equal


def test_comparison_skips_segments_outside_selection():
    data = bytes([index % 256 for index in range(320_000)])
    archive = _archive(data, 10, True)
    index = build_index(archive)
    assert len(index.segment_start) > 20

    middle = len(index.segment_start) // 2
    start_ordinal = index.segment_start[middle] + 5
    # Stay strictly inside the chosen segment so exactly one segment is needed.
    next_segment_start = index.segment_start[middle + 1]
    last_ordinal = next_segment_start - 2
    while index.is_clear(last_ordinal):
        last_ordinal -= 1
    end_byte = index.out_start[last_ordinal] + index.out_len[last_ordinal]
    start_byte = index.out_start[start_ordinal]
    selection = make_selection(
        index, [RangeSpec(ByteAnchor(start_byte), ByteAnchor(end_byte))]
    )

    walker = _WordWalker(selection)
    while walker.next_chunk() is not None:
        pass

    assert walker._needed_segments() == [middle]
    segment_codes = _segment_code_count(index, middle)
    assert walker.decoded_count <= segment_codes
    assert walker.decoded_count < index.n_codes // 10


def _segment_code_count(index, segment):
    start = index.segment_start[segment]
    end = (
        index.segment_start[segment + 1]
        if segment + 1 < len(index.segment_start)
        else index.n_codes
    )
    return sum(1 for ordinal in range(start, end) if not index.is_clear(ordinal))


def test_large_two_segment_comparison_is_linear_and_correct():
    data = bytes([(index * 7) % 256 for index in range(400_000)])
    left = build_index(_archive(data, 10, True))
    right = build_index(_archive(data, 12, True))
    assert left.n_codes > 100_000

    boundary = left.total_out // 2
    specs = [
        RangeSpec(ByteAnchor(0), ByteAnchor(boundary)),
        RangeSpec(ByteAnchor(boundary), ByteAnchor(left.total_out)),
    ]
    started = time.perf_counter()
    result = compare(make_selection(left, specs), make_selection(right, specs))
    elapsed = time.perf_counter() - started
    assert result.equal
    assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Persistence and CLI
# ---------------------------------------------------------------------------


def test_save_and_load_index_round_trip(sample_text, tmp_path):
    index = build_index(_archive(sample_text, 12, True), source_name="vol-a")
    path = tmp_path / "volume.lzwidx"
    save_index(index, path)
    restored = load_index(path)

    assert restored.n_codes == index.n_codes
    assert restored.total_out == index.total_out
    assert restored.segment_start == index.segment_start
    assert restored.bit_start == index.bit_start
    assert restored.code_value == index.code_value
    assert restored.source_name == "vol-a"
    assert compare(_full_selection(restored), _full_selection(index)).equal


def test_cli_compare_and_index(sample_text, tmp_path):
    left_path = tmp_path / "left.Z"
    right_path = tmp_path / "right.Z"
    left_path.write_bytes(_archive(sample_text, 10, True))
    right_path.write_bytes(_archive(sample_text, 16, True))

    index_path = tmp_path / "left.lzwidx"
    assert cli_main(["index", str(left_path), str(index_path)]) == 0

    rc = cli_main(
        [
            "compare",
            str(index_path),
            str(right_path),
            "--left-start",
            "byte:0",
            "--left-length",
            "1000",
            "--right-start",
            "byte:0",
            "--right-length",
            "1000",
        ]
    )
    assert rc == 0

    mutated = bytearray(sample_text)
    mutated[500] ^= 0x01
    bad_path = tmp_path / "bad.Z"
    bad_path.write_bytes(_archive(bytes(mutated), 12, True))
    rc = cli_main(
        [
            "compare",
            str(left_path),
            str(bad_path),
            "--left-start",
            "byte:0",
            "--left-length",
            "1000",
            "--right-start",
            "byte:0",
            "--right-length",
            "1000",
        ]
    )
    assert rc == 1


def test_cli_invalid_archive(tmp_path):
    bad = tmp_path / "bad.Z"
    good = tmp_path / "good.Z"
    bad.write_bytes(b"broken")
    good.write_bytes(lzw_compress(b"fine contents here " * 10, 12, True))
    with pytest.raises(SystemExit):
        cli_main(
            [
                "compare",
                str(bad),
                str(good),
                "--left-start",
                "byte:0",
                "--left-length",
                "5",
                "--right-start",
                "byte:0",
                "--right-length",
                "5",
            ]
        )
