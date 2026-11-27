"""Tests for offline board reconstruction from retained Atom responses."""

from __future__ import annotations

import arxiv
from arxiv import _offline
from arxiv._offline import RequestWindow, reconstruct_board

SEARCH = arxiv.Search(query="testing", max_results=5)
WINDOWS = [RequestWindow(0, 3), RequestWindow(1, 4), RequestWindow(3, 5)]
TOTAL = 5

_UPDATED = {
    letter: f"2026-01-{day:02d}T00:00:00Z" for day, letter in enumerate("ABCDEFGH", start=1)
}
_ID = {letter: f"2101.000{day}v1" for day, letter in enumerate("ABCDEFGH", start=1)}
_LETTER = {short_id: letter for letter, short_id in _ID.items()}


def _entry_xml(short_id: str, title: str, updated: str | None) -> str:
    timestamps = (
        f"<updated>{updated}</updated><published>{updated}</published>"
        if updated is not None
        else ""
    )
    return (
        "<entry>"
        f"<id>http://arxiv.org/abs/{short_id}</id>"
        f"<title>{title}</title>"
        f"{timestamps}"
        "<summary>summary</summary>"
        "<author><name>Author</name></author>"
        f'<link href="https://arxiv.org/abs/{short_id}" rel="alternate" type="text/html"/>'
        f'<link href="https://arxiv.org/pdf/{short_id}" title="pdf" type="application/pdf"/>'
        '<category term="cs.AI"/><arxiv:primary_category term="cs.AI"/>'
        "</entry>"
    )


def make_feed(
    start: int,
    items: list[tuple[str, str | None]],
    total: int = TOTAL,
    feed_tag: str = "feed",
) -> bytes:
    """Build an Atom page. Items are (letter-or-short-id, updated-letter-or-None)."""
    entries = []
    for short_id, updated_key in items:
        letter = _LETTER.get(short_id)
        title = f"Title {letter}" if letter else "Unversioned"
        updated = _UPDATED[updated_key] if updated_key is not None else None
        entries.append(_entry_xml(short_id, title, updated))
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:arxiv="http://arxiv.org/schemas/atom" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<id>https://arxiv.org/api/{feed_tag}</id>"
        f"<opensearch:totalResults>{total}</opensearch:totalResults>"
        f"<opensearch:startIndex>{start}</opensearch:startIndex>"
        f"<opensearch:itemsPerPage>{len(items)}</opensearch:itemsPerPage>"
        + "".join(entries)
        + "</feed>"
    ).encode()


def letter_feed(
    start: int,
    letters: list[str | None],
    missing: set[str] | None = None,
    total: int = TOTAL,
    feed_tag: str = "feed",
) -> bytes:
    """Build a page from letters; letters in `missing` have no readable timestamp."""
    missing = missing or set()
    items: list[tuple[str, str | None]] = []
    for letter in letters:
        if letter is None:
            items.append(("unversioned-record", "A"))
        else:
            items.append((_ID[letter], None if letter in missing else letter))
    return make_feed(start, items, total=total, feed_tag=feed_tag)


def board_letters(result: _offline.Reconstruction) -> list[str]:
    assert result.board is not None
    return [_LETTER[entry.version_id] for entry in result.board]


