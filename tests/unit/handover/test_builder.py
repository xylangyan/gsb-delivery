from collections import OrderedDict

import pytest
import yaml

from vcr.cassette import Cassette
from vcr.handover import (
    HandoverBuildError,
    HandoverConfig,
    Location,
    OwnerIdRules,
    build_handover_cassette,
)
from vcr.matchers import method, path, query
from vcr.persisters.filesystem import FilesystemPersister
from vcr.request import Request
from vcr.serializers import jsonserializer, yamlserializer


def _request(uri):
    return Request("GET", uri, b"", {})


def _response(body=b"ok"):
    return {
        "status": {"code": 200, "message": "OK"},
        "headers": {"X-Owner": ["OW-1"]},
        "body": {"string": body},
    }


def _rules():
    return OwnerIdRules(
        groups=OrderedDict([("OW-1", ("NW-7", "NW-8"))]),
        locations={
            Location("request", "uri"): "OW-1",
            Location("response", "header", "X-Owner"): "OW-1",
            Location("cursor", "query", "owner"): "OW-1",
        },
        group_match_on={"OW-1": (method, path, query)},
    )


def _write_cassette(path, interactions):
    FilesystemPersister.save_cassette(
        str(path),
        {"requests": [req for req, _ in interactions], "responses": [resp for _, resp in interactions]},
        serializer=yamlserializer,
    )


def _config(shift_id, book, page_turns=("p1", "p2", "p3", "continue")):
    return HandoverConfig(shift_id, _rules(), book_path=str(book), page_turns=page_turns)


def test_build_keeps_only_used_and_recorded_without_original_id(tmpdir):
    cassette = tmpdir.join("original.yaml")
    book = tmpdir.join("book.yaml")
    target = tmpdir.join("issued.yaml")
    interactions = [
        (_request("http://api.test/inbox?owner=OW-1&page=1"), _response()),
        (_request("http://api.test/inbox?owner=OW-1&page=2"), _response()),
        (_request("http://api.test/inbox?owner=OW-1&page=3"), _response()),
    ]
    _write_cassette(cassette, interactions)

    with Cassette.use(path=str(cassette), handover=_config("shift-a", book)) as loaded:
        loaded.play_response(_request("http://api.test/inbox?owner=OW-1&page=1"))

    issued, assignment = build_handover_cassette(
        _config("shift-a", book),
        str(cassette),
        str(target),
        FilesystemPersister,
        yamlserializer,
        page_turn="p1",
    )
    saved = yaml.safe_load(target.read())
    assert len(saved["interactions"]) == 1
    assert "OW-1" not in target.read()
    assert saved["interactions"][0]["request"]["uri"] == "http://api.test/inbox?owner=NW-7&page=1"
    assert assignment.group_values == {"OW-1": "NW-7"}


def test_withdrawn_used_page_is_excluded_but_reached_shift_keeps_playback(tmpdir):
    cassette = tmpdir.join("original.yaml")
    book = tmpdir.join("book.yaml")
    target = tmpdir.join("issued.yaml")
    interactions = [
        (_request("http://api.test/inbox?owner=OW-1&page=1"), _response()),
        (_request("http://api.test/inbox?owner=OW-1&page=2"), _response()),
    ]
    _write_cassette(cassette, interactions)

    with Cassette.use(path=str(cassette), handover=_config("shift-a", book)) as shift_a:
        shift_a.play_response(_request("http://api.test/inbox?owner=OW-1&page=1"))
        shift_a.play_response(_request("http://api.test/inbox?owner=OW-1&page=2"))
        shift_a.handover_withdraw(1)
        shift_a.rewind()
        # Reached page stays playable locally after withdrawal.
        shift_a.play_response(_request("http://api.test/inbox?owner=OW-1&page=2"))

    with Cassette.use(path=str(cassette), handover=_config("shift-b", book)) as shift_b:
        assert shift_b._hidden_occurrences == {1}
        assert not shift_b.can_play_response_for(
            _request("http://api.test/inbox?owner=OW-1&page=2")
        )

    build_handover_cassette(
        _config("shift-b", book),
        str(cassette),
        str(target),
        FilesystemPersister,
        yamlserializer,
        page_turn="continue",
    )
    uris = [entry["request"]["uri"] for entry in yaml.safe_load(target.read())["interactions"]]
    assert uris == ["http://api.test/inbox?owner=NW-7&page=1"]


