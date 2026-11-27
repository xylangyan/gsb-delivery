from collections import OrderedDict

import yaml

from vcr.cassette import Cassette
from vcr.handover import HandoverConfig, Location, OfflineScript, OwnerIdRules
from vcr.persisters.filesystem import FilesystemPersister
from vcr.request import Request
from vcr.serializers import yamlserializer


def _request(uri):
    return Request("GET", uri, b"", {})


def _response(body=b"ok"):
    return {
        "status": {"code": 200, "message": "OK"},
        "headers": {},
        "body": {"string": body},
    }


def _rules():
    return OwnerIdRules(
        groups=OrderedDict([("OW-1", ("NW-7",))]),
        locations={Location("request", "uri"): "OW-1"},
    )


def _cassette(tmpdir, interactions):
    path = tmpdir.join("original.yaml")
    FilesystemPersister.save_cassette(
        str(path),
        {"requests": [req for req, _ in interactions], "responses": [resp for _, resp in interactions]},
        serializer=yamlserializer,
    )
    return path


def _config(shift_id, book, page_turns=("turn-1", "turn-2", "retry-1")):
    return HandoverConfig(
        shift_id,
        _rules(),
        book_path=str(book),
        page_turns=page_turns,
        retries=("retry-1",),
    )


def test_original_cassette_file_is_never_modified(tmpdir):
    path = _cassette(tmpdir, [(_request("http://api.test/inbox?owner=OW-1&page=1"), _response())])
    book = tmpdir.join("book.yaml")
    before = path.read()

    with Cassette.use(path=str(path), handover=_config("shift-a", book)) as cassette:
        cassette.play_response(_request("http://api.test/inbox?owner=OW-1&page=1"))
        cassette.rewind()
        cassette.play_response(_request("http://api.test/inbox?owner=OW-1&page=1"))
        cassette.append(_request("http://api.test/new?owner=OW-1"), _response(b"new"))

    assert path.read() == before
    assert book.exists()
    data = yaml.safe_load(book.read())
    # One used occurrence however many times replayed; the new page is recorded.
    assert data["shifts"]["shift-a"]["used_occurrences"] == [0]
    assert len(data["shifts"]["shift-a"]["recorded"]) == 1


def test_each_shift_closes_independently_without_waiting_for_the_other(tmpdir):
    path = _cassette(
        tmpdir,
        [
            (_request("http://api.test/inbox?owner=OW-1&page=1"), _response()),
            (_request("http://api.test/inbox?owner=OW-1&page=2"), _response()),
        ],
    )
    book = tmpdir.join("book.yaml")

    with Cassette.use(path=str(path), handover=_config("shift-a", book)) as shift_a:
        shift_a.play_response(_request("http://api.test/inbox?owner=OW-1&page=1"))

    data = yaml.safe_load(book.read())
    assert data["shifts"]["shift-a"]["closed"] is True
    assert "shift-b" not in data["shifts"]

    with Cassette.use(path=str(path), handover=_config("shift-b", book)) as shift_b:
        shift_b.play_response(_request("http://api.test/inbox?owner=OW-1&page=2"))

    data = yaml.safe_load(book.read())
    assert set(data["shifts"]) == {"shift-a", "shift-b"}
    assert data["shifts"]["shift-a"]["used_occurrences"] == [0]
    assert data["shifts"]["shift-b"]["used_occurrences"] == [1]


def test_reopen_resumes_registered_progress_and_does_not_rerecord(tmpdir):
    path = _cassette(tmpdir, [(_request("http://api.test/inbox?owner=OW-1&page=1"), _response())])
    book = tmpdir.join("book.yaml")
    new_request = _request("http://api.test/new?owner=OW-1")

    with Cassette.use(path=str(path), handover=_config("shift-a", book)) as shift_a:
        shift_a.play_response(_request("http://api.test/inbox?owner=OW-1&page=1"))
        shift_a.append(new_request, _response(b"new"))

    with Cassette.use(path=str(path), handover=_config("shift-a", book)) as reopened:
        assert reopened.handover_cursor == 1
        # The previously recorded page is replayable from the book.
        assert reopened.can_play_response_for(new_request)
        reopened.append(new_request, _response(b"new"))  # resumed re-run, suppressed

    recorded = yaml.safe_load(book.read())["shifts"]["shift-a"]["recorded"]
    assert len(recorded) == 1


def test_offline_script_only_uses_declared_turns_and_never_connects(tmpdir):
    path = _cassette(tmpdir, [(_request("http://api.test/inbox?owner=OW-1&page=1"), _response())])
    book = tmpdir.join("book.yaml")
    config = _config("shift-a", book)

    with Cassette.use(path=str(path), handover=config, record_mode="new_episodes") as cassette:
        script = OfflineScript(
            config,
            [
                ("turn-1", _request("http://api.test/inbox?owner=OW-1&page=1")),
                ("turn-2", _request("http://api.test/inbox?owner=OW-1&page=2")),
            ],
        )
        results = script.run(cassette)

    assert [page_turn for page_turn, _ in results] == ["turn-1", "turn-2"]
    assert results[1][1]["headers"]["X-Page-Turn"] == ["turn-2"]
    recorded = yaml.safe_load(book.read())["shifts"]["shift-a"]["recorded"]
    assert len(recorded) == 1


def test_offline_script_rejects_undeclared_turns_and_missing_declarations(tmpdir):
    path = _cassette(tmpdir, [])
    book = tmpdir.join("book.yaml")
    config = _config("shift-a", book)
    try:
        OfflineScript(config, [("turn-9", _request("http://api.test/x"))])
    except ValueError:
        pass
    else:
        raise AssertionError("undeclared turn should be rejected")

    no_turns = HandoverConfig("shift-a", _rules(), book_path=str(book))
    with Cassette.use(path=str(path), handover=no_turns) as cassette:
        try:
            OfflineScript(no_turns, [("turn-1", _request("http://api.test/x"))]).run(cassette)
        except ValueError:
            pass
        else:
            raise AssertionError("a script without declared finite turns should be rejected")
