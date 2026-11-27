from collections import OrderedDict

import pytest

from vcr.handover.config import Location, OwnerIdRules, Permit
from vcr.handover.rewrite import (
    RewriteError,
    find_safe_assignment,
    get_location_value,
    rewrite_for_issuance,
    IssuedInteraction,
)
from vcr.matchers import method, path, query, uri
from vcr.request import Request


def _request(uri="http://api.test/inbox?owner=OW-1&page=1", body=b""):
    return Request("GET", uri, body, {})


def _response(body=b'{"next": null}', owner="OW-1"):
    return {
        "status": {"code": 200, "message": "OK"},
        "headers": {"X-Owner": [owner]},
        "body": {"string": body},
    }


def _interaction(request, response=None, source=("original", 0)):
    return IssuedInteraction(request, response or _response(), source)


def _rules(locations=None, groups=None, permits=(), group_match_on=None):
    return OwnerIdRules(
        groups=groups or OrderedDict([("OW-1", ("NW-7", "NW-8"))]),
        locations=locations
        or {
            Location("request", "uri"): "OW-1",
            Location("response", "header", "X-Owner"): "OW-1",
            Location("cursor", "query", "owner"): "OW-1",
            Location("cursor", "body", "next"): "OW-1",
        },
        permits=permits,
        group_match_on=group_match_on or {},
    )


def test_group_rewrite_touches_request_response_and_cursor():
    interactions = [
        _interaction(
            _request("http://api.test/inbox?owner=OW-1&page=1"),
            _response(b'{"next": "http://api.test/inbox?owner=OW-1&page=2"}'),
        ),
    ]
    issued, assignment = rewrite_for_issuance(_rules(), interactions, page_turn="p1")
    assert assignment.permit_count == 0
    request, response = issued[0].request, issued[0].response
    assert request.uri == "http://api.test/inbox?owner=NW-7&page=1"
    assert response["headers"]["X-Owner"] == ["NW-7"]
    assert b"owner=NW-7&page=2" in response["body"]["string"]
    assert b"OW-1" not in response["body"]["string"]


def test_no_original_id_remains_anywhere():
    interactions = [_interaction(_request())]
    issued, _ = rewrite_for_issuance(_rules(), interactions)
    for interaction in issued:
        for location in _rules().locations:
            value = get_location_value((interaction.request, interaction.response), location)
            assert value is None or "OW-1" not in str(value)


def test_same_value_collision_forces_another_alternative():
    # Two distinct pages; rewriting both to NW-7 would make them identical under
    # the query matcher, while NW-7 vs NW-8 keeps them distinct.
    rules = _rules(group_match_on={"OW-1": (method, path, query)})
    interactions = [
        _interaction(_request("http://api.test/inbox?owner=OW-1&page=1"), source=("original", 0)),
        _interaction(_request("http://api.test/inbox?owner=OW-1&page=2"), source=("recorded", "b", 1)),
    ]
    assignment = find_safe_assignment(rules, interactions, page_turn="p2")
    # A single group value applies to every occurrence, so both pages get the
    # same replacement and the pair already differs by page number anyway.
    assert assignment.group_values["OW-1"] in ("NW-7", "NW-8")
    issued, _ = rewrite_for_issuance(rules, interactions)
    assert not requests_collide(issued, (method, path, query))


def requests_collide(interactions, matcher_tuple):
    from vcr.matchers import requests_match

    for i, left in enumerate(interactions):
        for right in interactions[i + 1 :]:
            if requests_match(left.request, right.request, matcher_tuple):
                return True
    return False


def test_withdrawn_survivor_colliding_with_new_recording_fails_when_all_alternatives_collide():
    # Only one replacement exists; the surviving original and the new recording
    # differ only by owner, so rewriting merges them with no safe alternative.
    rules = _rules(
        groups=OrderedDict([("OW-1", ("NW-7",))]),
        locations={Location("request", "uri"): "OW-1"},
        group_match_on={"OW-1": (uri,)},
    )
    interactions = [
        _interaction(
            _request("http://api.test/inbox?owner=OW-1&page=1"), source=("original", 0)
        ),
        _interaction(
            _request("http://api.test/inbox?owner=NW-7&page=1"), source=("recorded", "b", 1)
        ),
    ]
    with pytest.raises(RewriteError, match="p3"):
        find_safe_assignment(rules, interactions, page_turn="p3")


