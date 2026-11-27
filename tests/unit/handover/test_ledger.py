from collections import OrderedDict

import pytest
import yaml

from vcr.handover.config import HandoverConfig, OwnerIdRules
from vcr.handover.ledger import BOOK_FORMAT_VERSION, HandoverBookError, HandoverLedger
from vcr.request import Request


def _rules():
    return OwnerIdRules(OrderedDict([("OW-1", ("NW-7", "NW-8"))]), {})


def _config(shift_id, book_path):
    return HandoverConfig(shift_id, _rules(), book_path=str(book_path))


def _request(uri="http://api.test/p", body=b""):
    return Request("GET", uri, body, {})


def _response(payload=b"ok"):
    return {
        "status": {"code": 200, "message": "OK"},
        "headers": {},
        "body": {"string": payload},
    }


def test_open_creates_book_lazily(tmpdir):
    book = tmpdir.join("book.yaml")
    ledger = HandoverLedger.open(_config("shift-a", book), "cassette.yaml")
    assert ledger.shift_id == "shift-a"
    assert not book.exists()
    ledger.save()
    assert book.exists()
    assert yaml.safe_load(book.read())["version"] == BOOK_FORMAT_VERSION


def test_used_occurrence_recorded_once_however_often_played(tmpdir):
    ledger = HandoverLedger.open(_config("shift-a", tmpdir.join("book.yaml")), "cassette.yaml")
    ledger.record_playback(0)
    ledger.record_playback(0)
    ledger.record_playback(2)
    assert ledger.shift.used_occurrences == [0, 2]
    assert ledger.shift.reached_occurrences == [0, 2]


def test_shifts_have_independent_cursors(tmpdir):
    book = tmpdir.join("book.yaml")
    ledger_a = HandoverLedger.open(_config("shift-a", book), "cassette.yaml")
    ledger_a.shift.advance_cursor(3)
    ledger_a.close_shift()

    ledger_b = HandoverLedger.open(_config("shift-b", book), "cassette.yaml")
    assert ledger_b.shift.cursor == 0
    ledger_b.shift.advance_cursor(1)
    ledger_b.close_shift()

    reopened_a = HandoverLedger.open(_config("shift-a", book), "cassette.yaml")
    assert reopened_a.shift.cursor == 3


def test_withdrawal_is_registered_against_a_specific_occurrence(tmpdir):
    book = tmpdir.join("book.yaml")
    ledger = HandoverLedger.open(_config("shift-a", book), "cassette.yaml")
    request = _request("http://api.test/p?page=2")
    ledger.withdraw(1, request)
    ledger.save()

    data = yaml.safe_load(book.read())
    assert data["withdrawn"] == [
        {"occurrence": 1, "request": "http://api.test/p?page=2", "method": "GET"},
    ]
    assert ledger.is_withdrawn(1)
    assert not ledger.is_withdrawn(0)
    # Idempotent.
    ledger.withdraw(1, request)
    assert len(ledger.raw_data()["withdrawn"]) == 1


def test_reached_page_stays_playable_but_new_open_hides_it(tmpdir):
    book = tmpdir.join("book.yaml")
    ledger_a = HandoverLedger.open(_config("shift-a", book), "cassette.yaml")
    ledger_a.record_playback(1)
    ledger_a.withdraw(1, _request())
    ledger_a.close_shift()
    assert ledger_a.can_play_original(1)

    ledger_b = HandoverLedger.open(_config("shift-b", book), "cassette.yaml")
    assert not ledger_b.can_play_original(1)


def test_identical_fetches_in_one_session_are_two_pickups(tmpdir):
    ledger = HandoverLedger.open(_config("shift-a", tmpdir.join("book.yaml")), "cassette.yaml")
    request, response = _request(), _response()
    ledger.record_new_interaction(request, response)
    _, _, recorded_before = ledger.record_new_interaction(request, response)
    assert recorded_before is False
    assert len(ledger.shift.recorded) == 2


def test_reopen_does_not_record_the_same_interaction_again(tmpdir):
    book = tmpdir.join("book.yaml")
    request, response = _request(), _response()
    ledger = HandoverLedger.open(_config("shift-a", book), "cassette.yaml")
    ledger.record_new_interaction(request, response)
    ledger.close_shift()

    reopened = HandoverLedger.open(_config("shift-a", book), "cassette.yaml")
    shift_id, _index, recorded_before = reopened.record_new_interaction(request, response)
    assert recorded_before is True
    assert shift_id == "shift-a"
    assert len(reopened.shift.recorded) == 1


def test_other_shift_submission_is_not_deleted_or_duplicated(tmpdir):
    book = tmpdir.join("book.yaml")
    request, response = _request(), _response()
    ledger_b = HandoverLedger.open(_config("shift-b", book), "cassette.yaml")
    ledger_b.record_new_interaction(request, response)
    ledger_b.close_shift()

    reopened_a = HandoverLedger.open(_config("shift-a", book), "cassette.yaml")
    _shift_id, _index, recorded_before = reopened_a.record_new_interaction(request, response)
    assert recorded_before is True
    assert reopened_a.shift.recorded == []
    assert len(reopened_a.all_recorded_interactions()) == 1


def test_used_occurrences_are_unioned_across_shifts(tmpdir):
    book = tmpdir.join("book.yaml")
    ledger_a = HandoverLedger.open(_config("shift-a", book), "cassette.yaml")
    ledger_a.record_playback(0)
    ledger_a.record_playback(1)
    ledger_a.close_shift()

    ledger_b = HandoverLedger.open(_config("shift-b", book), "cassette.yaml")
    ledger_b.record_playback(1)
    ledger_b.record_playback(2)
    ledger_b.close_shift()

    reopened = HandoverLedger.open(_config("shift-a", book), "cassette.yaml")
    assert reopened.used_original_occurrences() == [0, 1, 2]


def test_book_rejects_wrong_cassette(tmpdir):
    book = tmpdir.join("book.yaml")
    ledger = HandoverLedger.open(_config("shift-a", book), "cassette-a.yaml")
    ledger.save()
    with pytest.raises(HandoverBookError):
        HandoverLedger.open(_config("shift-a", book), "cassette-b.yaml")


def test_book_rejects_unknown_version(tmpdir):
    book = tmpdir.join("book.yaml")
    book.write(yaml.safe_dump({"version": 99, "shifts": {}, "withdrawn": []}))
    with pytest.raises(HandoverBookError):
        HandoverLedger.open(_config("shift-a", book), "cassette.yaml")