def test_build_is_resumable_after_a_stop_between_journal_steps(tmpdir, monkeypatch):
    cassette = tmpdir.join("original.yaml")
    book = tmpdir.join("book.yaml")
    target = tmpdir.join("issued.yaml")
    _write_cassette(cassette, [(_request("http://api.test/inbox?owner=OW-1&page=1"), _response())])

    with Cassette.use(path=str(cassette), handover=_config("shift-a", book)) as loaded:
        loaded.play_response(_request("http://api.test/inbox?owner=OW-1&page=1"))

    # Simulate a crash after the journal records the rewrite step but before
    # the issued cassette is swapped in.
    from vcr.handover import builder as builder_module

    real_write = builder_module._atomic_write
    calls = {"count": 0}

    def failing_write(target_path, interactions, serializer):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("process stopped before the swap")
        return real_write(target_path, interactions, serializer)

    monkeypatch.setattr(builder_module, "_atomic_write", failing_write)
    with pytest.raises(RuntimeError, match="process stopped"):
        build_handover_cassette(
            _config("shift-a", book),
            str(cassette),
            str(target),
            FilesystemPersister,
            yamlserializer,
            page_turn="p1",
        )
    assert not target.exists()
    journal = yaml.safe_load(book.read())["build"]
    assert "write" not in [step["step"] for step in journal["steps"]]

    monkeypatch.undo()
    issued, _ = build_handover_cassette(
        _config("shift-a", book),
        str(cassette),
        str(target),
        FilesystemPersister,
        yamlserializer,
        page_turn="p1",
    )
    assert target.exists()
    assert len(issued) == 1
    # Re-running the completed build does not duplicate work.
    build_handover_cassette(
        _config("shift-a", book),
        str(cassette),
        str(target),
        FilesystemPersister,
        yamlserializer,
        page_turn="p1",
    )
    assert len(yaml.safe_load(target.read())["interactions"]) == 1


def test_build_failure_saves_progress_and_reports_config_and_page_turn(tmpdir):
    cassette = tmpdir.join("original.yaml")
    book = tmpdir.join("book.yaml")
    target = tmpdir.join("issued.yaml")
    _write_cassette(cassette, [(_request("http://api.test/inbox?owner=OW-1&page=1"), _response())])
    with Cassette.use(path=str(cassette), handover=_config("shift-a", book)) as loaded:
        loaded.play_response(_request("http://api.test/inbox?owner=OW-1&page=1"))

    bad_rules = OwnerIdRules(
        groups=OrderedDict([("OW-1", ())]),
        locations={Location("request", "uri"): "OW-1"},
    )
    bad_config = HandoverConfig(
        "shift-a", bad_rules, book_path=str(book), receive_config="rc-42", page_turns=("turn-7",)
    )
    with pytest.raises(HandoverBuildError) as excinfo:
        build_handover_cassette(
            bad_config,
            str(cassette),
            str(target),
            FilesystemPersister,
            yamlserializer,
            page_turn="turn-7",
        )
    assert excinfo.value.receive_config == "rc-42"
    assert excinfo.value.page_turn == "turn-7"
    failure = yaml.safe_load(book.read())["build"]["failure"]
    assert failure["receive_config"] == "rc-42"
    assert failure["page_turn"] == "turn-7"


def test_undeclared_page_turn_is_rejected(tmpdir):
    cassette = tmpdir.join("original.yaml")
    book = tmpdir.join("book.yaml")
    target = tmpdir.join("issued.yaml")
    _write_cassette(cassette, [(_request("http://api.test/inbox?owner=OW-1&page=1"), _response())])
    with pytest.raises(ValueError):
        build_handover_cassette(
            _config("shift-a", book),
            str(cassette),
            str(target),
            FilesystemPersister,
            yamlserializer,
            page_turn="undeclared",
        )