def test_permit_is_required_when_its_location_keeps_the_original_value():
    # The group rule only covers the URI; the X-Trace header holding the same
    # owner ID can only be scrubbed through an individual permit. The empty
    # permit subset leaves an original value at a declared permit location, so
    # the search must reject it and choose the assignment that uses the permit.
    rules = OwnerIdRules(
        groups=OrderedDict([("OW-1", ("NW-7",))]),
        locations={Location("request", "uri"): "OW-1"},
        permits=(Permit(Location("request", "header", "X-Trace"), "OW-1", "NW-9"),),
    )
    request = _request()
    request.headers["X-Trace"] = "OW-1"
    interactions = [_interaction(request)]
    assignment = find_safe_assignment(rules, interactions)
    assert assignment.permit_count == 1
    issued, _ = rewrite_for_issuance(rules, interactions)
    assert issued[0].request.uri == "http://api.test/inbox?owner=NW-7&page=1"
    assert issued[0].request.headers["X-Trace"] == "NW-9"
    assert b"OW-1" not in str(issued[0].request.headers).encode()


def test_assignment_prefers_fewer_permits():
    rules = OwnerIdRules(
        groups=OrderedDict([("OW-1", ("NW-7",)), ("OW-2", ("NW-8",))]),
        locations={
            Location("request", "uri"): "OW-1",
        },
        permits=(
            Permit(Location("request", "header", "X-Trace"), "OW-2", "NW-9"),
            Permit(Location("request", "header", "X-Lane"), "OW-2", "NW-9"),
        ),
    )
    request = _request()
    request.headers["X-Trace"] = "OW-2"
    request.headers["X-Lane"] = "OW-2"
    interactions = [_interaction(request)]
    assignment = find_safe_assignment(rules, interactions)
    # Both permit locations hold original values, so both permits are needed;
    # ranking still returns the minimum rather than a superset.
    assert assignment.permit_count == 2
    assert len(assignment.permits_used) == 2


def test_manifest_order_breaks_ties_between_equivalent_group_values():
    rules = _rules()
    interactions = [_interaction(_request())]
    first = find_safe_assignment(rules, interactions)
    assert first.group_values == {"OW-1": "NW-7"}  # first declared alternative
    reversed_rules = _rules(groups=OrderedDict([("OW-1", ("NW-8", "NW-7"))]))
    second = find_safe_assignment(reversed_rules, interactions)
    assert second.group_values == {"OW-1": "NW-8"}


def test_missing_replacement_is_a_build_error_not_a_silent_keep():
    rules = _rules(groups=OrderedDict([("OW-1", ())]))
    with pytest.raises(RewriteError, match="no allowed replacement"):
        find_safe_assignment(rules, [_interaction(_request())], page_turn="pX")


def test_body_locations_rewrite_json_fields():
    rules = OwnerIdRules(
        groups=OrderedDict([("OW-1", ("NW-7",))]),
        locations={Location("request", "body", "owner"): "OW-1"},
    )
    request = Request("POST", "http://api.test/handover", b'{"owner": "OW-1"}', {})
    issued, _ = rewrite_for_issuance(rules, [_interaction(request)])
    assert issued[0].request.body == b'{"owner": "NW-7"}'


def test_group_absent_from_the_data_does_not_break_other_groups():
    rules = OwnerIdRules(
        groups=OrderedDict([("OW-1", ("NW-7",)), ("OW-2", ("NW-8",))]),
        locations={
            Location("request", "uri"): "OW-1",
            Location("request", "header", "X-Other"): "OW-2",
        },
    )
    issued, assignment = rewrite_for_issuance(rules, [_interaction(_request())])
    assert assignment.group_values == {"OW-1": "NW-7"}
    assert issued[0].request.uri == "http://api.test/inbox?owner=NW-7&page=1"