def test_round_one_cannot_publish() -> None:
    responses = [
        [letter_feed(0, ["A", "B", "C"], missing={"B"}), letter_feed(0, ["A", "B", "D"])],
        [letter_feed(1, ["B", "C", "E"]), letter_feed(1, ["B", "D", "F"])],
        [letter_feed(3, ["F", "A"])],
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    assert result.published is False
    assert result.board is None
    assert result.reasons
    # No candidate can be reported unless it belongs to a complete board.
    assert result.candidates == {}
    assert result.uncertain_ranks == []


def test_round_one_retains_previously_published_board() -> None:
    prior = reconstruct_board(
        SEARCH,
        0,
        5,
        WINDOWS,
        [
            [letter_feed(0, ["A", "B", "C"])],
            [letter_feed(1, ["B", "C", "E"])],
            [letter_feed(3, ["E", "G"])],
        ],
    )
    assert prior.published is True
    responses = [
        [letter_feed(0, ["A", "B", "C"], missing={"B"}), letter_feed(0, ["A", "B", "D"])],
        [letter_feed(1, ["B", "C", "E"]), letter_feed(1, ["B", "D", "F"])],
        [letter_feed(3, ["F", "A"])],
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses, previous=prior)
    assert result.published is False
    assert result.board is prior.board
    assert any("valid board" in reason for reason in result.reasons)


def test_round_two_publishes_completed_board() -> None:
    responses = [
        [letter_feed(0, ["A", "B", "C"], missing={"B"}), letter_feed(0, ["A", "B", "D"])],
        [letter_feed(1, ["B", "C", "E"]), letter_feed(1, ["B", "D", "F"])],
        [letter_feed(3, ["F", "A"]), letter_feed(3, ["E", "G"])],
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    assert result.published is True
    assert board_letters(result) == ["A", "B", "C", "E", "G"]
    assert result.uncertain_ranks == []
    assert result.total_results == TOTAL

    entries = {entry.rank: entry for entry in result.board}
    # B is readable and its Result must come from the overlapping window [1,4).
    b_entry = entries[1]
    assert b_entry.version_id == _ID["B"]
    assert b_entry.result is not None
    assert b_entry.result.updated.isoformat() == "2026-01-02T00:00:00+00:00"
    assert b_entry.title == "Title B"
    readability = {(ev.window_index, ev.position): ev.readable for ev in b_entry.evidence}
    assert readability[(0, 1)] is False  # unreadable B in window [0,3)
    assert readability[(1, 0)] is True  # readable B in window [1,4)

    # E at rank 3 is confirmed in both the [1,4) and [3,5) windows.
    e_entry = entries[3]
    windows_for_e = {ev.window_index for ev in e_entry.evidence}
    assert windows_for_e == {1, 2}
    assert all(ev.readable for ev in e_entry.evidence)


def test_round_two_is_input_order_independent() -> None:
    responses = [
        [letter_feed(0, ["A", "B", "C"], missing={"B"}), letter_feed(0, ["A", "B", "D"])],
        [letter_feed(1, ["B", "C", "E"]), letter_feed(1, ["B", "D", "F"])],
        [letter_feed(3, ["F", "A"]), letter_feed(3, ["E", "G"])],
    ]
    forward = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    reversed_windows = list(reversed(WINDOWS))
    reversed_responses = [list(reversed(batch)) for batch in reversed(responses)]
    backward = reconstruct_board(SEARCH, 0, 5, reversed_windows, reversed_responses)

    assert backward.published == forward.published
    assert board_letters(backward) == board_letters(forward)
    assert backward.uncertain_ranks == forward.uncertain_ranks
    forward_evidence = {
        (entry.rank, ev.window_index, ev.response_index, ev.position)
        for entry in forward.board  # type: ignore[union-attr]
        for ev in entry.evidence
    }
    backward_evidence = {
        (entry.rank, ev.window_index, ev.response_index, ev.position)
        for entry in backward.board  # type: ignore[union-attr]
        for ev in entry.evidence
    }
    assert backward_evidence == forward_evidence


def test_round_three_keeps_published_board_and_lists_candidates() -> None:
    responses = [
        [letter_feed(0, ["A", "B", "C"], missing={"B"}), letter_feed(0, ["A", "B", "D"])],
        [letter_feed(1, ["B", "C", "E"]), letter_feed(1, ["B", "D", "F"])],
        [
            letter_feed(3, ["F", "A"]),
            letter_feed(3, ["E", "G"]),
            letter_feed(3, ["F", "H"]),
        ],
    ]
    round_two = reconstruct_board(
        SEARCH,
        0,
        5,
        WINDOWS,
        [batch[:-1] if i == 2 else batch for i, batch in enumerate(responses)],
    )
    assert round_two.published is True

    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses, previous=round_two)
    assert result.published is False
    assert result.board is round_two.board
    assert result.uncertain_ranks == [2, 3, 4]
    assert {_LETTER[c.version_id] for c in result.candidates[2]} == {"C", "D"}
    assert {_LETTER[c.version_id] for c in result.candidates[3]} == {"E", "F"}
    assert {_LETTER[c.version_id] for c in result.candidates[4]} == {"G", "H"}

    # Every candidate must be backed by evidence traceable to a stored entry.
    for rank, candidate_list in result.candidates.items():
        for candidate in candidate_list:
            assert candidate.evidence
            assert candidate.updated is not None and candidate.title
            for evidence in candidate.evidence:
                window = next(
                    window
                    for window, index in result.window_indices.items()
                    if index == evidence.window_index
                )
                assert window.contains(rank)
                assert evidence.version_id == candidate.version_id

    # The retained board is still the round-two board.
    assert board_letters(result) == ["A", "B", "C", "E", "G"]


def test_round_three_diagnostics_are_stable_under_reversal() -> None:
    responses = [
        [letter_feed(0, ["A", "B", "C"], missing={"B"}), letter_feed(0, ["A", "B", "D"])],
        [letter_feed(1, ["B", "C", "E"]), letter_feed(1, ["B", "D", "F"])],
        [
            letter_feed(3, ["F", "A"]),
            letter_feed(3, ["E", "G"]),
            letter_feed(3, ["F", "H"]),
        ],
    ]
    forward = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    reversed_responses = [list(reversed(batch)) for batch in reversed(responses)]
    backward = reconstruct_board(SEARCH, 0, 5, list(reversed(WINDOWS)), reversed_responses)

    assert forward.published is backward.published is False
    assert forward.uncertain_ranks == backward.uncertain_ranks == [2, 3, 4]
    for rank in (2, 3, 4):
        forward_letters = sorted(_LETTER[c.version_id] for c in forward.candidates[rank])
        backward_letters = sorted(_LETTER[c.version_id] for c in backward.candidates[rank])
        assert forward_letters == backward_letters
        forward_keys = {
            (ev.window_index, ev.response_index, ev.position)
            for c in forward.candidates[rank]
            for ev in c.evidence
        }
        backward_keys = {
            (ev.window_index, ev.response_index, ev.position)
            for c in backward.candidates[rank]
            for ev in c.evidence
        }
        assert forward_keys == backward_keys


def test_identical_boards_across_selections_publish() -> None:
    # Two byte-distinct responses for one window carry identical records.
    first = letter_feed(1, ["B", "C", "E"], feed_tag="one")
    second = letter_feed(1, ["B", "C", "E"], feed_tag="two")
    responses = [
        [letter_feed(0, ["A", "B", "C"])],
        [first, second],
        [letter_feed(3, ["E", "G"])],
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    assert result.published is True
    assert board_letters(result) == ["A", "B", "C", "E", "G"]
    assert result.uncertain_ranks == []


def test_unversioned_entry_cannot_complete_a_rank() -> None:
    # Rank 1 has an unreadable versioned B in window [0,3) and only an
    # unversioned, readable record in window [1,4): nothing may complete B.
    responses = [
        [letter_feed(0, ["A", "B", "C"], missing={"B"})],
        [letter_feed(1, [None, "C", "E"])],
        [letter_feed(3, ["E", "G"])],
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    assert result.published is False
    assert result.board is None
    assert any("readable Result" in reason for reason in result.reasons)


def test_duplicate_version_across_ranks_is_rejected() -> None:
    responses = [
        [letter_feed(0, ["A", "B", "C"])],
        [letter_feed(1, ["B", "C", "E"])],
        [letter_feed(3, ["E", "A"])],  # A repeats at rank 4
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    assert result.published is False
    assert result.board is None


def test_conflicting_updated_timestamps_block_publication() -> None:
    stale = letter_feed(1, ["B", "C", "E"])
    # Same version ids at the overlap, but B carries a different updated.
    fresh = make_feed(
        0,
        [
            (_ID["A"], "A"),
            (_ID["B"], "A"),
            (_ID["C"], "C"),
        ],
        feed_tag="fresh",
    )
    responses = [
        [fresh],
        [stale],
        [letter_feed(3, ["E", "G"])],
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    assert result.published is False


def test_incomplete_response_is_excluded() -> None:
    # Declares itemsPerPage=3 but only retains two entries: not complete.
    broken = letter_feed(0, ["A", "B"])
    responses = [
        [broken, letter_feed(0, ["A", "B", "C"])],
        [letter_feed(1, ["B", "C", "E"])],
        [letter_feed(3, ["E", "G"])],
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    assert result.published is True
    assert board_letters(result) == ["A", "B", "C", "E", "G"]
    assert any("not complete" in reason for reason in result.reasons)


def test_window_without_any_complete_response_fails() -> None:
    responses = [
        [letter_feed(0, ["A", "B"])],
        [letter_feed(1, ["B", "C", "E"])],
        [letter_feed(3, ["E", "G"])],
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    assert result.published is False
    assert any("no complete response" in reason for reason in result.reasons)


def test_total_results_must_cover_rank_range() -> None:
    responses = [
        [letter_feed(0, ["A", "B", "C"], total=4)],
        [letter_feed(1, ["B", "C", "E"], total=4)],
        [letter_feed(3, ["E", "G"], total=4)],
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    assert result.published is False
    assert any("does not cover" in reason for reason in result.reasons)


def test_inconsistent_total_results_blocks_publication() -> None:
    responses = [
        [letter_feed(0, ["A", "B", "C"], total=5)],
        [letter_feed(1, ["B", "C", "E"], total=6)],
        [letter_feed(3, ["E", "G"], total=5)],
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS, responses)
    assert result.published is False
    assert any("totalResults" in reason for reason in result.reasons)


def test_uncovered_rank_range_fails() -> None:
    responses = [
        [letter_feed(0, ["A", "B", "C"])],
        [letter_feed(1, ["B", "C", "E"])],
    ]
    result = reconstruct_board(SEARCH, 0, 5, WINDOWS[:2], responses)
    assert result.published is False
    assert any("cover ranks" in reason for reason in result.reasons)


def test_tuple_windows_and_client_entry_points() -> None:
    responses = [
        [letter_feed(0, ["A", "B", "C"])],
        [letter_feed(1, ["B", "C", "E"])],
        [letter_feed(3, ["E", "G"])],
    ]
    tuple_windows = [(0, 3), (1, 4), (3, 5)]
    client_result = arxiv.Client().offline_board(SEARCH, 0, 5, tuple_windows, responses)
    search_result = SEARCH.offline_board(0, 5, tuple_windows, responses)
    assert client_result.published and search_result.published
    assert (
        board_letters(client_result)
        == board_letters(search_result)
        == [
            "A",
            "B",
            "C",
            "E",
            "G",
        ]
    )
    # Evidence carries window, response and page position traceability.
    rank_zero = client_result.board[0]  # type: ignore[index]
    evidence = rank_zero.evidence[0]
    assert (evidence.window_index, evidence.response_index, evidence.position) == (0, 0, 0)


def test_results_remain_individually_iterable() -> None:
    # The ordinary online path must be untouched.
    results = list(arxiv.Client(page_size=10).results(arxiv.Search(query="testing", max_results=3)))
    assert len(results) == 3
    assert all(isinstance(result, arxiv.Result) for result in results)