def test_withdrawn_original_is_replaced_by_a_fresh_same_request_recording(tmpdir):
    cassette = tmpdir.join("original.yaml")
    book = tmpdir.join("book.yaml")
    target = tmpdir.join("issued.yaml")
    same_request = _request("http://api.test/inbox?owner=OW-1&page=1")
    _write_cassette(cassette, [(same_request, _response(b"old"))])

    with Cassette.use(path=str(cassette), handover=_config("shift-a", book)) as shift_a:
        shift_a.play_response(same_request)
        shift_a.handover_withdraw(0)

    with Cassette.use(
        path=str(cassette), handover=_config("shift-b", book), record_mode="new_episodes"
    ) as shift_b:
        # The withdrawn original is hidden, so the same request is recorded anew.
        assert not shift_b.can_play_response_for(same_request)
        shift_b.append(same_request, _response(b"fresh"))

    build_handover_cassette(
        _config("shift-b", book),
        str(cassette),
        str(target),
        FilesystemPersister,
        yamlserializer,
        page_turn="continue",
    )
    saved = yaml.safe_load(target.read())["interactions"]
    # The withdrawn original is gone; the fresh same-request receipt is issued.
    assert len(saved) == 1
    assert saved[0]["response"]["body"]["string"] == "fresh"
    assert saved[0]["request"]["uri"] == "http://api.test/inbox?owner=NW-7&page=1"


def test_recorded_pages_from_both_shifts_are_issued_in_handover_order(tmpdir):
    cassette = tmpdir.join("original.yaml")
    book = tmpdir.join("book.yaml")
    target = tmpdir.join("issued.yaml")
    _write_cassette(cassette, [(_request("http://api.test/inbox?owner=OW-1&page=1"), _response())])

    with Cassette.use(path=str(cassette), handover=_config("shift-a", book)) as shift_a:
        shift_a.play_response(_request("http://api.test/inbox?owner=OW-1&page=1"))
        shift_a.append(_request("http://api.test/new-a?owner=OW-1"), _response(b"a"))

    with Cassette.use(path=str(cassette), handover=_config("shift-b", book)) as shift_b:
        shift_b.append(_request("http://api.test/new-b?owner=OW-1"), _response(b"b"))

    issued, _ = build_handover_cassette(
        _config("shift-b", book),
        str(cassette),
        str(target),
        FilesystemPersister,
        yamlserializer,
        page_turn="p2",
    )
    uris = [interaction.request.uri for interaction in issued]
    assert uris == [
        "http://api.test/inbox?owner=NW-7&page=1",
        "http://api.test/new-a?owner=NW-7",
        "http://api.test/new-b?owner=NW-7",
    ]
    # A later build still keeps both shifts' submissions.
    build_handover_cassette(
        _config("shift-b", book),
        str(cassette),
        str(target),
        FilesystemPersister,
        yamlserializer,
        page_turn="p2",
    )
    assert len(yaml.safe_load(target.read())["interactions"]) == 3


def test_issued_cassette_can_use_a_different_serializer(tmpdir):
    cassette = tmpdir.join("original.yaml")
    book = tmpdir.join("book.yaml")
    target = tmpdir.join("issued.json")
    _write_cassette(cassette, [(_request("http://api.test/inbox?owner=OW-1&page=1"), _response())])

    with Cassette.use(path=str(cassette), handover=_config("shift-a", book)) as loaded:
        loaded.play_response(_request("http://api.test/inbox?owner=OW-1&page=1"))

    build_handover_cassette(
        _config("shift-a", book),
        str(cassette),
        str(target),
        FilesystemPersister,
        yamlserializer,
        page_turn="p1",
        target_serializer=jsonserializer,
    )
    import json

    payload = json.loads(target.read())
    assert payload["interactions"][0]["request"]["uri"] == "http://api.test/inbox?owner=NW-7&page=1"


def test_handover_book_never_leaks_into_request_fields(tmpdir):
    cassette = tmpdir.join("original.yaml")
    book = tmpdir.join("book.yaml")
    target = tmpdir.join("issued.yaml")
    _write_cassette(cassette, [(_request("http://api.test/inbox?owner=OW-1&page=1"), _response())])
    with Cassette.use(path=str(cassette), handover=_config("shift-a", book)) as loaded:
        loaded.play_response(_request("http://api.test/inbox?owner=OW-1&page=1"))
    build_handover_cassette(
        _config("shift-a", book),
        str(cassette),
        str(target),
        FilesystemPersister,
        yamlserializer,
        page_turn="p1",
    )
    issued_text = target.read()
    assert "handover" not in issued_text
    assert "used_occurrences" not in issued_text
    # And the original cassette is still an ordinary VCR file.
    assert len(Cassette.load(path=str(cassette))) == 1
