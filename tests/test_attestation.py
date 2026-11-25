from __future__ import annotations

import time

from tiktoken import Encoding
from tiktoken.attestation import (
    Catalog,
    attest_catalog,
    utf8_boundaries,
    verify_small_catalog,
)


def byte_ranks(**extra: int) -> dict[bytes, int]:
    ranks = {bytes([index]): index for index in range(256)}
    for key, value in extra.items():
        ranks[key.encode("latin1").decode("unicode_escape").encode("latin1")] = value
    return ranks


def make_encodings() -> tuple[Encoding, Encoding]:
    left_ranks = {bytes([index]): index for index in range(256)}
    left_ranks.update({b"ab": 256, b"abc": 257, b"\xe6\x88": 258})
    left = Encoding(
        "attest-left",
        pat_str=r"[a-z]+|(?s:.)",
        mergeable_ranks=left_ranks,
        special_tokens={"<s>": 300},
    )

    right_ranks = {bytes([index]): index + 1000 for index in range(256)}
    right_ranks.update({b"ab": 1256})
    right = Encoding(
        "attest-right",
        pat_str=r"[a-z]+|(?s:.)",
        mergeable_ranks=right_ranks,
        special_tokens={},
    )
    return left, right


def test_decode_with_byte_offsets_distinguishes_shared_character_offset() -> None:
    left_ranks = {bytes([index]): index for index in range(256)}
    left_ranks[b"\xe6\x88"] = 258
    left = Encoding(
        "split-left", pat_str=r"(?s:.+)", mergeable_ranks=left_ranks, special_tokens={}
    )

    tokens = left.encode("我", disallowed_special=())
    data, starts, ends = left.decode_with_byte_offsets(tokens)
    assert data == "我".encode()
    assert (starts[0], ends[0]) == (0, 2)
    assert (starts[1], ends[1]) == (2, 3)

    text, char_offsets = left.decode_with_offsets(tokens)
    assert text == "我"
    assert char_offsets == [0, 0]


def test_ab_completions_prevent_joint_attestation_past_zero() -> None:
    left, right = make_encodings()
    catalog = {
        "pieces": {"ab": "ab", "c": "c"},
        "received": ["ab"],
        "dag": {
            "roots": ["stop", "continue_c"],
            "nodes": {
                "stop": {"terminal": True},
                "continue_c": {"piece": "c", "terminal": True},
            },
        },
    }

    report = attest_catalog(left, right, catalog)
    assert report.signed_byte == 0
    assert report.left.tokens == []
    assert report.right.tokens == []
    assert report.next_unsignable_boundary == 1
    assert report.witness is not None
    assert report.witness.side in {"left", "right"}
    assert report.witness.kind == "token_crosses_boundary"
    assert {path["nodes"][0] for path in [report.witness.replay_path.to_dict()]}

    verification = verify_small_catalog(left, right, catalog, report)
    assert verification["complete_paths_checked"] == 2


def test_stable_ab_can_be_signed_at_byte_two() -> None:
    left, right = make_encodings()
    catalog = Catalog.from_dict(
        {
            "pieces": {"ab": "ab", "x": "x"},
            "received": ["ab"],
            "dag": {
                "roots": ["x"],
                "nodes": {"x": {"piece": "x", "terminal": True}},
            },
        }
    )

    report = attest_catalog(left, right, catalog)
    assert report.signed_byte == 2
    assert report.left.tokens == [256]
    assert report.right.tokens == [1256]
    assert report.left.anchors[0]["sources"][0]["piece_id"] == "ab"
    assert report.witness is None or report.next_unsignable_boundary is None
    verify_small_catalog(left, right, catalog, report)


def test_special_token_can_cross_piece_seam_on_one_side() -> None:
    left, right = make_encodings()
    catalog = {
        "pieces": {"lead": "<", "special_tail": "s>", "ordinary_tail": "x"},
        "received": ["lead"],
        "dag": {
            "roots": ["special", "ordinary"],
            "nodes": {
                "special": {"piece": "special_tail", "terminal": True},
                "ordinary": {"piece": "ordinary_tail", "terminal": True},
            },
        },
    }

    report = attest_catalog(
        left,
        right,
        catalog,
        allowed_special_left={"<s>"},
        allowed_special_right=set(),
    )
    assert report.signed_byte == 0
    assert report.witness is not None
    assert report.witness.side == "left"
    assert report.witness.token == 300
    assert report.witness.start_byte == 0
    assert report.witness.end_byte == 3
    assert report.witness.replay_path.node_ids == ("special",)

    ordinary_left = left.encode("<s>", allowed_special=set(), disallowed_special=())
    assert 300 not in ordinary_left
    verify_small_catalog(
        left,
        right,
        catalog,
        report,
        allowed_special_left={"<s>"},
        allowed_special_right=set(),
    )


def test_multibyte_seam_maps_tokens_to_original_pieces() -> None:
    left, right = make_encodings()
    catalog = {
        "pieces": {"lead": {"hex": "e6"}, "rest": {"hex": "8891"}},
        "received": [{"piece": "lead"}, {"piece": "rest"}],
    }

    report = attest_catalog(left, right, catalog)
    assert report.signed_byte == 3
    assert report.prefix_text == "我"
    assert report.left.tokens == [258, 145]
    assert report.right.signed_byte == 3
    left_sources = report.left.anchors[0]["sources"]
    assert [(item["piece_id"], item["start"], item["end"]) for item in left_sources] == [
        ("lead", 0, 1),
        ("rest", 0, 1),
    ]
    verify_small_catalog(left, right, catalog, report)


def test_repeated_piece_occurrences_remain_distinct_sources() -> None:
    left, right = make_encodings()
    catalog = {
        "pieces": {"a": "a"},
        "received": ["a", "a"],
        "dag": {
            "roots": ["first_future"],
            "nodes": {
                "first_future": {"piece": "a", "next": ["second_future"]},
                "second_future": {"piece": "a", "terminal": True},
            },
        },
    }

    report = attest_catalog(left, right, catalog)
    assert [item["piece_id"] for item in report.occurrences] == ["a", "a"]
    assert report.occurrences[1]["global_start"] == 1
    verify_small_catalog(left, right, catalog, report)


def test_many_branches_converge_into_shared_suffix() -> None:
    left, right = make_encodings()
    branch_count = 40
    nodes = {
        "merge": {"next": ["suffix_0"]},
    }
    roots = [f"branch_{index}" for index in range(branch_count)]
    for index in range(branch_count):
        nodes[f"branch_{index}"] = {"piece": f"branch_{index}", "next": ["merge"]}

    suffix_length = 80
    for index in range(suffix_length):
        nodes[f"suffix_{index}"] = {
            "piece": "shared",
            "next": [] if index == suffix_length - 1 else [f"suffix_{index + 1}"],
            "terminal": index == suffix_length - 1,
        }

    catalog = {
        "pieces": {
            "ab": "ab",
            **{f"branch_{index}": f"b{index}!" for index in range(branch_count)},
            "shared": "ab",
        },
        "received": ["ab"],
        "dag": {"roots": roots, "nodes": nodes},
    }

    started = time.perf_counter()
    report = attest_catalog(left, right, catalog)
    elapsed = time.perf_counter() - started
    assert report.signed_byte in {0, 2}
    assert elapsed < 5.0
    verification = verify_small_catalog(left, right, catalog, report)
    assert verification["complete_paths_checked"] == branch_count


def test_utf8_boundaries_stop_inside_truncated_character() -> None:
    data = b"ab\xe6\x88\x91\xe6"
    assert utf8_boundaries(data) == [0, 1, 2, 5]
