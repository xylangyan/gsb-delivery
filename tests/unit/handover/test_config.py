import pytest

from vcr.handover.config import (
    HandoverConfig,
    HandoverConfigError,
    Location,
    OwnerIdRules,
    Permissions,
    Permit,
    query_parameter,
    replace_query_parameter,
)
from collections import OrderedDict


def test_location_parse():
    assert Location.parse("request.uri") == Location("request", "uri")
    assert Location.parse("request.header.X-Owner") == Location("request", "header", "X-Owner")
    assert Location.parse("response.body.data.owner") == Location(
        "response", "body", "data.owner"
    )
    assert Location.parse("cursor.query.page") == Location("cursor", "query", "page")
    assert str(Location("cursor", "body", "next")) == "cursor.body.next"


@pytest.mark.parametrize("text", ["uri", "request.unknown", "cursor.header.x", "response.query.x"])
def test_location_parse_rejects_bad_locations(text):
    with pytest.raises(HandoverConfigError):
        Location.parse(text)


def test_rules_reject_unknown_group_reference():
    with pytest.raises(HandoverConfigError):
        OwnerIdRules(
            groups=OrderedDict([("OW-1", ("NW-7",))]),
            locations={Location("request", "uri"): "OW-9"},
        )


def test_rules_reject_permit_on_a_group_location():
    location = Location("request", "header", "X-Owner")
    with pytest.raises(HandoverConfigError):
        OwnerIdRules(
            groups=OrderedDict([("OW-1", ("NW-7",))]),
            locations={location: "OW-1"},
            permits=(Permit(location, "OW-1", "NW-9"),),
        )


def test_permit_needs_replacement():
    with pytest.raises(HandoverConfigError):
        Permit(Location("request", "header", "X-Trace"), "trace-1", "")


def test_replace_query_parameter_keeps_position():
    uri = "http://api.test/inbox?owner=OW-1&page=2"
    assert replace_query_parameter(uri, "owner", "NW-7") == "http://api.test/inbox?owner=NW-7&page=2"
    assert query_parameter(uri, "owner") == "OW-1"
    assert replace_query_parameter("http://api.test/inbox?page=2", "owner", "NW-7") == (
        "http://api.test/inbox?page=2&owner=NW-7"
    )


def test_config_default_book_path_is_sibling(tmpdir):
    rules = OwnerIdRules(OrderedDict([("OW-1", ("NW-7",))]), {})
    config = HandoverConfig("shift-a", rules)
    assert config.book_path_for(str(tmpdir.join("c.yaml"))) == str(tmpdir.join("c.yaml.handover.yaml"))


def test_config_requires_shift_id():
    rules = OwnerIdRules(OrderedDict([("OW-1", ("NW-7",))]), {})
    with pytest.raises(HandoverConfigError):
        HandoverConfig("", rules)


def test_permissions_enforce_finite_script():
    rules = OwnerIdRules(OrderedDict([("OW-1", ("NW-7",))]), {})
    permissions = Permissions(rules, page_turns=("p1", "p2"), retries=("r1",))
    permissions.validate_page_turn("p1")
    permissions.validate_retry("r1")
    with pytest.raises(HandoverConfigError):
        permissions.validate_page_turn("p3")
    with pytest.raises(HandoverConfigError):
        permissions.validate_retry("r2")
